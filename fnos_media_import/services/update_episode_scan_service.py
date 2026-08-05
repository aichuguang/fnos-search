from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .update_values import non_negative_int, positive_int


EpisodeSet = set[tuple[int | None, int]]


@dataclass(frozen=True)
class UpdateEpisodeScanResult:
    subscription: dict[str, Any]
    root_context: dict[str, Any]
    inflight: EpisodeSet
    indexed_existing: EpisodeSet
    existing: EpisodeSet
    target_episodes: EpisodeSet
    previous_last_success_episode: int | None
    latest_existing_episode: int
    baseline_advanced: bool


class UpdateEpisodeScanService:
    """Refreshes scheduling metadata and computes existing, inflight and target episodes."""

    def __init__(
        self,
        *,
        database: Any,
        refresh_tmdb: Callable[..., dict[str, Any]],
        resolve_root: Callable[[dict[str, Any]], dict[str, Any]],
        inflight_episodes: Callable[[int], EpisodeSet],
        seen_episodes: Callable[[dict[str, Any]], EpisodeSet],
        target_episodes: Callable[[dict[str, Any], EpisodeSet], EpisodeSet],
        scan_existing: Callable[..., EpisodeSet],
        allow_full_scan: Callable[[dict[str, Any], EpisodeSet], bool],
        record_stage: Callable[..., None],
    ) -> None:
        self.database = database
        self.refresh_tmdb = refresh_tmdb
        self.resolve_root = resolve_root
        self.inflight_episodes = inflight_episodes
        self.seen_episodes = seen_episodes
        self.target_episodes = target_episodes
        self.scan_existing = scan_existing
        self.allow_full_scan = allow_full_scan
        self.record_stage = record_stage

    def scan(
        self,
        *,
        subscription_id: int,
        run_id: int,
        subscription: dict[str, Any],
    ) -> UpdateEpisodeScanResult:
        subscription = self.refresh_tmdb(subscription, run_id)
        root_context = self.resolve_root(subscription)
        self.record_stage(run_id, "scan_existing", "读取已知集数并轻量检查目标集")
        previous_last_success = _to_int(subscription.get("last_success_episode"))
        subscription_season = _to_season(subscription.get("season"))
        inflight = _for_subscription_season(self.inflight_episodes(subscription_id), subscription_season)
        indexed_existing = _for_subscription_season(self.seen_episodes(subscription), subscription_season)
        initial_targets = self.target_episodes(subscription, indexed_existing | inflight)
        existing = _for_subscription_season(
            self.scan_existing(
                subscription,
                target_episodes=initial_targets,
                allow_full_scan=self.allow_full_scan(subscription, indexed_existing),
            ),
            subscription_season,
        )
        latest_existing = max((episode for _season, episode in existing), default=0)
        baseline_advanced = bool(latest_existing and latest_existing > (previous_last_success or 0))
        targets = self.target_episodes(subscription, existing | inflight)
        self.record_stage(
            run_id,
            "discover",
            "检查追更来源目标集",
            {
                "existing": _numbers(existing),
                "inflight": _numbers(inflight),
                "target": _numbers(targets),
                "canonical_openlist_root": root_context.get("canonical_openlist_root") or "",
            },
        )
        if not targets:
            self.database.add_update_event(
                subscription_id,
                run_id,
                "info",
                "当前没有需要自动入库的新增集；可能已入库、已有提交等待整理，或 TMDB 暂无下一集",
                {"existing": _numbers(existing), "inflight": _numbers(inflight)},
            )
        return UpdateEpisodeScanResult(
            subscription=subscription,
            root_context=root_context,
            inflight=inflight,
            indexed_existing=indexed_existing,
            existing=existing,
            target_episodes=targets,
            previous_last_success_episode=previous_last_success,
            latest_existing_episode=latest_existing,
            baseline_advanced=baseline_advanced,
        )


def _numbers(values: EpisodeSet) -> list[int]:
    return sorted(episode for _season, episode in values)


def _to_int(value: Any) -> int | None:
    return positive_int(value)


def _to_season(value: Any) -> int | None:
    return non_negative_int(value)


def _for_subscription_season(values: EpisodeSet, season: int | None) -> EpisodeSet:
    if season is None:
        return set(values)
    return {
        (_to_season(item_season), episode)
        for item_season, episode in values
        if _to_season(item_season) == season
    }
