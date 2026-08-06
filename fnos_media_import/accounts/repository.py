"""Provider 账号仓库：provider_accounts / provider_account_revisions /
provider_account_selections 三类表的读写与任务引用计数。

``database.connect()`` 是唯一的连接入口（sqlite3 + Row）。仓库方法默认自开
连接并在方法内提交；传入 ``conn`` 时复用调用方连接，由调用方统一提交/回滚，
用于 service 层的事务性操作（激活、禁用、删除）。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from ..domain.models import AccountSelection, ProviderAccount
from ..time_utils import utc_now_iso as utc_now

# 终态任务不再阻挡账号/修订删除。
TERMINAL_JOB_STATUSES = frozenset(
    {"done", "failed", "unsupported", "cancelled", "canceled", "skipped", "rejected"}
)


class ProviderAccountError(RuntimeError):
    """账号体系错误基类（repository 层可直接抛出，service/blueprint 统一捕获）。"""

    code = "ACCOUNT_ERROR"


class AccountNotFoundError(ProviderAccountError):
    code = "ACCOUNT_NOT_FOUND"


class AccountConflictError(ProviderAccountError):
    code = "ACCOUNT_CONFLICT"


class SelectionChangedError(ProviderAccountError):
    """乐观并发冲突：期望的 selection_revision 与当前值不一致。"""

    code = "SELECTION_CHANGED"


class AccountInUseError(ProviderAccountError):
    """账号仍被引用（进行中任务 / 当前选择 / OpenList 挂载），无法删除。"""

    code = "ACCOUNT_IN_USE"


def _parse_raw_data(value: Any) -> Any:
    """import_jobs.raw_data 可能是 JSON 字符串或已解析对象。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    return None


def _plan_account_ids(raw: Any) -> set[str]:
    """从 v4 执行计划（raw_data.execution_plan）收集引用的 account_id。"""
    ids: set[str] = set()
    data = _parse_raw_data(raw)
    if not isinstance(data, dict):
        return ids
    plan = data.get("execution_plan")
    if isinstance(plan, dict):
        for key in ("source", "destination"):
            block = plan.get(key)
            if isinstance(block, dict):
                aid = str(block.get("account_id") or "").strip()
                if aid:
                    ids.add(aid)
    # 向前兼容：计划直接内联或平铺在 raw_data 时也能识别。
    for key in ("source", "destination"):
        block = data.get(key)
        if isinstance(block, dict):
            aid = str(block.get("account_id") or "").strip()
            if aid:
                ids.add(aid)
    aid = str(data.get("account_id") or "").strip()
    if aid:
        ids.add(aid)
    return ids


