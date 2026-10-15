"""TransferVerifier —— 目标校验器。

校验目标「存在 + 文件数 + 总大小」（+ 可选 checksum），返回 ok / missing / unknown。
- ok：目标存在且文件数/总大小符合预期。
- missing：目标不存在或文件数/大小不匹配（可重试，目标可能仍在稳定中）。
- unknown：目标状态无法确认（进入人工复核，绝不删除来源）。
"""

from __future__ import annotations

from dataclasses import dataclass
import posixpath
from typing import Any

from fnos_media_import.domain.models import (
    DestinationDescriptor,
    LocalLocator,
    NativeLocator,
    OpenListLocator,
    RcloneLocator,
    SourceDescriptor,
)
from fnos_media_import.transfer import (
    ErrorCode,
    OperationReceipt,
    TransferVerifyResult,
)

__all__ = [
    "ListedFiles",
    "VerifyRequest",
    "TransferVerifier",
]


@dataclass(frozen=True)
class ListedFiles:
    exists: bool
    file_count: int = 0
    total_size: int = 0
    checksum_ok: bool | None = None
    message: str = ""
    unknown: bool = False


@dataclass(frozen=True)
class VerifyRequest:
    source: SourceDescriptor | None = None
    destination: DestinationDescriptor | None = None
    locator: NativeLocator | RcloneLocator | LocalLocator | OpenListLocator | None = None
    expected_file_count: int = 0
    expected_total_size: int = 0
    checksum: str = ""
    operation: OperationReceipt | None = None


def _normalize_listed(value: Any) -> ListedFiles:
    if isinstance(value, ListedFiles):
        return value
    if isinstance(value, dict):
        unknown = bool(value.get("unknown"))
        return ListedFiles(
            exists=bool(value.get("exists") or value.get("ok")),
            file_count=int(value.get("file_count") or 0),
            total_size=int(value.get("total_size") or 0),
            checksum_ok=value.get("checksum_ok"),
            message=str(value.get("message") or ""),
            unknown=unknown,
        )
    if isinstance(value, bool):
        return ListedFiles(exists=value)
    return ListedFiles(exists=False, unknown=True, message="无法识别的目标列表结果")


