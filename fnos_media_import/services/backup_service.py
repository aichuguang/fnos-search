"""全量配置备份：导出/导入整套系统设置，供新系统导入即用。

导出文档覆盖：
- app_settings 全部键（排除 ``admin.profile``——管理员凭据不允许随文件迁移）
- 网盘账号、凭证修订、当前选择及不可变选择修订
- OpenList 连接、挂载及其不可变修订
- 存储目标：storage_targets 及其修订、账号绑定、绑定修订
- 来源分配规则：source_destination_assignments

凭证以密文原样传输（base64 + credential_key_id）。导入前会用本机
``FNOS_ROOT_KEY`` 试解每条密文，解不开立即报错，避免两台机器根密钥
不一致时账号到运行时才失效。导入为整库替换语义：单事务清空并重建上述
表与设置键；运行时重载失败时回滚到导入前快照并重新加载运行时。
旧版 v1 文档遗漏不可变修订，不能安全恢复，必须从原系统重新导出 v2。
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

from ..accounts.auth_sessions import expire_waiting_auth_sessions
from ..accounts.crypto import PURPOSE_OPENLIST, PURPOSE_PROVIDER_ACCOUNT, MasterKeyError, decrypt
from ..repositories.update_repository import UpdateRepository
from ..time_utils import utc_now_iso

logger = logging.getLogger("fnos_media_import.backup")

FULL_BACKUP_FORMAT = "fnos-media-import/full-backup"
FULL_BACKUP_VERSION = 2

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
    "provider_account_selection_revisions": _TableSpec(),
    "openlist_connections": _TableSpec(
        blob_columns={"password_ciphertext": "password_ciphertext"},
    ),
    "openlist_connection_revisions": _TableSpec(
        json_columns={"config": "config_json"},
        blob_columns={"password_ciphertext": "password_ciphertext"},
    ),
    "openlist_mounts": _TableSpec(
        json_columns={"native_locator": "native_locator_json"},
    ),
    "openlist_mount_revisions": _TableSpec(
        json_columns={"config": "config_json"},
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
    "provider_account_selection_revisions",
    "openlist_connections",
    "openlist_connection_revisions",
    "openlist_mounts",
    "openlist_mount_revisions",
    "storage_targets",
    "storage_target_revisions",
    "storage_target_account_bindings",
    "storage_target_binding_revisions",
    "source_destination_assignments",
)

# 清空、导出、重建与回滚必须覆盖完全相同的表，不能把历史修订当作可丢弃数据。
_DELETE_ORDER = tuple(reversed(_INSERT_ORDER))

_CREDENTIAL_TABLES = {
    "provider_account_revisions": ("credentials_ciphertext", PURPOSE_PROVIDER_ACCOUNT),
    "openlist_connections": ("password_ciphertext", PURPOSE_OPENLIST),
    "openlist_connection_revisions": ("password_ciphertext", PURPOSE_OPENLIST),
}


class BackupDocumentError(ValueError):
    """备份文档格式或内容不合法。"""


def _configuration_restore_blockers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    # Abandoned login windows may never call get()/poll() again. Apply the
    # same safe expiry before deciding they are still active configuration work.
    expire_waiting_auth_sessions(conn)
    blockers = UpdateRepository._history_cleanup_blockers(conn)
    # Unlike history cleanup, config replacement also changes the accounts
    # that a previously frozen authorization result is about to update.
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count, MIN(session_id) AS first_id "
        "FROM provider_auth_sessions "
        "WHERE lower(trim(COALESCE(status, ''))) NOT IN ('completed', 'failed', 'cancelled') "
        "GROUP BY status"
    )
    blockers.extend({"table": "provider_auth_sessions", "label": "网盘登录授权", **dict(row)} for row in rows)
    return blockers


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
        with self._db.connect() as conn:
            # 同一读快照保证并发账号/挂载更新不会导出互相不匹配的当前行和修订。
            conn.execute("BEGIN")
            exported_settings = {
                str(row["key"]): self._db.app_settings._decode_value(row["value"])
                for row in conn.execute("SELECT key, value FROM app_settings")
                if str(row["key"]) not in EXCLUDED_SETTING_KEYS
            }
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
                # Use the same cross-process quiescence check as history cleanup,
                # under the write reservation that excludes new claims/enqueues.
                blockers = _configuration_restore_blockers(conn)
                if blockers:
                    return {
                        "success": False,
                        "error_code": "CONFIG_RESTORE_BUSY",
                        "message": "仍有排队、执行、待恢复任务或网盘登录授权，不能覆盖全量配置；请等待任务安全结束并完成或取消登录授权后重试",
                        "blockers": blockers,
                    }, 409
                snapshot = {
                    table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                    for table in _INSERT_ORDER
                }
                settings_snapshot = {
                    str(row["key"]): self._db.app_settings._decode_value(row["value"])
                    for row in conn.execute("SELECT key, value FROM app_settings")
                    if str(row["key"]) not in EXCLUDED_SETTING_KEYS
                }
                auth_session_references = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT session_id, account_id, row_version FROM provider_auth_sessions "
                        "WHERE account_id IS NOT NULL"
                    )
                ]
                self._wipe(conn)
                self._insert_tables(conn, tables)
                self._validate_revision_integrity(conn)
                self._apply_settings(conn, settings)
        except BackupDocumentError as exc:
            return {"success": False, "message": str(exc)}, 400
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
            reload_result = self._reload_runtime_checked()
        except Exception as exc:  # noqa: BLE001
            logger.exception("full backup runtime reload failed; restoring snapshot")
            rollback_ok = self._restore_snapshot(snapshot, settings_snapshot, auth_session_references)
            runtime_restored = False
            if rollback_ok:
                try:
                    self._reload_runtime_checked()
                    runtime_restored = True
                except Exception:  # noqa: BLE001
                    logger.exception("full backup rollback runtime reload failed")
            if runtime_restored:
                message = "全量配置导入后运行时重载失败，已恢复导入前的配置和运行时"
            elif rollback_ok:
                message = "全量配置导入后运行时重载失败，数据库已回滚，但运行时恢复失败，请检查日志并重启服务"
            else:
                message = "全量配置导入后运行时重载失败，且数据库回滚失败，请检查服务日志"
            return {
                "success": False,
                "message": message + f"；{exc}",
                "rolled_back": rollback_ok,
                "runtime_restored": runtime_restored,
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

    def _reload_runtime_checked(self) -> dict[str, Any]:
        result = self._reload_runtime()
        if not isinstance(result, dict) or result.get("success") is False:
            message = result.get("message") if isinstance(result, dict) else ""
            raise RuntimeError(str(message or "运行时重载未成功"))
        return result

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
        if not isinstance(version, int) or isinstance(version, bool):
            raise BackupDocumentError("备份文件版本必须是整数")
        if version == 1:
            raise BackupDocumentError(
                "旧版 v1 备份缺少账号选择、OpenList 连接和挂载的不可变修订，不能安全导入；"
                "请在原系统升级后重新导出 v2 备份，或从完整 SQLite 备份离线恢复。当前配置未修改"
            )
        if version != FULL_BACKUP_VERSION:
            raise BackupDocumentError(
                f"备份文件版本 {version} 不受支持，当前仅支持版本 {FULL_BACKUP_VERSION}"
            )
        unknown = sorted(set(document) - _ENVELOPE_FIELDS - set(_TABLE_SPECS))
        if unknown:
            preview = "、".join(unknown[:8])
            raise BackupDocumentError(f"备份文件包含不支持的字段：{preview}")

        settings = document.get("app_settings")
        if not isinstance(settings, dict) or any(
            not isinstance(key, str) for key in settings
        ):
            raise BackupDocumentError("备份文件中的 app_settings 必须是对象")
        if EXCLUDED_SETTING_KEYS.intersection(settings):
            raise BackupDocumentError(
                "备份文件不能包含管理员账户设置；管理员登录信息需在新系统单独设置"
            )
        try:
            json.dumps(settings, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise BackupDocumentError("备份文件中的 app_settings 不是有效 JSON") from exc

        tables: dict[str, list[dict[str, Any]]] = {}
        with self._db.connect() as conn:
            for table in _INSERT_ORDER:
                if table not in document:
                    raise BackupDocumentError(f"备份文件缺少 {table}，禁止把缺失配置或修订当作空表导入")
                rows = document.get(table)
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) for row in rows
                ):
                    raise BackupDocumentError(f"备份文件中的 {table} 必须是对象数组")
                columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
                spec = _TABLE_SPECS[table]
                aliases = {raw: friendly for friendly, raw in spec.json_columns.items()}
                allowed = {aliases.get(str(column["name"]), str(column["name"])) for column in columns}
                required = {
                    aliases.get(str(column["name"]), str(column["name"]))
                    for column in columns
                    if column["pk"] or (column["notnull"] and column["dflt_value"] is None)
                }
                for row in rows:
                    if any(not isinstance(key, str) for key in row):
                        raise BackupDocumentError(f"{table} 的字段名必须是字符串")
                    unknown_keys = sorted(set(row) - allowed)
                    if unknown_keys:
                        preview = "、".join(unknown_keys[:8])
                        raise BackupDocumentError(
                            f"{table} 包含不支持的字段：{preview}"
                        )
                    missing = sorted(key for key in required if row.get(key) is None)
                    if missing:
                        raise BackupDocumentError(f"{table} 缺少必填字段：{'、'.join(missing)}")
                    for key in spec.json_columns:
                        if key not in row:
                            continue
                        if not isinstance(row[key], dict):
                            raise BackupDocumentError(f"{table}.{key} 必须是 JSON 对象")
                    for key in spec.blob_columns:
                        if key in row and not isinstance(row[key], str):
                            raise BackupDocumentError(f"{table}.{key} 必须是 base64 字符串")
                    for key, value in row.items():
                        if key not in spec.json_columns and isinstance(value, (dict, list)):
                            raise BackupDocumentError(f"{table}.{key} 必须是标量")
                        if value is not None and key in {
                            "revision", "current_revision", "target_revision", "account_revision", "selection_revision"
                        }:
                            minimum = 0 if table == "provider_accounts" and key == "current_revision" else 1
                            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                                raise BackupDocumentError(f"{table}.{key} 必须是大于等于 {minimum} 的整数")
                    try:
                        json.dumps(row, allow_nan=False)
                    except (TypeError, ValueError) as exc:
                        raise BackupDocumentError(f"{table} 包含无效 JSON 值") from exc
                tables[table] = copy.deepcopy(rows)
        return tables, copy.deepcopy(settings)

    @staticmethod
    def _validate_revision_integrity(conn: sqlite3.Connection) -> None:
        # 当前修订号没有全部由 SQL 外键约束；只插入空历史表仍会让 V1 计划失效。
        for table, id_column, revision_column, history in (
            ("provider_accounts", "account_id", "current_revision", "provider_account_revisions"),
            ("openlist_connections", "connection_id", "revision", "openlist_connection_revisions"),
            ("openlist_mounts", "mount_id", "revision", "openlist_mount_revisions"),
            ("storage_targets", "destination_id", "current_revision", "storage_target_revisions"),
            ("storage_target_account_bindings", "binding_id", "current_revision", "storage_target_binding_revisions"),
        ):
            missing = conn.execute(
                f"SELECT c.{id_column}, c.{revision_column} FROM {table} c "
                f"LEFT JOIN {history} h ON h.{id_column}=c.{id_column} AND h.revision=c.{revision_column} "
                f"WHERE c.{revision_column}>0 AND h.{id_column} IS NULL LIMIT 1"
            ).fetchone()
            if missing is not None:
                raise BackupDocumentError(f"{table} 引用的不可变修订缺失：{missing[0]}@{missing[1]}，导入已中止")

        invalid_selection = conn.execute(
            "SELECT s.provider_kind, s.scope, s.selection_revision "
            "FROM provider_account_selections s "
            "LEFT JOIN provider_account_selection_revisions h "
            "ON h.provider_kind=s.provider_kind AND h.scope=s.scope AND h.selection_revision=s.selection_revision "
            "AND h.account_id=s.account_id AND h.account_revision=s.account_revision "
            "WHERE h.provider_kind IS NULL LIMIT 1"
        ).fetchone()
        if invalid_selection is not None:
            raise BackupDocumentError("当前账号选择与不可变选择修订缺失或不匹配，导入已中止")
        invalid_provider = conn.execute(
            "SELECT h.account_id FROM provider_account_selection_revisions h "
            "JOIN provider_accounts a ON a.account_id=h.account_id "
            "WHERE a.provider_kind<>h.provider_kind LIMIT 1"
        ).fetchone()
        if invalid_provider is not None:
            raise BackupDocumentError("账号选择修订的 Provider 与账号不匹配，导入已中止")
        invalid_binding = conn.execute(
            "SELECT b.binding_id FROM storage_target_account_bindings b "
            "LEFT JOIN storage_target_revisions r "
            "ON r.destination_id=b.destination_id AND r.revision=b.target_revision "
            "WHERE r.destination_id IS NULL LIMIT 1"
        ).fetchone()
        if invalid_binding is not None:
            raise BackupDocumentError("存储目标绑定引用的目标修订缺失，导入已中止")
        # account_revision 后补入历史表，没有 SQL 外键保护，不能仅检查 binding 自身存在。
        invalid_binding_account = conn.execute(
            "SELECT h.binding_id FROM storage_target_binding_revisions h "
            "JOIN storage_target_account_bindings b ON b.binding_id=h.binding_id "
            "LEFT JOIN provider_account_revisions a "
            "ON a.account_id=b.account_id AND a.revision=h.account_revision "
            "WHERE (b.account_id IS NOT NULL AND a.account_id IS NULL) "
            "OR (b.account_id IS NULL AND h.account_revision IS NOT NULL) LIMIT 1"
        ).fetchone()
        if invalid_binding_account is not None:
            raise BackupDocumentError("存储目标绑定修订引用的账号修订缺失或不匹配，导入已中止")

        connection_revisions = set()
        for row in conn.execute("SELECT * FROM openlist_connection_revisions"):
            config = json.loads(row["config_json"])
            if not str(config.get("base_url") or "").strip() or not str(config.get("username") or "").strip():
                raise BackupDocumentError("OpenList 连接修订缺少 base_url 或 username，导入已中止")
            connection_revisions.add((str(row["connection_id"]), int(row["revision"])))
        for row in conn.execute(
            "SELECT h.config_json, h.revision AS history_revision, "
            "m.connection_id, m.namespace_id, m.revision AS current_revision, "
            "c.revision AS current_connection_revision "
            "FROM openlist_mount_revisions h "
            "JOIN openlist_mounts m ON m.mount_id=h.mount_id "
            "JOIN openlist_connections c ON c.connection_id=m.connection_id"
        ):
            config = json.loads(row["config_json"])
            connection_id = str(config.get("connection_id") or "")
            revision = config.get("connection_revision")
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or (connection_id, revision) not in connection_revisions
            ):
                raise BackupDocumentError("OpenList 挂载修订引用的连接修订缺失或无效，导入已中止")
            if not str(config.get("namespace_id") or "").strip() or not str(config.get("webdav_root") or "").strip():
                raise BackupDocumentError("OpenList 挂载修订缺少 namespace_id 或 webdav_root，导入已中止")
            if connection_id != row["connection_id"] or config["namespace_id"] != row["namespace_id"]:
                raise BackupDocumentError("OpenList 挂载修订的连接或命名空间与挂载不匹配，导入已中止")
            if row["history_revision"] == row["current_revision"] and revision != row["current_connection_revision"]:
                raise BackupDocumentError("OpenList 当前挂载修订引用了过期的连接修订，导入已中止")

    def _verify_credentials(self, tables: dict[str, list[dict[str, Any]]]) -> str | None:
        """用本机根密钥试解全部密文；不一致时给出明确错误而不是运行时失效。"""

        for table, (field, _purpose) in _CREDENTIAL_TABLES.items():
            if any(not str(row.get(field) or "").strip() for row in tables.get(table, [])):
                return f"{table} 缺少有效凭证密文，请使用完整导出的备份文件"
        pending = [
            (table, field, purpose, row)
            for table, (field, purpose) in _CREDENTIAL_TABLES.items()
            for row in tables.get(table, [])
            if str(row.get(field) or "").strip()
        ]
        if not pending:
            return None
        try:
            master_key = self._master_key()
        except MasterKeyError as exc:
            return f"导入机器未配置可用的根密钥，无法校验凭证密文：{exc}"
        for table, field, purpose, row in pending:
            key_id = str(row.get("credential_key_id") or "").strip()
            try:
                ciphertext = base64.b64decode(
                    str(row.get(field) or ""), validate=True
                )
                decrypt(master_key, key_id, ciphertext, purpose=purpose)
            except (binascii.Error, ValueError):
                return f"{table} 的密文 base64 编码不正确，请使用导出的原文件导入"
            except MasterKeyError as exc:
                return (
                    f"备份文件凭证无法用本机 FNOS_ROOT_KEY 解密（{exc}）。"
                    "请确认新旧系统使用相同的根密钥后重试"
                )
        return None

    def _wipe(self, conn: sqlite3.Connection) -> None:
        # 必须先解除会话外键，再删除账号；成功导入保留会话但不沿用旧账号关联。
        if _table_exists(conn, "provider_auth_sessions"):
            conn.execute(
                "UPDATE provider_auth_sessions SET account_id = NULL "
                "WHERE account_id IS NOT NULL"
            )
        for table in _DELETE_ORDER:
            if _table_exists(conn, table):
                conn.execute(f"DELETE FROM {table}")

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
                    values[raw_column] = base64.b64decode(str(value or ""), validate=True)
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
        auth_session_references: list[dict[str, Any]],
    ) -> bool:
        try:
            with self._db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if _configuration_restore_blockers(conn):
                    # A worker may have claimed new work after import committed.
                    # Do not replace the revisions that work is now using.
                    logger.error("full backup rollback refused: new work is active")
                    return False
                self._wipe(conn)
                for table in _INSERT_ORDER:
                    for row in snapshot.get(table, []):
                        names = ", ".join(row)
                        placeholders = ", ".join("?" for _ in row)
                        conn.execute(
                            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                            tuple(row.values()),
                        )
                for reference in auth_session_references:
                    # 只恢复未被并发登录流程更新的既有会话，不覆盖新会话状态。
                    conn.execute(
                        "UPDATE provider_auth_sessions SET account_id=? "
                        "WHERE session_id=? AND account_id IS NULL AND row_version=?",
                        (reference["account_id"], reference["session_id"], reference["row_version"]),
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
