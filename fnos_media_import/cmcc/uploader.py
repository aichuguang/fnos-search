from __future__ import annotations

import hashlib
import json
import os
import posixpath
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from .auth import CmccAuthExpired
from .client import CmccApiError, CmccClient


@dataclass
class CmccUploadResult:
    success: bool
    status: str
    message: str = ""
    mode: str = ""
    file_id: str = ""
    upload_id: str = ""
    parent_file_id: str = ""
    target_name: str = ""
    size: int = 0
    sha256: str = ""
    manifest_path: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ProgressCallback = Callable[[str], None]


def _format_bytes(value: int | float) -> str:
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    whole = int(seconds)
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _json_preview(payload: Any, *, limit: int = 1800) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        text = str(payload)
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


class _ProgressMeter:
    def __init__(
        self,
        *,
        label: str,
        total: int,
        callback: ProgressCallback | None = None,
        interval: float | None = None,
    ) -> None:
        self.label = label
        self.total = max(int(total or 0), 0)
        self.callback = callback
        self.interval = interval if interval is not None else float(os.getenv("CMCC_UPLOAD_PROGRESS_INTERVAL", "5") or "5")
        self.started_at = time.monotonic()
        self.last_emit_at = 0.0
        self.last_percent = -1

    def emit(self, done: int, *, force: bool = False) -> None:
        if not self.callback:
            return
        done = max(0, min(int(done or 0), self.total or int(done or 0)))
        now = time.monotonic()
        percent = int(done * 100 / self.total) if self.total else 0
        if not force and self.last_emit_at:
            if now - self.last_emit_at < self.interval and percent == self.last_percent:
                return
        elapsed = max(now - self.started_at, 0.001)
        speed = done / elapsed
        eta = (self.total - done) / speed if self.total and speed > 0 and done < self.total else 0
        self.last_emit_at = now
        self.last_percent = percent
        self.callback(
            f"{self.label}: {_format_bytes(done)} / {_format_bytes(self.total)}, "
            f"{percent}%, {_format_bytes(speed)}/s, ETA {_format_duration(eta)}"
        )


class _ProgressFileReader:
    def __init__(self, path: Path, *, total: int, callback: ProgressCallback | None, label: str) -> None:
        self._handle = path.open("rb")
        self._done = 0
        self._meter = _ProgressMeter(label=label, total=total, callback=callback)
        self._meter.emit(0, force=True)

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if chunk:
            self._done += len(chunk)
            self._meter.emit(self._done)
        else:
            self._meter.emit(self._done, force=True)
        return chunk

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "_ProgressFileReader":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _ProgressSegmentReader:
    def __init__(
        self,
        path: Path,
        *,
        offset: int,
        size: int,
        meter: _ProgressMeter,
        done_ref: dict[str, int],
    ) -> None:
        self._handle = path.open("rb")
        self._handle.seek(int(offset))
        self._remaining = int(size)
        self._meter = meter
        self._done_ref = done_ref

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        read_size = self._remaining if size is None or size < 0 else min(int(size), self._remaining)
        chunk = self._handle.read(read_size)
        if chunk:
            self._remaining -= len(chunk)
            self._done_ref["done"] = int(self._done_ref.get("done", 0)) + len(chunk)
            self._meter.emit(self._done_ref["done"])
        return chunk

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "_ProgressSegmentReader":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def sha256_file(
    path: Path,
    chunk_size: int = 1024 * 1024 * 4,
    *,
    progress: ProgressCallback | None = None,
    progress_label: str = "CMCC API hash progress",
) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    meter = _ProgressMeter(label=progress_label, total=total, callback=progress)
    meter.emit(0, force=True)
    done = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
            done += len(chunk)
            meter.emit(done)
    meter.emit(total, force=True)
    return digest.hexdigest()


def _safe_read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_name, path)


