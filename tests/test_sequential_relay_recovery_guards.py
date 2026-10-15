from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.transfer import CancelResult, OperationReceipt, OperationStatus, TransferVerifyResult, UploadOutcome
from fnos_media_import.transfer.sequential_relay import SequentialRelay
from fnos_media_import.transfer.tianyi_uploader import TianyiCloudUploader


class SequentialRelayRecoveryGuardTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "codex_tmp"
        temporary_root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name, "job-1")
        self.root.mkdir()
        self.provider = SimpleNamespace(
            _operation_manifest_path=TianyiCloudUploader._operation_manifest_path,
            _read_operation_manifest=TianyiCloudUploader._read_operation_manifest,
            verify_target=Mock(return_value=TransferVerifyResult("ok", file_count=1, total_size=3)),
            merge_upload_manifests=Mock(return_value=UploadOutcome("done", file_count=1, total_size=3)),
        )
        self.nested = OperationReceipt(
            "download-1", "completed", metadata={"operation_key": "relay-dl:base", "run_id": "1"},
        )
        self.adapter = SimpleNamespace(
            download=Mock(return_value=self.nested), find_download=Mock(return_value=None),
            invalidate=Mock(return_value=True), cancel=Mock(return_value=CancelResult("cancelled")),
        )
        self.executor = SimpleNamespace(
            plan=SimpleNamespace(job_id=1, idempotency_key="base", source_landing={}),
            rclone_adapter=self.adapter, provider_uploader=self.provider,
            _relay_dir=lambda: str(self.root), _destination_target=lambda: {},
            _check_download=Mock(return_value=OperationStatus("completed")),
            _upload=Mock(return_value=UploadOutcome("done", file_count=1, total_size=3)),
            _upload_status=lambda outcome: OperationStatus("completed" if outcome.status == "done" else outcome.status),
            _emit_event=Mock(), verify=Mock(return_value=TransferVerifyResult("ok", file_count=1, total_size=3)),
        )
        self.sequence = SequentialRelay(self.executor)
        self.receipt = self.sequence.submit()

    def _manifest(self, key="relay-up:base", status="completed"):
        path = self.provider._operation_manifest_path(str(self.root), key)
        self.assertTrue(TianyiCloudUploader._write_operation_manifest(path, {
            "version": 1, "idempotency_key": key, "target_fingerprint": "target", "status": status,
            "files": {"a.mkv": {"success": True, "size": 3}},
        }))

    def _state(self, phase="downloaded", *, next_file=False, upload_started=False):
        state = self.sequence.store.initialize(["a.mkv", "b.mkv"] if next_file else ["a.mkv"])
        entry = state["files"][0]
        entry.update(state=phase, size=3, upload_started=upload_started)
        if phase in {"verified", "cleaned"}:
            entry.update(
                upload_key="relay-file-up:0:base",
                verification={"status": "ok", "file_count": 1, "total_size": 3},
            )
        self.sequence.store.save(state)
        return state

    def test_completed_legacy_manifest_is_verified_without_new_download_or_upload(self):
        self._manifest()
        result = self.sequence.poll(self.receipt)
        self.assertEqual((result.status, result.completed_file_count, result.total_size), ("completed", 1, 3))
        self.executor.verify.assert_called_once()
        self.adapter.download.assert_not_called()
        self.adapter.invalidate.assert_not_called()
        self.executor._upload.assert_not_called()
        self.assertIsNone(self.sequence.store.load())

    def test_unfinished_legacy_upload_or_missing_fence_never_gets_new_file_keys(self):
        self._manifest(status="uploading")
        self.assertEqual(self.sequence.poll(self.receipt).status, "unknown")
        Path(self.provider._operation_manifest_path(str(self.root), "relay-up:base")).unlink()
        self.receipt.metadata["step"] = "upload"
        self.assertEqual(self.sequence.poll(self.receipt).status, "unknown")
        self.adapter.download.assert_not_called()
        self.executor._upload.assert_not_called()
        self.executor.verify.assert_not_called()

    def test_legacy_verify_failure_preserves_fence_without_resubmitting(self):
        self._manifest()
        self.executor.verify.return_value = TransferVerifyResult("missing", file_count=0, total_size=0)
        self.assertEqual(self.sequence.poll(self.receipt).status, "unknown")
        self.adapter.download.assert_not_called()
        self.executor._upload.assert_not_called()

    def test_native_upload_start_is_durable_before_provider_call(self):
        self._state()
        media = self.root / "a.mkv"
        media.write_bytes(b"abc")

        def upload(*_args, **_kwargs):
            self.assertIs(self.sequence.store.load()["files"][0]["upload_started"], True)
            return UploadOutcome("unknown")

        self.executor._upload.side_effect = upload
        self.assertEqual(self.sequence.poll(self.receipt).status, "unknown")
        self.assertTrue(media.exists())
        self.assertEqual(self.sequence.poll_for_cancel(self.receipt).status, "unknown")
        self.assertEqual(self.sequence.cancel(self.receipt).status, "cancel_pending")

    def test_failed_upload_start_checkpoint_prevents_native_submission(self):
        self._state()
        media = self.root / "a.mkv"
        media.write_bytes(b"abc")
        with patch.object(self.sequence.store, "save", side_effect=OSError("disk full")):
            self.assertEqual(self.sequence.poll(self.receipt).status, "failed")
        self.executor._upload.assert_not_called()
        self.assertTrue(media.exists())

    def test_cancel_after_verified_or_completed_upload_preserves_external_work(self):
        self._state("verified")
        self.assertEqual(self.sequence.poll_for_cancel(self.receipt).status, "completed")
        self.assertIn("外部操作已完成", self.sequence.cancel(self.receipt).message)
        state = self.sequence.store.load()
        state["files"][0]["state"] = "downloaded"
        self.sequence.store.save(state)
        self._manifest("relay-file-up:0:base")
        self.assertEqual(self.sequence.poll_for_cancel(self.receipt).status, "completed")

    def test_cancel_must_stop_active_next_download_before_preserving_uploaded_files(self):
        self._state("cleaned", next_file=True)
        self.adapter.find_download.return_value = OperationReceipt(
            "download-2", "running", metadata={"operation_key": "relay-file:1:base", "run_id": "2"},
        )
        self.executor._check_download.return_value = OperationStatus("running")
        self.assertEqual(self.sequence.poll_for_cancel(self.receipt).status, "running")
        result = self.sequence.cancel(self.receipt)
        self.adapter.cancel.assert_called_once()
        self.assertEqual(result.status, "cancelled")
        self.assertIn("外部操作已完成", result.message)
        self.executor._upload.assert_not_called()

    def test_missing_media_still_fsyncs_parent_before_cleaned_checkpoint(self):
        self._state("verified")
        fake_os = SimpleNamespace(
            path=os.path, name="posix", O_RDONLY=os.O_RDONLY,
            open=Mock(return_value=42), fsync=Mock(side_effect=OSError("directory flush failed")), close=Mock(),
        )
        with patch("fnos_media_import.transfer.sequential_relay.os", fake_os):
            self.assertEqual(self.sequence.poll(self.receipt).status, "failed")
            self.assertEqual(self.sequence.store.load()["files"][0]["state"], "verified")
            fake_os.fsync.side_effect = None
            self.assertEqual(self.sequence.poll(self.receipt).status, "completed")
        self.assertEqual(fake_os.fsync.call_count, 2)
        self.assertEqual(self.sequence.store.load()["files"][0]["state"], "cleaned")


if __name__ == "__main__":
    unittest.main()
