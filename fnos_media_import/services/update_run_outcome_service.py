from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..database import utc_now

from .update_values import non_negative_int, positive_int


@dataclass
class UpdateRunOutcomeInput:
    subscription_id: int
    run_id: int
    subscription: dict[str, Any]
    existing: set[tuple[int | None, int]]
    inflight: set[tuple[int | None, int]]
    target_episodes: set[tuple[int | None, int]]
    import_results: list[dict[str, Any]]
    sync_result: dict[str, Any]
    source_health_result: dict[str, Any]
    candidate_filter: dict[str, Any]
    source_plan: dict[str, Any]
    root_context: dict[str, Any]
    candidate_count: int
    discovered_count: int
    submitted: int
    completed: int
    skipped: int
    search_used: bool
    fixed_target_hit: bool
    previous_last_success_episode: int | None
    latest_existing_episode: int
    baseline_advanced: bool
    owner_id: str = "legacy"


class UpdateRunOutcomeService:
    """Persists the successful outcome and summary of an update run."""

    def __init__(
        self,
        *,
        database: Any,
        last_success_episode: Callable[..., int | None],
        finish_reason: Callable[..., str],
        next_run: Callable[..., str],
        record_stage: Callable[..., None],
    ) -> None:
        self.database = database
        self.last_success_episode = last_success_episode
        self.finish_reason = finish_reason
        self.next_run = next_run
        self.record_stage = record_stage

    def finalize(self, data: UpdateRunOutcomeInput) -> dict[str, Any]:
        completed_episodes = _completed_episode_numbers(self.database, data.import_results)
        failed_episodes = _failed_episode_numbers(self.database, data.import_results)
        last_success = self.last_success_episode(data.subscription, data.existing, data.import_results)
        next_episode = _to_int(data.subscription.get("next_episode"))
        if last_success and (not next_episode or next_episode <= last_success):
            next_episode = last_success + 1
        elif data.completed and last_success:
            next_episode = max(next_episode or 0, last_success + 1)
        raw_data = dict(data.subscription.get("raw_data")) if isinstance(data.subscription.get("raw_data"), dict) else {}
        raw_data.pop("last_run_failure", None)
        finish_reason = self.finish_reason(
            target_episodes=data.target_episodes,
            candidate_count=data.candidate_count,
            discovered_count=data.discovered_count,
            submitted=data.submitted,
            completed=data.completed,
            failed_import_count=len(failed_episodes),
            candidate_filter=data.candidate_filter,
        )
        raw_data["last_run_outcome"] = {
            "run_id": data.run_id,
            "target_episodes": _episode_numbers(data.target_episodes),
            "candidate_count": data.candidate_count,
            "submitted_count": data.submitted,
            "completed_count": data.completed,
            "failed_episodes": failed_episodes,
            "reason": finish_reason,
            "checked_at": utc_now(),
        }
        baseline_sync: dict[str, Any] = {}
        if data.baseline_advanced:
            baseline_sync = {
                "from_episode": data.previous_last_success_episode,
                "to_episode": data.latest_existing_episode,
                "latest_existing_episode": data.latest_existing_episode,
                "canonical_openlist_root": data.root_context.get("canonical_openlist_root") or "",
                "source": "openlist_snapshot",
                "message": "OpenList 目录已存在新增集，已更新追更基线；这不是本轮提交入库，不会生成入库任务或 Organizer 整理记录。",
            }
            raw_data["last_existing_sync"] = {**baseline_sync, "synced_at": utc_now()}
            self.record_stage(data.run_id, "sync_existing", "扫描到 OpenList 目录已有新增集，更新追更基线", baseline_sync)
            self.database.add_update_event(
                data.subscription_id,
                data.run_id,
                "info",
                f"OpenList 已存在 E{data.latest_existing_episode}，已更新追更基线（非本轮提交）",
                baseline_sync,
            )
        missing_episodes = _unresolved_missing_episodes(
            subscription=data.subscription,
            existing=data.existing,
            targets=data.target_episodes,
            completed_episodes=completed_episodes,
            last_success_episode=last_success,
        )
        unresolved_episodes = set(missing_episodes)
        next_run_at = self.next_run(
            data.subscription,
            next_episode_value=next_episode,
            completed=data.completed,
            submitted=data.submitted,
            pending_count=len(data.inflight) + max(0, data.submitted - data.completed),
            failed_episodes=set(failed_episodes),
            unresolved_episodes=unresolved_episodes,
            target_episodes=data.target_episodes,
            raw_data=raw_data,
        )
        self.database.update_update_subscription(
            data.subscription_id,
            {
                "last_run_at": utc_now(),
                "last_success_at": utc_now() if data.completed else data.subscription.get("last_success_at") or "",
                "last_success_episode": last_success,
                "next_episode": next_episode,
                "next_run_at": next_run_at,
                "missing_episodes": missing_episodes,
                "raw_data": raw_data,
            },
        )
        summary = {
            "candidate_count": data.candidate_count,
            "discovered_count": data.discovered_count,
            "submitted_count": data.submitted,
            "completed_count": data.completed,
            "imported_count": data.submitted,
            "skipped_count": data.skipped,
            "existing_episodes": _episode_numbers(data.existing),
            "inflight_episodes": _episode_numbers(data.inflight),
            "target_episodes": _episode_numbers(data.target_episodes),
            "imports": data.import_results,
            "failed_episodes": failed_episodes,
            "missing_episodes": missing_episodes,
            "completion_sync": data.sync_result,
            "baseline_sync": baseline_sync,
            "source_health": data.source_health_result.get("summary") if isinstance(data.source_health_result, dict) else {},
            "candidate_filter": data.candidate_filter,
            "finish_reason": finish_reason,
            "source_strategy": {
                "primary_source_count": len(data.source_plan.get("primary_sources") or []),
                "fallback_source_count": len(data.source_plan.get("fallback_sources") or []),
                "search_used": data.search_used,
                "fixed_target_hit": data.fixed_target_hit,
            },
            "canonical_openlist_root": data.root_context.get("canonical_openlist_root") or "",
        }
        self.record_stage(data.run_id, "finish", "定时追更执行完成", summary)
        finish = getattr(self.database, "finish_update_run", None)
        if callable(finish):
            finished = finish(
                data.run_id,
                data.owner_id,
                status="success",
                candidate_count=data.candidate_count,
                imported_count=data.submitted,
                skipped_count=data.skipped,
                summary=summary,
            )
        else:
            self.database.update_update_run(
                data.run_id,
                status="success",
                candidate_count=data.candidate_count,
                imported_count=data.submitted,
                skipped_count=data.skipped,
                summary=summary,
            )
            finished = True
        if not finished:
            raise RuntimeError("追更运行完成时已失去租约，拒绝覆盖运行状态")
        self.database.add_update_event(data.subscription_id, data.run_id, "info", "定时追更执行完成", summary)
        return {"success": True, "run_id": data.run_id, **summary}


