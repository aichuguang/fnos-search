from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CandidateSelectionResult:
    best_by_episode: dict[tuple[int | None, int], tuple[int, dict[str, Any], int]]
    candidate_count: int
    skipped_count: int
    filter_summary: dict[str, Any]


class UpdateCandidateSelectionService:
    """Matches, filters and persists discovered update candidates."""

    def __init__(
        self,
        *,
        database: Any,
        matcher: Any,
        decorate: Callable[..., dict[str, Any]],
        should_keep: Callable[..., tuple[bool, str]],
        new_filter_summary: Callable[[int], dict[str, Any]],
        count_ignored: Callable[[dict[str, Any], str], None],
        record_stage: Callable[..., None],
    ) -> None:
        self.database = database
        self.matcher = matcher
        self.decorate = decorate
        self.should_keep = should_keep
        self.new_filter_summary = new_filter_summary
        self.count_ignored = count_ignored
        self.record_stage = record_stage

    def select(
        self,
        *,
        subscription_id: int,
        run_id: int,
        subscription: dict[str, Any],
        root_context: dict[str, Any],
        target_episodes: set[tuple[int | None, int]],
        existing_episodes: set[tuple[int | None, int]],
        candidates: list[dict[str, Any]],
    ) -> CandidateSelectionResult:
        discovered_count = len(candidates)
        self.record_stage(
            run_id,
            "match",
            "匹配并评分目标候选文件",
            {"discovered_count": discovered_count},
        )
        best_by_episode: dict[tuple[int | None, int], tuple[int, dict[str, Any], int]] = {}
        summary = self.new_filter_summary(discovered_count)
        candidate_count = 0
        skipped = 0
        for source_candidate in candidates:
            if source_candidate.get("error"):
                self.database.add_update_event(
                    subscription_id,
                    run_id,
                    "warn",
                    f"来源发现失败：{source_candidate.get('error')}",
                    source_candidate,
                )
                self.count_ignored(summary, "error")
                continue
            candidate = self.decorate(subscription, source_candidate, root_context, target_episodes)
            match = self.matcher.match(
                subscription,
                candidate,
                existing_episodes=existing_episodes,
                target_episodes=target_episodes,
            )
            keep, ignore_reason = self.should_keep(candidate, match, target_episodes)
            if not keep:
                self.count_ignored(summary, ignore_reason)
                continue
            decision = match.decision if match.decision == "auto_import" else "skipped"
            reason = (
                match.reason
                if decision == "auto_import"
                else f"{match.reason}；定时追更不要求人工确认，未满足自动入库条件则等待下次检查"
            )
            candidate_id = self.database.create_update_candidate(
                {
                    "subscription_id": subscription_id,
                    "run_id": run_id,
                    "source_id": candidate.get("source_id"),
                    "title": candidate.get("title") or subscription.get("title") or "未命名资源",
                    "source_type": candidate.get("source_type") or "unknown",
                    "source_url": str(candidate.get("url") or candidate.get("source_url") or ""),
                    "password": candidate.get("password") or "",
                    "season": match.season,
                    "episode": match.episode,
                    "size_text": candidate.get("size_text") or "",
                    "published_at": candidate.get("published_at") or "",
                    "score": match.score,
                    "decision": decision,
                    "reason": reason,
                    "raw_data": {"candidate": candidate, "match": match.__dict__},
                }
            )
            candidate_count += 1
            if decision == "auto_import" and match.episode:
                key = (match.season, match.episode)
                previous = best_by_episode.get(key)
                if previous is None or match.score > previous[0]:
                    best_by_episode[key] = (match.score, candidate, candidate_id)
            else:
                skipped += 1

        summary["kept_count"] = candidate_count
        summary["ignored_count"] = max(0, discovered_count - candidate_count)
        if candidate_count == 0:
            self.database.add_update_event(
                subscription_id,
                run_id,
                "info",
                "未发现命中目标集的准确候选文件，本轮不自动入库",
                summary,
            )
        if summary.get("ignored_count"):
            self.record_stage(
                run_id,
                "filter_candidates",
                "来源结果未命中本轮目标集，已忽略",
                summary,
            )
        return CandidateSelectionResult(best_by_episode, candidate_count, skipped, summary)
