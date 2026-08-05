from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .repositories import AppSettingsRepository, GuestNotificationSubscriptionRepository, GuestRequestCommandRepository, GuestRequestQueryRepository, JobCommandRepository, JobQueryRepository, NotificationDeliveryRepository, OrganizerRepository, RateLimitRepository, RcloneRepository, ResourceRepository, SchedulerLeaseRepository, SearchCacheCommandRepository, SearchCacheQueryRepository, TrendingRepository, UpdateRepository, UpdateRunRepository, UpdateSubscriptionQueryRepository, WorkerTaskRepository
from .migrations import Migration, MigrationRunner
from .schema import BASE_SCHEMA_SQL
from .update_schema_migrator import UpdateSchemaMigrator
from .repositories.database_domain_mixin import DatabaseDomainMixin
from .time_utils import utc_now_iso, utc_now_iso_offset


def utc_now() -> str:
    return utc_now_iso()


def utc_minutes_from_now(minutes: int) -> str:
    return utc_now_iso_offset(minutes=minutes)


def utc_seconds_from_now(seconds: int) -> str:
    return utc_now_iso_offset(seconds=seconds)


class Database(DatabaseDomainMixin):
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.app_settings = AppSettingsRepository(self.connect)
        self.guest_request_commands = GuestRequestCommandRepository(self.connect)
        self.guest_request_queries = GuestRequestQueryRepository(self.connect, self.row_to_dict)
        self.guest_notification_subscriptions = GuestNotificationSubscriptionRepository(self.connect)
        self.job_commands = JobCommandRepository(self.connect, self._terminal_duplicate_status)
        self.job_queries = JobQueryRepository(self.connect, self.row_to_dict)
        self.resources = ResourceRepository(self.connect, self.row_to_dict)
        self.scheduler_leases = SchedulerLeaseRepository(self.connect)
        self.rate_limits = RateLimitRepository(self.connect)
        self.worker_tasks = WorkerTaskRepository(self.connect, self.row_to_dict)
        self.notification_deliveries = NotificationDeliveryRepository(self.connect)
        self.search_cache_commands = SearchCacheCommandRepository(self.connect)
        self.search_cache_queries = SearchCacheQueryRepository(self.connect, self.row_to_dict)
        self.update_subscription_queries = UpdateSubscriptionQueryRepository(
            self.connect,
            self._decode_update_subscription,
            self._decode_update_source,
        )
        self.update_runs = UpdateRunRepository(self.connect, self._decode_update_run)
        self.trending = TrendingRepository(self.connect)
        self.rclone = RcloneRepository(self.connect, self.row_to_dict)
        self.organizer = OrganizerRepository(self.connect)
        self.update = UpdateRepository(
            self.connect,
            subscription_queries=self.update_subscription_queries,
            update_runs=self.update_runs,
            decode_run=self._decode_update_run,
            decode_candidate=self._decode_update_candidate,
            backup=self.backup_database,
        )
        self.migration_runner = MigrationRunner(
            (
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
            )
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
        self._backup_before_pending_migrations()
        with self.connect() as conn:
            # journal_mode 是数据库级持久设置，启动建表时设置一次即可；
            # 避免每次短连接都执行 PRAGMA journal_mode=WAL 造成额外磁盘同步开销。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(BASE_SCHEMA_SQL)
            self.migration_runner.run(conn, utc_now)

    def _apply_update_schema_migration(self, conn: sqlite3.Connection) -> None:
        UpdateSchemaMigrator(self._ensure_column).apply(conn)

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

    def _backup_before_pending_migrations(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        connection = sqlite3.connect(self.path)
        try:
            has_user_tables = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            has_pending_migrations = bool(self.migration_runner.pending(connection))
        finally:
            connection.close()
        if has_user_tables and has_pending_migrations:
            self.backup_database(prefix=f"app_before_schema_migration_v{self.migration_runner.latest_version}")



    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field in ("raw_data",):
            if result.get(field):
                try:
                    result[field] = json.loads(result[field])
                except json.JSONDecodeError:
                    pass
        return result

    @staticmethod
    def _terminal_duplicate_status(*values: Any) -> bool:
        text = " ".join(str(value or "").strip().lower() for value in values if str(value or "").strip())
        if not text:
            return False
        terminal_words = (
            "cancelled",
            "canceled",
            "rejected",
            "failed",
            "unsupported",
            "skipped",
            "取消",
            "拒绝",
            "未通过",
            "失败",
            "暂不支持",
        )
        return any(word in text for word in terminal_words)

    def save_resource(self, item: dict[str, Any]) -> int:
        return self.save_resources([item])[0]

    def save_resources(self, items: list[dict[str, Any]]) -> list[int]:
        return self.resources.save_many(items)

    def get_resource(self, resource_id: int) -> dict[str, Any] | None:
        return self.resources.get(resource_id)

    def find_resource_by_url(self, source_url: str, *, source: str = "") -> dict[str, Any] | None:
        return self.resources.find_by_url(source_url, source=source)

    def save_search_cache(self, public_id: str, keyword: str, item: dict[str, Any], expires_minutes: int = 60) -> int:
        return self.save_search_cache_many([(public_id, item)], keyword=keyword, expires_minutes=expires_minutes)[0]

    def save_search_cache_many(self, records: list[tuple[str, dict[str, Any]]], keyword: str, expires_minutes: int = 60) -> list[int]:
        return self.search_cache_commands.save_many(records, keyword=keyword, expires_minutes=expires_minutes)

    def get_search_cache(self, public_id: str) -> dict[str, Any] | None:
        return self.search_cache_queries.get_active(public_id)

    def update_search_cache_item(self, public_id: str, item: dict[str, Any], expires_minutes: int = 60) -> bool:
        return self.search_cache_commands.update(public_id, item, expires_minutes)

    def prune_expired_search_cache(self, limit: int = 1000) -> int:
        return self.search_cache_commands.prune_expired(limit=limit)

    def find_active_search_cache_by_urls(self, keyword: str, urls: list[str]) -> dict[str, dict[str, Any]]:
        return self.search_cache_queries.find_active_by_urls(keyword, urls)

    def create_guest_request(self, data: dict[str, Any]) -> int:
        return self.guest_request_commands.create(data)

    def create_guest_request_with_event(
        self,
        data: dict[str, Any],
        *,
        level: str,
        message: str,
        event_data: Any = None,
        emit: Callable[[sqlite3.Connection, int], Any] | None = None,
    ) -> int:
        return self.guest_request_commands.create_with_event(
            data, level=level, message=message, event_data=event_data, emit=emit
        )

    def update_guest_request(self, request_id: int, **updates: Any) -> None:
        self.guest_request_commands.update(request_id, updates)

    def transition_guest_request_with_event(
        self,
        request_id: int,
        *,
        expected_statuses: set[str],
        status: str,
        public_status: str,
        raw_data: dict[str, Any] | None,
        level: str,
        message: str,
        event_data: Any = None,
    ) -> bool:
        return self.guest_request_commands.transition_with_event(
            request_id,
            expected_statuses=expected_statuses,
            status=status,
            public_status=public_status,
            raw_data=raw_data,
            level=level,
            message=message,
            event_data=event_data,
        )

    def add_guest_request_event(self, request_id: int, level: str, message: str, raw_data: Any = None) -> int:
        return self.guest_request_commands.add_event(request_id, level, message, raw_data)

    def get_guest_request_by_token(self, request_token: str) -> dict[str, Any] | None:
        return self.guest_request_queries.get_by_token(request_token)

    def get_guest_request(self, request_id: int) -> dict[str, Any] | None:
        return self.guest_request_queries.get(request_id)

    def list_guest_requests_by_job(self, job_id: int) -> list[dict[str, Any]]:
        return self.guest_request_queries.list_by_job(job_id)

    def list_guest_request_events(self, request_id: int) -> list[dict[str, Any]]:
        return self.guest_request_queries.list_events(request_id)

    def list_guest_request_events_for_requests(self, request_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        return self.guest_request_queries.list_events_for_requests(request_ids)

    def count_guest_requests(self, status: str | None = None) -> int:
        return self.guest_request_queries.count(status)

    def list_guest_requests(self, limit: int = 100, status: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        return self.guest_request_queries.list(limit=limit, status=status, offset=offset)

    def find_recent_guest_request_by_url(
        self,
        *,
        source_url: str,
        category: str,
        within_minutes: int = 1440,
    ) -> dict[str, Any] | None:
        return self.guest_request_queries.find_recent_by_url(
            source_url=source_url, category=category, within_minutes=within_minutes
        )

    def create_job(self, data: dict[str, Any]) -> tuple[int, bool]:
        return self.job_commands.create(data)

    def update_job(self, job_id: int, **updates: Any) -> None:
        self.job_commands.update(job_id, updates)

    def update_job_if_status(
        self,
        job_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
        **updates: Any,
    ) -> bool:
        return self.job_commands.update_if_status(job_id, expected_statuses, updates)

    def update_job_if_status_and_claim_token(
        self,
        job_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
        expected_claim_token: str | None,
        **updates: Any,
    ) -> bool:
        return self.job_commands.update_if_status_and_claim_token(
            job_id,
            expected_statuses,
            expected_claim_token,
            updates,
        )

    def add_event(self, job_id: int, level: str, message: str, raw_data: Any = None) -> int:
        return self.job_commands.add_event(job_id, level, message, raw_data)

    def delete_job_if_status(
        self,
        job_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
    ) -> bool:
        return self.job_commands.delete_if_status(job_id, expected_statuses)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return self.job_queries.get(job_id)

    def get_job_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        return self.job_queries.get_by_idempotency_key(idempotency_key)

    def get_jobs_by_ids(self, job_ids: list[int]) -> dict[int, dict[str, Any]]:
        return self.job_queries.get_many(job_ids)

    def list_jobs(
        self,
        limit: int = 100,
        status: str | None = None,
        category: str | None = None,
        source_type: str | None = None,
        keyword: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self.job_queries.list(
            limit=limit,
            status=status,
            category=category,
            source_type=source_type,
            keyword=keyword,
            offset=offset,
        )

    def count_jobs(
        self,
        status: str | None = None,
        category: str | None = None,
        source_type: str | None = None,
        keyword: str | None = None,
    ) -> int:
        return self.job_queries.count(status=status, category=category, source_type=source_type, keyword=keyword)

    def list_events(self, job_id: int) -> list[dict[str, Any]]:
        return self.job_queries.list_events(job_id)


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

    @staticmethod
    def _table_count(conn: sqlite3.Connection, table: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) AS total FROM {table}").fetchone()
        return int(row["total"] if row else 0)

    @staticmethod
    def _decode_json_fields(item: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        for field in fields:
            if item.get(field):
                try:
                    item[field] = json.loads(item[field])
                except (TypeError, json.JSONDecodeError):
                    pass
        return item

    @staticmethod
    def _decode_update_subscription(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        return Database._decode_json_fields(item, ("aliases", "days_of_week", "missing_episodes", "include_keywords", "exclude_keywords", "raw_data"))

    @staticmethod
    def _decode_update_source(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        return Database._decode_json_fields(item, ("options",))

    @staticmethod
    def _decode_update_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        return Database._decode_json_fields(item, ("summary", "raw_data", "run_log"))

    @staticmethod
    def _decode_update_candidate(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        return Database._decode_json_fields(item, ("raw_data",))

    def get_app_settings(self) -> dict[str, Any]:
        return self.app_settings.get_all()

    def set_app_settings(self, updates: dict[str, Any]) -> None:
        self.app_settings.set_many(updates)

    def update_app_setting_atomic(
        self,
        key: str,
        updater: Any,
    ) -> tuple[bool, Any, Any]:
        return self.app_settings.update_atomic(key, updater)

    def mutate_app_settings_atomic(
        self,
        updater: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.app_settings.mutate_all_atomic(updater)

    def compare_and_set_app_setting(
        self,
        key: str,
        expected: Any,
        replacement: Any,
        *,
        expected_exists: bool = True,
        replacement_exists: bool = True,
    ) -> bool:
        return self.app_settings.compare_and_set(
            key,
            expected,
            replacement,
            expected_exists=expected_exists,
            replacement_exists=replacement_exists,
        )

    def acquire_scheduler_lease(self, name: str, owner_id: str, ttl_seconds: int = 90) -> bool:
        return self.scheduler_leases.acquire(name, owner_id, ttl_seconds)

    def release_scheduler_lease(self, name: str, owner_id: str) -> bool:
        return self.scheduler_leases.release(name, owner_id)

    def scheduler_lease(self, name: str) -> dict[str, Any] | None:
        return self.scheduler_leases.get(name)

    # Trending discovery compatibility facade.  Keep callers independent of the
    # repository implementation while the feature is introduced incrementally.
    def create_trending_run(self, **kwargs: Any) -> int:
        return self.trending.create_run(**kwargs)

    def finish_trending_run(self, run_id: int, status: str = "success", **updates: Any) -> bool:
        return self.trending.finish_run(run_id, status=status, **updates)

    def get_trending_run(self, run_id: int) -> dict[str, Any] | None:
        return self.trending.get_run(run_id)

    def get_latest_trending_run(self) -> dict[str, Any] | None:
        return self.trending.get_latest_run()

    def list_trending_runs(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return self.trending.list_runs(limit=limit, offset=offset)

    def count_trending_runs(self) -> int:
        return self.trending.count_runs()

    def upsert_trending_snapshot(self, *, run_id: int | None = None, item: dict[str, Any] | None = None, **kwargs: Any) -> int:
        return self.trending.upsert_snapshot(run_id=run_id, item=item, **kwargs)

    def list_trending_snapshots(self, *, snapshot_date: str | None = None, source: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self.trending.list_snapshots(snapshot_date=snapshot_date, source=source, limit=limit, offset=offset)

    def upsert_trending_candidate(self, *, item: dict[str, Any] | None = None, **kwargs: Any) -> int:
        return self.trending.upsert_candidate(item=item, **kwargs)

    def get_trending_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return self.trending.get_candidate(candidate_id)

    def list_trending_candidates(
        self,
        *,
        status: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        last_run_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self.trending.list_candidates(
            status=status,
            media_type=media_type,
            source=source,
            last_run_id=last_run_id,
            limit=limit,
            offset=offset,
        )

    def count_trending_candidates(
        self,
        *,
        status: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        last_run_id: int | None = None,
    ) -> int:
        return self.trending.count_candidates(
            status=status,
            media_type=media_type,
            source=source,
            last_run_id=last_run_id,
        )

    def update_trending_candidate(self, candidate_id: int, updates: dict[str, Any] | None = None, **kwargs: Any) -> bool:
        return self.trending.update_candidate(candidate_id, updates, **kwargs)

    def claim_trending_candidate_for_initial_import(
        self,
        candidate_id: int,
        *,
        allowed_statuses: tuple[str, ...] = ("discovered", "import_failed"),
    ) -> dict[str, Any] | None:
        return self.trending.claim_candidate_for_initial_import(
            candidate_id,
            allowed_statuses=allowed_statuses,
        )

    def bind_trending_candidate_initial_import_job(
        self,
        candidate_id: int,
        job_id: int,
        *,
        status: str = "importing",
    ) -> bool:
        return self.trending.bind_candidate_initial_import_job(
            candidate_id,
            job_id,
            status=status,
        )

    def release_trending_candidate_initial_import(
        self,
        candidate_id: int,
        *,
        status: str = "discovered",
    ) -> bool:
        return self.trending.release_candidate_initial_import(candidate_id, status=status)

    # --- Rclone domain facade (delegates to RcloneRepository) ---

    def find_job_for_rclone_callback(self, category: str, filename: str, source_path: str = "", target_path: str = "") -> dict[str, Any] | None:
        return self.rclone.find_job_for_rclone_callback(category, filename, source_path=source_path, target_path=target_path)

    def create_rclone_run(self, trigger_reason: str) -> int:
        return self.rclone.create_rclone_run(trigger_reason)

    def update_rclone_run(self, run_id: int, status: str, exit_code: int | None = None, error_message: str = "") -> None:
        self.rclone.update_rclone_run(run_id, status, exit_code=exit_code, error_message=error_message)

    def add_rclone_event(self, run_id: int | None, level: str, message: str, raw_data: Any = None) -> int:
        return self.rclone.add_rclone_event(run_id, level, message, raw_data=raw_data)

    def count_rclone_runs(self) -> int:
        return self.rclone.count_rclone_runs()

    def list_rclone_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return self.rclone.list_rclone_runs(limit=limit, offset=offset)

    def list_rclone_events(self, run_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        return self.rclone.list_rclone_events(run_id=run_id, limit=limit)

    def add_rclone_file_event(
        self,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str,
        level: str,
        category: str = "",
        filename: str,
        source_path: str = "",
        target_path: str = "",
        message: str = "",
        raw_data: Any = None,
    ) -> int:
        return self.rclone.add_rclone_file_event(
            run_id=run_id,
            job_id=job_id,
            status=status,
            level=level,
            category=category,
            filename=filename,
            source_path=source_path,
            target_path=target_path,
            message=message,
            raw_data=raw_data,
        )

    def get_rclone_file_event(self, event_id: int) -> dict[str, Any] | None:
        return self.rclone.get_rclone_file_event(event_id)

    def attach_rclone_file_event_to_job(self, event_id: int, job_id: int) -> bool:
        return self.rclone.attach_rclone_file_event_to_job(event_id, job_id)

    def list_rclone_file_events(
        self,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str | None = None,
        category: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self.rclone.list_rclone_file_events(run_id=run_id, job_id=job_id, status=status, category=category, limit=limit, offset=offset)

    def list_unmatched_rclone_file_events(
        self,
        *,
        limit: int = 500,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.rclone.list_unmatched_rclone_file_events(limit=limit, before_id=before_id)

    def list_all_rclone_file_events(
        self,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str | None = None,
        category: str | None = None,
        batch_size: int = 1000,
    ) -> list[dict[str, Any]]:
        return self.rclone.list_all_rclone_file_events(run_id=run_id, job_id=job_id, status=status, category=category, batch_size=batch_size)

    def count_rclone_file_events(
        self,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str | None = None,
        category: str | None = None,
    ) -> int:
        return self.rclone.count_rclone_file_events(run_id=run_id, job_id=job_id, status=status, category=category)

    # --- Organizer domain facade (delegates to OrganizerRepository) ---

    def create_organizer_task(
        self,
        *,
        category: str,
        openlist_root_path: str,
        category_label: str = "",
        title: str = "",
        source_keyword: str = "",
        trigger_type: str = "",
        job_id: int | None = None,
        request_id: int | None = None,
        rclone_run_id: int | None = None,
        status: str = "pending",
        evidence: Any = None,
        raw_data: Any = None,
    ) -> int:
        return self.organizer.create_organizer_task(
            category=category,
            openlist_root_path=openlist_root_path,
            category_label=category_label,
            title=title,
            source_keyword=source_keyword,
            trigger_type=trigger_type,
            job_id=job_id,
            request_id=request_id,
            rclone_run_id=rclone_run_id,
            status=status,
            evidence=evidence,
            raw_data=raw_data,
        )

    def get_or_create_organizer_task_for_job(
        self,
        *,
        job_id: int,
        category: str,
        openlist_root_path: str,
        category_label: str = "",
        title: str = "",
        source_keyword: str = "",
        trigger_type: str = "",
        request_id: int | None = None,
        rclone_run_id: int | None = None,
        status: str = "pending",
        evidence: Any = None,
        raw_data: Any = None,
    ) -> tuple[int, bool]:
        return self.organizer.get_or_create_organizer_task_for_job(
            job_id=job_id,
            category=category,
            openlist_root_path=openlist_root_path,
            category_label=category_label,
            title=title,
            source_keyword=source_keyword,
            trigger_type=trigger_type,
            request_id=request_id,
            rclone_run_id=rclone_run_id,
            status=status,
            evidence=evidence,
            raw_data=raw_data,
        )

    def find_recent_organizer_task(self, openlist_root_path: str, category: str = "", active_only: bool = True) -> dict[str, Any] | None:
        return self.organizer.find_recent_organizer_task(openlist_root_path, category=category, active_only=active_only)

    def update_organizer_task(self, task_id: int, **updates: Any) -> bool:
        return self.organizer.update_organizer_task(task_id, **updates)

    def cancel_organizer_task(self, task_id: int, *, reason: str = "") -> bool:
        return self.organizer.cancel_organizer_task(task_id, reason=reason)

    def claim_organizer_task_for_scan(
        self,
        task_id: int,
        *,
        allowed_statuses: list[str] | tuple[str, ...] | set[str],
        owner_id: str = "",
        lease_seconds: int = 120,
    ) -> bool:
        return self.organizer.claim_organizer_task_for_scan(task_id, allowed_statuses=allowed_statuses, owner_id=owner_id, lease_seconds=lease_seconds)

    def renew_organizer_scan(
        self,
        task_id: int,
        owner_id: str,
        *,
        lease_seconds: int = 120,
        expected_revision: int | None = None,
    ) -> bool:
        return self.organizer.renew_organizer_scan(task_id, owner_id, lease_seconds=lease_seconds, expected_revision=expected_revision)

    def owns_organizer_scan(
        self,
        task_id: int,
        owner_id: str,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        return self.organizer.owns_organizer_scan(task_id, owner_id, expected_revision=expected_revision)

    def get_organizer_task(self, task_id: int, include_children: bool = True) -> dict[str, Any] | None:
        return self.organizer.get_organizer_task(task_id, include_children=include_children)

    def delete_organizer_task_if_status(
        self,
        task_id: int,
        expected_statuses: set[str] | list[str] | tuple[str, ...],
    ) -> bool:
        return self.organizer.delete_organizer_task_if_status(task_id, expected_statuses)

    def count_organizer_tasks(self, status: str | None = None) -> int:
        return self.organizer.count_organizer_tasks(status)

    def list_organizer_tasks(self, limit: int = 100, status: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        return self.organizer.list_organizer_tasks(limit=limit, status=status, offset=offset)

    def list_organizer_tasks_by_job(self, job_id: int, limit: int = 20) -> list[dict[str, Any]]:
        return self.organizer.list_organizer_tasks_by_job(job_id, limit=limit)

    def replace_organizer_plan(
        self,
        task_id: int,
        *,
        files: list[dict[str, Any]],
        mappings: list[dict[str, Any]],
        operations: list[dict[str, Any]],
        expected_revision: int | None = None,
        owner_id: str = "",
        expected_status: str | None = None,
        task_updates: dict[str, Any] | None = None,
    ) -> bool:
        return self.organizer.replace_organizer_plan(
            task_id,
            files=files,
            mappings=mappings,
            operations=operations,
            expected_revision=expected_revision,
            owner_id=owner_id,
            expected_status=expected_status,
            task_updates=task_updates,
        )

    def replace_organizer_operations(self, task_id: int, operations: list[dict[str, Any]]) -> None:
        self.organizer.replace_organizer_operations(task_id, operations)

    def update_organizer_mapping(self, mapping_id: int, **updates: Any) -> None:
        self.organizer.update_organizer_mapping(mapping_id, **updates)

    def update_organizer_mappings_and_plan(
        self,
        task_id: int,
        *,
        mapping_updates: list[dict[str, Any]],
        operations: list[dict[str, Any]],
        evidence: dict[str, Any],
        expected_status: str,
        expected_revision: int | None = None,
        task_updates: dict[str, Any] | None = None,
        clear_scan_lease: bool = False,
    ) -> bool:
        return self.organizer.update_organizer_mappings_and_plan(
            task_id,
            mapping_updates=mapping_updates,
            operations=operations,
            evidence=evidence,
            expected_status=expected_status,
            expected_revision=expected_revision,
            task_updates=task_updates,
            clear_scan_lease=clear_scan_lease,
        )

    def create_organizer_run(
        self,
        task_id: int,
        status: str = "running",
        *,
        owner_id: str = "",
        lease_seconds: int = 120,
    ) -> int:
        return self.organizer.create_organizer_run(task_id, status, owner_id=owner_id, lease_seconds=lease_seconds)

    def claim_organizer_run(
        self,
        task_id: int,
        *,
        owner_id: str = "",
        lease_seconds: int = 120,
    ) -> tuple[int | None, dict[str, Any] | None]:
        return self.organizer.claim_organizer_run(task_id, owner_id=owner_id, lease_seconds=lease_seconds)

    def renew_organizer_run(self, run_id: int, owner_id: str, *, lease_seconds: int = 120) -> bool:
        return self.organizer.renew_organizer_run(run_id, owner_id, lease_seconds=lease_seconds)

    def owns_organizer_run(self, run_id: int, owner_id: str) -> bool:
        return self.organizer.owns_organizer_run(run_id, owner_id)

    def update_organizer_run(
        self,
        run_id: int,
        status: str,
        *,
        summary: Any = None,
        undo_data: Any = None,
        error_message: str = "",
        owner_id: str | None = None,
    ) -> bool:
        return self.organizer.update_organizer_run(
            run_id,
            status,
            summary=summary,
            undo_data=undo_data,
            error_message=error_message,
            owner_id=owner_id,
        )

    def finalize_organizer_run_and_task(
        self,
        run_id: int,
        task_id: int,
        *,
        owner_id: str,
        run_status: str,
        task_status: str,
        summary: Any = None,
        undo_data: Any = None,
        error_message: str = "",
        evidence: Any = None,
        raw_data: Any = None,
    ) -> bool:
        return self.organizer.finalize_organizer_run_and_task(
            run_id,
            task_id,
            owner_id=owner_id,
            run_status=run_status,
            task_status=task_status,
            summary=summary,
            undo_data=undo_data,
            error_message=error_message,
            evidence=evidence,
            raw_data=raw_data,
        )

    def count_organizer_runs(self) -> int:
        return self.organizer.count_organizer_runs()

    def list_organizer_runs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self.organizer.list_organizer_runs(limit=limit, offset=offset)

    def list_organizer_runs_by_task_ids(self, task_ids: list[int]) -> list[dict[str, Any]]:
        return self.organizer.list_organizer_runs_by_task_ids(task_ids)

    def update_organizer_operation(self, operation_id: int, **updates: Any) -> None:
        self.organizer.update_organizer_operation(operation_id, **updates)

    def add_organizer_ai_suggestion(self, task_id: int, provider: str, model: str, prompt: Any, response: Any, parsed: Any) -> int:
        return self.organizer.add_organizer_ai_suggestion(task_id, provider, model, prompt, response, parsed)

    def add_organizer_tmdb_match(self, task_id: int, query: str, media_type: str, item: dict[str, Any], score: float = 0) -> int:
        return self.organizer.add_organizer_tmdb_match(task_id, query, media_type, item, score)

    def acquire_organizer_lock(self, lock_key: str, *, task_id: int | None = None, run_id: int | None = None, owner: str = "organizer") -> bool:
        return self.organizer.acquire_organizer_lock(lock_key, task_id=task_id, run_id=run_id, owner=owner)

    def release_organizer_locks(self, *, task_id: int | None = None, run_id: int | None = None, lock_keys: list[str] | None = None) -> None:
        self.organizer.release_organizer_locks(task_id=task_id, run_id=run_id, lock_keys=lock_keys)

    def recover_stale_organizer_runs(
        self,
        *,
        older_than_minutes: int = 30,
        owner_id: str = "",
        message: str = "服务重启后清理遗留 Organizer 运行锁",
    ) -> dict[str, Any]:
        return self.organizer.recover_stale_organizer_runs(
            older_than_minutes=older_than_minutes,
            owner_id=owner_id,
            message=message,
        )

    # --- Update domain facade (delegates to UpdateRepository) ---

    def create_update_subscription(self, data: dict[str, Any], sources: list[dict[str, Any]] | None = None) -> int:
        return self.update.create_update_subscription(data, sources)

    def create_update_subscription_with_outcome(
        self,
        data: dict[str, Any],
        sources: list[dict[str, Any]] | None = None,
    ) -> tuple[int, bool]:
        return self.update.create_update_subscription_with_outcome(data, sources)

    def update_update_subscription(self, subscription_id: int, updates: dict[str, Any], sources: list[dict[str, Any]] | None = None) -> None:
        self.update.update_update_subscription(subscription_id, updates, sources)

    def delete_update_subscription(self, subscription_id: int) -> bool:
        return self.update.delete_update_subscription(subscription_id)

    def get_update_subscription(self, subscription_id: int, include_sources: bool = True) -> dict[str, Any] | None:
        return self.update.get_update_subscription(subscription_id, include_sources)

    def list_update_subscriptions(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        due_before: str | None = None,
        include_sources: bool = False,
    ) -> list[dict[str, Any]]:
        return self.update.list_update_subscriptions(limit=limit, offset=offset, status=status, due_before=due_before, include_sources=include_sources)

    def count_update_subscriptions(self, status: str | None = None) -> int:
        return self.update.count_update_subscriptions(status)

    def create_update_run(self, subscription_id: int, trigger_type: str, scheduled_at: str = "", raw_data: Any = None) -> int:
        return self.update.create_update_run(subscription_id, trigger_type, scheduled_at=scheduled_at, raw_data=raw_data)

    def claim_update_run(
        self,
        subscription_id: int,
        trigger_type: str,
        *,
        scheduled_at: str = "",
        owner_id: str,
        lease_seconds: int = 120,
        raw_data: Any = None,
    ) -> tuple[int | None, dict[str, Any] | None]:
        return self.update.claim_update_run(
            subscription_id,
            trigger_type,
            scheduled_at=scheduled_at,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            raw_data=raw_data,
        )

    def renew_update_run(self, run_id: int, owner_id: str, *, lease_seconds: int = 120) -> bool:
        return self.update.renew_update_run(run_id, owner_id, lease_seconds=lease_seconds)

    def owns_update_run(self, run_id: int, owner_id: str) -> bool:
        return self.update.owns_update_run(run_id, owner_id)

    def finish_update_run(
        self,
        run_id: int,
        owner_id: str,
        *,
        status: str,
        **updates: Any,
    ) -> bool:
        return self.update.finish_update_run(run_id, owner_id, status=status, **updates)

    def recover_stale_update_runs(
        self,
        *,
        older_than_seconds: int = 120,
        message: str = "追更运行因进程中断或租约过期而终止",
    ) -> list[dict[str, Any]]:
        return self.update.recover_stale_update_runs(older_than_seconds=older_than_seconds, message=message)

    def update_update_run(self, run_id: int, **updates: Any) -> None:
        self.update.update_update_run(run_id, **updates)

    def append_update_run_log(self, run_id: int, stage: str, message: str, raw_data: Any = None) -> None:
        self.update.append_update_run_log(run_id, stage, message, raw_data=raw_data)

    def get_update_run(self, run_id: int) -> dict[str, Any] | None:
        return self.update.get_update_run(run_id)

    def list_update_runs(self, *, subscription_id: int | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return self.update.list_update_runs(subscription_id=subscription_id, limit=limit, offset=offset)

    def count_update_runs(self, subscription_id: int | None = None) -> int:
        return self.update.count_update_runs(subscription_id)

    def get_running_update_run(self) -> dict[str, Any] | None:
        return self.update.get_running_update_run()

    def create_update_candidate(self, data: dict[str, Any]) -> int:
        return self.update.create_update_candidate(data)

    def update_update_candidate(self, candidate_id: int, **updates: Any) -> None:
        self.update.update_update_candidate(candidate_id, **updates)

    def get_update_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return self.update.get_update_candidate(candidate_id)

    def list_update_candidates(
        self,
        *,
        subscription_id: int | None = None,
        run_id: int | None = None,
        decision: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self.update.list_update_candidates(subscription_id=subscription_id, run_id=run_id, decision=decision, limit=limit, offset=offset)

    def count_update_candidates(self, *, subscription_id: int | None = None, run_id: int | None = None, decision: str | None = None) -> int:
        return self.update.count_update_candidates(subscription_id=subscription_id, run_id=run_id, decision=decision)

    def upsert_update_seen_item(self, data: dict[str, Any]) -> bool:
        return self.update.upsert_update_seen_item(data)

    def get_update_preview_cache(self, source_type: str, source_url: str, *, now: str | None = None) -> dict[str, Any] | None:
        return self.update.get_update_preview_cache(source_type, source_url, now=now)

    def upsert_update_preview_cache(
        self,
        *,
        source_type: str,
        source_url: str,
        ok: bool,
        message: str = "",
        items: list[dict[str, Any]] | None = None,
        latest_season: int | None = None,
        latest_episode: int | None = None,
        raw_data: Any = None,
        ttl_seconds: int = 3600,
    ) -> None:
        self.update.upsert_update_preview_cache(
            source_type=source_type,
            source_url=source_url,
            ok=ok,
            message=message,
            items=items,
            latest_season=latest_season,
            latest_episode=latest_episode,
            raw_data=raw_data,
            ttl_seconds=ttl_seconds,
        )

    def get_update_path_snapshot(self, subscription_id: int, openlist_path: str, *, now: str | None = None) -> dict[str, Any] | None:
        return self.update.get_update_path_snapshot(subscription_id, openlist_path, now=now)

    def upsert_update_path_snapshot(
        self,
        *,
        subscription_id: int,
        openlist_path: str,
        files: list[dict[str, Any]],
        latest_season: int | None = None,
        latest_episode: int | None = None,
        raw_data: Any = None,
        ttl_seconds: int = 1800,
    ) -> None:
        self.update.upsert_update_path_snapshot(
            subscription_id=subscription_id,
            openlist_path=openlist_path,
            files=files,
            latest_season=latest_season,
            latest_episode=latest_episode,
            raw_data=raw_data,
            ttl_seconds=ttl_seconds,
        )

    def update_seen_episode_exists(self, subscription_id: int, season: int | None, episode: int | None) -> bool:
        return self.update.update_seen_episode_exists(subscription_id, season, episode)

    def list_update_seen_episodes(self, subscription_id: int) -> set[tuple[int | None, int]]:
        return self.update.list_update_seen_episodes(subscription_id)

    def add_update_event(self, subscription_id: int | None, run_id: int | None, level: str, message: str, raw_data: Any = None) -> int:
        return self.update.add_update_event(subscription_id, run_id, level, message, raw_data=raw_data)

    def list_update_events(self, *, subscription_id: int | None = None, run_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.update.list_update_events(subscription_id=subscription_id, run_id=run_id, limit=limit)

    def history_cleanup_summary(self) -> dict[str, Any]:
        return self.update.history_cleanup_summary()

    def cleanup_history_records(self, *, backup: bool = True, vacuum: bool = True, backup_prefix: str = "app_before_history_cleanup") -> dict[str, Any]:
        return self.update.cleanup_history_records(backup=backup, vacuum=vacuum, backup_prefix=backup_prefix)

    # --- Notification domain facade (delegates to NotificationDeliveryRepository) ---

    def record_notification_delivery(self, **kwargs: Any) -> int:
        return self.notification_deliveries.record(**kwargs)

    def latest_notification_delivery_status_by_task(self, task_id: int) -> dict[str, str]:
        return self.notification_deliveries.latest_status_by_channel(task_id)

    def list_notification_deliveries(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.notification_deliveries.list_deliveries(**kwargs)

    def notification_delivery_summary(self) -> dict[str, Any]:
        return self.notification_deliveries.summary()

    def prune_notification_deliveries(self, *, before: str) -> int:
        return self.notification_deliveries.prune(before=before)

    def last_notification_delivery_for_event(self, event_type: str, after: str) -> dict[str, Any] | None:
        return self.notification_deliveries.last_delivery_for_event(event_type, after)

    def create_guest_notification_subscription(self, **kwargs: Any) -> None:
        self.guest_notification_subscriptions.create(**kwargs)

    def get_guest_notification_subscription(self, request_id: int) -> dict[str, Any] | None:
        return self.guest_notification_subscriptions.get_for_request(request_id)

    def verify_guest_notification_subscription(self, token: str) -> dict[str, Any] | None:
        return self.guest_notification_subscriptions.verify(token)

    def opt_out_guest_notification_subscription(self, token: str) -> dict[str, Any] | None:
        return self.guest_notification_subscriptions.opt_out(token)

    def anonymize_guest_notification_subscriptions(self, **kwargs: Any) -> int:
        return self.guest_notification_subscriptions.anonymize_terminal(**kwargs)
