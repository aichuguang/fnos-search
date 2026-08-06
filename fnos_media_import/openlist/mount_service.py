"""OpenList 挂载服务：CRUD、目录选择器与完整读写检测（§7.3）。

mount 是 Provider 账号与 OpenList WebDAV 根的映射，``namespace_id`` 固定为
``openlist:<connection_id>:<mount_id>``，创建后不可编辑。检测在管理员配置的
专用探测目录（默认 ``/_fnos_probe``）内创建任务级临时目录与文件，绝不污染
mount 根；清理失败时记录残留完整路径并在结果中给出“需要清理”告警。
"""

from __future__ import annotations

import posixpath
import sqlite3
import uuid
from typing import Any, Callable

from ..time_utils import utc_now_iso
from . import OpenListNotFoundError, OpenListValidationError
from .connection_service import OpenListConnectionService
from .locator_resolver import normalize_openlist_path
from .webdav_client import OpenListWebDAVClient

WebDAVFactory = Callable[[dict[str, Any]], Any]
DEFAULT_PROBE_ROOT = "/_fnos_probe"
_MESSAGE_LIMIT = 300


def _short(message: Any) -> str:
    return str(message or "")[: _MESSAGE_LIMIT]


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _new_mount_id() -> str:
    return f"mount_{uuid.uuid4().hex[:12]}"


