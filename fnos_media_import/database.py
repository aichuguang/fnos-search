from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .migrations import Migration, MigrationRunner
from .schema import AGENT_SCHEMA_SQL, BASE_SCHEMA_SQL
from .update_schema_migrator import UpdateSchemaMigrator
from .time_utils import utc_now_iso, utc_now_iso_offset
from .v1_cutover import CUTOVER_NAME, V1JobContractHardCutover


logger = logging.getLogger(__name__)


def utc_now() -> str:
    return utc_now_iso()


def utc_minutes_from_now(minutes: int) -> str:
    return utc_now_iso_offset(minutes=minutes)


def utc_seconds_from_now(seconds: int) -> str:
    return utc_now_iso_offset(seconds=seconds)


class Database:
    PRE_CUTOVER_VERSION = 28

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last_migration_backup: dict[str, Any] | None = None
        self.last_cutover_summary: dict[str, Any] | None = None
        migrations = (
                Migration(1, "baseline_scheduler_leases", lambda connection: None),
                # 2-5 曾被早期开发版本用于其他迁移名称。继续复用这些版本号会让
                # 已有数据库误判为“已执行”，从而跳过真实表结构升级。
                Migration(6, "update_schema_compatibility", self._apply_update_schema_migration),
                Migration(7, "import_job_idempotency_metadata", self._apply_job_idempotency_migration),
                Migration(8, "durable_worker_tasks", self._apply_worker_task_migration),
                Migration(9, "update_run_leases", self._apply_update_run_lease_migration),
                Migration(10, "sqlite_rate_limits", self._apply_rate_limit_migration),
                Migration(11, "trending_discovery", self._apply_trending_discovery_migration),
                Migration(12, "organizer_run_owner", self._apply_organizer_run_owner_migration),
                Migration(13, "organizer_run_leases", self._apply_organizer_run_lease_migration),
                Migration(14, "organizer_task_revisions", self._apply_organizer_task_revision_migration),
                Migration(15, "event_retention_indexes", self._apply_event_retention_index_migration),
                Migration(16, "notification_deliveries", self._apply_notification_deliveries_migration),
                Migration(17, "guest_notification_subscriptions", self._apply_guest_notification_subscriptions_migration),
                Migration(18, "guest_notification_verification_ciphertext", self._apply_guest_notification_verification_ciphertext_migration),
                Migration(19, "notification_delivery_query_indexes", self._apply_notification_delivery_query_indexes_migration),
                # v20 曾随已撤销的电影多资源实验版本发布到部分数据库，不能复用。
                # 保留空迁移可同时兼容这些历史数据库和全新安装。
                Migration(20, "reserved_organizer_multi_resource_movies", lambda connection: None),
                Migration(21, "storage_destination_columns", self._apply_storage_destination_migration),
                Migration(22, "v1_accounts_openlist_targets_rules", self._apply_v1_accounts_openlist_migration),
                Migration(23, "v1_multilink_execution_model", self._apply_v1_multilink_execution_migration),
                Migration(24, "v1_assignments_and_binding_revisions", self._apply_v1_assignment_binding_migration),
                Migration(25, "provider_auth_sessions", self._apply_provider_auth_session_migration),
                Migration(26, "v1_scoped_rclone_runs", self._apply_v1_scoped_rclone_run_migration),
                Migration(27, "provider_auth_session_cas", self._apply_provider_auth_session_cas_migration),
                Migration(28, "openlist_mount_native_locator_column", self._apply_openlist_mount_native_locator_migration),
                Migration(29, CUTOVER_NAME, self._apply_v1_job_contract_hard_cutover),
                Migration(30, "organizer_subscription_identity", self._apply_organizer_subscription_identity_migration),
        )
        self.migration_runner = MigrationRunner(migrations)
        self.pre_cutover_migration_runner = MigrationRunner(
            migration
            for migration in migrations
            if migration.version <= self.PRE_CUTOVER_VERSION
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        fresh = self._is_fresh_database()
        if not fresh:
            with closing(sqlite3.connect(self.path)) as connection:
                current = self.migration_runner.current_version(connection)
                self.migration_runner.ensure_compatible(connection)
            if current != self.migration_runner.latest_version:
                raise RuntimeError(
                    f"当前数据库为 v{current}，禁止应用启动时自动迁移；"
                    f"请停止所有服务后执行 python -m fnos_media_import.v1_cutover "
                    f"--database {self.path} --dry-run，再使用 --apply 升级到 "
                    f"v{self.migration_runner.latest_version}"
                )
        self.last_migration_backup = None
        with self.connect() as conn:
            # journal_mode 是数据库级持久设置，启动建表时设置一次即可；
            # 避免每次短连接都执行 PRAGMA journal_mode=WAL 造成额外磁盘同步开销。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(BASE_SCHEMA_SQL)
            conn.executescript(AGENT_SCHEMA_SQL)
            if fresh:
                self.migration_runner.run(conn, utc_now)
        if self.last_cutover_summary is not None:
            self.last_cutover_summary["backup_path"] = str(
                (self.last_migration_backup or {}).get("path") or ""
            )
            logger.warning(
                "event=v1_job_contract_hard_cutover summary=%s",
                json.dumps(
                    self.last_cutover_summary,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )

    def run_pre_cutover_migrations(self, connection: sqlite3.Connection) -> list[int]:
        """Bring a legacy database through v28 for the offline cutover CLI."""

        return self.pre_cutover_migration_runner.run(connection, utc_now)

    @staticmethod
    def install_base_schema(connection: sqlite3.Connection) -> None:
        deferred_indexes: list[str] = []
        statement = ""
        for line in BASE_SCHEMA_SQL.splitlines(keepends=True):
            statement += line
            if not sqlite3.complete_statement(statement):
                continue
            sql = statement.strip()
            statement = ""
            if not sql:
                continue
            try:
                connection.execute(sql)
            except sqlite3.OperationalError as exc:
                normalized = sql.lstrip().upper()
                if (
                    normalized.startswith("CREATE INDEX")
                    or normalized.startswith("CREATE UNIQUE INDEX")
                ) and "no such column" in str(exc).lower():
                    deferred_indexes.append(sql)
                    continue
                raise
        if statement.strip():
            connection.execute(statement)
        # Legacy tables may receive the indexed columns in a later migration.
        # The caller reruns this installer after those migrations complete.
        for sql in deferred_indexes:
            try:
                connection.execute(sql)
            except sqlite3.OperationalError as exc:
                if "no such column" not in str(exc).lower():
                    raise

    def _is_fresh_database(self) -> bool:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return True
        with closing(sqlite3.connect(self.path)) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            if not tables:
                return True
            if tables == ["schema_migrations"]:
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM schema_migrations"
                    ).fetchone()[0]
                )
                return count == 0
            return False

    def _apply_v1_job_contract_hard_cutover(self, conn: sqlite3.Connection) -> None:
        self.last_cutover_summary = V1JobContractHardCutover().apply(conn)

    def _apply_update_schema_migration(self, conn: sqlite3.Connection) -> None:
        UpdateSchemaMigrator(self._ensure_column).apply(conn)

    def _apply_organizer_subscription_identity_migration(self, conn: sqlite3.Connection) -> None:
        for column in ("identity_ns", "identity_value", "canonical_dir"):
            self._ensure_column(conn, "update_subscriptions", column, "TEXT")

    def _apply_job_idempotency_migration(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "import_jobs", "idempotency_key", "TEXT")
        self._ensure_column(conn, "import_jobs", "config_revision", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(conn, "import_jobs", "executor_id", "TEXT")
        conn.execute(
            "UPDATE import_jobs SET idempotency_key = 'legacy:' || id WHERE idempotency_key IS NULL OR idempotency_key = ''"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency_key ON import_jobs(idempotency_key)"
        )

    def _apply_storage_destination_migration(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "import_jobs", "destination_id", "TEXT")
        self._ensure_column(conn, "import_jobs", "plan_version", "INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_destination_id ON import_jobs(destination_id)"
        )

    def _apply_v1_accounts_openlist_migration(self, conn: sqlite3.Connection) -> None:
        """V1.0 统一账号体系、OpenList 连接/挂载、存储目标与来源规则表。"""

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_accounts (
              account_id TEXT PRIMARY KEY,
              provider_kind TEXT NOT NULL,
              name TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              current_revision INTEGER NOT NULL DEFAULT 0,
              identity_hint TEXT NOT NULL DEFAULT '',
              last_test_status TEXT NOT NULL DEFAULT 'untested',
              last_test_message TEXT NOT NULL DEFAULT '',
              last_tested_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_provider_accounts_kind ON provider_accounts(provider_kind);

            CREATE TABLE IF NOT EXISTS provider_account_revisions (
              account_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              config_json TEXT NOT NULL,
              credentials_ciphertext BLOB NOT NULL,
              credential_key_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (account_id, revision),
              FOREIGN KEY (account_id) REFERENCES provider_accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS provider_account_selections (
              provider_kind TEXT NOT NULL,
              scope TEXT NOT NULL DEFAULT 'default',
              account_id TEXT NOT NULL,
              account_revision INTEGER NOT NULL,
              selection_revision INTEGER NOT NULL DEFAULT 1,
              selected_at TEXT NOT NULL,
              PRIMARY KEY (provider_kind, scope),
              FOREIGN KEY (account_id) REFERENCES provider_accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS openlist_connections (
              connection_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              base_url TEXT NOT NULL,
              username TEXT NOT NULL,
              password_ciphertext BLOB NOT NULL,
              credential_key_id TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              revision INTEGER NOT NULL DEFAULT 1,
              last_test_status TEXT NOT NULL DEFAULT 'untested',
              last_tested_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS openlist_mounts (
              mount_id TEXT PRIMARY KEY,
              connection_id TEXT NOT NULL,
              name TEXT NOT NULL,
              provider_kind TEXT,
              account_id TEXT,
              webdav_root TEXT NOT NULL,
              namespace_id TEXT NOT NULL,
              readable INTEGER NOT NULL DEFAULT 0,
              writable INTEGER NOT NULL DEFAULT 0,
              enabled INTEGER NOT NULL DEFAULT 1,
              revision INTEGER NOT NULL DEFAULT 1,
              identity_status TEXT NOT NULL DEFAULT 'unverified',
              last_tested_at TEXT,
              FOREIGN KEY (connection_id) REFERENCES openlist_connections(connection_id),
              FOREIGN KEY (account_id) REFERENCES provider_accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_openlist_mounts_connection ON openlist_mounts(connection_id);
            CREATE INDEX IF NOT EXISTS idx_openlist_mounts_account ON openlist_mounts(account_id);

            CREATE TABLE IF NOT EXISTS storage_targets (
              destination_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              provider_kind TEXT NOT NULL,
              account_scope TEXT,
              target_kind TEXT NOT NULL DEFAULT 'provider',
              enabled INTEGER NOT NULL DEFAULT 1,
              audience TEXT NOT NULL DEFAULT 'admin',
              current_revision INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS storage_target_revisions (
              destination_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              categories_json TEXT NOT NULL,
              display_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (destination_id, revision),
              FOREIGN KEY (destination_id) REFERENCES storage_targets(destination_id)
            );

            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_storage_targets_provider ON storage_targets(provider_kind)"
        )

    def _apply_openlist_mount_native_locator_migration(self, conn: sqlite3.Connection) -> None:
        """迁移 23 曾在此列加入前被旧库应用过，现有库需要单独补列。"""
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='openlist_mounts'"
        ).fetchone():
            return
        self._ensure_column(conn, "openlist_mounts", "native_locator_json", "TEXT NOT NULL DEFAULT '{}'")

    def _apply_v1_multilink_execution_migration(self, conn: sqlite3.Connection) -> None:
        """Install the only supported V1 planning and durable execution model."""

        self._ensure_column(conn, "openlist_mounts", "storage_identity_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "openlist_mounts", "identity_root", "TEXT NOT NULL DEFAULT '/'")
        self._ensure_column(conn, "openlist_mounts", "probe_root", "TEXT NOT NULL DEFAULT '/_fnos_probe'")
        self._ensure_column(conn, "openlist_mounts", "native_locator_json", "TEXT NOT NULL DEFAULT '{}'")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_account_selection_revisions (
              provider_kind TEXT NOT NULL,
              scope TEXT NOT NULL,
              selection_revision INTEGER NOT NULL,
              account_id TEXT NOT NULL,
              account_revision INTEGER NOT NULL,
              selected_at TEXT NOT NULL,
              PRIMARY KEY (provider_kind, scope, selection_revision),
              FOREIGN KEY (account_id, account_revision)
                REFERENCES provider_account_revisions(account_id, revision)
            );

            CREATE TABLE IF NOT EXISTS openlist_connection_revisions (
              connection_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              config_json TEXT NOT NULL,
              password_ciphertext BLOB NOT NULL,
              credential_key_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (connection_id, revision),
              FOREIGN KEY (connection_id) REFERENCES openlist_connections(connection_id)
            );

            CREATE TABLE IF NOT EXISTS openlist_mount_revisions (
              mount_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              config_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (mount_id, revision),
              FOREIGN KEY (mount_id) REFERENCES openlist_mounts(mount_id)
            );

            CREATE TABLE IF NOT EXISTS storage_target_account_bindings (
              binding_id TEXT PRIMARY KEY,
              destination_id TEXT NOT NULL,
              target_revision INTEGER NOT NULL,
              account_id TEXT,
              enabled INTEGER NOT NULL DEFAULT 1,
              current_revision INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (destination_id, target_revision)
                REFERENCES storage_target_revisions(destination_id, revision),
              FOREIGN KEY (account_id) REFERENCES provider_accounts(account_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_target_binding_account
              ON storage_target_account_bindings(destination_id, target_revision, IFNULL(account_id, ''));

            CREATE TABLE IF NOT EXISTS storage_target_binding_revisions (
              binding_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              locator_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (binding_id, revision),
              FOREIGN KEY (binding_id) REFERENCES storage_target_account_bindings(binding_id)
            );

            CREATE TABLE IF NOT EXISTS source_records (
              source_record_id TEXT PRIMARY KEY,
              search_source_id TEXT NOT NULL,
              provider_kind TEXT NOT NULL,
              source_form TEXT NOT NULL,
              canonical_source_id TEXT NOT NULL,
              current_revision INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_source_record_canonical
              ON source_records(search_source_id, canonical_source_id);

            CREATE TABLE IF NOT EXISTS source_record_revisions (
              source_record_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              access_payload_ciphertext BLOB NOT NULL,
              credential_key_id TEXT NOT NULL,
              selected_items_json TEXT NOT NULL,
              selected_items_hash TEXT NOT NULL,
              expected_file_count INTEGER,
              expected_total_bytes INTEGER,
              created_at TEXT NOT NULL,
              PRIMARY KEY (source_record_id, revision),
              FOREIGN KEY (source_record_id) REFERENCES source_records(source_record_id)
            );

            CREATE TABLE IF NOT EXISTS import_execution_plans (
              job_id INTEGER PRIMARY KEY,
              schema_version INTEGER NOT NULL CHECK(schema_version = 1),
              plan_json TEXT NOT NULL,
              plan_hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY (job_id) REFERENCES import_jobs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS transfer_executions (
              job_id INTEGER PRIMARY KEY,
              strategy TEXT NOT NULL,
              phase TEXT NOT NULL,
              snapshot_json TEXT NOT NULL DEFAULT '{}',
              receipt_json TEXT,
              attempts INTEGER NOT NULL DEFAULT 0,
              resume_at TEXT,
              organizer_task_id INTEGER,
              staging_verified INTEGER NOT NULL DEFAULT 0,
              final_verified INTEGER NOT NULL DEFAULT 0,
              app_acked INTEGER NOT NULL DEFAULT 0,
              cleanup_json TEXT NOT NULL DEFAULT '{}',
              last_error_code TEXT NOT NULL DEFAULT '',
              last_error_message TEXT NOT NULL DEFAULT '',
              row_version INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (job_id) REFERENCES import_jobs(id) ON DELETE CASCADE,
              FOREIGN KEY (organizer_task_id) REFERENCES organizer_tasks(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_transfer_execution_phase_resume
              ON transfer_executions(phase, resume_at, job_id);
            """
        )
        self._ensure_column(conn, "source_record_revisions", "access_payload_hash", "TEXT NOT NULL DEFAULT ''")

    def _apply_v1_assignment_binding_migration(self, conn: sqlite3.Connection) -> None:
        """Install the canonical V1 admin routing and target binding model."""

        self._ensure_column(conn, "storage_target_account_bindings", "provider_kind", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "storage_target_binding_revisions", "account_revision", "INTEGER")
        conn.execute("DROP TABLE IF EXISTS source_destination_rules")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_destination_assignments (
              search_source_id TEXT NOT NULL,
              category_key TEXT NOT NULL,
              destination_id TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              revision INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (search_source_id, category_key),
              FOREIGN KEY (destination_id) REFERENCES storage_targets(destination_id)
            );
            CREATE INDEX IF NOT EXISTS idx_source_assignments_destination
              ON source_destination_assignments(destination_id);
            """
        )

    def _apply_provider_auth_session_migration(self, conn: sqlite3.Connection) -> None:
        """Durable, encrypted login sessions for Provider accounts."""

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_auth_sessions (
              session_id TEXT PRIMARY KEY,
              provider_kind TEXT NOT NULL,
              auth_method TEXT NOT NULL,
              status TEXT NOT NULL,
              state_ciphertext BLOB NOT NULL,
              credential_key_id TEXT NOT NULL,
              public_json TEXT NOT NULL DEFAULT '{}',
              message TEXT NOT NULL DEFAULT '',
              account_id TEXT,
              expires_at TEXT,
              row_version INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (account_id) REFERENCES provider_accounts(account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_provider_auth_sessions_status
              ON provider_auth_sessions(status, updated_at);
            """
        )

    def _apply_provider_auth_session_cas_migration(self, conn: sqlite3.Connection) -> None:
        """Add the optimistic-lock token used to serialize Provider polling."""

        self._ensure_column(
            conn,
            "provider_auth_sessions",
            "row_version",
            "INTEGER NOT NULL DEFAULT 1",
        )

    def _apply_v1_scoped_rclone_run_migration(self, conn: sqlite3.Connection) -> None:
        """Make every V1 rclone receipt address one durable run.

        The previous table only described a process-global invocation, so a
        worker could not safely poll or cancel the copy it submitted.  V1
        freezes the operation key, job and copy descriptor before the thread
        starts; process restarts can therefore reconcile the exact run rather
        than consulting global in-memory status.
        """

        self._ensure_column(conn, "rclone_runs", "operation_key", "TEXT")
        self._ensure_column(conn, "rclone_runs", "job_id", "INTEGER")
        self._ensure_column(
            conn,
            "rclone_runs",
            "copy_descriptor_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        self._ensure_column(conn, "rclone_runs", "updated_at", "TEXT NOT NULL DEFAULT ''")
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rclone_runs_operation_key
              ON rclone_runs(operation_key)
              WHERE operation_key IS NOT NULL AND operation_key <> '';
            CREATE INDEX IF NOT EXISTS idx_rclone_runs_job_status
              ON rclone_runs(job_id, status, id);
            """
        )

    def _apply_worker_task_migration(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS worker_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                idempotency_key TEXT NOT NULL UNIQUE,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                config_revision INTEGER NOT NULL DEFAULT 1,
                owner_id TEXT,
                lease_expires_at TEXT,
                available_at TEXT NOT NULL,
                result TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_worker_tasks_claim
            ON worker_tasks(status, available_at, lease_expires_at, id);
            CREATE INDEX IF NOT EXISTS idx_worker_tasks_type_status
            ON worker_tasks(task_type, status, available_at);
            """
        )

    def _apply_update_run_lease_migration(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "update_runs", "owner_id", "TEXT")
        self._ensure_column(conn, "update_runs", "heartbeat_at", "TEXT")
        self._ensure_column(conn, "update_runs", "lease_expires_at", "TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_update_runs_active_lease
            ON update_runs(subscription_id, status, lease_expires_at)
            """
        )

    def _apply_organizer_run_owner_migration(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "organizer_runs", "owner_id", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_organizer_runs_owner_status "
            "ON organizer_runs(owner_id, status)"
        )

    def _apply_organizer_run_lease_migration(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "organizer_runs", "heartbeat_at", "TEXT")
        self._ensure_column(conn, "organizer_runs", "lease_expires_at", "TEXT")
        self._ensure_column(conn, "organizer_locks", "expires_at", "TEXT")
        now = utc_now()
        # 老版本的 running 记录没有租约。升级时统一授予一次宽限租约，避免
        # 仅因 started_at 很早就误杀仍在执行的长任务；之后只按明确租约过期回收。
        conn.execute(
            """
            UPDATE organizer_runs
            SET heartbeat_at = COALESCE(NULLIF(heartbeat_at, ''), ?),
                lease_expires_at = COALESCE(NULLIF(lease_expires_at, ''), ?)
            WHERE status = 'running'
            """,
            (now, utc_minutes_from_now(30)),
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_organizer_runs_active_lease
            ON organizer_runs(task_id, status, lease_expires_at)
            """
        )

    def _apply_organizer_task_revision_migration(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "organizer_tasks", "revision", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(conn, "organizer_tasks", "scan_owner", "TEXT")
        self._ensure_column(conn, "organizer_tasks", "scan_lease_expires_at", "TEXT")
        self._ensure_column(conn, "organizer_runs", "task_revision", "INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE organizer_tasks SET revision = 1 WHERE revision IS NULL OR revision < 1")
        conn.execute("UPDATE organizer_runs SET task_revision = 1 WHERE task_revision IS NULL OR task_revision < 1")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_organizer_tasks_scan_lease "
            "ON organizer_tasks(status, scan_lease_expires_at)"
        )

    @staticmethod
    def _apply_event_retention_index_migration(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_job_events_created_at_id
            ON job_events(created_at, id);
            CREATE INDEX IF NOT EXISTS idx_guest_request_events_created_at_id
            ON guest_request_events(created_at, id);
            CREATE INDEX IF NOT EXISTS idx_rclone_events_created_at_id
            ON rclone_events(created_at, id);
            CREATE INDEX IF NOT EXISTS idx_rclone_file_events_created_at_id
            ON rclone_file_events(created_at, id);
            CREATE INDEX IF NOT EXISTS idx_update_events_created_at_id
            ON update_events(created_at, id);
            """
        )

    @staticmethod
    def _apply_notification_deliveries_migration(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                event_type TEXT NOT NULL,
                channel TEXT NOT NULL,
                recipient TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                status_code INTEGER,
                response_summary TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_task_id
            ON notification_deliveries(task_id);
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_event_created
            ON notification_deliveries(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
            ON notification_deliveries(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_task_channel
            ON notification_deliveries(task_id, channel, id);
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_created_at
            ON notification_deliveries(created_at);
            """
        )

    @staticmethod
    def _apply_notification_delivery_query_indexes_migration(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_task_channel
            ON notification_deliveries(task_id, channel, id);
            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_created_at
            ON notification_deliveries(created_at);
            """
        )

    @staticmethod
    def _apply_guest_notification_subscriptions_migration(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guest_notification_subscriptions (
                request_id INTEGER PRIMARY KEY,
                email_encrypted TEXT NOT NULL,
                email_hash TEXT NOT NULL,
                verification_token_encrypted TEXT NOT NULL DEFAULT '',
                verification_token_hash TEXT NOT NULL,
                verification_expires_at TEXT NOT NULL,
                unsubscribe_token_encrypted TEXT NOT NULL,
                unsubscribe_token_hash TEXT NOT NULL UNIQUE,
                verified_at TEXT,
                opted_out_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES guest_requests(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_guest_notification_email_hash
            ON guest_notification_subscriptions(email_hash);
            CREATE INDEX IF NOT EXISTS idx_guest_notification_verification
            ON guest_notification_subscriptions(verification_token_hash, verification_expires_at);
            """
        )

    @staticmethod
    def _apply_guest_notification_verification_ciphertext_migration(conn: sqlite3.Connection) -> None:
        Database._ensure_column(
            conn,
            "guest_notification_subscriptions",
            "verification_token_encrypted",
            "TEXT NOT NULL DEFAULT ''",
        )

    @staticmethod
    def _apply_agent_token_migration(conn: sqlite3.Connection) -> None:
        """Create the agent tables without touching the schema version.

        Kept as a helper so the token CLI can prepare an already-initialized
        database without running the full migration chain.
        """

        conn.executescript(AGENT_SCHEMA_SQL)

    @staticmethod
    def _apply_rate_limit_migration(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_buckets (
                bucket_key TEXT PRIMARY KEY,
                window_started_at INTEGER NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rate_limit_buckets_expires_at
            ON rate_limit_buckets(expires_at);
            """
        )

    @staticmethod
    def _apply_trending_discovery_migration(conn: sqlite3.Connection) -> None:
        # Keep the migration idempotent even for databases initialized by an
        # earlier development build of this feature.
        existing = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trending_discovery_runs'"
        ).fetchone()
        if existing:
            Database._ensure_column(conn, "trending_discovery_runs", "success_source_count", "INTEGER NOT NULL DEFAULT 0")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trending_discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_type TEXT NOT NULL DEFAULT 'scheduled',
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                source_count INTEGER NOT NULL DEFAULT 0,
                success_source_count INTEGER NOT NULL DEFAULT 0,
                raw_item_count INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                auto_subscribed_count INTEGER NOT NULL DEFAULT 0,
                review_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                summary TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trending_runs_started_at
            ON trending_discovery_runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trending_runs_status
            ON trending_discovery_runs(status, started_at DESC);

            CREATE TABLE IF NOT EXISTS trending_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                snapshot_date TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                original_title TEXT,
                year INTEGER,
                media_type TEXT,
                rank INTEGER,
                heat REAL,
                score REAL,
                update_text TEXT,
                platform TEXT,
                is_completed INTEGER NOT NULL DEFAULT 0,
                image_url TEXT,
                raw_data TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES trending_discovery_runs(id) ON DELETE SET NULL,
                UNIQUE(snapshot_date, source, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_trending_snapshots_source_date
            ON trending_snapshots(source, snapshot_date DESC, rank);
            CREATE INDEX IF NOT EXISTS idx_trending_snapshots_run
            ON trending_snapshots(run_id);

            CREATE TABLE IF NOT EXISTS trending_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                original_title TEXT,
                year INTEGER,
                media_type TEXT,
                rank INTEGER,
                heat REAL,
                score REAL,
                platform TEXT,
                platform_ranks TEXT,
                status TEXT NOT NULL DEFAULT 'discovered',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                media_exists INTEGER NOT NULL DEFAULT 0,
                subscription_id INTEGER,
                initial_import_job_id INTEGER,
                ignore_until TEXT,
                ignore_reason TEXT,
                raw_data TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(subscription_id) REFERENCES update_subscriptions(id) ON DELETE SET NULL,
                FOREIGN KEY(initial_import_job_id) REFERENCES import_jobs(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trending_candidates_status
            ON trending_candidates(status, heat DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_trending_candidates_type
            ON trending_candidates(media_type, status, heat DESC);
            CREATE INDEX IF NOT EXISTS idx_trending_candidates_source
            ON trending_candidates(source, source_id);
            """
        )

    def _backup_before_pending_migrations(self) -> dict[str, Any] | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        connection = sqlite3.connect(self.path)
        try:
            has_user_tables = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            has_pending_migrations = bool(self.migration_runner.pending(connection))
        finally:
            connection.close()
        if has_user_tables and has_pending_migrations:
            return self.backup_database(
                prefix=f"app_before_schema_migration_v{self.migration_runner.latest_version}"
            )
        return None



    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def backup_database(self, *, prefix: str = "app_backup") -> dict[str, Any]:
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        safe_prefix = re.sub(r"[^0-9a-zA-Z_-]+", "_", str(prefix or "app_backup")).strip("_") or "app_backup"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"{safe_prefix}_{stamp}.db"
        source = sqlite3.connect(self.path, timeout=30)
        target_connection = sqlite3.connect(target)
        try:
            quick_check = str(source.execute("PRAGMA quick_check").fetchone()[0])
            if quick_check.lower() != "ok":
                raise sqlite3.DatabaseError(f"数据库校验失败：{quick_check}")
            source.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source.close()
        validation = sqlite3.connect(target)
        try:
            validation.row_factory = sqlite3.Row
            copied_check = str(validation.execute("PRAGMA quick_check").fetchone()[0])
            has_subscriptions = validation.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='update_subscriptions'"
            ).fetchone()
            subscription_count = int(validation.execute("SELECT COUNT(*) FROM update_subscriptions").fetchone()[0]) if has_subscriptions else 0
        finally:
            validation.close()
        if copied_check.lower() != "ok":
            raise sqlite3.DatabaseError(f"备份数据库校验失败：{copied_check}")
        return {
            "path": str(target),
            "size": target.stat().st_size,
            "quick_check": copied_check,
            "subscription_count": subscription_count,
            "backup_api": "sqlite3.Connection.backup",
        }
