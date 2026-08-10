"""OpenList connection CRUD, encrypted credentials, and health probes.

Passwords use the OpenList subkey derived from ``FNOS_ROOT_KEY``.  Only
``v2:openlist`` envelopes are accepted; older credential rows must be
re-entered.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import urllib.parse
from typing import Any, Callable

from ..accounts.crypto import PURPOSE_OPENLIST, ROOT_KEY_ENV, decrypt, encrypt
from ..time_utils import utc_now_iso
from . import (
    OpenListCredentialError,
    OpenListError,
    OpenListMasterKeyError,
    OpenListNotFoundError,
    OpenListValidationError,
)
from .webdav_client import OpenListWebDAVClient

CONNECTION_KEY_ID = f"v2:{PURPOSE_OPENLIST}"
_CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# The managed rclone worker reads one mutable WebDAV remote.  Operational
# connection edits must therefore wait until every automatically executable
# job using that connection has quiesced.  Review/terminal rows are excluded:
# they are protected separately by the frozen-vs-current runtime identity
# check when somebody explicitly retries them later.
_PAUSED_OR_TERMINAL_JOB_STATUSES = frozenset(
    {
        "review",
        "manual_review",
        "done",
        "success",
        "failed",
        "unsupported",
        "cancelled",
        "canceled",
        "rejected",
        "skipped",
        "skipped_existing",
    }
)
_PAUSED_OR_TERMINAL_TRANSFER_PHASES = frozenset(
    {"manual_review", "completed", "failed", "cancelled", "canceled"}
)
_ACTIVE_RCLONE_RUN_STATUSES = frozenset(
    {"queued", "starting", "running", "recovering", "cancel_pending"}
)
_ACTIVE_ORGANIZER_TASK_STATUSES = frozenset(
    {
        "pending",
        "waiting_openlist",
        "stabilizing",
        "scanning",
        "auto_approved",
        "executing",
    }
)

ProbeRunner = Callable[[dict[str, Any]], dict[str, Any]]
MasterKeyProvider = Callable[[], bytes | str]


def env_master_key_provider(env_name: str = ROOT_KEY_ENV) -> MasterKeyProvider:
    """Return a provider that reads the single supported root-key variable."""

    if env_name != ROOT_KEY_ENV:
        raise ValueError(f"OpenList credentials require {ROOT_KEY_ENV}")

    def provider() -> str:
        return str(os.environ.get(ROOT_KEY_ENV) or "").strip()

    return provider


def _master_key_bytes(master_key_provider: MasterKeyProvider) -> bytes:
    raw_value = master_key_provider()
    if isinstance(raw_value, (bytes, bytearray)):
        key = bytes(raw_value)
        if len(key) != 32:
            raise OpenListMasterKeyError("FNOS_ROOT_KEY 必须解码为 32 字节根密钥")
        return key
    hex_value = str(raw_value or "").strip()
    if not hex_value:
        raise OpenListMasterKeyError("未配置 FNOS_ROOT_KEY，无法加密保存 OpenList 连接密码")
    try:
        key = bytes.fromhex(hex_value)
    except ValueError as exc:
        raise OpenListMasterKeyError("FNOS_ROOT_KEY 不是合法的 hex 字符串") from exc
    if len(key) != 32:
        raise OpenListMasterKeyError("FNOS_ROOT_KEY 解码后必须是 32 字节根密钥")
    return key


def _encrypt_password(password: str, master_key_provider: MasterKeyProvider) -> tuple[bytes, str]:
    return encrypt(
        _master_key_bytes(master_key_provider),
        password,
        purpose=PURPOSE_OPENLIST,
    )


def _decrypt_password(ciphertext: bytes, key_id: str, master_key_provider: MasterKeyProvider) -> str:
    try:
        return decrypt(
            _master_key_bytes(master_key_provider),
            str(key_id or ""),
            bytes(ciphertext or b""),
            purpose=PURPOSE_OPENLIST,
        ).decode("utf-8")
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


def _optional_revision(value: Any, field_name: str = "expected_revision") -> int | None:
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
        credential_configured = bool(row["password_ciphertext"]) and str(
            row["credential_key_id"] or ""
        ) == CONNECTION_KEY_ID
        return {
            "connection_id": row["connection_id"],
            "name": row["name"],
            "base_url": row["base_url"],
            "username": row["username"],
            "credential_configured": credential_configured,
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
        ciphertext, credential_key_id = _encrypt_password(password, self.master_key_provider)
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
                    (connection_id, name, base_url, username, ciphertext, credential_key_id, int(enabled), now, now),
                )
                conn.execute(
                    "INSERT INTO openlist_connection_revisions "
                    "(connection_id, revision, config_json, password_ciphertext, credential_key_id, created_at) "
                    "VALUES (?,1,?,?,?,?)",
                    (
                        connection_id,
                        json.dumps(
                            {"name": name, "base_url": base_url, "username": username, "enabled": enabled},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        ciphertext,
                        credential_key_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OpenListValidationError(f"OpenList 连接已存在：{connection_id}") from exc
        return self.get_connection(connection_id)

    def update_connection(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_row(conn, connection_id)
            if row is None:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
            expected_revision = _optional_revision(
                payload.get("expected_revision", payload.get("revision"))
            )
            if (
                expected_revision is not None
                and expected_revision != int(row["revision"] or 0)
            ):
                conflict = OpenListValidationError(
                    "OpenList 连接已被其他管理员修改，请刷新后重试"
                )
                conflict.code = "OPENLIST_CONNECTION_CONFLICT"
                conflict.status = 409
                raise conflict
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
                    current_password = _decrypt_password(
                        password_ciphertext,
                        credential_key_id,
                        self.master_key_provider,
                    )
                    if not hmac.compare_digest(
                        password.encode("utf-8"),
                        current_password.encode("utf-8"),
                    ):
                        password_ciphertext, credential_key_id = _encrypt_password(
                            password, self.master_key_provider
                        )
            connection_identity_changed = any(
                (
                    str(row["base_url"] or "") != base_url,
                    str(row["username"] or "") != username,
                    password_ciphertext != bytes(row["password_ciphertext"] or b""),
                    bool(row["enabled"]) != bool(enabled),
                )
            )
            if connection_identity_changed:
                self._assert_runtime_mutation_safe(conn, connection_id)
            revision = int(row["revision"] or 1) + 1
            now = utc_now_iso()
            next_test_status = (
                "untested" if connection_identity_changed else str(row["last_test_status"] or "untested")
            )
            next_tested_at = None if connection_identity_changed else row["last_tested_at"]
            conn.execute(
                """
                UPDATE openlist_connections
                   SET name=?, base_url=?, username=?, password_ciphertext=?, credential_key_id=?,
                       enabled=?, revision=?, last_test_status=?, last_tested_at=?, updated_at=?
                 WHERE connection_id=?
                """,
                (
                    name,
                    base_url,
                    username,
                    password_ciphertext,
                    credential_key_id,
                    int(enabled),
                    revision,
                    next_test_status,
                    next_tested_at,
                    now,
                    connection_id,
                ),
            )
            # Every connection revision must be represented by the current
            # mount revision.  Operational changes invalidate health/identity;
            # display-only changes (for example the connection name) preserve
            # them but still advance the mount snapshot so current objects do
            # not reference an older connection revision.
            mount_rows = conn.execute(
                "SELECT * FROM openlist_mounts WHERE connection_id=?",
                (connection_id,),
            ).fetchall()
            for mount_row in mount_rows:
                mount_revision = int(mount_row["revision"] or 1) + 1
                if connection_identity_changed:
                    conn.execute(
                        """
                        UPDATE openlist_mounts
                           SET revision=?, identity_status='unverified', readable=0,
                               writable=0, last_tested_at=NULL
                         WHERE mount_id=?
                        """,
                        (mount_revision, mount_row["mount_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE openlist_mounts SET revision=? WHERE mount_id=?",
                        (mount_revision, mount_row["mount_id"]),
                    )
                updated_mount = conn.execute(
                    "SELECT * FROM openlist_mounts WHERE mount_id=?",
                    (mount_row["mount_id"],),
                ).fetchone()
                conn.execute(
                    "INSERT INTO openlist_mount_revisions(mount_id, revision, config_json, created_at) "
                    "VALUES (?,?,?,?)",
                    (
                        mount_row["mount_id"],
                        mount_revision,
                        _mount_snapshot_json(updated_mount, revision),
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO openlist_connection_revisions "
                "(connection_id, revision, config_json, password_ciphertext, credential_key_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    connection_id,
                    revision,
                    json.dumps(
                        {"name": name, "base_url": base_url, "username": username, "enabled": enabled},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    password_ciphertext,
                    credential_key_id,
                    now,
                ),
            )
        return self.get_connection(connection_id)

    def get_revision(self, connection_id: str, revision: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM openlist_connection_revisions WHERE connection_id=? AND revision=?",
                (connection_id, int(revision)),
            ).fetchone()
        if row is None:
            return None
        config = json.loads(row["config_json"] or "{}")
        return {
            "connection_id": connection_id,
            "revision": int(revision),
            **config,
            "credential_configured": bool(row["password_ciphertext"])
            and str(row["credential_key_id"] or "") == CONNECTION_KEY_ID,
        }

    def runtime_identity_matches(self, connection_id: str, revision: int) -> bool:
        """Return whether a frozen revision still matches the managed remote.

        Display-only revisions intentionally compare equal.  URL, username,
        enabled state and encrypted credential identity must all remain exact;
        otherwise a historical rclone plan must fail closed instead of using
        the single current Remote with stale paths.
        """

        try:
            selected_revision = int(revision)
        except (TypeError, ValueError):
            return False
        if selected_revision <= 0:
            return False
        with self.database.connect() as conn:
            current = self._get_row(conn, connection_id)
            frozen = conn.execute(
                "SELECT * FROM openlist_connection_revisions "
                "WHERE connection_id=? AND revision=?",
                (connection_id, selected_revision),
            ).fetchone()
        if current is None or frozen is None:
            return False
        try:
            config = json.loads(frozen["config_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return bool(
            str(current["base_url"] or "") == str(config.get("base_url") or "")
            and str(current["username"] or "") == str(config.get("username") or "")
            and bool(current["enabled"]) == bool(config.get("enabled", True))
            and bytes(current["password_ciphertext"] or b"")
            == bytes(frozen["password_ciphertext"] or b"")
            and str(current["credential_key_id"] or "")
            == str(frozen["credential_key_id"] or "")
        )

    def _assert_runtime_mutation_safe(self, conn: Any, connection_id: str) -> None:
        """Reject an operational edit while the shared Remote is in use."""

        referenced_job_ids: set[int] = set()
        if _table_exists(conn, "import_execution_plans"):
            rows = conn.execute(
                """
                SELECT p.job_id, p.plan_json, COALESCE(j.status, '') AS job_status
                  FROM import_execution_plans p
                  LEFT JOIN import_jobs j ON j.id=p.job_id
                """
            ).fetchall()
            for plan_row in rows:
                if not _json_contains_connection_id(plan_row["plan_json"], connection_id):
                    continue
                job_id = int(plan_row["job_id"] or 0)
                if job_id > 0:
                    referenced_job_ids.add(job_id)
                job_status = str(plan_row["job_status"] or "").strip().lower()
                if job_status not in _PAUSED_OR_TERMINAL_JOB_STATUSES:
                    self._raise_runtime_in_use(connection_id, f"入库任务 #{job_id}")

        if _table_exists(conn, "transfer_executions"):
            rows = conn.execute(
                "SELECT job_id, phase, snapshot_json, receipt_json, cleanup_json "
                "FROM transfer_executions"
            ).fetchall()
            for execution in rows:
                job_id = int(execution["job_id"] or 0)
                references_connection = job_id in referenced_job_ids or any(
                    _json_contains_connection_id(execution[key], connection_id)
                    for key in ("snapshot_json", "receipt_json", "cleanup_json")
                )
                if not references_connection:
                    continue
                referenced_job_ids.add(job_id)
                phase = str(execution["phase"] or "").strip().lower()
                if phase not in _PAUSED_OR_TERMINAL_TRANSFER_PHASES:
                    self._raise_runtime_in_use(
                        connection_id,
                        f"传输任务 #{job_id}（{phase or 'unknown'}）",
                    )

        if _table_exists(conn, "rclone_runs"):
            active_statuses = tuple(sorted(_ACTIVE_RCLONE_RUN_STATUSES))
            placeholders = ",".join("?" for _ in active_statuses)
            rows = conn.execute(
                "SELECT id, job_id, status FROM rclone_runs "
                f"WHERE lower(status) IN ({placeholders})",
                active_statuses,
            ).fetchall()
            for run in rows:
                job_id = int(run["job_id"] or 0)
                # Legacy/unscoped runs also use the one managed Remote, so an
                # identity edit is unsafe even without a plan reference.
                if job_id <= 0 or job_id in referenced_job_ids:
                    self._raise_runtime_in_use(
                        connection_id,
                        f"rclone 运行 #{int(run['id'] or 0)}",
                    )

        active_organizer_ids: set[int] = set()
        if referenced_job_ids and _table_exists(conn, "organizer_tasks"):
            placeholders = ",".join("?" for _ in referenced_job_ids)
            rows = conn.execute(
                f"SELECT id, job_id, status FROM organizer_tasks WHERE job_id IN ({placeholders})",
                tuple(sorted(referenced_job_ids)),
            ).fetchall()
            for task in rows:
                status = str(task["status"] or "").strip().lower()
                if status in _ACTIVE_ORGANIZER_TASK_STATUSES:
                    task_id = int(task["id"] or 0)
                    active_organizer_ids.add(task_id)
                    self._raise_runtime_in_use(
                        connection_id,
                        f"Organizer 任务 #{task_id}（{status}）",
                    )

        if _table_exists(conn, "worker_tasks"):
            rows = conn.execute(
                "SELECT id, task_type, payload FROM worker_tasks "
                "WHERE status IN ('pending','running')"
            ).fetchall()
            for worker in rows:
                payload = _json_object(worker["payload"])
                job_id = _safe_positive_int(payload.get("job_id"))
                organizer_task_id = _safe_positive_int(payload.get("task_id"))
                if job_id in referenced_job_ids or organizer_task_id in active_organizer_ids:
                    self._raise_runtime_in_use(
                        connection_id,
                        f"Worker 任务 #{int(worker['id'] or 0)}",
                    )

    @staticmethod
    def _raise_runtime_in_use(connection_id: str, reference: str) -> None:
        conflict = OpenListValidationError(
            f"OpenList 连接 {connection_id} 正在被待执行或运行中的入库任务使用，"
            "请等待任务结束或取消后再修改 URL、用户名、密码或启用状态"
            f"（{reference}）"
        )
        conflict.code = "OPENLIST_CONNECTION_IN_USE"
        conflict.status = 409
        conflict.retryable = True
        raise conflict

    def delete_connection(self, connection_id: str) -> dict[str, Any]:
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_row(conn, connection_id)
            if row is None:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
            if conn.execute(
                "SELECT 1 FROM openlist_mounts WHERE connection_id=? LIMIT 1", (connection_id,)
            ).fetchone():
                raise OpenListValidationError("连接下仍存在挂载，请先删除挂载")
            # A plan freezes the connection revision independently of the
            # current mount row.  Do not remove the revision history while an
            # in-flight plan may still need to decrypt/use it.  This check is
            # deliberately structural rather than a brittle SQL LIKE so a
            # provider or locator cannot spoof an unrelated connection ID.
            plan_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_execution_plans'"
            ).fetchone()
            if plan_table is not None:
                for plan_row in conn.execute("SELECT plan_json FROM import_execution_plans").fetchall():
                    if _json_contains_connection_id(plan_row["plan_json"], connection_id):
                        raise OpenListValidationError("连接仍被执行计划引用，不能删除")
            execution_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_executions'"
            ).fetchone()
            if execution_table is not None:
                for execution_row in conn.execute(
                    "SELECT snapshot_json, receipt_json, cleanup_json FROM transfer_executions"
                ).fetchall():
                    if any(
                        _json_contains_connection_id(value, connection_id)
                        for value in (
                            execution_row["snapshot_json"],
                            execution_row["receipt_json"],
                            execution_row["cleanup_json"],
                        )
                    ):
                        raise OpenListValidationError("连接仍被传输执行引用，不能删除")
            conn.execute(
                "DELETE FROM openlist_connection_revisions WHERE connection_id=?", (connection_id,)
            )
            try:
                cursor = conn.execute("DELETE FROM openlist_connections WHERE connection_id=?", (connection_id,))
            except sqlite3.IntegrityError as exc:
                raise OpenListValidationError("连接下仍存在挂载，请先删除挂载") from exc
            if cursor.rowcount == 0:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
        return {"connection_id": connection_id, "deleted": True}

    # ---- 凭证解析（供 mount 服务使用）----

    def resolve_credentials(self, connection_id: str, revision: int | None = None) -> dict[str, Any]:
        """Return decrypted WebDAV credentials for the current/frozen revision.

        ``revision`` is required by durable execution code when a plan has
        frozen an OpenList connection.  Omitting it preserves the admin/setup
        behaviour of resolving the current row; execution callers must pass
        the revision from their immutable plan to avoid a connection edit
        changing an in-flight transfer.
        """
        with self.database.connect() as conn:
            current = self._get_row(conn, connection_id)
            if current is None:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
            if revision is None:
                row = current
                selected_revision = int(current["revision"] or 1)
                config = {
                    "base_url": row["base_url"],
                    "username": row["username"],
                    "enabled": bool(row["enabled"]),
                }
                ciphertext = row["password_ciphertext"]
                key_id = row["credential_key_id"]
            else:
                selected_revision = int(revision)
                if selected_revision <= 0:
                    raise OpenListValidationError("connection revision 必须为正整数")
                row = conn.execute(
                    "SELECT * FROM openlist_connection_revisions WHERE connection_id=? AND revision=?",
                    (connection_id, selected_revision),
                ).fetchone()
                if row is None:
                    raise OpenListNotFoundError(
                        f"OpenList 连接修订不存在：{connection_id}@{selected_revision}"
                    )
                config = json.loads(row["config_json"] or "{}")
                ciphertext = row["password_ciphertext"]
                key_id = row["credential_key_id"]
            password = _decrypt_password(
                ciphertext, key_id, self.master_key_provider
            )
        return {
            "connection_id": connection_id,
            "revision": selected_revision,
            "base_url": config.get("base_url") or "",
            "username": config.get("username") or "",
            "enabled": bool(config.get("enabled", True)),
            "password": password,
        }

    # ---- 检测 ----

    def test_connection(
        self,
        connection_id: str,
        *,
        expected_revision: int | None = None,
        persist_result: bool = True,
    ) -> dict[str, Any]:
        # Aggregate preflight passes persist_result=False so the probe remains
        # CAS-checked without mutating connection health telemetry.
        """执行 WebDAV 认证探测并更新健康状态。

        探测本身在数据库事务之外执行，因而必须把读取时的 connection
        revision 带到最后一次 ``UPDATE``。否则管理员在探测期间修改密码或
        URL 时，旧凭证的探测结果可能覆盖新配置，后台会错误显示“连接正常”。
        ``expected_revision`` 同时给管理 API 提供显式 CAS，避免旧页面发起
        的检测请求影响最新配置。
        """
        with self.database.connect() as conn:
            row = self._get_row(conn, connection_id)
            if row is None:
                raise OpenListNotFoundError(f"OpenList 连接不存在：{connection_id}")
            revision = int(row["revision"] or 1)
            parsed_expected_revision = _optional_revision(expected_revision)
            if parsed_expected_revision is not None and parsed_expected_revision != revision:
                conflict = OpenListValidationError(
                    "OpenList 连接已被其他管理员修改，请刷新后重试"
                )
                conflict.code = "OPENLIST_CONNECTION_CONFLICT"
                conflict.status = 409
                raise conflict
            password = _decrypt_password(
                row["password_ciphertext"], row["credential_key_id"], self.master_key_provider
            )
        probe_context = {
            "connection_id": connection_id,
            "revision": revision,
            "base_url": row["base_url"],
            "username": row["username"],
            "password": password,
        }
        result = self.probe_runner(probe_context) or {}
        ok = bool(result.get("ok"))
        status = "ok" if ok else "failed"
        now = utc_now_iso()
        with self.database.connect() as conn:
            if persist_result:
                cursor = conn.execute(
                    "UPDATE openlist_connections SET last_test_status=?, last_tested_at=? "
                    "WHERE connection_id=? AND revision=?",
                    (status, now, connection_id, revision),
                )
                current_revision = revision if cursor.rowcount == 1 else None
            else:
                current = self._get_row(conn, connection_id)
                current_revision = int(current["revision"] or 0) if current is not None else None
            if current_revision != revision:
                # A newer configuration won the race.  Never report the stale
                # probe result as if it belonged to the current credentials.
                conflict = OpenListValidationError(
                    "OpenList 连接在检测期间已被修改，请重新检测"
                )
                conflict.code = "OPENLIST_CONNECTION_CONFLICT"
                conflict.status = 409
                raise conflict
        return {
            "connection_id": connection_id,
            "revision": revision,
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


def _json_contains_connection_id(raw: Any, connection_id: str) -> bool:
    """Return whether a plan JSON object structurally references a connection."""

    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False

    wanted = str(connection_id or "")

    def contains(candidate: Any) -> bool:
        if isinstance(candidate, dict):
            for key, item in candidate.items():
                normalized_key = str(key or "")
                if (
                    normalized_key == "connection_id"
                    or normalized_key.endswith("_connection_id")
                ) and str(item or "") == wanted:
                    return True
                if contains(item):
                    return True
            return False
        if isinstance(candidate, list):
            return any(contains(item) for item in candidate)
        return False

    return bool(wanted and contains(value))


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _table_exists(conn: Any, table_name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(table_name or ""),),
        ).fetchone()
    )


def _mount_snapshot_json(row: Any, connection_revision: int) -> str:
    """Serialize a mount health invalidation snapshot without importing mount_service."""

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
            "probe_root": str(row["probe_root"] or "/_fnos_probe"),
            "native_locator": native_locator if isinstance(native_locator, dict) else {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
