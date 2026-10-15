from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.transfer import ErrorCode
from fnos_media_import.transfer.mobile_relay_executor import CmccMobileUploader
from fnos_media_import.transfer.relay_upload_manifest import write_operation_manifest
from fnos_media_import.transfer.tianyi_uploader import TianyiCloudUploader


class _FakeMobileUploader:
    def __init__(self):
        self.calls = []
        self.items = []
        self.client = SimpleNamespace(list_tree=lambda _parent: list(self.items))

    def upload_file(self, **kwargs):
        self.calls.append(kwargs)
        item = {
            "file_id": f"mobile-{len(self.calls)}",
            "name": kwargs["target_name"],
            "size": Path(kwargs["local_file"]).stat().st_size,
        }
        self.items.append(item)
        return {**item, "success": True}


class _FakeTianyiImporter:
    def __init__(self):
        self.calls = []
        self.items = {"root": []}

    def resolve_folder_locator(self, _kind, _value, *, create):
        return "root"

    def _ensure_folder_path(self, parent, relative):
        current = parent
        for part in relative.split("/"):
            folder_id = f"{current}/{part}"
            if folder_id not in self.items:
                self.items[folder_id] = []
                self.items[current].append({"file_id": folder_id, "name": part, "is_folder": True})
            current = folder_id
        return current

    def upload_file(self, **kwargs):
        self.calls.append(kwargs)
        item = {
            "file_id": f"tianyi-{len(self.calls)}",
            "name": kwargs["target_name"],
            "size": Path(kwargs["local_file"]).stat().st_size,
        }
        self.items[kwargs["parent_folder_id"]].append(item)
        return {**item, "success": True}

    def _list_drive_items(self, folder_id):
        return self.items[folder_id]


