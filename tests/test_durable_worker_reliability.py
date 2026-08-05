from __future__ import annotations

import sqlite3
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.app import (
    _public_import_compensation_retry_job_id,
    _public_import_worker_result,
)
from fnos_media_import.database import Database
from fnos_media_import.services.durable_worker_runtime import DurableWorkerRuntime
from fnos_media_import.services.worker_task_dispatcher import WorkerTaskDispatcher
from fnos_media_import.services.worker_queue_diagnostics_service import (
    WorkerQueueDiagnosticsService,
)


class WorkerRepositoryReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"worker-reliability-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_final_attempt_with_expired_lease_is_atomically_failed(self) -> None:
        task_id, _created = self.database.worker_tasks.enqueue(
            "example",
            {},
            "expired-final-attempt",
            max_attempts=1,
        )
        claimed = self.database.worker_tasks.claim("worker-a", lease_seconds=30)
        self.assertEqual(claimed["id"], task_id)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE worker_tasks SET lease_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00Z", task_id),
            )

        reclaimed = self.database.worker_tasks.claim("worker-b", lease_seconds=30)
        stored = self.database.worker_tasks.get(task_id)

        self.assertIsNone(reclaimed)
        self.assertEqual(stored["status"], "failed")
        self.assertIsNone(stored["owner_id"])
        self.assertIsNone(stored["lease_expires_at"])
        self.assertIn("lease expired", stored["error_message"].lower())
        self.assertTrue(stored["completed_at"])

    def test_defer_releases_to_pending_without_consuming_attempt(self) -> None:
        task_id, _created = self.database.worker_tasks.enqueue(
            "example",
            {},
            "defer-without-attempt",
        )
        claimed = self.database.worker_tasks.claim("worker-a")
        self.assertEqual(claimed["attempts"], 1)

        updated = self.database.worker_tasks.defer(
            task_id,
            "worker-a",
            delay_seconds=60,
            result={"deferred": True, "delay_seconds": 60},
        )
        stored = self.database.worker_tasks.get(task_id)

        self.assertTrue(updated)
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(stored["attempts"], 0)
        self.assertIsNone(stored["owner_id"])
        self.assertTrue(stored["result"]["deferred"])
        self.assertGreater(stored["available_at"], stored["updated_at"])

    def test_pending_idempotent_command_updates_but_running_command_does_not(self) -> None:
        task_id, created = self.database.worker_tasks.enqueue(
            "example",
            {"value": 1},
            "mutable-pending-command",
            max_attempts=2,
            config_revision=1,
        )
        same_id, updated = self.database.worker_tasks.enqueue(
            "example",
            {"value": 2},
            "mutable-pending-command",
            max_attempts=5,
            config_revision=3,
        )
        pending = self.database.worker_tasks.get(task_id)

        self.assertTrue(created)
        self.assertEqual(same_id, task_id)
        self.assertTrue(updated)
        self.assertEqual(pending["payload"], {"value": 2})
        self.assertEqual(pending["max_attempts"], 5)
        self.assertEqual(pending["config_revision"], 3)

        claimed = self.database.worker_tasks.claim("worker-a")
        self.assertEqual(claimed["payload"], {"value": 2})
        same_id, updated = self.database.worker_tasks.enqueue(
            "example",
            {"value": 3},
            "mutable-pending-command",
            max_attempts=9,
            config_revision=4,
        )
        running = self.database.worker_tasks.get(task_id)

        self.assertEqual(same_id, task_id)
        self.assertFalse(updated)
        self.assertEqual(running["payload"], {"value": 2})
        self.assertEqual(running["max_attempts"], 5)
        self.assertEqual(running["config_revision"], 3)

    def test_repeatable_business_commands_reactivate_terminal_worker_tasks(self) -> None:
        dispatcher = WorkerTaskDispatcher(
            repository=self.database.worker_tasks,
            enabled=lambda: True,
            config_revision=lambda: 1,
        )

        first = dispatcher.import_retry(42, reason="first")
        task_id = int(first["worker_task_id"])
        claimed = self.database.worker_tasks.claim("worker-a")
        self.assertEqual(claimed["id"], task_id)
        self.assertTrue(self.database.worker_tasks.complete(task_id, "worker-a", {"success": True}))

        completed_retry = dispatcher.import_retry(42, reason="second")
        self.assertEqual(completed_retry["worker_task_id"], task_id)
        self.assertTrue(completed_retry["created"])
        self.assertEqual(self.database.worker_tasks.get(task_id)["status"], "pending")
        claimed = self.database.worker_tasks.claim("worker-b")
        self.assertTrue(
            self.database.worker_tasks.fail(
                task_id,
                "worker-b",
                "business failure",
                terminal=True,
            )
        )

        failed_retry = dispatcher.import_retry(42, reason="third")
        self.assertEqual(failed_retry["worker_task_id"], task_id)
        self.assertTrue(failed_retry["created"])
        self.assertEqual(self.database.worker_tasks.get(task_id)["status"], "pending")
        claimed = self.database.worker_tasks.claim("worker-c")
        self.assertEqual(claimed["id"], task_id)
        self.assertTrue(self.database.worker_tasks.complete(task_id, "worker-c"))

        category = dispatcher.media_category_refresh("tv")
        category_id = int(category["worker_task_id"])
        claimed = self.database.worker_tasks.claim("worker-d")
        self.assertEqual(claimed["id"], category_id)
        self.assertTrue(self.database.worker_tasks.complete(category_id, "worker-d"))
        repeated_category = dispatcher.media_category_refresh("tv")
        self.assertEqual(repeated_category["worker_task_id"], category_id)
        self.assertTrue(repeated_category["created"])


class WorkerOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"worker-outcomes-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def _run_result(self, key: str, result: dict) -> dict:
        task_id, _created = self.database.worker_tasks.enqueue(
            key,
            {},
            f"outcome:{key}",
            max_attempts=3,
        )
        runtime = DurableWorkerRuntime(
            repository=self.database.worker_tasks,
            owner_id="worker-a",
            handlers={key: lambda _payload, _task: result},
            retry_delay_seconds=30,
        )
        self.assertTrue(runtime.run_once())
        return self.database.worker_tasks.get(task_id)

    def test_deferred_result_returns_to_pending(self) -> None:
        stored = self._run_result(
            "deferred",
            {"success": True, "deferred": True, "delay_seconds": 90},
        )
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(stored["attempts"], 0)
        self.assertTrue(stored["result"]["deferred"])

    def test_retryable_result_consumes_attempt_and_remains_pending(self) -> None:
        stored = self._run_result(
            "retryable",
            {"success": False, "retryable": True, "message": "temporary"},
        )
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(stored["attempts"], 1)
        self.assertEqual(stored["error_message"], "temporary")

    def test_success_false_is_terminal_business_failure(self) -> None:
        stored = self._run_result(
            "business-failed",
            {"success": False, "message": "rejected"},
        )
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["attempts"], 1)
        self.assertEqual(stored["error_message"], "rejected")

    def test_success_false_cannot_be_overridden_to_completed(self) -> None:
        stored = self._run_result(
            "contradictory-completed",
            {"worker_outcome": "completed", "success": False, "message": "rejected"},
        )
        self.assertEqual(stored["status"], "failed")

    def test_completed_result_is_completed(self) -> None:
        stored = self._run_result(
            "completed",
            {"worker_outcome": "completed", "success": True, "value": 7},
        )
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["result"]["value"], 7)

    def test_public_import_worker_wrapper_preserves_retry_and_defer_markers(self) -> None:
        outcome = SimpleNamespace(
            result={
                "success": False,
                "retryable": True,
                "compensation_failed": True,
                "delay_seconds": 17,
                "message": "provider busy",
                "job": {"id": 999},
            },
            job={"id": 42},
            public_status="处理中",
            bind_outcome="bound",
            rclone_start={"queued": True},
        )

        wrapped = _public_import_worker_result(outcome)

        self.assertFalse(wrapped["success"])
        self.assertTrue(wrapped["retryable"])
        self.assertTrue(wrapped["compensation_failed"])
        self.assertEqual(wrapped["delay_seconds"], 17)
        self.assertEqual(wrapped["message"], "provider busy")
        self.assertEqual(wrapped["job_id"], 42)
        self.assertEqual(wrapped["rclone_start"], {"queued": True})
        self.assertEqual(_public_import_compensation_retry_job_id({"result": wrapped}), 42)

    def test_public_import_compensation_retry_requires_explicit_failure_marker(self) -> None:
        self.assertEqual(
            _public_import_compensation_retry_job_id(
                {"result": {"retryable": True, "job_id": 42}}
            ),
            0,
        )
        self.assertEqual(
            _public_import_compensation_retry_job_id(
                {"result": {"compensation_failed": True, "job_id": 42}}
            ),
            0,
        )


