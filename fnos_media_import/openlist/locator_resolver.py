"""OpenList 受控 rclone locator 解析（§7.4）。

任务只能提交 ``connection_id + mount_id + normalized_relative_path``，由本
解析器在服务端转换为受控 ``remote:path``。客户端、任务 payload 与 Adapter
都不能传 rclone 配置文件路径或任意 remote 名称。
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Callable

from . import OpenListValidationError

REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
NAMESPACE_PREFIX = "openlist"

MountRootResolver = Callable[[str, str], str]


def normalize_openlist_path(value: Any, *, allow_empty: bool = False) -> str:
    """规范化 OpenList 虚拟路径。

    规则：统一反斜杠、强制前导 ``/``、拒绝 ``..`` 越级、URL 协议与控制字符；
    根路径恒为 ``/``。
    """
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        if allow_empty:
            return ""
        raise OpenListValidationError("路径为空")
    if "://" in text:
        raise OpenListValidationError("路径不允许包含 URL 协议")
    if any(ord(ch) < 32 for ch in text):
        raise OpenListValidationError("路径不允许包含控制字符")
    # normpath 会把开头的 “..” 折叠到虚拟根，这里必须在折叠前显式拒绝越级。
    if ".." in text.strip("/").split("/"):
        raise OpenListValidationError("路径不允许越级（..）")
    return posixpath.normpath("/" + text.strip("/"))


class RcloneLocatorResolver:
    """把 (connection_id, mount_id, relative_path) 解析为受控 remote:path。

    ``config`` 只包含部署级受控参数：

    - ``remote_name``：应用管理的单个 WebDAV remote 名称。
    - ``config_path``：应用管理的 rclone 配置文件路径。
    - ``mount_root_resolver``：可选，``(connection_id, mount_id) -> webdav_root``；
      缺省时需通过 ``webdav_root=`` 显式传入挂载根。
    - ``namespace_prefix``：namespace 前缀，默认 ``openlist``。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = dict(config or {})
        self.config_path = str(config.get("config_path") or "").strip()
        self.remote_name = str(config.get("remote_name") or config.get("remote") or "").strip()
        self.namespace_prefix = str(config.get("namespace_prefix") or NAMESPACE_PREFIX).strip() or NAMESPACE_PREFIX
        mount_root_resolver = config.get("mount_root_resolver")
        self._mount_root_resolver: MountRootResolver = (
            mount_root_resolver if callable(mount_root_resolver) else (lambda _cid, _mid: "")
        )
        self._validate_managed_remote()

    def _validate_managed_remote(self) -> None:
        if not self.remote_name:
            raise OpenListValidationError("缺少受控 rclone remote 名称（remote_name）")
        if not REMOTE_NAME_RE.fullmatch(self.remote_name):
            raise OpenListValidationError(
                f"受控 rclone remote 名称不合法：{self.remote_name!r}（只允许字母/数字/_.-）"
            )
        if not self.config_path:
            raise OpenListValidationError("缺少受控 rclone 配置文件路径（config_path）")

    def namespace_id(self, connection_id: str, mount_id: str) -> str:
        """生成 mount 的 namespace 标识：``openlist:<connection_id>:<mount_id>``。"""
        connection_id = str(connection_id or "").strip()
        mount_id = str(mount_id or "").strip()
        if not connection_id or not mount_id:
            raise OpenListValidationError("namespace 需要非空的 connection_id 与 mount_id")
        return f"{self.namespace_prefix}:{connection_id}:{mount_id}"

    def webdav_root(self, connection_id: str, mount_id: str) -> str:
        """解析挂载根路径（规范化为虚拟路径）。"""
        raw = self._mount_root_resolver(connection_id, mount_id)
        return normalize_openlist_path(raw)

    def remote_path(self, webdav_root: str, relative_path: str = "") -> str:
        """把挂载根与相对路径拼成受控 ``remote:path``（不校验根非空）。"""
        root = normalize_openlist_path(webdav_root)
        relative = normalize_openlist_path(relative_path, allow_empty=True)
        joined = posixpath.normpath(root + "/" + relative.lstrip("/"))
        if joined in ("", "/"):
            return f"{self.remote_name}:"
        return f"{self.remote_name}:{joined}"

    def resolve(self, connection_id: str, mount_id: str, relative_path: str) -> str:
        """解析任务可提交的完整受控路径。

        mount 根本身不允许作为任务写入路径（路径红线，§8.5），除非管理员显式
        开启“允许写根目录”；本解析器默认拒绝根写入，因此相对路径不能为空。
        """
        root = self.webdav_root(connection_id, mount_id)
        if root in ("", "/"):
            raise OpenListValidationError("mount webdav_root 为空或为根目录，不允许作为任务路径")
        relative = normalize_openlist_path(relative_path, allow_empty=False)
        return self.remote_path(root, relative)

    def path_ref(self, connection_id: str, mount_id: str, relative_path: str) -> dict[str, str]:
        """返回 (namespace_id, normalized_path) 身份对，用于路径重叠比较。"""
        return {
            "namespace_id": self.namespace_id(connection_id, mount_id),
            "path": normalize_openlist_path(relative_path),
        }


__all__ = ["RcloneLocatorResolver", "normalize_openlist_path", "NAMESPACE_PREFIX"]