class ProviderAccountRepository:
    def __init__(self, database: Any) -> None:
        self._database = database

    @contextmanager
    def _session(self, conn: Any = None) -> Iterator[Any]:
        if conn is not None:
            yield conn
        else:
            with self._database.connect() as session:
                yield session

    # ---- accounts ----

    def create_account(self, account: ProviderAccount, *, conn: Any = None) -> None:
        with self._session(conn) as c:
            try:
                c.execute(
                    """
                    INSERT INTO provider_accounts (
                        account_id, provider_kind, name, enabled, current_revision,
                        identity_hint, last_test_status, last_test_message, last_tested_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account.account_id,
                        account.provider_kind,
                        account.name,
                        1 if account.enabled else 0,
                        account.current_revision,
                        account.identity_hint,
                        account.last_test_status,
                        account.last_test_message,
                        account.last_tested_at,
                        account.created_at,
                        account.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountConflictError(f"账号已存在：{account.account_id}") from exc

    def get_account(self, account_id: str, *, conn: Any = None) -> ProviderAccount | None:
        with self._session(conn) as c:
            row = c.execute(
                "SELECT * FROM provider_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            return self._row_to_account(row) if row is not None else None

    def list_accounts(
        self, provider_kind: str | None = None, *, conn: Any = None
    ) -> list[ProviderAccount]:
        with self._session(conn) as c:
            if provider_kind:
                rows = c.execute(
                    "SELECT * FROM provider_accounts WHERE provider_kind = ? ORDER BY created_at",
                    (provider_kind,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM provider_accounts ORDER BY created_at"
                ).fetchall()
            return [self._row_to_account(row) for row in rows]

    def update_account_name(self, account_id: str, name: str, *, conn: Any = None) -> None:
        with self._session(conn) as c:
            cursor = c.execute(
                "UPDATE provider_accounts SET name = ?, updated_at = ? WHERE account_id = ?",
                (name, utc_now(), account_id),
            )
            if cursor.rowcount == 0:
                raise AccountNotFoundError(account_id)

    def update_identity_hint(self, account_id: str, identity_hint: str, *, conn: Any = None) -> None:
        with self._session(conn) as c:
            c.execute(
                "UPDATE provider_accounts SET identity_hint = ?, updated_at = ? WHERE account_id = ?",
                (identity_hint, utc_now(), account_id),
            )

    def update_account_test(
        self,
        account_id: str,
        status: str,
        message: str,
        tested_at: str | None,
        *,
        conn: Any = None,
    ) -> None:
        with self._session(conn) as c:
            c.execute(
                """
                UPDATE provider_accounts
                SET last_test_status = ?, last_test_message = ?, last_tested_at = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (status, message, tested_at, utc_now(), account_id),
            )

    def set_account_enabled(self, account_id: str, enabled: bool, *, conn: Any = None) -> None:
        with self._session(conn) as c:
            cursor = c.execute(
                "UPDATE provider_accounts SET enabled = ?, updated_at = ? WHERE account_id = ?",
                (1 if enabled else 0, utc_now(), account_id),
            )
            if cursor.rowcount == 0:
                raise AccountNotFoundError(account_id)

    def set_account_revision(self, account_id: str, revision: int, *, conn: Any = None) -> None:
        with self._session(conn) as c:
            c.execute(
                "UPDATE provider_accounts SET current_revision = ?, updated_at = ? WHERE account_id = ?",
                (revision, utc_now(), account_id),
            )

    def delete_account(self, account_id: str, *, conn: Any = None) -> None:
        with self._session(conn) as c:
            c.execute(
                "DELETE FROM provider_account_revisions WHERE account_id = ?", (account_id,)
            )
            c.execute(
                "DELETE FROM provider_account_selections WHERE account_id = ?", (account_id,)
            )
            try:
                cursor = c.execute(
                    "DELETE FROM provider_accounts WHERE account_id = ?", (account_id,)
                )
            except sqlite3.IntegrityError as exc:
                raise AccountInUseError(
                    "账号仍被 OpenList 挂载或存储目标引用，无法删除"
                ) from exc
            if cursor.rowcount == 0:
                raise AccountNotFoundError(account_id)

    # ---- revisions ----

    def insert_revision(
        self,
        account_id: str,
        revision: int,
        config_json: str,
        credentials_ciphertext: bytes,
        credential_key_id: str,
        *,
        conn: Any = None,
    ) -> None:
        with self._session(conn) as c:
            c.execute(
                """
                INSERT INTO provider_account_revisions (
                    account_id, revision, config_json, credentials_ciphertext,
                    credential_key_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (account_id, revision, config_json, credentials_ciphertext, credential_key_id, utc_now()),
            )

    def get_revision(
        self, account_id: str, revision: int, *, conn: Any = None
    ) -> dict[str, Any] | None:
        with self._session(conn) as c:
            row = c.execute(
                "SELECT * FROM provider_account_revisions WHERE account_id = ? AND revision = ?",
                (account_id, revision),
            ).fetchone()
            if row is None:
                return None
            return {
                "account_id": row["account_id"],
                "revision": int(row["revision"]),
                "config": _parse_raw_data(row["config_json"]) or {},
                "credentials_ciphertext": bytes(row["credentials_ciphertext"]),
                "credential_key_id": row["credential_key_id"],
                "created_at": row["created_at"],
            }

    def latest_revision(self, account_id: str, *, conn: Any = None) -> int | None:
        with self._session(conn) as c:
            row = c.execute(
                "SELECT MAX(revision) AS max_rev FROM provider_account_revisions WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None or row["max_rev"] is None:
                return None
            return int(row["max_rev"])

    # 任务冻结 account_id + revision 时读取不可变修订；旧修订始终可读。
    read_revision = get_revision

    def list_revisions(self, account_id: str, *, conn: Any = None) -> list[int]:
        with self._session(conn) as c:
            rows = c.execute(
                "SELECT revision FROM provider_account_revisions WHERE account_id = ? ORDER BY revision",
                (account_id,),
            ).fetchall()
            return [int(row["revision"]) for row in rows]

    # ---- selections ----

    def get_selection(
        self, provider_kind: str, scope: str, *, conn: Any = None
    ) -> AccountSelection | None:
        with self._session(conn) as c:
            row = c.execute(
                "SELECT * FROM provider_account_selections WHERE provider_kind = ? AND scope = ?",
                (provider_kind, scope),
            ).fetchone()
            return self._row_to_selection(row) if row is not None else None

    def list_selections(self, *, conn: Any = None) -> list[AccountSelection]:
        with self._session(conn) as c:
            rows = c.execute("SELECT * FROM provider_account_selections").fetchall()
            return [self._row_to_selection(row) for row in rows]

    def selection_references_account(self, account_id: str, *, conn: Any = None) -> bool:
        with self._session(conn) as c:
            row = c.execute(
                "SELECT 1 FROM provider_account_selections WHERE account_id = ? LIMIT 1",
                (account_id,),
            ).fetchone()
            return row is not None

    def upsert_selection(
        self,
        provider_kind: str,
        scope: str,
        account_id: str,
        account_revision: int,
        expected_selection_revision: int | None,
        *,
        conn: Any = None,
    ) -> AccountSelection:
        """乐观并发写入当前账号选择。

        - 已存在选择：必须传 ``expected_selection_revision`` 且与当前一致，否则抛
          :class:`SelectionChangedError`；成功后 selection_revision 递增。
        - 不存在选择：首次激活，selection_revision 固定为 1。
        """
        with self._session(conn) as c:
            existing = c.execute(
                "SELECT selection_revision FROM provider_account_selections WHERE provider_kind = ? AND scope = ?",
                (provider_kind, scope),
            ).fetchone()
            if existing is not None:
                current = int(existing["selection_revision"])
                if expected_selection_revision is None or int(expected_selection_revision) != current:
                    raise SelectionChangedError(
                        f"账号选择已变化（当前 selection_revision={current}），请刷新后重试"
                    )
                next_revision = current + 1
                c.execute(
                    """
                    UPDATE provider_account_selections
                    SET account_id = ?, account_revision = ?, selection_revision = ?, selected_at = ?
                    WHERE provider_kind = ? AND scope = ?
                    """,
                    (account_id, account_revision, next_revision, utc_now(), provider_kind, scope),
                )
            else:
                next_revision = 1
                c.execute(
                    """
                    INSERT INTO provider_account_selections (
                        provider_kind, scope, account_id, account_revision,
                        selection_revision, selected_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (provider_kind, scope, account_id, account_revision, next_revision, utc_now()),
                )
            return AccountSelection(
                provider_kind=provider_kind,
                scope=scope,
                account_id=account_id,
                account_revision=account_revision,
                selection_revision=next_revision,
            )

    def delete_selection(self, provider_kind: str, scope: str, *, conn: Any = None) -> None:
        with self._session(conn) as c:
            c.execute(
                "DELETE FROM provider_account_selections WHERE provider_kind = ? AND scope = ?",
                (provider_kind, scope),
            )

    # ---- job reference counting ----

    def count_non_terminal_jobs_referencing_account(
        self, account_id: str, *, conn: Any = None
    ) -> int:
        """统计引用该账号的非终态 v4 任务数量（计划中 source/destination 任意一侧）。"""
        with self._session(conn) as c:
            rows = c.execute("SELECT status, raw_data FROM import_jobs").fetchall()
        count = 0
        for row in rows:
            status = str(row["status"] or "").strip().lower()
            if status in TERMINAL_JOB_STATUSES:
                continue
            if account_id in _plan_account_ids(row["raw_data"]):
                count += 1
        return count

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> ProviderAccount:
        return ProviderAccount(
            account_id=row["account_id"],
            provider_kind=row["provider_kind"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            current_revision=int(row["current_revision"]),
            identity_hint=row["identity_hint"] or "",
            last_test_status=row["last_test_status"] or "untested",
            last_test_message=row["last_test_message"] or "",
            last_tested_at=row["last_tested_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_selection(row: sqlite3.Row) -> AccountSelection:
        return AccountSelection(
            provider_kind=row["provider_kind"],
            scope=row["scope"],
            account_id=row["account_id"],
            account_revision=int(row["account_revision"]),
            selection_revision=int(row["selection_revision"]),
        )


__all__ = [
    "AccountConflictError",
    "AccountInUseError",
    "AccountNotFoundError",
    "ProviderAccountError",
    "ProviderAccountRepository",
    "SelectionChangedError",
]
