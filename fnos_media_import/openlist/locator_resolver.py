"""OpenList 受控 rclone locator 解析（§7.4）。

任务只能提交 ``connection_id + mount_id + normalized_relative_path``，由本
解析器在服务端转换为受控 ``remote:path``。客户端、任务 payload 与 Adapter
都不能传 rclone 配置文件路径或任意 remote 名称。
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Callable, Mapping

from . import OpenListValidationError

REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
NAMESPACE_PREFIX = "openlist"

MountRootResolver = Callable[[str, str], str]
RevisionResolver = Callable[[str, int], Mapping[str, Any] | None]


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
        self.config_path = ""
        self.remote_name = ""
        self.namespace_prefix = NAMESPACE_PREFIX
        self.local_target_root = "/storage-targets"
        self.relay_root = "/data/relay"
        self._mount_root_resolver: MountRootResolver = lambda _cid, _mid: ""
        self._mount_revision_resolver: RevisionResolver | None = None
        self._connection_revision_resolver: RevisionResolver | None = None
        self.update_config(config)

    def update_config(self, config: Mapping[str, Any] | None = None) -> None:
        """Apply deployment tuning without dropping bound revision services.

        The resolver is shared by the transfer worker and the direct fallback.
        Hot-reload must therefore mutate this instance rather than replace it;
        the immutable mount/connection callbacks remain attached across a
        reload.  No task path or remote supplied by a caller is persisted.
        """

        values = dict(config or {})
        self.config_path = str(values.get("config_path") or self.config_path or "").strip()
        self.remote_name = str(
            values.get("remote_name") or values.get("remote") or self.remote_name or ""
        ).strip()
        self.namespace_prefix = (
            str(values.get("namespace_prefix") or self.namespace_prefix or NAMESPACE_PREFIX).strip()
            or NAMESPACE_PREFIX
        )
        configured_local_root = values.get("local_target_root")
        if configured_local_root not in (None, ""):
            self.local_target_root = _normalize_local_root(configured_local_root)
        configured_relay_root = values.get("relay_root")
        if configured_relay_root not in (None, ""):
            self.relay_root = _normalize_local_root(configured_relay_root)
        mount_root_resolver = values.get("mount_root_resolver")
        if callable(mount_root_resolver):
            self._mount_root_resolver = mount_root_resolver
        mount_revision_resolver = values.get("mount_revision_resolver")
        if callable(mount_revision_resolver):
            self._mount_revision_resolver = mount_revision_resolver
        connection_revision_resolver = values.get("connection_revision_resolver")
        if callable(connection_revision_resolver):
            self._connection_revision_resolver = connection_revision_resolver
        self._validate_managed_remote()

    def bind_revision_services(
        self,
        mount_service: Any = None,
        connection_service: Any = None,
    ) -> "RcloneLocatorResolver":
        """Bind the V1 revision readers used by task-scoped resolution.

        Only the ``get_revision(id, revision)`` methods are retained.  The
        resolver never reads a mutable current mount/connection while building
        a descriptor for an immutable plan.
        """

        mount_reader = getattr(mount_service, "get_revision", None)
        connection_reader = getattr(connection_service, "get_revision", None)
        if callable(mount_reader):
            self._mount_revision_resolver = mount_reader
        if callable(connection_reader):
            self._connection_revision_resolver = connection_reader
        return self

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

    def parse_namespace(self, namespace_id: Any, *, mount_id: str = "") -> tuple[str, str]:
        """Parse and validate one managed OpenList namespace identity."""

        text = str(namespace_id or "").strip()
        parts = text.split(":", 2)
        if len(parts) != 3 or parts[0] != self.namespace_prefix:
            raise OpenListValidationError(f"非法 OpenList namespace：{namespace_id}")
        connection_id, namespace_mount_id = parts[1].strip(), parts[2].strip()
        if not connection_id or not namespace_mount_id:
            raise OpenListValidationError("OpenList namespace 缺少 connection_id 或 mount_id")
        if mount_id and str(mount_id).strip() != namespace_mount_id:
            raise OpenListValidationError("namespace 与 mount_id 不一致")
        if self.namespace_id(connection_id, namespace_mount_id) != text:
            raise OpenListValidationError("OpenList namespace 身份格式不一致")
        return connection_id, namespace_mount_id

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

    def remote(
        self,
        *,
        namespace_id: str,
        mount_id: str = "",
        path: str = "",
        mount_revision: int | None = None,
        connection_id: str = "",
        connection_revision: int | None = None,
        storage_identity_id: str = "",
        identity_root: str = "/",
        role: str = "source",
        mount_snapshot: Mapping[str, Any] | None = None,
        connection_snapshot: Mapping[str, Any] | None = None,
    ) -> str:
        """Resolve a remote path, using a frozen revision when supplied.

        The old three-argument form remains useful for admin diagnostics.  A
        data-plane caller must pass ``mount_revision`` (or an explicit
        ``mount_snapshot``), which switches to the fail-closed managed path
        below and prevents a current mount edit from changing an in-flight
        task's remote root.
        """

        if mount_snapshot is not None or mount_revision not in (None, ""):
            return self.resolve_frozen_remote(
                namespace_id=namespace_id,
                mount_id=mount_id,
                mount_revision=mount_revision,
                connection_id=connection_id,
                connection_revision=connection_revision,
                storage_identity_id=storage_identity_id,
                identity_root=identity_root,
                path=path,
                role=role,
                mount_snapshot=mount_snapshot,
                connection_snapshot=connection_snapshot,
            )
        parsed_connection, parsed_mount = self.parse_namespace(namespace_id, mount_id=mount_id)
        root = self.webdav_root(parsed_connection, parsed_mount)
        return self.remote_path(root, path)

    def resolve_frozen_remote(
        self,
        *,
        namespace_id: str,
        mount_id: str,
        mount_revision: int | None,
        connection_id: str = "",
        connection_revision: int | None = None,
        storage_identity_id: str = "",
        identity_root: str = "/",
        path: str = "",
        role: str = "source",
        mount_snapshot: Mapping[str, Any] | None = None,
        connection_snapshot: Mapping[str, Any] | None = None,
    ) -> str:
        """Resolve ``remote:/path`` from immutable mount/connection snapshots."""

        parsed_connection, parsed_mount = self.parse_namespace(namespace_id, mount_id=mount_id)
        revision = _positive_revision(mount_revision, "mount_revision")
        expected_connection = str(connection_id or parsed_connection).strip()
        if expected_connection != parsed_connection:
            raise OpenListValidationError("冻结 connection_id 与 namespace 不一致")
        conn_revision = _positive_revision(connection_revision, "connection_revision")
        expected_identity = str(storage_identity_id or "").strip()
        expected_root = normalize_openlist_path(identity_root or "/")
        snapshot = dict(mount_snapshot or {})
        if not snapshot:
            reader = self._mount_revision_resolver
            if not callable(reader):
                raise OpenListValidationError("缺少 OpenList mount revision 读取器")
            snapshot = dict(reader(parsed_mount, revision) or {})
        if not snapshot:
            raise OpenListValidationError(
                f"OpenList mount 修订不存在：{parsed_mount}@{revision}"
            )
        self._validate_mount_snapshot(
            snapshot,
            namespace_id=namespace_id,
            mount_id=parsed_mount,
            mount_revision=revision,
            connection_id=parsed_connection,
            connection_revision=conn_revision,
            storage_identity_id=expected_identity,
            identity_root=expected_root,
            role=role,
        )
        conn = dict(connection_snapshot or {})
        if not conn:
            reader = self._connection_revision_resolver
            if not callable(reader):
                raise OpenListValidationError("缺少 OpenList connection revision 读取器")
            conn = dict(reader(parsed_connection, conn_revision) or {})
        if not conn:
            raise OpenListValidationError(
                f"OpenList connection 修订不存在：{parsed_connection}@{conn_revision}"
            )
        self._validate_connection_snapshot(
            conn,
            connection_id=parsed_connection,
            connection_revision=conn_revision,
        )
        return self.remote_path(str(snapshot.get("webdav_root") or ""), path)

    @staticmethod
    def split_remote_path(value: Any) -> tuple[str, str]:
        """Split one resolver result into ``(managed_remote, absolute_path)``."""

        text = str(value or "").strip().replace("\\", "/")
        if ":" not in text.split("/", 1)[0]:
            raise OpenListValidationError("受控 remote 路径缺少 remote 名称")
        remote, path = text.split(":", 1)
        if not REMOTE_NAME_RE.fullmatch(remote):
            raise OpenListValidationError("受控 remote 名称不合法")
        normalized = normalize_openlist_path(path or "/")
        return remote, normalized

    def local(self, *, namespace_id: str, path: str) -> str:
        text_namespace = str(namespace_id or "").strip()
        if not text_namespace.lower().startswith("local:") or not text_namespace[6:].strip():
            raise OpenListValidationError("本地路径 namespace 非法")
        value = str(path or "").replace("\\", "/").strip()
        if not value.startswith("/"):
            raise OpenListValidationError("本地路径必须是绝对路径")
        if any(part in {".", ".."} for part in value.split("/") if part):
            raise OpenListValidationError("本地路径不允许越级")
        normalized = posixpath.normpath(value)
        if not _path_contained(normalized, self.local_target_root):
            raise OpenListValidationError("本地路径越出 /storage-targets 受控根目录")
        return normalized

    def relay(self, *, namespace_id: str, path: str) -> str:
        if str(namespace_id or "").strip() != "local:relay":
            raise OpenListValidationError("中继路径 namespace 必须是 local:relay")
        value = str(path or "").replace("\\", "/").strip()
        if not value.startswith("/"):
            raise OpenListValidationError("中继路径必须是绝对路径")
        if any(part in {".", ".."} for part in value.split("/") if part):
            raise OpenListValidationError("中继路径不允许越级")
        normalized = posixpath.normpath(value)
        if not _path_contained(normalized, self.relay_root):
            raise OpenListValidationError("中继路径越出受控 relay 根目录")
        return normalized

    def _validate_mount_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        namespace_id: str,
        mount_id: str,
        mount_revision: int,
        connection_id: str,
        connection_revision: int,
        storage_identity_id: str,
        identity_root: str,
        role: str,
    ) -> None:
        if str(snapshot.get("mount_id") or mount_id).strip() != mount_id:
            raise OpenListValidationError("冻结 mount_id 与 mount revision 不一致")
        if _positive_revision(snapshot.get("revision"), "mount revision") != mount_revision:
            raise OpenListValidationError("冻结 mount revision 不一致")
        if str(snapshot.get("connection_id") or "").strip() != connection_id:
            raise OpenListValidationError("冻结 mount connection_id 不一致")
        if _positive_revision(snapshot.get("connection_revision"), "mount connection revision") != connection_revision:
            raise OpenListValidationError("冻结 mount connection_revision 不一致")
        if str(snapshot.get("namespace_id") or "").strip() != namespace_id:
            raise OpenListValidationError("冻结 mount namespace 不一致")
        if not bool(snapshot.get("enabled")):
            raise OpenListValidationError("冻结 OpenList mount 已停用")
        status = str(snapshot.get("identity_status") or "").strip().lower()
        if status not in {"verified", "declared"}:
            raise OpenListValidationError("冻结 OpenList mount 身份未确认")
        if not storage_identity_id or str(snapshot.get("storage_identity_id") or "").strip() != storage_identity_id:
            raise OpenListValidationError("冻结 mount storage identity 不一致")
        if normalize_openlist_path(snapshot.get("identity_root") or "/") != identity_root:
            raise OpenListValidationError("冻结 mount identity_root 不一致")
        if not str(snapshot.get("webdav_root") or "").strip():
            raise OpenListValidationError("冻结 mount 缺少 webdav_root")
        readable = bool(snapshot.get("readable"))
        writable = bool(snapshot.get("writable"))
        normalized_role = str(role or "source").strip().lower()
        if normalized_role == "source" and not readable:
            raise OpenListValidationError("来源 OpenList mount 不可读")
        if normalized_role == "destination" and (not readable or not writable):
            raise OpenListValidationError("目标 OpenList mount 不具备读写能力")

    @staticmethod
    def _validate_connection_snapshot(
        snapshot: Mapping[str, Any], *, connection_id: str, connection_revision: int
    ) -> None:
        if str(snapshot.get("connection_id") or "").strip() != connection_id:
            raise OpenListValidationError("冻结 connection_id 不一致")
        if _positive_revision(snapshot.get("revision"), "connection revision") != connection_revision:
            raise OpenListValidationError("冻结 connection revision 不一致")
        if not bool(snapshot.get("enabled")):
            raise OpenListValidationError("冻结 OpenList connection 已停用")
        if "credential_configured" in snapshot and not bool(snapshot.get("credential_configured")):
            raise OpenListValidationError("冻结 OpenList connection 未配置凭证")
        if "last_test_status" in snapshot and str(snapshot.get("last_test_status") or "").strip().lower() not in {"", "ok"}:
            raise OpenListValidationError("冻结 OpenList connection 检测未通过")


def _positive_revision(value: Any, label: str) -> int:
    try:
        revision = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise OpenListValidationError(f"{label} 必须为正整数") from exc
    if revision <= 0:
        raise OpenListValidationError(f"{label} 必须为正整数")
    return revision


def _normalize_local_root(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text.startswith("/"):
        raise OpenListValidationError("local_target_root 必须是绝对路径")
    if any(part in {".", ".."} for part in text.split("/") if part):
        raise OpenListValidationError("local_target_root 不允许越级")
    normalized = posixpath.normpath(text)
    if normalized in {"", ".", "/"}:
        raise OpenListValidationError("local_target_root 不能是根目录")
    return normalized.rstrip("/")


def _path_contained(path: Any, root: Any) -> bool:
    normalized_path = posixpath.normpath(str(path or "").replace("\\", "/"))
    normalized_root = _normalize_local_root(root)
    return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")


__all__ = ["RcloneLocatorResolver", "normalize_openlist_path", "NAMESPACE_PREFIX"]
