"""TransferCleanupService —— 来源落地与本地缓存清理（§13.3 删除保留规则）。

安全规则：
- 只有在「目标校验成功 AND 应用 ACK」之后才删除任务级 landing 与本地缓存。
- 递归删除只允许发生在叶子为 ``job-<id>`` 的任务级目录内。
- 永远不对 mount 根、分类 final 根或非 ``job-<id>`` 目录执行递归删除。
- 清理失败不回滚已完成入库，记录告警并可重试。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fnos_media_import.transfer import TransferPlan

__all__ = [
    "CleanupItem",
    "CleanupResult",
    "FilesystemOps",
    "LocalFilesystemOps",
    "OpenListFilesystemOps",
    "CompositeFilesystemOps",
    "TransferCleanupService",
]


@dataclass(frozen=True)
class CleanupItem:
    descriptor: dict[str, str]
    ok: bool
    skipped: bool = False
    refused: bool = False
    retryable: bool = False
    message: str = ""


@dataclass(frozen=True)
class CleanupResult:
    ok: bool
    items: list[CleanupItem] = field(default_factory=list)
    skipped: bool = False
    warning: bool = False
    retryable: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "warning": self.warning,
            "retryable": self.retryable,
            "reason": self.reason,
            "items": [
                {
                    "descriptor": item.descriptor,
                    "ok": item.ok,
                    "skipped": item.skipped,
                    "refused": item.refused,
                    "retryable": item.retryable,
                    "message": item.message,
                }
                for item in self.items
            ],
        }


class FilesystemOps:
    """文件系统删除适配器协议。

    ``delete_tree(descriptor)`` 返回 ``{"ok": bool, "skipped": bool, "refused": bool, "message": str}``。
    ``resolve_real(descriptor)`` 返回本地绝对路径（仅 local 描述符需要）。
    """

    def delete_tree(self, descriptor: dict[str, str]) -> dict[str, Any]:
        raise NotImplementedError

    def resolve_real(self, descriptor: dict[str, str]) -> str:
        return str(descriptor.get("path") or "")


class LocalFilesystemOps(FilesystemOps):
    """本地目录删除：realpath containment + 永不删除配置根自身。"""

    def __init__(self, allowed_roots: tuple[str, ...] = ()) -> None:
        self.allowed_roots = [str(root) for root in allowed_roots]

    def resolve_real(self, descriptor: dict[str, str]) -> str:
        return os.path.realpath(str(descriptor.get("path") or ""))

    def delete_tree(self, descriptor: dict[str, str]) -> dict[str, Any]:
        raw = str(descriptor.get("path") or "")
        if not raw:
            return {"ok": False, "refused": True, "message": "空路径，拒绝删除"}
        real = os.path.realpath(raw)
        if not self.allowed_roots:
            return {"ok": False, "refused": True, "message": "未配置允许的本地根目录，拒绝删除"}
        inside = False
        for root in self.allowed_roots:
            root_real = os.path.realpath(root)
            if real == root_real:
                return {"ok": False, "refused": True, "message": f"拒绝删除配置根目录自身：{root_real}"}
            try:
                if os.path.commonpath([os.path.normpath(real), os.path.normpath(root_real)]) == os.path.normpath(root_real):
                    inside = True
                    break
            except ValueError:
                continue
        if not inside:
            return {"ok": False, "refused": True, "message": f"本地路径越界，拒绝删除：{real}"}
        if not os.path.lexists(real):
            return {"ok": True, "skipped": True, "message": f"本地路径不存在，视为已清理：{real}"}
        shutil.rmtree(real, ignore_errors=False)
        return {"ok": True, "message": f"已递归删除任务级目录：{real}"}


class OpenListFilesystemOps(FilesystemOps):
    """OpenList 远端删除适配器：按冻结挂载删除任务级落地区目录。

    2026-09-05 线上事故：relay / rclone_copy 计划的 source_landing 是
    openlist 描述符，而组合根从未装配 OpenList 删除适配器，导致每次入库
    成功后清理阶段必然报「OpenList 删除适配器未配置」并耗尽重试，把已
    入库的任务标记为失败。这里补上真实适配器：挂载与修订都取冻结计划
    descriptor，不读实时配置；删除范围仍由 ``_job_scope_check`` 先行限定
    为 ``job-<id>`` 叶子。
    """

    def __init__(self, mount_service: Any) -> None:
        self.mount_service = mount_service

    def resolve_real(self, descriptor: dict[str, str]) -> str:
        return str(descriptor.get("path") or "")

    def delete_tree(self, descriptor: dict[str, str]) -> dict[str, Any]:
        mount_id = str(descriptor.get("mount_id") or "").strip()
        path = str(descriptor.get("path") or "").strip()
        if not mount_id or not path:
            return {
                "ok": False,
                "refused": True,
                "message": "OpenList 落地目录缺少挂载信息，拒绝删除",
            }
        try:
            revision = int(descriptor.get("mount_revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        try:
            client, mount = self.mount_service.client_for_mount(
                mount_id, revision=revision or None
            )
            resolver = getattr(self.mount_service, "resolve_mount_absolute_path", None)
            if callable(resolver):
                absolute = resolver(mount, path)
            else:
                root = str(mount.get("webdav_root") or "/")
                absolute = normalize_join(root, path)
            if not client.exists(absolute):
                return {
                    "ok": True,
                    "skipped": True,
                    "message": f"远端目录不存在，视为已清理：{absolute}",
                }
            client.delete(absolute)
            return {"ok": True, "message": f"已删除远端任务级目录：{absolute}"}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "retryable": True,
                "message": f"OpenList 远端目录删除失败：{exc}",
            }


def normalize_join(root: str, path: str) -> str:
    text = str(path or "").strip()
    root = str(root or "/").strip() or "/"
    if not text:
        return root
    if text == root or text.startswith(f"{root}/"):
        return text
    return f"{root}/{text.lstrip('/')}"


class CompositeFilesystemOps(FilesystemOps):
    """按描述符 kind 分发：local -> LocalFilesystemOps；openlist -> OpenList 删除适配器。"""

    def __init__(self, openlist_ops: FilesystemOps | None = None, local_ops: FilesystemOps | None = None) -> None:
        self.openlist_ops = openlist_ops
        self.local_ops = local_ops

    def _kind(self, descriptor: dict[str, str]) -> str:
        kind = str(descriptor.get("kind") or "")
        if kind:
            return kind
        namespace_id = str(descriptor.get("namespace_id") or "").lower()
        return "local" if namespace_id.startswith("local:") else "openlist"

    def resolve_real(self, descriptor: dict[str, str]) -> str:
        if self._kind(descriptor) in {"local", "relay"} and self.local_ops is not None:
            return self.local_ops.resolve_real(descriptor)
        return str(descriptor.get("path") or "")

    def delete_tree(self, descriptor: dict[str, str]) -> dict[str, Any]:
        kind = self._kind(descriptor)
        if kind in {"local", "relay"}:
            if self.local_ops is None:
                return {"ok": False, "retryable": True, "message": "本地删除适配器未配置"}
            return self.local_ops.delete_tree(descriptor)
        if kind == "native":
            return {"ok": False, "refused": True, "message": "native 目标不是可清理的落地区"}
        if self.openlist_ops is None:
            return {"ok": False, "retryable": True, "message": "OpenList 删除适配器未配置，跳过远端清理"}
        return self.openlist_ops.delete_tree(descriptor)


def _job_scope_check(descriptor: dict[str, str], job_id: int, filesystem: FilesystemOps, local_roots: tuple[str, ...]) -> CleanupItem | None:
    """返回 None 表示可安全删除；否则返回拒绝的 CleanupItem。"""
    path = str(descriptor.get("path") or "").strip().replace("\\", "/")
    kind = str(descriptor.get("kind") or "").strip()
    namespace_id = str(descriptor.get("namespace_id") or "")
    if not kind:
        kind = "local" if namespace_id.lower().startswith("local:") else "openlist"
    if not path or path in {"/", "."}:
        return CleanupItem(descriptor, False, refused=True, message="空路径，拒绝删除")
    parts = [part for part in path.strip("/").split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        return CleanupItem(descriptor, False, refused=True, message="越界路径，拒绝删除")
    if not parts:
        return CleanupItem(descriptor, False, refused=True, message="路径为空，拒绝删除")
    expected = f"job-{job_id}"
    if parts[-1] != expected:
        return CleanupItem(
            descriptor,
            False,
            refused=True,
            message=f"非任务级目录 {parts[-1]}，拒绝递归删除（只允许 {expected}）",
        )
    if kind in {"local", "relay"} and local_roots:
        real = os.path.normpath(os.path.realpath(str(filesystem.resolve_real(descriptor))))
        for root in local_roots:
            root_real = os.path.normpath(os.path.realpath(root))
            if real == root_real:
                return CleanupItem(descriptor, False, refused=True, message=f"拒绝删除配置根目录自身：{root_real}")
            try:
                if os.path.commonpath([real, root_real]) == root_real:
                    return None
            except ValueError:
                continue
        return CleanupItem(descriptor, False, refused=True, message=f"本地路径越界，拒绝删除：{real}")
    return None


class TransferCleanupService:
    """任务级清理：只在目标已确认 + 应用已 ACK 后执行，且只删除 job-<id> 作用域。"""

    def __init__(
        self,
        filesystem: FilesystemOps,
        *,
        local_roots: tuple[str, ...] = (),
        relay_root: str = "",
        openlist_ops: FilesystemOps | None = None,
    ) -> None:
        if filesystem is None:
            filesystem = CompositeFilesystemOps(
                openlist_ops=openlist_ops,
                local_ops=LocalFilesystemOps(allowed_roots=tuple(local_roots)) if local_roots else None,
            )
        self.filesystem = filesystem
        self.local_roots = tuple(str(root) for root in local_roots)
        if relay_root and relay_root not in self.local_roots:
            self.local_roots = self.local_roots + (relay_root,)

    def cleanup(
        self,
        plan: TransferPlan | dict[str, Any],
        *,
        target_verified: bool = False,
        app_acked: bool = False,
        delete_source: bool = True,
        delete_local_temp: bool = True,
    ) -> CleanupResult:
        if not isinstance(plan, TransferPlan):
            try:
                plan = TransferPlan.from_dict(plan)
            except Exception as exc:  # noqa: BLE001
                # Cleanup is a safety boundary, not a reason to crash the
                # worker.  A malformed/legacy descriptor must be represented
                # as a durable refusal so the caller can keep the source and
                # surface manual review; it must never be interpreted as a
                # wildcard root.
                return CleanupResult(
                    ok=False,
                    warning=False,
                    retryable=False,
                    reason=f"清理计划无效，拒绝删除：{exc}",
                    items=[],
                )
        if not target_verified or not app_acked:
            return CleanupResult(
                ok=True,
                skipped=True,
                reason="目标未确认或应用未 ACK，保留来源落地与本地缓存",
                items=[],
            )
        job_id = plan.job_id
        items: list[CleanupItem] = []
        landing = plan.source_landing
        if delete_source and landing is not None:
            item = self._delete_scoped(landing, job_id)
            items.append(item)
        local_temp = plan.local_temp
        if delete_local_temp and local_temp is not None:
            item = self._delete_scoped(local_temp, job_id)
            items.append(item)
        if not items:
            return CleanupResult(
                ok=True,
                skipped=True,
                reason="当前任务没有启用需要执行的任务级清理项",
                items=[],
            )
        failed = [item for item in items if not item.ok]
        if failed:
            return CleanupResult(
                ok=False,
                warning=True,
                retryable=True,
                reason="部分任务级目录清理失败，不影响已完成入库，可重试",
                items=items,
            )
        return CleanupResult(ok=True, items=items)

    def release_staging(
        self,
        plan: TransferPlan | dict[str, Any],
        *,
        target_root: str | None = None,
    ) -> CleanupResult:
        """终态失败时把任务级中继缓存移出单任务通道，不删除任何文件。

        与 :meth:`cleanup` 不同，这里不要求目标校验/应用 ACK：任务已经失败，
        缓存留在原目录只会永久占住 relay 的单任务通道。目录默认被移到同级
        ``.held/`` 下，也可由调用方指定同一挂载内的 ``relay_backups``；移动
        仍然经过 ``_job_scope_check``，只允许 ``job-<id>`` 叶子目录。
        """
        if not isinstance(plan, TransferPlan):
            try:
                plan = TransferPlan.from_dict(plan)
            except Exception as exc:  # noqa: BLE001
                return CleanupResult(
                    ok=False,
                    retryable=False,
                    reason=f"清理计划无效，拒绝移动：{exc}",
                    items=[],
                )
        local_temp = plan.local_temp
        if local_temp is None:
            return CleanupResult(ok=True, skipped=True, reason="当前任务没有本地中继缓存", items=[])
        refused = _job_scope_check(local_temp, plan.job_id, self.filesystem, self.local_roots)
        if refused is not None:
            return CleanupResult(
                ok=False,
                warning=True,
                retryable=False,
                reason=refused.message or "任务级中继缓存路径不安全，拒绝移动",
                items=[refused],
            )
        real = os.path.realpath(str(self.filesystem.resolve_real(local_temp)))
        if not os.path.exists(real):
            return CleanupResult(ok=True, skipped=True, reason="当前任务没有本地中继缓存", items=[])
        if not os.path.isdir(real):
            return CleanupResult(
                ok=False,
                warning=True,
                retryable=False,
                reason=f"任务级中继缓存不是目录，拒绝移动：{real}",
                items=[],
            )
        held_root = (
            str(target_root).strip()
            if isinstance(target_root, str) and target_root.strip()
            else os.path.join(os.path.dirname(real), ".held")
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = os.path.join(held_root, f"{os.path.basename(real)}-{stamp}")
        try:
            os.makedirs(held_root, exist_ok=True)
            suffix = 1
            while os.path.exists(target):
                suffix += 1
                target = os.path.join(held_root, f"{os.path.basename(real)}-{stamp}-{suffix}")
            os.replace(real, target)
        except OSError as exc:
            return CleanupResult(
                ok=False,
                warning=True,
                retryable=True,
                reason=f"隔离任务级中继缓存失败：{exc}",
                items=[],
            )
        return CleanupResult(
            ok=True,
            reason=f"任务级中继缓存已隔离至 {target}",
            items=[CleanupItem(local_temp, True, message=f"已移动到 {target}")],
        )

    def _delete_scoped(self, descriptor: dict[str, str], job_id: int) -> CleanupItem:
        refused = _job_scope_check(descriptor, job_id, self.filesystem, self.local_roots)
        if refused is not None:
            return refused
        try:
            result = self.filesystem.delete_tree(descriptor)
        except Exception as exc:  # noqa: BLE001
            return CleanupItem(descriptor=descriptor, ok=False, retryable=True, message=f"清理异常：{exc}")
        if isinstance(result, dict):
            ok = bool(result.get("ok"))
            skipped = bool(result.get("skipped"))
            refused_flag = bool(result.get("refused"))
            return CleanupItem(
                descriptor=descriptor,
                ok=ok,
                skipped=skipped,
                refused=refused_flag,
                retryable=not refused_flag and not ok,
                message=str(result.get("message") or ("已清理" if ok else "清理失败")),
            )
        return CleanupItem(descriptor=descriptor, ok=bool(result), message="已清理")