def _cached_sha256_from_manifest(manifest: dict[str, Any], *, file_path: Path, size: int, mtime_ns: int) -> str:
    sha = str(manifest.get("sha256") or "").strip().lower()
    if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
        return ""
    if str(manifest.get("local_path") or "") != str(file_path):
        return ""
    try:
        if int(manifest.get("size")) != int(size):
            return ""
    except Exception:
        return ""
    cached_mtime = manifest.get("mtime_ns")
    if cached_mtime not in (None, ""):
        try:
            if int(cached_mtime) != int(mtime_ns):
                return ""
        except Exception:
            return ""
    return sha


def _split_relative_dir(relative_dir: str) -> list[str]:
    text = str(relative_dir or "").replace("\\", "/").strip("/")
    if not text or text == ".":
        return []
    norm = posixpath.normpath(text)
    if norm in {"", "."} or norm.startswith("../") or norm == "..":
        return []
    return [part for part in norm.split("/") if part and part != "."]


def _openlist_part_size(size: int) -> int:
    configured = int(os.getenv("CMCC_UPLOAD_PART_SIZE", "0") or "0")
    if configured > 0:
        return configured
    gib = 1024 * 1024 * 1024
    mib = 1024 * 1024
    if int(size or 0) // gib > 30:
        return 512 * mib
    return 100 * mib


def _build_part_infos(size: int) -> list[dict[str, Any]]:
    total = max(int(size or 0), 0)
    part_size = _openlist_part_size(total)
    if total == 0:
        return [{"partNumber": 1, "partSize": 0, "parallelHashCtx": {"partOffset": 0}}]
    result: list[dict[str, Any]] = []
    offset = 0
    part_number = 1
    while offset < total:
        current_size = min(part_size, total - offset)
        result.append(
            {
                "partNumber": part_number,
                "partSize": current_size,
                "parallelHashCtx": {"partOffset": offset},
            }
        )
        offset += current_size
        part_number += 1
    return result


