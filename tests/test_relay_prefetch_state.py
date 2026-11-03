from __future__ import annotations

import errno
import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.transfer.relay_file_state import RelayFileState
from fnos_media_import.transfer.relay_pipeline_config import RelayPipelineConfig


def _increment_checkpoint(root: str, count: int) -> None:
    store = RelayFileState(root, job_id=1, plan_key="plan")
    for _ in range(count):
        def increment(state):
            state["files"][0]["counter"] = state["files"][0].get("counter", 0) + 1

        store.mutate(increment)


class RelayPrefetchStateTests(unittest.TestCase):
    def test_missing_checkpoint_read_never_creates_unwritable_relay_root(self):
        missing = self.parent / "not-created" / "relay" / "job-7"
        store = RelayFileState(str(missing), job_id=7, plan_key="unwritable-root")
        with patch.object(Path, "mkdir", side_effect=PermissionError("CI runner cannot create /data")) as mkdir:
            self.assertIsNone(store.load())
        mkdir.assert_not_called()
        self.assertFalse(missing.parent.exists())

    def test_missing_checkpoint_read_does_not_create_shared_lock_file(self):
        store = self._store(7)
        with patch.object(store, "_lock", side_effect=AssertionError("missing reads must not acquire a write lock")):
            self.assertIsNone(store.load())

    def test_readers_share_write_lock_and_nested_reads_are_reentrant(self):
        started, finished = threading.Event(), threading.Event()

        def read():
            started.set()
            self.store.load()
            finished.set()

        with self.store._lock():
            thread = threading.Thread(target=read, daemon=True)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.05))
            self.assertEqual(self.store.load()["job_id"], 1)
        self.assertTrue(finished.wait(1))
        thread.join(1)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-prefetch-state-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.store = self._store(1)
        self.store.initialize(["a.mkv", "b.mkv", "c.mkv", "d.mkv"])

    def _store(self, job_id):
        return RelayFileState(str(self.parent / f"job-{job_id}"), job_id=job_id, plan_key="plan")

    def _enable(self, depth=1, budget=100, reserve=0):
        return self.store.enable_pipeline(RelayPipelineConfig(depth, budget, reserve))

    def _download(self, index, size=10):
        self.store.prepare_file(index, size)
        entry = self.store.load()["files"][index]
        Path(self.store.file_path(entry["path"])).write_bytes(b"x" * size)
        self.store.mark_downloaded(index, size)

    def _clean(self, index):
        entry = self.store.load()["files"][index]
        self.store.update_entry(
            index, state="verified", upload_key=f"upload-{index}",
            verification={"status": "ok", "file_count": 1, "total_size": entry["size"]},
        )
        Path(self.store.file_path(entry["path"])).unlink()
        self.store.update_entry(index, state="cleaned")

    def test_configuration_is_strict_and_frozen_in_existing_manifest(self):
        defaults = RelayPipelineConfig.from_env({})
        self.assertEqual(defaults, RelayPipelineConfig(1, 21474836480, 1073741824))
        self.assertEqual(self.store.load()["version"], 1)
        self._enable(depth=2, budget=99, reserve=7)
        with patch.dict("os.environ", {"RELAY_PREFETCH_FILES": "0"}):
            self.assertEqual(self.store.enable_pipeline()["pipeline"]["prefetch_files"], 2)
        for key, value in [
            ("RELAY_PREFETCH_FILES", "3"), ("RELAY_PREFETCH_FILES", "1.0"),
            ("RELAY_CACHE_MAX_BYTES", "0"), ("RELAY_DISK_RESERVE_BYTES", "-1"),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                RelayPipelineConfig.from_env({key: value})

    def test_default_window_holds_upload_and_one_prefetch_only(self):
        self._enable()
        self._download(0)
        self._download(1)
        self.assertEqual([item["state"] for item in self.store.load()["files"]], ["downloaded", "downloaded", "pending", "pending"])
        with self.assertRaises(BlockingIOError) as raised:
            self.store.prepare_file(2, 10)
        self.assertEqual(raised.exception.errno, errno.EAGAIN)
        self._clean(0)
        self._download(2)
        self.assertEqual(len(list(self.store.root.glob("*.mkv"))), 2)

    def test_depth_zero_is_serial_and_depth_two_is_bounded_to_three(self):
        self._enable(depth=0)
        self._download(0)
        with self.assertRaises(BlockingIOError):
            self.store.prepare_file(1, 10)
        # A separate temporary root avoids coupling this independent limit test
        # to the globally exclusive active cache job.
        other = RelayFileState(str(self.parent / "isolated" / "job-2"), job_id=2, plan_key="plan")
        other.initialize(["a", "b", "c", "d"])
        other.enable_pipeline(RelayPipelineConfig(2, 100, 0))
        for index in range(3):
            other.prepare_file(index, 1)
            Path(other.file_path(other.load()["files"][index]["path"])).write_bytes(b"x")
            other.mark_downloaded(index, 1)
        with self.assertRaises(BlockingIOError):
            other.prepare_file(3, 1)

    def test_reservation_survives_restart_and_allows_only_one_download(self):
        self._enable(depth=2)
        self.store.prepare_file(0, 10)
        restarted = self._store(1)
        self.assertTrue(restarted.load()["files"][0]["download_reserved"])
        self.assertEqual(restarted.candidate_indexes(), [])
        with self.assertRaises(BlockingIOError):
            restarted.prepare_file(1, 10)
        restarted.prepare_file(0, 10)
        self.assertTrue(restarted.load()["files"][0]["download_reserved"])

    def test_bytes_limit_counts_cached_upload_and_whole_next_file(self):
        self._enable(budget=19)
        self._download(0)
        with self.assertRaises(BlockingIOError):
            self.store.prepare_file(1, 10)
        self.assertFalse(self.store.load()["files"][1]["download_reserved"])
        self._clean(0)
        self.store.prepare_file(1, 10)

    def test_disk_reserve_and_capacity_are_independent_limits(self):
        self._enable(budget=100, reserve=5)
        with patch("fnos_media_import.transfer.relay_file_state.shutil.disk_usage", return_value=SimpleNamespace(free=14)):
            with self.assertRaises(BlockingIOError):
                self.store.prepare_file(0, 10)
        with patch("fnos_media_import.transfer.relay_file_state.shutil.disk_usage", return_value=SimpleNamespace(free=15)):
            self.store.prepare_file(0, 10)
        with patch("fnos_media_import.transfer.relay_file_state.shutil.disk_usage", return_value=SimpleNamespace(free=4)):
            with self.assertRaises(BlockingIOError):
                self.store.check_space(0)

    def test_complete_file_recovery_does_not_reserve_another_copy(self):
        self._enable(budget=10, reserve=5)
        self.store.prepare_file(0, 10)
        Path(self.store.file_path("a.mkv")).write_bytes(b"0123456789")
        with patch("fnos_media_import.transfer.relay_file_state.shutil.disk_usage", return_value=SimpleNamespace(free=5)):
            self.store.prepare_file(0, 10)
        self.store.mark_downloaded(0, 10)

    def test_inplace_partial_resumes_at_exact_budget_without_double_counting(self):
        self._enable(budget=10, reserve=5)
        self.store.prepare_file(0, 10)
        target = Path(self.store.file_path("a.mkv"))
        target.write_bytes(b"partial")
        restarted = self._store(1)
        with patch("fnos_media_import.transfer.relay_file_state.shutil.disk_usage", return_value=SimpleNamespace(free=8)):
            restarted.prepare_file(0, 10)
        self.assertEqual(target.read_bytes(), b"partial")
        self.assertTrue(restarted.load()["files"][0]["download_reserved"])

    def test_old_temporary_file_is_not_assumed_to_belong_to_current_reservation(self):
        self._enable(depth=2)
        self.store.prepare_file(0, 10)
        orphan = self.store.root / "a.mkv.unknown.partial"
        orphan.write_bytes(b"partial")
        with self.assertRaises(BlockingIOError) as raised:
            self._store(1).prepare_file(0, 10)
        self.assertIn("临时文件", str(raised.exception))
        self.assertEqual(orphan.read_bytes(), b"partial")

    def test_partial_file_is_preserved_and_never_silently_frees_its_reservation(self):
        self._enable()
        self.store.prepare_file(0, 10)
        target = Path(self.store.file_path("a.mkv"))
        target.write_bytes(b"partial")
        restarted = self._store(1)
        with self.assertRaises(ValueError):
            restarted.prepare_file(0, 11)
        with self.assertRaises(ValueError):
            restarted.mark_downloaded(0, 10)
        self.assertEqual(target.read_bytes(), b"partial")
        self.assertTrue(restarted.load()["files"][0]["download_reserved"])

    def test_unknown_size_cannot_be_reserved(self):
        self._enable()
        for invalid in [None, -1, True, 1.0, "1"]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.prepare_file(0, invalid)
        self.assertIsNone(self.store.load()["files"][0]["size"])

    def test_other_job_reservation_and_orphan_bytes_block_admission(self):
        self._enable()
        other = self._store(2)
        other.initialize(["other.mkv"])
        other.prepare_file(0, 5)
        with self.assertRaises(BlockingIOError):
            self.store.prepare_file(0, 10)
        other.path.unlink()
        Path(other.file_path("untracked.partial")).write_bytes(b"leftover")
        with self.assertRaises(BlockingIOError):
            self.store.prepare_file(0, 10)
        self.assertIsNone(self.store.load()["files"][0]["size"])

    def test_v2_reservation_prevents_legacy_job_from_starting(self):
        self._enable()
        self.store.prepare_file(0, 10)
        other = self._store(2)
        other.initialize(["other.mkv"])
        with self.assertRaises(BlockingIOError):
            other.prepare_file(0, 5)

    def test_pause_is_durable_and_resume_is_explicit(self):
        self._enable()
        self._download(0)
        self.store.pause_prefetch("verification uncertain")
        restarted = self._store(1)
        self.assertEqual(restarted.candidate_indexes(), [])
        with self.assertRaises(BlockingIOError):
            restarted.prepare_file(1, 10)
        restarted.enable_pipeline()
        self.assertEqual(restarted.load()["prefetch_paused"], "verification uncertain")
        restarted.resume_prefetch()
        self.assertEqual(restarted.candidate_indexes(), [1])

    def test_stale_save_cannot_drop_another_entry_download_receipt(self):
        self._enable()
        self._download(0)
        stale = self.store.load()
        receipt = {"operation_id": "download-2", "status": "running"}
        self.store.record_download(1, receipt)
        stale["files"][0]["upload_started"] = True
        with self.assertRaises(BlockingIOError):
            self.store.save(stale)
        latest = self.store.update_entry(0, upload_started=True)
        self.assertTrue(latest["files"][0]["upload_started"])
        self.assertEqual(latest["files"][1]["download_receipt"], receipt)
        self.assertEqual(self.store.candidate_indexes(), [])

    def test_cancel_between_poll_read_and_resume_remains_terminal(self):
        self._enable()
        self.store.pause_prefetch("verification uncertain")
        stale_poll = self.store.load()
        self.assertNotEqual(stale_poll["prefetch_paused"], "cancelled")

        # Another coordinator persists cancellation after the poll's decision
        # to resume, but before its resume call acquires the shared ledger lock.
        self._store(1).pause_prefetch("cancelled")
        resumed = self.store.resume_prefetch()
        self.assertEqual(resumed["prefetch_paused"], "cancelled")
        self.assertEqual(self._store(1).load()["prefetch_paused"], "cancelled")
        self.assertEqual(self.store.candidate_indexes(), [])
        with self.assertRaises(BlockingIOError):
            self.store.prepare_file(0, 10)

    def test_late_error_pause_cannot_overwrite_persisted_cancellation(self):
        self._enable()
        self.store.pause_prefetch("cancelled")
        paused = self._store(1).pause_prefetch("late download error")
        self.assertEqual(paused["prefetch_paused"], "cancelled")
        self.store.pause_prefetch("cancelled")
        self.assertEqual(self.store.resume_prefetch()["prefetch_paused"], "cancelled")

    def test_cross_process_mutations_do_not_lose_updates(self):
        self._enable()
        context = multiprocessing.get_context("spawn")
        children = [context.Process(target=_increment_checkpoint, args=(str(self.store.root), 8)) for _ in range(2)]
        for child in children:
            child.start()
        for child in children:
            child.join(20)
            if child.is_alive():
                child.terminate()
                child.join(5)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(self.store.load()["files"][0]["counter"], 16)

    def test_next_file_cannot_upload_or_be_cleaned_before_current(self):
        self._enable()
        self._download(0)
        self._download(1)
        with self.assertRaises(ValueError):
            self.store.update_entry(1, upload_started=True)
        with self.assertRaises(ValueError):
            self.store.update_entry(
                1, state="verified", upload_key="bad",
                verification={"status": "ok", "file_count": 1, "total_size": 10},
            )
        self.assertEqual(self.store.load()["files"][1]["state"], "downloaded")

    def test_queued_receipt_blocks_another_download_candidate(self):
        self._enable(depth=2)
        self._download(0)
        self.store.record_download(1, {"operation_id": "queued", "status": "pending"})
        self.assertEqual(self.store.candidate_indexes(), [])

    def test_cli_explicit_enable_protects_the_first_download(self):
        from fnos_media_import.transfer.relay_file_state import main

        arguments = [
            "relay_file_state", "enable", "--local-dir", str(self.store.root),
            "--job-id", "1", "--plan-key", "plan", "--index", "0",
        ]
        with patch("sys.argv", arguments), patch.dict("os.environ", {
            "RELAY_PREFETCH_FILES": "0", "RELAY_CACHE_MAX_BYTES": "9", "RELAY_DISK_RESERVE_BYTES": "0",
        }):
            self.assertEqual(main(), 0)
        self.assertEqual(self.store.load()["version"], 2)
        with self.assertRaises(BlockingIOError):
            self.store.prepare_file(0, 10)

    def test_cli_environment_increase_resumes_original_oversized_first_file(self):
        from fnos_media_import.transfer.relay_file_state import main

        self._enable(depth=0, budget=9, reserve=3)
        self.store.record_download(0, {"operation_id": "original-run", "status": "pending"})
        before = self.store.load()
        with self.assertRaises(BlockingIOError):
            self.store.prepare_file(0, 10)
        arguments = [
            "relay_file_state", "enable", "--local-dir", str(self.store.root),
            "--job-id", "1", "--plan-key", "plan", "--index", "0",
        ]
        with patch("sys.argv", arguments), patch.dict("os.environ", {
            "RELAY_CACHE_MAX_BYTES": "10", "RELAY_PREFETCH_FILES": "2", "RELAY_DISK_RESERVE_BYTES": "999",
        }):
            frozen = self.store.enable_pipeline(RelayPipelineConfig(2, 100, 999))
            self.assertEqual(frozen["pipeline"]["cache_max_bytes"], 9)
            self.assertEqual(main(), 0)
        raised = self.store.load()
        self.assertEqual(raised["pipeline"], {"prefetch_files": 0, "cache_max_bytes": 10, "disk_reserve_bytes": 3})
        self.assertEqual(raised["files"], before["files"])
        self.assertEqual(raised["plan_key"], before["plan_key"])
        self.store.prepare_file(0, 10)
        self.assertTrue(self.store.load()["files"][0]["download_reserved"])

    def test_budget_raise_preserves_cancellation_and_rejects_unsafe_changes(self):
        self._enable(budget=20)
        self._download(0)
        self.store.pause_prefetch("cancelled")
        before = self.store.load()
        raised = self.store.increase_cache_budget(30)
        self.assertEqual(raised["pipeline"]["cache_max_bytes"], 30)
        self.assertEqual(raised["prefetch_paused"], "cancelled")
        self.assertEqual(raised["files"], before["files"])
        self.assertEqual(Path(self.store.file_path("a.mkv")).read_bytes(), b"x" * 10)
        for invalid in [30, 29, 0, True, "31", -1]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.increase_cache_budget(invalid)
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.store.apply_environment_budget_increase(), raised)
        with patch.dict("os.environ", {"RELAY_CACHE_MAX_BYTES": "10"}):
            self.assertEqual(self.store.apply_environment_budget_increase(), raised)
        with patch.dict("os.environ", {"RELAY_CACHE_MAX_BYTES": "invalid"}), self.assertRaises(ValueError):
            self.store.apply_environment_budget_increase()
        self.assertEqual(self.store.load(), raised)
        with self.assertRaises(ValueError):
            self.store.mutate(lambda state: state["pipeline"].update(cache_max_bytes=40))
        with patch.dict("os.environ", {"RELAY_CACHE_MAX_BYTES": "40"}):
            latest = self.store.apply_environment_budget_increase()
        self.assertEqual(latest["pipeline"]["cache_max_bytes"], 40)
        self.assertEqual(latest["prefetch_paused"], "cancelled")


if __name__ == "__main__":
    unittest.main()
