from __future__ import annotations

import sqlite3
from typing import Callable


class UpdateSchemaMigrator:
    def __init__(self, ensure_column: Callable[[sqlite3.Connection, str, str, str], None]) -> None:
        self._ensure_column = ensure_column

    def apply(self, conn: sqlite3.Connection) -> None:
        self._ensure_update_schema(conn)
        self._ensure_update_indexes(conn)

    def _ensure_update_schema(self, conn: sqlite3.Connection) -> None:
        """兼容早期追更表的安全迁移。

        追更功能是在已有项目上增量加入的，开发过程中可能已经创建过
        字段不完整的 update_* 表。这里只做 ADD COLUMN，不做破坏性变更。
        """

        for column, definition in {
            "category_label": "TEXT",
            "media_type": "TEXT NOT NULL DEFAULT 'tv'",
            "season": "INTEGER",
            "year": "TEXT",
            "tmdb_id": "INTEGER",
            "query_template": "TEXT",
            "aliases": "TEXT",
            "schedule_kind": "TEXT NOT NULL DEFAULT 'weekly'",
            "days_of_week": "TEXT",
            "time_of_day": "TEXT",
            "interval_minutes": "INTEGER",
            "timezone": "TEXT NOT NULL DEFAULT 'Asia/Shanghai'",
            "next_run_at": "TEXT",
            "last_run_at": "TEXT",
            "last_success_at": "TEXT",
            "next_episode": "INTEGER",
            "last_success_episode": "INTEGER",
            "missing_episodes": "TEXT",
            "source_strategy": "TEXT NOT NULL DEFAULT 'mixed'",
            "auto_import_policy": "TEXT NOT NULL DEFAULT 'auto_high_confidence'",
            "min_score": "INTEGER DEFAULT 75",
            "quality_profile": "TEXT",
            "include_keywords": "TEXT",
            "exclude_keywords": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'enabled'",
            "raw_data": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        }.items():
            self._ensure_column(conn, "update_subscriptions", column, definition)
        for column, definition in {
            "password": "TEXT",
            "provider": "TEXT",
            "priority": "INTEGER DEFAULT 100",
            "enabled": "INTEGER DEFAULT 1",
            "options": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        }.items():
            self._ensure_column(conn, "update_sources", column, definition)
        for column, definition in {
            "trigger_type": "TEXT NOT NULL DEFAULT 'manual'",
            "status": "TEXT NOT NULL DEFAULT 'running'",
            "scheduled_at": "TEXT",
            "started_at": "TEXT",
            "finished_at": "TEXT",
            "candidate_count": "INTEGER DEFAULT 0",
            "imported_count": "INTEGER DEFAULT 0",
            "skipped_count": "INTEGER DEFAULT 0",
            "error_message": "TEXT",
            "stage": "TEXT",
            "run_log": "TEXT",
            "summary": "TEXT",
            "raw_data": "TEXT",
        }.items():
            self._ensure_column(conn, "update_runs", column, definition)
        for column, definition in {
            "run_id": "INTEGER",
            "job_id": "INTEGER",
            "source_id": "INTEGER",
            "title": "TEXT",
            "source_type": "TEXT",
            "source_url": "TEXT",
            "source_url_hash": "TEXT",
            "password": "TEXT",
            "season": "INTEGER",
            "episode": "INTEGER",
            "size_text": "TEXT",
            "published_at": "TEXT",
            "score": "INTEGER DEFAULT 0",
            "decision": "TEXT NOT NULL DEFAULT 'review'",
            "reason": "TEXT",
            "raw_data": "TEXT",
            "created_at": "TEXT",
        }.items():
            self._ensure_column(conn, "update_candidates", column, definition)
        for column, definition in {
            "subscription_id": "INTEGER",
            "fingerprint": "TEXT",
            "source_type": "TEXT",
            "source_url_hash": "TEXT",
            "file_id": "TEXT",
            "file_name": "TEXT",
            "size": "INTEGER",
            "season": "INTEGER",
            "episode": "INTEGER",
            "first_seen_at": "TEXT",
            "last_seen_at": "TEXT",
            "raw_data": "TEXT",
        }.items():
            self._ensure_column(conn, "update_seen_items", column, definition)
        for column, definition in {
            "source_type": "TEXT",
            "source_url_hash": "TEXT",
            "source_url": "TEXT",
            "ok": "INTEGER DEFAULT 0",
            "message": "TEXT",
            "items_json": "TEXT",
            "latest_season": "INTEGER",
            "latest_episode": "INTEGER",
            "raw_data": "TEXT",
            "expires_at": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        }.items():
            self._ensure_column(conn, "update_preview_cache", column, definition)
        for column, definition in {
            "subscription_id": "INTEGER",
            "openlist_path": "TEXT",
            "files_json": "TEXT",
            "latest_season": "INTEGER",
            "latest_episode": "INTEGER",
            "raw_data": "TEXT",
            "captured_at": "TEXT",
            "expires_at": "TEXT",
        }.items():
            self._ensure_column(conn, "update_path_snapshots", column, definition)
        for column, definition in {
            "subscription_id": "INTEGER",
            "run_id": "INTEGER",
            "level": "TEXT NOT NULL DEFAULT 'info'",
            "message": "TEXT NOT NULL DEFAULT ''",
            "raw_data": "TEXT",
            "created_at": "TEXT",
        }.items():
            self._ensure_column(conn, "update_events", column, definition)

    def _ensure_update_indexes(self, conn: sqlite3.Connection) -> None:
        """在追更字段补齐后再创建索引，兼容早期不完整表。"""

        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_update_subscriptions_status ON update_subscriptions(status)",
            "CREATE INDEX IF NOT EXISTS idx_update_subscriptions_next_run_at ON update_subscriptions(next_run_at)",
            "CREATE INDEX IF NOT EXISTS idx_update_subscriptions_category ON update_subscriptions(category)",
            "CREATE INDEX IF NOT EXISTS idx_update_sources_subscription_id ON update_sources(subscription_id)",
            "CREATE INDEX IF NOT EXISTS idx_update_sources_enabled ON update_sources(enabled)",
            "CREATE INDEX IF NOT EXISTS idx_update_runs_subscription_id ON update_runs(subscription_id)",
            "CREATE INDEX IF NOT EXISTS idx_update_runs_started_at ON update_runs(started_at)",
            "CREATE INDEX IF NOT EXISTS idx_update_runs_status ON update_runs(status)",
            "CREATE INDEX IF NOT EXISTS idx_update_candidates_subscription_id ON update_candidates(subscription_id)",
            "CREATE INDEX IF NOT EXISTS idx_update_candidates_run_id ON update_candidates(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_update_candidates_decision ON update_candidates(decision)",
            "CREATE INDEX IF NOT EXISTS idx_update_candidates_url_hash ON update_candidates(source_url_hash)",
            "CREATE INDEX IF NOT EXISTS idx_update_seen_subscription_id ON update_seen_items(subscription_id)",
            "CREATE INDEX IF NOT EXISTS idx_update_seen_episode ON update_seen_items(subscription_id, season, episode)",
            "CREATE INDEX IF NOT EXISTS idx_update_preview_cache_hash ON update_preview_cache(source_type, source_url_hash)",
            "CREATE INDEX IF NOT EXISTS idx_update_preview_cache_expires_at ON update_preview_cache(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_update_path_snapshots_subscription_id ON update_path_snapshots(subscription_id)",
            "CREATE INDEX IF NOT EXISTS idx_update_path_snapshots_expires_at ON update_path_snapshots(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_update_events_subscription_id ON update_events(subscription_id)",
            "CREATE INDEX IF NOT EXISTS idx_update_events_run_id ON update_events(run_id)",
        ):
            conn.execute(statement)
