from __future__ import annotations

import hashlib
import logging
import posixpath
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..constants import JOB_STATUS_LABELS
from ..database import Database, utc_now
from ..organizer.openlist_client import VIDEO_EXTENSIONS, OpenListClient, basename, dirname, join_path
from ..organizer.parser import category_target_root, parse_file_name
from ..media_path_rules import format_title_year, sanitize_resource_dir_name, split_title_year
from ..organizer.tmdb import TmdbClient
from ..updater.discovery import UpdateDiscovery
from ..updater.matcher import UpdateMatcher
from .update_candidate_import_service import UpdateCandidateImportService
from .update_completion_sync_service import UpdateCompletionSyncService
from .update_source_planner import UpdateSourcePlanner
from .update_next_run_policy import UpdateNextRunPolicy
from .update_subscription_normalizer import UpdateSubscriptionNormalizer
from .update_subscription_command_service import UpdateSubscriptionCommandService
from .update_subscription_query_service import UpdateSubscriptionQueryService
from .update_due_run_service import UpdateDueRunService
from .update_run_coordinator import UpdateRunCoordinator
from .update_run_initializer import UpdateRunInitializer
from .update_candidate_selection_service import UpdateCandidateSelectionService
from .update_candidate_batch_import_service import UpdateCandidateBatchImportService
from .update_run_outcome_service import UpdateRunOutcomeInput, UpdateRunOutcomeService
from .update_run_failure_service import UpdateRunFailureService
from .update_run_lease_service import UpdateRunAlreadyActive, UpdateRunLease
from .update_episode_scan_service import UpdateEpisodeScanService
from .update_source_discovery_service import UpdateSourceDiscoveryService

logger = logging.getLogger(__name__)

ImportHandler = Callable[[dict[str, Any], str], Any]