class TransferVerifier:
    """通用目标校验器；target_lister 由应用注入（OpenList 目录读取 / 本地目录扫描 / 移动云 API）。"""

    def __init__(self, target_lister: Any = None) -> None:
        self.target_lister = target_lister

    def verify_manifest(
        self,
        *,
        descriptor: dict[str, Any],
        files: list[dict[str, Any]],
    ) -> TransferVerifyResult:
        """Verify this task's retained files, not totals in a shared library.

        Each parent is listed once. Unrelated episodes, artwork and files from
        other jobs cannot satisfy a missing mapping or affect the result.
        """
        if self.target_lister is None or not files:
            return TransferVerifyResult("unknown", "缺少任务级最终文件清单或读取器", code=ErrorCode.RESULT_UNKNOWN)
        grouped: dict[str, list[tuple[str, int | None]]] = {}
        seen: set[str] = set()
        try:
            for item in files:
                path = str(item["path"] or "").replace("\\", "/")
                if not path.startswith("/") or ".." in path.split("/") or any(ord(ch) < 32 for ch in path):
                    raise ValueError("invalid target path")
                path = posixpath.normpath(path)
                if path == "/" or path in seen:
                    raise ValueError("empty or duplicate target")
                seen.add(path)
                size = None if item.get("size") is None else int(item["size"])
                if size is not None and size < 0:
                    raise ValueError("invalid expected size")
                grouped.setdefault(posixpath.dirname(path), []).append((posixpath.basename(path), size))
        except (KeyError, TypeError, ValueError):
            return TransferVerifyResult("unknown", "任务级最终文件清单无效", code=ErrorCode.RESULT_UNKNOWN)
        verified_count = verified_size = 0
        for parent, expected in grouped.items():
            try:
                listing = self.target_lister({**descriptor, "path": parent, "include_file_manifest": True})
                if not isinstance(listing, dict) or listing.get("unknown"):
                    raise ValueError("target listing unavailable")
                if not listing.get("exists"):
                    return TransferVerifyResult("missing", f"最终目标目录不存在：{parent}", code=ErrorCode.VERIFY_MISMATCH)
                entries = listing.get("files")
                if not isinstance(entries, list):
                    raise ValueError("target listing has no file manifest")
                for name, expected_size in expected:
                    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("name") == name]
                    if not matches or (len(matches) == 1 and matches[0].get("is_dir")):
                        return TransferVerifyResult("missing", f"最终目标文件不存在：{parent}/{name}", code=ErrorCode.VERIFY_MISMATCH)
                    if len(matches) != 1:
                        raise ValueError("ambiguous target file")
                    size = matches[0].get("size")
                    size = None if size is None else int(size)
                    if (size is not None and size < 0) or (size is None and expected_size is not None):
                        raise ValueError("target file size unavailable")
                    if expected_size is not None and size != expected_size:
                        return TransferVerifyResult(
                            "missing", f"最终目标文件大小不符：{parent}/{name}（{size} != {expected_size}）",
                            code=ErrorCode.VERIFY_MISMATCH,
                        )
                    verified_count += 1
                    verified_size += size or 0
            except Exception as exc:  # noqa: BLE001 - unknown listings must never authorize cleanup
                return TransferVerifyResult("unknown", f"最终文件清单查询失败：{exc}", code=ErrorCode.RESULT_UNKNOWN)
        return TransferVerifyResult(
            "ok", "任务级最终文件清单校验通过", file_count=verified_count, total_size=verified_size,
            expected_file_count=len(files), expected_total_size=sum(int(item.get("size") or 0) for item in files),
        )

    def verify(
        self,
        plan: Any = None,
        *,
        descriptor: dict[str, str] | None = None,
        expected_file_count: int | None = None,
        expected_total_size: int | None = None,
        checksum: str | None = None,
        allow_empty: bool = False,
        require_non_empty: bool = False,
    ) -> TransferVerifyResult:
        if descriptor is None and plan is not None:
            descriptor = getattr(plan, "destination_staging", None) or getattr(plan, "destination_final", None)
        if plan is not None and not allow_empty:
            verification = getattr(plan, "verification", None)
            if isinstance(verification, dict):
                allow_empty = bool(
                    verification.get("allow_empty")
                    or verification.get("empty_folder_contract")
                )
        if descriptor is None:
            return TransferVerifyResult("unknown", "缺少目标路径，无法校验", code=ErrorCode.RESULT_UNKNOWN)
        if self.target_lister is None:
            return TransferVerifyResult("unknown", "未配置目标读取器，无法确认目标", code=ErrorCode.RESULT_UNKNOWN)
        try:
            listing = _normalize_listed(self.target_lister(descriptor))
        except Exception as exc:  # noqa: BLE001
            return TransferVerifyResult("unknown", f"目标列表查询失败：{exc}", code=ErrorCode.RESULT_UNKNOWN)
        if listing.unknown:
            return TransferVerifyResult("unknown", listing.message or "目标状态未知", code=ErrorCode.RESULT_UNKNOWN)
        if not listing.exists:
            return TransferVerifyResult(
                "missing",
                listing.message or "目标目录不存在",
                code=ErrorCode.VERIFY_MISMATCH,
            )
        expected_count = None if expected_file_count is None else int(expected_file_count)
        expected_size = None if expected_total_size is None else int(expected_total_size)
        if (
            not allow_empty
            and expected_count is not None
            and expected_size is not None
            and expected_count <= 0
            and expected_size <= 0
        ):
            if not require_non_empty:
                return TransferVerifyResult(
                    "unknown",
                    "计划缺少冻结的文件数或总大小",
                    file_count=listing.file_count,
                    total_size=listing.total_size,
                    code=ErrorCode.RESULT_UNKNOWN,
                )
            # 数量未知（如访客整分享提交）的任务：冻结计划没有可比对的
            # 文件数/总大小，退化为「目录存在且至少落了一个文件」的校验。
            # 空目录仍判失败，不允许假装成功。
            if listing.file_count <= 0 and listing.total_size <= 0:
                return TransferVerifyResult(
                    "missing",
                    "数量未知的任务要求目标目录至少存在一个文件",
                    file_count=listing.file_count,
                    total_size=listing.total_size,
                    code=ErrorCode.VERIFY_MISMATCH,
                )
        problems: list[str] = []
        if expected_count is not None and (expected_count > 0 or allow_empty) and listing.file_count != expected_count:
            problems.append(f"文件数 {listing.file_count} != {expected_file_count}")
        if expected_size is not None and (expected_size > 0 or allow_empty) and listing.total_size != expected_size:
            problems.append(f"总大小 {listing.total_size} != {expected_total_size}")
        if checksum and listing.checksum_ok is False:
            problems.append("校验和不一致")
        if problems:
            return TransferVerifyResult(
                "missing",
                "; ".join(problems),
                file_count=listing.file_count,
                total_size=listing.total_size,
                expected_file_count=int(expected_file_count or 0),
                expected_total_size=int(expected_total_size or 0),
                code=ErrorCode.VERIFY_MISMATCH,
            )
        return TransferVerifyResult(
            "ok",
            listing.message or "目标校验通过",
            file_count=listing.file_count,
            total_size=listing.total_size,
            expected_file_count=int(expected_file_count or 0),
            expected_total_size=int(expected_total_size or 0),
        )
