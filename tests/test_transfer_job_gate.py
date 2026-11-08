from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path

from fnos_media_import.repositories.scheduler_lease_repository import SchedulerLeaseRepository
from fnos_media_import.transfer.job_gate import TransferJobGate


class _LeaseFactory:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def __call__(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


class TransferJobGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "leases.db"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                CREATE TABLE scheduler_leases (
                    name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        self.factory = _LeaseFactory(self.path)
        self.lease_repository = SchedulerLeaseRepository(self.factory)

    def _gate(self, ttl_seconds: int = 900) -> TransferJobGate:
        return TransferJobGate(self.lease_repository, ttl_seconds=ttl_seconds)

    def test_only_one_job_can_own_the_transfer_slot(self) -> None:
        gate = self._gate()
        self.assertEqual(gate.try_acquire(87), (True, 87))
        self.assertEqual(gate.try_acquire(88), (False, 87))
        self.assertEqual(gate.active_job_id(), 87)

        self.assertTrue(gate.release(87))
        self.assertEqual(gate.try_acquire(88), (True, 88))
        self.assertEqual(gate.active_job_id(), 88)

    def test_same_job_renews_but_another_job_cannot_steal_an_active_lease(self) -> None:
        gate = self._gate()
        self.assertEqual(gate.try_acquire(87), (True, 87))
        self.assertTrue(gate.renew(87))
        self.assertEqual(gate.try_acquire(88), (False, 87))

    def test_expired_lease_can_be_taken_by_the_next_job(self) -> None:
        gate = self._gate(ttl_seconds=5)
        self.assertEqual(gate.try_acquire(87), (True, 87))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE scheduler_leases SET expires_at='2000-01-01T00:00:00Z' WHERE name=?",
                ("v1_transfer_job_gate",),
            )
            connection.commit()
        self.assertEqual(gate.try_acquire(88), (True, 88))

    def test_release_is_owner_scoped(self) -> None:
        gate = self._gate()
        self.assertEqual(gate.try_acquire(87), (True, 87))
        self.assertFalse(gate.release(88))
        self.assertEqual(gate.active_job_id(), 87)
        self.assertTrue(gate.release(87))
        self.assertEqual(gate.active_job_id(), 0)

    def test_missing_lease_repository_fails_open_for_legacy_composition(self) -> None:
        gate = TransferJobGate(None)
        self.assertEqual(gate.try_acquire(87), (True, 87))
        self.assertFalse(gate.release(87))


if __name__ == "__main__":
    unittest.main()
