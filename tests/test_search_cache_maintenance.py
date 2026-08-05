from __future__ import annotations

import threading
import time
import unittest
import uuid
from pathlib import Path

from fnos_media_import.database import Database
from fnos_media_import.services.search_cache_maintenance_worker import SearchCacheMaintenanceWorker


class SearchCachePruneRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"search-cache-maintenance-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_prune_expired_is_bounded_and_preserves_active_rows(self) -> None:
        self.database.save_search_cache(
            "active",
            keyword="active",
            item={"title": "Active", "url": "https://example.invalid/active"},
            expires_minutes=60,
        )
        self.database.save_search_cache_many(
            [
                (
                    f"expired-{index}",
                    {"title": f"Expired {index}", "url": f"https://example.invalid/expired/{index}"},
                )
                for index in range(3)
            ],
            keyword="expired",
            expires_minutes=-1,
        )

        self.assertEqual(self.database.prune_expired_search_cache(limit=2), 2)
        self.assertEqual(self.database.prune_expired_search_cache(limit=2), 1)
        self.assertEqual(self.database.prune_expired_search_cache(limit=2), 0)

        with self.database.connect() as connection:
            rows = connection.execute("SELECT public_id FROM search_cache ORDER BY public_id").fetchall()
        self.assertEqual([str(row["public_id"]) for row in rows], ["active"])


class _MaintenanceDatabase:
    def __init__(self, *, remaining: int = 0, block_prune: bool = False) -> None:
        self.remaining = remaining
        self.block_prune = block_prune
        self.prune_entered = threading.Event()
        self.allow_prune = threading.Event()
        self.second_acquire = threading.Event()
        self.acquire_count = 0
        self.release_count = 0
        self.prune_limits: list[int] = []

    def acquire_scheduler_lease(self, _name: str, _owner_id: str, _ttl_seconds: int) -> bool:
        self.acquire_count += 1
        if self.acquire_count >= 2:
            self.second_acquire.set()
        return True

    def release_scheduler_lease(self, _name: str, _owner_id: str) -> bool:
        self.release_count += 1
        return True

    def prune_expired_search_cache(self, *, limit: int) -> int:
        self.prune_limits.append(limit)
        self.prune_entered.set()
        if self.block_prune:
            self.allow_prune.wait(2)
        deleted = min(limit, self.remaining)
        self.remaining -= deleted
        return deleted


class SearchCacheMaintenanceWorkerTests(unittest.TestCase):
    def test_run_once_deletes_in_batches_up_to_per_run_cap(self) -> None:
        database = _MaintenanceDatabase(remaining=8)
        worker = SearchCacheMaintenanceWorker(
            database=database,
            owner_id="worker-a",
            log=lambda _message: None,
            batch_size=2,
            max_delete_per_run=5,
        )

        result = worker.run_once()

        self.assertTrue(result["success"])
        self.assertEqual(result["deleted"], 5)
        self.assertEqual(result["batches"], 3)
        self.assertEqual(database.prune_limits, [2, 2, 1])
        self.assertEqual(database.remaining, 3)
        self.assertEqual(database.release_count, 1)

    def test_worker_runs_periodically_until_shutdown(self) -> None:
        database = _MaintenanceDatabase()
        worker = SearchCacheMaintenanceWorker(
            database=database,
            owner_id="worker-a",
            log=lambda _message: None,
            interval_seconds=0.05,
            shutdown_timeout_seconds=1,
        )

        worker.start()
        self.assertTrue(database.second_acquire.wait(1))
        worker.shutdown()

        self.assertGreaterEqual(database.acquire_count, 2)
        self.assertFalse(worker.thread and worker.thread.is_alive())
        self.assertEqual(database.release_count, database.acquire_count)

    def test_shutdown_does_not_release_lease_while_prune_is_still_running(self) -> None:
        database = _MaintenanceDatabase(remaining=1, block_prune=True)
        worker = SearchCacheMaintenanceWorker(
            database=database,
            owner_id="worker-a",
            log=lambda _message: None,
            interval_seconds=60,
            shutdown_timeout_seconds=0.01,
        )

        worker.start()
        self.assertTrue(database.prune_entered.wait(1))
        worker.shutdown()

        self.assertTrue(worker.thread and worker.thread.is_alive())
        self.assertEqual(database.release_count, 0)

        database.allow_prune.set()
        deadline = time.monotonic() + 1
        while worker.thread and worker.thread.is_alive() and time.monotonic() < deadline:
            worker.thread.join(timeout=0.05)

        self.assertFalse(worker.thread and worker.thread.is_alive())
        self.assertEqual(database.release_count, 1)


if __name__ == "__main__":
    unittest.main()