def _episode_numbers(values: set[tuple[int | None, int]]) -> list[int]:
    return sorted(episode for _season, episode in values)


def _completed_episode_numbers(database: Any, results: list[dict[str, Any]]) -> set[int]:
    return {
        episode
        for item in results
        if item.get("completed") and (episode := _result_episode(database, item))
    }


def _failed_episode_numbers(database: Any, results: list[dict[str, Any]]) -> list[int]:
    return sorted(
        {
            episode
            for item in results
            if not item.get("success") and (episode := _result_episode(database, item))
        }
    )


def _result_episode(database: Any, item: dict[str, Any]) -> int | None:
    episode = _to_int(item.get("episode"))
    if episode:
        return episode
    candidate_id = _to_int(item.get("candidate_id"))
    get_candidate = getattr(database, "get_update_candidate", None)
    if not candidate_id or not callable(get_candidate):
        return None
    try:
        candidate = get_candidate(candidate_id)
    except Exception:  # noqa: BLE001
        return None
    return _to_int(candidate.get("episode")) if isinstance(candidate, dict) else None


def _unresolved_missing_episodes(
    *,
    subscription: dict[str, Any],
    existing: set[tuple[int | None, int]],
    targets: set[tuple[int | None, int]],
    completed_episodes: set[int],
    last_success_episode: int | None,
) -> list[int]:
    subscription_season = _to_season(subscription.get("season"))
    known = {
        episode
        for season, episode in existing
        if subscription_season is None or _to_season(season) == subscription_season
    } | completed_episodes
    previous_success = _to_int(subscription.get("last_success_episode"))
    previous_missing = {
        episode
        for value in subscription.get("missing_episodes") or []
        if (episode := _to_int(value))
    }
    target_numbers = {episode for _season, episode in targets}
    inferred_gaps: set[int] = set()
    if previous_success and last_success_episode and last_success_episode > previous_success:
        inferred_gaps = set(range(previous_success + 1, last_success_episode + 1))
    return sorted((previous_missing | target_numbers | inferred_gaps) - known)


def _to_int(value: Any) -> int | None:
    return positive_int(value)


def _to_season(value: Any) -> int | None:
    return non_negative_int(value)
