from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any, Callable

import sqlite3

from ..time_utils import utc_now_iso


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


def _now() -> str:
    return utc_now_iso()


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _decode(value: Any) -> Any:
    if value in (None, ""):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _candidate_year(value: Any) -> str:
    text = str(value or "").strip()
    return text if len(text) == 4 and text.isdigit() else ""


class TrendingRepository:
    """Persistence for hot-content discovery runs, snapshots and candidates."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for key in ("summary", "raw_data", "platform_ranks"):
            if key in item and item[key] is not None:
                item[key] = _decode(item[key])
        for key in ("is_completed", "media_exists"):
            if key in item:
                item[key] = bool(item[key])
        return item

    def create_run(self, *, trigger_type: str = "scheduled", source_count: int = 0, **data: Any) -> int:
        now = _now()
        values = {
            "trigger_type": trigger_type,
            "status": data.get("status", "running"),
            "started_at": data.get("started_at", now),
            "source_count": int(source_count or data.get("source_count", 0)),
            "success_source_count": int(data.get("success_source_count", 0)),
            "raw_item_count": int(data.get("raw_item_count", 0)),
            "candidate_count": int(data.get("candidate_count", 0)),
            "auto_subscribed_count": int(data.get("auto_subscribed_count", 0)),
            "review_count": int(data.get("review_count", 0)),
            "error_message": data.get("error_message"),
            "summary": _json(data.get("summary")),
        }
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trending_discovery_runs
                (trigger_type, status, started_at, source_count, success_source_count, raw_item_count,
                 candidate_count, auto_subscribed_count, review_count, error_message, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(values[key] for key in ("trigger_type", "status", "started_at", "source_count", "success_source_count", "raw_item_count", "candidate_count", "auto_subscribed_count", "review_count", "error_message", "summary")),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, status: str = "success", **updates: Any) -> bool:
        allowed = {
            "raw_item_count", "candidate_count", "auto_subscribed_count", "review_count", "source_count", "success_source_count", "error_message", "summary"
        }
        assignments = ["status = ?", "finished_at = ?"]
        values: list[Any] = [str(status), updates.pop("finished_at", _now())]
        for key, value in updates.items():
            if key in allowed:
                assignments.append(f"{key} = ?")
                values.append(_json(value) if key == "summary" else value)
        values.append(int(run_id))
        with self._connection_factory() as connection:
            cursor = connection.execute(
                f"UPDATE trending_discovery_runs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            return cursor.rowcount > 0

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            row = connection.execute("SELECT * FROM trending_discovery_runs WHERE id = ?", (int(run_id),)).fetchone()
        return self._row(row)

    def list_runs(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._connection_factory() as connection:
            rows = connection.execute(
                "SELECT * FROM trending_discovery_runs ORDER BY id DESC LIMIT ? OFFSET ?",
                (max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def count_runs(self) -> int:
        with self._connection_factory() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM trending_discovery_runs").fetchone()
        return int(row["total"] if row else 0)

    def get_latest_run(self) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT * FROM trending_discovery_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._row(row)

    def upsert_snapshot(self, *, run_id: int | None = None, item: dict[str, Any] | None = None, **kwargs: Any) -> int:
        payload = dict(item or {})
        payload.update(kwargs)
        now = _now()
        source = str(payload.get("source") or "unknown")
        source_id = str(payload.get("source_id") or payload.get("series_id") or payload.get("id") or payload.get("title") or "")
        snapshot_date = str(payload.get("snapshot_date") or now[:10])
        with self._connection_factory() as connection:
            connection.execute(
                """
                INSERT INTO trending_snapshots
                (run_id, snapshot_date, source, source_id, title, original_title, year,
                 media_type, rank, heat, score, update_text, platform, is_completed,
                 image_url, raw_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date, source, source_id) DO UPDATE SET
                 run_id=excluded.run_id, title=excluded.title, original_title=excluded.original_title,
                 year=excluded.year, media_type=excluded.media_type, rank=excluded.rank,
                 heat=excluded.heat, score=excluded.score, update_text=excluded.update_text,
                 platform=excluded.platform, is_completed=excluded.is_completed,
                 image_url=excluded.image_url, raw_data=excluded.raw_data
                """,
                (
                    run_id, snapshot_date, source, source_id, payload.get("title") or "",
                    payload.get("original_title"), payload.get("year"), payload.get("media_type"),
                    payload.get("rank"), payload.get("heat"), payload.get("score"),
                    payload.get("update_text"), payload.get("platform"), int(bool(payload.get("is_completed"))),
                    payload.get("image_url"), _json(payload.get("raw_data", payload)), now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM trending_snapshots WHERE snapshot_date = ? AND source = ? AND source_id = ?",
                (snapshot_date, source, source_id),
            ).fetchone()
            return int(row["id"])

    def list_snapshots(self, *, snapshot_date: str | None = None, source: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        where: list[str] = []
        values: list[Any] = []
        if snapshot_date:
            where.append("snapshot_date = ?"); values.append(snapshot_date)
        if source:
            where.append("source = ?"); values.append(source)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        values.extend([max(1, int(limit)), max(0, int(offset))])
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"SELECT * FROM trending_snapshots {clause} ORDER BY snapshot_date DESC, rank ASC, id DESC LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def upsert_candidate(self, *, item: dict[str, Any] | None = None, **kwargs: Any) -> int:
        payload = dict(item or {})
        payload.update(kwargs)
        now = _now()
        source = str(payload.get("source") or "unknown")
        source_id = str(payload.get("source_id") or payload.get("series_id") or payload.get("id") or "")
        canonical_key = str(payload.get("canonical_key") or (f"{source}:{source_id}" if source_id else payload.get("title") or ""))
        with self._connection_factory() as connection:
            legacy_canonical_key = str(payload.get("legacy_canonical_key") or "").strip()
            if legacy_canonical_key:
                canonical_key, migrated_year = self._migrate_legacy_candidate_key(
                    connection,
                    legacy_canonical_key=legacy_canonical_key,
                    canonical_key=canonical_key,
                    year=payload.get("year"),
                )
                if migrated_year and not _candidate_year(payload.get("year")):
                    payload["year"] = int(migrated_year)
            connection.execute(
                """
                INSERT INTO trending_candidates
                (canonical_key, source, source_id, title, original_title, year, media_type,
                 rank, heat, score, platform, platform_ranks, status, first_seen_at,
                 last_seen_at, media_exists, subscription_id, initial_import_job_id,
                 ignore_until, ignore_reason, raw_data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                 source=excluded.source, source_id=excluded.source_id, title=excluded.title,
                 original_title=excluded.original_title, year=excluded.year, media_type=excluded.media_type,
                 rank=excluded.rank, heat=excluded.heat, score=excluded.score, platform=excluded.platform,
                 platform_ranks=excluded.platform_ranks,
                 status=CASE
                    WHEN trending_candidates.status IN ('ignored', 'importing', 'imported', 'import_failed')
                    THEN trending_candidates.status
                    ELSE excluded.status
                 END,
                 last_seen_at=excluded.last_seen_at,
                 media_exists=excluded.media_exists, subscription_id=COALESCE(excluded.subscription_id, trending_candidates.subscription_id),
                 initial_import_job_id=COALESCE(excluded.initial_import_job_id, trending_candidates.initial_import_job_id),
                 ignore_until=COALESCE(excluded.ignore_until, trending_candidates.ignore_until),
                 ignore_reason=COALESCE(excluded.ignore_reason, trending_candidates.ignore_reason),
                 raw_data=excluded.raw_data, updated_at=excluded.updated_at
                """,
                (
                    canonical_key, source, source_id, payload.get("title") or "", payload.get("original_title"),
                    payload.get("year"), payload.get("media_type"), payload.get("rank"), payload.get("heat"),
                    payload.get("score"), payload.get("platform"), _json(payload.get("platform_ranks")),
                    payload.get("status", "discovered"), payload.get("first_seen_at", now), payload.get("last_seen_at", now),
                    int(bool(payload.get("media_exists"))), payload.get("subscription_id"), payload.get("initial_import_job_id"),
                    payload.get("ignore_until"), payload.get("ignore_reason"), _json(payload.get("raw_data", payload)), now, now,
                ),
            )
            row = connection.execute("SELECT id FROM trending_candidates WHERE canonical_key = ?", (canonical_key,)).fetchone()
            return int(row["id"])

    @staticmethod
    def _migrate_legacy_candidate_key(
        connection: sqlite3.Connection,
        *,
        legacy_canonical_key: str,
        canonical_key: str,
        year: Any,
    ) -> tuple[str, str]:
        """Move a pre-year identity to the year-aware key without duplicating it."""

        prefix = f"{legacy_canonical_key}|"
        year_aware = connection.execute(
            """
            SELECT canonical_key, year
            FROM trending_candidates
            WHERE substr(canonical_key, 1, ?) = ?
              AND year IS NOT NULL AND year != ''
            ORDER BY id ASC
            """,
            (len(prefix), prefix),
        ).fetchall()
        identities = {
            (str(row["canonical_key"]), _candidate_year(row["year"]))
            for row in year_aware
            if _candidate_year(row["year"])
        }
        legacy = connection.execute(
            """
            SELECT id, year, status, subscription_id, initial_import_job_id,
                   ignore_until, ignore_reason, first_seen_at, last_seen_at
            FROM trending_candidates
            WHERE canonical_key = ?
            """,
            (legacy_canonical_key,),
        ).fetchone()
        current_year = _candidate_year(year)
        canonical_key = f"{legacy_canonical_key}|{current_year}" if current_year else legacy_canonical_key
        if not legacy:
            if current_year:
                return canonical_key, current_year
            if len(identities) == 1:
                return next(iter(identities))
            return legacy_canonical_key, ""

        legacy_year = _candidate_year(legacy["year"])
        known_years = {identity_year for _identity_key, identity_year in identities}

        if current_year:
            if legacy_year and legacy_year != current_year:
                TrendingRepository._move_legacy_candidate_row(
                    connection,
                    legacy=legacy,
                    canonical_key=f"{legacy_canonical_key}|{legacy_year}",
                )
                return canonical_key, current_year
            if not legacy_year and any(known_year != current_year for known_year in known_years):
                return canonical_key, current_year
            TrendingRepository._move_legacy_candidate_row(connection, legacy=legacy, canonical_key=canonical_key)
            return canonical_key, current_year

        if legacy_year:
            canonical_key = f"{legacy_canonical_key}|{legacy_year}"
            TrendingRepository._move_legacy_candidate_row(connection, legacy=legacy, canonical_key=canonical_key)
            if len(known_years | {legacy_year}) > 1:
                return legacy_canonical_key, ""
            return canonical_key, legacy_year

        if len(identities) == 1:
            canonical_key, current_year = next(iter(identities))
            TrendingRepository._move_legacy_candidate_row(connection, legacy=legacy, canonical_key=canonical_key)
            return canonical_key, current_year
        return legacy_canonical_key, ""

    @staticmethod
    def _move_legacy_candidate_row(
        connection: sqlite3.Connection,
        *,
        legacy: sqlite3.Row,
        canonical_key: str,
    ) -> None:
        """Preserve the legacy row id while folding in an existing year-aware row."""

        current = connection.execute(
            """
            SELECT id, status, subscription_id, initial_import_job_id,
                   ignore_until, ignore_reason, first_seen_at, last_seen_at
            FROM trending_candidates
            WHERE canonical_key = ?
            """,
            (canonical_key,),
        ).fetchone()
        if current and int(current["id"]) != int(legacy["id"]):
            protected_statuses = {"ignored", "importing", "imported", "import_failed"}
            legacy_status = str(legacy["status"] or "")
            current_status = str(current["status"] or "")
            merged_status = (
                legacy_status
                if legacy_status in protected_statuses
                else current_status
                if current_status in protected_statuses
                else legacy_status or current_status or "discovered"
            )
            connection.execute(
                """
                UPDATE trending_candidates
                SET status = ?,
                    subscription_id = COALESCE(subscription_id, ?),
                    initial_import_job_id = COALESCE(initial_import_job_id, ?),
                    ignore_until = COALESCE(ignore_until, ?),
                    ignore_reason = COALESCE(ignore_reason, ?),
                    first_seen_at = MIN(first_seen_at, ?),
                    last_seen_at = MAX(last_seen_at, ?)
                WHERE id = ?
                """,
                (
                    merged_status,
                    current["subscription_id"],
                    current["initial_import_job_id"],
                    current["ignore_until"],
                    current["ignore_reason"],
                    current["first_seen_at"],
                    current["last_seen_at"],
                    int(legacy["id"]),
                ),
            )
            connection.execute("DELETE FROM trending_candidates WHERE id = ?", (int(current["id"]),))
        connection.execute(
            "UPDATE trending_candidates SET canonical_key = ? WHERE id = ?",
            (canonical_key, int(legacy["id"])),
        )

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        with self._connection_factory() as connection:
            row = connection.execute("SELECT * FROM trending_candidates WHERE id = ?", (int(candidate_id),)).fetchone()
        return self._row(row)

    def list_candidates(
        self,
        *,
        status: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        last_run_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        values: list[Any] = []
        if status:
            where.append("status = ?"); values.append(status)
        if media_type:
            where.append("media_type = ?"); values.append(media_type)
        if source:
            where.append(
                "(source = ? OR EXISTS (SELECT 1 FROM json_each(trending_candidates.platform_ranks) WHERE key = ?))"
            )
            values.extend([source, source])
        if last_run_id is not None:
            where.append("CAST(json_extract(raw_data, '$.last_run_id') AS INTEGER) = ?")
            values.append(int(last_run_id))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        values.extend([max(1, int(limit)), max(0, int(offset))])
        with self._connection_factory() as connection:
            rows = connection.execute(
                f"""SELECT * FROM trending_candidates {clause}
                ORDER BY CASE WHEN rank IS NULL OR rank <= 0 THEN 1 ELSE 0 END,
                         rank ASC, id DESC LIMIT ? OFFSET ?""",
                values,
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def count_candidates(
        self,
        *,
        status: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        last_run_id: int | None = None,
    ) -> int:
        where: list[str] = []
        values: list[Any] = []
        if status:
            where.append("status = ?"); values.append(status)
        if media_type:
            where.append("media_type = ?"); values.append(media_type)
        if source:
            where.append(
                "(source = ? OR EXISTS (SELECT 1 FROM json_each(trending_candidates.platform_ranks) WHERE key = ?))"
            )
            values.extend([source, source])
        if last_run_id is not None:
            where.append("CAST(json_extract(raw_data, '$.last_run_id') AS INTEGER) = ?")
            values.append(int(last_run_id))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connection_factory() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS total FROM trending_candidates {clause}", values).fetchone()
        return int(row["total"] if row else 0)

    def update_candidate(self, candidate_id: int, updates: dict[str, Any] | None = None, **kwargs: Any) -> bool:
        values_map = dict(updates or {}); values_map.update(kwargs)
        allowed = {"status", "media_exists", "subscription_id", "initial_import_job_id", "ignore_until", "ignore_reason", "title", "rank", "heat", "score", "raw_data"}
        assignments: list[str] = []; values: list[Any] = []
        for key, value in values_map.items():
            if key in allowed:
                assignments.append(f"{key} = ?"); values.append(_json(value) if key == "raw_data" else (int(bool(value)) if key == "media_exists" else value))
        if not assignments:
            return False
        assignments.append("updated_at = ?"); values.append(_now()); values.append(int(candidate_id))
        with self._connection_factory() as connection:
            cursor = connection.execute(f"UPDATE trending_candidates SET {', '.join(assignments)} WHERE id = ?", values)
            return cursor.rowcount > 0

    def claim_candidate_for_initial_import(
        self,
        candidate_id: int,
        *,
        allowed_statuses: tuple[str, ...] = ("discovered", "import_failed"),
    ) -> dict[str, Any] | None:
        statuses = tuple(dict.fromkeys(str(value).strip() for value in allowed_statuses if str(value).strip()))
        if not statuses:
            return None
        unsupported = set(statuses) - {"discovered", "import_failed"}
        if unsupported:
            raise ValueError("invalid initial import claim status")
        claim_conditions: list[str] = []
        if "discovered" in statuses:
            claim_conditions.append("(status = 'discovered' AND initial_import_job_id IS NULL)")
        if "import_failed" in statuses:
            claim_conditions.append("status = 'import_failed'")
        claim_condition = " OR ".join(claim_conditions)
        now = _now()
        with self._connection_factory() as connection:
            # Serialize the claim across web workers before checking status so
            # only one request can start an initial import for a candidate.
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE trending_candidates
                SET status = 'importing',
                    initial_import_job_id = CASE
                        WHEN status = 'import_failed' THEN NULL
                        ELSE initial_import_job_id
                    END,
                    updated_at = ?
                WHERE id = ?
                  AND ({claim_condition})
                """,
                (now, int(candidate_id)),
            )
            if cursor.rowcount <= 0:
                return None
            row = connection.execute(
                "SELECT * FROM trending_candidates WHERE id = ?",
                (int(candidate_id),),
            ).fetchone()
            return self._row(row)

    def bind_candidate_initial_import_job(
        self,
        candidate_id: int,
        job_id: int,
        *,
        status: str = "importing",
    ) -> bool:
        target_status = str(status or "importing").strip()
        if target_status not in {"importing", "imported", "import_failed"}:
            raise ValueError("invalid initial import status")
        now = _now()
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT initial_import_job_id, status FROM trending_candidates WHERE id = ?",
                (int(candidate_id),),
            ).fetchone()
            if current is None:
                return False
            current_job_id = int(current["initial_import_job_id"] or 0)
            current_status = str(current["status"] or "")
            # A candidate can only advance from importing. Once a terminal
            # state is stored, repeating the same write is idempotent, while
            # a different job cannot take ownership of it.
            if current_status in {"imported", "import_failed"}:
                return current_job_id == int(job_id) and current_status == target_status
            if current_status != "importing" or (current_job_id and current_job_id != int(job_id)):
                return False
            cursor = connection.execute(
                """
                UPDATE trending_candidates
                SET initial_import_job_id = ?, status = ?, updated_at = ?
                WHERE id = ? AND status = 'importing'
                  AND (initial_import_job_id IS NULL OR initial_import_job_id = ?)
                """,
                (int(job_id), target_status, now, int(candidate_id), int(job_id)),
            )
            if cursor.rowcount > 0:
                return True
            return False

    def release_candidate_initial_import(
        self,
        candidate_id: int,
        *,
        status: str = "discovered",
    ) -> bool:
        target_status = str(status or "discovered").strip()
        if target_status not in {"discovered", "import_failed"}:
            raise ValueError("invalid initial import release status")
        with self._connection_factory() as connection:
            cursor = connection.execute(
                """
                UPDATE trending_candidates
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = 'importing' AND initial_import_job_id IS NULL
                """,
                (target_status, _now(), int(candidate_id)),
            )
            return cursor.rowcount > 0
