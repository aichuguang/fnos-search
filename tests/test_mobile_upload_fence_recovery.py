from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.cmcc.auth import CmccAuthConfigError, CmccAuthExpired
from fnos_media_import.cmcc.client import CmccApiError
from fnos_media_import.cmcc.uploader import CmccUploadResult
from fnos_media_import.transfer import ErrorCode, TransferPlan
from fnos_media_import.transfer.mobile_relay_executor import CmccMobileUploader
from fnos_media_import.transfer.provider_relay_executor import ProviderRelayExecutor
from tests.test_sequential_relay import _Downloads
from tests.test_transfer_v1 import make_plan


class _NativeUploader:
    def __init__(self) -> None:
        self.calls = []
        self.reads = []
        self.items = {
            "/": [{"fileId": "root", "name": "target", "type": "folder"}],
            "root": [{"fileId": "season", "name": "season", "type": "folder"}],
            "season": [],
        }
        self.client = SimpleNamespace(list_dir=self.list_dir, check_exists=self.check_exists)

    def list_dir(self, parent_id):
        self.reads.append(parent_id)
        return copy.deepcopy(self.items.get(parent_id, []))

    def check_exists(self, *, parent_file_id, file_name):
        for item in self.list_dir(parent_file_id):
            if item["name"] == file_name:
                return {"exist": True, "file_id": item["fileId"], "file_type": item["type"]}
        return {"exist": False}

    def upload_file(self, **kwargs):
        self.calls.append(kwargs)
        size = Path(kwargs["local_file"]).stat().st_size
        parent_id = "season" if kwargs["target_relative_dir"] == "season" else "root"
        remote_id = f"file-{len(self.calls)}"
        self.items[parent_id].append({
            "fileId": remote_id, "name": kwargs["target_name"], "size": size, "type": "file",
        })
        return CmccUploadResult(
            True, "done", "Fake upload complete", file_id=remote_id,
            parent_file_id=parent_id, target_name=kwargs["target_name"], size=size,
        )


class MobileUploadFenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="mobile-fence-recovery-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve() / "job-1"
        self.media = self.root / "season/movie.mkv"
        self.media.parent.mkdir(parents=True)
        self.media.write_bytes(b"abc")
        self.native = _NativeUploader()
        self.uploader = CmccMobileUploader(self.native)
        self.kwargs = {
            "local_dir": str(self.root),
            "target_locator": {"path": "/target", "parent_file_id": "root"},
            "idempotency_key": "relay-file-up:0:frozen-account-revision-and-plan",
            "relative_paths": ["season/movie.mkv"],
        }
        self.path = Path(self.uploader._operation_manifest_path(str(self.root), self.kwargs["idempotency_key"]))

    def _read(self):
        return self.uploader._read_operation_manifest(str(self.path))

    def _write(self, value):
        self.assertTrue(self.uploader._write_operation_manifest(str(self.path), value))

    def _lose_completion(self):
        write = self.uploader._write_operation_manifest

        def fail_completed(path, value):
            return False if value.get("status") == "completed" else write(path, value)

        with patch.object(self.uploader, "_write_operation_manifest", side_effect=fail_completed):
            result = self.uploader.upload_local_dir(**self.kwargs)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(self._read()["status"], "uploading")

    def _resume(self, **overrides):
        # A new wrapper models restarting the worker; only durable state remains.
        return CmccMobileUploader(self.native).upload_local_dir(**{**self.kwargs, **overrides})

    def _assert_unknown_unchanged(self, **overrides):
        before = self.path.read_bytes()
        result = self._resume(**overrides)
        self.assertEqual((result.status, result.error_code), ("unknown", ErrorCode.RESULT_UNKNOWN))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.native.calls), 1)
        self.assertTrue(self.media.exists())

    def test_last_completion_write_failure_reconciles_without_upload(self):
        self._lose_completion()
        original = self._read()
        with patch.object(self.native, "upload_file", side_effect=AssertionError("must not upload again")):
            result = self._resume()
        self.assertEqual((result.status, result.file_count, result.total_size), ("done", 1, 3))
        self.assertEqual(self._read(), {**original, "status": "completed"})
        self.assertEqual(len(self.native.calls), 1)
        self.assertTrue(self.media.exists(), "Upload reconciliation must not delete the local source")
        verified = self.uploader.verify_target(
            target_locator=self.kwargs["target_locator"], expected_file_count=1, expected_total_size=3,
            idempotency_key=self.kwargs["idempotency_key"], local_dir=str(self.root),
        )
        self.assertEqual(verified.status, "ok")

    def test_missing_size_id_name_and_directory_mismatch_stay_unknown(self):
        self._lose_completion()
        original = copy.deepcopy(self.native.items["season"])
        cases = [[], [{**original[0], "size": 4}], [{**original[0], "fileId": "different-file"}],
                 [{**original[0], "name": "other.mkv"}], [{**original[0], "type": "folder"}],
                 original + [{**original[0], "fileId": "ambiguous-duplicate"}]]
        for items in cases:
            with self.subTest(items=items):
                self.native.items["season"] = items
                self._assert_unknown_unchanged()

    def test_remote_file_in_another_directory_cannot_confirm_ownership(self):
        self._lose_completion()
        self.native.items["root"].extend(self.native.items["season"])
        self.native.items["season"] = []
        self._assert_unknown_unchanged()

    def test_success_receipt_must_include_complete_matching_file_identity(self):
        self._lose_completion()
        original = self._read()
        relative = self.kwargs["relative_paths"][0]
        for patch_record, patch_result in (
            ({"success": False}, {}), ({"size": -1}, {}), ({"size": "3"}, {}),
            ({}, {"success": False}), ({}, {"status": "failed"}), ({}, {"status": []}), ({}, {"size": 4}),
            ({}, {"file_id": ""}), ({}, {"parent_file_id": "elsewhere"}),
            ({}, {"target_name": "other.mkv"}),
        ):
            with self.subTest(record=patch_record, result=patch_result):
                changed = copy.deepcopy(original)
                changed["files"][relative].update(patch_record)
                changed["files"][relative]["result"].update(patch_result)
                self._write(changed)
                self._assert_unknown_unchanged()
        self._write({**original, "files": {}})
        self._assert_unknown_unchanged()

    def test_changed_selection_or_missing_expected_file_stays_unknown(self):
        self._lose_completion()
        self._assert_unknown_unchanged(relative_paths=["season/other.mkv"])
        self._assert_unknown_unchanged(relative_paths=["season/movie.mkv", "season/other.mkv"])

    def test_wrong_operation_or_target_fingerprint_never_overwrites_even_without_selection(self):
        self._lose_completion()
        original = self._read()
        for field, value in (("idempotency_key", "another-operation"), ("target_fingerprint", "another-account-target")):
            for selection in (self.kwargs["relative_paths"], None):
                with self.subTest(field=field, selection=selection):
                    self._write({**original, field: value})
                    self._assert_unknown_unchanged(relative_paths=selection)
        self.assertEqual(self.native.reads, [], "Identity must be checked before any remote observation")

    def test_frozen_adapter_auth_or_network_failure_remains_unknown(self):
        self._lose_completion()
        for error in (CmccAuthExpired("expired"), CmccAuthConfigError("unavailable"),
                      CmccApiError("wrong account", auth_expired=True), TimeoutError("unavailable")):
            with self.subTest(error=type(error).__name__), patch.object(self.native.client, "list_dir", side_effect=error):
                self._assert_unknown_unchanged()

    def test_recovery_completion_write_failure_stays_unknown_and_can_retry_readonly(self):
        self._lose_completion()
        before = self.path.read_bytes()
        restored = CmccMobileUploader(self.native)
        with patch.object(restored, "_write_operation_manifest", return_value=False):
            result = restored.upload_local_dir(**self.kwargs)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self._resume().status, "done")
        self.assertEqual(len(self.native.calls), 1)

    def test_legacy_scope_requires_explicit_frozen_selection_not_remaining_local_files(self):
        self._lose_completion()
        original = self._read()
        original.pop("relative_paths")
        self._write(original)
        self._assert_unknown_unchanged(relative_paths=None)
        self.assertEqual(self._resume().status, "done")
        self.assertEqual(len(self.native.calls), 1)

    def test_persisted_scope_is_not_replaced_by_remaining_local_files(self):
        self._lose_completion()
        original = self._read()
        original["relative_paths"].append("season/another-original-file.mkv")
        self._write(original)
        self._assert_unknown_unchanged(relative_paths=None)

    def test_path_only_target_is_resolved_readonly(self):
        self.kwargs["target_locator"] = {"path": "/target"}
        self._lose_completion()
        self.assertEqual(self._resume().status, "done")
        self.assertIn("/", self.native.reads)
        self.assertEqual(len(self.native.calls), 1)

    def test_corrupt_or_unsupported_fence_is_never_treated_as_new_upload(self):
        self._lose_completion()
        original = self._read()
        self.path.write_text("not-json", encoding="utf-8")
        self._assert_unknown_unchanged()
        self._write({**original, "status": "unrecognized"})
        self._assert_unknown_unchanged()
        self._write({**original, "version": 2})
        self._assert_unknown_unchanged()

    def test_newer_receipt_written_during_observation_is_not_overwritten(self):
        self._lose_completion()
        original_list = self.native.list_dir
        updated = {**self._read(), "diagnostic": "another worker changed this receipt"}

        def concurrent_read(parent_id):
            self._write(updated)
            return original_list(parent_id)

        with patch.object(self.native.client, "list_dir", side_effect=concurrent_read):
            self.assertEqual(self._resume().status, "unknown")
        self.assertEqual(self._read(), updated)
        self.assertEqual(len(self.native.calls), 1)

    def test_sequential_relay_only_releases_cache_after_existing_remote_verification(self):
        # Use the real coordinator and its fake download worker, without a DB or network.
        self.media.unlink()
        adapter = _Downloads({"season/movie.mkv": b"abc"}, [])
        plan = TransferPlan.from_dict(make_plan(
            dest_kind="mobile", strategy="relay_upload_provider", landing="/source/job-1", final="/target",
            local_temp=self.root.as_posix(), expected_file_count=1, expected_total_size=3,
        ))

        def executor():
            return ProviderRelayExecutor(adapter, self.uploader, relay_root=self.root.parent.as_posix()).bind(plan)

        current = executor()
        receipt = current.submit()
        write = self.uploader._write_operation_manifest
        with patch.object(self.uploader, "_write_operation_manifest", side_effect=lambda path, value: False if value.get("status") == "completed" else write(path, value)):
            self.assertEqual(current.poll(receipt).status, "unknown")
        self.assertTrue(self.media.exists())
        self.assertEqual(adapter.store.load()["files"][0]["state"], "downloaded")
        resumed = executor().poll(receipt)
        self.assertEqual(resumed.status, "completed", resumed.message)
        self.assertEqual(adapter.store.load()["files"][0]["state"], "cleaned")
        self.assertFalse(self.media.exists())
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(executor().verify().status, "ok")


if __name__ == "__main__":
    unittest.main()
