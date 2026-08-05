from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso, utc_now_iso_offset
from .update_run_repository import UpdateRunRepository
from .update_subscription_query_repository import UpdateSubscriptionQueryRepository

ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
RowDecoder = Callable[[sqlite3.Row | None], dict[str, Any] | None]
BackupCallable = Callable[..., dict[str, Any]]

HISTORY_CLEANUP_TABLES = (
    "guest_request_events", "guest_requests", "job_events", "rclone_file_events",
    "rclone_events", "rclone_runs", "organizer_operations", "organizer_mappings",
    "organizer_files", "organizer_runs", "organizer_ai_suggestions",
    "organizer_tmdb_matches", "organizer_locks", "organizer_tasks", "import_jobs",
    "resources", "search_cache", "update_candidates", "update_events", "update_runs",
    "update_seen_items", "update_path_snapshots", "update_preview_cache", "worker_tasks",
    "trending_snapshots", "trending_candidates", "trending_discovery_runs",
)
HISTORY_PRESERVED_TABLES = ("app_settings", "update_subscriptions", "update_sources")


def utc_now() -> str:
    return utc_now_iso()


def utc_seconds_from_now(seconds: int) -> str:
    return utc_now_iso_offset(seconds=seconds)


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _decode_json_fields(item: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    for field in fields:
        if item.get(field):
            try:
                item[field] = json.loads(item[field])
            except (TypeError, json.JSONDecodeError):
                pass
    return item


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS total FROM {table}").fetchone()
    return int(row["total"] if row else 0)


class UpdateRepository:
    """Persists update subscriptions, runs, candidates and preview caches.

    Run lease coordination and subscription reads are delegated to
    :class:`UpdateRunRepository` and :class:`UpdateSubscriptionQueryRepository`
    respectively.
    """

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        subscription_queries: UpdateSubscriptionQueryRepository,
        update_runs: UpdateRunRepository,
        decode_run: RowDecoder,
        decode_candidate: RowDecoder,
        backup: BackupCallable,
    ) -> None:
        self._connection_factory = connection_factory
        self._subscription_queries = subscription_queries
        self._update_runs = update_runs
        self._decode_run = decode_run
        self._decode_candidate = decode_candidate
        self._backup = backup

    def create_update_subscription(self, data: dict[str, Any], sources: list[dict[str, Any]] | None = None) -> int:
        subscription_id, _created = self.create_update_subscription_with_outcome(data, sources)
        return subscription_id

    def create_update_subscription_with_outcome(
        self,
        data: dict[str, Any],
        sources: list[dict[str, Any]] | None = None,
    ) -> tuple[int, bool]:
        now = utc_now()
        tmdb_id = int(data.get("tmdb_id") or 0)
        category = str(data.get("category") or "").strip()
        season = data.get("season")
        with self._connection_factory() as conn:
            if tmdb_id and category:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """
                    SELECT id
                    FROM update_subscriptions
                    WHERE tmdb_id = ?
                      AND category = ?
                      AND (season = ? OR (season IS NULL AND ? IS NULL))
                      AND status != 'archived'
                    ORDER BY CASE status WHEN 'enabled' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, id ASC
                    LIMIT 1
                    """,
                    (tmdb_id, category, season, season),
                ).fetchone()
                if existing:
                    return int(existing["id"]), False
            return self._insert_update_subscription_conn(conn, data, sources or [], now), True

    def get_or_create_trending_subscription(
        self,
        candidate_id: int,
        data: dict[str, Any],
        sources: list[dict[str, Any]] | None = None,
    ) -> tuple[int, bool]:
        """Atomically bind a hot candidate to one logical update subscription.

        ``BEGIN IMMEDIATE`` serializes competing hot-candidate requests before the
        identity lookup.  This keeps two browser tabs from both inserting a row
        even on databases created before an optional unique index existed.
        """

        tmdb_id = int(data.get("tmdb_id") or 0)
        category = str(data.get("category") or "").strip()
        season = data.get("season")
        if not tmdb_id or not category:
            raise ValueError("热榜追更缺少 TMDB 或分类信息")
        now = utc_now()
        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                "SELECT id, subscription_id FROM trending_candidates WHERE id = ?",
                (int(candidate_id),),
            ).fetchone()
            if not candidate:
                raise ValueError("热榜候选不存在")

            bound_id = int(candidate["subscription_id"] or 0)
            if bound_id:
                bound = conn.execute(
                    """
                    SELECT id
                    FROM update_subscriptions
                    WHERE id = ?
                      AND tmdb_id = ?
                      AND category = ?
                      AND (season = ? OR (season IS NULL AND ? IS NULL))
                      AND status != 'archived'
                    """,
                    (bound_id, tmdb_id, category, season, season),
                ).fetchone()
                if bound:
                    return bound_id, False
                conn.execute(
                    "UPDATE trending_candidates SET subscription_id = NULL, updated_at = ? WHERE id = ?",
                    (now, int(candidate_id)),
                )

            existing = conn.execute(
                """
                SELECT id
                FROM update_subscriptions
                WHERE tmdb_id = ?
                  AND category = ?
                  AND (season = ? OR (season IS NULL AND ? IS NULL))
                  AND status != 'archived'
                ORDER BY CASE status WHEN 'enabled' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, id ASC
                LIMIT 1
                """,
                (tmdb_id, category, season, season),
            ).fetchone()
            created = existing is None
            subscription_id = (
                self._insert_update_subscription_conn(conn, data, sources or [], now)
                if created
                else int(existing["id"])
            )
            conn.execute(
                "UPDATE trending_candidates SET subscription_id = ?, updated_at = ? WHERE id = ?",
                (subscription_id, now, int(candidate_id)),
            )
            return subscription_id, created

    def bind_trending_candidate_subscription(self, candidate_id: int, subscription_id: int) -> bool:
        """Atomically bind an existing subscription discovered by the hot-title index."""

        with self._connection_factory() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                "SELECT id, subscription_id FROM trending_candidates WHERE id = ?",
                (int(candidate_id),),
            ).fetchone()
            subscription = conn.execute(
                "SELECT id FROM update_subscriptions WHERE id = ? AND status != 'archived'",
                (int(subscription_id),),
            ).fetchone()
            if not candidate or not subscription:
                return False
            current_id = int(candidate["subscription_id"] or 0)
            if current_id and current_id != int(subscription_id):
                return False
            conn.execute(
                "UPDATE trending_candidates SET subscription_id = ?, updated_at = ? WHERE id = ?",
                (int(subscription_id), utc_now(), int(candidate_id)),
            )
            return True

    def _insert_update_subscription_conn(
        self,
        conn: sqlite3.Connection,
        data: dict[str, Any],
        sources: list[dict[str, Any]],
        now: str,
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO update_subscriptions
            (title, category, category_label, media_type, season, year, tmdb_id, query_template,
             aliases, schedule_kind, days_of_week, time_of_day, interval_minutes, timezone,
             next_run_at, last_run_at, last_success_at, next_episode, last_success_episode,
             missing_episodes, source_strategy, auto_import_policy, min_score, quality_profile,
             include_keywords, exclude_keywords, status, raw_data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("title") or "",
                data.get("category") or "movie",
                data.get("category_label") or "",
                data.get("media_type") or "tv",
                data.get("season"),
                data.get("year") or "",
                data.get("tmdb_id"),
                data.get("query_template") or "",
                _json_text(data.get("aliases") or []),
                data.get("schedule_kind") or "weekly",
                _json_text(data.get("days_of_week") or []),
                data.get("time_of_day") or "",
                data.get("interval_minutes"),
                data.get("timezone") or "Asia/Shanghai",
                data.get("next_run_at") or "",
                data.get("last_run_at") or "",
                data.get("last_success_at") or "",
                data.get("next_episode"),
                data.get("last_success_episode"),
                _json_text(data.get("missing_episodes") or []),
                data.get("source_strategy") or "mixed",
                data.get("auto_import_policy") or "auto_high_confidence",
                int(data.get("min_score") or 75),
                data.get("quality_profile") or "",
                _json_text(data.get("include_keywords") or []),
                _json_text(data.get("exclude_keywords") or []),
                data.get("status") or "enabled",
                _json_text(data.get("raw_data")),
                now,
                now,
            ),
        )
        subscription_id = int(cur.lastrowid)
        self._replace_update_sources_conn(conn, subscription_id, sources, now)
        return subscription_id

    def update_update_subscription(self, subscription_id: int, updates: dict[str, Any], sources: list[dict[str, Any]] | None = None) -> None:
        now = utc_now()
        json_fields = {"aliases", "days_of_week", "missing_episodes", "include_keywords", "exclude_keywords", "raw_data"}
        with self._connection_factory() as conn:
            if updates:
                assignments: list[str] = []
                values: list[Any] = []
                for key, value in updates.items():
                    if key in json_fields:
                        value = _json_text(value)
                    assignments.append(f"{key} = ?")
                    values.append(value)
                assignments.append("updated_at = ?")
                values.append(now)
                values.append(subscription_id)
                conn.execute(f"UPDATE update_subscriptions SET {', '.join(assignments)} WHERE id = ?", values)
            if sources is not None:
                conn.execute("DELETE FROM update_sources WHERE subscription_id = ?", (subscription_id,))
                self._replace_update_sources_conn(conn, subscription_id, sources, now)

    def delete_update_subscription(self, subscription_id: int) -> bool:
        with self._connection_factory() as conn:
            row = conn.execute("SELECT id FROM update_subscriptions WHERE id = ?", (subscription_id,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM update_subscriptions WHERE id = ?", (subscription_id,))
            return True

    def _replace_update_sources_conn(self, conn: sqlite3.Connection, subscription_id: int, sources: list[dict[str, Any]], now: str) -> None:
        rows: list[tuple[Any, ...]] = []
        for source in sources:
            rows.append(
                (
                    subscription_id,
                    source.get("type") or "search",
                    source.get("name") or "",
                    source.get("url") or "",
                    source.get("password") or "",
                    source.get("provider") or "",
                    int(source.get("priority") or 100),
                    1 if source.get("enabled", True) else 0,
                    _json_text(source.get("options") or {}),
                    now,
                    now,
                )
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO update_sources
                (subscription_id, type, name, url, password, provider, priority, enabled, options, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def get_update_subscription(self, subscription_id: int, include_sources: bool = True) -> dict[str, Any] | None:
        return self._subscription_queries.get(subscription_id, include_sources)

    def list_update_subscriptions(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        due_before: str | None = None,
        include_sources: bool = False,
    ) -> list[dict[str, Any]]:
        return self._subscription_queries.list(
            limit=limit,
            offset=offset,
            status=status,
            due_before=due_before,
            include_sources=include_sources,
        )

    def count_update_subscriptions(self, status: str | None = None) -> int:
        return self._subscription_queries.count(status)

    def create_update_run(self, subscription_id: int, trigger_type: str, scheduled_at: str = "", raw_data: Any = None) -> int:
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
                INSERT INTO update_runs
                (subscription_id, trigger_type, status, scheduled_at, started_at, summary, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (subscription_id, trigger_type, "running", scheduled_at, utc_now(), None, _json_text(raw_data)),
            )
            return int(cur.lastrowid)

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
        return self._update_runs.claim(
            subscription_id,
            trigger_type,
            scheduled_at=scheduled_at,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            raw_data=raw_data,
        )

    def renew_update_run(self, run_id: int, owner_id: str, *, lease_seconds: int = 120) -> bool:
        return self._update_runs.renew(run_id, owner_id, lease_seconds=lease_seconds)

    def owns_update_run(self, run_id: int, owner_id: str) -> bool:
        return self._update_runs.owns(run_id, owner_id)

    def finish_update_run(
        self,
        run_id: int,
        owner_id: str,
        *,
        status: str,
        **updates: Any,
    ) -> bool:
        return self._update_runs.finish(run_id, owner_id, status=status, updates=updates)

    def recover_stale_update_runs(
        self,
        *,
        older_than_seconds: int = 120,
        message: str = "追更运行因进程中断或租约过期而终止",
    ) -> list[dict[str, Any]]:
        return self._update_runs.recover_stale(older_than_seconds=older_than_seconds, message=message)

    def update_update_run(self, run_id: int, **updates: Any) -> None:
        if not updates:
            return
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key in {"summary", "raw_data", "run_log"}:
                value = _json_text(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(run_id)
        with self._connection_factory() as conn:
            conn.execute(f"UPDATE update_runs SET {', '.join(assignments)} WHERE id = ?", values)

    def append_update_run_log(self, run_id: int, stage: str, message: str, raw_data: Any = None) -> None:
        with self._connection_factory() as conn:
            row = conn.execute("SELECT run_log FROM update_runs WHERE id = ?", (run_id,)).fetchone()
            logs: list[dict[str, Any]] = []
            if row and row["run_log"]:
                try:
                    decoded = json.loads(row["run_log"])
                    if isinstance(decoded, list):
                        logs = [item for item in decoded if isinstance(item, dict)]
                except (TypeError, json.JSONDecodeError):
                    logs = []
            logs.append({"stage": stage, "message": message, "raw_data": raw_data, "created_at": utc_now()})
            conn.execute("UPDATE update_runs SET stage = ?, run_log = ? WHERE id = ?", (stage, _json_text(logs), run_id))

    def get_update_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as conn:
            row = conn.execute("SELECT * FROM update_runs WHERE id = ?", (run_id,)).fetchone()
            return self._decode_run(row)

    def list_update_runs(self, *, subscription_id: int | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        where = ""
        values: list[Any] = []
        if subscription_id:
            where = "WHERE subscription_id = ?"
            values.append(subscription_id)
        values.extend([max(1, int(limit or 50)), max(0, int(offset or 0))])
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT r.*, s.title AS subscription_title, s.category AS subscription_category
                FROM update_runs r
                JOIN update_subscriptions s ON s.id = r.subscription_id
                {where}
                ORDER BY r.id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
            return [self._decode_run(row) for row in rows if row is not None]

    def count_update_runs(self, subscription_id: int | None = None) -> int:
        where = ""
        values: list[Any] = []
        if subscription_id:
            where = "WHERE subscription_id = ?"
            values.append(subscription_id)
        with self._connection_factory() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS total FROM update_runs {where}", values).fetchone()
            return int(row["total"] if row else 0)

    def get_running_update_run(self) -> dict[str, Any] | None:
        return self._update_runs.get_active()

    def create_update_candidate(self, data: dict[str, Any]) -> int:
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
                INSERT INTO update_candidates
                (subscription_id, run_id, job_id, source_id, title, source_type, source_url, source_url_hash,
                 password, season, episode, size_text, published_at, score, decision, reason, raw_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["subscription_id"],
                    data.get("run_id"),
                    data.get("job_id"),
                    data.get("source_id"),
                    data.get("title") or "",
                    data.get("source_type") or "unknown",
                    data.get("source_url") or "",
                    data.get("source_url_hash") or _hash_text(data.get("source_url") or ""),
                    data.get("password") or "",
                    data.get("season"),
                    data.get("episode"),
                    data.get("size_text") or "",
                    data.get("published_at") or "",
                    int(data.get("score") or 0),
                    data.get("decision") or "review",
                    data.get("reason") or "",
                    _json_text(data.get("raw_data")),
                    utc_now(),
                ),
            )
            return int(cur.lastrowid)

    def update_update_candidate(self, candidate_id: int, **updates: Any) -> None:
        if not updates:
            return
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key == "raw_data":
                value = _json_text(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(candidate_id)
        with self._connection_factory() as conn:
            conn.execute(f"UPDATE update_candidates SET {', '.join(assignments)} WHERE id = ?", values)

    def get_update_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as conn:
            row = conn.execute("SELECT * FROM update_candidates WHERE id = ?", (candidate_id,)).fetchone()
            return self._decode_candidate(row)

    def list_update_candidates(
        self,
        *,
        subscription_id: int | None = None,
        run_id: int | None = None,
        decision: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        values: list[Any] = []
        if subscription_id:
            where.append("c.subscription_id = ?")
            values.append(subscription_id)
        if run_id:
            where.append("c.run_id = ?")
            values.append(run_id)
        if decision:
            where.append("c.decision = ?")
            values.append(decision)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        values.extend([max(1, int(limit or 100)), max(0, int(offset or 0))])
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT c.*, s.title AS subscription_title, j.status AS job_status
                FROM update_candidates c
                JOIN update_subscriptions s ON s.id = c.subscription_id
                LEFT JOIN import_jobs j ON j.id = c.job_id
                {where_sql}
                ORDER BY c.id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
            return [self._decode_candidate(row) for row in rows if row is not None]

    def count_update_candidates(self, *, subscription_id: int | None = None, run_id: int | None = None, decision: str | None = None) -> int:
        where: list[str] = []
        values: list[Any] = []
        if subscription_id:
            where.append("subscription_id = ?")
            values.append(subscription_id)
        if run_id:
            where.append("run_id = ?")
            values.append(run_id)
        if decision:
            where.append("decision = ?")
            values.append(decision)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._connection_factory() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS total FROM update_candidates {where_sql}", values).fetchone()
            return int(row["total"] if row else 0)

    def upsert_update_seen_item(self, data: dict[str, Any]) -> bool:
        now = utc_now()
        with self._connection_factory() as conn:
            existing = conn.execute(
                "SELECT id FROM update_seen_items WHERE subscription_id = ? AND fingerprint = ?",
                (data["subscription_id"], data["fingerprint"]),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE update_seen_items SET last_seen_at = ?, raw_data = COALESCE(?, raw_data) WHERE id = ?",
                    (now, _json_text(data.get("raw_data")), existing["id"]),
                )
                return False
            conn.execute(
                """
                INSERT INTO update_seen_items
                (subscription_id, fingerprint, source_type, source_url_hash, file_id, file_name,
                 size, season, episode, first_seen_at, last_seen_at, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["subscription_id"],
                    data["fingerprint"],
                    data.get("source_type") or "",
                    data.get("source_url_hash") or "",
                    data.get("file_id") or "",
                    data.get("file_name") or "",
                    data.get("size"),
                    data.get("season"),
                    data.get("episode"),
                    now,
                    now,
                    _json_text(data.get("raw_data")),
                ),
            )
            return True

    def get_update_preview_cache(self, source_type: str, source_url: str, *, now: str | None = None) -> dict[str, Any] | None:
        now_text = now or utc_now()
        with self._connection_factory() as conn:
            row = conn.execute(
                """
                SELECT * FROM update_preview_cache
                WHERE source_type = ? AND source_url_hash = ? AND expires_at > ?
                LIMIT 1
                """,
                (source_type, _hash_text(source_url), now_text),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["ok"] = bool(item.get("ok"))
            return _decode_json_fields(item, ("items_json", "raw_data"))

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
        now = utc_now()
        expires_at = utc_seconds_from_now(max(60, int(ttl_seconds or 3600)))
        source_url_hash = _hash_text(source_url)
        row_values = (
            source_url,
            1 if ok else 0,
            message,
            _json_text(items or []),
            latest_season,
            latest_episode,
            _json_text(raw_data),
            expires_at,
            now,
        )
        with self._connection_factory() as conn:
            existing = conn.execute(
                """
                SELECT id FROM update_preview_cache
                WHERE source_type = ? AND source_url_hash = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (source_type, source_url_hash),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE update_preview_cache
                    SET source_url = ?, ok = ?, message = ?, items_json = ?,
                        latest_season = ?, latest_episode = ?, raw_data = ?,
                        expires_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (*row_values, existing["id"]),
                )
                return
            conn.execute(
                """
                INSERT INTO update_preview_cache
                (source_type, source_url_hash, source_url, ok, message, items_json,
                 latest_season, latest_episode, raw_data, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_type, source_url_hash, *row_values[:8], now, now),
            )

    def get_update_path_snapshot(self, subscription_id: int, openlist_path: str, *, now: str | None = None) -> dict[str, Any] | None:
        now_text = now or utc_now()
        with self._connection_factory() as conn:
            row = conn.execute(
                """
                SELECT * FROM update_path_snapshots
                WHERE subscription_id = ? AND openlist_path = ? AND expires_at > ?
                LIMIT 1
                """,
                (subscription_id, openlist_path, now_text),
            ).fetchone()
            if not row:
                return None
            return _decode_json_fields(dict(row), ("files_json", "raw_data"))

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
        now = utc_now()
        expires_at = utc_seconds_from_now(max(60, int(ttl_seconds or 1800)))
        files_text = _json_text(files)
        raw_text = _json_text(raw_data)
        with self._connection_factory() as conn:
            existing = conn.execute(
                """
                SELECT id FROM update_path_snapshots
                WHERE subscription_id = ? AND openlist_path = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (subscription_id, openlist_path),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE update_path_snapshots
                    SET files_json = ?, latest_season = ?, latest_episode = ?,
                        raw_data = ?, captured_at = ?, expires_at = ?
                    WHERE id = ?
                    """,
                    (files_text, latest_season, latest_episode, raw_text, now, expires_at, existing["id"]),
                )
                return
            conn.execute(
                """
                INSERT INTO update_path_snapshots
                (subscription_id, openlist_path, files_json, latest_season, latest_episode, raw_data, captured_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (subscription_id, openlist_path, files_text, latest_season, latest_episode, raw_text, now, expires_at),
            )

    def update_seen_episode_exists(self, subscription_id: int, season: int | None, episode: int | None) -> bool:
        if not episode:
            return False
        with self._connection_factory() as conn:
            row = conn.execute(
                """
                SELECT id FROM update_seen_items
                WHERE subscription_id = ?
                  AND (season = ? OR (season IS NULL AND ? IS NULL))
                  AND episode = ?
                LIMIT 1
                """,
                (subscription_id, season, season, episode),
            ).fetchone()
            return row is not None

    def list_update_seen_episodes(self, subscription_id: int) -> set[tuple[int | None, int]]:
        with self._connection_factory() as conn:
            rows = conn.execute(
                """
                SELECT season, episode FROM update_seen_items
                WHERE subscription_id = ? AND episode IS NOT NULL
                """,
                (subscription_id,),
            ).fetchall()
        result: set[tuple[int | None, int]] = set()
        for row in rows:
            try:
                episode = int(row["episode"])
            except (TypeError, ValueError):
                continue
            if episode <= 0:
                continue
            try:
                season = int(row["season"]) if row["season"] not in (None, "") else None
            except (TypeError, ValueError):
                season = None
            result.add((season, episode))
        return result

    def add_update_event(self, subscription_id: int | None, run_id: int | None, level: str, message: str, raw_data: Any = None) -> int:
        with self._connection_factory() as conn:
            cur = conn.execute(
                """
                INSERT INTO update_events (subscription_id, run_id, level, message, raw_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (subscription_id, run_id, level, message, _json_text(raw_data), utc_now()),
            )
            return int(cur.lastrowid)

    def list_update_events(self, *, subscription_id: int | None = None, run_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where: list[str] = []
        values: list[Any] = []
        if subscription_id:
            where.append("subscription_id = ?")
            values.append(subscription_id)
        if run_id:
            where.append("run_id = ?")
            values.append(run_id)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        values.append(max(1, int(limit or 100)))
        with self._connection_factory() as conn:
            rows = conn.execute(f"SELECT * FROM update_events {where_sql} ORDER BY id DESC LIMIT ?", values).fetchall()
            return [_decode_json_fields(dict(row), ("raw_data",)) for row in rows]

    def history_cleanup_summary(self) -> dict[str, Any]:
        with self._connection_factory() as conn:
            cleanup_counts = {table: _table_count(conn, table) for table in HISTORY_CLEANUP_TABLES}
            preserved_counts = {table: _table_count(conn, table) for table in HISTORY_PRESERVED_TABLES}
            subscriptions = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, title, status, category, category_label, season,
                           next_episode, last_success_episode, next_run_at, updated_at
                    FROM update_subscriptions
                    ORDER BY id ASC
                    """
                ).fetchall()
            ]
            sources = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, subscription_id, type, name, enabled, priority, updated_at
                    FROM update_sources
                    ORDER BY subscription_id ASC, priority ASC, id ASC
                    """
                ).fetchall()
            ]
        total_cleanup_rows = sum(int(value or 0) for value in cleanup_counts.values())
        return {
            "cleanup_tables": cleanup_counts,
            "preserved_tables": preserved_counts,
            "cleanup_total": total_cleanup_rows,
            "preserved_subscriptions": subscriptions,
            "preserved_sources": sources,
            "will_clear": list(HISTORY_CLEANUP_TABLES),
            "will_preserve": list(HISTORY_PRESERVED_TABLES),
        }

    def cleanup_history_records(self, *, backup: bool = True, vacuum: bool = True, backup_prefix: str = "app_before_history_cleanup") -> dict[str, Any]:
        backup_result = self._backup(prefix=backup_prefix) if backup else {}
        before = self.history_cleanup_summary()
        with self._connection_factory() as conn:
            for table in HISTORY_CLEANUP_TABLES:
                conn.execute(f"DELETE FROM {table}")
            placeholders = ",".join(["?"] * len(HISTORY_CLEANUP_TABLES))
            conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", list(HISTORY_CLEANUP_TABLES))
        vacuum_error = ""
        if vacuum:
            try:
                with self._connection_factory() as conn:
                    conn.execute("VACUUM")
            except sqlite3.Error as exc:
                vacuum_error = str(exc)
        after = self.history_cleanup_summary()
        return {
            "backup": backup_result,
            "before": before,
            "after": after,
            "vacuum_error": vacuum_error,
            "cleared_total": int(before.get("cleanup_total") or 0) - int(after.get("cleanup_total") or 0),
        }
