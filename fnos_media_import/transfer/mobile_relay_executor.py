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
from fnos_media_import.transfer.relay_upload_manifest import (
    merge_completed_upload_manifests,
    selected_local_files,
    validate_manifest_selection,
    validate_relative_paths,
    write_operation_manifest,
)
__all__ = ["CmccMobileUploader"]

MOBILE_UPLOAD_UNCONFIRMED_MESSAGE = (
    "检测到移动云上传记录，但成功结果尚未核实；"
    "已暂停以避免重复上传，请保留原任务和现有文件。"
)


class CmccMobileUploader:
    """把现有 CmccUploader 包装为 relay 链路的移动云上传器。

    对 /data/relay/job-<id> 下每个文件调用 CmccUploader.upload_file，随后读取
    目标目录清单做文件数与总大小校验。认证过期以 CmccAuthExpired / auth_expired
    显式返回，不做盲重试。
    """

    provider_kind = "mobile"
    display_name = "移动云盘"

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
        relative_paths: list[str] | None = None,
    ) -> UploadOutcome:
        operation_key = str(idempotency_key or "").strip()
        if not operation_key:
            return UploadOutcome("failed", "移动云中继上传缺少幂等键", error_code=ErrorCode.INVALID_PLAN)
        if not isinstance(target_locator, dict):
            return UploadOutcome("failed", "移动云中继上传缺少明确的目标定位器", error_code=ErrorCode.INVALID_PLAN)
        target_path = str(target_locator.get("path") or "").strip()
        target_parent_id = str(
            target_locator.get("parent_file_id")
            or target_locator.get("parentFileId")
            or ""
        ).strip()
        if not target_path and not target_parent_id:
            return UploadOutcome("failed", "移动云中继目标路径和父目录 ID 不能同时为空", error_code=ErrorCode.INVALID_PLAN)

        try:
            selection = validate_relative_paths(local_dir, relative_paths) if relative_paths is not None else None
        except TransferError as exc:
            return UploadOutcome("failed", exc.message, error_code=exc.code)
        files = self._list_local_files(local_dir) if selection is None else []
        if selection is None and not files:
            return UploadOutcome("failed", f"中继目录为空或不存在：{local_dir}", error_code=ErrorCode.SOURCE_LANDING_UNAVAILABLE)

        manifest_path = self._operation_manifest_path(local_dir, operation_key)
        invalid_manifest_file = os.path.lexists(manifest_path) and (
            os.path.islink(manifest_path) or not os.path.isfile(manifest_path)
        )
        manifest = {} if invalid_manifest_file else self._read_operation_manifest(manifest_path)
        if os.path.lexists(manifest_path) and (invalid_manifest_file or not manifest):
            return UploadOutcome("unknown", "移动云上传回执无法读取，保留现场等待复核", error_code=ErrorCode.RESULT_UNKNOWN)
        target_fingerprint = json.dumps(
            {"path": target_path, "parent_file_id": target_parent_id},
            ensure_ascii=False,
            sort_keys=True,
        )
        fresh_manifest = (
            manifest.get("idempotency_key") != operation_key
            or manifest.get("target_fingerprint") != target_fingerprint
        )
        interrupted = str(manifest.get("status") or "").lower() == "uploading"
        if manifest and fresh_manifest:
            return UploadOutcome(
                "unknown" if interrupted else "failed", "中继上传幂等回执目标不一致",
                error_code=ErrorCode.RESULT_UNKNOWN if interrupted else ErrorCode.INVALID_PLAN,
            )
        if manifest and (
            type(manifest.get("version")) is not int or manifest["version"] != 1
            or str(manifest.get("status") or "").lower() not in {"uploading", "completed"}
        ):
            return UploadOutcome("unknown", "移动云上传回执状态无法确认，保留现场等待复核", error_code=ErrorCode.RESULT_UNKNOWN)
        if selection is not None and not fresh_manifest:
            try:
                validate_manifest_selection(manifest, selection)
            except TransferError as exc:
                return UploadOutcome(
                    "unknown" if interrupted else "failed", exc.message,
                    error_code=ErrorCode.RESULT_UNKNOWN if interrupted else exc.code,
                )
        if fresh_manifest:
            manifest = {
                "version": 1,
                "idempotency_key": operation_key,
                "target_fingerprint": target_fingerprint,
                "status": "uploading",
                "files": {},
                "relative_paths": selection if selection is not None else [relative for _, relative, _ in files],
            }
        elif str(manifest.get("status") or "").lower() == "completed":
            records = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
            return UploadOutcome(
                "done",
                "移动云上传已根据现有回执确认完成",
                file_count=sum(1 for item in records.values() if isinstance(item, dict) and item.get("success")),
                total_size=sum(int(item.get("size") or 0) for item in records.values() if isinstance(item, dict) and item.get("success")),
            )
        elif str(manifest.get("status") or "").lower() == "uploading":
            # Only reconcile complete success receipts. Never call upload_file
            # again: the original process may have lost its final checkpoint.
            return self._reconcile_upload_fence(
                manifest_path=manifest_path,
                manifest=manifest,
                target_path=target_path,
                target_parent_id=target_parent_id,
                relative_paths=selection,
            )
        if selection is not None:
            try:
                files = selected_local_files(local_dir, selection)
            except TransferError as exc:
                return UploadOutcome("failed", exc.message, error_code=exc.code)
        records = manifest.setdefault("files", {})
        if not self._write_operation_manifest(manifest_path, manifest):
            return UploadOutcome("unknown", "无法保存移动云上传记录，已暂停以避免重复上传", error_code=ErrorCode.RESULT_UNKNOWN)
        uploaded: list[CmccUploadResult] = []
        callback = progress or self.progress
        for full_path, relative_path, size in files:
            entry = records.get(relative_path)
            if isinstance(entry, dict) and entry.get("success") and int(entry.get("size") or -1) == size:
                uploaded.append(_cmcc_result_from_dict(entry.get("result"), relative_path, size))
                continue
            kwargs: dict[str, Any] = {
                "local_file": full_path,
                "target_parent_file_id": target_parent_id,
                "target_parent_path": target_path,
                "target_name": posixpath.basename(relative_path),
                "target_relative_dir": posixpath.dirname(relative_path) if posixpath.dirname(relative_path) != "." else "",
                "progress": callback,
            }
            try:
                result = self._call_upload_file(kwargs)
            except CmccAuthExpired:
                return UploadOutcome("auth_expired", "移动云账号授权已过期")
            except CmccAuthConfigError as exc:
                return UploadOutcome("failed", f"移动云凭据配置错误：{exc}", error_code=ErrorCode.AUTH_CONFIG)
            except CmccApiError as exc:
                return UploadOutcome(
                    "auth_expired" if exc.auth_expired else "failed",
                    f"移动云上传失败：{exc}",
                    error_code=ErrorCode.AUTH_EXPIRED if exc.auth_expired else ErrorCode.NETWORK_TRANSIENT,
                )
            except TransferError as exc:
                return UploadOutcome("failed", exc.message, error_code=exc.code)
            except Exception as exc:  # noqa: BLE001
                return UploadOutcome("failed", f"移动云上传异常：{exc}", error_code=ErrorCode.NETWORK_TRANSIENT)
            result = _coerce_cmcc_result(result, relative_path, size)
            records[relative_path] = {"success": bool(result.success), "size": size, "result": result.to_dict()}
            manifest["status"] = "uploading"
            if not self._write_operation_manifest(manifest_path, manifest):
                return UploadOutcome("unknown", "无法持久化移动云逐文件回执", error_code=ErrorCode.RESULT_UNKNOWN)
            if not result.success:
                return UploadOutcome(
                    "failed",
                    f"移动云上传不完整：{result.message}",
                    file_count=len(uploaded),
                    total_size=sum(int(item.size or 0) for item in uploaded),
                    error_code=ErrorCode.NETWORK_TRANSIENT,
                )
            uploaded.append(result)

        manifest["status"] = "completed"
        if not self._write_operation_manifest(manifest_path, manifest):
            return UploadOutcome("unknown", "无法持久化移动云完成回执", error_code=ErrorCode.RESULT_UNKNOWN)
        total_size = sum(int(item.size or 0) for item in uploaded)
        return UploadOutcome("done", "移动云上传完成", file_count=len(uploaded), total_size=total_size)

    def _reconcile_upload_fence(
        self,
        *,
        manifest_path: str,
        manifest: dict[str, Any],
        target_path: str,
        target_parent_id: str,
        relative_paths: list[str] | None,
    ) -> UploadOutcome:
        unknown = UploadOutcome("unknown", MOBILE_UPLOAD_UNCONFIRMED_MESSAGE, error_code=ErrorCode.RESULT_UNKNOWN)
        records = manifest.get("files")
        # Only the original persisted scope or the caller's frozen selection
        # can prove completeness, never the set of files remaining on disk.
        selected = manifest.get("relative_paths", relative_paths)
        try:
            selected = validate_relative_paths(os.path.dirname(manifest_path), selected)
        except TransferError:
            return unknown
        if (
            (relative_paths is not None and set(selected) != set(relative_paths))
            or not isinstance(records, dict) or set(records) != set(selected)
        ):
            return unknown
        file_ids: set[str] = set()
        for relative, record in records.items():
            result = record.get("result") if isinstance(record, dict) else None
            if (
                not isinstance(record, dict) or record.get("success") is not True
                or type(record.get("size")) is not int or record["size"] < 0
                or not isinstance(result, dict) or result.get("success") is not True
                or result.get("status") not in ("done", "completed", "success")
                or type(result.get("size")) is not int or result["size"] != record["size"]
                or result.get("target_name") != posixpath.basename(relative)
                or not isinstance(result.get("file_id"), str) or not result["file_id"].strip()
                or not isinstance(result.get("parent_file_id"), str) or not result["parent_file_id"].strip()
                or result["file_id"] in file_ids
            ):
                return unknown
            file_ids.add(result["file_id"])

        directories: dict[str, list[dict[str, Any]]] = {}

        def lookup(parent_id: str, name: str) -> dict[str, Any]:
            if parent_id not in directories:
                entries = self.uploader.client.list_dir(parent_id)
                if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
                    raise ValueError("invalid directory listing")
                directories[parent_id] = entries
            matches = [
                item for item in directories[parent_id]
                if str(item.get("name") or item.get("fileName") or "") == name
            ]
            return matches[0] if len(matches) == 1 else {}

        def file_id(item: dict[str, Any]) -> str:
            return str(item.get("file_id") or item.get("fileId") or item.get("id") or "")

        def resolve(parent_id: str, path: str) -> str:
            for segment in path.strip("/").split("/"):
                if not segment:
                    continue
                if segment in {".", ".."} or "\\" in segment:
                    return ""
                item = lookup(parent_id, segment)
                parent_id = file_id(item)
                if not parent_id or not _item_is_dir(item):
                    return ""
            return parent_id

        try:
            root_id = target_parent_id or resolve("/", target_path)
            if not root_id:
                return unknown
            for relative, record in records.items():
                result = record["result"]
                parent_id = resolve(root_id, posixpath.dirname(relative))
                if not parent_id or result["parent_file_id"] != parent_id:
                    return unknown
                item = lookup(parent_id, posixpath.basename(relative))
                size = item.get("size") if item.get("size") is not None else item.get("fileSize")
                if (
                    not item or _item_is_dir(item) or file_id(item) != result["file_id"]
                    or isinstance(size, bool) or not isinstance(size, (int, str))
                    or str(size) != str(record["size"])
                ):
                    return unknown
        except Exception:  # noqa: BLE001 - failed observation never authorizes another upload
            return UploadOutcome("unknown", "移动云上传回执只读核验失败，保留现场等待复核", error_code=ErrorCode.RESULT_UNKNOWN)

        if self._read_operation_manifest(manifest_path) != manifest:
            return unknown
        completed = {**manifest, "status": "completed"}
        if not self._write_operation_manifest(manifest_path, completed):
            return UploadOutcome("unknown", "无法持久化移动云完成回执", error_code=ErrorCode.RESULT_UNKNOWN)
        return UploadOutcome(
            "done", "移动云上传已按逐文件回执只读对账完成，等待后续校验",
            file_count=len(records), total_size=sum(record["size"] for record in records.values()),
        )

    def merge_upload_manifests(
        self,
        *,
        local_dir: str,
        idempotency_key: str,
        file_operation_keys: list[str],
    ) -> UploadOutcome:
        return merge_completed_upload_manifests(
            local_dir=local_dir,
            idempotency_key=idempotency_key,
            file_operation_keys=file_operation_keys,
            provider_kind=self.provider_kind,
            manifest_path=self._operation_manifest_path,
            read_manifest=self._read_operation_manifest,
            write_manifest=self._write_operation_manifest,
        )

    def _call_upload_file(self, kwargs: dict[str, Any]) -> Any:
        method = getattr(self.uploader, "upload_file", None)
        if not callable(method):
            raise TransferError(ErrorCode.AUTH_CONFIG, "移动云上传器未提供 upload_file 方法")
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
        return write_operation_manifest(path, value)

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
            return TransferVerifyResult("unknown", "移动云中继缺少目标定位器", code=ErrorCode.INVALID_PLAN)
        target_path = str(target_locator.get("path") or "").strip()
        parent_id = str(target_locator.get("parent_file_id") or target_locator.get("parentFileId") or "").strip()
        if not target_path and not parent_id:
            return TransferVerifyResult("unknown", "移动云中继目标路径和父目录 ID 不能同时为空", code=ErrorCode.INVALID_PLAN)
        if not parent_id:
            parent_id = self._resolve_readonly(target_path)
        if not parent_id:
            return TransferVerifyResult("missing", f"移动云目标目录不存在：{target_path}", code=ErrorCode.VERIFY_MISMATCH)
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
                "移动云上传 manifest 缺失或未完成",
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
            return TransferVerifyResult("unknown", "移动云认证已过期，无法确认目标", code=ErrorCode.RESULT_UNKNOWN)
        except CmccApiError as exc:
            return TransferVerifyResult(
                "unknown" if exc.auth_expired else "unknown",
                f"移动云目录读取失败：{exc}",
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
            problems.append("缺失：" + "、".join(missing[:10]))
        if expected_file_count > 0 and len(expected_records) != expected_file_count:
            problems.append(
                f"fence 文件数 {len(expected_records)} != 期望 {expected_file_count}"
            )
        if expected_total_size > 0 and manifest_size != expected_total_size:
            problems.append(
                f"fence 总大小 {manifest_size} != 期望 {expected_total_size}"
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
            "移动云目标校验通过",
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
                "message": "移动云原生上传探测需要目标 locator",
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
                "message": "移动云原生上传探测目标为空",
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
                    "message": result.message or "移动云原生上传探测失败",
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
                "message": "移动云盘原生上传与 OpenList 整理链路检测通过",
            }
        except CmccAuthExpired:
            return {
                "ok": False,
                "upload_verified": False,
                "organizer_mutation_verified": False,
                "code": "AUTH_EXPIRED",
                "message": "移动云盘认证已过期",
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
    return CmccUploadResult(False, "failed", f"无法识别的移动云上传结果：{type(value).__name__}", target_name=relative_path, size=size)


def _cmcc_result_from_dict(value: Any, relative_path: str, size: int) -> CmccUploadResult:
    return _coerce_cmcc_result(value if isinstance(value, dict) else {}, relative_path, size)


def _item_is_dir(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("is_dir") is True or item.get("isDir") is True or item.get("dir") is True:
        return True
    type_text = str(item.get("file_type") or item.get("fileType") or item.get("type") or "").strip().lower()
    return type_text in {"dir", "folder", "catalog", "directory"}
