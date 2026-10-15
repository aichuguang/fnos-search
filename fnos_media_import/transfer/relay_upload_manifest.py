"""Exact relay file selection and durable aggregation of upload receipts."""

from __future__ import annotations

import json
import ntpath
import os
import tempfile
from collections.abc import Callable, Mapping
from typing import Any

from fnos_media_import.transfer import ErrorCode, TransferError, UploadOutcome


def _resolve_relative_file(root: str, relative: str) -> str:
    full = os.path.realpath(os.path.join(root, *relative.split("/")))
    try:
        inside = os.path.normcase(os.path.commonpath((root, full))) == os.path.normcase(root)
    except ValueError:
        inside = False
    if not inside:
        raise TransferError(ErrorCode.INVALID_PLAN, "中继上传文件不能通过符号链接越出缓存目录")
    return full


def validate_relative_paths(local_dir: str, relative_paths: list[str]) -> list[str]:
    """Validate the requested names without requiring released media to exist."""

    if not str(local_dir or "").strip() or not isinstance(relative_paths, list) or not relative_paths:
        raise TransferError(ErrorCode.INVALID_PLAN, "中继上传必须指定非空的相对文件路径列表")
    root = os.path.realpath(local_dir)
    selected: list[str] = []
    seen: set[str] = set()
    for relative in relative_paths:
        if (
            not isinstance(relative, str)
            or not relative.strip()
            or "\x00" in relative
            or "\\" in relative
            or ntpath.isabs(relative)
            or ntpath.splitdrive(relative)[0]
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or any(
                part.startswith(".fnos-relay-upload-") or part.endswith(".fnos-cmcc-upload.json")
                for part in relative.split("/")
            )
        ):
            raise TransferError(ErrorCode.INVALID_PLAN, "中继上传文件路径必须是规范的相对路径")
        identity = os.path.normcase(relative)
        if identity in seen:
            raise TransferError(ErrorCode.INVALID_PLAN, "中继上传文件路径不能重复")
        seen.add(identity)
        _resolve_relative_file(root, relative)
        selected.append(relative)
    return selected


def selected_local_files(local_dir: str, relative_paths: list[str]) -> list[tuple[str, str, int]]:
    """Resolve only the explicit files; never enumerate neighbouring cache files."""

    selected = validate_relative_paths(local_dir, relative_paths)
    root = os.path.realpath(local_dir)
    files: list[tuple[str, str, int]] = []
    for relative in selected:
        full = _resolve_relative_file(root, relative)
        try:
            if not os.path.isfile(full):
                raise OSError("selected source is not a regular file")
            size = os.path.getsize(full)
        except OSError as exc:
            raise TransferError(
                ErrorCode.SOURCE_LANDING_UNAVAILABLE,
                f"中继指定文件缺失或不可读取：{relative}",
            ) from exc
        files.append((full, relative, int(size)))
    return files


def validate_manifest_selection(manifest: Mapping[str, Any], relative_paths: list[str]) -> None:
    """A completed fence may replace local bytes, but never the selected scope."""

    selected = set(relative_paths)
    bound_paths = manifest.get("relative_paths")
    records = manifest.get("files")
    completed = str(manifest.get("status") or "").lower() == "completed"
    bound_mismatch = bound_paths is not None and (
        not isinstance(bound_paths, list)
        or any(not isinstance(path, str) for path in bound_paths)
        or len(set(bound_paths)) != len(bound_paths)
        or set(bound_paths) != selected
    )
    if (
        bound_mismatch
        or not isinstance(records, dict)
        or not set(records).issubset(selected)
        or (completed and (set(records) != selected or any(not _valid_record(item) for item in records.values())))
    ):
        raise TransferError(ErrorCode.INVALID_PLAN, "中继上传幂等回执与指定文件范围不一致")


def _valid_record(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("success") is True
        and isinstance(record.get("size"), int)
        and not isinstance(record.get("size"), bool)
        and record["size"] >= 0
    )


