"""OpenList 连接服务：CRUD + 凭证 AES-GCM 信封加密 + 认证探测。

表结构（迁移 22，``openlist_connections``）见 ``fnos_media_import/database.py``。
密码以 ``nonce(12) + tag(16) + ciphertext`` 二进制信封入库，``credential_key_id``
固定为 ``ol:v1``。主密钥来自 ``FNOS_ACCOUNT_MASTER_KEY``（hex，16/24/32 字节）。
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import urllib.parse
from typing import Any, Callable

from ..time_utils import utc_now_iso
from . import (
    OpenListCredentialError,
    OpenListError,
    OpenListMasterKeyError,
    OpenListNotFoundError,
    OpenListValidationError,
)
from .webdav_client import OpenListWebDAVClient

CONNECTION_KEY_ID = "ol:v1"
_CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

ProbeRunner = Callable[[dict[str, Any]], dict[str, Any]]
MasterKeyProvider = Callable[[], str]


def env_master_key_provider(env_name: str = "FNOS_ACCOUNT_MASTER_KEY") -> MasterKeyProvider:
    """返回读取指定环境变量的主密钥提供者。"""

    def provider() -> str:
        return str(os.environ.get(env_name, "") or "").strip()

    return provider


def _master_key_bytes(master_key_provider: MasterKeyProvider) -> bytes:
    hex_value = str(master_key_provider() or "").strip()
    if not hex_value:
        raise OpenListMasterKeyError("未配置 FNOS_ACCOUNT_MASTER_KEY，无法加密保存 OpenList 连接密码")
    try:
        key = bytes.fromhex(hex_value)
    except ValueError as exc:
        raise OpenListMasterKeyError("FNOS_ACCOUNT_MASTER_KEY 不是合法的 hex 字符串") from exc
    if len(key) not in (16, 24, 32):
        raise OpenListMasterKeyError("FNOS_ACCOUNT_MASTER_KEY 解码后长度必须是 16/24/32 字节（AES 密钥）")
    return key


def _encrypt_password(password: str, master_key_provider: MasterKeyProvider) -> bytes:
    from Crypto.Cipher import AES

    key = _master_key_bytes(master_key_provider)
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(password.encode("utf-8"))
    return nonce + tag + ciphertext


def _decrypt_password(ciphertext: bytes, key_id: str, master_key_provider: MasterKeyProvider) -> str:
    from Crypto.Cipher import AES

    if str(key_id or "") != CONNECTION_KEY_ID:
        raise OpenListCredentialError(f"OpenList 连接密码使用未知密钥版本：{key_id or '空'}")
    raw = bytes(ciphertext or b"")
    if len(raw) < 12 + 16:
        raise OpenListCredentialError("OpenList 连接密码密文不完整")
    key = _master_key_bytes(master_key_provider)
    nonce, tag, body = raw[:12], raw[12:28], raw[28:]
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(body, tag).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - GCM 校验失败统一报主密钥可能变更
        raise OpenListCredentialError("OpenList 连接密码无法解密，主密钥可能已变更") from exc


def _validate_base_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise OpenListValidationError("base_url 不能为空")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise OpenListValidationError("base_url 只允许 http/https")
    if parsed.username or parsed.password:
        raise OpenListValidationError("base_url 不允许内嵌用户名/密码")
    if parsed.query or parsed.fragment:
        raise OpenListValidationError("base_url 不允许包含查询参数或锚点")
    if not parsed.hostname:
        raise OpenListValidationError("base_url 缺少主机名")
    if " " in text:
        raise OpenListValidationError("base_url 不允许包含空白字符")
    return text.rstrip("/")


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


class OpenListConnectionService:
    """OpenList WebDAV 连接配置、认证与健康检测（§16.2 / §7.2）。"""

    def __init__(
        self,
        database: Any,
        master_key_provider: MasterKeyProvider,
        probe_runner: ProbeRunner | None = None,
    ) -> None:
        self.database = database
        self.master_key_provider = master_key_provider
        self.probe_runner = probe_runner or self._default_probe_runner

    # ---- 查询 / 序列化 ----

    def list_connections(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute("SELECT * FROM openlist_connections ORDER BY name").fetchall()
            return [self._serialize(row) for row in rows]

    def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = self._get_row(conn, connection_id)
            return self._serialize(row) if row is not None else None

    @staticmethod
    def _get_row(conn: Any, connection_id: str) -> Any:
        return conn.execute(
            "SELECT * FROM openlist_connections WHERE connection_id=?", (connection_id,)
        ).fetchone()

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        return {
            "connection_id": row["connection_id"],
            "name": row["name"],
            "base_url": row["base_url"],
            "username": row["username"],
            "credential_configured": bool(row["password_ciphertext"]),
            "enabled": bool(row["enabled"]),
            "revision": int(row["revision"] or 1),
            "last_test_status": row["last_test_status"],
            "last_tested_at": row["last_tested_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---- 写操作 ----

    def create_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        connection_id = str(payload.get("connection_id") or "").strip()
        if not connection_id:
            raise OpenListValidationError("connection_id 不能为空")
        if not _CONNECTION_ID_RE.fullmatch(connection_id):
            raise OpenListValidationError("connection_id 只能包含字母/数字/_.-")
        name = str(payload.get("name") or "").strip() or connection_id
        base_url = _validate_base_url(payload.get("base_url"))
        username = str(payload.get("username") or "").strip()
        if not username:
            raise OpenListValidationError("username 不能为空")
        password = str(payload.get("password") or "")
        if not password:
            raise OpenListValidationError("password 不能为空")
        enabled = _to_bool(payload.get("enabled"), True)
        ciphertext = _encrypt_password(password, self.master_key_provider)
        now = utc_now_iso()
        with self.database.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO openlist_connections
                      (connection_id, name, base_url, username, password_ciphertext, credential_key_id,
                       enabled, revision, last_test_status, last_tested_at, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,1,'untested',NULL,?,?)
                    """,
                    (connection_id, name, base_url, username, ciphertext, CONNECTION_KEY_ID, int(enabled), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise OpenListValidationError(f"OpenList 连接已存在：{connection_id}") from exc
        return self.get_connection(connection_id)

    def update_connection(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        with self.database.connect() as conn:
            row = self._get_row(conn, connection_id)
            if row is None:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
            name = str(payload.get("name") if payload.get("name") is not None else row["name"] or "").strip() or row["name"]
            base_url = _validate_base_url(payload.get("base_url", row["base_url"]))
            username = str(payload.get("username") if payload.get("username") is not None else row["username"] or "").strip()
            if not username:
                raise OpenListValidationError("username 不能为空")
            enabled = _to_bool(payload.get("enabled"), bool(row["enabled"]))
            password_ciphertext = bytes(row["password_ciphertext"] or b"")
            credential_key_id = str(row["credential_key_id"] or "")
            if payload.get("password") is not None:
                password = str(payload.get("password") or "")
                if password:
                    password_ciphertext = _encrypt_password(password, self.master_key_provider)
                    credential_key_id = CONNECTION_KEY_ID
            revision = int(row["revision"] or 1) + 1
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE openlist_connections
                   SET name=?, base_url=?, username=?, password_ciphertext=?, credential_key_id=?,
                       enabled=?, revision=?, updated_at=?
                 WHERE connection_id=?
                """,
                (name, base_url, username, password_ciphertext, credential_key_id, int(enabled), revision, now, connection_id),
            )
        return self.get_connection(connection_id)

    def delete_connection(self, connection_id: str) -> dict[str, Any]:
        with self.database.connect() as conn:
            row = self._get_row(conn, connection_id)
            if row is None:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
            try:
                cursor = conn.execute("DELETE FROM openlist_connections WHERE connection_id=?", (connection_id,))
            except sqlite3.IntegrityError as exc:
                raise OpenListValidationError("连接下仍存在挂载，请先删除挂载") from exc
            if cursor.rowcount == 0:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
        return {"connection_id": connection_id, "deleted": True}

    # ---- 凭证解析（供 mount 服务使用）----

    def resolve_credentials(self, connection_id: str) -> dict[str, Any]:
        """返回解密后的 WebDAV 凭证；API 永不直接暴露该结果。"""
        with self.database.connect() as conn:
            row = self._get_row(conn, connection_id)
            if row is None:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
            password = _decrypt_password(
                row["password_ciphertext"], row["credential_key_id"], self.master_key_provider
            )
        return {
            "connection_id": connection_id,
            "base_url": row["base_url"],
            "username": row["username"],
            "password": password,
        }

    # ---- 检测 ----

    def test_connection(self, connection_id: str) -> dict[str, Any]:
        """执行 WebDAV 认证探测并更新 last_test_status / last_tested_at。"""
        with self.database.connect() as conn:
            row = self._get_row(conn, connection_id)
            if row is None:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
            password = _decrypt_password(
                row["password_ciphertext"], row["credential_key_id"], self.master_key_provider
            )
        probe_context = {
            "connection_id": connection_id,
            "base_url": row["base_url"],
            "username": row["username"],
            "password": password,
        }
        result = self.probe_runner(probe_context) or {}
        ok = bool(result.get("ok"))
        status = "ok" if ok else "failed"
        now = utc_now_iso()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE openlist_connections SET last_test_status=?, last_tested_at=? WHERE connection_id=?",
                (status, now, connection_id),
            )
        return {
            "connection_id": connection_id,
            "success": ok,
            "ok": ok,
            "last_test_status": status,
            "last_tested_at": now,
            "message": str(result.get("message") or ("连接检测通过" if ok else "连接检测未通过")),
        }

    @staticmethod
    def _default_probe_runner(probe_context: dict[str, Any]) -> dict[str, Any]:
        client = OpenListWebDAVClient(
            base_url=probe_context["base_url"],
            username=probe_context["username"],
            password=probe_context["password"],
        )
        try:
            client.auth_probe()
            return {"ok": True, "message": "WebDAV 认证与服务版本探测通过"}
        except OpenListError as exc:
            return {"ok": False, "message": str(exc)[:300]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)[:300]}


__all__ = [
    "OpenListConnectionService",
    "CONNECTION_KEY_ID",
    "env_master_key_provider",
    "ProbeRunner",
    "MasterKeyProvider",
]
