from __future__ import annotations

import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fnos_media_import.app import _public_adapter_capabilities
from fnos_media_import.content_guard import evaluate_submission_content_risk
from fnos_media_import.database import Database
from fnos_media_import.services.rclone_history_repair_worker import RcloneHistoryRepairWorker
from fnos_media_import.services.rclone_waiting_job_recovery_service import RcloneWaitingJobRecoveryService


class _RecoveryDatabase:
    def __init__(self) -> None:
        self.jobs = [{"id": value} for value in range(60, 0, -1)]
        self.runs = [{"id": value, "status": "success", "exit_code": 0} for value in range(600, 0, -1)]
        self.job_offsets: list[int] = []
        self.run_offsets: list[int] = []

    def list_jobs(self, *, limit: int, offset: int, status: str, source_type: str):
        if status != "waiting_transfer" or source_type != "quark":
            return []
        self.job_offsets.append(offset)
        return self.jobs[offset : offset + limit]

    def list_rclone_runs(self, *, limit: int, offset: int):
        self.run_offsets.append(offset)
        return self.runs[offset : offset + limit]


class RcloneRecoveryPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _RecoveryDatabase()
        self.service = RcloneWaitingJobRecoveryService(
            database=self.database,
            state_lock=threading.Lock(),
            is_running=lambda: False,
            file_identity=lambda _event: "",
            is_pollution_file=lambda _name: False,
            finalize_run=lambda _run_id, _exit_code: None,
        )

    def test_waiting_jobs_scan_all_pages_instead_of_starving_old_jobs(self) -> None:
        jobs = self.service._waiting_jobs(limit=1)

        self.assertEqual(len(jobs), 60)
        self.assertEqual(self.database.job_offsets, [0, 50])

    def test_run_lookup_pages_until_requested_old_run_is_found(self) -> None:
        runs = self.service._runs_by_ids([1])

        self.assertEqual(runs[1]["id"], 1)
        self.assertEqual(self.database.run_offsets, [0, 500])


class RcloneOrphanEventPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"rclone-orphan-pagination-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()
        self.job_id, created = self.database.create_job(
            {
                "title": "Old match",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": "https://pan.quark.cn/s/old-match",
                "target_route": "quark_to_mobile",
                "target_path": "/移动云/电视剧/Old match",
                "status": "waiting_transfer",
                "idempotency_key": f"orphan-pagination:{uuid.uuid4().hex}",
            }
        )
        self.assertTrue(created)
        self.service = RcloneWaitingJobRecoveryService(
            database=self.database,
            state_lock=threading.Lock(),
            is_running=lambda: False,
            file_identity=lambda _event: "",
            is_pollution_file=lambda _name: False,
            finalize_run=lambda _run_id, _exit_code: None,
        )

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_old_match_is_relinked_after_more_than_one_page_of_new_orphans(self) -> None:
        old_event_id = self.database.add_rclone_file_event(
            status="done",
            level="info",
            category="电视剧",
            filename="Old.Match.S01E01.mkv",
            source_path="quark/Old Match/Old.Match.S01E01.mkv",
            target_path="webdav/电视剧/Old Match/Old.Match.S01E01.mkv",
        )
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO rclone_file_events
                (run_id, job_id, status, level, category, filename, source_path,
                 target_path, message, raw_data, created_at)
                VALUES (NULL, NULL, 'done', 'info', '电影', ?, '', '', '', NULL, ?)
                """,
                [
                    (f"unmatched-{index:03d}.mkv", "2026-07-31T00:00:00Z")
                    for index in range(501)
                ],
            )

        relinked = self.service._relink_unmatched_events(limit=1)

        self.assertEqual(relinked, [old_event_id])
        self.assertEqual(
            self.database.get_rclone_file_event(old_event_id)["job_id"],
            self.job_id,
        )


class _LeaseDatabase:
    def __init__(self) -> None:
        self.release_count = 0

    @staticmethod
    def acquire_scheduler_lease(_name: str, _owner: str, _seconds: int) -> bool:
        return True

    def release_scheduler_lease(self, _name: str, _owner: str) -> None:
        self.release_count += 1


class RcloneRecoveryWorkerTests(unittest.TestCase):
    def test_worker_repeats_recovery_until_shutdown(self) -> None:
        database = _LeaseDatabase()
        reached_two = threading.Event()
        calls = 0

        def repair(*, limit: int):
            nonlocal calls
            calls += 1
            if calls >= 2:
                reached_two.set()
            return {"success": True, "run_ids": [], "limit": limit}

        worker = RcloneHistoryRepairWorker(
            database=database,
            owner_id="worker-a",
            repair=repair,
            log=lambda _message: None,
            limit=5,
            interval_seconds=0.05,
        )
        worker.start()
        self.assertTrue(reached_two.wait(1.0))
        worker.shutdown()

        self.assertGreaterEqual(calls, 2)
        self.assertFalse(worker.thread and worker.thread.is_alive())

    def test_lease_acquisition_error_does_not_kill_periodic_thread(self) -> None:
        database = _LeaseDatabase()
        original_acquire = database.acquire_scheduler_lease
        acquire_calls = 0

        def flaky_acquire(name: str, owner: str, seconds: int) -> bool:
            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 1:
                raise RuntimeError("database busy")
            return original_acquire(name, owner, seconds)

        database.acquire_scheduler_lease = flaky_acquire
        repaired = threading.Event()
        worker = RcloneHistoryRepairWorker(
            database=database,
            owner_id="worker-a",
            repair=lambda **_kwargs: repaired.set() or {"success": True, "run_ids": []},
            log=lambda _message: None,
            interval_seconds=0.05,
        )
        worker.start()
        self.assertTrue(repaired.wait(1.0))
        worker.shutdown()

        self.assertGreaterEqual(acquire_calls, 2)

    def test_shutdown_does_not_release_lease_while_repair_is_still_running(self) -> None:
        database = _LeaseDatabase()
        repair_started = threading.Event()
        allow_repair_to_finish = threading.Event()

        def blocking_repair(*, limit: int):
            repair_started.set()
            allow_repair_to_finish.wait(5.0)
            return {"success": True, "run_ids": [], "limit": limit}

        worker = RcloneHistoryRepairWorker(
            database=database,
            owner_id="worker-a",
            repair=blocking_repair,
            log=lambda _message: None,
            interval_seconds=0.05,
        )
        worker.start()
        try:
            self.assertTrue(repair_started.wait(1.0))
            worker.shutdown()

            self.assertTrue(worker.thread and worker.thread.is_alive())
            self.assertEqual(database.release_count, 0)
        finally:
            allow_repair_to_finish.set()
            if worker.thread:
                worker.thread.join(timeout=2.0)

        self.assertFalse(worker.thread and worker.thread.is_alive())
        self.assertEqual(database.release_count, 1)


class SearchCacheCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"search-cache-cleanup-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_new_cache_write_prunes_expired_rows(self) -> None:
        expired_id = self.database.save_search_cache(
            "expired",
            keyword="old",
            item={"title": "Old", "url": "https://example.invalid/old"},
            expires_minutes=-1,
        )

        self.database.save_search_cache(
            "active",
            keyword="new",
            item={"title": "New", "url": "https://example.invalid/new"},
            expires_minutes=60,
        )

        with self.database.connect() as connection:
            expired = connection.execute(
                "SELECT 1 FROM search_cache WHERE id = ?", (expired_id,)
            ).fetchone()
            total = int(connection.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0])
        self.assertIsNone(expired)
        self.assertEqual(total, 1)


class ContentGuardFailurePolicyTests(unittest.TestCase):
    def test_ai_failure_defaults_to_manual_review(self) -> None:
        config = {
            "content_review": {
                "enabled": True,
                "keyword_enabled": False,
                "bt_ai_enabled": True,
            },
            "ai": {
                "enabled": True,
                "base_url": "https://example.invalid/v1",
                "api_key": "test-key",
                "model": "test-model",
            },
        }
        with patch(
            "fnos_media_import.content_guard._ai_review",
            side_effect=ConnectionResetError("connection reset"),
        ):
            result = evaluate_submission_content_risk(
                config=config,
                title="Normal Show",
                source_type="magnet",
                source_url="magnet:?xt=urn:btih:abc",
                files=[{"name": "Normal.Show.S01E01.mkv"}],
            )

        self.assertTrue(result["review_required"])
        self.assertEqual(result["stage"], "ai_failure")
        self.assertEqual(result["ai_result"]["fallback"], "review")


class PublicAdapterCapabilityTests(unittest.TestCase):
    @staticmethod
    def _cloud189(items: list[dict]) -> dict:
        return next(item for item in items if item.get("key") == "cloud189")

    def test_unconfigured_cloud189_webhook_is_not_advertised_as_enabled(self) -> None:
        item = self._cloud189(
            _public_adapter_capabilities(
                {
                    "routes": {"cloud189": {"enabled": True}},
                    "cloud189": {},
                    "cloud139": {},
                    "cmcc_upload": {},
                    "sixpan": {},
                    "quark": {},
                }
            )
        )

        self.assertTrue(item["route_enabled"])
        self.assertFalse(item["configured"])
        self.assertFalse(item["enabled"])
        self.assertFalse(item["capabilities"]["submit"])

    def test_configured_cloud189_webhook_exposes_submit_capability(self) -> None:
        item = self._cloud189(
            _public_adapter_capabilities(
                {
                    "routes": {"cloud189": {"enabled": True}},
                    "cloud189": {"endpoint": "https://example.invalid/import"},
                    "cloud139": {},
                    "cmcc_upload": {},
                    "sixpan": {},
                    "quark": {},
                }
            )
        )

        self.assertTrue(item["configured"])
        self.assertTrue(item["enabled"])
        self.assertTrue(item["capabilities"]["submit"])


if __name__ == "__main__":
    unittest.main()
