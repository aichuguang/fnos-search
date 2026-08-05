from __future__ import annotations

import logging
import math
import re
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any


logger = logging.getLogger(__name__)

MEDIA_TYPE_ALIASES = {
    "tv": "tv", "teleplay": "tv", "series": "tv",
    "\u7535\u89c6\u5267": "tv", "\u5267\u96c6": "tv", "\u7f51\u7edc\u5267": "tv",
    "movie": "movie", "film": "movie", "\u7535\u5f71": "movie", "\u7f51\u7edc\u7535\u5f71": "movie",
    "anime": "anime", "animation": "anime", "cartoon": "anime",
    "\u52a8\u6f2b": "anime", "\u52a8\u753b": "anime", "\u56fd\u6f2b": "anime",
    "variety": "variety", "show": "variety", "\u7efc\u827a": "variety", "\u771f\u4eba\u79c0": "variety",
}



class TrendingDiscoveryService:
    """聚合热榜来源，仅生成发现快照与候选，不触发下载。"""

    lease_name = "trending-discovery-scheduler"

    def __init__(
        self,
        *,
        sources: Iterable[Any],
        repository: Any,
        media_exists: Callable[[dict[str, Any]], bool] | None = None,
        task_exists: Callable[[dict[str, Any]], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        log: logging.Logger | None = None,
        owner_id: str = "",
        lease_seconds: int = 900,
        max_items_per_source: int = 20,
    ) -> None:
        self.sources = list(sources)
        self.repository = repository
        self.media_exists = media_exists or (lambda _item: False)
        self.task_exists = task_exists or (lambda _item: False)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.log = log or logger
        self.owner_id = str(owner_id or f"trending-discovery-{id(self)}")
        self.lease_seconds = max(60, int(lease_seconds or 900))
        self.max_items_per_source = max(1, int(max_items_per_source or 20))
        self._run_lock = threading.Lock()
        self._running = False
        self._last_result: dict[str, Any] | None = None

    def run(self, *, trigger_type: str = "manual", lease_held: bool = False) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"success": True, "skipped": True, "message": "热榜发现任务正在运行"}
        acquired_lease = False
        try:
            if not lease_held:
                acquire = getattr(self.repository, "acquire_scheduler_lease", None)
                if callable(acquire):
                    acquired_lease = bool(acquire(self.lease_name, self.owner_id, self.lease_seconds))
                    if not acquired_lease:
                        return {"success": True, "skipped": True, "message": "其他进程正在执行热榜发现"}
            self._running = True
            result = self._execute(trigger_type=trigger_type)
            self._last_result = result
            return result
        finally:
            self._running = False
            if acquired_lease:
                release = getattr(self.repository, "release_scheduler_lease", None)
                if callable(release):
                    try:
                        release(self.lease_name, self.owner_id)
                    except Exception:  # noqa: BLE001
                        self.log.exception("release trending discovery lease failed")
            self._run_lock.release()

    def _execute(self, *, trigger_type: str) -> dict[str, Any]:
        started_at = self._time_text(self.clock())
        run_id = self.repository.create_trending_run(
            trigger_type=str(trigger_type or "manual"),
            source_count=len(self.sources),
            started_at=started_at,
        )
        source_results: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        errors: list[str] = []

        for source in self.sources:
            source_name = self._source_name(source)
            try:
                fetched = self._fetch(source)
                normalized = self._deduplicate_source(
                    self._normalize_item(item, source_name=source_name) for item in fetched
                )
                grouped: dict[str, list[dict[str, Any]]] = {}
                for item in normalized:
                    grouped.setdefault(str(item.get("media_type") or "unknown"), []).append(item)
                normalized = []
                for media_type in sorted(grouped):
                    rows = grouped[media_type]
                    rows.sort(key=lambda item: (self._rank(item), str(item.get("title") or "")))
                    normalized.extend(rows[: self.max_items_per_source])
                snapshots.extend(normalized)
                source_results.append({"source": source_name, "success": True, "count": len(normalized)})
            except Exception as exc:  # noqa: BLE001
                message = f"{source_name}: {exc}"
                errors.append(message)
                source_results.append({"source": source_name, "success": False, "count": 0, "error": str(exc)})
                self.log.exception("trending source failed: %s", source_name)

        succeeded = sum(1 for item in source_results if item["success"])
        status = "success" if succeeded == len(self.sources) else "partial" if succeeded else "failed"
        candidates = self._cap_candidates_by_category(self._merge_candidates(snapshots))
        self._assign_category_ranks(candidates)
        saved_candidates = 0

        try:
            for item in snapshots:
                self.repository.upsert_trending_snapshot(run_id=run_id, item=item)
            for item in candidates:
                item_raw = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
                item_raw["last_run_id"] = run_id
                item["raw_data"] = item_raw
                item["media_exists"] = bool(self.media_exists(item))
                item["task_exists"] = bool(self.task_exists(item))
                if item["media_exists"]:
                    item["status"] = "already_exists"
                elif item["task_exists"]:
                    item["status"] = "task_exists"
                else:
                    item["status"] = "discovered"
                self.repository.upsert_trending_candidate(item=item)
                saved_candidates += 1
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            errors.append(f"persistence: {exc}")
            self.log.exception("persist trending discovery failed")

        finished_at = self._time_text(self.clock())
        self.repository.finish_trending_run(
            run_id=run_id,
            status=status,
            finished_at=finished_at,
            raw_item_count=len(snapshots),
            candidate_count=saved_candidates,
            success_source_count=succeeded,
            error_message="; ".join(errors),
            summary={"sources": source_results},
        )
        return {
            "success": status != "failed",
            "run_id": run_id,
            "status": status,
            "source_count": len(self.sources),
            "success_source_count": succeeded,
            "raw_item_count": len(snapshots),
            "candidate_count": saved_candidates,
            "sources": source_results,
            "errors": errors,
        }

    def status(self) -> dict[str, Any]:
        latest = None
        getter = getattr(self.repository, "get_latest_trending_run", None)
        if callable(getter):
            latest = getter()
        else:
            list_runs = getattr(self.repository, "list_trending_runs", None)
            if callable(list_runs):
                rows = list_runs(limit=1, offset=0)
                latest = rows[0] if rows else None
        return {"running": self._running, "latest_run": latest, "last_result": self._last_result}

    def list_candidates(
        self,
        *,
        page: int = 1,
        per_page: int = 50,
        status: str | None = None,
        media_type: str | None = None,
        source: str | None = None,
        last_run_id: int | None = None,
    ) -> dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_per_page = min(200, max(1, int(per_page or 50)))
        filters = {
            "status": status,
            "media_type": media_type,
            "source": source,
            "last_run_id": last_run_id,
        }
        try:
            items = self.repository.list_trending_candidates(
                limit=safe_per_page,
                offset=(safe_page - 1) * safe_per_page,
                **filters,
            )
            total = self.repository.count_trending_candidates(**filters)
        except TypeError:
            # Compatibility with repositories created before current-run and
            # multi-platform source filtering were added.
            filters.pop("last_run_id", None)
            filters.pop("source", None)
            items = self.repository.list_trending_candidates(
                limit=safe_per_page,
                offset=(safe_page - 1) * safe_per_page,
                **filters,
            )
            total = self.repository.count_trending_candidates(**filters)
        return {"items": items, "total": total, "page": safe_page, "per_page": safe_per_page}

    def grouped_candidates(
        self,
        *,
        status: str | None = None,
        source: str | None = None,
        limit: int = 20,
    ) -> dict[str, dict[str, Any]]:
        safe_limit = min(25, max(1, int(limit or 20)))
        latest = self.status().get("latest_run") or {}
        if isinstance(latest, Mapping) and str(latest.get("status") or "") not in {"success", "partial"}:
            list_runs = getattr(self.repository, "list_trending_runs", None)
            if callable(list_runs):
                latest = next(
                    (
                        row
                        for row in list_runs(limit=20, offset=0)
                        if isinstance(row, Mapping) and str(row.get("status") or "") in {"success", "partial"}
                    ),
                    {},
                )
        latest_run_id = int(latest.get("id") or latest.get("run_id") or 0) if isinstance(latest, Mapping) else 0
        use_current_run = False
        if latest_run_id:
            try:
                marker_total = int(self.repository.count_trending_candidates(last_run_id=latest_run_id))
                use_current_run = marker_total > 0 or int(latest.get("candidate_count") or 0) == 0
            except TypeError:
                # Old repository facade: retain the historical fallback below.
                use_current_run = False

        result: dict[str, dict[str, Any]] = {}
        for media_type in ("tv", "movie", "variety", "anime"):
            page = self.list_candidates(
                page=1,
                per_page=200,
                status=status,
                media_type=media_type,
                source=source,
                last_run_id=latest_run_id if use_current_run else None,
            )
            items = list(page.get("items") or [])
            items.sort(key=lambda item: (self._category_rank(item), str(item.get("title") or "")))
            items = items[:safe_limit]
            result[media_type] = {
                "items": items,
                "total": int(page.get("total") or 0),
                "count": len(items),
            }
        return result

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return self.repository.get_trending_candidate(int(candidate_id))

    @staticmethod
    def _fetch(source: Any) -> list[Any]:
        fetch = getattr(source, "fetch", None)
        result = fetch() if callable(fetch) else source()
        if result is None:
            return []
        if isinstance(result, Mapping):
            for key in ("items", "list", "data"):
                value = result.get(key)
                if isinstance(value, list):
                    return value
            raise ValueError("热榜来源未返回列表")
        return list(result)

    @staticmethod
    def _source_name(source: Any) -> str:
        name = str(getattr(source, "name", None) or getattr(source, "source", None) or source.__class__.__name__).strip().lower()
        return re.sub(r"(?:hot)?source$", "", name) or name

    @classmethod
    def _normalize_item(cls, item: Any, *, source_name: str) -> dict[str, Any]:
        if hasattr(item, "to_dict"):
            item = item.to_dict()
        elif hasattr(item, "__dict__") and not isinstance(item, Mapping):
            item = vars(item)
        if not isinstance(item, Mapping):
            raise ValueError("热榜条目必须是字典")
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            raise ValueError("热榜条目缺少标题")
        source = str(item.get("source") or source_name).strip().lower()
        source_id = str(item.get("source_id") or item.get("id") or item.get("content_id") or "").strip()
        year = cls._normalize_year(item.get("year"))
        media_type = cls.normalize_media_type(item.get("media_type") or item.get("type") or item.get("category"))
        normalized = dict(item)
        normalized.update(
            {
                "source": source,
                "source_id": source_id,
                "title": title,
                "normalized_title": cls.normalize_title(title),
                "year": year,
                "media_type": media_type,
            }
        )
        return normalized

    @classmethod
    def _deduplicate_source(cls, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in items:
            key = (
                item["source"],
                item["source_id"] or item["normalized_title"],
                item.get("year"),
                item.get("media_type"),
            )
            previous = unique.get(key)
            if previous is None or cls._rank(item) < cls._rank(previous):
                unique[key] = item
        return list(unique.values())

    @classmethod
    def _merge_candidates(cls, snapshots: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot_items = list(snapshots)
        years_by_title: dict[tuple[Any, ...], set[int]] = {}
        for snapshot in snapshot_items:
            base_key = (snapshot["normalized_title"], snapshot.get("media_type"))
            year = cls._normalize_year(snapshot.get("year"))
            if year is not None:
                years_by_title.setdefault(base_key, set()).add(year)

        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for snapshot in snapshot_items:
            base_key = (snapshot["normalized_title"], snapshot.get("media_type"))
            year = cls._normalize_year(snapshot.get("year"))
            known_years = years_by_title.get(base_key, set())
            # A source that omits the year may join the only known-year work,
            # but must stay separate when two remakes with the same title/type
            # are present.  Known conflicting years are never merged.
            identity_year = year if year is not None else next(iter(known_years)) if len(known_years) == 1 else None
            key = (*base_key, identity_year)
            legacy_key = "|".join(str(value or "") for value in base_key)
            canonical_key = f"{legacy_key}|{identity_year}" if identity_year is not None else legacy_key
            candidate = merged.get(key)
            source_ref = {
                "source": snapshot["source"],
                "source_id": snapshot.get("source_id") or "",
                "rank": snapshot.get("rank"),
                "heat": snapshot.get("heat"),
                "score": snapshot.get("score"),
            }
            if candidate is None:
                candidate = {
                    "canonical_key": canonical_key,
                    "legacy_canonical_key": legacy_key,
                    "source": snapshot["source"],
                    "source_id": snapshot.get("source_id") or "",
                    "title": snapshot["title"],
                    "normalized_title": snapshot["normalized_title"],
                    "year": snapshot.get("year"),
                    "media_type": snapshot.get("media_type"),
                    "best_rank": snapshot.get("rank"),
                    "rank": snapshot.get("rank"),
                    "latest_heat": snapshot.get("heat"),
                    "heat": snapshot.get("heat"),
                    "latest_score": snapshot.get("score"),
                    "score": snapshot.get("score"),
                    "image_url": snapshot.get("image_url") or "",
                    "sources": [source_ref],
                    "platform_ranks": {snapshot["source"]: snapshot.get("rank")},
                    "raw_data": {"source_items": [snapshot]},
                }
                merged[key] = candidate
                continue
            candidate["sources"].append(source_ref)
            candidate["raw_data"]["source_items"].append(snapshot)
            candidate["platform_ranks"][snapshot["source"]] = snapshot.get("rank")
            if cls._rank(snapshot) < cls._rank(candidate):
                candidate["best_rank"] = snapshot.get("rank")
                candidate["rank"] = snapshot.get("rank")
                candidate["source"] = snapshot["source"]
                candidate["source_id"] = snapshot.get("source_id") or ""
            candidate["latest_heat"] = max(cls._number(candidate.get("latest_heat")), cls._number(snapshot.get("heat"))) or None
            candidate["heat"] = candidate["latest_heat"]
            candidate["latest_score"] = max(cls._number(candidate.get("latest_score")), cls._number(snapshot.get("score"))) or None
            candidate["score"] = candidate["latest_score"]
            if snapshot.get("year") and (
                not candidate.get("year") or int(snapshot["year"]) > int(candidate["year"])
            ):
                candidate["year"] = snapshot["year"]
            if not candidate.get("image_url") and snapshot.get("image_url"):
                candidate["image_url"] = snapshot["image_url"]
        return list(merged.values())

    @classmethod
    def _cap_candidates_by_category(cls, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            groups.setdefault(str(candidate.get("media_type") or "unknown"), []).append(candidate)
        capped: list[dict[str, Any]] = []
        for media_type, items in groups.items():
            items.sort(key=lambda item: (cls._composite_rank(item), cls._rank(item), str(item.get("normalized_title") or "")))
            capped.extend(items[:25])
        return capped

    @classmethod
    def _assign_category_ranks(cls, candidates: list[dict[str, Any]]) -> None:
        groups: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            groups.setdefault(str(candidate.get("media_type") or "unknown"), []).append(candidate)
        for items in groups.values():
            items.sort(key=lambda item: (cls._composite_rank(item), cls._rank(item), str(item.get("normalized_title") or "")))
            for index, item in enumerate(items, 1):
                item["aggregate_rank"] = cls._composite_rank(item)
                item["aggregate_score"] = cls._aggregate_score(item)
                item["category_rank"] = index
                item["rank"] = index
                raw = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
                raw.update(
                    {
                        "sources": item.get("sources") or [],
                        "platform_ranks": item.get("platform_ranks") or {},
                        "aggregate_rank": item["aggregate_rank"],
                        "aggregate_score": item["aggregate_score"],
                        "category_rank": index,
                        "best_rank": item.get("best_rank"),
                    }
                )
                item["raw_data"] = raw

    @classmethod
    def _composite_rank(cls, item: Mapping[str, Any]) -> float:
        score = cls._aggregate_score(item)
        return 1.0 / score if score > 0 else cls._rank(item)

    @classmethod
    def _aggregate_score(cls, item: Mapping[str, Any]) -> float:
        refs = item.get("sources") if isinstance(item.get("sources"), list) else []
        score = 0.0
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            rank = cls._number(ref.get("rank"))
            if rank <= 0:
                continue
            # Platform rank remains the primary signal. Bounded heat and
            # rating bonuses only refine close ranks without allowing one
            # platform's raw heat scale to dominate the cross-platform list.
            score += 1.0 / (60.0 + rank)
            heat = max(0.0, cls._number(ref.get("heat")))
            if heat > 0:
                score += min(1.0, math.log10(1.0 + heat) / 8.0) * 0.00035
            rating = max(0.0, cls._number(ref.get("score")))
            if rating > 0:
                score += min(1.0, rating / 10.0) * 0.0001
        return score

    @classmethod
    def _category_rank(cls, item: Mapping[str, Any]) -> float:
        raw = item.get("raw_data") if isinstance(item.get("raw_data"), Mapping) else {}
        value = raw.get("category_rank") if isinstance(raw, Mapping) else None
        try:
            return float(value)
        except (TypeError, ValueError):
            return cls._number(item.get("rank")) or float("inf")

    @staticmethod
    def normalize_media_type(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in MEDIA_TYPE_ALIASES:
            return MEDIA_TYPE_ALIASES[text]
        for alias, canonical in MEDIA_TYPE_ALIASES.items():
            if alias and alias in text:
                return canonical
        return "unknown"

    @staticmethod
    def normalize_title(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
        text = re.sub(r"[\s\-_:：·•，,。.!?！？《》<>\[\]【】()（）]+", "", text)
        return text

    @staticmethod
    def _normalize_year(value: Any) -> int | None:
        match = re.search(r"(?:19|20)\d{2}", str(value or ""))
        return int(match.group(0)) if match else None

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _rank(cls, item: Mapping[str, Any]) -> float:
        value = item.get("rank") if "rank" in item else item.get("best_rank")
        rank = cls._number(value)
        return rank if rank > 0 else float("inf")

    @staticmethod
    def _time_text(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
