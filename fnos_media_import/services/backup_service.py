"""全量配置备份：导出/导入整套系统设置，供新系统导入即用。

导出文档覆盖：
- app_settings 全部键（排除 ``admin.profile``——管理员凭据不允许随文件迁移）
- 网盘账号：provider_accounts / provider_account_revisions / selections
- OpenList 连接与挂载
- 存储目标：storage_targets 及其修订、账号绑定、绑定修订
- 来源分配规则：source_destination_assignments

凭证以密文原样传输（base64 + credential_key_id）。导入前会用本机
``FNOS_ROOT_KEY`` 试解每条密文，解不开立即报错，避免两台机器根密钥
不一致时账号到运行时才失效。导入为整库替换语义：单事务清空并重建上述
表与设置键；运行时重载失败时回滚到导入前快照。
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from ..accounts.crypto import MasterKeyError, decrypt
from ..time_utils import utc_now_iso

logger = logging.getLogger("fnos_media_import.backup")

FULL_BACKUP_FORMAT = "fnos-media-import/full-backup"
FULL_BACKUP_VERSION = 1

# 管理员登录凭据留在本机，永远不进备份文件。
EXCLUDED_SETTING_KEYS = frozenset({"admin.profile"})

_ENVELOPE_FIELDS = frozenset(
    {"format", "version", "exported_at", "source", "app_settings"}
)


@dataclass(frozen=True)
class _TableSpec:
    """导出/导入的列映射：JSON 列转对象、密文列转 base64。"""

    # friendly 名称 -> 原始 JSON 文本列
    json_columns: dict[str, str] = field(default_factory=dict)
    # friendly 名称 -> 原始 BLOB 密文列
    blob_columns: dict[str, str] = field(default_factory=dict)


_TABLE_SPECS: dict[str, _TableSpec] = {
    "provider_accounts": _TableSpec(),
    "provider_account_revisions": _TableSpec(
        json_columns={"config": "config_json"},
        blob_columns={"credentials_ciphertext": "credentials_ciphertext"},
    ),
    "provider_account_selections": _TableSpec(),
    "openlist_connections": _TableSpec(
        blob_columns={"password_ciphertext": "password_ciphertext"},
    ),
    "openlist_mounts": _TableSpec(
        json_columns={"native_locator": "native_locator_json"},
    ),
    "storage_targets": _TableSpec(),
    "storage_target_revisions": _TableSpec(
        json_columns={"categories": "categories_json", "display": "display_json"},
    ),
    "storage_target_account_bindings": _TableSpec(),
    "storage_target_binding_revisions": _TableSpec(
        json_columns={"locator": "locator_json"},
    ),
    "source_destination_assignments": _TableSpec(),
}

# 父表先插、子表先删。
_INSERT_ORDER = (
    "provider_accounts",
    "provider_account_revisions",
    "provider_account_selections",
    "openlist_connections",
    "openlist_mounts",
    "storage_targets",
    "storage_target_revisions",
    "storage_target_account_bindings",
    "storage_target_binding_revisions",
    "source_destination_assignments",
)

# 导入时要清空的全部表（含纯历史表，避免外键残留），子表在前。
_DELETE_ORDER = (
    "source_destination_assignments",
    "storage_target_binding_revisions",
    "storage_target_account_bindings",
    "storage_target_revisions",
    "storage_targets",
    "openlist_mount_revisions",
    "openlist_mounts",
    "openlist_connection_revisions",
    "openlist_connections",
    "provider_account_selection_revisions",
    "provider_account_selections",
    "provider_account_revisions",
    "provider_accounts",
)

_CREDENTIAL_TABLES = {
    "provider_account_revisions": "credentials_ciphertext",
    "openlist_connections": "password_ciphertext",
}


class BackupDocumentError(ValueError):
    """备份文档格式或内容不合法。"""


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row["name"]) for row in rows]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _export_row(row: sqlite3.Row, spec: _TableSpec) -> dict[str, Any]:
    raw_json = {raw: friendly for friendly, raw in spec.json_columns.items()}
    raw_blob = {raw: friendly for friendly, raw in spec.blob_columns.items()}
    item: dict[str, Any] = {}
    for column, value in dict(row).items():
        if column in raw_json:
            item[raw_json[column]] = _parse_json(value)
        elif column in raw_blob:
            item[raw_blob[column]] = base64.b64encode(bytes(value or b"")).decode("ascii")
        else:
            item[column] = value
    return item


def _parse_json(value: Any) -> Any:
    if value is None or value == "":
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _encode_setting(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class FullBackupService:
    def __init__(
        self,
        *,
        db: Any,
        master_key: Callable[[], bytes],
        reload_runtime: Callable[[], dict[str, Any]],
    ) -> None:
        self._db = db
        self._master_key = master_key
        self._reload_runtime = reload_runtime

    # ---- export ----

    def export_document(self) -> tuple[dict[str, Any], int]:
        settings = self._db.app_settings.get_all()
        exported_settings = {
            key: copy.deepcopy(value)
            for key, value in settings.items()
            if key not in EXCLUDED_SETTING_KEYS
        }
        with self._db.connect() as conn:
            tables = {table: self._read_table(conn, table) for table in _INSERT_ORDER}
        document = {
            "format": FULL_BACKUP_FORMAT,
            "version": FULL_BACKUP_VERSION,
            "exported_at": utc_now_iso(),
            "source": {
                "application": "fnos-media-import",
                "sensitive": True,
                "credentials": "密文原样导出；导入机器必须配置相同的 FNOS_ROOT_KEY",
            },
            "app_settings": exported_settings,
            **tables,
        }
        return {
            "success": True,
            "document": document,
            "meta": {
                "settings_keys": sorted(exported_settings.keys()),
                "table_counts": {table: len(rows) for table, rows in tables.items()},
                "contains_secrets": True,
            },
        }, 200

    def _read_table(self, conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        spec = _TABLE_SPECS[table]
        return [
            _export_row(row, spec)
            for row in conn.execute(f"SELECT * FROM {table}").fetchall()
        ]

    # ---- import ----

    def import_document(
        self,
        document: Any,
        *,
        confirm: bool = False,
    ) -> tuple[dict[str, Any], int]:
        if not confirm:
            return {"success": False, "message": "请先确认覆盖导入全量配置"}, 400
        try:
            tables, settings = self._validate_document(document)
        except BackupDocumentError as exc:
            return {"success": False, "message": str(exc)}, 400

        verify_error = self._verify_credentials(tables)
        if verify_error:
            return {"success": False, "message": verify_error}, 400

        try:
            with self._db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                snapshot = {
                    table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                    for table in _INSERT_ORDER
                }
                settings_snapshot = {
                    str(row["key"]): self._db.app_settings._decode_value(row["value"])
                    for row in conn.execute("SELECT key, value FROM app_settings")
                }
                self._wipe(conn)
                self._insert_tables(conn, tables)
                self._apply_settings(conn, settings)
        except sqlite3.IntegrityError as exc:
            logger.exception("full backup import rejected by database constraints")
            return {
                "success": False,
                "message": f"备份文件与当前数据库结构冲突，导入已中止：{exc}",
            }, 400
        except sqlite3.Error as exc:
            logger.exception("full backup import failed")
            return {
                "success": False,
                "message": f"全量配置导入失败：{exc}",
            }, 500

        try:
            reload_result = self._reload_runtime()
        except Exception as exc:  # noqa: BLE001
            logger.exception("full backup runtime reload failed; restoring snapshot")
            rollback_ok = self._restore_snapshot(snapshot, settings_snapshot)
            return {
                "success": False,
                "message": (
                    "全量配置导入后运行时重载失败，已回滚到导入前状态"
                    if rollback_ok
                    else "全量配置导入后运行时重载失败，且回滚失败，请检查服务日志"
                )
                + f"；{exc}",
                "rolled_back": rollback_ok,
                "runtime_restored": rollback_ok,
            }, int(getattr(exc, "status_code", 500) or 500)

        logger.info(
            "full backup import completed: tables=%s settings_keys=%s",
            {table: len(rows) for table, rows in tables.items()},
            len(settings),
        )
        return {
            "success": True,
            "message": "全量配置已导入并重载运行时服务",
            "reload": reload_result,
            "meta": {
                "table_counts": {table: len(rows) for table, rows in tables.items()},
                "settings_keys": sorted(settings.keys()),
            },
        }, 200

    def _validate_document(
        self, document: Any
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        if not isinstance(document, dict):
            raise BackupDocumentError("备份文件必须是 JSON 对象")
        if document.get("format") != FULL_BACKUP_FORMAT:
            raise BackupDocumentError(
                f"备份文件格式标识不受支持，仅支持 {FULL_BACKUP_FORMAT}"
            )
        version = document.get("version")
        if isinstance(version, bool):
            raise BackupDocumentError("备份文件版本不正确")
        try:
            version_number = int(version)
        except (TypeError, ValueError) as exc:
            raise BackupDocumentError("备份文件版本不正确") from exc
        if version_number != FULL_BACKUP_VERSION:
            raise BackupDocumentError(
                f"备份文件版本 {version_number} 不受支持，当前仅支持版本 {FULL_BACKUP_VERSION}"
            )
        unknown = sorted(set(document) - _ENVELOPE_FIELDS - set(_TABLE_SPECS))
        if unknown:
            preview = "、".join(unknown[:8])
            raise BackupDocumentError(f"备份文件包含不支持的字段：{preview}")

        settings = document.get("app_settings")
        if settings is None:
            settings = {}
        if not isinstance(settings, dict) or any(
            not isinstance(key, str) for key in settings
        ):
            raise BackupDocumentError("备份文件中的 app_settings 必须是对象")
        if EXCLUDED_SETTING_KEYS.intersection(settings):
            raise BackupDocumentError(
                "备份文件不能包含管理员账户设置；管理员登录信息需在新系统单独设置"
            )

        tables: dict[str, list[dict[str, Any]]] = {}
        with self._db.connect() as conn:
            for table in _INSERT_ORDER:
                rows = document.get(table)
                if rows is None:
                    tables[table] = []
                    continue
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) for row in rows
                ):
                    raise BackupDocumentError(f"备份文件中的 {table} 必须是对象数组")
                allowed = self._allowed_keys(conn, table)
                for row in rows:
                    unknown_keys = sorted(set(row) - allowed)
                    if unknown_keys:
                        preview = "、".join(unknown_keys[:8])
                        raise BackupDocumentError(
                            f"{table} 包含不支持的字段：{preview}"
                        )
                tables[table] = rows
        return tables, settings

    def _allowed_keys(self, conn: sqlite3.Connection, table: str) -> set[str]:
        columns = set(_table_columns(conn, table)) if _table_exists(conn, table) else set()
        spec = _TABLE_SPECS[table]
        return columns | set(spec.json_columns) | set(spec.blob_columns)

    def _verify_credentials(self, tables: dict[str, list[dict[str, Any]]]) -> str | None:
        """用本机根密钥试解全部密文；不一致时给出明确错误而不是运行时失效。"""

        pending = [
            (table, field, row)
            for table, field in _CREDENTIAL_TABLES.items()
            for row in tables.get(table, [])
            if str(row.get(field) or "").strip()
        ]
        if not pending:
            return None
        try:
            master_key = self._master_key()
        except MasterKeyError as exc:
            return f"导入机器未配置可用的根密钥，无法校验凭证密文：{exc}"
        for table, field, row in pending:
            key_id = str(row.get("credential_key_id") or "").strip()
            try:
                ciphertext = base64.b64decode(
                    str(row.get(field) or ""), validate=True
                )
                decrypt(master_key, key_id, ciphertext)
            except (binascii.Error, ValueError):
                return f"{table} 的密文 base64 编码不正确，请使用导出的原文件导入"
            except MasterKeyError as exc:
                return (
                    f"备份文件凭证无法用本机 FNOS_ROOT_KEY 解密（{exc}）。"
                    "请确认新旧系统使用相同的根密钥后重试"
                )
        return None

    def _wipe(self, conn: sqlite3.Connection) -> None:
        for table in _DELETE_ORDER:
            if _table_exists(conn, table):
                conn.execute(f"DELETE FROM {table}")
        # 登录会话是临时数据；置空引用以解除外键，与账号删除路径行为一致。
        if _table_exists(conn, "provider_auth_sessions"):
            conn.execute(
                "UPDATE provider_auth_sessions SET account_id = NULL "
                "WHERE account_id IS NOT NULL"
            )

    def _insert_tables(
        self, conn: sqlite3.Connection, tables: dict[str, list[dict[str, Any]]]
    ) -> None:
        for table in _INSERT_ORDER:
            spec = _TABLE_SPECS[table]
            columns = set(_table_columns(conn, table))
            for row in tables.get(table, []):
                values = self._row_to_columns(row, spec, columns)
                names = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                conn.execute(
                    f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                    tuple(values.values()),
                )

    def _row_to_columns(
        self,
        row: dict[str, Any],
        spec: _TableSpec,
        columns: set[str],
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, value in row.items():
            if key in spec.json_columns:
                raw_column = spec.json_columns[key]
                if raw_column in columns:
                    values[raw_column] = (
                        None
                        if value is None
                        else (value if isinstance(value, str) else _encode_setting(value))
                    )
            elif key in spec.blob_columns:
                raw_column = spec.blob_columns[key]
                if raw_column in columns:
                    values[raw_column] = base64.b64decode(str(value or ""))
            elif key in columns:
                values[key] = value
        return values

    def _apply_settings(self, conn: sqlite3.Connection, settings: dict[str, Any]) -> None:
        now = utc_now_iso()
        conn.execute("DELETE FROM app_settings WHERE key NOT IN ('admin.profile')")
        for key, value in settings.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value = excluded.value, updated_at = excluded.updated_at
                """,
                (str(key), _encode_setting(value), now),
            )

    def _restore_snapshot(
        self,
        snapshot: dict[str, list[dict[str, Any]]],
        settings_snapshot: dict[str, Any],
    ) -> bool:
        try:
            with self._db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._wipe(conn)
                for table in _INSERT_ORDER:
                    for row in snapshot.get(table, []):
                        names = ", ".join(row)
                        placeholders = ", ".join("?" for _ in row)
                        conn.execute(
                            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                            tuple(row.values()),
                        )
                now = utc_now_iso()
                # 先清掉导入写入、快照里没有的键，再完整恢复快照。
                placeholders = ", ".join("?" for _ in settings_snapshot)
                conn.execute(
                    "DELETE FROM app_settings WHERE key NOT IN ('admin.profile')"
                    + (f" AND key NOT IN ({placeholders})" if settings_snapshot else ""),
                    tuple(settings_snapshot),
                )
                for key, value in settings_snapshot.items():
                    conn.execute(
                        """
                        INSERT INTO app_settings (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                          value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (str(key), _encode_setting(value), now),
                    )
        except Exception:  # noqa: BLE001
            logger.exception("full backup rollback failed")
            return False
        return True