class RelayUploaderFileSelectionTests(unittest.TestCase):
    def _providers(self):
        mobile = _FakeMobileUploader()
        yield CmccMobileUploader(mobile), mobile, {"path": "/target", "parent_file_id": "root"}
        tianyi = _FakeTianyiImporter()
        yield TianyiCloudUploader(tianyi), tianyi, {"kind": "folder_id", "path": "root"}

    @staticmethod
    def _upload(uploader, target, root, relative_paths, key="file-1"):
        return uploader.upload_local_dir(
            local_dir=str(root), target_locator=target, idempotency_key=key, relative_paths=relative_paths,
        )

    @staticmethod
    def _manifest(uploader, root, key):
        return uploader._read_operation_manifest(uploader._operation_manifest_path(str(root), key))

    def test_explicit_selection_uploads_only_one_file_without_directory_scan(self):
        for uploader, provider, target in self._providers():
            with self.subTest(provider=uploader.provider_kind), tempfile.TemporaryDirectory() as local_dir:
                root = Path(local_dir)
                (root / "nested").mkdir()
                (root / "nested" / "selected.mkv").write_bytes(b"selected")
                (root / "unselected.mkv").write_bytes(b"cached legacy file")
                (root / "empty.bin").touch()
                (root / ".fnos-relay-upload-old.json").write_text("{}", encoding="utf-8")
                with patch.object(uploader, "_list_local_files", side_effect=AssertionError("must not scan")):
                    result = self._upload(uploader, target, root, ["nested/selected.mkv"])
                self.assertEqual((result.status, result.file_count, result.total_size), ("done", 1, 8))
                self.assertEqual([call["target_name"] for call in provider.calls], ["selected.mkv"])
                self.assertEqual(set(self._manifest(uploader, root, "file-1")["files"]), {"nested/selected.mkv"})

    def test_none_preserves_recursive_upload_compatibility(self):
        for uploader, provider, target in self._providers():
            with self.subTest(provider=uploader.provider_kind), tempfile.TemporaryDirectory() as local_dir:
                root = Path(local_dir)
                (root / "a.mkv").write_bytes(b"a")
                (root / "nested").mkdir()
                (root / "nested" / "b.mkv").write_bytes(b"bb")
                result = self._upload(uploader, target, root, None)
                self.assertEqual((result.status, result.file_count, result.total_size), ("done", 2, 3))
                self.assertEqual(len(provider.calls), 2)

    def test_invalid_or_missing_selection_fails_before_any_provider_upload(self):
        invalid = [
            [], [""], [" "], ["a.mkv", "a.mkv"], ["/a.mkv"], ["C:/a.mkv"],
            ["C:a.mkv"], ["\\\\server\\share\\a.mkv"], ["../a.mkv"], ["nested/../a.mkv"],
            ["./a.mkv"], ["nested//a.mkv"], ["a\x00.mkv"], ["a\\b.mkv"],
            [".fnos-relay-upload-meta.json"], ["a.fnos-cmcc-upload.json"], [3], "a.mkv",
        ]
        for uploader, provider, target in self._providers():
            with tempfile.TemporaryDirectory() as local_dir:
                root = Path(local_dir)
                (root / "a.mkv").write_bytes(b"a")
                (root / "nested").mkdir()
                for selection in invalid:
                    with self.subTest(provider=uploader.provider_kind, selection=selection):
                        result = self._upload(uploader, target, root, selection)
                        self.assertEqual((result.status, result.error_code), ("failed", ErrorCode.INVALID_PLAN))
                for selection in (["missing.mkv"], ["a.mkv", "missing.mkv"], ["nested"]):
                    result = self._upload(uploader, target, root, selection)
                    self.assertEqual((result.status, result.error_code), ("failed", ErrorCode.SOURCE_LANDING_UNAVAILABLE))
                self.assertEqual(provider.calls, [])
                self.assertEqual(list(root.glob(".fnos-relay-upload-*.json")), [])

    def test_symlink_escape_fails_closed(self):
        for uploader, provider, target in self._providers():
            with self.subTest(provider=uploader.provider_kind), tempfile.TemporaryDirectory() as local_dir:
                root = Path(local_dir, "cache")
                root.mkdir()
                outside = Path(local_dir, "outside.mkv")
                outside.write_bytes(b"outside")
                try:
                    (root / "escape.mkv").symlink_to(outside)
                except OSError as exc:
                    self.skipTest(f"symlink creation is unavailable: {exc}")
                result = self._upload(uploader, target, root, ["escape.mkv"])
                self.assertEqual((result.status, result.error_code), ("failed", ErrorCode.INVALID_PLAN))
                self.assertEqual(provider.calls, [])

    def test_completed_file_can_reconcile_and_verify_after_local_media_release(self):
        for uploader, provider, target in self._providers():
            with self.subTest(provider=uploader.provider_kind), tempfile.TemporaryDirectory() as local_dir:
                root = Path(local_dir)
                file = root / "a.mkv"
                file.write_bytes(b"abc")
                self.assertEqual(self._upload(uploader, target, root, [file.name]).status, "done")
                file.unlink()
                self.assertEqual(self._upload(uploader, target, root, [file.name]).status, "done")
                verified = uploader.verify_target(
                    local_dir=str(root), target_locator=target, idempotency_key="file-1",
                    expected_file_count=1, expected_total_size=3,
                )
                self.assertEqual((verified.status, verified.file_count, verified.total_size), ("ok", 1, 3))
                self.assertEqual(len(provider.calls), 1)
                for selection in (["other.mkv"], ["a.mkv", "other.mkv"], [], ["../a.mkv"]):
                    result = self._upload(uploader, target, root, selection)
                    self.assertEqual((result.status, result.error_code), ("failed", ErrorCode.INVALID_PLAN))
                self.assertEqual(len(provider.calls), 1)

    def test_completed_key_cannot_be_rebound_to_another_target(self):
        for uploader, provider, target in self._providers():
            with self.subTest(provider=uploader.provider_kind), tempfile.TemporaryDirectory() as local_dir:
                root = Path(local_dir)
                (root / "a.mkv").write_bytes(b"a")
                self.assertEqual(self._upload(uploader, target, root, ["a.mkv"]).status, "done")
                result = self._upload(uploader, {**target, "path": "other"}, root, ["a.mkv"])
                self.assertEqual((result.status, result.error_code), ("failed", ErrorCode.INVALID_PLAN))
                self.assertEqual(len(provider.calls), 1)

    def test_merge_preserves_native_records_and_verifies_without_local_media(self):
        for uploader, provider, target in self._providers():
            with self.subTest(provider=uploader.provider_kind), tempfile.TemporaryDirectory() as local_dir:
                root = Path(local_dir)
                for index, data in enumerate((b"abc", b"defgh"), start=1):
                    relative = f"part-{index}.mkv"
                    (root / relative).write_bytes(data)
                    self.assertEqual(self._upload(uploader, target, root, [relative], key=f"file-{index}").status, "done")
                    verified = uploader.verify_target(
                        local_dir=str(root), target_locator=target, idempotency_key=f"file-{index}",
                        expected_file_count=1, expected_total_size=len(data),
                    )
                    self.assertEqual(verified.status, "ok")
                    (root / relative).unlink()
                original_records = {
                    relative: record
                    for index in (1, 2)
                    for relative, record in self._manifest(uploader, root, f"file-{index}")["files"].items()
                }
                for _ in range(2):
                    result = uploader.merge_upload_manifests(
                        local_dir=str(root), idempotency_key="task", file_operation_keys=["file-1", "file-2"],
                    )
                    self.assertEqual((result.status, result.file_count, result.total_size), ("done", 2, 8))
                self.assertEqual(self._manifest(uploader, root, "task")["files"], original_records)
                verified = uploader.verify_target(
                    local_dir=str(root), target_locator=target, idempotency_key="task",
                    expected_file_count=2, expected_total_size=8,
                )
                self.assertEqual((verified.status, verified.file_count, verified.total_size), ("ok", 2, 8))
                self.assertEqual(len(provider.calls), 2)

    def test_merge_rejects_invalid_incomplete_mismatched_and_conflicting_receipts(self):
        for uploader, _provider, target in self._providers():
            with self.subTest(provider=uploader.provider_kind), tempfile.TemporaryDirectory() as local_dir:
                root = Path(local_dir)
                (root / "a.mkv").write_bytes(b"abc")
                self.assertEqual(self._upload(uploader, target, root, ["a.mkv"]).status, "done")
                original = self._manifest(uploader, root, "file-1")
                for keys in ([], ["file-1", "file-1"], ["file-1", ""], ["task"]):
                    result = uploader.merge_upload_manifests(local_dir=str(root), idempotency_key="task", file_operation_keys=keys)
                    self.assertEqual((result.status, result.error_code), ("failed", ErrorCode.INVALID_PLAN))
                missing = uploader.merge_upload_manifests(
                    local_dir=str(root), idempotency_key="task", file_operation_keys=["missing"],
                )
                self.assertEqual((missing.status, missing.error_code), ("unknown", ErrorCode.RESULT_UNKNOWN))
                for mutation, code in (
                    ({"status": "uploading"}, ErrorCode.RESULT_UNKNOWN),
                    ({"target_fingerprint": "another-target"}, ErrorCode.INVALID_PLAN),
                    ({"files": {"a.mkv": {"success": True, "size": 99}}}, ErrorCode.INVALID_PLAN),
                    ({"files": {"a.mkv": {"success": False, "size": 3}}}, ErrorCode.RESULT_UNKNOWN),
                ):
                    modified = {**copy.deepcopy(original), "idempotency_key": "file-2", **mutation}
                    uploader._write_operation_manifest(uploader._operation_manifest_path(str(root), "file-2"), modified)
                    result = uploader.merge_upload_manifests(
                        local_dir=str(root), idempotency_key="task", file_operation_keys=["file-1", "file-2"],
                    )
                    self.assertEqual(result.error_code, code)
                    self.assertFalse(Path(uploader._operation_manifest_path(str(root), "task")).exists())

    def test_merge_refuses_to_claim_completion_if_persistence_fails(self):
        for uploader, _provider, target in self._providers():
            with self.subTest(provider=uploader.provider_kind), tempfile.TemporaryDirectory() as local_dir:
                root = Path(local_dir)
                (root / "a.mkv").write_bytes(b"a")
                self.assertEqual(self._upload(uploader, target, root, ["a.mkv"]).status, "done")
                with patch.object(uploader, "_write_operation_manifest", return_value=False):
                    result = uploader.merge_upload_manifests(
                        local_dir=str(root), idempotency_key="task", file_operation_keys=["file-1"],
                    )
                self.assertEqual((result.status, result.error_code), ("unknown", ErrorCode.RESULT_UNKNOWN))

    def test_manifest_persistence_fsyncs_file_and_posix_directory(self):
        with tempfile.TemporaryDirectory() as local_dir:
            path = str(Path(local_dir, "manifest.json"))
            real_fsync = os.fsync
            with patch("fnos_media_import.transfer.relay_upload_manifest.os.fsync", wraps=real_fsync) as fsync:
                self.assertTrue(write_operation_manifest(path, {"status": "completed"}))
            self.assertEqual(fsync.call_count, 2 if os.name == "posix" else 1)
            self.assertFalse(Path(path).read_bytes().startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
