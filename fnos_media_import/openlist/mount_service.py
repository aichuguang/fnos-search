"""OpenList 挂载服务：CRUD、目录选择器与完整读写检测（§7.3）。

mount 是 Provider 账号与 OpenList WebDAV 根的映射，``namespace_id`` 固定为
``openlist:<connection_id>:<mount_id>``，创建后不可编辑。检测在管理员配置的
专用探测目录（默认 ``/_fnos_probe``）内创建任务级临时目录与文件，绝不污染
mount 根；清理失败时记录残留完整路径并在结果中给出“需要清理”告警。
"""

from __future__ import annotations

import json
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


def _optional_revision(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        error = OpenListValidationError(f"{field_name} 必须是正整数")
        error.code = "INVALID_REVISION"
        error.status = 400
        error.field_errors = {field_name: "必须是正整数"}
        raise error from exc
    if revision <= 0:
        error = OpenListValidationError(f"{field_name} 必须是正整数")
        error.code = "INVALID_REVISION"
        error.status = 400
        error.field_errors = {field_name: "必须是正整数"}
        raise error
    return revision


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
            return [self._serialize(row, conn) for row in rows]

    def get_mount(self, mount_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = self._get_mount_row(conn, mount_id)
            return self._serialize(row, conn) if row is not None else None

    def get_revision(self, mount_id: str, revision: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT config_json FROM openlist_mount_revisions WHERE mount_id=? AND revision=?",
                (mount_id, int(revision)),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["config_json"] or "{}")
        return {"mount_id": mount_id, "revision": int(revision), **value}

    def find_mount_for_account(
        self,
        provider_kind: str,
        account_id: str,
        *,
        readable: bool = True,
    ) -> dict[str, Any] | None:
        clauses = ["provider_kind=?", "account_id=?", "enabled=1", "identity_status IN ('verified','declared')"]
        values: list[Any] = [provider_kind, account_id]
        if readable:
            clauses.append("readable=1")
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM openlist_mounts WHERE {' AND '.join(clauses)} ORDER BY revision DESC, mount_id",
                values,
            ).fetchall()
            if len(rows) > 1:
                raise OpenListValidationError(
                    f"账号 {account_id} 存在多个可读 landing mount，请保留唯一启用映射"
                )
            return self._serialize(rows[0], conn) if rows else None

    @staticmethod
    def _get_mount_row(conn: Any, mount_id: str) -> Any:
        return conn.execute(
            "SELECT * FROM openlist_mounts WHERE mount_id=?", (mount_id,)
        ).fetchone()

    @staticmethod
    def _serialize(row: Any, connection: Any | None = None) -> dict[str, Any]:
        try:
            native_locator = json.loads(row["native_locator_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            native_locator = {}
        result = {
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
            "storage_identity_id": row["storage_identity_id"],
            "identity_root": row["identity_root"],
            "probe_root": row["probe_root"],
            "native_locator": native_locator if isinstance(native_locator, dict) else {},
            "last_tested_at": row["last_tested_at"],
        }
        if connection is not None:
            revision_row = connection.execute(
                "SELECT config_json FROM openlist_mount_revisions "
                "WHERE mount_id=? AND revision=?",
                (str(row["mount_id"]), int(row["revision"] or 1)),
            ).fetchone()
            if revision_row is not None:
                try:
                    revision_config = json.loads(revision_row["config_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    revision_config = {}
                result["connection_revision"] = int(
                    revision_config.get("connection_revision") or 0
                ) or None
        return result

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
        storage_identity_id = str(payload.get("storage_identity_id") or "").strip()
        identity_root = normalize_openlist_path(payload.get("identity_root") or webdav_root)
        probe_root = normalize_openlist_path(payload.get("probe_root") or DEFAULT_PROBE_ROOT)
        native_locator = _native_locator(payload.get("native_locator"))
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            connection = conn.execute(
                "SELECT revision, enabled FROM openlist_connections WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
            if connection is None:
                raise OpenListValidationError(f"OpenList 连接不存在：{connection_id}")
            if not bool(connection["enabled"]):
                raise OpenListValidationError(f"OpenList 连接已停用：{connection_id}")
            if account_id:
                account = conn.execute(
                    "SELECT provider_kind FROM provider_accounts WHERE account_id=?", (account_id,)
                ).fetchone()
                if account is None:
                    raise OpenListValidationError(f"绑定的账号不存在：{account_id}")
                if provider_kind and provider_kind != account["provider_kind"]:
                    raise OpenListValidationError("绑定的 provider_kind 与账号类型不一致")
                provider_kind = account["provider_kind"]
                storage_identity_id = f"provider:{provider_kind}:account:{account_id}"
            if not storage_identity_id:
                raise OpenListValidationError("未绑定账号的 mount 必须配置 storage_identity_id")
            try:
                conn.execute(
                    """
                    INSERT INTO openlist_mounts
                      (mount_id, connection_id, name, provider_kind, account_id, webdav_root,
                       namespace_id, readable, writable, enabled, revision, identity_status, last_tested_at,
                       storage_identity_id, identity_root, probe_root, native_locator_json)
                    VALUES (?,?,?,?,?,?,?,0,0,?,1,'unverified',NULL,?,?,?,?)
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
                        storage_identity_id,
                        identity_root,
                        probe_root,
                        json.dumps(native_locator, ensure_ascii=False, sort_keys=True),
                    ),
                )
                created = self._get_mount_row(conn, mount_id)
                self._insert_revision_snapshot(
                    conn,
                    created,
                    connection_revision=int(connection["revision"] or 1),
                )
            except sqlite3.IntegrityError as exc:
                raise OpenListValidationError(f"挂载已存在或绑定冲突：{mount_id}") from exc
        return self.get_mount(mount_id)

    def update_mount(self, mount_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_mount_row(conn, mount_id)
            if row is None:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
            expected_revision = _optional_revision(
                payload.get("expected_revision", payload.get("revision")),
                "expected_revision",
            )
            if expected_revision is not None and expected_revision != int(row["revision"] or 0):
                conflict = OpenListValidationError(
                    f"mount revision conflict: expected {expected_revision}, actual {row['revision']}"
                )
                conflict.code = "MOUNT_CONFLICT"
                conflict.status = 409
                raise conflict
            requested_connection = str(payload.get("connection_id") or row["connection_id"]).strip()
            if requested_connection != str(row["connection_id"]):
                raise OpenListValidationError("mount 的 connection_id 创建后不可修改，请新建挂载")
            name = str(payload.get("name") if payload.get("name") is not None else row["name"] or "").strip() or row["name"]
            webdav_root = normalize_openlist_path(
                payload.get("webdav_root") if payload.get("webdav_root") is not None else row["webdav_root"]
            )
            enabled = _to_bool(payload.get("enabled"), bool(row["enabled"]))
            if "provider_kind" in payload or "account_id" in payload:
                provider_kind = str(payload.get("provider_kind") if payload.get("provider_kind") is not None else row["provider_kind"] or "").strip() or None
                account_id = str(payload.get("account_id") if payload.get("account_id") is not None else row["account_id"] or "").strip() or None
                self._validate_bind(conn, provider_kind, account_id)
                if account_id:
                    account_row = conn.execute(
                        "SELECT provider_kind FROM provider_accounts WHERE account_id=?",
                        (account_id,),
                    ).fetchone()
                    if account_row is None:
                        raise OpenListValidationError(f"绑定的账号不存在：{account_id}")
                    provider_kind = str(account_row["provider_kind"] or "")
            else:
                provider_kind = row["provider_kind"]
                account_id = row["account_id"]
            revision = int(row["revision"] or 1) + 1
            storage_identity_id = str(payload.get("storage_identity_id") or row["storage_identity_id"] or "").strip()
            if account_id:
                storage_identity_id = f"provider:{provider_kind}:account:{account_id}"
            if not storage_identity_id:
                raise OpenListValidationError("storage_identity_id 不能为空")
            identity_root = normalize_openlist_path(payload.get("identity_root") or row["identity_root"] or webdav_root)
            probe_root = normalize_openlist_path(payload.get("probe_root") or row["probe_root"] or DEFAULT_PROBE_ROOT)
            current_native_locator = json.loads(row["native_locator_json"] or "{}")
            native_locator = _native_locator(
                payload.get("native_locator") if "native_locator" in payload else current_native_locator
            )
            identity_changed = any(
                (
                    str(row["webdav_root"] or "") != webdav_root,
                    str(row["provider_kind"] or "") != str(provider_kind or ""),
                    str(row["account_id"] or "") != str(account_id or ""),
                    str(row["storage_identity_id"] or "") != storage_identity_id,
                    str(row["identity_root"] or "") != identity_root,
                    str(row["probe_root"] or "") != probe_root,
                    current_native_locator != native_locator,
                )
            )
            if identity_changed:
                identity_status = "unverified"
                readable = writable = 0
                last_tested_at = None
            else:
                identity_status = str(row["identity_status"] or "unverified")
                readable = int(bool(row["readable"]))
                writable = int(bool(row["writable"]))
                last_tested_at = row["last_tested_at"]
            conn.execute(
                """
                UPDATE openlist_mounts
                   SET name=?, webdav_root=?, provider_kind=?, account_id=?, enabled=?, revision=?,
                       storage_identity_id=?, identity_root=?, probe_root=?, identity_status=?,
                       readable=?, writable=?, last_tested_at=?, native_locator_json=?
                 WHERE mount_id=?
                """,
                (name, webdav_root, provider_kind, account_id, int(enabled), revision,
                 storage_identity_id, identity_root, probe_root,
                 identity_status, readable, writable, last_tested_at,
                 json.dumps(native_locator, ensure_ascii=False, sort_keys=True), mount_id),
            )
            updated = self._get_mount_row(conn, mount_id)
            self._insert_revision_snapshot(conn, updated)
        return self.get_mount(mount_id)

    def restore_mount_snapshot(
        self,
        mount_id: str,
        snapshot: dict[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Restore a trusted pre-change snapshot with optimistic locking.

        Aggregate configuration writes may revise/probe a mount before the
        storage target revision is persisted.  If that final write fails, a
        normal ``update_mount`` cannot compensate safely because it clears the
        probe and identity state.  This internal recovery operation restores
        the complete prior snapshot while still appending a new immutable
        revision and refusing to overwrite a concurrent administrator change.
        """

        snapshot = dict(snapshot or {})
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_mount_row(conn, mount_id)
            if row is None:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
            parsed_expected_revision = _optional_revision(expected_revision, "expected_revision")
            if parsed_expected_revision is None:
                raise OpenListValidationError("mount compensation 必须提供 expected_revision")
            if int(row["revision"] or 0) != parsed_expected_revision:
                conflict = OpenListValidationError(
                    "mount compensation revision conflict: "
                    f"expected {expected_revision}, actual {row['revision']}"
                )
                conflict.code = "MOUNT_CONFLICT"
                conflict.status = 409
                raise conflict

            connection_id = str(snapshot.get("connection_id") or "").strip()
            if connection_id != str(row["connection_id"] or ""):
                raise OpenListValidationError("不能把 mount 恢复到其他 OpenList 连接")
            connection_row = conn.execute(
                "SELECT revision, enabled FROM openlist_connections WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
            snapshot_connection_revision = int(snapshot.get("connection_revision") or 0)
            if connection_row is None:
                raise OpenListValidationError("恢复快照引用的 OpenList 连接不存在")
            if (
                snapshot_connection_revision <= 0
                and (
                    _to_bool(snapshot.get("readable"), False)
                    or _to_bool(snapshot.get("writable"), False)
                    or str(snapshot.get("identity_status") or "") in {"verified", "declared"}
                )
            ):
                raise OpenListValidationError("可用恢复快照缺少 connection_revision")
            if snapshot_connection_revision > 0 and int(connection_row["revision"] or 0) != snapshot_connection_revision:
                raise OpenListValidationError("恢复快照的 connection revision 已过期")
            if str(snapshot.get("namespace_id") or "") != str(row["namespace_id"] or ""):
                raise OpenListValidationError("不能修改 mount 的 namespace_id")

            provider_kind = str(snapshot.get("provider_kind") or "").strip() or None
            account_id = str(snapshot.get("account_id") or "").strip() or None
            self._validate_bind(conn, provider_kind, account_id)
            if account_id:
                account_row = conn.execute(
                    "SELECT provider_kind FROM provider_accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                if account_row is None:
                    raise OpenListValidationError(f"绑定的账号不存在：{account_id}")
                provider_kind = str(account_row["provider_kind"] or "")
            storage_identity_id = str(snapshot.get("storage_identity_id") or "").strip()
            if account_id:
                expected_identity = f"provider:{provider_kind}:account:{account_id}"
                if storage_identity_id != expected_identity:
                    raise OpenListValidationError("恢复快照中的存储身份与账号不一致")
            if not storage_identity_id:
                raise OpenListValidationError("恢复快照缺少 storage_identity_id")

            identity_status = str(snapshot.get("identity_status") or "unverified").strip()
            if identity_status not in {"verified", "declared", "mismatch", "unverified"}:
                raise OpenListValidationError("恢复快照中的 identity_status 无效")
            readable = _to_bool(snapshot.get("readable"), False)
            writable = _to_bool(snapshot.get("writable"), False)
            last_tested_at = str(snapshot.get("last_tested_at") or "").strip() or None
            if (readable or writable or identity_status in {"verified", "declared"}) and not last_tested_at:
                raise OpenListValidationError("可用 mount 的恢复快照缺少 last_tested_at")

            revision = int(row["revision"] or 1) + 1
            native_locator = _native_locator(snapshot.get("native_locator"))
            conn.execute(
                """
                UPDATE openlist_mounts
                   SET name=?, webdav_root=?, provider_kind=?, account_id=?, enabled=?, revision=?,
                       storage_identity_id=?, identity_root=?, probe_root=?, identity_status=?,
                       readable=?, writable=?, last_tested_at=?, native_locator_json=?
                 WHERE mount_id=?
                """,
                (
                    str(snapshot.get("name") or row["name"] or "").strip() or row["name"],
                    normalize_openlist_path(snapshot.get("webdav_root")),
                    provider_kind,
                    account_id,
                    int(_to_bool(snapshot.get("enabled"), True)),
                    revision,
                    storage_identity_id,
                    normalize_openlist_path(snapshot.get("identity_root")),
                    normalize_openlist_path(snapshot.get("probe_root") or DEFAULT_PROBE_ROOT),
                    identity_status,
                    int(readable),
                    int(writable),
                    last_tested_at,
                    json.dumps(native_locator, ensure_ascii=False, sort_keys=True),
                    mount_id,
                ),
            )
            restored = self._get_mount_row(conn, mount_id)
            self._insert_revision_snapshot(
                conn,
                restored,
                connection_revision=(snapshot_connection_revision or None),
            )
        result = self.get_mount(mount_id)
        if result is None:  # pragma: no cover - guarded by the transaction above
            raise OpenListNotFoundError(f"挂载恢复后无法读取：{mount_id}")
        return result

    @staticmethod
    def _connection_revision(conn: Any, connection_id: str) -> int:
        row = conn.execute(
            "SELECT revision FROM openlist_connections WHERE connection_id=?",
            (str(connection_id),),
        ).fetchone()
        if row is None:
            raise OpenListValidationError(f"OpenList 连接不存在：{connection_id}")
        return int(row["revision"] or 1)

    def _insert_revision_snapshot(
        self,
        conn: Any,
        row: Any,
        *,
        connection_revision: int | None = None,
        created_at: str | None = None,
    ) -> None:
        if row is None:
            raise OpenListValidationError("无法冻结不存在的 mount")
        selected_connection_revision = connection_revision or self._connection_revision(
            conn, str(row["connection_id"])
        )
        if connection_revision is not None:
            exists = conn.execute(
                "SELECT 1 FROM openlist_connection_revisions WHERE connection_id=? AND revision=?",
                (str(row["connection_id"]), int(selected_connection_revision)),
            ).fetchone()
            if exists is None:
                raise OpenListValidationError(
                    "mount 快照引用的 OpenList connection revision 不存在"
                )
        conn.execute(
            "INSERT INTO openlist_mount_revisions(mount_id, revision, config_json, created_at) "
            "VALUES (?,?,?,?)",
            (
                str(row["mount_id"]),
                int(row["revision"] or 1),
                _mount_revision_json(row, selected_connection_revision),
                created_at or utc_now_iso(),
            ),
        )

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
            conn.execute("BEGIN IMMEDIATE")
            if self._get_mount_row(conn, mount_id) is None:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
            for row in conn.execute("SELECT categories_json FROM storage_target_revisions").fetchall():
                if _json_contains_mount_id(row["categories_json"], mount_id):
                    raise OpenListValidationError("mount 已被存储目标修订引用，不能删除")
            plan_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_execution_plans'"
            ).fetchone()
            if plan_table is not None:
                for row in conn.execute("SELECT plan_json FROM import_execution_plans").fetchall():
                    if _json_contains_mount_id(row["plan_json"], mount_id):
                        raise OpenListValidationError("mount 已被执行计划引用，不能删除")
            execution_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_executions'"
            ).fetchone()
            if execution_table is not None:
                for row in conn.execute(
                    "SELECT snapshot_json, receipt_json, cleanup_json FROM transfer_executions"
                ).fetchall():
                    if any(
                        _json_contains_mount_id(value, mount_id)
                        for value in (row["snapshot_json"], row["receipt_json"], row["cleanup_json"])
                    ):
                        raise OpenListValidationError("mount 已被传输执行引用，不能删除")
            conn.execute("DELETE FROM openlist_mount_revisions WHERE mount_id=?", (mount_id,))
            cursor = conn.execute("DELETE FROM openlist_mounts WHERE mount_id=?", (mount_id,))
            if cursor.rowcount != 1:
                raise OpenListValidationError("mount 删除失败")
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

    def list_dirs_for_mount(
        self, mount_id: str, path: str = "", *, revision: int | None = None
    ) -> list[str]:
        client, mount = self.client_for_mount(mount_id, revision=revision)
        base = normalize_openlist_path(posixpath.join(mount["webdav_root"], str(path).lstrip("/")))
        return [str(item["path"]) for item in client.list_dir(base) if item.get("is_dir")]

    # ---- 完整读写检测（§7.3）----

    def probe_mount(
        self,
        mount_id: str,
        probe_root: str | None = None,
        *,
        expected_revision: int | None = None,
        expected_connection_revision: int | None = None,
        persist_result: bool = True,
    ) -> dict[str, Any]:
        with self.database.connect() as conn:
            row = self._get_mount_row(conn, mount_id)
            if row is None:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
            mount_revision = int(row["revision"] or 1)
            parsed_expected_revision = _optional_revision(expected_revision, "expected_revision")
            if parsed_expected_revision is not None and parsed_expected_revision != mount_revision:
                conflict = OpenListValidationError(
                    "mount 已被其他管理员修改，请刷新后重试"
                )
                conflict.code = "MOUNT_CONFLICT"
                conflict.status = 409
                raise conflict
        if self.connection_service is None:
            raise OpenListValidationError("缺少 connection_service，无法检测挂载")
        connection = self.connection_service.get_connection(row["connection_id"])
        connection_revision = int((connection or {}).get("revision") or 0)
        if connection is None or connection_revision <= 0 or not bool(connection.get("enabled", True)):
            raise OpenListValidationError("OpenList 连接不存在、已停用或缺少 revision")
        if (
            (parsed_expected_connection_revision := _optional_revision(
                expected_connection_revision, "expected_connection_revision"
            ))
            is not None
            and parsed_expected_connection_revision != connection_revision
        ):
            conflict = OpenListValidationError(
                "OpenList 连接已被其他管理员修改，请刷新后重试"
            )
            conflict.code = "OPENLIST_CONNECTION_CONFLICT"
            conflict.status = 409
            raise conflict
        try:
            credentials = self.connection_service.resolve_credentials(
                row["connection_id"], connection_revision
            )
        except TypeError as exc:  # pragma: no cover - stale composition root
            raise OpenListValidationError(
                "OpenList connection service 不支持冻结 revision 凭证解析"
            ) from exc
        client = self.webdav_factory(credentials)
        relative_probe = normalize_openlist_path(probe_root or row["probe_root"] or self.probe_root)
        root = normalize_openlist_path(posixpath.join(row["webdav_root"], relative_probe.lstrip("/")))
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
            return self._finish(
                row,
                checks,
                residue,
                readable=False,
                writable=False,
                connection_revision=connection_revision,
                persist_result=persist_result,
            )

        # 2) mount 根目录列举
        list_ok = False
        try:
            entries = client.list_dir(row["webdav_root"])
            list_ok = True
            checks.append({"name": "mount 根目录列举", "ok": True, "required": True, "message": f"读取到 {len(entries)} 个条目"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "mount 根目录列举", "ok": False, "required": True, "message": _short(exc)})

        # 3) 在探测目录创建任务级临时目录与小文件（绝不写 mount 根）
        probe_root_ok = True
        if list_ok:
            # WebDAV MKCOL 只建一层，探测根目录（例如 /_fnos_probe）缺失时
            # 直接建任务目录会失败，因此先确认/创建根目录本身。
            try:
                client.mkcol(root)
            except Exception as exc:  # noqa: BLE001
                probe_root_ok = False
                checks.append({"name": "探测根目录", "ok": False, "required": True, "message": f"探测根目录创建/确认失败：{_short(exc)}"})
        else:
            probe_root_ok = False

        created = False
        probe_started = False
        if list_ok and probe_root_ok:
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
            checks.append({"name": "临时目录与文件创建", "ok": False, "required": True, "message": "根目录列举或探测根目录确认失败，未执行写入探测"})

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
        return self._finish(
            row,
            checks,
            residue,
            readable=readable,
            writable=writable,
            connection_revision=connection_revision,
            persist_result=persist_result,
        )

    def _finish(
        self,
        row: Any,
        checks: list[dict[str, Any]],
        residue: list[str],
        *,
        readable: bool,
        writable: bool,
        connection_revision: int,
        persist_result: bool = True,
    ) -> dict[str, Any]:
        mount_id = row["mount_id"]
        now = utc_now_iso()
        with self.database.connect() as conn:
            if persist_result:
                conn.execute("BEGIN IMMEDIATE")
            current = self._get_mount_row(conn, mount_id)
            if current is None:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
            if int(current["revision"] or 0) != int(row["revision"] or 0):
                conflict = OpenListValidationError("mount 在检测期间已被修改，请重新检测")
                conflict.code = "MOUNT_CONFLICT"
                conflict.status = 409
                raise conflict
            connection_row = conn.execute(
                "SELECT revision, enabled FROM openlist_connections WHERE connection_id=?",
                (str(row["connection_id"]),),
            ).fetchone()
            if (
                connection_row is None
                or int(connection_row["revision"] or 0) != int(connection_revision)
                or not bool(connection_row["enabled"])
            ):
                conflict = OpenListValidationError(
                    "OpenList 连接在检测期间已被修改或停用，请重新检测"
                )
                conflict.code = "OPENLIST_CONNECTION_CONFLICT"
                conflict.status = 409
                raise conflict
            identity_status = str(current["identity_status"] or "unverified")
            health_changed = (
                bool(current["readable"]) != bool(readable)
                or bool(current["writable"]) != bool(writable)
            )
            if persist_result and health_changed:
                next_revision = int(current["revision"] or 1) + 1
                conn.execute(
                    """
                    UPDATE openlist_mounts
                       SET readable=?, writable=?, identity_status=?, last_tested_at=?, revision=?
                     WHERE mount_id=?
                    """,
                    (int(readable), int(writable), identity_status, now, next_revision, mount_id),
                )
                updated = self._get_mount_row(conn, mount_id)
                self._insert_revision_snapshot(
                    conn,
                    updated,
                    connection_revision=int(connection_revision),
                    created_at=now,
                )
            elif persist_result:
                # A probe timestamp is operational telemetry, not a new frozen
                # mount configuration.  Repeated healthy preflights must not
                # invalidate optimistic-lock revisions or grow snapshot history.
                conn.execute(
                    "UPDATE openlist_mounts SET last_tested_at=? WHERE mount_id=?",
                    (now, mount_id),
                )
                updated = self._get_mount_row(conn, mount_id)
            else:
                updated = current
        errors = [str(item.get("message") or item.get("name") or "检测失败") for item in checks if item.get("required") and not item.get("ok")]
        ok = not errors
        return {
            "success": ok,
            "ok": ok,
            "mount_id": mount_id,
            "connection_id": row["connection_id"],
            "connection_revision": int(connection_revision),
            "revision": int(updated["revision"] or 0),
            "readable": readable,
            "writable": writable,
            "identity_status": identity_status,
            "checks": checks,
            "errors": errors,
            "residue_warnings": residue,
            "message": "mount 读写检测通过" if ok else "mount 读写检测未通过",
        }

    def confirm_identity(
        self,
        mount_id: str,
        *,
        expected_revision: int | None = None,
        expected_connection_revision: int | None = None,
    ) -> dict[str, Any]:
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_mount_row(conn, mount_id)
            if row is None:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
            parsed_expected_revision = _optional_revision(expected_revision, "expected_revision")
            if parsed_expected_revision is not None and int(row["revision"] or 0) != parsed_expected_revision:
                conflict = OpenListValidationError(
                    f"mount revision conflict: expected {expected_revision}, actual {row['revision']}"
                )
                conflict.code = "MOUNT_CONFLICT"
                conflict.status = 409
                raise conflict
            if not row["account_id"] and not row["storage_identity_id"]:
                raise OpenListValidationError("mount 缺少可确认的存储身份")
            if not row["last_tested_at"] or not bool(row["readable"]):
                raise OpenListValidationError("mount 必须先通过读取检测，才能确认存储身份")
            snapshot_row = conn.execute(
                "SELECT config_json FROM openlist_mount_revisions "
                "WHERE mount_id=? AND revision=?",
                (mount_id, int(row["revision"] or 0)),
            ).fetchone()
            try:
                snapshot = json.loads(snapshot_row["config_json"] or "{}") if snapshot_row else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                snapshot = {}
            connection_revision = int(snapshot.get("connection_revision") or 0)
            current_connection = conn.execute(
                "SELECT revision, enabled FROM openlist_connections WHERE connection_id=?",
                (str(row["connection_id"]),),
            ).fetchone()
            if (
                connection_revision <= 0
                or current_connection is None
                or int(current_connection["revision"] or 0) != connection_revision
                or not bool(current_connection["enabled"])
            ):
                raise OpenListValidationError(
                    "OpenList 连接已变化或停用，请重新检测 mount"
                )
            if (
                (parsed_expected_connection_revision := _optional_revision(
                    expected_connection_revision, "expected_connection_revision"
                ))
                is not None
                and parsed_expected_connection_revision != connection_revision
            ):
                conflict = OpenListValidationError(
                    "connection revision conflict: expected "
                    f"{expected_connection_revision}, actual {connection_revision}"
                )
                conflict.code = "OPENLIST_CONNECTION_CONFLICT"
                conflict.status = 409
                raise conflict
            revision = int(row["revision"] or 1) + 1
            conn.execute(
                "UPDATE openlist_mounts SET identity_status='declared', revision=? WHERE mount_id=?",
                (revision, mount_id),
            )
            updated = self._get_mount_row(conn, mount_id)
            self._insert_revision_snapshot(conn, updated)
        return self.get_mount(mount_id)

    def client_for_mount(
        self, mount_id: str, *, revision: int | None = None
    ) -> tuple[Any, dict[str, Any]]:
        if revision is not None:
            mount = self.get_revision(mount_id, int(revision))
            if mount is None:
                raise OpenListNotFoundError(f"挂载修订不存在：{mount_id}@{revision}")
            if not bool(mount.get("enabled")) or str(mount.get("identity_status") or "") not in {
                "verified",
                "declared",
            }:
                raise OpenListValidationError("冻结 mount 未启用或身份未确认")
        else:
            mount = self.get_mount(mount_id)
            if mount is None:
                raise OpenListNotFoundError(f"挂载不存在：{mount_id}")
        if self.connection_service is None:
            raise OpenListValidationError("缺少 connection_service")
        connection_revision = int(mount.get("connection_revision") or 0)
        try:
            credentials = self.connection_service.resolve_credentials(
                mount["connection_id"], connection_revision or None
            )
        except TypeError as exc:  # pragma: no cover - stale composition root
            raise OpenListValidationError(
                "OpenList connection service 不支持冻结 revision 凭证解析"
            ) from exc
        if connection_revision and int(credentials.get("revision") or 0) != connection_revision:
            raise OpenListValidationError("OpenList connection 凭证 revision 不一致")
        return self.webdav_factory(credentials), mount

    def list_path(
        self, mount_id: str, path: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        client, mount = self.client_for_mount(mount_id, revision=revision)
        absolute = normalize_openlist_path(posixpath.join(mount["webdav_root"], str(path).lstrip("/")))
        if not client.exists(absolute):
            return {"exists": False, "file_count": 0, "total_size": 0}
        files = self._walk_files(client, absolute)
        return {
            "exists": True,
            "file_count": len(files),
            "total_size": sum(int(item.get("size") or 0) for item in files),
        }

    @staticmethod
    def _walk_files(client: Any, root: str, *, max_entries: int = 100_000) -> list[dict[str, Any]]:
        """Return a recursive file manifest without trusting direct-child counts."""

        pending = [normalize_openlist_path(root)]
        visited: set[str] = set()
        files: list[dict[str, Any]] = []
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            try:
                entries = client.list_dir(current)
            except TypeError:
                # Test doubles and older clients may not accept optional
                # WebDAV depth arguments; the default remains depth=1.
                entries = client.list_dir(current)
            for item in entries or []:
                if not isinstance(item, dict):
                    continue
                item_path = normalize_openlist_path(item.get("path") or "")
                if not item_path or item_path == current:
                    # PROPFIND depth=1 often includes the directory itself.
                    if item_path == current and not item.get("is_dir"):
                        files.append(item)
                    continue
                if item.get("is_dir"):
                    pending.append(item_path)
                else:
                    files.append(item)
                if len(files) + len(pending) > max_entries:
                    raise OpenListValidationError("OpenList 目录清单超过安全上限")
        return files

    def move_path(
        self,
        mount_id: str,
        source: str,
        destination: str,
        *,
        revision: int | None = None,
    ) -> dict[str, Any]:
        client, mount = self.client_for_mount(mount_id, revision=revision)
        root = str(mount["webdav_root"])
        src = normalize_openlist_path(posixpath.join(root, str(source).lstrip("/")))
        dst = normalize_openlist_path(posixpath.join(root, str(destination).lstrip("/")))
        client.move(src, dst)
        return {"status": "done", "source": src, "destination": dst}


def _mount_revision_json(row: Any, connection_revision: int) -> str:
    try:
        native_locator = json.loads(row["native_locator_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        native_locator = {}
    return json.dumps(
        {
            "name": str(row["name"] or ""),
            "connection_id": str(row["connection_id"] or ""),
            "connection_revision": int(connection_revision or 0),
            "provider_kind": str(row["provider_kind"] or "") or None,
            "account_id": str(row["account_id"] or "") or None,
            "webdav_root": str(row["webdav_root"] or "/"),
            "namespace_id": str(row["namespace_id"] or ""),
            "readable": bool(row["readable"]),
            "writable": bool(row["writable"]),
            "enabled": bool(row["enabled"]),
            "identity_status": str(row["identity_status"] or "unverified"),
            "last_tested_at": str(row["last_tested_at"] or "") or None,
            "storage_identity_id": str(row["storage_identity_id"] or ""),
            "identity_root": str(row["identity_root"] or "/"),
            "probe_root": str(row["probe_root"] or DEFAULT_PROBE_ROOT),
            "native_locator": native_locator if isinstance(native_locator, dict) else {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _native_locator(value: Any) -> dict[str, str]:
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise OpenListValidationError("native_locator 必须是对象")
    kind = str(value.get("kind") or value.get("locator_kind") or "").strip()
    locator_value = str(value.get("value") or "").strip()
    if not kind or not locator_value:
        raise OpenListValidationError("native_locator 必须包含 kind 和 value")
    return {"kind": kind, "value": locator_value}


def _json_contains_mount_id(raw: Any, mount_id: str) -> bool:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False

    def contains(candidate: Any) -> bool:
        if isinstance(candidate, dict):
            if str(candidate.get("mount_id") or "") == mount_id:
                return True
            return any(contains(item) for item in candidate.values())
        if isinstance(candidate, list):
            return any(contains(item) for item in candidate)
        return False

    return contains(value)


__all__ = ["OpenListMountService", "DEFAULT_PROBE_ROOT", "WebDAVFactory"]
