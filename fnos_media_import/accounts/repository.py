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


def _object_account_ids(value: Any) -> set[str]:
    """Extract account IDs from a canonical plan or execution snapshot."""
    data = _parse_raw_data(value)
    ids: set[str] = set()

    def visit(candidate: Any) -> None:
        if isinstance(candidate, dict):
            for key, child in candidate.items():
                if str(key).strip().lower() == "account_id":
                    account_id = str(child or "").strip()
                    if account_id:
                        ids.add(account_id)
                elif isinstance(child, (dict, list, tuple)):
                    visit(child)
        elif isinstance(candidate, (list, tuple)):
            for child in candidate:
                visit(child)

    visit(data)
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

    def invalidate_storage_mappings(self, account_id: str, *, conn: Any) -> int:
        """Invalidate current mappings when credential continuity is unproven.

        Append mount revisions rather than rewriting old proofs. Existing
        frozen plans retain their original credential/mount snapshots; current
        bindings are disabled until the administrator revalidates the location.
        """
        from ..openlist.mount_service import _mount_revision_json

        now = utc_now()
        mounts = conn.execute("SELECT * FROM openlist_mounts WHERE account_id=?", (account_id,)).fetchall()
        for mount in mounts:
            history = conn.execute(
                "SELECT config_json FROM openlist_mount_revisions WHERE mount_id=? AND revision=?",
                (mount["mount_id"], mount["revision"]),
            ).fetchone()
            if history is None:
                raise AccountConflictError("账号挂载缺少不可变修订，请先修复保存位置")
            snapshot = json.loads(history["config_json"])
            locator = json.loads(mount["native_locator_json"] or "{}")
            locator = dict(locator) if isinstance(locator, dict) else {}
            for key in ("probe_fingerprint", "upload_tested_at", "organizer_tested_at", "probe_message"):
                locator.pop(key, None)
            locator.update(upload_verified=False, organizer_mutation_verified=False)
            conn.execute(
                "UPDATE openlist_mounts SET revision=revision+1, identity_status='unverified', "
                "readable=0, writable=0, last_tested_at=NULL, native_locator_json=? WHERE mount_id=?",
                (json.dumps(locator, ensure_ascii=False, sort_keys=True), mount["mount_id"]),
            )
            updated = conn.execute("SELECT * FROM openlist_mounts WHERE mount_id=?", (mount["mount_id"],)).fetchone()
            conn.execute(
                "INSERT INTO openlist_mount_revisions(mount_id, revision, config_json, created_at) VALUES (?,?,?,?)",
                (mount["mount_id"], updated["revision"], _mount_revision_json(updated, int(snapshot["connection_revision"])), now),
            )
        bindings = conn.execute(
            "UPDATE storage_target_account_bindings SET enabled=0, updated_at=? WHERE account_id=? AND enabled=1",
            (now, account_id),
        )
        return len(mounts) + int(bindings.rowcount)

    def delete_account(self, account_id: str, *, conn: Any = None) -> None:
        with self._session(conn) as c:
            # Historical selection revisions are immutable references to
            # account revisions.  They may be removed only after the service
            # has proved no live V1 plan/execution still depends on them.
            c.execute(
                "DELETE FROM provider_account_selections WHERE account_id = ?", (account_id,)
            )
            c.execute(
                "DELETE FROM provider_account_selection_revisions WHERE account_id = ?",
                (account_id,),
            )
            # Auth sessions are ephemeral; retain no FK pointing at a deleted
            # account while preserving the session audit row.
            try:
                c.execute(
                    "UPDATE provider_auth_sessions SET account_id = NULL "
                    "WHERE account_id = ?",
                    (account_id,),
                )
            except sqlite3.OperationalError:
                # Older isolated test schemas may not have auth sessions.
                pass
            c.execute(
                "DELETE FROM provider_account_revisions WHERE account_id = ?", (account_id,)
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

    def get_selection_revision(
        self,
        provider_kind: str,
        scope: str,
        selection_revision: int,
        *,
        conn: Any = None,
    ) -> AccountSelection | None:
        """Read an immutable historical account selection.

        Execution plans freeze the selection revision at creation time.  The
        current selection may legitimately move to another account later, so
        validators must consult this append-only history rather than compare
        against :meth:`get_selection`.
        """
        with self._session(conn) as c:
            row = c.execute(
                "SELECT provider_kind, scope, account_id, account_revision, selection_revision "
                "FROM provider_account_selection_revisions "
                "WHERE provider_kind = ? AND scope = ? AND selection_revision = ?",
                (provider_kind, scope, int(selection_revision)),
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
            c.execute(
                "INSERT INTO provider_account_selection_revisions "
                "(provider_kind, scope, selection_revision, account_id, account_revision, selected_at) "
                "VALUES (?,?,?,?,?,?)",
                (provider_kind, scope, next_revision, account_id, account_revision, utc_now()),
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

    def count_frozen_plans_referencing_account(
        self, account_id: str, *, conn: Any = None
    ) -> int:
        """Count live references using canonical V1 plan/execution rows.

        ``import_jobs.raw_data`` is a denormalized presentation snapshot and
        is not a safe source for deletion decisions. Canonical plans and
        execution snapshots are both inspected. Immutable V1 plans are audit
        records, so even completed/cancelled executions retain their frozen
        account revisions and block physical account deletion.
        """
        account_id = str(account_id or "").strip()
        if not account_id:
            return 0
        try:
            with self._session(conn) as c:
                rows = c.execute(
                    "SELECT p.job_id, p.plan_json, e.phase, e.snapshot_json "
                    "FROM import_execution_plans p "
                    "LEFT JOIN transfer_executions e ON e.job_id = p.job_id"
                ).fetchall()
                execution_rows = c.execute(
                    "SELECT e.job_id, e.phase, e.snapshot_json "
                    "FROM transfer_executions e "
                    "LEFT JOIN import_execution_plans p ON p.job_id = e.job_id "
                    "WHERE p.job_id IS NULL"
                ).fetchall()
        except sqlite3.OperationalError:
            # A missing V1 schema must never fall back to mutable job raw_data.
            # Report one conservative live reference so destructive account
            # deletion remains fail-closed in isolated or broken databases.
            return 1

        referenced_jobs: set[int] = set()
        for row in rows:
            plan_ids = _object_account_ids(row["plan_json"])
            snapshot_ids = _object_account_ids(row["snapshot_json"])
            if account_id in plan_ids or account_id in snapshot_ids:
                referenced_jobs.add(int(row["job_id"]))
        for row in execution_rows:
            if account_id in _object_account_ids(row["snapshot_json"]):
                referenced_jobs.add(int(row["job_id"]))
        return len(referenced_jobs)

    def count_non_terminal_jobs_referencing_account(
        self, account_id: str, *, conn: Any = None
    ) -> int:
        """Compatibility name for the stricter immutable-plan reference count."""

        return self.count_frozen_plans_referencing_account(account_id, conn=conn)

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
