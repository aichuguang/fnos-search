from __future__ import annotations

import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.database import Database
from fnos_media_import.importers.cloud139 import Cloud139Importer
from fnos_media_import.organizer.tmdb import tmdb_match_rejection_reason
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.repositories.organizer_repository import _organizer_progress
from fnos_media_import.services.public_submission_preflight_service import (
    PublicSubmissionPreflightService,
)
from fnos_media_import.services.public_submission_service import (
    PublicSubmissionDependencies,
    PublicSubmissionService,
)
from fnos_media_import.services.rclone_live_progress import RcloneLiveProgress
from fnos_media_import.services.transfer_status_projection import project_transfer_status
from fnos_media_import.services.worker_lane_runtime import WorkerLaneRuntime


class TmdbHardValidationTests(unittest.TestCase):
    def test_wrong_year_type_and_near_title_are_rejected(self) -> None:
        cases = [
            ("幕府将军", "2024", "tv", {"title": "幕府将军", "year": "1980", "media_type": "tv"}),
            ("Voice", "2017", "tv", {"title": "The Voice", "year": "2017", "media_type": "tv"}),
            ("鬼怪", "2016", "tv", {"title": "鬼怪异闻录", "year": "2016", "media_type": "tv"}),
            ("少女歌剧", "2018", "anime", {"title": "歌剧少女！！", "year": "2018", "media_type": "tv"}),
            ("Shogun", "2024", "tv", {"title": "Shogun", "year": "2024", "media_type": "movie"}),
        ]
        for title, year, category, result in cases:
            with self.subTest(title=title, result=result["title"]):
                self.assertTrue(tmdb_match_rejection_reason(title, year, category, result))

    def test_exact_title_and_close_year_are_accepted(self) -> None:
        self.assertEqual(
            tmdb_match_rejection_reason(
                "幕府将军", "2024", "tv",
                {"title": "幕府将军", "year": "2024", "media_type": "tv"},
            ),
            "",
        )
        self.assertEqual(
            tmdb_match_rejection_reason(
                "鬼怪", "2016", "tv",
                {"title": "孤单又灿烂的神：鬼怪", "year": "2016", "media_type": "tv"},
            ),
            "",
        )


class OrganizerProgressTests(unittest.TestCase):
    def test_move_progress_is_readable(self) -> None:
        progress = _organizer_progress({
            "status": "executing",
            "operations": [
                {"type": "move_file", "status": "done"},
                {"type": "move_file", "status": "done"},
                {"type": "move_file", "status": "pending"},
                {"type": "create_dir", "status": "done"},
            ],
        })
        self.assertEqual(progress["done"], 2)
        self.assertEqual(progress["total"], 3)
        self.assertEqual(progress["text"], "已移动 2/3")


class WorkerLeaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"hardening-{uuid.uuid4().hex}.db"
        local = Database(self.db_path)
        local.init_schema()
        self.database = RepositorySet.from_database(local)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()

    def _table_exists(self, name: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        return row is not None

    def test_expired_task_is_requeued_for_a_later_attempt(self) -> None:
        task_id, _ = self.database.worker_tasks.enqueue(
            "legacy_unhandled_type", {}, "lease-recovery", max_attempts=3,
        )
        self.database.worker_tasks.claim("worker-a", lease_seconds=30)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE worker_tasks SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (task_id,),
            )
        recovered = self.database.worker_tasks.recover_expired_leases()
        task = self.database.worker_tasks.get(task_id)
        self.assertEqual(recovered["requeued"], 1)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["attempts"], 1)
        self.assertIsNone(task["owner_id"])
        self.assertIsNone(task["lease_expires_at"])


class WorkerLaneIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"lanes-{uuid.uuid4().hex}.db"
        local = Database(self.db_path)
        local.init_schema()
        self.database = RepositorySet.from_database(local)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()

    def test_transfer_finishes_while_organizer_handler_is_blocked(self) -> None:
        organizer_started = threading.Event()
        release_organizer = threading.Event()
        transfer_done = threading.Event()

        def organizer(_payload, _task):
            organizer_started.set()
            release_organizer.wait(5)
            return {}

        def transfer(_payload, _task):
            transfer_done.set()
            return {}

        runtime = WorkerLaneRuntime(
            repository=self.database.worker_tasks,
            owner_id="test-worker",
            lane_handlers={
                "organizer": {"organizer_process": organizer},
                "transfer": {"import_transfer_execute": transfer},
            },
            poll_seconds=0.02,
            lease_seconds=30,
        )
        self.database.worker_tasks.enqueue("organizer_process", {}, "organizer-block")
        self.database.worker_tasks.enqueue("import_transfer_execute", {}, "transfer-quick")
        runtime.start()
        try:
            self.assertTrue(organizer_started.wait(2))
            self.assertTrue(transfer_done.wait(2), "transfer lane was blocked by organizer work")
        finally:
            release_organizer.set()
            runtime.shutdown(2)


class Cloud139EmptyShareTests(unittest.TestCase):
    def test_recursive_scan_counts_nested_video_not_root_folder(self) -> None:
        class FakeCmcc:
            def list_share_dir(self, _link, *, password, pdir_fid):
                if pdir_fid == "root":
                    return [{"fid": "folder-1", "name": "Season 1", "is_dir": True}]
                return [
                    {"fid": "video-1", "name": "S01E01.mkv", "size": 1024 * 1024},
                    {"fid": "video-2", "name": "S01E02.mp4", "size": 2 * 1024 * 1024},
                ]

        importer = Cloud139Importer({"check_before_save": False}, {"enabled": True, "access_token": "x"})
        importer._cmcc_client = FakeCmcc()
        result = importer._scan_share_videos("share", "", "root")
        self.assertTrue(result["complete"])
        self.assertEqual(result["video_file_count"], 2)
        self.assertEqual(result["total_size"], 3 * 1024 * 1024)

    def test_preflight_rejects_zero_video_file_count(self) -> None:
        result = PublicSubmissionPreflightService._checked(
            "mobile", True, "ok", {"summary": {"file_count": 0}},
        )
        self.assertFalse(result["allowed"])
        self.assertIn("没有可入库的视频文件", result["message"])


class SubmissionTitleRefreshTests(unittest.TestCase):
    def test_changed_title_bypasses_old_duplicate(self) -> None:
        duplicate = {"id": 1, "title": "幕府将军 1980", "status": "review",
                     "request_token": "old", "public_status": "review"}
        service = PublicSubmissionService(PublicSubmissionDependencies(
            queries=SimpleNamespace(find_recent_by_url=lambda **_kwargs: duplicate),
            commands=SimpleNamespace(),
            sync_request=lambda item: item,
            public_status=lambda status: status,
            public_request=lambda item: item,
        ))
        same = service.duplicate_response(
            source_url="https://example.com/s", category="tv", within_minutes=60,
            enabled=True, scoped_selection=False, title="幕府将军 1980",
        )
        changed = service.duplicate_response(
            source_url="https://example.com/s", category="tv", within_minutes=60,
            enabled=True, scoped_selection=False, title="幕府将军 2024",
        )
        self.assertTrue(same and same["duplicate"])
        self.assertIsNone(changed)


class TransferAggregateTests(unittest.TestCase):
    def test_expected_count_and_done_count_are_projected(self) -> None:
        item = project_transfer_status(
            [{
                "job_id": 7, "stage": "transferring", "completed": False,
                "updated_at": "2026-11-07T10:00:00Z",
                "upload_files_done": 12, "upload_bytes_done": 38 * 1024**3,
                "upload": {"state": "running", "bytes_done": 1, "bytes_total": 2},
            }],
            [{
                "job_id": 7, "title": "Demo", "job_status": "transferring",
                "execution_phase": "transferring", "execution_plan_json": (
                    '{"verification":{"expected_file_count":91,'
                    '"expected_total_size":144000000000}}'
                ),
            }],
        )[0]
        self.assertEqual(item["file_progress"]["done"], 12)
        self.assertEqual(item["file_progress"]["total"], 91)
        self.assertIn("已传 12/91 个文件", item["progress_text"])
        self.assertIn("38.0G/134.1G", item["progress_text"])


if __name__ == "__main__":
    unittest.main()