class UpdateService:
    def __init__(
        self,
        db: Database,
        config: dict[str, Any],
        categories: dict[str, dict[str, Any]],
        search_service: Any,
        import_service: Any,
        quark_importer: Any,
        cloud139_importer: Any,
        import_handler: ImportHandler | None = None,
        owner_id: str = "",
    ) -> None:
        self.db = db
        self.config = config
        self.categories = categories
        self.search_service = search_service
        self.import_service = import_service
        self.openlist = OpenListClient(config.get("openlist", {}) if isinstance(config.get("openlist"), dict) else {})
        self.tmdb = TmdbClient(config.get("tmdb", {}) if isinstance(config.get("tmdb"), dict) else {})
        self.discovery = self._build_discovery(search_service, quark_importer, cloud139_importer)
        self.matcher = UpdateMatcher()
        self.import_handler = import_handler
        self.owner_id = str(owner_id or f"update-service-{id(self)}")
        self.candidate_selection = UpdateCandidateSelectionService(
            database=self.db,
            matcher=self.matcher,
            decorate=self._decorate_update_candidate,
            should_keep=self._should_keep_update_candidate,
            new_filter_summary=self._new_candidate_filter_summary,
            count_ignored=self._count_ignored_candidate,
            record_stage=self._run_stage,
        )
        self.candidate_import_service = UpdateCandidateImportService(
            database=self.db,
            import_service=lambda: self.import_service,
            import_handler=lambda: self.import_handler,
            mark_seen=self._mark_candidate_seen,
        )
        self.candidate_batch_import = UpdateCandidateBatchImportService(
            import_candidate=self.import_candidate,
            record_stage=self._run_stage,
            mark_failed=self._mark_candidate_import_failed,
        )
        self.completion_sync_service = UpdateCompletionSyncService(
            database=self.db,
            mark_seen=self._mark_candidate_seen,
        )
        self.next_run_policy = UpdateNextRunPolicy(
            scheduler_config=self._scheduler_config,
            compute_next_run=self._compute_next_run,
        )
        self.subscription_normalizer = UpdateSubscriptionNormalizer(
            categories=lambda: self.categories,
            tmdb_schedule_hint=self._tmdb_schedule_hint,
            tmdb_basic_hint=self._tmdb_basic_hint,
            path_health=self._subscription_path_health,
            normalize_source=self._normalize_source,
        )
        self.subscription_queries = UpdateSubscriptionQueryService(
            database=self.db,
            categories=lambda: self.categories,
            path_health=self._subscription_path_health,
        )
        self.subscription_commands = UpdateSubscriptionCommandService(
            database=self.db,
            normalize=self._normalize_subscription_payload,
            compute_next_run=self._compute_next_run,
            refresh_context=self._refresh_subscription_root_context_and_path_health,
            get_subscription=self.get_subscription,
        )
        self.run_initializer = UpdateRunInitializer(
            database=self.db,
            sync_completion=self.sync_subscription_completion,
            record_stage=self._run_stage,
            owner_id=self.owner_id,
            lease_seconds=self._run_lease_seconds,
        )
        self.run_outcomes = UpdateRunOutcomeService(
            database=self.db,
            last_success_episode=self._last_success_episode,
            finish_reason=self._run_finish_reason,
            next_run=self._next_run_after_result,
            record_stage=self._run_stage,
        )
        self.run_failures = UpdateRunFailureService(
            database=self.db,
            record_stage=self._run_stage,
            next_retry_at=self._next_run_after_failure,
        )
        self.episode_scans = UpdateEpisodeScanService(
            database=self.db,
            refresh_tmdb=self._refresh_tmdb_schedule_for_run,
            resolve_root=self._resolve_update_root_context,
            inflight_episodes=self._inflight_episodes,
            seen_episodes=self._seen_episodes,
            target_episodes=self._target_episodes,
            scan_existing=self.scan_existing_episodes,
            allow_full_scan=self._allow_bootstrap_full_scan,
            record_stage=self._run_stage,
        )
        self.source_discovery = UpdateSourceDiscoveryService(
            database=self.db,
            discovery=lambda: self.discovery,
            select_sources=self._select_sources_for_discovery,
            candidates_hit_target=self._candidates_hit_target,
            record_fixed_gate=self._record_fixed_source_gate,
            record_source_health=self._record_source_health,
            record_stage=self._run_stage,
        )
        self.run_coordinator = UpdateRunCoordinator(self._run_subscription_locked)
        self.due_runs = UpdateDueRunService(
            database=self.db,
            scheduler_config=self._scheduler_config,
            run_subscription=self.run_subscription,
            record_result=lambda result: setattr(self, "last_run_result", result),
        )
        self.last_run_result: dict[str, Any] | None = None
        self.recover_stale_runs()

    def set_runtime(self, *, config: dict[str, Any], categories: dict[str, dict[str, Any]], search_service: Any, import_service: Any, quark_importer: Any, cloud139_importer: Any) -> None:
        self.config = config
        self.categories = categories
        self.search_service = search_service
        self.import_service = import_service
        self.openlist = OpenListClient(config.get("openlist", {}) if isinstance(config.get("openlist"), dict) else {})
        self.tmdb = TmdbClient(config.get("tmdb", {}) if isinstance(config.get("tmdb"), dict) else {})
        self.discovery = self._build_discovery(search_service, quark_importer, cloud139_importer)

    def _scheduler_config(self) -> dict[str, Any]:
        return self.config.get("update_scheduler", {}) if isinstance(self.config.get("update_scheduler"), dict) else {}

    def _run_lease_seconds(self) -> int:
        return max(30, int(self._scheduler_config().get("run_lease_seconds") or 120))

    def recover_stale_runs(self) -> list[dict[str, Any]]:
        recover = getattr(self.db, "recover_stale_update_runs", None)
        if not callable(recover):
            return []
        try:
            return list(recover(older_than_seconds=self._run_lease_seconds()) or [])
        except Exception:  # noqa: BLE001
            logger.exception("recover stale update runs failed")
            return []

    def _build_discovery(self, search_service: Any, quark_importer: Any, cloud139_importer: Any) -> UpdateDiscovery:
        scheduler = self._scheduler_config()
        return UpdateDiscovery(
            search_service=search_service,
            quark_importer=quark_importer,
            cloud139_importer=cloud139_importer,
            routes=self.config.get("routes", {}) if isinstance(self.config.get("routes"), dict) else {},
            db=self.db,
            cache_ttl_seconds=int(scheduler.get("preview_cache_ttl_seconds") or 21600),
            negative_cache_ttl_seconds=int(scheduler.get("negative_preview_cache_ttl_seconds") or 600),
        )

    def list_subscriptions(self, *, page: int = 1, per_page: int = 50, status: str | None = None) -> dict[str, Any]:
        return self.subscription_queries.list(page=page, per_page=per_page, status=status)

    def get_subscription(self, subscription_id: int) -> dict[str, Any] | None:
        return self.subscription_queries.get(subscription_id)

    def with_current_path_health(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        return self.subscription_queries.with_path_health(item)

    def create_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.subscription_commands.create(payload)

    def create_subscription_from_trending_candidate(self, candidate_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.subscription_commands.create_from_trending_candidate(candidate_id, payload)

    def update_subscription(self, subscription_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.subscription_commands.update(subscription_id, payload)

    def _refresh_subscription_root_context_and_path_health(self, subscription_id: int) -> dict[str, Any]:
        subscription = self.db.get_update_subscription(subscription_id, include_sources=True) or {"id": subscription_id}
        if subscription.get("id"):
            self._resolve_update_root_context(subscription)
            subscription = self.db.get_update_subscription(subscription_id, include_sources=True) or subscription
        category_key = str(subscription.get("category") or "movie")
        category = self.categories.get(category_key, {})
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        raw_data = dict(raw_data)
        raw_data["path_health"] = self._subscription_path_health(category_key, category, raw_data)
        if subscription.get("id"):
            self.db.update_update_subscription(subscription_id, {"raw_data": raw_data})
        subscription["raw_data"] = raw_data
        return subscription

    def set_status(self, subscription_id: int, status: str) -> dict[str, Any]:
        return self.subscription_commands.set_status(subscription_id, status)

    def delete_subscription(self, subscription_id: int) -> dict[str, Any]:
        return self.subscription_commands.delete(subscription_id)

    def run_due(self, *, limit: int = 10, trigger_type: str = "external") -> dict[str, Any]:
        self.recover_stale_runs()
        return self.due_runs.run_due(limit=limit, trigger_type=trigger_type)

    def run_subscription(self, subscription_id: int, *, trigger_type: str = "manual") -> dict[str, Any]:
        return self.run_coordinator.run(subscription_id, trigger_type=trigger_type)

    def _run_subscription_locked(self, subscription_id: int, *, trigger_type: str) -> dict[str, Any]:
        try:
            context = self.run_initializer.initialize(subscription_id, trigger_type=trigger_type)
        except UpdateRunAlreadyActive as exc:
            return {
                "success": False,
                "locked": True,
                "subscription_id": subscription_id,
                "message": str(exc),
                "active_run": exc.active_run,
            }
        sync_result = context.sync_result
        subscription = context.subscription
        run_id = context.run_id
        try:
            lease = UpdateRunLease(
                database=self.db,
                run_id=run_id,
                owner_id=context.owner_id,
                lease_seconds=context.lease_seconds,
                log=logger.warning,
            )
            with lease:
                lease.ensure_owned()
                return self._execute_update_run(
                    context=context,
                    lease=lease,
                    subscription_id=subscription_id,
                    run_id=run_id,
                    subscription=subscription,
                    sync_result=sync_result,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("update subscription run failed: %s", subscription_id)
            raise

    def _execute_update_run(
        self,
        *,
        context: Any,
        lease: UpdateRunLease,
        subscription_id: int,
        run_id: int,
        subscription: dict[str, Any],
        sync_result: dict[str, Any],
    ) -> dict[str, Any]:
        submitted = 0
        completed = 0
        skipped = 0
        candidate_count = 0
        discovered_count = 0
        import_results: list[dict[str, Any]] = []
        try:
            episode_scan = self.episode_scans.scan(
                subscription_id=subscription_id,
                run_id=run_id,
                subscription=subscription,
            )
            subscription = episode_scan.subscription
            root_context = episode_scan.root_context
            inflight = episode_scan.inflight
            existing = episode_scan.existing
            target_episodes = episode_scan.target_episodes
            previous_last_success_episode = episode_scan.previous_last_success_episode
            latest_existing_episode = episode_scan.latest_existing_episode
            baseline_advanced = episode_scan.baseline_advanced
            lease.ensure_owned()
            discovery_result = self.source_discovery.discover(
                subscription_id=subscription_id,
                run_id=run_id,
                subscription=subscription,
                target_episodes=target_episodes,
            )
            candidates = discovery_result.candidates
            source_plan = discovery_result.source_plan
            source_health_result = discovery_result.source_health_result
            search_used = discovery_result.search_used
            fixed_target_hit = discovery_result.fixed_target_hit
            discovered_count = len(candidates)
            lease.ensure_owned()
            selection = self.candidate_selection.select(
                subscription_id=subscription_id,
                run_id=run_id,
                subscription=subscription,
                root_context=root_context,
                target_episodes=target_episodes,
                existing_episodes=existing,
                candidates=candidates,
            )
            best_by_episode = selection.best_by_episode
            candidate_count = selection.candidate_count
            skipped += selection.skipped_count
            candidate_filter = selection.filter_summary
            lease.ensure_owned()
            batch_import = self.candidate_batch_import.import_best(
                subscription_id=subscription_id,
                run_id=run_id,
                best_by_episode=best_by_episode,
            )
            import_results.extend(batch_import.items)
            submitted += batch_import.submitted_count
            completed += batch_import.completed_count
            skipped += batch_import.failed_count
            lease.ensure_owned()
            return self.run_outcomes.finalize(
                UpdateRunOutcomeInput(
                    subscription_id=subscription_id,
                    run_id=run_id,
                    subscription=subscription,
                    existing=existing,
                    inflight=inflight,
                    target_episodes=target_episodes,
                    import_results=import_results,
                    sync_result=sync_result,
                    source_health_result=source_health_result,
                    candidate_filter=candidate_filter,
                    source_plan=source_plan,
                    root_context=root_context,
                    candidate_count=candidate_count,
                    discovered_count=discovered_count,
                    submitted=submitted,
                    completed=completed,
                    skipped=skipped,
                    search_used=search_used,
                    fixed_target_hit=fixed_target_hit,
                    previous_last_success_episode=previous_last_success_episode,
                    latest_existing_episode=latest_existing_episode,
                    baseline_advanced=baseline_advanced,
                    owner_id=context.owner_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.run_failures.record(
                subscription_id=subscription_id,
                run_id=run_id,
                error=exc,
                candidate_count=candidate_count,
                submitted_count=submitted,
                skipped_count=skipped,
                owner_id=context.owner_id,
                trigger_type=context.trigger_type,
            )
            raise

    def _run_stage(self, run_id: int, stage: str, message: str, raw_data: Any = None) -> None:
        try:
            self.db.append_update_run_log(run_id, stage, message, raw_data)
        except Exception:  # noqa: BLE001
            logger.debug("append update run log failed", exc_info=True)

    def import_candidate(
        self,
        candidate_id: int,
        *,
        reason: str = "manual_update_candidate",
        auto: bool = False,
        candidate_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.candidate_import_service.import_candidate(
            candidate_id,
            reason=reason,
            auto=auto,
            candidate_override=candidate_override,
        )

    def _mark_candidate_import_failed(self, candidate_id: int, message: str) -> None:
        self.db.update_update_candidate(
            candidate_id,
            decision="failed",
            reason=f"单集入库异常，将在后续追更任务重试：{message}",
        )

    def sync_subscription_completion(self, subscription_id: int) -> dict[str, Any]:
        return self.completion_sync_service.sync(subscription_id)

    def _pending_import_status_from_job(
        self,
        candidate: dict[str, Any],
        job: dict[str, Any],
        *,
        status: str,
        decision: str,
    ) -> dict[str, Any]:
        return self.completion_sync_service.status_from_job(
            candidate,
            job,
            status=status,
            decision=decision,
        )

    def _select_sources_for_discovery(
        self,
        subscription: dict[str, Any],
        target_episodes: set[tuple[int | None, int]],
    ) -> dict[str, Any]:
        return UpdateSourcePlanner.plan(
            subscription,
            fallback_threshold=self._fixed_source_fallback_threshold(subscription),
            target_key=self._target_key_text(target_episodes),
        )

    def _fixed_source_fallback_threshold(self, subscription: dict[str, Any] | None = None) -> int:
        scheduler = self._scheduler_config()
        value = scheduler.get("source_health_warn_threshold")
        if value in (None, ""):
            value = scheduler.get("empty_retry_max_attempts")
        base = max(1, int(value or 4))
        if not subscription:
            return base
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        health = raw_data.get("source_health") if isinstance(raw_data.get("source_health"), dict) else {}
        if not health:
            return base
        warn = max(1, int(scheduler.get("source_health_warn_threshold") or scheduler.get("empty_retry_max_attempts") or 4))
        # 固定源持续报错/空命中时提前启用综合搜索兜底，减少新集发现延迟。
        for item in health.values():
            if not isinstance(item, dict) or str(item.get("type") or "").strip().lower() == "search":
                continue
            consecutive_error = int(item.get("consecutive_error") or 0)
            consecutive_empty = int(item.get("consecutive_empty") or 0)
            if consecutive_error >= warn or consecutive_empty >= warn:
                return max(1, base - 2)
        return base

    def _record_fixed_source_gate(
        self,
        subscription: dict[str, Any],
        run_id: int | None,
        target_episodes: set[tuple[int | None, int]],
        candidates: list[dict[str, Any]],
        *,
        source_plan: dict[str, Any],
        target_hit: bool,
        search_used: bool,
    ) -> dict[str, Any]:
        if not source_plan.get("fixed_sources") or not target_episodes:
            return subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        raw_data = dict(raw_data)
        previous = raw_data.get("fixed_source_gate") if isinstance(raw_data.get("fixed_source_gate"), dict) else {}
        target_key = str(source_plan.get("target_key") or self._target_key_text(target_episodes))
        same_target = previous.get("target_key") == target_key
        threshold = max(1, int(source_plan.get("threshold") or 4))
        attempts = int(previous.get("attempts") or 0) if same_target else 0
        pre_air_probe = self._is_tmdb_pre_air_probe(subscription, target_episodes)
        fixed_candidates = [
            item
            for item in candidates
            if not item.get("repair_source") and str(item.get("source_type") or "").strip().lower() != "search"
        ]
        error_count = len([item for item in fixed_candidates if item.get("error")])
        if target_hit or pre_air_probe:
            # 纯播出日前探测不应继承同一目标在旧版本中误累计的次数，否则即使
            # 本轮不再增加 attempts，也可能继续保持 search_allowed=True。
            attempts = 0
        elif not search_used:
            attempts += 1
        search_allowed = bool(attempts >= threshold)
        state = {
            "target_key": target_key,
            "target_episodes": sorted([episode for _season, episode in target_episodes]),
            "attempts": attempts,
            "threshold": threshold,
            "search_allowed": search_allowed,
            "search_used": bool(search_used),
            "target_hit": bool(target_hit),
            "fixed_candidate_count": len([item for item in fixed_candidates if not item.get("error")]),
            "fixed_error_count": error_count,
            "last_checked_at": utc_now(),
            "reason": (
                "固定源已发现目标单集"
                if target_hit
                else (
                    "播出日前提前探测未命中，不累计综合搜索兜底次数"
                    if pre_air_probe
                    else ("已达到固定源补查阈值，本轮启用综合搜索兜底" if search_used else "固定源未发现目标单集，继续累计补查次数")
                )
            ),
        }
        raw_data["fixed_source_gate"] = state
        subscription_id = int(subscription.get("id") or 0)
        if subscription_id:
            self.db.update_update_subscription(subscription_id, {"raw_data": raw_data})
            if search_allowed and not bool(previous.get("search_allowed")) and not target_hit:
                self.db.add_update_event(subscription_id, run_id, "warn", "固定追更源已达到综合搜索兜底阈值", state)
        return raw_data

    def _candidates_hit_target(self, subscription: dict[str, Any], candidates: list[dict[str, Any]], target_episodes: set[tuple[int | None, int]]) -> bool:
        if not target_episodes:
            return False
        expected_season = _to_season(subscription.get("season"))
        for candidate in candidates:
            if candidate.get("error"):
                continue
            if not self._candidate_has_single_file_evidence(candidate):
                continue
            parsed = parse_file_name(str(candidate.get("title") or ""), current_dir=str(candidate.get("parent_name") or ""), parent_dir=str(subscription.get("title") or ""))
            season = _first_season(candidate.get("season"), parsed.season, expected_season)
            episode = _to_int(candidate.get("episode")) or parsed.episode
            if episode and _episode_in_set(season, episode, target_episodes):
                return True
        return False

    def _should_keep_update_candidate(self, candidate: dict[str, Any], match: Any, target_episodes: set[tuple[int | None, int]]) -> tuple[bool, str]:
        """只有文件级且命中本轮目标集的资源，才写入候选表并在页面展示。"""

        episode = _to_int(getattr(match, "episode", None))
        season = _to_season(getattr(match, "season", None))
        if not episode:
            return False, "no_episode"
        if target_episodes and not _episode_in_set(season, episode, target_episodes):
            return False, "not_target"
        if not self._candidate_has_single_file_evidence(candidate):
            return False, "not_file_level"
        return True, ""

    @staticmethod
    def _candidate_has_single_file_evidence(candidate: dict[str, Any]) -> bool:
        source_type = str(candidate.get("source_type") or "").strip().lower()
        return bool(candidate.get("file_level") or source_type in {"magnet", "torrent", "bt"})

    @staticmethod
    def _new_candidate_filter_summary(discovered_count: int) -> dict[str, Any]:
        return {
            "discovered_count": int(discovered_count or 0),
            "kept_count": 0,
            "ignored_count": 0,
            "ignored_error": 0,
            "ignored_no_episode": 0,
            "ignored_not_target": 0,
            "ignored_not_file_level": 0,
            "ignored_other": 0,
            "reason": "候选表只保留文件级且命中本轮目标集的资源；未识别集数、非目标集和整目录结果仅作为来源诊断，不展示为候选。",
        }

    @staticmethod
    def _count_ignored_candidate(summary: dict[str, Any], reason: str) -> None:
        key = f"ignored_{reason or 'other'}"
        if key not in summary:
            key = "ignored_other"
        summary[key] = int(summary.get(key) or 0) + 1

    @staticmethod
    def _run_finish_reason(
        *,
        target_episodes: set[tuple[int | None, int]],
        candidate_count: int,
        discovered_count: int,
        submitted: int,
        completed: int,
        candidate_filter: dict[str, Any],
        failed_import_count: int = 0,
    ) -> str:
        target_text = "、".join(f"E{episode}" for _season, episode in sorted(target_episodes, key=lambda item: (item[0] or 0, item[1]))) or "目标集"
        target_count = len(target_episodes)
        if completed and completed >= target_count and not failed_import_count:
            return f"{target_text} 已完整入库"
        if completed:
            return f"{target_text} 已完成 {completed} 集，未完成缺集将在后续自动重试"
        if submitted:
            return f"{target_text} 已提交入库，等待整理完成"
        if failed_import_count:
            return f"{target_text} 本轮入库失败，已保留缺集并将在后续自动重试"
        if candidate_count:
            return f"已找到 {target_text} 的候选文件，但未满足自动入库条件"
        if not target_episodes:
            return "当前没有需要追更的目标集"
        if not discovered_count:
            return f"本轮未在追更源发现 {target_text} 的文件，下次自动继续检查"
        if int(candidate_filter.get("ignored_no_episode") or 0):
            return f"追更源返回了目录或未识别集数的结果，未命中 {target_text} 单文件，下次自动继续检查"
        if int(candidate_filter.get("ignored_not_target") or 0):
            return f"追更源返回的文件不是 {target_text}，已忽略，下次自动继续检查"
        if int(candidate_filter.get("ignored_not_file_level") or 0):
            return f"追更源未展开到 {target_text} 单文件，已忽略，下次自动继续检查"
        return f"本轮未命中 {target_text} 的准确单文件，下次自动继续检查"

    @staticmethod
    def _target_key_text(target_episodes: set[tuple[int | None, int]]) -> str:
        return ",".join(
            f"S{_season_key(season)}E{episode}"
            for season, episode in sorted(target_episodes, key=lambda item: (item[0] is not None, item[0] or 0, item[1]))
        )

    def _record_source_health(
        self,
        subscription: dict[str, Any],
        run_id: int | None,
        sources: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        subscription_id = int(subscription.get("id") or 0)
        if not subscription_id:
            return {"updated": False, "summary": {}}
        enabled_sources = [source for source in sources if source.get("enabled", True)]
        if not enabled_sources:
            return {"updated": False, "summary": {}}
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        raw_data = dict(raw_data)
        previous_health = raw_data.get("source_health") if isinstance(raw_data.get("source_health"), dict) else {}
        source_by_key = {_source_health_key(source): source for source in enabled_sources}
        source_by_id = {str(source.get("id")): source for source in enabled_sources if source.get("id") is not None}
        grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in source_by_key}
        for candidate in candidates:
            source = source_by_id.get(str(candidate.get("source_id")))
            key = _source_health_key(source) if source else _candidate_health_key(candidate)
            if key not in grouped:
                grouped[key] = []
                if source:
                    source_by_key[key] = source
            grouped[key].append(candidate)
        now_text = utc_now()
        threshold = max(1, int(self._scheduler_config().get("source_health_warn_threshold") or self._scheduler_config().get("empty_retry_max_attempts") or 4))
        new_health: dict[str, Any] = {}
        summary_items: list[dict[str, Any]] = []
        for key, source in source_by_key.items():
            rows = grouped.get(key) or []
            errors = [item for item in rows if item.get("error")]
            valid_rows = [item for item in rows if not item.get("error")]
            previous = previous_health.get(key) if isinstance(previous_health.get(key), dict) else {}
            if errors and not valid_rows:
                status = "error"
                message = str(errors[0].get("error") or "来源发现失败")
            elif not valid_rows:
                status = "empty"
                message = "本轮未命中目标集"
            else:
                status = "ok"
                message = "来源检查完成"
            consecutive_error = int(previous.get("consecutive_error") or 0) + 1 if status == "error" else 0
            consecutive_empty = int(previous.get("consecutive_empty") or 0) + 1 if status == "empty" else 0
            latest_episode = max((_to_int(item.get("episode")) or 0 for item in valid_rows), default=0) or None
            health_item = {
                "key": key,
                "source_id": source.get("id"),
                "type": source.get("type") or "",
                "name": source.get("name") or source.get("type") or "",
                "url": source.get("url") or "",
                "status": status,
                "message": message,
                "checked_count": len(valid_rows),
                "error_count": len(errors),
                "consecutive_error": consecutive_error,
                "consecutive_empty": consecutive_empty,
                "latest_episode": latest_episode,
                "last_checked_at": now_text,
                "warn_threshold": threshold,
                "repair_suggested": consecutive_error >= threshold,
            }
            new_health[key] = health_item
            summary_items.append({k: health_item.get(k) for k in ("source_id", "type", "name", "status", "message", "checked_count", "consecutive_error", "consecutive_empty", "latest_episode", "repair_suggested")})
            if consecutive_error >= threshold and int(previous.get("consecutive_error") or 0) < threshold:
                self.db.add_update_event(
                    subscription_id,
                    run_id,
                    "warn",
                    f"追更源连续失败 {consecutive_error} 次，建议检查或依赖搜索兜底：{health_item['name']}",
                    health_item,
                )
        raw_data["source_health"] = new_health
        self.db.update_update_subscription(subscription_id, {"raw_data": raw_data})
        return {"updated": True, "raw_data": raw_data, "summary": {"items": summary_items, "checked_at": now_text}}

    def _mark_candidate_seen(self, subscription: dict[str, Any], candidate_row: dict[str, Any], candidate: dict[str, Any], candidate_id: int, job: dict[str, Any], *, auto: bool, completion_state: str) -> None:
        raw = candidate_row.get("raw_data") if isinstance(candidate_row.get("raw_data"), dict) else {}
        match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
        self.db.upsert_update_seen_item(
            {
                "subscription_id": subscription.get("id"),
                "fingerprint": str(match.get("fingerprint") or candidate.get("fingerprint") or candidate_row.get("source_url_hash")),
                "source_type": candidate_row.get("source_type"),
                "source_url_hash": candidate_row.get("source_url_hash"),
                "file_id": candidate.get("file_id") or "",
                "file_name": candidate_row.get("title") or "",
                "season": candidate_row.get("season"),
                "episode": candidate_row.get("episode"),
                "raw_data": {"candidate_id": candidate_id, "job_id": job.get("id"), "auto": auto, "completion_state": completion_state},
            }
        )

    def reject_candidate(self, candidate_id: int, reason: str = "管理员拒绝候选") -> dict[str, Any]:
        candidate = self.db.get_update_candidate(candidate_id)
        if not candidate:
            raise ValueError("候选不存在")
        self.db.update_update_candidate(candidate_id, decision="rejected", reason=reason)
        self.db.add_update_event(int(candidate["subscription_id"]), candidate.get("run_id"), "warn", f"候选已拒绝：{reason}", {"candidate_id": candidate_id})
        return {"success": True, "message": "已拒绝候选"}

    def _resolve_update_root_context(self, subscription: dict[str, Any]) -> dict[str, Any]:
        """定位追更应落入的既有 OpenList 资源目录。

        定时追更只处理单集，但最终整理必须进入已经按 TMDB 命名过的资源根
        目录，例如 /动漫/完美世界 (2021)，不能再按订阅标题新建一层“完美世界”。
        """

        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        raw_data = dict(raw_data)
        category = self.categories.get(str(subscription.get("category") or ""), {})
        category_root = _clean_openlist_root(category_target_root(category)) if category else ""
        configured_raw = _clean_openlist_root(raw_data.get("canonical_openlist_root") or raw_data.get("existing_openlist_root") or raw_data.get("openlist_path"))
        configured = _resource_root_from_openlist_root(configured_raw)
        candidates = self._canonical_title_candidates(subscription)
        resolved = ""
        confidence = 0
        reason = ""
        if configured and configured != category_root:
            evidence = self._openlist_episode_evidence(subscription, configured_raw or configured)
            if evidence.get("count"):
                resolved = _resource_root_from_openlist_root(evidence.get("root") or configured)
                confidence = 106 if self._openlist_dir_matches_subscription(resolved, candidates) else 96
                reason = f"使用已配置目录，且检测到剧集文件证据（最新 E{evidence.get('latest_episode')}）"
                child_resolved, child_confidence, child_reason = self._find_matching_openlist_child_resource_root(subscription, resolved, candidates)
                if child_resolved and _resource_root_standard_score(child_resolved, candidates) > _resource_root_standard_score(resolved, candidates):
                    resolved = child_resolved
                    confidence = max(confidence, child_confidence)
                    reason = child_reason or "已配置目录下发现 TMDB 标准子目录，优先使用子目录"
                better_resolved, better_confidence, better_reason = self._find_existing_openlist_resource_root(subscription, category_root, candidates) if self.openlist.configured and category_root else ("", 0, "")
                if better_resolved and _resource_root_standard_score(better_resolved, candidates) > _resource_root_standard_score(resolved, candidates) and better_confidence >= 95:
                    resolved = better_resolved
                    confidence = max(confidence, better_confidence)
                    reason = f"{better_reason or '发现 TMDB 标准目录'}，优先使用已整理的标准资源目录"
            else:
                better_resolved, better_confidence, better_reason = self._find_existing_openlist_resource_root(subscription, category_root, candidates) if self.openlist.configured and category_root else ("", 0, "")
                if better_resolved and better_confidence >= 100:
                    resolved = better_resolved
                    confidence = better_confidence
                    reason = better_reason
                else:
                    resolved = configured
                    confidence = 100 if self._openlist_dir_matches_subscription(configured, candidates) else 88
                    reason = "使用订阅已配置的既有 OpenList 资源目录（未检测到剧集文件证据）"
        elif self.openlist.configured and category_root:
            resolved, confidence, reason = self._find_existing_openlist_resource_root(subscription, category_root, candidates)
        resolved = _resource_root_from_openlist_root(resolved)
        expected_root = self._expected_canonical_openlist_root(subscription, category_root, candidates)
        auto_create_pending = False
        if not resolved and expected_root:
            resolved = expected_root
            confidence = max(confidence, 82)
            reason = "未找到既有标准目录，已使用 TMDB 标准保存目录，首次入库时自动创建"
            auto_create_pending = True
        canonical_title = _canonical_update_title(subscription, resolved, candidates)
        official_cloud139_target = self._cloud139_target_for_openlist_root(category, resolved) if resolved else ""
        previous_resolution = raw_data.get("canonical_root_resolution") if isinstance(raw_data.get("canonical_root_resolution"), dict) else {}
        context = {
            "canonical_openlist_root": resolved,
            "canonical_resource_root": resolved,
            "canonical_title": canonical_title,
            "canonical_year": str(subscription.get("year") or ""),
            "category_root": category_root,
            "expected_openlist_root": expected_root,
            "auto_create_pending": auto_create_pending,
            "cloud139_target_path": official_cloud139_target,
            "confidence": min(100, confidence),
            "reason": reason or ("未能自动识别既有 OpenList 资源目录" if category_root else "分类 OpenList 根目录为空"),
            "needs_manual_selection": not bool(resolved),
        }
        raw_data["canonical_root_resolution"] = context
        if resolved:
            raw_data["canonical_openlist_root"] = resolved
        subscription["raw_data"] = raw_data
        if int(subscription.get("id") or 0):
            if previous_resolution.get("canonical_openlist_root") != context.get("canonical_openlist_root") or previous_resolution.get("needs_manual_selection") != context.get("needs_manual_selection"):
                self.db.update_update_subscription(int(subscription["id"]), {"raw_data": raw_data})
                level = "info" if resolved else "warn"
                self.db.add_update_event(
                    int(subscription["id"]),
                    None,
                    level,
                    "已确定追更保存目录" if resolved else "未能自动识别追更保存目录",
                    context,
                )
        return context

    def _decorate_update_candidate(
        self,
        subscription: dict[str, Any],
        candidate: dict[str, Any],
        root_context: dict[str, Any],
        target_episodes: set[tuple[int | None, int]],
    ) -> dict[str, Any]:
        item = dict(candidate)
        payload = dict(item.get("import_payload") or {})
        target_episode = _to_int(item.get("episode")) or min((episode for _season, episode in target_episodes), default=0) or None
        canonical_root = _resource_root_from_openlist_root(root_context.get("canonical_openlist_root"))
        canonical_title = _canonical_update_title(subscription, canonical_root, self._canonical_title_candidates(subscription))
        expected_name = str(item.get("title") or "").strip()
        update_context = {
            "subscription_id": subscription.get("id"),
            "target_episode": target_episode,
            "target_episodes": sorted([episode for _season, episode in target_episodes]),
            "canonical_openlist_root": canonical_root,
            "canonical_resource_root": canonical_root,
            "canonical_title": canonical_title,
            "canonical_year": root_context.get("canonical_year") or subscription.get("year") or "",
            "target_root_is_resource": bool(canonical_root),
            "scan_filters": {
                "expected_names": [expected_name] if expected_name else [],
                "expected_paths": [],
            },
        }
        if canonical_root:
            payload["title"] = canonical_title or subscription.get("title") or payload.get("title") or item.get("title")
            payload["update_context"] = update_context
            payload["organizer_context"] = {
                "target_root_path": canonical_root,
                "canonical_resource_root": canonical_root,
                "canonical_title": canonical_title,
                "target_root_is_resource": True,
                "scan_filters": update_context["scan_filters"],
            }
            if str(item.get("source_type") or "").strip().lower() == "cloud139" and root_context.get("cloud139_target_path"):
                payload["target_path_override"] = root_context.get("cloud139_target_path")
                payload["cloud139_target_path_override"] = root_context.get("cloud139_target_path")
        elif target_episodes and str(subscription.get("category") or "").strip().lower() in {"tv", "anime", "variety"}:
            item["decision_hint"] = "review"
            item["canonical_root_missing"] = True
            payload["update_context"] = {**update_context, "needs_manual_selection": True}
        item["import_payload"] = payload
        return item

    def _canonical_title_candidates(self, subscription: dict[str, Any]) -> list[dict[str, str]]:
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        tmdb_schedule = raw_data.get("tmdb_schedule") if isinstance(raw_data.get("tmdb_schedule"), dict) else {}
        raw_values = [
            subscription.get("title"),
            tmdb_schedule.get("title"),
            *(subscription.get("aliases") or []),
        ]
        years = [str(subscription.get("year") or "").strip(), str(tmdb_schedule.get("year") or "").strip()]
        result: list[dict[str, str]] = []
        for value in raw_values:
            title, detected_year = split_title_year(value)
            title = title or sanitize_resource_dir_name(value, fallback="")
            year = next((item for item in [*years, detected_year] if item), "")
            displays = [format_title_year(title, year) if title and year else "", title, str(value or "").strip()]
            for display in displays:
                text = sanitize_resource_dir_name(display, fallback="")
                norm = _norm(text)
                if text and norm and not any(item.get("norm") == norm for item in result):
                    result.append({"display": text, "title": title, "year": year, "norm": norm})
        return result or [{"display": str(subscription.get("title") or ""), "title": str(subscription.get("title") or ""), "year": str(subscription.get("year") or ""), "norm": _norm(subscription.get("title"))}]

    def _expected_canonical_openlist_root(self, subscription: dict[str, Any], category_root: str, candidates: list[dict[str, str]]) -> str:
        """根据 TMDB 标题/年份生成预期标准目录；不存在也可由 Organizer 入库时创建。"""

        root = _clean_openlist_root(category_root)
        if not root or _invalid_openlist_path(root):
            return ""
        tmdb_id = _to_int(subscription.get("tmdb_id"))
        preferred = next((item for item in candidates if str(item.get("display") or "").strip() and str(item.get("year") or "").strip()), None)
        preferred = preferred or next((item for item in candidates if str(item.get("display") or "").strip() and tmdb_id), None)
        preferred = preferred or next((item for item in candidates if str(item.get("display") or "").strip()), None)
        display = sanitize_resource_dir_name((preferred or {}).get("display"), fallback="")
        if not display:
            return ""
        if _norm(display) == _norm(basename(root)):
            return root
        return _resource_root_from_openlist_root(join_path(root, display))

    def _find_existing_openlist_resource_root(self, subscription: dict[str, Any], category_root: str, candidates: list[dict[str, str]]) -> tuple[str, int, str]:
        if not self.openlist.configured or not category_root:
            return "", 0, ""
        try:
            rows = [item for item in self.openlist.list_dir(category_root) if item.is_dir]
        except Exception as exc:  # noqa: BLE001
            logger.debug("list openlist category root failed", exc_info=True)
            return "", 0, f"OpenList 目录扫描失败：{exc}"
        best: tuple[int, str, str] = (0, "", "")
        for row in rows:
            row_norm = _norm(row.name)
            score = 0
            reason = ""
            for candidate in candidates:
                cand_norm = candidate.get("norm") or ""
                title_norm = _norm(candidate.get("title"))
                year = str(candidate.get("year") or "").strip()
                if cand_norm and row_norm == cand_norm:
                    score = max(score, 100)
                    reason = "目录名与 TMDB 标准名完全一致"
                elif title_norm and title_norm in row_norm and year and year in row.name:
                    score = max(score, 95)
                    reason = "目录名命中标题和年份"
                elif title_norm and (row_norm.startswith(title_norm) or title_norm in row_norm):
                    score = max(score, 82)
                    reason = "目录名命中标题"
            if score:
                evidence = self._openlist_episode_evidence(subscription, row.path)
                if evidence.get("count"):
                    score = max(score + 20, 105)
                    evidence_root = _resource_root_from_openlist_root(evidence.get("root") or row.path)
                    reason = f"{reason or '目录名命中标题'}，且目录内有剧集文件证据（最新 E{evidence.get('latest_episode')}）"
                    if score > best[0]:
                        best = (score, evidence_root, reason)
                    continue
            if score > best[0]:
                best = (score, _resource_root_from_openlist_root(row.path), reason)
        return (_resource_root_from_openlist_root(best[1]), best[0], best[2]) if best[0] >= 80 else ("", best[0], "未找到足够相似的既有资源目录")

    def _find_matching_openlist_child_resource_root(self, subscription: dict[str, Any], root: str, candidates: list[dict[str, str]]) -> tuple[str, int, str]:
        root = _resource_root_from_openlist_root(root)
        if not self.openlist.configured or not root:
            return "", 0, ""
        try:
            rows = [item for item in self.openlist.list_dir(root) if item.is_dir]
        except Exception:  # noqa: BLE001
            return "", 0, ""
        best: tuple[int, str, str] = (0, "", "")
        for row in rows:
            path = _resource_root_from_openlist_root(row.path)
            score = _resource_root_standard_score(path, candidates)
            if not score:
                continue
            evidence = self._openlist_episode_evidence(subscription, path, max_depth=1, max_dirs=8, max_files=120)
            if evidence.get("count"):
                weighted = 100 + score * 10
                reason = f"已配置目录下发现 TMDB 标准子目录，且子目录内有剧集文件证据（最新 E{evidence.get('latest_episode')}）"
            else:
                weighted = 80 + score * 10
                reason = "已配置目录下发现 TMDB 标准子目录"
            if weighted > best[0]:
                best = (weighted, path, reason)
        return (best[1], best[0], best[2]) if best[0] >= 90 else ("", best[0], "未发现 TMDB 标准子目录")

    def _openlist_episode_evidence(self, subscription: dict[str, Any], root: str, *, max_depth: int = 2, max_dirs: int = 16, max_files: int = 160) -> dict[str, Any]:
        """浅层确认某个目录是否真的是剧集资源根，避免把同名电影/空目录当追更根。"""

        scan_root = _clean_openlist_root(root)
        resource_root = _resource_root_from_openlist_root(scan_root)
        if not self.openlist.configured or not scan_root:
            return {"count": 0}
        expected_season = _to_season(subscription.get("season"))
        queue: list[tuple[str, int]] = [(scan_root, 0)]
        visited: set[str] = set()
        checked_dirs = 0
        checked_files = 0
        best_root = ""
        latest_episode = 0
        latest_season: int | None = None
        while queue and checked_dirs < max_dirs and checked_files < max_files:
            current, depth = queue.pop(0)
            current = _clean_openlist_root(current)
            if not current or current in visited:
                continue
            visited.add(current)
            try:
                rows = self.openlist.list_dir(current)
            except Exception:  # noqa: BLE001
                continue
            checked_dirs += 1
            dir_episode_count = 0
            for item in rows:
                if item.is_dir:
                    if depth < max_depth and self._should_probe_openlist_child_dir(subscription, item.name, set()):
                        queue.append((item.path, depth + 1))
                    continue
                if not self._is_video_file_name(item.name):
                    continue
                checked_files += 1
                season, episode = self._episode_from_openlist_file(subscription, item.name, item.path)
                season = _first_season(season, expected_season)
                if episode and _season_compatible(expected_season, season):
                    dir_episode_count += 1
                    if episode > latest_episode:
                        latest_episode = episode
                        latest_season = season
            if dir_episode_count and not best_root:
                best_root = _resource_root_from_openlist_root(current)
        return {
            "count": 1 if latest_episode else 0,
            "root": _resource_root_from_openlist_root(best_root or resource_root or scan_root),
            "latest_episode": latest_episode or None,
            "latest_season": latest_season,
            "checked_dirs": checked_dirs,
            "checked_files": checked_files,
        }

    def _openlist_dir_matches_subscription(self, path: str, candidates: list[dict[str, str]]) -> bool:
        if not path:
            return False
        name_norm = _norm(basename(path))
        if not name_norm:
            return False
        for candidate in candidates:
            title_norm = _norm(candidate.get("title"))
            cand_norm = candidate.get("norm") or ""
            if cand_norm and name_norm == cand_norm:
                return True
            if title_norm and title_norm in name_norm:
                return True
        return False

    @staticmethod
    def _cloud139_target_for_openlist_root(category: dict[str, Any], canonical_root: str) -> str:
        root = _resource_root_from_openlist_root(canonical_root)
        if not root:
            return ""
        official_root = str(category.get("cloud139_target_path") or "").strip().strip("/")
        if not official_root:
            return ""
        suffix = root.strip("/")
        for key in ("cloud139_fnos_target_path", "openlist_root_path", "mobile_openlist_root_path", "mobile_target_path", "sixpan_fnos_target_path"):
            base = _clean_openlist_root(category.get(key)).strip("/")
            if base and (suffix == base or suffix.startswith(f"{base}/")):
                suffix = suffix[len(base):].strip("/")
                break
        else:
            suffix = _resource_suffix_after_category_anchor(suffix, [posixpath.basename(official_root), category.get("label")]) or posixpath.basename(suffix)
        if suffix:
            return "/".join([official_root.rstrip("/"), suffix.strip("/")]).strip("/")
        return official_root

    def _allow_bootstrap_full_scan(self, subscription: dict[str, Any], indexed_existing: set[tuple[int | None, int]]) -> bool:
        """仅在订阅完全没有本地基线时允许一次全量快照初始化。"""

        scheduler = self._scheduler_config()
        if not _as_bool(scheduler.get("bootstrap_full_scan_enabled"), True):
            return False
        if indexed_existing:
            return False
        if _to_int(subscription.get("last_success_episode")):
            return False
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        if isinstance(raw_data.get("last_existing_sync"), dict):
            return False
        return True

    def scan_existing_episodes(
        self,
        subscription: dict[str, Any],
        *,
        force_refresh: bool = False,
        target_episodes: set[tuple[int | None, int]] | None = None,
        allow_full_scan: bool = False,
    ) -> set[tuple[int | None, int]]:
        seen = self._seen_episodes(subscription)
        target_episodes = set(target_episodes or set())
        if not self.openlist.configured:
            return seen
        category = self.categories.get(str(subscription.get("category") or ""), {})
        if not category:
            return seen
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        configured_root = _resource_root_from_openlist_root(raw_data.get("canonical_openlist_root") or raw_data.get("openlist_path"))
        category_root = _clean_openlist_root(category_target_root(category))
        root = configured_root
        if not root:
            root = category_root
        if allow_full_scan and not force_refresh and (not configured_root or configured_root == category_root):
            allow_full_scan = False
        if not root:
            self.db.add_update_event(
                int(subscription.get("id") or 0) or None,
                None,
                "warn",
                "OpenList 基线扫描跳过：追更订阅未配置有效的分类路径",
                {"category": subscription.get("category"), "raw_openlist_path": subscription.get("raw_data", {}).get("openlist_path") if isinstance(subscription.get("raw_data"), dict) else ""},
            )
            return seen
        snapshot_ttl = int(self._scheduler_config().get("snapshot_ttl_seconds") or 1800)
        snapshot = self.db.get_update_path_snapshot(int(subscription.get("id") or 0), root) if subscription.get("id") and not force_refresh else None
        if snapshot:
            files = snapshot.get("files_json") if isinstance(snapshot.get("files_json"), list) else []
            episodes = self._episodes_from_snapshot(subscription, files)
            if target_episodes:
                episodes |= self._probe_target_episodes_in_openlist(subscription, root, target_episodes)
            return episodes | seen
        if target_episodes and not force_refresh and not allow_full_scan:
            return seen | self._probe_target_episodes_in_openlist(subscription, root, target_episodes)
        if not force_refresh and not allow_full_scan:
            return seen
        try:
            items = self.openlist.scan_videos(root, max_depth=8, max_files=1200)
        except Exception as exc:  # noqa: BLE001
            self.db.add_update_event(int(subscription.get("id") or 0) or None, None, "warn", f"OpenList 基线扫描失败：{exc}", {"root": root})
            return seen
        title_norms = [_norm(subscription.get("title")), *[_norm(item) for item in subscription.get("aliases") or []]]
        expected_season = _to_season(subscription.get("season"))
        episodes: set[tuple[int | None, int]] = set()
        for item in items:
            text = f"{item.path} {item.name}"
            if title_norms and not any(norm and norm in _norm(text) for norm in title_norms):
                continue
            season, episode = self._episode_from_openlist_file(subscription, item.name, item.path)
            season = _first_season(season, expected_season)
            if episode and _season_compatible(expected_season, season):
                episodes.add((season, episode))
                self._record_openlist_seen_item(subscription, item.name, item.path, getattr(item, "size", None), season, episode, source="openlist_full_scan")
        if subscription.get("id"):
            files = [
                {"name": item.name, "path": item.path, "size": getattr(item, "size", None)}
                for item in items
            ]
            latest_episode = max((episode for _season, episode in episodes), default=None)
            latest_season = next((season for season, episode in episodes if episode == latest_episode), None) if latest_episode else None
            try:
                self.db.upsert_update_path_snapshot(
                    subscription_id=int(subscription["id"]),
                    openlist_path=root,
                    files=files,
                    latest_season=latest_season,
                    latest_episode=latest_episode,
                    raw_data={"count": len(files), "source": "openlist"},
                    ttl_seconds=snapshot_ttl,
                )
            except Exception:  # noqa: BLE001
                logger.debug("update path snapshot save failed", exc_info=True)
        return episodes | seen

    def _probe_target_episodes_in_openlist(
        self,
        subscription: dict[str, Any],
        root: str,
        target_episodes: set[tuple[int | None, int]],
    ) -> set[tuple[int | None, int]]:
        """只围绕目标集做浅层探测，避免日常追更递归扫描整棵 OpenList。"""

        if not self.openlist.configured or not root or not target_episodes:
            return set()
        scheduler = self._scheduler_config()
        max_depth = max(0, min(3, int(scheduler.get("target_probe_max_depth") or 2)))
        max_dirs = max(1, int(scheduler.get("target_probe_max_dirs") or 24))
        max_files = max(1, int(scheduler.get("target_probe_max_files") or 400))
        refresh = _as_bool(scheduler.get("target_probe_refresh"), True)
        roots = self._target_probe_roots(subscription, root)
        queue: list[tuple[str, int]] = [(item, 0) for item in roots]
        visited: set[str] = set()
        result: set[tuple[int | None, int]] = set()
        listed_dirs = 0
        checked_files = 0
        expected_season = _to_season(subscription.get("season"))
        while queue and listed_dirs < max_dirs and checked_files < max_files:
            current, depth = queue.pop(0)
            current = _clean_openlist_root(current)
            if not current or current in visited:
                continue
            visited.add(current)
            try:
                rows = self.openlist.list_dir(current, refresh=refresh)
            except Exception:  # noqa: BLE001
                logger.debug("target openlist probe list_dir failed: %s", current, exc_info=True)
                continue
            listed_dirs += 1
            for item in rows:
                if checked_files >= max_files:
                    break
                if item.is_dir:
                    if depth < max_depth and self._should_probe_openlist_child_dir(subscription, item.name, target_episodes):
                        queue.append((item.path, depth + 1))
                    continue
                if not self._is_video_file_name(item.name):
                    continue
                checked_files += 1
                season, episode = self._episode_from_openlist_file(subscription, item.name, item.path)
                season = _first_season(season, expected_season)
                if episode and _episode_in_set(season, episode, target_episodes):
                    result.add((season, episode))
                    self._record_openlist_seen_item(subscription, item.name, item.path, item.size, season, episode, source="openlist_target_probe")
            if target_episodes and all(_episode_in_set(season, episode, result) for season, episode in target_episodes):
                break
        if result:
            self.db.add_update_event(
                int(subscription.get("id") or 0) or None,
                None,
                "info",
                "OpenList 轻量探测发现目标集已存在",
                {
                    "root": root,
                    "episodes": sorted([episode for _season, episode in result]),
                    "target": sorted([episode for _season, episode in target_episodes]),
                    "listed_dirs": listed_dirs,
                    "checked_files": checked_files,
                },
            )
        return result

    def _target_probe_roots(self, subscription: dict[str, Any], root: str) -> list[str]:
        roots: list[str] = []
        root = _resource_root_from_openlist_root(root)
        for item in [root]:
            cleaned = _clean_openlist_root(item)
            if cleaned and cleaned not in roots:
                roots.append(cleaned)
        season = _to_season(subscription.get("season"))
        if season is not None:
            season_names = [
                f"Season {season:02d}",
                f"Season {season}",
                f"S{season:02d}",
                f"S{season}",
                f"第{season}季",
            ]
            for name in season_names:
                path = join_path(root, name)
                if path not in roots:
                    roots.append(path)
        return roots

    def _should_probe_openlist_child_dir(
        self,
        subscription: dict[str, Any],
        name: str,
        target_episodes: set[tuple[int | None, int]],
    ) -> bool:
        if name in {"@eaDir", "#recycle", ".Trash", "System Volume Information"}:
            return False
        expected_season = _to_season(subscription.get("season"))
        parsed = parse_file_name(name, parent_dir=str(subscription.get("title") or ""))
        if parsed.episode and _episode_in_set(_first_season(parsed.season, expected_season), parsed.episode, target_episodes):
            return True
        if parsed.season is not None and _season_compatible(expected_season, parsed.season):
            return True
        name_norm = _norm(name)
        title_norms = [_norm(subscription.get("title")), *[_norm(item) for item in subscription.get("aliases") or []]]
        if any(norm and norm in name_norm for norm in title_norms):
            return True
        return bool(re.fullmatch(r"(?i)(?:season\s*)?\d{1,2}|s\d{1,2}|第\s*\d{1,2}\s*季", str(name or "").strip()))

    @staticmethod
    def _is_video_file_name(name: str) -> bool:
        return posixpath.splitext(str(name or ""))[1].lower() in VIDEO_EXTENSIONS

    def _episode_from_openlist_file(self, subscription: dict[str, Any], name: str, path: str) -> tuple[int | None, int | None]:
        parent_path = dirname(path) if path else ""
        current_dir = basename(parent_path) if parent_path else ""
        grand_parent_path = dirname(parent_path) if parent_path else ""
        parent_dir = basename(grand_parent_path) if grand_parent_path else str(subscription.get("title") or "")
        parsed = parse_file_name(name, current_dir=current_dir, parent_dir=parent_dir)
        return parsed.season, parsed.episode

    def _record_openlist_seen_item(
        self,
        subscription: dict[str, Any],
        name: str,
        path: str,
        size: Any,
        season: int | None,
        episode: int,
        *,
        source: str,
    ) -> None:
        subscription_id = int(subscription.get("id") or 0)
        if not subscription_id or not episode:
            return
        fingerprint_raw = f"openlist|{path or name}|S{_season_key(season)}E{episode}"
        fingerprint = "openlist:" + hashlib.sha256(fingerprint_raw.encode("utf-8", "ignore")).hexdigest()
        try:
            self.db.upsert_update_seen_item(
                {
                    "subscription_id": subscription_id,
                    "fingerprint": fingerprint,
                    "source_type": "openlist",
                    "source_url_hash": hashlib.sha256(str(path or name).encode("utf-8", "ignore")).hexdigest(),
                    "file_id": path or name,
                    "file_name": name,
                    "size": _to_int(size),
                    "season": season,
                    "episode": episode,
                    "raw_data": {"path": path, "name": name, "source": source},
                }
            )
        except Exception:  # noqa: BLE001
            logger.debug("record openlist seen item failed", exc_info=True)

    def _seen_episodes(self, subscription: dict[str, Any]) -> set[tuple[int | None, int]]:
        subscription_id = int(subscription.get("id") or 0)
        if not subscription_id:
            return set()
        try:
            rows = self.db.list_update_seen_episodes(subscription_id)
            return _episodes_for_season(rows, _to_season(subscription.get("season")))
        except Exception:  # noqa: BLE001
            logger.debug("list update seen episodes failed", exc_info=True)
            return set()

    def _inflight_episodes(self, subscription_id: int) -> set[tuple[int | None, int]]:
        try:
            rows = self.db.list_update_candidates(subscription_id=subscription_id, limit=500)
        except Exception:  # noqa: BLE001
            logger.debug("list inflight update candidates failed", exc_info=True)
            return set()
        result: set[tuple[int | None, int]] = set()
        for row in rows:
            if str(row.get("decision") or "") not in {"submitted", "imported"}:
                continue
            status = str(row.get("job_status") or "").strip()
            if status in {"failed", "cancelled", "unsupported", "review", "done", "success"}:
                continue
            episode = _to_int(row.get("episode"))
            if not episode:
                continue
            result.add((_to_season(row.get("season")), episode))
        return result

    def refresh_snapshot(self, subscription_id: int) -> dict[str, Any]:
        subscription = self.db.get_update_subscription(subscription_id, include_sources=True)
        if not subscription:
            raise ValueError("追更订阅不存在")
        episodes = self.scan_existing_episodes(subscription, force_refresh=True)
        return {"success": True, "subscription_id": subscription_id, "episodes": sorted([episode for _season, episode in episodes]), "count": len(episodes)}

    def preview_sources(self, subscription_id: int) -> dict[str, Any]:
        subscription = self.db.get_update_subscription(subscription_id, include_sources=True)
        if not subscription:
            raise ValueError("追更订阅不存在")
        indexed_existing = self._seen_episodes(subscription)
        initial_target_episodes = self._target_episodes(subscription, indexed_existing)
        existing = self.scan_existing_episodes(subscription, target_episodes=initial_target_episodes)
        target_episodes = self._target_episodes(subscription, existing)
        data = dict(subscription)
        data["_target_episode_numbers"] = sorted({episode for _season, episode in target_episodes})
        data["_force_preview_refresh"] = bool(target_episodes)
        discovered = self.discovery.discover(data, subscription.get("sources") or [])
        summary = self._new_candidate_filter_summary(len(discovered))
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for candidate in discovered:
            if candidate.get("error"):
                errors.append(candidate)
                self._count_ignored_candidate(summary, "error")
                continue
            match = self.matcher.match(subscription, candidate, existing_episodes=existing, target_episodes=target_episodes)
            keep_candidate, ignore_reason = self._should_keep_update_candidate(candidate, match, target_episodes)
            if not keep_candidate:
                self._count_ignored_candidate(summary, ignore_reason)
                continue
            decision = match.decision if match.decision == "auto_import" else "skipped"
            items.append(
                {
                    **candidate,
                    "season": match.season,
                    "episode": match.episode,
                    "score": match.score,
                    "decision": decision,
                    "reason": match.reason if decision == "auto_import" else f"{match.reason}；未满足自动入库条件",
                    "raw_data": {"candidate": candidate, "match": match.__dict__},
                }
            )
        summary["kept_count"] = len(items)
        summary["ignored_count"] = max(0, len(discovered) - len(items))
        return {
            "success": True,
            "subscription_id": subscription_id,
            "items": items,
            "errors": errors,
            "count": len(items),
            "discovered_count": len(discovered),
            "filter": summary,
        }

    def filter_display_candidates(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """过滤历史脏数据：未识别集数或非本轮目标上下文的记录不再作为候选展示。"""

        result: list[dict[str, Any]] = []
        for row in rows or []:
            episode = _to_int(row.get("episode"))
            if not episode:
                continue
            raw_data = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
            candidate = raw_data.get("candidate") if isinstance(raw_data.get("candidate"), dict) else {}
            payload = candidate.get("import_payload") if isinstance(candidate.get("import_payload"), dict) else {}
            update_context = payload.get("update_context") if isinstance(payload.get("update_context"), dict) else {}
            target_numbers = {_to_int(item) for item in update_context.get("target_episodes") or []}
            target_numbers = {item for item in target_numbers if item}
            if target_numbers and episode not in target_numbers:
                continue
            result.append(row)
        return result

    def _refresh_tmdb_schedule_for_run(self, subscription: dict[str, Any], run_id: int | None = None) -> dict[str, Any]:
        if str(subscription.get("schedule_kind") or "") != "tmdb":
            return subscription
        tmdb_id = _to_int(subscription.get("tmdb_id"))
        if not tmdb_id or not self.tmdb.configured:
            return subscription
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        hint = self._tmdb_schedule_hint(tmdb_id, str(subscription.get("media_type") or "tv"), subscription)
        if not hint:
            return subscription
        old_hint = raw_data.get("tmdb_schedule") if isinstance(raw_data.get("tmdb_schedule"), dict) else {}
        if hint == old_hint:
            return subscription
        merged_raw = dict(raw_data)
        merged_raw["tmdb_schedule"] = hint
        updates: dict[str, Any] = {"raw_data": merged_raw}
        current_season = _to_season(subscription.get("season"))
        hint_season = _to_season(hint.get("season"))
        if hint_season is not None and current_season is None:
            updates["season"] = hint_season
        hint_episode = _to_int(hint.get("episode"))
        last_success_episode = _to_int(subscription.get("last_success_episode"))
        if hint_episode and (not last_success_episode or hint_episode > last_success_episode):
            current_next = _to_int(subscription.get("next_episode"))
            if not current_next or (last_success_episode is not None and current_next <= last_success_episode) or hint_episode < current_next:
                updates["next_episode"] = hint_episode
        self.db.update_update_subscription(int(subscription["id"]), updates)
        self.db.add_update_event(
            int(subscription["id"]),
            run_id,
            "info",
            "已刷新 TMDB 下一集播出信息",
            {"old": old_hint, "new": hint},
        )
        return self.db.get_update_subscription(int(subscription["id"]), include_sources=True) or {**subscription, **updates}

    def _episodes_from_snapshot(self, subscription: dict[str, Any], files: list[dict[str, Any]]) -> set[tuple[int | None, int]]:
        title_norms = [_norm(subscription.get("title")), *[_norm(item) for item in subscription.get("aliases") or []]]
        expected_season = _to_season(subscription.get("season"))
        episodes: set[tuple[int | None, int]] = set()
        for item in files:
            name = str(item.get("name") or "")
            path = str(item.get("path") or "")
            text = f"{path} {name}"
            if title_norms and not any(norm and norm in _norm(text) for norm in title_norms):
                continue
            season, episode = self._episode_from_openlist_file(subscription, name, path)
            season = _first_season(season, expected_season)
            if episode and _season_compatible(expected_season, season):
                episodes.add((season, episode))
                self._record_openlist_seen_item(subscription, name, path, item.get("size"), season, episode, source="openlist_snapshot")
        return episodes

    def _target_episodes(self, subscription: dict[str, Any], existing: set[tuple[int | None, int]]) -> set[tuple[int | None, int]]:
        season = _to_season(subscription.get("season"))
        explicit = {_to_int(item) for item in subscription.get("missing_episodes") or []}
        explicit = {item for item in explicit if item}
        next_episode = _to_int(subscription.get("next_episode"))
        existing_numbers = {
            episode
            for existing_season, episode in existing
            if season is None or _to_season(existing_season) == season
        }
        schedule_kind = str(subscription.get("schedule_kind") or "")
        if schedule_kind == "tmdb":
            raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
            tmdb_schedule = raw_data.get("tmdb_schedule") if isinstance(raw_data.get("tmdb_schedule"), dict) else {}
            latest_aired_episode = _to_int(tmdb_schedule.get("latest_aired_episode")) or _to_int(tmdb_schedule.get("last_air_episode"))
            latest_aired_season = _first_season(
                tmdb_schedule.get("latest_aired_season"),
                tmdb_schedule.get("last_air_season"),
                season,
            )
            boundary_season, boundary_episode, _boundary_date = self._tmdb_schedule_boundary(subscription, tmdb_schedule)
            schedule_episode = boundary_episode or _to_int(tmdb_schedule.get("episode"))
            schedule_season = _first_season(boundary_season, tmdb_schedule.get("season"), season)
            targets: set[tuple[int | None, int]] = set()

            # 已记录的缺集必须优先继续补，不允许因为 last_success 已被更高集推进而丢失。
            for episode in sorted(explicit - existing_numbers):
                if self._tmdb_episode_due(subscription, season, episode):
                    targets.add((season, episode))

            due_upper = 0
            due_season = season
            has_target_season = season is not None
            for candidate_season, candidate_episode in (
                (latest_aired_season, latest_aired_episode),
                (season, next_episode),
                (schedule_season, schedule_episode),
            ):
                if not candidate_episode:
                    continue
                resolved_season = _first_season(candidate_season, season)
                # 订阅有显式目标季时只按该季推进：跨季集号比较（如 S2E3 vs S1E25）
                # 会生成幻影目标并卡死订阅。无显式季则跟随 TMDB 最新季。
                if has_target_season and resolved_season != season:
                    continue
                if self._tmdb_episode_due(subscription, resolved_season, candidate_episode) and candidate_episode > due_upper:
                    due_upper = candidate_episode
                    due_season = resolved_season

            # TMDB 或固定源可能一次跨过多集，例如本地 E150 时已更新到 E152。
            # 从已有进度到最新已播集一次生成多个目标，单集缺失不会再阻断后续集。
            progress_episode = _to_int(subscription.get("last_success_episode"))
            if not progress_episode and existing_numbers:
                progress_episode = max(existing_numbers)
            if progress_episode and due_upper > progress_episode:
                targets.update(
                    (due_season, episode)
                    for episode in range(progress_episode + 1, due_upper + 1)
                    if episode not in existing_numbers
                )
            elif not progress_episode and due_upper:
                # 新订阅没有本地基线时只检查明确目标；全量基线由 OpenList 首次扫描负责。
                targets.add((due_season, due_upper))
            return self._limit_tmdb_targets(targets, explicit)

        if explicit:
            return {(season, int(item)) for item in explicit if int(item) > 0 and int(item) not in existing_numbers}
        if next_episode:
            return {(season, episode) for episode in range(1, next_episode + 1) if episode not in existing_numbers}
        last_success_episode = _to_int(subscription.get("last_success_episode"))
        if last_success_episode:
            return {(season, last_success_episode + 1)}
        if existing_numbers:
            return {(season, max(existing_numbers) + 1)}
        return set()

    def _limit_tmdb_targets(
        self,
        targets: set[tuple[int | None, int]],
        explicit: set[int],
    ) -> set[tuple[int | None, int]]:
        limit = max(2, int(self._scheduler_config().get("max_episodes_per_run") or 10))
        ordered = sorted(targets, key=lambda item: (item[1], item[0] or 0))
        if len(ordered) <= limit:
            return set(ordered)
        explicit_targets = [item for item in ordered if item[1] in explicit]
        selected = explicit_targets[:limit]
        for item in ordered:
            if len(selected) >= limit:
                break
            if item not in selected:
                selected.append(item)
        # 长时间断更时既补最早缺集，也检查最新已播集，避免旧缺集再次卡死整条订阅。
        if ordered[-1] not in selected:
            selected[-1] = ordered[-1]
        return set(selected)

    def _tmdb_probe_lead(self) -> int:
        """播出日提前探测窗口（分钟）。0 表示关闭，保持原行为。"""
        return max(0, int(self._scheduler_config().get("tmdb_probe_lead_minutes") or 0))

    @staticmethod
    def _tmdb_schedule_boundary(
        subscription: dict[str, Any],
        tmdb_schedule: dict[str, Any],
    ) -> tuple[int | None, int | None, str]:
        """选择与订阅季号兼容的下一播出边界，避免 S00 被其他季排期覆盖。"""

        subscription_season = _to_season(subscription.get("season"))
        candidates = (
            (
                _to_season(tmdb_schedule.get("next_air_season")),
                _to_int(tmdb_schedule.get("next_air_episode")),
                str(tmdb_schedule.get("next_air_date") or "").strip(),
            ),
            (
                _to_season(tmdb_schedule.get("season")),
                _to_int(tmdb_schedule.get("episode")),
                str(tmdb_schedule.get("air_date") or "").strip(),
            ),
        )
        for season, episode, air_date in candidates:
            if not episode or not air_date or not _season_compatible(subscription_season, season):
                continue
            return _first_season(season, subscription_season), episode, air_date
        return None, None, ""

    def _is_tmdb_pre_air_probe(self, subscription: dict[str, Any], target_episodes: set[tuple[int | None, int]]) -> bool:
        """仅当本轮所有目标都有明确的未播出证据时，才视为提前探测。

        一轮可能同时包含历史缺集与即将播出的下一集。只看最大集数会让历史
        缺集的 miss 也不累计补查预算，因此这里必须逐集判断；任何一集已经播出
        或缺乏足够的 TMDB 排期证据，整轮都按正常补查处理。
        """
        if (
            str(subscription.get("schedule_kind") or "") != "tmdb"
            or not self._tmdb_probe_lead()
            or not target_episodes
        ):
            return False
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        tmdb_schedule = raw_data.get("tmdb_schedule") if isinstance(raw_data.get("tmdb_schedule"), dict) else {}
        next_air_season, next_air_episode, air_date = self._tmdb_schedule_boundary(subscription, tmdb_schedule)
        if not next_air_episode or not air_date or self._tmdb_air_date_reached(subscription, air_date, lead_minutes=0):
            return False

        subscription_season = _to_season(subscription.get("season"))
        latest_aired = _to_int(tmdb_schedule.get("latest_aired_episode")) or _to_int(tmdb_schedule.get("last_air_episode")) or 0
        latest_season = _first_season(
            tmdb_schedule.get("latest_aired_season"),
            tmdb_schedule.get("last_air_season"),
            subscription_season,
        )

        for season, episode in target_episodes:
            target_episode = _to_int(episode)
            target_season = _first_season(season, subscription_season, next_air_season, latest_season)
            if not target_episode:
                return False
            if latest_aired:
                if _season_compatible(target_season, latest_season) and target_episode <= latest_aired:
                    return False
            # next_air_episode 是当前唯一有明确未来日期的边界。同季更早的集数
            # 属于历史缺集；更晚的集数可按顺序确定同样尚未播出。
            if not _season_compatible(target_season, next_air_season) or target_episode < next_air_episode:
                return False
        return True

    def _tmdb_episode_due(self, subscription: dict[str, Any], season: int | None, episode: int) -> bool:
        """TMDB 驱动订阅只在目标集到达播出日/检查时间后才进入搜索。

        之前只要 next_episode 存在就会搜索，导致《完美世界》这类 TMDB 已给出
        未来下一集日期的订阅在播出日前反复空跑。这里把 TMDB 最新已播/下一播出
        信息作为门禁：未来集不产生候选、不触发兜底搜索。
        """

        if str(subscription.get("schedule_kind") or "") != "tmdb":
            return True
        raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
        tmdb_schedule = raw_data.get("tmdb_schedule") if isinstance(raw_data.get("tmdb_schedule"), dict) else {}
        if not tmdb_schedule:
            return True
        target_season = _first_season(season, subscription.get("season"), tmdb_schedule.get("season"))
        latest_episode = _to_int(tmdb_schedule.get("latest_aired_episode")) or _to_int(tmdb_schedule.get("last_air_episode"))
        latest_season = _first_season(
            tmdb_schedule.get("latest_aired_season"),
            tmdb_schedule.get("last_air_season"),
            target_season,
        )
        if latest_episode and episode <= latest_episode and _season_compatible(target_season, latest_season):
            latest_date = str(tmdb_schedule.get("latest_aired_date") or tmdb_schedule.get("last_air_date") or "").strip()
            return self._tmdb_air_date_reached(subscription, latest_date)
        schedule_season, schedule_episode, air_date = self._tmdb_schedule_boundary(subscription, tmdb_schedule)
        # TMDB 有明确未来下一集，且目标集大于已播集：不能提前搜索。
        if schedule_episode and episode >= schedule_episode and _season_compatible(target_season, schedule_season):
            return self._tmdb_air_date_reached(subscription, air_date)
        # 没有足够日期证据时不强行阻断，避免 TMDB 字段缺失导致追更停摆。
        return True

    def _tmdb_air_date_reached(self, subscription: dict[str, Any], air_date: Any, *, lead_minutes: int | None = None) -> bool:
        air_day = _parse_date(air_date)
        if not air_day:
            return True
        tz = _safe_zoneinfo(subscription.get("timezone") or "Asia/Shanghai")
        hour, minute = _parse_time(subscription.get("time_of_day") or "12:00")
        check_time = datetime.combine(air_day, time(hour, minute), tzinfo=tz)
        if lead_minutes is None:
            lead_minutes = self._tmdb_probe_lead()
        if lead_minutes:
            check_time = check_time - timedelta(minutes=lead_minutes)
        return datetime.now(timezone.utc) >= check_time.astimezone(timezone.utc)

    def _last_success_episode(self, subscription: dict[str, Any], existing: set[tuple[int | None, int]], import_results: list[dict[str, Any]]) -> int | None:
        subscription_season = _to_season(subscription.get("season"))
        values = [
            episode
            for season, episode in existing
            if subscription_season is None or _to_season(season) == subscription_season
        ]
        values.append(_to_int(subscription.get("last_success_episode")) or 0)
        for result in import_results:
            if result.get("completed"):
                result_episode = _to_int(result.get("episode"))
                if result_episode:
                    values.append(result_episode)
                    continue
                candidate = self.db.get_update_candidate(int(result.get("candidate_id") or 0)) if result.get("candidate_id") else None
                if candidate and candidate.get("episode"):
                    values.append(int(candidate["episode"]))
        return max(values) if values else None

    def _normalize_subscription_payload(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return self.subscription_normalizer.normalize(payload)

    def _tmdb_basic_hint(self, tmdb_id: int | None, media_type: str) -> dict[str, Any]:
        if not tmdb_id or media_type not in {"movie", "tv"} or not self.tmdb.configured:
            return {}
        try:
            details = self.tmdb.details(tmdb_id, media_type) or {}
        except Exception:  # noqa: BLE001
            logger.debug("load tmdb basic hint failed", exc_info=True)
            return {}
        if not details:
            return {}
        return {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": details.get("title") or "",
            "year": details.get("year") or "",
            "status": details.get("status") or "",
        }

    def _tmdb_schedule_hint(self, tmdb_id: int | None, media_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """从 TMDB 取下一集播出信息，作为创建追更任务的默认季/集/调度依据。"""

        if not tmdb_id or media_type != "tv" or not self.tmdb.configured:
            return {}
        try:
            details = self.tmdb.details(tmdb_id, "tv") or {}
        except Exception:  # noqa: BLE001
            logger.debug("load tmdb schedule hint failed", exc_info=True)
            return {}
        next_air = details.get("next_episode_to_air") if isinstance(details.get("next_episode_to_air"), dict) else {}
        last_air = details.get("last_episode_to_air") if isinstance(details.get("last_episode_to_air"), dict) else {}
        next_air_season = _to_season(next_air.get("season_number"))
        next_air_episode = _to_int(next_air.get("episode_number"))
        last_air_season = _to_season(last_air.get("season_number"))
        last_air_episode = _to_int(last_air.get("episode_number"))
        requested_season = _to_season(payload.get("season"))
        season = _first_season(requested_season, next_air_season, last_air_season)
        episode = _to_int(payload.get("next_episode"))
        air_date = ""
        if episode is None and _season_compatible(requested_season, next_air_season):
            episode = next_air_episode
            air_date = str(next_air.get("air_date") or "")
        latest_aired_season = last_air_season if _season_compatible(requested_season, last_air_season) else None
        latest_aired_episode = last_air_episode if latest_aired_season is not None or requested_season is None else None
        latest_aired_date = str(last_air.get("air_date") or "") if latest_aired_episode else ""
        latest_listed_season = latest_aired_season
        latest_listed_episode = latest_aired_episode
        if season is not None:
            try:
                today = datetime.now(timezone.utc).date()
                for row in self.tmdb.season_episodes(tmdb_id, season):
                    row_episode = _to_int(row.get("episode"))
                    row_air_day = _parse_date(row.get("air_date"))
                    if row_episode and (not latest_listed_episode or row_episode > latest_listed_episode):
                        latest_listed_episode = row_episode
                        latest_listed_season = _first_season(row.get("season"), season)
                    if row_episode and row_air_day and row_air_day <= today and (not latest_aired_episode or row_episode > latest_aired_episode):
                        latest_aired_episode = row_episode
                        latest_aired_season = _first_season(row.get("season"), season)
                        latest_aired_date = str(row.get("air_date") or "")
                    if episode and row_episode == episode and row_air_day and not air_date:
                        air_date = str(row.get("air_date") or "")
                    elif not episode and row_episode and row_air_day and row_air_day >= today:
                        episode = row_episode
                        air_date = str(row.get("air_date") or "")
            except Exception:  # noqa: BLE001
                logger.debug("load tmdb season schedule failed", exc_info=True)
        if season is None and not episode and not air_date and not latest_aired_episode:
            return {
                "tmdb_id": tmdb_id,
                "status": details.get("status") or "",
                "title": details.get("title") or "",
                "year": details.get("year") or "",
            }
        return {
            "tmdb_id": tmdb_id,
            "title": details.get("title") or "",
            "year": details.get("year") or "",
            "status": details.get("status") or "",
            "season": season,
            "episode": episode,
            "air_date": air_date,
            "next_air_season": next_air_season,
            "next_air_episode": next_air_episode,
            "next_air_date": str(next_air.get("air_date") or ""),
            "last_air_season": last_air_season,
            "last_air_episode": last_air_episode,
            "last_air_date": str(last_air.get("air_date") or ""),
            "latest_aired_season": latest_aired_season,
            "latest_aired_episode": latest_aired_episode,
            "latest_aired_date": latest_aired_date,
            "latest_listed_season": latest_listed_season,
            "latest_listed_episode": latest_listed_episode,
            "source": "next_episode_to_air" if next_air else "season_episodes",
        }

    def _subscription_path_health(self, category_key: str, category: dict[str, Any], raw_data: dict[str, Any]) -> dict[str, Any]:
        raw_data = raw_data if isinstance(raw_data, dict) else {}
        resolution = raw_data.get("canonical_root_resolution") if isinstance(raw_data.get("canonical_root_resolution"), dict) else {}
        configured_path = (
            raw_data.get("canonical_openlist_root")
            or resolution.get("canonical_openlist_root")
            or raw_data.get("existing_openlist_root")
            or raw_data.get("openlist_path")
            or ""
        )
        configured_root = _resource_root_from_openlist_root(configured_path)
        category_root = _clean_openlist_root(category_target_root(category))
        root = configured_root or category_root
        invalid_path = _invalid_openlist_path(root)
        auto_create_pending = bool(resolution.get("auto_create_pending"))
        source = "已识别标准资源目录" if configured_root else "分类路径"
        path_message = "追更基线扫描路径正常" if root and not invalid_path else "分类路径为空或包含 None，基线扫描会失败"
        if auto_create_pending and configured_root and not invalid_path:
            path_message = "将使用 TMDB 标准保存目录，首次入库时自动创建"
        if not configured_root and raw_data.get("openlist_path"):
            source = "订阅覆盖路径"
        checks: list[dict[str, Any]] = []
        checks.append(
            {
                "name": "openlist_configured",
                "success": self.openlist.configured,
                "message": "OpenList 已配置" if self.openlist.configured else "OpenList 未配置，无法扫描已入库集数",
            }
        )
        checks.append(
            {
                "name": "openlist_path",
                "path": root,
                "success": bool(root) and not invalid_path,
                "message": path_message,
                "source": source,
                "auto_create_pending": auto_create_pending,
            }
        )
        return {
            "success": all(item.get("success") for item in checks),
            "category": category_key,
            "openlist_path": root,
            "checks": checks,
            "message": "追更路径体检通过" if all(item.get("success") for item in checks) else "追更路径体检存在风险，请检查分类路径/OpenList 映射",
        }

    @staticmethod
    def _normalize_source(source: dict[str, Any]) -> dict[str, Any]:
        source_type = _choice(source.get("type"), {"search", "quark", "cloud139", "rss", "webhook", "openlist"}, "search")
        return {
            "type": source_type,
            "name": str(source.get("name") or source_type).strip(),
            "url": str(source.get("url") or "").strip(),
            "password": str(source.get("password") or "").strip(),
            "provider": str(source.get("provider") or "").strip(),
            "priority": _to_int(source.get("priority")) or 100,
            "enabled": bool(source.get("enabled", True)),
            "options": source.get("options") if isinstance(source.get("options"), dict) else {},
        }

    def _compute_next_run(self, data: dict[str, Any], after: datetime | None = None) -> str:
        kind = str(data.get("schedule_kind") or "weekly")
        if kind == "manual":
            return ""
        now_utc = after or datetime.now(timezone.utc)
        if kind == "tmdb":
            tmdb_run = self._compute_tmdb_next_run(data, after=now_utc)
            if tmdb_run:
                return tmdb_run
            # TMDB 暂不可用时降级为每天检查一次，不让订阅彻底失效。
            return _utc_text(now_utc + timedelta(days=1))
        tz = _safe_zoneinfo(data.get("timezone") or "Asia/Shanghai")
        local_now = now_utc.astimezone(tz)
        if kind == "interval":
            minutes = max(5, int(data.get("interval_minutes") or 1440))
            return _utc_text(now_utc + timedelta(minutes=minutes))
        hour, minute = _parse_time(data.get("time_of_day") or "10:00")
        days = _int_list(data.get("days_of_week")) or [local_now.isoweekday()]
        for offset in range(0, 14):
            day = local_now.date() + timedelta(days=offset)
            if kind == "weekly" and day.isoweekday() not in days:
                continue
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            if candidate > local_now:
                return _utc_text(candidate.astimezone(timezone.utc))
        return _utc_text((local_now + timedelta(days=1)).astimezone(timezone.utc))

    def _next_run_after_result(
        self,
        subscription: dict[str, Any],
        *,
        next_episode_value: int | None,
        completed: int,
        submitted: int,
        pending_count: int = 0,
        failed_episodes: set[int] | None = None,
        unresolved_episodes: set[int] | None = None,
        target_episodes: set[tuple[int | None, int]],
        raw_data: dict[str, Any],
    ) -> str:
        return self.next_run_policy.next_run(
            subscription,
            next_episode_value=next_episode_value,
            completed=completed,
            submitted=submitted,
            pending_count=pending_count,
            failed_episodes=failed_episodes,
            unresolved_episodes=unresolved_episodes,
            target_episodes=target_episodes,
            raw_data=raw_data,
            pre_air_probe=self._is_tmdb_pre_air_probe(subscription, target_episodes),
        )

    def _next_run_after_failure(self, subscription: dict[str, Any], trigger_type: str = "schedule") -> str:
        if str(subscription.get("status") or "enabled") != "enabled":
            return ""
        if str(subscription.get("schedule_kind") or "") == "manual":
            return str(subscription.get("next_run_at") or "")
        if str(trigger_type or "") == "manual" and subscription.get("next_run_at"):
            return str(subscription.get("next_run_at") or "")
        scheduler = self._scheduler_config()
        interval = max(
            5,
            int(
                scheduler.get("failure_retry_interval_minutes")
                or scheduler.get("empty_retry_interval_minutes")
                or 30
            ),
        )
        return _utc_text(datetime.now(timezone.utc) + timedelta(minutes=interval))

    def _compute_tmdb_next_run(self, data: dict[str, Any], after: datetime | None = None) -> str:
        tmdb_id = _to_int(data.get("tmdb_id"))
        media_type = str(data.get("media_type") or "tv").strip().lower()
        if not tmdb_id or media_type == "movie" or not self.tmdb.configured:
            return ""
        now_utc = after or datetime.now(timezone.utc)
        tz = _safe_zoneinfo(data.get("timezone") or "Asia/Shanghai")
        local_now = now_utc.astimezone(tz)
        hour, minute = _parse_time(data.get("time_of_day") or "12:00")
        cached_next_run = self._tmdb_next_run_from_cached_hint(data, local_now=local_now, hour=hour, minute=minute)
        if cached_next_run:
            return cached_next_run
        season = _to_season(data.get("season"))
        next_episode = _to_int(data.get("next_episode")) or _to_int(data.get("last_success_episode"))
        if next_episode and _to_int(data.get("last_success_episode")) and next_episode <= int(data.get("last_success_episode") or 0):
            next_episode = int(data.get("last_success_episode") or 0) + 1
        candidates: list[tuple[int | None, int | None, str]] = []
        details = self.tmdb.details(tmdb_id, "tv") or {}
        next_air = details.get("next_episode_to_air") if isinstance(details.get("next_episode_to_air"), dict) else {}
        if next_air:
            candidates.append((_to_season(next_air.get("season_number")), _to_int(next_air.get("episode_number")), str(next_air.get("air_date") or "")))
            if season is None:
                season = _to_season(next_air.get("season_number"))
            if not next_episode:
                next_episode = _to_int(next_air.get("episode_number"))
        seasons_to_check = [season] if season is not None else []
        if not seasons_to_check:
            for item in details.get("seasons") or []:
                if isinstance(item, dict):
                    number = _to_int(item.get("season_number"))
                    if number:
                        seasons_to_check.append(number)
        for season_number in seasons_to_check[:6]:
            for episode in self.tmdb.season_episodes(tmdb_id, int(season_number)):
                candidates.append((_to_season(episode.get("season")), _to_int(episode.get("episode")), str(episode.get("air_date") or "")))
        best: datetime | None = None
        for season_number, episode_number, air_date in candidates:
            if season is not None and season_number is not None and season_number != season:
                continue
            if next_episode and episode_number and episode_number < next_episode:
                continue
            air_day = _parse_date(air_date)
            if not air_day:
                continue
            check_local = datetime.combine(air_day, time(hour, minute), tzinfo=tz) - timedelta(minutes=self._tmdb_probe_lead())
            if check_local <= local_now:
                # 已到播出日/检查时间但还没入库的集数，立即安排检查。
                if next_episode and episode_number and episode_number >= next_episode:
                    return _utc_text(now_utc + timedelta(seconds=1))
                continue
            if best is None or check_local < best:
                best = check_local
        return _utc_text(best.astimezone(timezone.utc)) if best else ""

    def _tmdb_next_run_from_cached_hint(self, data: dict[str, Any], *, local_now: datetime, hour: int, minute: int) -> str:
        raw_data = data.get("raw_data") if isinstance(data.get("raw_data"), dict) else {}
        tmdb_schedule = raw_data.get("tmdb_schedule") if isinstance(raw_data.get("tmdb_schedule"), dict) else {}
        if not tmdb_schedule:
            return ""
        _season, _episode, air_date = self._tmdb_schedule_boundary(data, tmdb_schedule)
        air_day = _parse_date(air_date)
        if not air_day:
            return ""
        check_local = datetime.combine(air_day, time(hour, minute), tzinfo=local_now.tzinfo) - timedelta(minutes=self._tmdb_probe_lead())
        return _utc_text(check_local.astimezone(timezone.utc)) if check_local > local_now else ""



# Compatibility re-export; implementation lives in update_scheduler.py.
from .update_scheduler import UpdateScheduler

from .update_values import allowed_choice, as_bool, non_negative_int, positive_int, positive_int_list, unique_string_list

def _parse_time(value: Any) -> tuple[int, int]:
    match = re.match(r"^\s*(\d{1,2}):(\d{1,2})\s*$", str(value or ""))
    if not match:
        return 12, 0
    return max(0, min(23, int(match.group(1)))), max(0, min(59, int(match.group(2))))


def _safe_zoneinfo(value: Any) -> timezone:
    name = str(value or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        # Windows 精简环境可能没有 IANA tzdata；追更默认按国内时区运行，不能因此崩溃。
        if name in {"Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "Asia/Urumqi", "PRC", "CST"}:
            return timezone(timedelta(hours=8))
        return timezone.utc


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    return positive_int(value)


def _to_season(value: Any) -> int | None:
    return non_negative_int(value)


def _first_season(*values: Any) -> int | None:
    for value in values:
        season = _to_season(value)
        if season is not None:
            return season
    return None


def _season_compatible(expected: int | None, actual: int | None) -> bool:
    return expected is None or actual is None or expected == actual


def _season_key(season: int | None) -> str:
    normalized = _to_season(season)
    return "unknown" if normalized is None else f"{normalized:02d}"


def _episodes_for_season(
    episodes: set[tuple[int | None, int]],
    season: int | None,
) -> set[tuple[int | None, int]]:
    if season is None:
        return set(episodes)
    return {
        (_to_season(item_season), episode)
        for item_season, episode in episodes
        if _to_season(item_season) == season
    }


def _episode_in_set(season: int | None, episode: int, episodes: set[tuple[int | None, int]]) -> bool:
    for target_season, target_episode in episodes:
        if target_episode != episode:
            continue
        if target_season is not None and season is not None and target_season != season:
            continue
        return True
    return False


def _int_list(value: Any) -> list[int]:
    return positive_int_list(value)


def _string_list(value: Any) -> list[str]:
    return unique_string_list(value)


def _choice(value: Any, allowed: set[str], default: str) -> str:
    return allowed_choice(value, allowed, default)


def _norm(value: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _as_bool(value: Any, default: bool = False) -> bool:
    return as_bool(value, default)


def _clean_openlist_root(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if text.lower() in {"", "none", "null", "undefined", "/none", "/null", "/undefined"}:
        return ""
    return "/" + text.strip("/") if text.strip("/") else ""


def _is_season_dir_name(value: Any) -> bool:
    """只识别明确的季目录名，避免把资源根误下沉到 Season 01。"""

    text = basename(str(value or "").strip().replace("\\", "/")).strip()
    if not text:
        return False
    return bool(
        re.fullmatch(r"(?i)season\s*0*\d{1,2}", text)
        or re.fullmatch(r"(?i)s0*\d{1,2}", text)
        or re.fullmatch(r"第\s*(?:\d{1,2}|[零〇一二两三四五六七八九十百]+)\s*季", text)
    )


def _resource_root_from_openlist_root(value: Any) -> str:
    """纠正 Season 层及“剧名/剧名 (年份)”双重资源目录。"""

    root = _clean_openlist_root(value)
    while root and root != "/" and _is_season_dir_name(basename(root)):
        parent = dirname(root)
        if not parent or parent == root:
            break
        root = _clean_openlist_root(parent)
    # 旧版追更可能先按订阅标题创建一层，再由 Organizer 创建 TMDB 标准目录，
    # 形成 /动漫/完美世界/完美世界 (2021)。若父子目录实际是同一标题，
    # 资源根应直接收敛为 /动漫/完美世界 (2021)。
    if root and root != "/":
        child_name = basename(root)
        parent_path = dirname(root)
        parent_name = basename(parent_path)
        child_title, _child_year = split_title_year(child_name)
        parent_title, _parent_year = split_title_year(parent_name)
        if child_title and parent_title and _norm(child_title) == _norm(parent_title):
            root = _clean_openlist_root(join_path(dirname(parent_path), child_name))
    return root


def _canonical_update_title(subscription: dict[str, Any], resource_root: Any, candidates: list[dict[str, str]] | None = None) -> str:
    """追更入库标题必须来自订阅/TMDB/资源根，不能来自 Season 目录。"""

    raw_data = subscription.get("raw_data") if isinstance(subscription.get("raw_data"), dict) else {}
    tmdb_schedule = raw_data.get("tmdb_schedule") if isinstance(raw_data.get("tmdb_schedule"), dict) else {}
    raw_values: list[Any] = [
        tmdb_schedule.get("title"),
        subscription.get("title"),
        *(subscription.get("aliases") or []),
    ]
    for item in candidates or []:
        raw_values.extend([item.get("title"), item.get("display")])
    root = _resource_root_from_openlist_root(resource_root)
    if root:
        raw_values.append(basename(root))
    for value in raw_values:
        text = str(value or "").strip()
        if not text or _is_season_dir_name(text):
            continue
        title, _year = split_title_year(text)
        title = sanitize_resource_dir_name(title or text, fallback="")
        if title and not _is_season_dir_name(title):
            return title
    return str(subscription.get("title") or "").strip()


def _resource_root_standard_score(path: Any, candidates: list[dict[str, str]] | None = None) -> int:
    """目录名越接近 TMDB 标准名分越高：剧名 (年份) > 剧名 > 其它。"""

    name_norm = _norm(basename(_resource_root_from_openlist_root(path)))
    if not name_norm:
        return 0
    score = 0
    for candidate in candidates or []:
        title = str(candidate.get("title") or "").strip()
        year = str(candidate.get("year") or "").strip()
        display = str(candidate.get("display") or "").strip()
        if year and display and name_norm == _norm(display):
            score = max(score, 2)
        elif title and name_norm == _norm(title):
            score = max(score, 1)
    return score


def _resource_suffix_after_category_anchor(path: Any, anchors: list[Any]) -> str:
    parts = [item for item in str(path or "").replace("\\", "/").strip().strip("/").split("/") if item]
    normalized_anchors = {str(item or "").strip().casefold() for item in anchors if str(item or "").strip()}
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].casefold() in normalized_anchors:
            return "/".join(parts[index + 1 :]).strip("/")
    return ""


def _invalid_openlist_path(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/").lower()
    if not text:
        return True
    normalized = text.strip("/")
    return normalized in {"none", "null", "undefined"} or "/none" in f"/{normalized}" or normalized.endswith("/none")


def _source_health_key(source: dict[str, Any] | None) -> str:
    if not isinstance(source, dict):
        return "unknown"
    source_id = source.get("id")
    if source_id not in (None, ""):
        return f"id:{source_id}"
    return f"{source.get('type') or 'unknown'}:{source.get('url') or source.get('name') or 'default'}"


def _candidate_health_key(candidate: dict[str, Any]) -> str:
    source_id = candidate.get("source_id")
    if source_id not in (None, ""):
        return f"id:{source_id}"
    if candidate.get("repair_source"):
        return "repair:search"
    return f"{candidate.get('source_type') or 'unknown'}:{candidate.get('url') or candidate.get('title') or 'candidate'}"
