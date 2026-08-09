"""移动云 Provider 的本地中继上传器。

链路：来源原生保存到落地区 -> rclone 下载到 /data/relay/job-<id> -> 移动云 API 上传
（CmccUploader）-> 目录结果校验 -> ACK -> 清理落地区与本地缓存。

移动云大文件通过 OpenList 直传存在卡 100% 的问题，因此跨盘写移动云唯一例外链路为
「本地中继 + 移动云上传 API」。rclone 只负责下载；移动云 Adapter 负责秒传、完整上传、
认证错误与结果确认。

认证失效：停止同账号后续上传任务并在后台生成账号告警，不进行盲重试（§10.4）。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import posixpath
import tempfile
import uuid
from typing import Any

from fnos_media_import.cmcc.auth import CmccAuthConfigError, CmccAuthExpired
from fnos_media_import.cmcc.client import CmccApiError
from fnos_media_import.cmcc.uploader import CmccUploadResult, CmccUploader
from fnos_media_import.transfer import (
    ErrorCode,
    TransferError,
    TransferVerifyResult,
    UploadOutcome,
)
__all__ = ["CmccMobileUploader"]


class CmccMobileUploader:
    """把现有 CmccUploader 包装为 relay 链路的移动云上传器。

    对 /data/relay/job-<id> 下每个文件调用 CmccUploader.upload_file，随后读取
    目标目录清单做文件数与总大小校验。认证过期以 CmccAuthExpired / auth_expired
    显式返回，不做盲重试。
    """

    provider_kind = "mobile"
    display_name = "\u79fb\u52a8\u4e91\u76d8"

    def __init__(
        self,
        uploader: CmccUploader | None = None,
        *,
        target_parent_path: str = "",
        progress: Any = None,
    ) -> None:
        self.uploader = uploader or CmccUploader()
        self.target_parent_path = str(target_parent_path or "")
        self.progress = progress

    def _list_local_files(self, local_dir: str) -> list[tuple[str, str, int]]:
        root = str(local_dir or "")
        if not root or not os.path.isdir(root):
            return []
        root_real = os.path.realpath(root)
        result: list[tuple[str, str, int]] = []
        for current, _dirs, files in os.walk(root_real):
            for name in sorted(files):
                # These are executor/uploader bookkeeping files, never source
                # content.  Uploading them was a subtle relay corruption bug.
                if name.startswith(".fnos-relay-upload-") or name.endswith(".fnos-cmcc-upload.json"):
                    continue
                full = os.path.realpath(os.path.join(current, name))
                try:
                    if os.path.commonpath((root_real, full)) != root_real:
                        continue
                    size = os.path.getsize(full)
                except (OSError, ValueError):
                    continue
                rel = os.path.relpath(full, root_real).replace(os.sep, "/")
                rel = posixpath.normpath(rel)
                if rel.startswith("../") or rel in {"..", "."}:
                    continue
                result.append((full, rel, int(size)))
        return result

    def upload_local_dir(
        self,
        *,
        local_dir: str,
        target_locator: dict[str, str],
        idempotency_key: str = "",
        progress: Any = None,
    ) -> UploadOutcome:
        operation_key = str(idempotency_key or "").strip()
        if not operation_key:
            return UploadOutcome("failed", "mobile relay upload requires an idempotency key", error_code=ErrorCode.INVALID_PLAN)
        if not isinstance(target_locator, dict):
            return UploadOutcome("failed", "mobile relay upload requires an explicit target locator", error_code=ErrorCode.INVALID_PLAN)
        target_path = str(target_locator.get("path") or "").strip()
        target_parent_id = str(
            target_locator.get("parent_file_id")
            or target_locator.get("parentFileId")
            or ""
        ).strip()
        if not target_path and not target_parent_id:
            return UploadOutcome("failed", "mobile relay target path or parent id is required", error_code=ErrorCode.INVALID_PLAN)

        files = self._list_local_files(local_dir)
        if not files:
            return UploadOutcome("failed", f"relay directory is empty or missing: {local_dir}", error_code=ErrorCode.SOURCE_LANDING_UNAVAILABLE)

        manifest_path = self._operation_manifest_path(local_dir, operation_key)
        manifest = self._read_operation_manifest(manifest_path)
        target_fingerprint = json.dumps(
            {"path": target_path, "parent_file_id": target_parent_id},
            ensure_ascii=False,
            sort_keys=True,
        )
        fresh_manifest = (
            manifest.get("idempotency_key") != operation_key
            or manifest.get("target_fingerprint") != target_fingerprint
        )
        if fresh_manifest:
            manifest = {
                "version": 1,
                "idempotency_key": operation_key,
                "target_fingerprint": target_fingerprint,
                "status": "uploading",
                "files": {},
            }
        elif str(manifest.get("status") or "").lower() == "completed":
            records = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
            return UploadOutcome(
                "done",
                "mobile upload already reconciled from durable manifest",
                file_count=sum(1 for item in records.values() if isinstance(item, dict) and item.get("success")),
                total_size=sum(int(item.get("size") or 0) for item in records.values() if isinstance(item, dict) and item.get("success")),
            )
        elif str(manifest.get("status") or "").lower() == "uploading":
            # The previous process may have crashed after the Provider call
            # but before recording its response.  Do not submit the same
            # operation again; an operator/provider reconcile is required.
            return UploadOutcome("unknown", "移动云上传 fence 存在但结果未知")
        records = manifest.setdefault("files", {})
        if not self._write_operation_manifest(manifest_path, manifest):
            return UploadOutcome("unknown", "无法持久化移动云上传 fence", error_code=ErrorCode.RESULT_UNKNOWN)
        uploaded: list[CmccUploadResult] = []
        callback = progress or self.progress
        for full_path, relative_path, size in files:
            entry = records.get(relative_path)
            if isinstance(entry, dict) and entry.get("success") and int(entry.get("size") or -1) == size:
                uploaded.append(_cmcc_result_from_dict(entry.get("result"), relative_path, size))
                continue
            file_key = f"{operation_key}:file:{hashlib.sha256(relative_path.encode('utf-8')).hexdigest()[:24]}"
            kwargs: dict[str, Any] = {
                "local_file": full_path,
                "target_parent_file_id": target_parent_id,
                "target_parent_path": target_path,
                "target_name": posixpath.basename(relative_path),
                "target_relative_dir": posixpath.dirname(relative_path) if posixpath.dirname(relative_path) != "." else "",
                "progress": callback,
            }
            try:
                result = self._call_upload_file(kwargs, file_key)
            except CmccAuthExpired:
                return UploadOutcome("auth_expired", "mobile account authorization expired")
            except CmccAuthConfigError as exc:
                return UploadOutcome("failed", f"mobile credential configuration error: {exc}", error_code=ErrorCode.AUTH_CONFIG)
            except CmccApiError as exc:
                return UploadOutcome(
                    "auth_expired" if exc.auth_expired else "failed",
                    f"mobile upload failed: {exc}",
                    error_code=ErrorCode.AUTH_EXPIRED if exc.auth_expired else ErrorCode.NETWORK_TRANSIENT,
                )
            except TransferError as exc:
                return UploadOutcome("failed", exc.message, error_code=exc.code)
            except Exception as exc:  # noqa: BLE001
                return UploadOutcome("failed", f"mobile upload exception: {exc}", error_code=ErrorCode.NETWORK_TRANSIENT)
            result = _coerce_cmcc_result(result, relative_path, size)
            records[relative_path] = {"success": bool(result.success), "size": size, "result": result.to_dict()}
            manifest["status"] = "uploading"
            if not self._write_operation_manifest(manifest_path, manifest):
                return UploadOutcome("unknown", "无法持久化移动云逐文件回执", error_code=ErrorCode.RESULT_UNKNOWN)
            if not result.success:
                return UploadOutcome(
                    "failed",
                    f"mobile upload incomplete: {result.message}",
                    file_count=len(uploaded),
                    total_size=sum(int(item.size or 0) for item in uploaded),
                    error_code=ErrorCode.NETWORK_TRANSIENT,
                )
            uploaded.append(result)

        manifest["status"] = "completed"
        if not self._write_operation_manifest(manifest_path, manifest):
            return UploadOutcome("unknown", "无法持久化移动云完成回执", error_code=ErrorCode.RESULT_UNKNOWN)
        total_size = sum(int(item.size or 0) for item in uploaded)
        return UploadOutcome("done", "mobile upload completed", file_count=len(uploaded), total_size=total_size)

    def _call_upload_file(self, kwargs: dict[str, Any], file_key: str) -> Any:
        method = getattr(self.uploader, "upload_file", None)
        if not callable(method):
            raise TransferError(ErrorCode.AUTH_CONFIG, "mobile uploader does not expose upload_file")
        try:
            params = inspect.signature(method).parameters
        except (TypeError, ValueError):
            params = {}
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if "target_relative_dir" not in params and not accepts_kwargs:
            raise TransferError(ErrorCode.INVALID_PLAN, "mobile uploader must support target_relative_dir")
        if "idempotency_key" in params or accepts_kwargs:
            kwargs["idempotency_key"] = file_key
        if not accepts_kwargs:
            kwargs = {key: value for key, value in kwargs.items() if key in params}
        return method(**kwargs)

    @staticmethod
    def _operation_manifest_path(local_dir: str, operation_key: str) -> str:
        digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:24]
        return os.path.join(os.path.realpath(local_dir), f".fnos-relay-upload-{digest}.json")

    @staticmethod
    def _read_operation_manifest(path: str) -> dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _write_operation_manifest(path: str, value: dict[str, Any]) -> bool:
        directory = os.path.dirname(path) or "."
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".fnos-relay-upload-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            return True
        except OSError:
            try:
                if "temp_path" in locals() and os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
            return False

    def _list_all_files(self, parent_id: str) -> list[dict[str, Any]]:
        """Recursively enumerate a Mobile Cloud directory when possible."""

        client = self.uploader.client
        for method_name in ("list_tree", "walk_dir", "list_recursive"):
            method = getattr(client, method_name, None)
            if callable(method):
                try:
                    value = method(parent_id)
                    if isinstance(value, list):
                        return [item for item in value if isinstance(item, dict)]
                except Exception:
                    # Fall back to the conservative recursive list below.
                    pass
        result: list[dict[str, Any]] = []
        queue: list[tuple[str, int]] = [(str(parent_id), 0)]
        visited: set[str] = set()
        while queue and len(result) < 10000:
            current, depth = queue.pop(0)
            if current in visited or depth > 32:
                continue
            visited.add(current)
            entries = client.list_dir(current)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                if _item_is_dir(item):
                    child = str(item.get("file_id") or item.get("fileId") or item.get("id") or "")
                    if child:
                        queue.append((child, depth + 1))
                else:
                    result.append(item)
        return result


    def verify_target(
        self,
        *,
        target_locator: dict[str, str],
        expected_file_count: int,
        expected_total_size: int,
        idempotency_key: str = "",
        local_dir: str = "",
    ) -> TransferVerifyResult:
        if not isinstance(target_locator, dict):
            return TransferVerifyResult("unknown", "mobile relay target locator is missing", code=ErrorCode.INVALID_PLAN)
        target_path = str(target_locator.get("path") or "").strip()
        parent_id = str(target_locator.get("parent_file_id") or target_locator.get("parentFileId") or "").strip()
        if not target_path and not parent_id:
            return TransferVerifyResult("unknown", "mobile relay target path or parent id is required", code=ErrorCode.INVALID_PLAN)
        if not parent_id:
            parent_id = self._resolve_readonly(target_path)
        if not parent_id:
            return TransferVerifyResult("missing", f"\u79fb\u52a8\u4e91\u76ee\u6807\u76ee\u5f55\u4e0d\u5b58\u5728\uff1a{target_path}", code=ErrorCode.VERIFY_MISMATCH)
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
                "\u79fb\u52a8\u4e91\u4e0a\u4f20 manifest \u7f3a\u5931\u6216\u672a\u5b8c\u6210",
                code=ErrorCode.RESULT_UNKNOWN,
            )
        records = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
        expected_records = {
            str(path): item
            for path, item in records.items()
            if isinstance(item, dict) and item.get("success")
        }
        try:
            items = self._list_all_files(parent_id)
        except CmccAuthExpired:
            return TransferVerifyResult("unknown", "\u79fb\u52a8\u4e91\u8ba4\u8bc1\u5df2\u8fc7\u671f\uff0c\u65e0\u6cd5\u786e\u8ba4\u76ee\u6807", code=ErrorCode.RESULT_UNKNOWN)
        except CmccApiError as exc:
            return TransferVerifyResult(
                "unknown" if exc.auth_expired else "unknown",
                f"\u79fb\u52a8\u4e91\u76ee\u5f55\u8bfb\u53d6\u5931\u8d25\uff1a{exc}",
                code=ErrorCode.RESULT_UNKNOWN,
            )
        files = [item for item in items if not _item_is_dir(item)]
        files_by_id = {
            str(item.get("file_id") or item.get("fileId") or item.get("id") or ""): item
            for item in files
            if str(item.get("file_id") or item.get("fileId") or item.get("id") or "")
        }
        missing: list[str] = []
        verified_size = 0
        for relative_path, record in expected_records.items():
            result = record.get("result") if isinstance(record.get("result"), dict) else {}
            file_id = str(result.get("file_id") or "")
            actual = files_by_id.get(file_id) if file_id else None
            if actual is None:
                missing.append(relative_path)
                continue
            actual_size = int(actual.get("size") or actual.get("fileSize") or 0)
            record_size = int(record.get("size") or 0)
            if actual_size != record_size:
                missing.append(f"{relative_path}(size)")
                continue
            verified_size += actual_size
        manifest_size = sum(
            int(item.get("size") or 0)
            for item in expected_records.values()
        )
        count = len(expected_records) - len(missing)
        problems: list[str] = []
        if missing:
            problems.append("missing: " + ", ".join(missing[:10]))
        if expected_file_count > 0 and len(expected_records) != expected_file_count:
            problems.append(
                f"manifest files {len(expected_records)} != {expected_file_count}"
            )
        if expected_total_size > 0 and manifest_size != expected_total_size:
            problems.append(
                f"manifest size {manifest_size} != {expected_total_size}"
            )
        if problems:
            return TransferVerifyResult(
                "missing",
                "; ".join(problems),
                file_count=count,
                total_size=verified_size,
                expected_file_count=expected_file_count,
                expected_total_size=expected_total_size,
                code=ErrorCode.VERIFY_MISMATCH,
            )
        return TransferVerifyResult(
            "ok",
            "\u79fb\u52a8\u4e91\u76ee\u6807\u6821\u9a8c\u901a\u8fc7",
            file_count=len(expected_records),
            total_size=manifest_size,
            expected_file_count=expected_file_count,
            expected_total_size=expected_total_size,
        )

    def probe_target(
        self,
        *,
        target_locator: dict[str, Any],
        observer: Any = None,
    ) -> dict[str, Any]:
        """Verify native upload plus the shared OpenList organizer surface."""

        if not isinstance(target_locator, dict):
            return {
                "ok": False,
                "upload_verified": False,
                "organizer_mutation_verified": False,
                "code": "TARGET_LOCATOR_INVALID",
                "message": "mobile native upload probe requires a target locator",
            }
        target_path = str(
            target_locator.get("path")
            or target_locator.get("value")
            or ""
        ).strip()
        target_parent_id = str(
            target_locator.get("parent_file_id")
            or target_locator.get("parentFileId")
            or ""
        ).strip()
        if not target_path and not target_parent_id:
            return {
                "ok": False,
                "upload_verified": False,
                "organizer_mutation_verified": False,
                "code": "TARGET_LOCATOR_INVALID",
                "message": "mobile native upload probe target is empty",
            }
        file_name = f"fnos-native-probe-{uuid.uuid4().hex}.txt"
        content = b"fnos-mobile-native-upload-probe"
        try:
            with tempfile.TemporaryDirectory(prefix="fnos-mobile-probe-") as temp_dir:
                local_file = os.path.join(temp_dir, file_name)
                with open(local_file, "wb") as handle:
                    handle.write(content)
                value = self.uploader.upload_file(
                    local_file=local_file,
                    target_parent_file_id=target_parent_id,
                    target_parent_path=target_path,
                    target_name=file_name,
                    rename_mode="force_rename",
                )
            result = _coerce_cmcc_result(value, file_name, len(content))
            if not result.success:
                return {
                    "ok": False,
                    "upload_verified": False,
                    "organizer_mutation_verified": False,
                    "code": "PROVIDER_UPLOAD_PROBE_FAILED",
                    "message": result.message or "mobile native upload probe failed",
                }
            observer_result: dict[str, Any] = {}
            if callable(observer):
                observed = observer(
                    {
                        "file_id": result.file_id,
                        "file_name": result.target_name or file_name,
                        "file_size": len(content),
                        "content": content,
                    }
                )
                observer_result = dict(observed or {}) if isinstance(observed, dict) else {}
            organizer_verified = bool(
                observer_result.get("organizer_mutation_verified")
            )
            if callable(observer) and not organizer_verified:
                return {
                    "ok": False,
                    "upload_verified": True,
                    "organizer_mutation_verified": False,
                    "code": "OPENLIST_MUTATION_PROBE_FAILED",
                    "message": str(
                        observer_result.get("message")
                        or "OpenList could not read, move and delete the mobile probe file"
                    ),
                }
            return {
                "ok": True,
                "upload_verified": True,
                "organizer_mutation_verified": organizer_verified,
                "message": "\u79fb\u52a8\u4e91\u76d8\u539f\u751f\u4e0a\u4f20\u4e0e OpenList \u6574\u7406\u94fe\u8def\u68c0\u6d4b\u901a\u8fc7",
            }
        except CmccAuthExpired:
            return {
                "ok": False,
                "upload_verified": False,
                "organizer_mutation_verified": False,
                "code": "AUTH_EXPIRED",
                "message": "\u79fb\u52a8\u4e91\u76d8\u8ba4\u8bc1\u5df2\u8fc7\u671f",
            }
        except (CmccAuthConfigError, CmccApiError) as exc:
            return {
                "ok": False,
                "upload_verified": False,
                "organizer_mutation_verified": False,
                "code": "PROVIDER_UPLOAD_PROBE_FAILED",
                "message": str(exc),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "upload_verified": False,
                "organizer_mutation_verified": False,
                "code": "PROVIDER_UPLOAD_PROBE_FAILED",
                "message": str(exc),
            }

    def _resolve_readonly(self, cloud_path: str) -> str:
        current = "/"
        for segment in str(cloud_path or "").strip("/").split("/"):
            if not segment:
                continue
            try:
                exists = self.uploader.client.check_exists(parent_file_id=current, file_name=segment)
            except CmccAuthExpired:
                raise
            except Exception:  # noqa: BLE001
                return ""
            if not exists.get("exist") or not exists.get("file_id"):
                return ""
            current = str(exists["file_id"])
        return current



def _upload_outcome_dict(outcome: UploadOutcome) -> dict[str, Any]:
    return {
        "status": outcome.status,
        "message": outcome.message,
        "file_count": int(outcome.file_count or 0),
        "total_size": int(outcome.total_size or 0),
        "error_code": outcome.error_code,
    }


def _coerce_cmcc_result(value: Any, relative_path: str, size: int) -> CmccUploadResult:
    if isinstance(value, CmccUploadResult):
        return value
    if isinstance(value, dict):
        payload = dict(value)
        return CmccUploadResult(
            bool(payload.get("success")),
            str(payload.get("status") or ("done" if payload.get("success") else "failed")),
            str(payload.get("message") or ""),
            mode=str(payload.get("mode") or ""),
            file_id=str(payload.get("file_id") or payload.get("fileId") or ""),
            upload_id=str(payload.get("upload_id") or payload.get("uploadId") or ""),
            parent_file_id=str(payload.get("parent_file_id") or payload.get("parentFileId") or ""),
            target_name=str(payload.get("target_name") or posixpath.basename(relative_path)),
            size=int(payload.get("size") or size),
            sha256=str(payload.get("sha256") or ""),
            manifest_path=str(payload.get("manifest_path") or ""),
            raw=dict(payload.get("raw") or {}),
        )
    return CmccUploadResult(False, "failed", f"unsupported mobile upload result: {type(value).__name__}", target_name=relative_path, size=size)


def _cmcc_result_from_dict(value: Any, relative_path: str, size: int) -> CmccUploadResult:
    return _coerce_cmcc_result(value if isinstance(value, dict) else {}, relative_path, size)


def _item_is_dir(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("is_dir") is True or item.get("isDir") is True or item.get("dir") is True:
        return True
    type_text = str(item.get("file_type") or item.get("fileType") or item.get("type") or "").strip().lower()
    return type_text in {"dir", "folder", "catalog", "directory"}
