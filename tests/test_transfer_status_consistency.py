from __future__ import annotations

import copy
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fnos_media_import.repositories.job_query_repository import JobQueryRepository
from fnos_media_import.services.rclone_admin_service import RcloneAdminQueryDependencies, RcloneAdminQueryService
from fnos_media_import.services.transfer_status_projection import project_transfer_status


def observation(job_id=1, **changes):
    return {
        "job_id": job_id, "run_id": 8, "stage": "upload", "completed": False,
        "updated_at": "2026-10-14T01:00:01Z", "age_seconds": 3, "stale": False,
        "upload": {"state": "running", "scope": "file", "filename": "movie.mkv", "percent": 45,
                   "bytes_done": 450, "bytes_total": 1000, "speed_bps": 100, "eta_seconds": 6},
        **changes,
    }


def record(job_id=1, job_status="transferring", phase="transferring", **changes):
    return {
        "job_id": job_id, "title": "Movie", "job_status": job_status, "execution_phase": phase,
        "job_updated_at": "2026-10-14T01:00:00Z", "state_updated_at": "2026-10-14T01:00:00Z",
        "final_verified": int(phase == "completed"), "app_acked": int(phase == "completed"),
        **changes,
    }


class TransferStatusProjectionTests(unittest.TestCase):
    def test_lost_completed_event_cannot_leave_done_task_running(self):
        item = project_transfer_status([observation()], [record(job_status="done", phase="completed")])[0]
        self.assertTrue(item["completed"])
        self.assertEqual(item["stage"], "completed")
        self.assertEqual(item["job_status"], "done")
        self.assertIsNone(item["upload"]["percent"])

    def test_cancel_failure_review_and_rollback_override_old_completed_telemetry(self):
        for job_status, expected in (("cancelled", "cancelled"), ("failed", "failed"),
                                     ("review", "manual_review"), ("rejected", "cancelled")):
            with self.subTest(job_status=job_status):
                item = project_transfer_status(
                    [observation(completed=True, stage="completed")],
                    [record(job_status=job_status, phase="completed", job_error="Current durable reason")],
                )[0]
                self.assertFalse(item["completed"])
                self.assertEqual(item["stage"], expected)
                self.assertEqual(item["job_status"], job_status)
                self.assertIsNone(item["upload"]["percent"])
                if job_status in {"review", "failed"}:
                    self.assertEqual(item["message"], "Current durable reason")

    def test_retry_clears_previous_attempt_metrics_and_batch_identity(self):
        item = project_transfer_status(
            [observation(completed=True, stage="completed")],
            [record(job_status="retry_requested", phase="retry_wait")],
        )[0]
        self.assertFalse(item["completed"])
        self.assertEqual(item["stage"], "retry_wait")
        self.assertIsNone(item["run_id"])
        self.assertIsNone(item["upload"]["percent"])

    def test_current_file_metrics_are_preserved_without_mutating_worker_snapshot(self):
        live = observation()
        before = copy.deepcopy(live)
        item = project_transfer_status([live], [record()])[0]
        self.assertEqual(item["stage"], "upload")
        self.assertEqual(item["upload"], live["upload"])
        self.assertEqual(item["updated_at"], live["updated_at"])
        self.assertEqual(live, before)
        item["upload"]["percent"] = 88
        self.assertEqual(live["upload"]["percent"], 45)

    def test_worker_restart_restores_existing_tasks_without_fabricating_progress(self):
        items = project_transfer_status([], [record(), record(2, "organizing", "organizing")])
        self.assertEqual([item["job_id"] for item in items], [1, 2])
        self.assertEqual(items[0]["stage"], "transferring")
        self.assertEqual(items[1]["stage"], "organizing")
        for item in items:
            self.assertFalse(item["completed"])
            for channel in ("download", "hash", "upload"):
                self.assertIsNone(item[channel]["percent"])
                self.assertIsNone(item[channel]["bytes_done"])

    def test_one_hundred_percent_or_worker_completion_is_not_whole_job_completion(self):
        live = observation()
        live["upload"]["percent"] = 100
        self.assertFalse(project_transfer_status([live], [record()])[0]["completed"])
        stale = project_transfer_status([observation(completed=True, stage="completed")], [record()])[0]
        self.assertFalse(stale["completed"])
        self.assertEqual(stale["stage"], "transferring")
        self.assertIsNone(stale["upload"]["percent"])

    def test_inconsistent_durable_completion_is_visible_not_silently_reported_success(self):
        for state in (record(job_status="done", phase="organizing"),
                      record(job_status="done", phase="completed", final_verified=0),
                      record(job_status="done", phase="completed", app_acked=0)):
            with self.subTest(state=state):
                item = project_transfer_status([observation(completed=True, stage="completed")], [state])[0]
                self.assertFalse(item["completed"])
                self.assertTrue(item["state_conflict"])
                self.assertEqual(item["stage"], "manual_review")
                self.assertIn("不一致", item["message"])

    def test_deleted_tasks_do_not_reappear_and_duplicate_observations_merge_by_job_id(self):
        old = observation(updated_at="2026-10-14T00:00:00Z")
        new = observation()
        new["upload"]["percent"] = 75
        items = project_transfer_status([old, observation(99), new, {"job_id": True}], [record()])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["job_id"], 1)
        self.assertEqual(items[0]["upload"]["percent"], 75)

    def test_legacy_job_without_transfer_execution_uses_explicit_formal_terminal_state(self):
        self.assertTrue(project_transfer_status([observation()], [record(job_status="done", phase=None)])[0]["completed"])
        self.assertFalse(project_transfer_status([observation(completed=True)], [record(phase=None)])[0]["completed"])


class TransferStatusReadModelTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE import_jobs(id INTEGER PRIMARY KEY, title TEXT, status TEXT, error_message TEXT, updated_at TEXT);
            CREATE TABLE transfer_executions(job_id INTEGER PRIMARY KEY, phase TEXT, updated_at TEXT,
                final_verified INTEGER, app_acked INTEGER, last_error_message TEXT);
        """)

        @contextmanager
        def connect():
            yield self.db

        self.repo = JobQueryRepository(connect, lambda row: dict(row) if row else None)

    def add(self, job_id=1, status="transferring", phase="transferring", timestamp="2026-10-14T01:00:00Z"):
        self.db.execute("INSERT INTO import_jobs VALUES (?, ?, ?, '', ?)", (job_id, f"Movie {job_id}", status, timestamp))
        if phase is not None:
            self.db.execute("INSERT INTO transfer_executions VALUES (?, ?, ?, ?, ?, '')",
                            (job_id, phase, timestamp, int(phase == "completed"), int(phase == "completed")))
        self.db.commit()

    def service(self, observations=None, **extra):
        status = {"running": False, "status": "success", "queue_count": 0, "transfers": observations or [], **extra}
        return RcloneAdminQueryService(RcloneAdminQueryDependencies(
            rclone=SimpleNamespace(status=lambda: status), counts=object(),
            load_transfer_states=self.repo.list_transfer_statuses,
        ))

    def test_status_join_is_one_read_snapshot_and_never_creates_or_updates_tasks(self):
        self.add(1, "done", "completed")
        self.add(2)
        statements = []
        self.db.set_trace_callback(statements.append)
        self.db.execute("PRAGMA query_only=ON")
        response = self.service([observation(1), observation(2), observation(99)]).status()
        self.assertTrue(response["success"])
        self.assertEqual(response["status"]["transfer_state_source"], "import_jobs_and_transfer_executions")
        self.assertEqual(len(response["status"]["transfers"]), 2)
        self.assertEqual(sum(statement.lstrip().startswith("WITH recent") for statement in statements), 1)
        self.assertFalse(any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements))
        self.assertEqual(self.db.execute("SELECT count(*) FROM import_jobs").fetchone()[0], 2)

    def test_recent_history_never_pushes_an_older_active_task_out_of_the_snapshot(self):
        self.add(1, timestamp="2020-01-01")
        for job_id in range(2, 70):
            self.add(job_id, "done", "completed")
        records = self.repo.list_transfer_statuses([], limit=3)
        self.assertEqual(len(records), 3)
        self.assertIn(1, [item["job_id"] for item in records])
        self.assertEqual(len(self.repo.list_transfer_statuses([2], limit=3)), 4)
        self.assertNotIn("snapshot_json", records[0])

    def test_rollback_and_cancel_use_latest_formal_state_without_waiting_for_another_log(self):
        self.add(1, "done", "completed")
        service = self.service([observation(1, stage="completed", completed=True)])
        self.assertTrue(service.status()["status"]["transfers"][0]["completed"])
        for status, phase in (("review", "manual_review"), ("cancelled", "cancelled")):
            self.db.execute("UPDATE import_jobs SET status=?, error_message='Latest action' WHERE id=1", (status,))
            current = service.status()["status"]["transfers"][0]
            self.assertFalse(current["completed"])
            self.assertEqual(current["stage"], phase)

    def test_worker_outage_keeps_failure_contract_and_does_not_fake_an_empty_success(self):
        loader = Mock(side_effect=AssertionError("Should not query after worker failure"))
        service = RcloneAdminQueryService(RcloneAdminQueryDependencies(
            rclone=SimpleNamespace(status=lambda: {"_http_status": 503, "last_error": "Worker offline"}),
            counts=object(), load_transfer_states=loader,
        ))
        response = service.status()
        self.assertFalse(response["success"])
        self.assertEqual(response["_http_status"], 503)
        loader.assert_not_called()

    def test_database_failure_never_falls_back_to_stale_success(self):
        service = RcloneAdminQueryService(RcloneAdminQueryDependencies(
            rclone=SimpleNamespace(status=lambda: {"transfers": [observation(completed=True)]}), counts=object(),
            load_transfer_states=Mock(side_effect=sqlite3.OperationalError("private SQL diagnostic")),
        ))
        with self.assertLogs("fnos_media_import.services.rclone_admin_service", "ERROR"):
            response = service.status()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "task_state_unavailable")
        self.assertEqual(response["status"]["transfers"], [])
        self.assertNotIn("private SQL", response["message"])

    def test_web_status_endpoint_wires_the_shared_formal_task_read_model(self):
        source = (Path(__file__).resolve().parents[1] / "fnos_media_import/app.py").read_text(encoding="utf-8")
        self.assertIn("load_transfer_states=db.job_queries.list_transfer_statuses", source)


if __name__ == "__main__":
    unittest.main()
