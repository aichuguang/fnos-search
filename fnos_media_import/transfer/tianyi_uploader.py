"""Tianyi native uploader used by Provider relay and storage probes."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import tempfile
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from fnos_media_import.importers.tianyi import TianyiApiError, TianyiImporter
from fnos_media_import.transfer import (
    ErrorCode,
    TransferVerifyResult,
    UploadOutcome,
)

__all__ = ["TianyiCloudUploader"]


_AUTH_ERROR_CODES = frozenset(
    {
        "AUTH_EXPIRED",
        "InvalidAccessToken",
        "InvalidSessionKey",
        "SESSION_CREDENTIALS_MISSING",
        "SESSION_IP_MISMATCH",
        "UserInvalidOpenToken",
    }
)


class TianyiCloudUploader:
    """Upload a local directory through Tianyi's 189CloudPC API."""

    provider_kind = "tianyi"
    display_name = "天翼云盘"

    def __init__(self, importer: TianyiImporter) -> None:
        self.importer = importer

    @staticmethod
    def _list_local_files(local_dir: str) -> list[tuple[str, str, int]]:
        root = os.path.realpath(str(local_dir or ""))
        if not root or not os.path.isdir(root):
            return []
        result: list[tuple[str, str, int]] = []
        for current, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.startswith(".fnos-relay-upload-"):
                    continue
                full = os.path.realpath(os.path.join(current, name))
                try:
                    if os.path.commonpath((root, full)) != root:
                        continue
                    size = int(os.path.getsize(full))
                except (OSError, ValueError):
                    continue
                relative = posixpath.normpath(
                    os.path.relpath(full, root).replace(os.sep, "/")
                )
                if relative in frozenset({".", ".."}) or relative.startswith("../"):
                    continue
                result.append((full, relative, size))
        return result

    @staticmethod
    def _target_parts(
        target_locator: Mapping[str, Any] | None,
    ) -> tuple[str, str]:
        if not isinstance(target_locator, Mapping):
            return "", ""
        locator_kind = str(
            target_locator.get("locator_kind")
            or target_locator.get("kind")
            or "folder_id"
        ).strip()
        if locator_kind == "native":
            locator_kind = str(
                target_locator.get("native_locator_kind") or "folder_id"
            ).strip()
        value = str(
            target_locator.get("path")
            or target_locator.get("value")
            or target_locator.get("folder_id")
            or ""
        ).strip()
        return locator_kind, value

    def _target_folder_id(
        self,
        target_locator: Mapping[str, Any],
        *,
        create: bool,
    ) -> str:
        locator_kind, value = self._target_parts(target_locator)
        return self.importer.resolve_folder_locator(
            locator_kind,
            value,
            create=create,
        )

    def upload_local_dir(
        self,
        *,
        local_dir: str,
        target_locator: dict[str, Any],
        idempotency_key: str = "",
        progress: Callable[[float], Any] | None = None,
    ) -> UploadOutcome:
        operation_key = str(idempotency_key or "").strip()
        if not operation_key:
            return UploadOutcome(
                "failed",
                "天翼云中继上传缺少幂等键",
                error_code=ErrorCode.INVALID_PLAN,
            )

        locator_kind, locator_value = self._target_parts(target_locator)
        if not locator_kind or not locator_value:
            return UploadOutcome(
                "failed",
                "天翼云中继上传缺少原生目标定位器",
                error_code=ErrorCode.INVALID_PLAN,
            )

        files = self._list_local_files(local_dir)
        if not files:
            return UploadOutcome(
                "failed",
                f"中继目录为空或不存在：{local_dir}",
                error_code=ErrorCode.SOURCE_LANDING_UNAVAILABLE,
            )
        if any(size <= 0 for _path, _relative, size in files):
            return UploadOutcome(
                "failed",
                "天翼云 PC 客户端上传不支持空文件",
                error_code=ErrorCode.INVALID_PLAN,
            )

        manifest_path = self._operation_manifest_path(local_dir, operation_key)
        manifest = self._read_operation_manifest(manifest_path)
        target_fingerprint = json.dumps(
            {"locator_kind": locator_kind, "value": locator_value},
            ensure_ascii=False,
            sort_keys=True,
        )
        fresh = (
            manifest.get("idempotency_key") != operation_key
            or manifest.get("target_fingerprint") != target_fingerprint
        )
        if fresh:
            manifest = {
                "version": 1,
                "provider_kind": self.provider_kind,
                "idempotency_key": operation_key,
                "target_fingerprint": target_fingerprint,
                "status": "uploading",
                "files": {},
            }
        elif str(manifest.get("status") or "").lower() == "completed":
            records = self._manifest_records(manifest)
            completed = [
                item
                for item in records.values()
                if isinstance(item, dict) and item.get("success")
            ]
            return UploadOutcome(
                "done",
                "天翼云上传已按持久 fence 对账完成",
                file_count=len(completed),
                total_size=sum(int(item.get("size") or 0) for item in completed),
            )
        elif str(manifest.get("status") or "").lower() == "uploading":
            return UploadOutcome(
                "unknown",
                "天翼云上传 fence 存在但结果未知",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )

        records = manifest.setdefault("files", {})
        if not isinstance(records, dict):
            records = {}
            manifest["files"] = records
        if not self._write_operation_manifest(manifest_path, manifest):
            return UploadOutcome(
                "unknown",
                "无法持久化天翼云上传 fence",
                error_code=ErrorCode.RESULT_UNKNOWN,
            )

        try:
            root_folder_id = self._target_folder_id(target_locator, create=True)
        except TianyiApiError as exc:
            return self._api_failure(
                exc,
                prefix="天翼云目标目录解析失败",
            )

        callback = progress if callable(progress) else None
        uploaded_count = 0
        uploaded_size = 0
        for index, (full_path, relative_path, size) in enumerate(files, start=1):
            entry = records.get(relative_path)
            if (
                isinstance(entry, dict)
                and entry.get("success")
                and int(entry.get("size") or -1) == size
            ):
                uploaded_count += 1
                uploaded_size += size
                continue

            relative_dir = posixpath.dirname(relative_path)
            try:
                parent_id = (
                    self.importer._ensure_folder_path(root_folder_id, relative_dir)
                    if relative_dir not in frozenset({"", "."})
                    else root_folder_id
                )
                result = self.importer.upload_file(
                    local_file=full_path,
                    parent_folder_id=parent_id,
                    target_name=posixpath.basename(relative_path),
                    overwrite=True,
                    progress=(
                        lambda percent, current=index: callback(
                            ((current - 1) + float(percent) / 100.0)
                            * 100.0
                            / len(files)
                        )
                        if callback is not None
                        else None
                    ),
                )
            except TianyiApiError as exc:
                return self._api_failure(exc, prefix="天翼云上传失败")
            except Exception as exc:  # noqa: BLE001
                return UploadOutcome(
                    "failed",
                    f"天翼云上传异常：{exc}",
                    file_count=uploaded_count,
                    total_size=uploaded_size,
                    error_code=ErrorCode.NETWORK_TRANSIENT,
                )

            record = {
                "success": bool(result.get("success", True)),
                "size": size,
                "relative_path": relative_path,
                "file_id": str(result.get("file_id") or ""),
                "file_name": str(
                    result.get("file_name") or posixpath.basename(relative_path)
                ),
                "parent_folder_id": parent_id,
                "file_md5": str(result.get("file_md5") or ""),
                "rapid_upload": bool(result.get("rapid_upload")),
            }
            records[relative_path] = record
            manifest["status"] = "uploading"
            if not self._write_operation_manifest(manifest_path, manifest):
                return UploadOutcome(
                    "unknown",
                    "无法持久化天翼云逐文件回执",
                    file_count=uploaded_count,
                    total_size=uploaded_size,
                    error_code=ErrorCode.RESULT_UNKNOWN,
                )
            if not record["success"]:
                return UploadOutcome(
                    "failed",
                    f"天翼云上传不完整：{relative_path}",
                    file_count=uploaded_count,
                    total_size=uploaded_size,
                    error_code=ErrorCode.NETWORK_TRANSIENT,
                )
            uploaded_count += 1
            uploaded_size += size

        manifest["status"] = "completed"
        if not self._write_operation_manifest(manifest_path, manifest):
            return UploadOutcome(
                "unknown",
                "无法持久化天翼云完成回执",
                file_count=uploaded_count,
                total_size=uploaded_size,
                error_code=ErrorCode.RESULT_UNKNOWN,
            )
        return UploadOutcome(
            "done",
            "天翼云原生上传完成",
            file_count=uploaded_count,
            total_size=uploaded_size,
        )

    def verify_target(
        self,
        *,
        target_locator: dict[str, Any],
        expected_file_count: int = 0,
        expected_total_size: int = 0,
        idempotency_key: str = "",
        local_dir: str = "",
    ) -> TransferVerifyResult:
        operation_key = str(idempotency_key or "").strip()
        manifest = (
            self._read_operation_manifest(
                self._operation_manifest_path(local_dir, operation_key)
            )
            if local_dir and operation_key
            else {}
        )
        if str(manifest.get("status") or "").lower() != "completed":
            return TransferVerifyResult(
                "unknown",
                "天翼云上传 fence 缺失或不完整",
                code=ErrorCode.RESULT_UNKNOWN,
            )
        records = self._manifest_records(manifest)
        expected_records = {
            str(path): item
            for path, item in records.items()
            if isinstance(item, dict) and item.get("success")
        }

        try:
            folder_id = self._target_folder_id(target_locator, create=False)
            if not folder_id:
                return TransferVerifyResult(
                    "missing",
                    "天翼云目标目录不存在",
                    code=ErrorCode.VERIFY_MISMATCH,
                )
            actual = self._walk_files(folder_id)
        except TianyiApiError as exc:
            if exc.code in _AUTH_ERROR_CODES:
                return TransferVerifyResult(
                    "unknown",
                    "天翼云认证已失效，无法校验目标",
                    code=ErrorCode.RESULT_UNKNOWN,
                )
            return TransferVerifyResult(
                "unknown",
                f"天翼云目标校验失败：{exc}",
                code=ErrorCode.RESULT_UNKNOWN,
            )

        by_id = {
            str(item.get("file_id") or ""): item
            for item in actual
            if item.get("file_id")
        }
        by_path = {str(item.get("relative_path") or ""): item for item in actual}
        missing: list[str] = []
        verified_size = 0
        for relative_path, record in expected_records.items():
            file_id = str(record.get("file_id") or "")
            actual_item = by_id.get(file_id) if file_id else by_path.get(relative_path)
            if actual_item is None:
                missing.append(relative_path)
                continue
            actual_size = int(actual_item.get("size") or 0)
            record_size = int(record.get("size") or 0)
            if actual_size != record_size:
                missing.append(f"{relative_path}(size)")
                continue
            verified_size += actual_size

        verified_count = len(expected_records) - len(missing)
        manifest_size = sum(
            int(item.get("size") or 0) for item in expected_records.values()
        )
        problems: list[str] = []
        if missing:
            problems.append("缺失：" + "、".join(missing[:10]))
        if expected_file_count > 0 and len(expected_records) != expected_file_count:
            problems.append(
                f"fence 文件数 {len(expected_records)} != 期望 {expected_file_count}"
            )
        if expected_total_size > 0 and manifest_size != expected_total_size:
            problems.append(f"fence 总大小 {manifest_size} != 期望 {expected_total_size}")
        if problems:
            return TransferVerifyResult(
                "missing",
                "; ".join(problems),
                file_count=verified_count,
                total_size=verified_size,
                expected_file_count=expected_file_count,
                expected_total_size=expected_total_size,
                code=ErrorCode.VERIFY_MISMATCH,
            )
        return TransferVerifyResult(
            "ok",
            "天翼云原生上传目标校验通过",
            file_count=len(expected_records),
            total_size=manifest_size,
            expected_file_count=expected_file_count,
            expected_total_size=expected_total_size,
        )

    def probe_target(
        self,
        *,
        target_locator: dict[str, Any],
        observer: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Upload, verify, observe OpenList mutation, and clean a tiny file."""

        # No leading dot: OpenList hides dotfiles, so a ``.fnos-native-probe``
        # upload succeeds on Cloud189 and then stays invisible over WebDAV,
        # failing the organizer mutation check for a file that is really there.
        file_name = f"fnos-native-probe-{uuid.uuid4().hex}.txt"
        content = b"fnos-tianyi-native-upload-probe"
        uploaded: dict[str, Any] = {}
        observer_result: dict[str, Any] = {}
        deleted_by_observer = False
        try:
            folder_id = self._target_folder_id(target_locator, create=False)
            if not folder_id:
                raise TianyiApiError(
                    "天翼云目标目录不存在",
                    code="TARGET_FOLDER_MISSING",
                )
            with tempfile.TemporaryDirectory(prefix="fnos-tianyi-probe-") as temp_dir:
                local_file = os.path.join(temp_dir, file_name)
                with open(local_file, "wb") as handle:
                    handle.write(content)
                uploaded = self.importer.upload_file(
                    local_file=local_file,
                    parent_folder_id=folder_id,
                    target_name=file_name,
                    overwrite=False,
                )
            file_id = str(uploaded.get("file_id") or "")
            listed = next(
                (
                    item
                    for item in self.importer._list_drive_items(folder_id)
                    if str(item.get("file_id") or "") == file_id
                    or str(item.get("name") or "") == file_name
                ),
                None,
            )
            if not isinstance(listed, dict) or int(listed.get("size") or 0) != len(
                content
            ):
                raise TianyiApiError(
                    "Cloud189 native upload probe could not verify the file",
                    code="UPLOAD_PROBE_VERIFY_FAILED",
                )
            if callable(observer):
                value = observer(
                    {
                        "file_id": file_id,
                        "file_name": file_name,
                        "file_size": len(content),
                        "content": content,
                    }
                )
                observer_result = dict(value or {}) if isinstance(value, Mapping) else {}
                deleted_by_observer = bool(observer_result.get("deleted"))
            organizer_verified = bool(
                observer_result.get("organizer_mutation_verified")
            )
            if callable(observer) and not organizer_verified:
                result = {
                    "ok": False,
                    "upload_verified": True,
                    "organizer_mutation_verified": False,
                    "code": "OPENLIST_MUTATION_PROBE_FAILED",
                    "message": str(
                        observer_result.get("message")
                        or "OpenList could not read, move and delete the native probe file"
                    ),
                }
            else:
                result = {
                    "ok": True,
                    "upload_verified": True,
                    "organizer_mutation_verified": organizer_verified,
                    "message": (
                        "天翼云原生上传及 OpenList 整理链路检测通过"
                        if organizer_verified
                        else "天翼云原生上传检测通过"
                    ),
                }
        except TianyiApiError as exc:
            result = {
                "ok": False,
                "upload_verified": False,
                "organizer_mutation_verified": False,
                "code": str(exc.code or "PROVIDER_UPLOAD_PROBE_FAILED"),
                "message": str(exc),
            }
        except Exception as exc:  # noqa: BLE001
            result = {
                "ok": False,
                "upload_verified": False,
                "organizer_mutation_verified": False,
                "code": "PROVIDER_UPLOAD_PROBE_FAILED",
                "message": str(exc),
            }

        if uploaded and not deleted_by_observer:
            try:
                cleaned = self.importer.delete_drive_item(
                    file_id=str(uploaded.get("file_id") or ""),
                    file_name=str(uploaded.get("file_name") or file_name),
                )
                if not cleaned:
                    raise TianyiApiError(
                        "Cloud189 native upload probe cleanup was not confirmed",
                        code="UPLOAD_PROBE_CLEANUP_FAILED",
                    )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "upload_verified": False,
                    "organizer_mutation_verified": False,
                    "code": "UPLOAD_PROBE_CLEANUP_FAILED",
                    "message": f"Cloud189 native upload probe cleanup failed: {exc}",
                }
        return result

    def list_folders(
        self,
        parent_folder_id: str = "-11",
    ) -> list[dict[str, Any]]:
        return self.importer.list_drive_folders(parent_folder_id)

    def _walk_files(self, root_folder_id: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        queue = [(str(root_folder_id), "", 0)]
        visited: set[str] = set()
        while queue and len(files) < 10000:
            folder_id, relative_root, depth = queue.pop(0)
            if folder_id in visited or depth > 32:
                continue
            visited.add(folder_id)
            for item in self.importer._list_drive_items(folder_id):
                name = str(item.get("name") or "")
                relative = (
                    posixpath.join(relative_root, name) if relative_root else name
                )
                if item.get("is_folder"):
                    child_id = str(item.get("file_id") or "")
                    if not child_id:
                        continue
                    queue.append((child_id, relative, depth + 1))
                    continue
                files.append({**dict(item), "relative_path": relative})
        return files

    @staticmethod
    def _operation_manifest_path(local_dir: str, operation_key: str) -> str:
        digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:24]
        return os.path.join(
            os.path.realpath(str(local_dir or "")),
            f".fnos-relay-upload-{digest}.json",
        )

    @staticmethod
    def _manifest_records(manifest: Mapping[str, Any]) -> dict[str, Any]:
        records = manifest.get("files")
        return dict(records) if isinstance(records, Mapping) else {}

    @staticmethod
    def _read_operation_manifest(path: str) -> dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, TypeError, ValueError):
            return {}

    @staticmethod
    def _write_operation_manifest(path: str, value: dict[str, Any]) -> bool:
        directory = os.path.dirname(path) or "."
        temp_path = ""
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=".fnos-relay-upload-",
                suffix=".tmp",
                dir=directory,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            return True
        except OSError:
            try:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
            return False

    @staticmethod
    def _api_failure(exc: TianyiApiError, *, prefix: str) -> UploadOutcome:
        code = str(exc.code or "")
        if code in _AUTH_ERROR_CODES or exc.http_status in frozenset({401, 403}):
            return UploadOutcome(
                "auth_expired",
                "天翼云认证已失效，请重新登录",
                error_code=ErrorCode.AUTH_EXPIRED,
            )
        retryable = code.startswith("HTTP_5") or code in frozenset(
            {"BATCH_TASK_TIMEOUT", "UPLOAD_PROBE_VERIFY_FAILED"}
        )
        return UploadOutcome(
            "failed",
            f"{prefix}: {exc}",
            error_code=(
                ErrorCode.NETWORK_TRANSIENT
                if retryable
                else ErrorCode.AUTH_CONFIG
                if code == "SESSION_CREDENTIALS_MISSING"
                else ErrorCode.NETWORK_TRANSIENT
            ),
        )