class OpenListMountService:
    """Provider 账号与 OpenList mount 的映射及读写能力检测。"""

    def __init__(
        self,
        database: Any,
        connection_service: OpenListConnectionService | None = None,
        webdav_factory: WebDAVFactory | None = None,
        probe_root: str = DEFAULT_PROBE_ROOT,
    ) -> None:
        self.database = database
        self.connection_service = connection_service
        self.webdav_factory = webdav_factory or self._default_webdav_factory
        self.probe_root = probe_root

    @staticmethod
    def _default_webdav_factory(credentials: dict[str, Any]) -> OpenListWebDAVClient:
        return OpenListWebDAVClient(
            base_url=credentials["base_url"],
            username=credentials["username"],
            password=credentials["password"],
        )

    # ---- 查询 / 序列化 ----

    def list_mounts(self, connection_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            if connection_id:
                rows = conn.execute(
                    "SELECT * FROM openlist_mounts WHERE connection_id=? ORDER BY name",
                    (connection_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM openlist_mounts ORDER BY name").fetchall()
            return [self._serialize(row) for row in rows]

    def get_mount(self, mount_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = self._get_mount_row(conn, mount_id)
            return self._serialize(row) if row is not None else None

    @staticmethod
    def _get_mount_row(conn: Any, mount_id: str) -> Any:
        return conn.execute(
            "SELECT * FROM openlist_mounts WHERE mount_id=?", (mount_id,)
        ).fetchone()

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        return {
            "mount_id": row["mount_id"],
            "connection_id": row["connection_id"],
            "name": row["name"],
            "provider_kind": row["provider_kind"],
            "account_id": row["account_id"],
            "webdav_root": row["webdav_root"],
            "namespace_id": row["namespace_id"],
            "readable": bool(row["readable"]),
            "writable": bool(row["writable"]),
            "enabled": bool(row["enabled"]),
            "revision": int(row["revision"] or 1),
            "identity_status": row["identity_status"],
            "last_tested_at": row["last_tested_at"],
        }

    # ---- 写操作 ----

    def create_mount(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        connection_id = str(payload.get("connection_id") or "").strip()
        if not connection_id:
            raise OpenListValidationError("connection_id 不能为空")
        webdav_root = normalize_openlist_path(payload.get("webdav_root"))
        mount_id = str(payload.get("mount_id") or "").strip() or _new_mount_id()
        name = str(payload.get("name") or "").strip() or posixpath.basename(webdav_root.rstrip("/")) or webdav_root
        provider_kind = str(payload.get("provider_kind") or "").strip() or None
        account_id = str(payload.get("account_id") or "").strip() or None
        enabled = _to_bool(payload.get("enabled"), True)
        namespace_id = f"openlist:{connection_id}:{mount_id}"
        with self.database.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM openlist_connections WHERE connection_id=?", (connection_id,)
            ).fetchone() is None:
                raise OpenListValidationError(f"OpenList 连接不存在：{connection_id}")
            if account_id:
                account = conn.execute(
                    "SELECT provider_kind FROM provider_accounts WHERE account_id=?", (account_id,)
                ).fetchone()
                if account is None:
                    raise OpenListValidationError(f"绑定的账号不存在：{account_id}")
                if provider_kind and provider_kind != account["provider_kind"]:
                    raise OpenListValidationError("绑定的 provider_kind 与账号类型不一致")
                provider_kind = account["provider_kind"]
            try:
                conn.execute(
                    """
                    INSERT INTO openlist_mounts
                      (mount_id, connection_id, name, provider_kind, account_id, webdav_root,
                       namespace_id, readable, writable, enabled, revision, identity_status, last_tested_at)
                    VALUES (?,?,?,?,?,?,?,0,0,?,1,'unverified',NULL)
                    """,
                    (
                        mount_id,
                        connection_id,
                        name,
                        provider_kind,
                        account_id,
                        webdav_root,
                        namespace_id,
                        int(enabled),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OpenListValidationError(f"挂载已存在或绑定冲突：{mount_id}") from exc
        return self.get_mount(mount_id)

    def update_mount(self, mount_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        with self.database.connect() as conn:
            row = self._get_mount_row(conn, mount_id)
            if row is None:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
            name = str(payload.get("name") if payload.get("name") is not None else row["name"] or "").strip() or row["name"]
            webdav_root = normalize_openlist_path(
                payload.get("webdav_root") if payload.get("webdav_root") is not None else row["webdav_root"]
            )
            enabled = _to_bool(payload.get("enabled"), bool(row["enabled"]))
            if "provider_kind" in payload or "account_id" in payload:
                provider_kind = str(payload.get("provider_kind") if payload.get("provider_kind") is not None else row["provider_kind"] or "").strip() or None
                account_id = str(payload.get("account_id") if payload.get("account_id") is not None else row["account_id"] or "").strip() or None
                self._validate_bind(conn, provider_kind, account_id)
            else:
                provider_kind = row["provider_kind"]
                account_id = row["account_id"]
            revision = int(row["revision"] or 1) + 1
            conn.execute(
                """
                UPDATE openlist_mounts
                   SET name=?, webdav_root=?, provider_kind=?, account_id=?, enabled=?, revision=?
                 WHERE mount_id=?
                """,
                (name, webdav_root, provider_kind, account_id, int(enabled), revision, mount_id),
            )
        return self.get_mount(mount_id)

    @staticmethod
    def _validate_bind(conn: Any, provider_kind: str | None, account_id: str | None) -> None:
        if not account_id:
            return
        account = conn.execute(
            "SELECT provider_kind FROM provider_accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        if account is None:
            raise OpenListValidationError(f"绑定的账号不存在：{account_id}")
        if provider_kind and provider_kind != account["provider_kind"]:
            raise OpenListValidationError("绑定的 provider_kind 与账号类型不一致")

    def delete_mount(self, mount_id: str) -> dict[str, Any]:
        with self.database.connect() as conn:
            cursor = conn.execute("DELETE FROM openlist_mounts WHERE mount_id=?", (mount_id,))
            if cursor.rowcount == 0:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
        return {"mount_id": mount_id, "deleted": True}

    # ---- 目录选择器（§17.3）----

    def list_mountable_dirs(self, connection_id: str) -> list[dict[str, str]]:
        """列出连接 WebDAV 根下的目录，供管理员选择 mount 根，不手输路径。"""
        if self.connection_service is None:
            raise OpenListValidationError("缺少 connection_service，无法列出 OpenList 目录")
        credentials = self.connection_service.resolve_credentials(connection_id)
        client = self.webdav_factory(credentials)
        try:
            entries = client.list_dir("/")
        except Exception as exc:  # noqa: BLE001 - 统一包装为可读错误
            raise OpenListValidationError(f"列出 OpenList 目录失败：{_short(exc)}") from exc
        return [
            {"path": str(item["path"]), "name": str(item["name"])}
            for item in entries
            if item.get("is_dir")
        ]

    # ---- 完整读写检测（§7.3）----

    def probe_mount(self, mount_id: str, probe_root: str | None = None) -> dict[str, Any]:
        with self.database.connect() as conn:
            row = self._get_mount_row(conn, mount_id)
            if row is None:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
        if self.connection_service is None:
            raise OpenListValidationError("缺少 connection_service，无法检测挂载")
        credentials = self.connection_service.resolve_credentials(row["connection_id"])
        client = self.webdav_factory(credentials)
        root = normalize_openlist_path(probe_root or self.probe_root)
        task_dir = posixpath.join(root, f"fnos-probe-{uuid.uuid4().hex}")
        probe_file = posixpath.join(task_dir, "probe.txt")
        probe_content = b"fnos-openlist-probe"

        checks: list[dict[str, Any]] = []
        residue: list[str] = []

        # 1) WebDAV 认证与服务版本探测
        try:
            client.auth_probe()
            checks.append({"name": "认证与服务探测", "ok": True, "required": True, "message": "WebDAV 认证通过"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "认证与服务探测", "ok": False, "required": True, "message": _short(exc)})
            return self._finish(row, checks, residue, readable=False, writable=False)

        # 2) mount 根目录列举
        list_ok = False
        try:
            entries = client.list_dir(row["webdav_root"])
            list_ok = True
            checks.append({"name": "mount 根目录列举", "ok": True, "required": True, "message": f"读取到 {len(entries)} 个条目"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "mount 根目录列举", "ok": False, "required": True, "message": _short(exc)})

        # 3) 在探测目录创建任务级临时目录与小文件（绝不写 mount 根）
        created = False
        probe_started = False
        if list_ok:
            try:
                client.mkcol(task_dir)
                probe_started = True
                client.put_bytes(probe_file, probe_content)
                created = True
                checks.append({"name": "临时目录与文件创建", "ok": True, "required": True, "message": f"探测目录创建成功：{probe_file}"})
            except Exception as exc:  # noqa: BLE001
                # mkcol 成功但写文件失败时，目录仍在服务器上，交给清理步骤处理。
                probe_started = True
                checks.append({"name": "临时目录与文件创建", "ok": False, "required": True, "message": _short(exc)})
        else:
            checks.append({"name": "临时目录与文件创建", "ok": False, "required": True, "message": "根目录列举失败，未执行写入探测"})

        # 4) 读取文件大小与内容摘要
        read_ok = False
        if created:
            try:
                size = client.file_size(probe_file)
                content = client.read_bytes(probe_file)
                read_ok = size == len(probe_content) and content == probe_content
                message = "文件大小与内容一致" if read_ok else "文件大小或内容不一致"
            except Exception as exc:  # noqa: BLE001
                message = _short(exc)
        else:
            message = "临时文件未创建，跳过读取"
        checks.append({"name": "文件读取（大小与内容）", "ok": read_ok, "required": True, "message": message})

        # 5) rename / move 能力探测
        moved_path = posixpath.join(task_dir, "probe-moved.txt")
        move_ok = False
        if created:
            try:
                client.move(probe_file, moved_path)
                move_ok = True
                message = "rename/move 成功"
            except Exception as exc:  # noqa: BLE001
                message = f"rename/move 不可用：{_short(exc)}"
        else:
            message = "临时文件未创建，跳过移动探测"
        checks.append({"name": "rename/move 能力", "ok": move_ok, "required": False, "message": message})

        # 6) 删除文件与临时目录；失败则记录残留路径
        delete_ok = False
        if probe_started:
            try:
                for path in (probe_file, moved_path, task_dir):
                    if client.exists(path):
                        client.delete(path)
                leftover = [path for path in (probe_file, moved_path, task_dir) if client.exists(path)]
                delete_ok = not leftover
                if delete_ok:
                    message = "临时文件与目录删除成功"
                else:
                    message = "残留探测路径：" + ", ".join(leftover)
                    residue.extend(leftover)
            except Exception as exc:  # noqa: BLE001
                delete_ok = False
                message = _short(exc)
                residue.extend([probe_file, moved_path, task_dir])
        else:
            message = "探测目录未创建，跳过清理"
        checks.append({"name": "临时清理删除", "ok": delete_ok, "required": True, "message": message})

        readable = bool(list_ok and read_ok)
        writable = bool(created and delete_ok)
        return self._finish(row, checks, residue, readable=readable, writable=writable)

    def _finish(
        self,
        row: Any,
        checks: list[dict[str, Any]],
        residue: list[str],
        *,
        readable: bool,
        writable: bool,
    ) -> dict[str, Any]:
        mount_id = row["mount_id"]
        identity_status = str(row["identity_status"] or "unverified")
        bound = bool(row["account_id"] or row["provider_kind"])
        if bound and identity_status == "unverified" and (readable or writable):
            # 接口无法自动比对身份；管理员已确认映射但未验证 -> declared（§7.3）
            identity_status = "declared"
        now = utc_now_iso()
        with self.database.connect() as conn:
            conn.execute(
                """
                UPDATE openlist_mounts
                   SET readable=?, writable=?, identity_status=?, last_tested_at=?
                 WHERE mount_id=?
                """,
                (int(readable), int(writable), identity_status, now, mount_id),
            )
        errors = [str(item.get("message") or item.get("name") or "检测失败") for item in checks if item.get("required") and not item.get("ok")]
        ok = not errors
        return {
            "success": ok,
            "ok": ok,
            "mount_id": mount_id,
            "connection_id": row["connection_id"],
            "readable": readable,
            "writable": writable,
            "identity_status": identity_status,
            "checks": checks,
            "errors": errors,
            "residue_warnings": residue,
            "message": "mount 读写检测通过" if ok else "mount 读写检测未通过",
        }


__all__ = ["OpenListMountService", "DEFAULT_PROBE_ROOT", "WebDAVFactory"]