class WorkerRuntimeResilienceTests(unittest.TestCase):
    def test_repeated_repository_errors_use_bounded_backoff(self) -> None:
        class UnavailableRepository:
            def __init__(self) -> None:
                self.claim_calls = 0

            def claim(self, *_args, **_kwargs):
                self.claim_calls += 1
                raise sqlite3.OperationalError("database unavailable")

        repository = UnavailableRepository()
        runtime = DurableWorkerRuntime(
            repository=repository,
            owner_id="worker-a",
            handlers={"example": lambda _payload, _task: {"success": True}},
            poll_seconds=0.05,
            error_backoff_seconds=0.05,
            max_error_backoff_seconds=0.1,
        )
        runtime.start()
        try:
            time.sleep(0.28)
            self.assertTrue(runtime.status()["runtime_running"])
        finally:
            runtime.shutdown()

        self.assertGreaterEqual(repository.claim_calls, 2)
        self.assertLessEqual(repository.claim_calls, 5)

    def test_claim_and_complete_errors_do_not_kill_worker_thread(self) -> None:
        completed = threading.Event()

        class FlakyRepository:
            def __init__(self) -> None:
                self.claim_calls = 0
                self.complete_calls = 0

            @staticmethod
            def prune_terminal(**_kwargs) -> int:
                return 0

            def claim(self, owner_id, **_kwargs):
                self.claim_calls += 1
                if self.claim_calls == 1:
                    raise sqlite3.OperationalError("database is locked")
                if self.claim_calls in {2, 3}:
                    return {
                        "id": 1,
                        "task_type": "example",
                        "payload": {},
                        "owner_id": owner_id,
                    }
                return None

            @staticmethod
            def renew(*_args, **_kwargs) -> bool:
                return True

            def complete(self, *_args, **_kwargs) -> bool:
                self.complete_calls += 1
                if self.complete_calls == 1:
                    raise sqlite3.OperationalError("database is busy")
                completed.set()
                return True

            @staticmethod
            def fail(*_args, **_kwargs) -> bool:
                return True

        repository = FlakyRepository()
        runtime = DurableWorkerRuntime(
            repository=repository,
            owner_id="worker-a",
            handlers={"example": lambda _payload, _task: {"success": True}},
            poll_seconds=0.05,
            lease_seconds=1,
            error_backoff_seconds=0.05,
            max_error_backoff_seconds=0.1,
        )
        runtime.start()
        try:
            self.assertTrue(completed.wait(2))
            self.assertTrue(runtime.status()["runtime_running"])
            self.assertGreaterEqual(repository.claim_calls, 2)
            self.assertEqual(repository.complete_calls, 2)
        finally:
            runtime.shutdown()

    def test_fail_error_does_not_kill_worker_thread(self) -> None:
        recovered = threading.Event()

        class FlakyRepository:
            def __init__(self) -> None:
                self.claim_calls = 0
                self.fail_calls = 0

            @staticmethod
            def prune_terminal(**_kwargs) -> int:
                return 0

            def claim(self, owner_id, **_kwargs):
                self.claim_calls += 1
                if self.claim_calls in {1, 2}:
                    return {
                        "id": 2,
                        "task_type": "example",
                        "payload": {"attempt": self.claim_calls},
                        "owner_id": owner_id,
                    }
                return None

            @staticmethod
            def renew(*_args, **_kwargs) -> bool:
                return True

            def fail(self, *_args, **_kwargs) -> bool:
                self.fail_calls += 1
                if self.fail_calls == 1:
                    raise sqlite3.OperationalError("write failed")
                recovered.set()
                return True

        repository = FlakyRepository()

        def handler(payload, _task):
            raise RuntimeError(f"handler failed {payload['attempt']}")

        runtime = DurableWorkerRuntime(
            repository=repository,
            owner_id="worker-a",
            handlers={"example": handler},
            poll_seconds=0.05,
            lease_seconds=1,
            error_backoff_seconds=0.05,
            max_error_backoff_seconds=0.1,
        )
        runtime.start()
        try:
            self.assertTrue(recovered.wait(2))
            self.assertTrue(runtime.status()["runtime_running"])
            self.assertEqual(repository.fail_calls, 2)
        finally:
            runtime.shutdown()

    def test_heartbeat_exception_is_captured_and_worker_remains_alive(self) -> None:
        failed = threading.Event()

        class HeartbeatRepository:
            def __init__(self) -> None:
                self.claimed = False

            @staticmethod
            def prune_terminal(**_kwargs) -> int:
                return 0

            def claim(self, owner_id, **_kwargs):
                if self.claimed:
                    return None
                self.claimed = True
                return {
                    "id": 3,
                    "task_type": "example",
                    "payload": {},
                    "owner_id": owner_id,
                }

            @staticmethod
            def renew(*_args, **_kwargs) -> bool:
                raise sqlite3.OperationalError("heartbeat write failed")

            @staticmethod
            def fail(*_args, **_kwargs) -> bool:
                failed.set()
                return True

        repository = HeartbeatRepository()
        runtime = DurableWorkerRuntime(
            repository=repository,
            owner_id="worker-a",
            handlers={"example": lambda _payload, _task: time.sleep(0.7) or {"success": True}},
            poll_seconds=0.05,
            lease_seconds=1,
            error_backoff_seconds=0.05,
            max_error_backoff_seconds=0.1,
        )
        runtime.start()
        try:
            self.assertTrue(failed.wait(2))
            state = runtime.status()
            self.assertTrue(state["runtime_running"])
            self.assertEqual(state["last_error"]["stage"], "heartbeat")
        finally:
            runtime.shutdown()


class WorkerQueueDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def _queue_status() -> dict:
        return {
            "counts": {},
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "expired_leases": 0,
            "next_task": None,
        }

    def test_runtime_health_is_part_of_queue_health(self) -> None:
        repository = SimpleNamespace(status=self._queue_status)
        runtime = SimpleNamespace(
            owner_id="worker-a",
            handlers={"example": object()},
            status=lambda: {
                "runtime_running": False,
                "heartbeat_stale": True,
                "healthy": False,
            },
        )
        diagnostics = WorkerQueueDiagnosticsService(
            repository=repository,
            runtime=runtime,
            dispatch_enabled=lambda: True,
        )

        result = diagnostics.status()

        self.assertFalse(result["healthy"])
        self.assertTrue(result["queue_healthy"])
        self.assertFalse(result["runtime_running"])

    def test_terminal_failures_are_history_not_active_queue_health(self) -> None:
        status = self._queue_status()
        status.update({"counts": {"failed": 2}, "failed": 2})
        repository = SimpleNamespace(status=lambda: status)
        runtime = SimpleNamespace(
            owner_id="worker-a",
            handlers={"example": object()},
            status=lambda: {
                "runtime_running": True,
                "heartbeat_stale": False,
                "healthy": True,
            },
        )
        diagnostics = WorkerQueueDiagnosticsService(
            repository=repository,
            runtime=runtime,
            dispatch_enabled=lambda: True,
        )

        result = diagnostics.status()

        self.assertTrue(result["queue_healthy"])
        self.assertTrue(result["healthy"])
        self.assertEqual(result["failed"], 2)

    def test_external_worker_mode_can_mark_local_runtime_not_required(self) -> None:
        repository = SimpleNamespace(status=self._queue_status)
        runtime = SimpleNamespace(
            owner_id="web-a",
            handlers={},
            status=lambda: {
                "runtime_running": False,
                "heartbeat_stale": True,
                "healthy": False,
            },
        )
        diagnostics = WorkerQueueDiagnosticsService(
            repository=repository,
            runtime=runtime,
            dispatch_enabled=lambda: True,
            runtime_required=False,
        )

        result = diagnostics.status()

        self.assertTrue(result["healthy"])
        self.assertFalse(result["runtime_required"])

    def test_repository_error_is_reported_as_unhealthy(self) -> None:
        def fail_status():
            raise sqlite3.OperationalError("database unavailable")

        runtime = SimpleNamespace(
            owner_id="worker-a",
            handlers={},
            status=lambda: {
                "runtime_running": True,
                "heartbeat_stale": False,
                "healthy": True,
            },
        )
        diagnostics = WorkerQueueDiagnosticsService(
            repository=SimpleNamespace(status=fail_status),
            runtime=runtime,
            dispatch_enabled=lambda: True,
        )

        result = diagnostics.status()

        self.assertFalse(result["healthy"])
        self.assertIn("database unavailable", result["repository_error"])


if __name__ == "__main__":
    unittest.main()
