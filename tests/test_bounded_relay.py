from __future__ import annotations

import dataclasses
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fnos_media_import.transfer import CancelResult, OperationReceipt, UploadOutcome
from fnos_media_import.transfer.bounded_relay import BoundedRelay
from tests import test_sequential_relay as serial_fixtures


class BufferedDownloads(serial_fixtures._Downloads):
    supports_bounded_prefetch = True

    def __init__(self, files, events):
        super().__init__(files, events)
        self.finished = [threading.Event() for _ in files]
        self.uploading = None
        self.pending = set()
        self.cancelled = []
        self.peak_files = 0

    def download(self, *, source, local_dir, idempotency_key):
        if idempotency_key in self.runs:
            return self.runs[idempotency_key]
        index = int(idempotency_key.split(":", 2)[1]) if idempotency_key.startswith("relay-file:") else 0
        state = self.store.initialize(list(self.files))
        # Real worker enables the budget before the first byte as well.
        self.store.enable_pipeline()
        relative = state["files"][index]["path"]
        if index and self.uploading is not None:
            if not self.uploading.wait(2):
                raise AssertionError("download did not overlap an active upload")
        self.store.prepare_file(index, len(self.files[relative]))
        self.events.append(("download", relative))
        receipt = OperationReceipt(f"run-{index}", "running" if index in self.pending else "completed", "rclone",
            metadata={"run_id": str(index + 1), "operation_key": idempotency_key})
        self.runs[idempotency_key] = receipt
        if index not in self.pending:
            target = Path(self.store.file_path(relative))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.files[relative])
            self.store.mark_downloaded(index, len(self.files[relative]))
            self.peak_files = max(self.peak_files, len(list(self.root.rglob("*.mkv"))))
            self.finished[index].set()
        return receipt

    def check(self, receipt):
        return {"status": "cancelled" if receipt.operation_id in self.cancelled else receipt.status}

    def cancel(self, receipt):
        self.cancelled.append(receipt.operation_id)
        return CancelResult("cancelled")