def write_operation_manifest(path: str, value: dict[str, Any]) -> bool:
    """Persist both contents and the rename before permitting media release."""

    directory = os.path.dirname(path) or "."
    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".fnos-relay-upload-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        if os.name == "posix":
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return True
    except (OSError, TypeError, ValueError):
        try:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass
        return False


def merge_completed_upload_manifests(
    *,
    local_dir: str,
    idempotency_key: str,
    file_operation_keys: list[str],
    provider_kind: str,
    manifest_path: Callable[[str, str], str],
    read_manifest: Callable[[str], dict[str, Any]],
    write_manifest: Callable[[str, dict[str, Any]], bool],
) -> UploadOutcome:
    """Aggregate completed per-file receipts after the caller verified each file."""

    operation_key = str(idempotency_key or "").strip()
    if (
        not operation_key
        or not str(local_dir or "").strip()
        or not os.path.isdir(local_dir)
        or not isinstance(file_operation_keys, list)
        or not file_operation_keys
        or any(not isinstance(key, str) or not key.strip() or key != key.strip() for key in file_operation_keys)
        or len(set(file_operation_keys)) != len(file_operation_keys)
        or operation_key in file_operation_keys
    ):
        return UploadOutcome("failed", "中继逐文件回执合并参数无效", error_code=ErrorCode.INVALID_PLAN)

    target_fingerprint = ""
    records: dict[str, Any] = {}
    for key in file_operation_keys:
        manifest = read_manifest(manifest_path(local_dir, key))
        fingerprint = manifest.get("target_fingerprint")
        entries = manifest.get("files")
        if (
            manifest.get("idempotency_key") != key
            or str(manifest.get("status") or "").lower() != "completed"
            or manifest.get("provider_kind", provider_kind) != provider_kind
            or not isinstance(fingerprint, str)
            or not fingerprint.strip()
            or not isinstance(entries, dict)
            or not entries
            or any(not _valid_record(item) for item in entries.values())
        ):
            return UploadOutcome("unknown", "中继逐文件上传回执缺失或未完成", error_code=ErrorCode.RESULT_UNKNOWN)
        if target_fingerprint and fingerprint != target_fingerprint:
            return UploadOutcome("failed", "中继逐文件上传回执目标不一致", error_code=ErrorCode.INVALID_PLAN)
        target_fingerprint = fingerprint
        try:
            validate_relative_paths(local_dir, list(entries))
            validate_manifest_selection(manifest, list(entries))
        except TransferError as exc:
            return UploadOutcome("failed", exc.message, error_code=exc.code)
        for relative, record in entries.items():
            if relative in records and records[relative] != record:
                return UploadOutcome("failed", "中继逐文件上传回执路径冲突", error_code=ErrorCode.INVALID_PLAN)
            records[relative] = record

    path = manifest_path(local_dir, operation_key)
    previous = read_manifest(path)
    previous_records = previous.get("files", {})
    if previous and (
        previous.get("idempotency_key") != operation_key
        or previous.get("target_fingerprint") != target_fingerprint
        or not isinstance(previous_records, dict)
        or any(relative not in records or records[relative] != record for relative, record in previous_records.items())
    ):
        return UploadOutcome("failed", "中继任务已有回执与逐文件回执冲突", error_code=ErrorCode.INVALID_PLAN)

    combined = {
        "version": 1,
        "provider_kind": provider_kind,
        "idempotency_key": operation_key,
        "target_fingerprint": target_fingerprint,
        "status": "completed",
        "files": records,
        "file_operation_keys": list(file_operation_keys),
    }
    if not write_manifest(path, combined):
        return UploadOutcome("unknown", "无法持久化中继合并上传回执", error_code=ErrorCode.RESULT_UNKNOWN)
    return UploadOutcome(
        "done",
        "中继逐文件上传回执已合并",
        file_count=len(records),
        total_size=sum(record["size"] for record in records.values()),
    )
