from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import base64
import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from fnos_media_import.importers.tianyi import TianyiApiError, TianyiImporter
from fnos_media_import.transfer import ErrorCode
from fnos_media_import.transfer.tianyi_uploader import TianyiCloudUploader


class _Response(RepositoryFakeMixin):
    def __init__(
        self,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = dict(payload or {})
        self.text = text
        self.content = text.encode("utf-8")
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return dict(self._payload)


class TianyiUploadProtocolTests(unittest.TestCase):
    def _importer(self) -> TianyiImporter:
        return TianyiImporter(
            {
                "session_key": "session-key",
                "session_secret": "0123456789abcdef",
            }
        )

    def test_aes_ecb_pkcs7_returns_openlist_uppercase_hex(self) -> None:
        encrypted = TianyiImporter._aes_ecb_encrypt(
            "a=1&b=2",
            "0123456789abcdef",
        )
        self.assertEqual(encrypted, encrypted.upper())
        decrypted = unpad(
            AES.new(b"0123456789abcdef", AES.MODE_ECB).decrypt(
                bytes.fromhex(encrypted)
            ),
            AES.block_size,
        ).decode("utf-8")
        self.assertEqual(decrypted, "a=1&b=2")

    def test_signature_matches_openlist_189pc_source(self) -> None:
        date = "Sun, 09 Aug 2026 00:00:00 GMT"
        source = (
            "SessionKey=session-key&Operate=GET"
            "&RequestURI=/person/initMultiUpload&Date="
            f"{date}&params=ABCDEF"
        )
        expected = hmac.new(
            b"0123456789abcdef",
            source.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest().upper()
        headers = TianyiImporter._pc_signature_headers(
            "https://upload.cloud.189.cn/person/initMultiUpload",
            "GET",
            session_key="session-key",
            session_secret="0123456789abcdef",
            params_data="ABCDEF",
            date=date,
            request_id="request-id",
        )
        self.assertEqual(headers["Signature"], expected)
        self.assertEqual(headers["SessionKey"], "session-key")
        self.assertEqual(headers["X-Request-ID"], "request-id")

    def test_upload_request_sorts_then_encrypts_params(self) -> None:
        importer = self._importer()
        captured: dict[str, Any] = {}

        def request(method: str, url: str, **kwargs: Any) -> _Response:
            captured.update(method=method, url=url, **kwargs)
            return _Response(200, {})

        importer.http.session.request = request  # type: ignore[method-assign]
        value = importer._upload_request(
            "GET",
            "/person/initMultiUpload",
            params={"b": 2, "a": 1},
        )
        self.assertEqual(value, {})
        encrypted = str(captured["params"]["params"])
        decrypted = unpad(
            AES.new(b"0123456789abcdef", AES.MODE_ECB).decrypt(
                bytes.fromhex(encrypted)
            ),
            AES.block_size,
        ).decode("utf-8")
        self.assertEqual(decrypted, "a=1&b=2")
        self.assertRegex(encrypted, r"^[0-9A-F]+$")
        self.assertTrue(captured["headers"]["Signature"])

    def test_hashes_use_ten_mib_parts_and_openlist_part_info(self) -> None:
        first = b"a" * (10 * 1024 * 1024)
        second = b"tail"
        with tempfile.TemporaryDirectory() as temp_dir:
            local_file = Path(temp_dir) / "payload.bin"
            local_file.write_bytes(first + second)
            slice_size = TianyiImporter._upload_part_size(local_file.stat().st_size)
            file_md5, slice_md5, parts = TianyiImporter._upload_hashes(
                str(local_file),
                local_file.stat().st_size,
                slice_size,
            )
        first_digest = hashlib.md5(first).digest()  # noqa: S324
        second_digest = hashlib.md5(second).digest()  # noqa: S324
        self.assertEqual(slice_size, 10 * 1024 * 1024)
        self.assertEqual(file_md5, hashlib.md5(first + second).hexdigest().upper())  # noqa: S324
        self.assertEqual(
            parts[0]["part_info"],
            "1-" + base64.b64encode(first_digest).decode("ascii"),
        )
        self.assertEqual(
            parts[1]["part_info"],
            "2-" + base64.b64encode(second_digest).decode("ascii"),
        )
        joined = "\n".join(
            (first_digest.hex().upper(), second_digest.hex().upper())
        )
        self.assertEqual(
            slice_md5,
            hashlib.md5(joined.encode("utf-8")).hexdigest().upper(),  # noqa: S324
        )

    def test_upload_file_runs_init_parts_and_commit(self) -> None:
        importer = self._importer()
        calls: list[tuple[str, dict[str, Any]]] = []
        put_calls: list[tuple[str, int, int]] = []

        def upload_request(
            _method: str,
            path: str,
            *,
            params: dict[str, Any],
            retried: bool = False,
        ) -> dict[str, Any]:
            del retried
            calls.append((path, dict(params)))
            if path.endswith("initMultiUpload"):
                return {"data": {"uploadFileId": "upload-1", "fileDataExists": 0}}
            if path.endswith("getMultiUploadUrls"):
                part_number = str(params["partInfo"]).split("-", 1)[0]
                return {
                    "uploadUrls": {
                        f"partNumber_{part_number}": {
                            "requestURL": f"https://upload.invalid/{part_number}",
                            "requestHeader": "x-test=1",
                        }
                    }
                }
            if path.endswith("commitMultiUploadFile"):
                return {
                    "file": {
                        "userFileId": "file-1",
                        "fileName": "payload.bin",
                        "fileSize": 7,
                    }
                }
            raise AssertionError(path)

        def put_part(
            request_url: str,
            _headers: dict[str, str],
            _local_file: str,
            offset: int,
            size: int,
        ) -> None:
            put_calls.append((request_url, offset, size))

        importer._upload_request = upload_request  # type: ignore[method-assign]
        importer._put_upload_part = put_part  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            local_file = Path(temp_dir) / "payload.bin"
            local_file.write_bytes(b"payload")
            result = importer.upload_file(
                local_file=str(local_file),
                parent_folder_id="root",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["file_id"], "file-1")
        self.assertEqual(
            [path.rsplit("/", 1)[-1] for path, _params in calls],
            ["initMultiUpload", "getMultiUploadUrls", "commitMultiUploadFile"],
        )
        self.assertEqual(len(put_calls), 1)
        self.assertFalse(result["rapid_upload"])

    def test_rapid_upload_skips_presigned_put_and_still_commits(self) -> None:
        importer = self._importer()
        calls: list[str] = []

        def upload_request(
            _method: str,
            path: str,
            *,
            params: dict[str, Any],
            retried: bool = False,
        ) -> dict[str, Any]:
            del params, retried
            calls.append(path)
            if path.endswith("initMultiUpload"):
                return {"data": {"uploadFileId": "upload-rapid", "fileDataExists": 1}}
            if path.endswith("commitMultiUploadFile"):
                return {"file": {"userFileId": "file-rapid"}}
            raise AssertionError(path)

        importer._upload_request = upload_request  # type: ignore[method-assign]
        importer._put_upload_part = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: self.fail("rapid upload must not PUT parts")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            local_file = Path(temp_dir) / "payload.bin"
            local_file.write_bytes(b"payload")
            result = importer.upload_file(
                local_file=str(local_file),
                parent_folder_id="root",
            )
        self.assertEqual(
            [path.rsplit("/", 1)[-1] for path in calls],
            ["initMultiUpload", "commitMultiUploadFile"],
        )
        self.assertTrue(result["rapid_upload"])

    def test_presigned_put_requires_http_200(self) -> None:
        importer = self._importer()
        importer.http.session.put = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: _Response(201, {}, "created")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            local_file = Path(temp_dir) / "payload.bin"
            local_file.write_bytes(b"payload")
            with patch("fnos_media_import.importers.tianyi.time.sleep"):
                with self.assertRaises(TianyiApiError) as raised:
                    importer._put_upload_part(
                        "https://upload.invalid/part",
                        {},
                        str(local_file),
                        0,
                        7,
                    )
        self.assertEqual(raised.exception.http_status, 201)
        self.assertEqual(raised.exception.stage, "upload_part")


class _FakeDriveImporter(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.upload_calls: list[dict[str, Any]] = []
        self.folder_calls: list[tuple[str, str]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.delete_ok = True
        self.items: dict[str, list[dict[str, Any]]] = {"root": []}

    def resolve_folder_locator(
        self,
        locator_kind: str,
        locator_value: str,
        *,
        create: bool,
    ) -> str:
        del create
        if locator_kind != "folder_id":
            raise TianyiApiError("invalid locator", code="TARGET_LOCATOR_INVALID")
        return str(locator_value) if str(locator_value) in self.items else ""

    def _ensure_folder_path(self, root_folder_id: str, path: str) -> str:
        current = str(root_folder_id)
        for segment in str(path or "").split("/"):
            if not segment:
                continue
            self.folder_calls.append((current, segment))
            existing = next(
                (
                    item
                    for item in self.items.setdefault(current, [])
                    if item.get("is_folder") and item.get("name") == segment
                ),
                None,
            )
            if existing is None:
                child_id = f"folder-{len(self.items)}"
                existing = {
                    "file_id": child_id,
                    "name": segment,
                    "size": 0,
                    "is_folder": True,
                }
                self.items[current].append(existing)
                self.items[child_id] = []
            current = str(existing["file_id"])
        return current

    def upload_file(self, **kwargs: Any) -> dict[str, Any]:
        call = dict(kwargs)
        self.upload_calls.append(call)
        local_file = Path(str(kwargs["local_file"]))
        file_id = f"file-{len(self.upload_calls)}"
        target_name = str(kwargs["target_name"])
        size = local_file.stat().st_size
        parent_folder_id = str(kwargs["parent_folder_id"])
        self.items.setdefault(parent_folder_id, []).append(
            {
                "file_id": file_id,
                "name": target_name,
                "size": size,
                "is_folder": False,
            }
        )
        progress = kwargs.get("progress")
        if callable(progress):
            progress(100.0)
        return {
            "success": True,
            "file_id": file_id,
            "file_name": target_name,
            "file_size": size,
            "file_md5": hashlib.md5(local_file.read_bytes()).hexdigest().upper(),  # noqa: S324
            "rapid_upload": False,
        }

    def _list_drive_items(self, folder_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.items.get(str(folder_id), [])]

    def delete_drive_item(self, **kwargs: Any) -> bool:
        self.delete_calls.append(dict(kwargs))
        return bool(self.delete_ok)

    def list_drive_folders(self, parent_folder_id: str) -> list[dict[str, Any]]:
        return [
            {
                "folder_id": str(item.get("file_id") or ""),
                "name": str(item.get("name") or ""),
                "parent_folder_id": str(parent_folder_id),
            }
            for item in self.items.get(str(parent_folder_id), [])
            if item.get("is_folder")
        ]


class TianyiCloudUploaderTests(unittest.TestCase):
    target = {"kind": "folder_id", "path": "root"}

    def test_recursive_upload_manifest_idempotency_and_exact_verify(self) -> None:
        importer = _FakeDriveImporter()
        uploader = TianyiCloudUploader(importer)  # type: ignore[arg-type]
        progress: list[float] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_bytes(b"abc")
            (root / "nested").mkdir()
            (root / "nested" / "b.bin").write_bytes(b"1234")
            first = uploader.upload_local_dir(
                local_dir=str(root),
                target_locator=self.target,
                idempotency_key="operation-1",
                progress=progress.append,
            )
            second = uploader.upload_local_dir(
                local_dir=str(root),
                target_locator=self.target,
                idempotency_key="operation-1",
            )
            verified = uploader.verify_target(
                target_locator=self.target,
                expected_file_count=2,
                expected_total_size=7,
                idempotency_key="operation-1",
                local_dir=str(root),
            )
        self.assertEqual(first.status, "done")
        self.assertEqual(second.status, "done")
        self.assertEqual(verified.status, "ok")
        self.assertEqual(verified.file_count, 2)
        self.assertEqual(verified.total_size, 7)
        self.assertEqual(len(importer.upload_calls), 2)
        self.assertEqual(importer.folder_calls, [("root", "nested")])
        self.assertTrue(progress)

    def test_verify_fails_when_provider_file_size_changes(self) -> None:
        importer = _FakeDriveImporter()
        uploader = TianyiCloudUploader(importer)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_bytes(b"abc")
            outcome = uploader.upload_local_dir(
                local_dir=str(root),
                target_locator=self.target,
                idempotency_key="operation-size",
            )
            self.assertEqual(outcome.status, "done")
            importer.items["root"][0]["size"] = 99
            verified = uploader.verify_target(
                target_locator=self.target,
                expected_file_count=1,
                expected_total_size=3,
                idempotency_key="operation-size",
                local_dir=str(root),
            )
        self.assertEqual(verified.status, "missing")
        self.assertIn("size", verified.message)

    def test_probe_file_name_is_not_hidden_from_openlist(self) -> None:
        """A dot-prefixed probe name uploads fine but stays invisible over WebDAV.

        The organizer half of the probe then fails on a file that is really
        there, so the name must not start with a dot.
        """

        importer = _FakeDriveImporter()
        uploader = TianyiCloudUploader(importer)  # type: ignore[arg-type]

        uploader.probe_target(target_locator=self.target)

        self.assertEqual(len(importer.upload_calls), 1)
        target_name = str(importer.upload_calls[0]["target_name"])
        self.assertFalse(target_name.startswith("."))
        self.assertTrue(target_name.startswith("fnos-native-probe-"))

    def test_probe_cleanup_failure_fails_closed(self) -> None:
        importer = _FakeDriveImporter()
        importer.delete_ok = False
        uploader = TianyiCloudUploader(importer)  # type: ignore[arg-type]
        result = uploader.probe_target(target_locator=self.target)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "UPLOAD_PROBE_CLEANUP_FAILED")
        self.assertEqual(len(importer.delete_calls), 1)


class TianyiUploadPreSubmissionTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "codex_tmp"
        temporary_root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "movie.mkv").write_bytes(b"isolated payload")
        self.importer = TianyiImporter({"session_key": "isolated", "session_secret": "0123456789abcdef"})
        self.importer.resolve_folder_locator = Mock(return_value="root")

        def upload_request(_method, path, *, params, **_kwargs):
            if path.endswith("initMultiUpload"):
                return {"data": {"uploadFileId": "upload-1", "fileDataExists": 1}}
            if path.endswith("commitMultiUploadFile"):
                return {"file": {"userFileId": "file-1"}}
            raise AssertionError(f"Unexpected upload API: {path}")

        self.importer._upload_request = Mock(side_effect=upload_request)
        self.uploader = TianyiCloudUploader(self.importer)
        self.kwargs = {
            "local_dir": str(self.root),
            "target_locator": {"kind": "folder_id", "path": "root"},
            "idempotency_key": "local-preflight-test",
        }

    def _manifest(self):
        return self.uploader._read_operation_manifest(
            self.uploader._operation_manifest_path(str(self.root), self.kwargs["idempotency_key"])
        )

    def test_local_read_failure_can_retry_without_unknown_result(self):
        with patch.object(self.importer, "_upload_hashes", side_effect=PermissionError("temporary local read failure")):
            first = self.uploader.upload_local_dir(**self.kwargs)
        self.assertEqual(first.status, "failed")
        self.assertEqual(self._manifest()["status"], "prepared")
        self.importer._upload_request.assert_not_called()
        second = self.uploader.upload_local_dir(**self.kwargs)
        self.assertEqual(second.status, "done")
        self.assertEqual(self.importer._upload_request.call_count, 2)

    def test_changed_local_file_during_hash_is_known_not_submitted(self):
        with patch.object(self.importer, "_upload_hashes", side_effect=TianyiApiError(
            "source changed", code="LOCAL_FILE_CHANGED", stage="upload_hash",
        )):
            first = self.uploader.upload_local_dir(**self.kwargs)
        self.assertEqual(first.status, "failed")
        self.assertEqual(self._manifest()["status"], "prepared")
        self.importer._upload_request.assert_not_called()
        self.assertEqual(self.uploader.upload_local_dir(**self.kwargs).status, "done")

    def test_local_failure_after_upload_init_still_keeps_unknown_fence(self):
        self.importer._upload_request.side_effect = [
            {"data": {"uploadFileId": "upload-1", "fileDataExists": 0}},
            {"uploadUrls": {"partNumber_1": {"requestURL": "https://upload.invalid/unused", "requestHeader": ""}}},
        ]
        self.importer._put_upload_part = Mock(side_effect=PermissionError("read failed after init"))
        first = self.uploader.upload_local_dir(**self.kwargs)
        self.assertEqual(first.status, "failed")
        self.assertEqual(self._manifest()["status"], "uploading")
        second = self.uploader.upload_local_dir(**self.kwargs)
        self.assertEqual((second.status, second.error_code), ("unknown", ErrorCode.RESULT_UNKNOWN))
        self.assertEqual(self.importer._upload_request.call_count, 2)
        self.importer._put_upload_part.assert_called_once()

    def test_upload_init_unknown_response_is_never_treated_as_local_failure(self):
        self.importer._upload_request.side_effect = TianyiApiError("response lost", code="NETWORK_ERROR")
        first = self.uploader.upload_local_dir(**self.kwargs)
        second = self.uploader.upload_local_dir(**self.kwargs)
        self.assertEqual(first.status, "failed")
        self.assertEqual((second.status, second.error_code), ("unknown", ErrorCode.RESULT_UNKNOWN))
        self.importer._upload_request.assert_called_once()

    def test_failed_prepared_fence_write_remains_fail_closed(self):
        write = self.uploader._write_operation_manifest
        writes = 0

        def fail_reset(path, value):
            nonlocal writes
            writes += 1
            return False if writes == 3 else write(path, value)

        with patch.object(self.importer, "_upload_hashes", side_effect=PermissionError("local read failed")):
            with patch.object(self.uploader, "_write_operation_manifest", side_effect=fail_reset):
                first = self.uploader.upload_local_dir(**self.kwargs)
        self.assertEqual((first.status, first.error_code), ("unknown", ErrorCode.RESULT_UNKNOWN))
        self.assertEqual(self._manifest()["status"], "uploading")
        self.assertEqual(self.uploader.upload_local_dir(**self.kwargs).status, "unknown")
        self.importer._upload_request.assert_not_called()

    def test_retry_after_second_file_hash_failure_does_not_reupload_first(self):
        (self.root / "a.mkv").write_bytes(b"first file")
        hashes = self.importer._upload_hashes

        def fail_second(path, file_size, slice_size):
            if Path(path).name == "movie.mkv":
                raise PermissionError("second file unavailable")
            return hashes(path, file_size, slice_size)

        with patch.object(self.importer, "_upload_hashes", side_effect=fail_second):
            first = self.uploader.upload_local_dir(**self.kwargs)
        self.assertEqual(first.status, "failed")
        self.assertEqual(self._manifest()["status"], "prepared")
        self.assertTrue(self._manifest()["files"]["a.mkv"]["success"])
        self.assertEqual(self.importer._upload_request.call_count, 2)
        self.assertEqual(self.uploader.upload_local_dir(**self.kwargs).status, "done")
        self.assertEqual(self.importer._upload_request.call_count, 4)


if __name__ == "__main__":
    unittest.main()
