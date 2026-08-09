from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from fnos_media_import.transfer.execution_repository import (
    ExecutionConflictError,
    TransferExecutionRepository,
)
from fnos_media_import.services.job_admin_query_service import serialize_transfer_execution


class _MemoryDatabase(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE import_execution_plans (
                job_id INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                plan_json TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE transfer_executions (
                job_id INTEGER PRIMARY KEY,
                strategy TEXT NOT NULL,
                phase TEXT NOT NULL,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                receipt_json TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                resume_at TEXT,
                organizer_task_id INTEGER,
                staging_verified INTEGER NOT NULL DEFAULT 0,
                final_verified INTEGER NOT NULL DEFAULT 0,
                app_acked INTEGER NOT NULL DEFAULT 0,
                cleanup_json TEXT NOT NULL DEFAULT '{}',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                row_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE import_jobs (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                error_message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO import_jobs(id, status) VALUES (1, 'created');
            """
        )

    @contextmanager
    def connect(self):
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    @staticmethod
    def update_unless_status_with_connection(
        connection,
        job_id: int,
        excluded_statuses,
        updates=None,
        **fields,
    ) -> bool:
        values = {**(updates or {}), **fields}
        statuses = sorted(str(value).strip().lower() for value in excluded_statuses)
        assignments = ", ".join(f"{name} = ?" for name in values)
        placeholders = ", ".join("?" for _value in statuses)
        cursor = connection.execute(
            f"UPDATE import_jobs SET {assignments} "
            f"WHERE id = ? AND LOWER(status) NOT IN ({placeholders})",
            (*values.values(), int(job_id), *statuses),
        )
        return int(cursor.rowcount or 0) > 0


def _plan(job_id: int = 1) -> dict:
    return {
        "version": 1,
        "job_id": job_id,
        "transfer": {
            "strategy": "rclone_copy",
            "idempotency_key": f"import:{job_id}",
        },
    }


class TransferExecutionRepositoryGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _MemoryDatabase()
        self.repository = TransferExecutionRepository(self.database)
        self.repository.create(1, _plan())

    def test_illegal_phase_regression_is_rejected(self) -> None:
        execution = self.repository.get(1)
        assert execution is not None
        with self.assertRaises(ExecutionConflictError):
            self.repository.save(execution, {"phase": "planning"}, phase="completed")

    def test_stale_phase_write_is_rejected_even_with_old_row_version(self) -> None:
        execution = self.repository.get(1)
        assert execution is not None
        first = self.repository.save(
            execution,
            {"phase": "source_materializing"},
            phase="source_materializing",
        )
        with self.assertRaises(ExecutionConflictError):
            self.repository.save(
                execution,
                {"phase": "planning"},
                phase="planning",
            )
        self.assertEqual(self.repository.get(1).phase, first.phase)

    def test_receipt_operation_key_cannot_change(self) -> None:
        execution = self.repository.get(1)
        assert execution is not None
        first = self.repository.save(
            execution,
            {
                "phase": "transferring",
                "receipt": {
                    "operation_id": "run-a",
                    "metadata": {"operation_key": "import:1"},
                },
            },
            phase="transferring",
        )
        with self.assertRaises(ExecutionConflictError):
            self.repository.save(
                first,
                {
                    "phase": "target_verifying",
                    "receipt": {
                        "operation_id": "run-b",
                        "metadata": {"operation_key": "other"},
                    },
                },
                phase="target_verifying",
            )

    def test_receipt_cannot_disappear_outside_explicit_handoff(self) -> None:
        execution = self.repository.get(1)
        assert execution is not None
        current = self.repository.save(
            execution,
            {
                "phase": "transferring",
                "receipt": {
                    "operation_id": "run-a",
                    "metadata": {"operation_key": "import:1"},
                },
            },
            phase="transferring",
        )
        with self.assertRaises(ExecutionConflictError):
            self.repository.save(
                current,
                {"phase": "target_verifying", "receipt": None},
                phase="target_verifying",
            )

    def test_materialization_receipt_can_be_cleared_at_transfer_handoff(self) -> None:
        execution = self.repository.get(1)
        assert execution is not None
        materializing = self.repository.save(
            execution,
            {"phase": "source_materializing"},
            phase="source_materializing",
        )
        waiting = self.repository.save(
            materializing,
            {
                "phase": "source_waiting_visible",
                "receipt": {
                    "operation_id": "landing-a",
                    "metadata": {"operation_key": "landing:import:1"},
                },
            },
            phase="source_waiting_visible",
        )
        transferred = self.repository.save(
            waiting,
            {"phase": "transferring", "receipt": None},
            phase="transferring",
        )
        self.assertIsNone(transferred.receipt)

    def test_attempt_counter_cannot_move_backwards(self) -> None:
        execution = self.repository.get(1)
        assert execution is not None
        current = self.repository.save(
            execution,
            {"phase": "retry_wait", "attempt": 2},
            phase="retry_wait",
        )
        with self.assertRaises(ExecutionConflictError):
            self.repository.save(
                current,
                {"phase": "retry_wait", "attempt": 1},
                phase="retry_wait",
            )

    def test_claim_owner_is_verified_when_worker_saves(self) -> None:
        claimed = self.repository.claim(1, "worker-a", lease_seconds=60)
        assert claimed is not None
        with self.assertRaises(ExecutionConflictError):
            self.repository.save(
                claimed,
                {"phase": "source_materializing", "claim": claimed.snapshot["claim"]},
                phase="source_materializing",
                claim_owner_id="worker-b",
            )

    def test_execution_claim_can_be_renewed_only_by_current_owner(self) -> None:
        claimed = self.repository.claim(1, "worker-a", lease_seconds=60)
        assert claimed is not None
        renewed = self.repository.renew_claim(1, "worker-a", lease_seconds=120)
        self.assertGreater(renewed.row_version, claimed.row_version)
        self.assertEqual(renewed.snapshot["claim"]["owner_id"], "worker-a")
        self.assertGreaterEqual(
            renewed.snapshot["claim"]["lease_until"],
            claimed.snapshot["claim"]["lease_until"],
        )
        with self.assertRaises(ExecutionConflictError):
            self.repository.renew_claim(1, "worker-b", lease_seconds=120)

    def test_execution_and_job_projection_share_one_transaction(self) -> None:
        execution = self.repository.get(1)
        assert execution is not None
        self.repository.save(
            execution,
            {"phase": "source_materializing"},
            phase="source_materializing",
            job_status="transferring",
        )
        row = self.database.connection.execute(
            "SELECT status FROM import_jobs WHERE id=1"
        ).fetchone()
        self.assertEqual(row["status"], "transferring")

    def test_worker_does_not_overwrite_concurrent_cancellation(self) -> None:
        self.database.connection.execute(
            "UPDATE import_jobs SET status='cancelled' WHERE id=1"
        )
        self.database.connection.commit()
        execution = self.repository.get(1)
        assert execution is not None
        updated = self.repository.save(
            execution,
            {"phase": "source_materializing"},
            phase="source_materializing",
            job_status="transferring",
        )
        self.assertEqual(updated.phase, "source_materializing")
        row = self.database.connection.execute(
            "SELECT status FROM import_jobs WHERE id=1"
        ).fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_external_submission_fence_fails_when_cancellation_won(self) -> None:
        self.database.connection.execute(
            "UPDATE import_jobs SET status='cancelled' WHERE id=1"
        )
        self.database.connection.commit()
        execution = self.repository.get(1)
        assert execution is not None

        with self.assertRaises(ExecutionConflictError):
            self.repository.save(
                execution,
                {
                    "phase": "source_materializing",
                    "submission_fence": {
                        "state": "prepared",
                        "operation_key": "import:1:source_materializing",
                    },
                },
                phase="source_materializing",
                job_status="provider_submitting",
                require_job_status_transition=True,
            )

        latest = self.repository.get(1)
        assert latest is not None
        self.assertEqual(latest.phase, "planning")

    def test_external_submission_fence_and_job_status_commit_together(self) -> None:
        execution = self.repository.get(1)
        assert execution is not None
        latest = self.repository.save(
            execution,
            {
                "phase": "source_materializing",
                "submission_fence": {
                    "state": "prepared",
                    "operation_key": "import:1:source_materializing",
                },
            },
            phase="source_materializing",
            job_status="provider_submitting",
            require_job_status_transition=True,
        )

        self.assertEqual(latest.phase, "source_materializing")
        row = self.database.connection.execute(
            "SELECT status FROM import_jobs WHERE id=1"
        ).fetchone()
        self.assertEqual(row["status"], "provider_submitting")

    def test_list_due_excludes_terminal_and_manual_review(self) -> None:
        execution = self.repository.get(1)
        assert execution is not None
        self.repository.save(execution, {"phase": "retry_wait"}, phase="retry_wait")
        due = self.repository.list_due()
        self.assertEqual([item.job_id for item in due], [1])

        current = self.repository.get(1)
        assert current is not None
        self.repository.save(current, {"phase": "manual_review"}, phase="manual_review")
        self.assertEqual(self.repository.list_due(), [])


class TransferExecutionAdminViewTests(unittest.TestCase):
    def test_admin_view_exposes_state_but_redacts_receipt_identity(self) -> None:
        execution = SimpleNamespace(
            phase="transferring",
            strategy="rclone_copy",
            attempts=3,
            resume_at="2026-08-09T00:00:00Z",
            organizer_task_id=17,
            staging_verified=True,
            final_verified=False,
            app_acked=False,
            cleanup={"status": "pending", "path": "/secret/path"},
            last_error_code="NETWORK_TRANSIENT",
            last_error_message="temporary",
            receipt={
                "provider_kind": "rclone",
                "status": "running",
                "operation_id": "very-secret-operation-id",
                "metadata": {
                    "run_id": "very-secret-run-id",
                    "operation_key": "very-secret-operation-key",
                    "source": {"password": "must-not-leak"},
                },
            },
        )

        result = serialize_transfer_execution(execution)

        assert result is not None
        self.assertEqual(result["phase"], "transferring")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["cleanup"], {"status": "pending"})
        encoded = str(result)
        self.assertNotIn("very-secret-operation-id", encoded)
        self.assertNotIn("very-secret-run-id", encoded)
        self.assertNotIn("very-secret-operation-key", encoded)
        self.assertNotIn("must-not-leak", encoded)


if __name__ == "__main__":
    unittest.main()