def _extract_part_infos(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("partInfos") or payload.get("part_infos") or payload.get("partInfoList") or []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


class CmccUploader:
    def __init__(self, client: CmccClient | None = None, *, put_timeout: int | None = None) -> None:
        self.client = client or CmccClient()
        self.put_timeout = put_timeout or int(os.getenv("CMCC_UPLOAD_PUT_TIMEOUT", "7200") or "7200")

    def upload_file(
        self,
        *,
        local_file: str | os.PathLike[str],
        target_parent_file_id: str = "",
        target_parent_path: str = "",
        target_name: str = "",
        target_relative_dir: str = "",
        category: str = "",
        mode: str = "rapid_first",
        rename_mode: str = "auto_rename",
        progress: ProgressCallback | None = None,
    ) -> CmccUploadResult:
        file_path = Path(local_file)
        if not file_path.exists() or not file_path.is_file():
            return CmccUploadResult(False, "failed", f"local file does not exist: {file_path}")

        file_stat = file_path.stat()
        size = file_stat.st_size
        mtime_ns = file_stat.st_mtime_ns
        name = target_name or file_path.name
        manifest_path = Path(str(file_path) + ".fnos-cmcc-upload.json")
        manifest = _safe_read_json(manifest_path)

        sha = _cached_sha256_from_manifest(manifest, file_path=file_path, size=size, mtime_ns=mtime_ns)
        if not sha:
            if progress:
                progress(f"CMCC API local hash start: {name}, size={_format_bytes(size)}")
            sha = sha256_file(file_path, progress=progress, progress_label=f"CMCC API local hash progress: {name}")
            if progress:
                progress(f"CMCC API local hash done: {name}, sha256={sha[:16]}...")
            _atomic_write_json(
                manifest_path,
                {
                    "local_path": str(file_path),
                    "target_name": name,
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "sha256": sha,
                    "status": "hashed",
                    "hash_cached_at": int(time.time()),
                },
            )
        elif progress:
            progress(f"CMCC API local hash cache hit: {name}, sha256={sha[:16]}...")

        base_manifest = {
            "local_path": str(file_path),
            "target_parent_file_id": target_parent_file_id,
            "target_parent_path": target_parent_path,
            "target_relative_dir": target_relative_dir,
            "target_name": name,
            "category": category,
            "size": size,
            "mtime_ns": mtime_ns,
            "sha256": sha,
        }

        if self._manifest_verified(manifest, base_manifest):
            return CmccUploadResult(
                True,
                "done",
                "already verified by local manifest",
                mode="manifest",
                file_id=str(manifest.get("file_id") or ""),
                upload_id=str(manifest.get("upload_id") or ""),
                parent_file_id=str(manifest.get("parent_file_id") or target_parent_file_id),
                target_name=name,
                size=size,
                sha256=sha,
                manifest_path=str(manifest_path),
                raw={"manifest": manifest},
            )

        parent_file_id = str(target_parent_file_id or "").strip()
        parent_path = str(target_parent_path or "").strip()
        configured_parent_path = parent_path
        if parent_file_id:
            try:
                parent_file_id = self._ensure_relative_dir(parent_file_id, target_relative_dir)
            except CmccApiError as exc:
                if not configured_parent_path or not self._is_missing_resource_error(exc):
                    raise
                fallback_path = self._join_cloud_path(configured_parent_path, target_relative_dir)
                if progress:
                    progress(
                        "CMCC WebAPI parent id unavailable, fallback to path: "
                        f"{configured_parent_path} -> {fallback_path} ({exc})"
                    )
                parent_file_id = self.client.ensure_path(fallback_path)
        else:
            parent_path = self._join_cloud_path(parent_path, target_relative_dir)
            if parent_path and parent_path != "/":
                if progress:
                    progress(f"CMCC WebAPI resolve path: {parent_path}")
                parent_file_id = self.client.ensure_path(parent_path)

        if not parent_file_id:
            return CmccUploadResult(False, "failed", "target parent file id/path is not configured")

        exists = self._find_existing(parent_file_id, name)
        if self._existing_matches(exists, size):
            payload = {
                **base_manifest,
                "status": "verified",
                "mode": "existing",
                "file_id": exists.get("file_id") or "",
                "parent_file_id": parent_file_id,
            }
            _atomic_write_json(manifest_path, payload)
            return CmccUploadResult(
                True,
                "done",
                "same file already exists in CMCC cloud",
                mode="existing",
                file_id=str(exists.get("file_id") or ""),
                parent_file_id=parent_file_id,
                target_name=name,
                size=size,
                sha256=sha,
                manifest_path=str(manifest_path),
                raw={"exists": exists},
            )
        if exists.get("exist"):
            return CmccUploadResult(
                False,
                "failed",
                f"target file exists with different or unknown size: {name}",
                mode="existing_conflict",
                file_id=str(exists.get("file_id") or ""),
                parent_file_id=parent_file_id,
                target_name=name,
                size=size,
                sha256=sha,
                manifest_path=str(manifest_path),
                raw={"exists": exists},
            )

        if progress:
            progress(f"CMCC API create request: {name}, size={_format_bytes(size)}")
        part_infos = _build_part_infos(size)
        first_part_infos = part_infos[:100]
        try:
            created = self.client.create_file(
                name=name,
                size=size,
                parent_file_id=parent_file_id,
                parent_path="",
                content_hash=sha,
                rename_mode=rename_mode,
                part_infos=first_part_infos,
            )
        except CmccApiError as exc:
            if not parent_path or not self._is_missing_resource_error(exc):
                raise
            fallback_path = (
                self._join_cloud_path(configured_parent_path, target_relative_dir)
                if configured_parent_path
                else parent_path
            )
            if progress:
                progress(
                    "CMCC WebAPI create parent id unavailable, retry by path: "
                    f"{configured_parent_path or parent_path} -> {fallback_path} ({exc})"
                )
            parent_file_id = self.client.ensure_path(fallback_path)
            created = self.client.create_file(
                name=name,
                size=size,
                parent_file_id=parent_file_id,
                parent_path="",
                content_hash=sha,
                rename_mode=rename_mode,
                part_infos=first_part_infos,
            )
        upload_id = str(created.get("uploadId") or created.get("upload_id") or "")
        file_id = str(created.get("fileId") or created.get("file_id") or "")
        file_name = str(created.get("fileName") or created.get("name") or name)
        parent_file_id = str(created.get("parentFileId") or parent_file_id or "")
        upload_part_infos = _extract_part_infos(created)
        exist = bool(created.get("exist") or created.get("exists") or created.get("isExist"))
        rapid = bool(
            created.get("rapidUpload")
            or created.get("rapid_upload")
            or created.get("rapid")
            or created.get("uploadComplete")
            or created.get("complete")
        )
        if progress:
            keys = ",".join(sorted(str(key) for key in created.keys())) if isinstance(created, dict) else ""
            progress(
                "CMCC WebAPI create response: "
                f"exist={exist}, rapid={rapid}, upload_parts={len(upload_part_infos)}, "
                f"file_id={file_id or '-'}, upload_id={upload_id or '-'}, keys={keys or '-'}"
            )

        _atomic_write_json(
            manifest_path,
            {
                **base_manifest,
                "status": "created",
                "mode": "rapid" if rapid else "api_put",
                "upload_id": upload_id,
                "file_id": file_id,
                "parent_file_id": parent_file_id,
                "raw_create": created,
            },
        )

        if exist:
            _atomic_write_json(
                manifest_path,
                {
                    **base_manifest,
                    "status": "verified",
                    "mode": "existing",
                    "upload_id": upload_id,
                    "file_id": file_id,
                    "parent_file_id": parent_file_id,
                    "raw_create": created,
                },
            )
            return CmccUploadResult(
                True,
                "done",
                "same file already exists in CMCC cloud",
                mode="existing",
                file_id=file_id,
                upload_id=upload_id,
                parent_file_id=parent_file_id,
                target_name=name,
                size=size,
                sha256=sha,
                manifest_path=str(manifest_path),
                raw={"create": created},
            )

        if rapid and not upload_part_infos:
            _atomic_write_json(
                manifest_path,
                {
                    **base_manifest,
                    "status": "verified",
                    "mode": "rapid",
                    "upload_id": upload_id,
                    "file_id": file_id,
                    "parent_file_id": parent_file_id,
                    "raw_create": created,
                },
            )
            return CmccUploadResult(
                True,
                "done",
                "CMCC hash rapid upload completed",
                mode="rapid",
                file_id=file_id,
                upload_id=upload_id,
                parent_file_id=parent_file_id,
                target_name=name,
                size=size,
                sha256=sha,
                manifest_path=str(manifest_path),
                raw={"create": created},
            )

        if mode == "rapid_only":
            _atomic_write_json(
                manifest_path,
                {
                    **base_manifest,
                    "status": "rapid_miss",
                    "mode": "rapid_only",
                    "upload_id": upload_id,
                    "file_id": file_id,
                    "parent_file_id": parent_file_id,
                    "raw_create": created,
                },
            )
            return CmccUploadResult(
                False,
                "rapid_miss",
                "rapid upload not hit; rapid_only refused real upload",
                mode="rapid_only",
                file_id=file_id,
                upload_id=upload_id,
                parent_file_id=parent_file_id,
                target_name=name,
                size=size,
                sha256=sha,
                manifest_path=str(manifest_path),
                raw={"create": created},
            )

        if not upload_part_infos:
            if progress:
                progress(f"CMCC WebAPI create raw response: {_json_preview(created)}")
            return CmccUploadResult(
                False,
                "failed",
                "CMCC WebAPI create did not return partInfos/uploadUrl and rapidUpload is false",
                mode="api_put",
                file_id=file_id,
                upload_id=upload_id,
                parent_file_id=parent_file_id,
                target_name=name,
                size=size,
                sha256=sha,
                manifest_path=str(manifest_path),
                raw={"create": created},
            )

        _atomic_write_json(
            manifest_path,
            {
                **base_manifest,
                "status": "uploading",
                "mode": "api_put",
                "upload_id": upload_id,
                "file_id": file_id,
                "parent_file_id": parent_file_id,
                "raw_create": created,
            },
        )
        if progress:
            progress(
                f"CMCC API PUT start: {name}, size={_format_bytes(size)}, "
                f"parts={len(part_infos)}, part_size={_format_bytes(_openlist_part_size(size))}"
            )
        self._upload_parts(
            file_path=file_path,
            size=size,
            all_part_infos=part_infos,
            initial_upload_part_infos=upload_part_infos,
            file_id=file_id,
            upload_id=upload_id,
            progress=progress,
            progress_label=f"CMCC API PUT progress: {name}",
        )
        if progress:
            progress(f"CMCC API complete request: {name}")
        completed = self.client.complete_file(upload_id=upload_id, file_id=file_id, content_hash=sha, parent_file_id=parent_file_id)
        verified = self._find_existing(parent_file_id, name) if parent_file_id else {}
        if parent_file_id and not self._existing_matches(verified, size):
            detail = (
                "cloud file exists but size does not match local file"
                if verified.get("exist")
                else "cloud file is not visible after complete request"
            )
            return CmccUploadResult(
                False,
                "failed",
                f"upload completed but verification failed: {detail}",
                mode="api_put",
                file_id=file_id,
                upload_id=upload_id,
                parent_file_id=parent_file_id,
                target_name=name,
                size=size,
                sha256=sha,
                manifest_path=str(manifest_path),
                raw={"complete": completed, "exists": verified},
            )

        _atomic_write_json(
            manifest_path,
            {
                **base_manifest,
                "status": "verified",
                "mode": "api_put",
                "upload_id": upload_id,
                "file_id": file_id,
                "parent_file_id": parent_file_id,
                "raw_create": created,
                "raw_complete": completed,
                "raw_exists": verified,
                "server_file_name": file_name,
            },
        )
        return CmccUploadResult(
            True,
            "done",
            "CMCC API upload completed",
            mode="api_put",
            file_id=file_id,
            upload_id=upload_id,
            parent_file_id=parent_file_id,
            target_name=name,
            size=size,
            sha256=sha,
            manifest_path=str(manifest_path),
            raw={"create": created, "complete": completed, "exists": verified},
        )

    def _finish_rapid(
        self,
        *,
        manifest_path: Path,
        base_manifest: dict[str, Any],
        created: dict[str, Any],
        file_id: str,
        upload_id: str,
        parent_file_id: str,
        name: str,
        size: int,
        sha: str,
    ) -> CmccUploadResult:
        completed: dict[str, Any] = {}
        exists: dict[str, Any] = {}
        if parent_file_id:
            exists = self._find_existing(parent_file_id, name)
        if not self._existing_matches(exists, size) and upload_id and file_id:
            try:
                completed = self.client.complete_file(
                    upload_id=upload_id,
                    file_id=file_id,
                    content_hash=sha,
                    parent_file_id=parent_file_id,
                )
            except CmccApiError:
                if not exists.get("exist"):
                    raise
            if parent_file_id:
                exists = self._find_existing(parent_file_id, name)

        _atomic_write_json(
            manifest_path,
            {
                **base_manifest,
                "status": "verified",
                "mode": "rapid",
                "upload_id": upload_id,
                "file_id": file_id,
                "parent_file_id": parent_file_id,
                "raw_create": created,
                "raw_complete": completed,
                "raw_exists": exists,
            },
        )
        return CmccUploadResult(
            True,
            "done",
            "CMCC hash rapid upload completed",
            mode="rapid",
            file_id=file_id,
            upload_id=upload_id,
            parent_file_id=parent_file_id,
            target_name=name,
            size=size,
            sha256=sha,
            manifest_path=str(manifest_path),
            raw={"create": created, "complete": completed, "exists": exists},
        )

    def _ensure_relative_dir(self, parent_file_id: str, relative_dir: str) -> str:
        current = parent_file_id or "/"
        for segment in _split_relative_dir(relative_dir):
            exists = self._find_existing(current, segment)
            if exists.get("exist") and exists.get("file_id"):
                file_type = str(exists.get("file_type") or "").lower()
                if file_type and "folder" not in file_type and "dir" not in file_type:
                    raise CmccApiError(f"target path segment exists but is not a folder: {segment}")
                current = str(exists["file_id"])
                continue
            created = self.client.create_folder(name=segment, parent_file_id=current)
            current = str(created.get("fileId") or created.get("appFileId") or created.get("id") or "")
            if not current:
                raise CmccApiError(f"create folder succeeded but no fileId returned: {segment}")
        return current

    @staticmethod
    def _is_missing_resource_error(exc: CmccApiError) -> bool:
        text = f"{exc.code or ''} {exc}".lower()
        return any(token in text for token in ("00010010", "资源不存在", "not exist", "not found"))

    def _find_existing(self, parent_file_id: str, name: str) -> dict[str, Any]:
        if not parent_file_id:
            return {}
        try:
            return self.client.check_exists(parent_file_id=parent_file_id, file_name=name)
        except CmccAuthExpired:
            raise
        except CmccApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            # An existence lookup is part of the destructive-source safety
            # decision.  Treat an unknown query failure as a hard failure rather
            # than returning an empty result that can later be marked verified.
            raise CmccApiError(
                f"CMCC 云端文件查询失败，未能确认 {name or '<unnamed>'}: {exc}"
            ) from exc

    @staticmethod
    def _existing_matches(exists: dict[str, Any], size: int) -> bool:
        if not exists.get("exist"):
            return False
        remote_size = exists.get("size")
        return remote_size is not None and int(remote_size) == int(size)

    @staticmethod
    def _manifest_verified(manifest: dict[str, Any], expected: dict[str, Any]) -> bool:
        if manifest.get("status") != "verified":
            return False
        for key in ("local_path", "target_name", "size", "sha256"):
            if manifest.get(key) != expected.get(key):
                return False
        for key in ("target_parent_file_id", "target_parent_path", "target_relative_dir"):
            if str(manifest.get(key) or "") != str(expected.get(key) or ""):
                return False
        return True

    @staticmethod
    def _extract_upload_url(created: dict[str, Any]) -> str:
        direct = (
            created.get("uploadUrl")
            or created.get("uploadURL")
            or created.get("upload_url")
            or created.get("putUrl")
            or created.get("putURL")
            or created.get("url")
        )
        if direct:
            return str(direct)
        for key in ("uploadInfo", "upload_info", "fileUploadInfo", "uploadFileInfo"):
            nested = created.get(key)
            if isinstance(nested, dict):
                nested_url = CmccUploader._extract_upload_url(nested)
                if nested_url:
                    return nested_url
        part_infos = (
            created.get("partInfos")
            or created.get("part_infos")
            or created.get("partInfoList")
            or created.get("parts")
            or []
        )
        if isinstance(part_infos, list) and part_infos:
            first = part_infos[0] if isinstance(part_infos[0], dict) else {}
            return str(
                first.get("uploadUrl")
                or first.get("uploadURL")
                or first.get("upload_url")
                or first.get("putUrl")
                or first.get("putURL")
                or first.get("url")
                or ""
            )
        return ""

    def _upload_parts(
        self,
        *,
        file_path: Path,
        size: int,
        all_part_infos: list[dict[str, Any]],
        initial_upload_part_infos: list[dict[str, Any]],
        file_id: str,
        upload_id: str,
        progress: ProgressCallback | None = None,
        progress_label: str = "CMCC API PUT progress",
    ) -> None:
        if not file_id or not upload_id:
            raise CmccApiError("CMCC WebAPI create response misses fileId/uploadId")
        meter = _ProgressMeter(label=progress_label, total=size, callback=progress)
        done_ref = {"done": 0}
        meter.emit(0, force=True)
        self._upload_part_batch(
            file_path=file_path,
            all_part_infos=all_part_infos,
            upload_part_infos=initial_upload_part_infos,
            meter=meter,
            done_ref=done_ref,
        )
        for start in range(100, len(all_part_infos), 100):
            batch_part_infos = all_part_infos[start : start + 100]
            more = self.client.get_upload_urls(file_id=file_id, upload_id=upload_id, part_infos=batch_part_infos)
            more_upload_part_infos = _extract_part_infos(more)
            if not more_upload_part_infos:
                raise CmccApiError(f"CMCC WebAPI getUploadUrl returned no partInfos for batch offset {start}")
            self._upload_part_batch(
                file_path=file_path,
                all_part_infos=all_part_infos,
                upload_part_infos=more_upload_part_infos,
                meter=meter,
                done_ref=done_ref,
            )
        meter.emit(size, force=True)

    def _upload_part_batch(
        self,
        *,
        file_path: Path,
        all_part_infos: list[dict[str, Any]],
        upload_part_infos: list[dict[str, Any]],
        meter: _ProgressMeter,
        done_ref: dict[str, int],
    ) -> None:
        sorted_parts = sorted(upload_part_infos, key=lambda item: int(item.get("partNumber") or item.get("part_number") or 0))
        for upload_part_info in sorted_parts:
            part_number = int(upload_part_info.get("partNumber") or upload_part_info.get("part_number") or 0)
            if part_number <= 0 or part_number > len(all_part_infos):
                raise CmccApiError(f"CMCC WebAPI returned invalid partNumber: {part_number}")
            upload_url = str(
                upload_part_info.get("uploadUrl")
                or upload_part_info.get("uploadURL")
                or upload_part_info.get("upload_url")
                or ""
            )
            if not upload_url:
                raise CmccApiError(f"CMCC WebAPI returned empty uploadUrl for part {part_number}")
            part_info = all_part_infos[part_number - 1]
            part_size = int(part_info.get("partSize") or part_info.get("part_size") or 0)
            hash_ctx = part_info.get("parallelHashCtx") if isinstance(part_info.get("parallelHashCtx"), dict) else {}
            part_offset = int(hash_ctx.get("partOffset") or part_info.get("partOffset") or 0)
            self._put_part(upload_url, file_path, offset=part_offset, size=part_size, meter=meter, done_ref=done_ref)

    def _put_part(
        self,
        upload_url: str,
        file_path: Path,
        *,
        offset: int,
        size: int,
        meter: _ProgressMeter,
        done_ref: dict[str, int],
    ) -> None:
        headers = {
            "Content-Length": str(size),
            "Content-Type": "application/octet-stream",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
        }
        with _ProgressSegmentReader(file_path, offset=offset, size=size, meter=meter, done_ref=done_ref) as handle:
            response = requests.put(upload_url, data=handle, headers=headers, timeout=self.put_timeout)
        if response.status_code != 200:
            text = (response.text or "").strip()
            raise CmccApiError(f"CMCC upload part PUT failed: HTTP {response.status_code} {text[:300]}")

    def _put_file(
        self,
        upload_url: str,
        file_path: Path,
        size: int,
        *,
        progress: ProgressCallback | None = None,
        progress_label: str = "CMCC API PUT progress",
    ) -> None:
        headers = {
            "Content-Length": str(size),
            "Content-Type": "application/octet-stream",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
        }
        with _ProgressFileReader(file_path, total=size, callback=progress, label=progress_label) as handle:
            response = requests.put(upload_url, data=handle, headers=headers, timeout=self.put_timeout)
        if response.status_code < 200 or response.status_code >= 300:
            text = (response.text or "").strip()
            raise CmccApiError(f"CMCC uploadUrl PUT failed: HTTP {response.status_code} {text[:300]}")

    @staticmethod
    def _join_cloud_path(parent_path: str, relative_dir: str) -> str:
        base = "/" + str(parent_path or "").strip().strip("/")
        rel = str(relative_dir or "").strip().strip("/")
        if not rel or rel == ".":
            return base
        return (base.rstrip("/") + "/" + rel).replace("//", "/")