class BoundedRelayTests(unittest.TestCase):
    _executor = serial_fixtures.SequentialRelayTests._executor

    def setUp(self):
        self.enterContext(patch.dict("os.environ", {
            "RELAY_PREFETCH_FILES": "1", "RELAY_CACHE_MAX_BYTES": "1024", "RELAY_DISK_RESERVE_BYTES": "0",
        }))
        serial_fixtures.SequentialRelayTests.setUp(self)
        self.files["season/03.mkv"] = b"third"
        self.files["season/04.mkv"] = b"fourth"
        self.adapter = BufferedDownloads(self.files, self.events)
        self.executor = self._executor()
        self.executor._sequential.POLL_SECONDS = 0.01
        self.receipt = self.executor.submit()

    def _hold_upload_for_prefetch(self, depth):
        self.adapter.uploading = threading.Event()
        original = self.native.upload_file

        def upload(**kwargs):
            if kwargs["target_name"] == "01.mkv":
                self.adapter.uploading.set()
                self.assertTrue(self.adapter.finished[depth].wait(2), "prefetch did not run during blocked upload")
                self.assertFalse(self.adapter.finished[depth + 1].is_set(), "producer exceeded read-ahead limit")
            return original(**kwargs)
        self.native.upload_file = upload

    def test_one_file_prefetch_overlaps_upload_and_keeps_same_job_keys(self):
        self._hold_upload_for_prefetch(1)
        self.assertIsInstance(self.executor._sequential, BoundedRelay)
        result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "running", result.message)
        self.assertEqual(self.adapter.peak_files, 2)
        self.assertEqual(len(self.native.calls), 1)
        self.assertFalse((self.root / "season/01.mkv").exists())
        self.assertTrue((self.root / "season/02.mkv").exists())
        self.assertFalse((self.root / "season/03.mkv").exists())
        self.assertEqual(self.receipt.metadata["operation_key"], f"relay-dl:{self.plan.idempotency_key}")
        self.assertEqual(set(self.adapter.runs), {f"relay-dl:{self.plan.idempotency_key}", f"relay-file:1:{self.plan.idempotency_key}"})

    def test_depth_two_downloads_at_most_two_following_files_while_upload_blocks(self):
        with patch.dict("os.environ", {"RELAY_PREFETCH_FILES": "2"}):
            self._hold_upload_for_prefetch(2)
            result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "running", result.message)
        self.assertEqual(self.adapter.peak_files, 3)
        self.assertFalse(self.adapter.finished[3].is_set())
        self.assertTrue((self.root / "season/03.mkv").exists())

    def test_depth_zero_keeps_serial_upload_with_new_budget(self):
        with patch.dict("os.environ", {"RELAY_PREFETCH_FILES": "0"}):
            result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "running", result.message)
        self.assertEqual(len(self.adapter.runs), 1)
        self.assertEqual(self.adapter.peak_files, 1)
        self.assertFalse(list(self.root.rglob("*.mkv")))

    def test_cache_budget_falls_back_to_serial_without_false_failure(self):
        with patch.dict("os.environ", {"RELAY_CACHE_MAX_BYTES": "7"}):
            result = self.executor.poll(self.receipt)
            self.assertEqual(result.status, "running", result.message)
            self.assertFalse(self.adapter.finished[1].is_set())
            result = self._executor().poll(self.receipt)
        self.assertEqual(result.status, "running", result.message)
        self.assertEqual(self.adapter.peak_files, 1)
        self.assertEqual(len(self.native.calls), 2)

    def test_restart_uses_cached_next_episode_and_original_upload_receipts(self):
        self._hold_upload_for_prefetch(1)
        self.executor.poll(self.receipt)
        restored = OperationReceipt(**dataclasses.asdict(self.receipt))
        for _ in range(5):
            result = self._executor().poll(restored)
            if result.status == "completed":
                break
        self.assertEqual(result.status, "completed", result.message)
        self.assertEqual(len(self.native.calls), 4)
        self.assertEqual(len([event for event in self.events if event[0] == "download"]), 4)
        self.assertFalse(list(self.root.rglob("*.mkv")))
        state = self.adapter.store.load()
        self.assertTrue(all(item["state"] == "cleaned" for item in state["files"]))

    def test_unknown_upload_stops_further_prefetch_and_retains_files(self):
        self.adapter.uploading = threading.Event()
        original = self.executor._upload

        def upload(*args, **kwargs):
            self.adapter.uploading.set()
            self.assertTrue(self.adapter.finished[1].wait(2))
            return UploadOutcome("unknown", "remote result unavailable")
        with patch.object(self.executor, "_upload", side_effect=upload):
            result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(len(list(self.root.rglob("*.mkv"))), 2)
        self.assertEqual(len(self.adapter.runs), 2)
        state = self.adapter.store.load()
        self.assertTrue(state["prefetch_paused"])
        self.assertTrue(state["files"][0]["upload_started"])
        # Replaying the same unknown outcome must not open the next window.
        with patch.object(self.executor, "_upload", return_value=UploadOutcome("unknown")):
            self._executor()._sequential.store.load()
            self.executor.poll(self.receipt)
        self.assertEqual(len(self.adapter.runs), 2)

    def test_verified_checkpoint_failure_preserves_cache_and_does_not_reupload(self):
        self._hold_upload_for_prefetch(1)
        update = self.executor._sequential.store.update_entry

        def fail_verified(index, **fields):
            if fields.get("state") == "verified":
                raise OSError("checkpoint failed")
            return update(index, **fields)
        with patch.object(self.executor._sequential.store, "update_entry", side_effect=fail_verified):
            result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "failed", result.message)
        self.assertTrue((self.root / "season/01.mkv").exists())
        result = self._executor().poll(self.receipt)
        self.assertEqual(result.status, "running", result.message)
        self.assertEqual(len(self.native.calls), 1)

    def test_cancel_enumerates_prefetch_receipt_not_only_current_upload(self):
        self.adapter.pending.add(1)
        self.adapter.uploading = threading.Event()
        original = self.native.upload_file

        def upload(**kwargs):
            self.adapter.uploading.set()
            # Deterministically wait for producer admission, not its completion.
            for _ in range(100):
                if len(self.adapter.runs) == 2:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(len(self.adapter.runs), 2)
            return original(**kwargs)
        self.native.upload_file = upload
        result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "running", result.message)
        cancelled = self.executor.cancel(self.receipt)
        self.assertEqual(cancelled.status, "cancelled", cancelled.message)
        self.assertIn("run-1", self.adapter.cancelled)
        self.assertEqual(self.adapter.store.load()["prefetch_paused"], "cancelled")

    def test_no_new_download_after_job_cancellation(self):
        self.executor.cancel_requested = lambda: True
        self.adapter.store.initialize(list(self.files))
        self.adapter.store.enable_pipeline()
        result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "unknown", result.message)
        self.assertEqual(self.adapter.runs, {})
        self.assertEqual(self.native.calls, [])

    def test_cancellation_before_first_manifest_never_submits_first_download(self):
        self.executor.cancel_requested = lambda: True
        result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(self.adapter.runs, {})
        self.assertIsNone(self.adapter.store.load())

    def test_progress_messages_carry_stable_file_identity_and_cache_count(self):
        messages = []
        self.executor.progress_sink = messages.append
        self._hold_upload_for_prefetch(1)
        self.executor.poll(self.receipt)
        buffers = [json.loads(message.split(": ", 1)[1]) for message in messages if message.startswith("relay-buffer:")]
        self.assertTrue(buffers)
        self.assertTrue(all(item["enabled"] and item["depth"] == 1 for item in buffers))
        self.assertTrue(any(item["ready"] == 1 for item in buffers))
        identities = [json.loads(message.split(": ", 1)[1]) for message in messages if message.startswith("relay-file:")]
        self.assertEqual({item["channel"] for item in identities}, {"upload", "download"})
        self.assertTrue(all(item["job_id"] == 1 for item in identities))

    def test_pause_during_log_delivery_does_not_submit_prefetch(self):
        self.adapter.store.initialize(list(self.files))
        self.adapter.store.enable_pipeline()
        self.executor.progress_sink = lambda _line: self.adapter.store.pause_prefetch("upload result unknown")
        result = self.executor._sequential._download_one(0)
        self.assertEqual(result.status, "running")
        self.assertEqual(self.adapter.runs, {})
        self.assertFalse(self.adapter.store.load()["files"][0].get("download_submitting"))

    def test_cancel_after_new_upload_fence_does_not_leave_false_ambiguous_upload(self):
        self.executor.cancel_requested = lambda: bool((self.adapter.store.load() or {}).get("files", [{}])[0].get("upload_started"))
        result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "unknown", result.message)
        self.assertEqual(self.native.calls, [])
        self.assertFalse(self.adapter.store.load()["files"][0].get("upload_started"))
        self.assertEqual(self.executor.cancel(self.receipt).status, "cancelled")

    def test_cancel_during_upload_log_bridge_prevents_native_submission(self):
        def cancel_from_bridge(line):
            if line.startswith("relay-file:") and json.loads(line.split(": ", 1)[1])["channel"] == "upload":
                self.adapter.store.pause_prefetch("cancelled")
        self.executor.progress_sink = cancel_from_bridge
        result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "unknown", result.message)
        self.assertEqual(self.native.calls, [])
        self.assertFalse(self.adapter.store.load()["files"][0].get("upload_started"))


if __name__ == "__main__":
    unittest.main()
