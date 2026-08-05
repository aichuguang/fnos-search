from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class UpdateSourceDiscoveryResult:
    candidates: list[dict[str, Any]]
    source_plan: dict[str, Any]
    source_health_result: dict[str, Any]
    search_used: bool
    fixed_target_hit: bool


class UpdateSourceDiscoveryService:
    """Runs fixed-source discovery, gated search fallback and source-health recording."""

    def __init__(
        self,
        *,
        database: Any,
        discovery: Callable[[], Any],
        select_sources: Callable[..., dict[str, Any]],
        candidates_hit_target: Callable[..., bool],
        record_fixed_gate: Callable[..., dict[str, Any]],
        record_source_health: Callable[..., dict[str, Any]],
        record_stage: Callable[..., None],
    ) -> None:
        self.database = database
        self.discovery = discovery
        self.select_sources = select_sources
        self.candidates_hit_target = candidates_hit_target
        self.record_fixed_gate = record_fixed_gate
        self.record_source_health = record_source_health
        self.record_stage = record_stage

    def discover(
        self,
        *,
        subscription_id: int,
        run_id: int,
        subscription: dict[str, Any],
        target_episodes: set[tuple[int | None, int]],
    ) -> UpdateSourceDiscoveryResult:
        discovery_payload = dict(subscription)
        discovery_payload["_target_episode_numbers"] = sorted({episode for _season, episode in target_episodes})
        discovery_payload["_force_preview_refresh"] = bool(target_episodes)
        source_plan = self.select_sources(subscription, target_episodes)
        client = self.discovery()
        candidates = [] if not target_episodes else client.discover(discovery_payload, source_plan["primary_sources"])
        search_used = False
        fixed_target_hit = self.candidates_hit_target(subscription, candidates, target_episodes)
        gate_raw = self.record_fixed_gate(
            subscription,
            run_id,
            target_episodes,
            candidates,
            source_plan=source_plan,
            target_hit=fixed_target_hit,
            search_used=False,
        )
        if gate_raw:
            subscription["raw_data"] = gate_raw
        search_allowed = bool((gate_raw or {}).get("fixed_source_gate", {}).get("search_allowed"))
        should_search = bool(source_plan.get("fallback_sources")) and bool(target_episodes) and not fixed_target_hit and search_allowed
        if should_search:
            search_used = True
            self.database.add_update_event(
                subscription_id,
                run_id,
                "warn",
                "固定追更源连续未发现目标单集，自动启用一次综合搜索兜底",
                (gate_raw or {}).get("fixed_source_gate") or {},
            )
            repair_candidates = client.discover(discovery_payload, source_plan["fallback_sources"])
            for candidate in repair_candidates:
                if candidate.get("source_id") in (None, ""):
                    candidate["source_id"] = None
                candidate["repair_source"] = True
            candidates.extend(repair_candidates)
            gate_raw = self.record_fixed_gate(
                subscription,
                run_id,
                target_episodes,
                candidates,
                source_plan=source_plan,
                target_hit=fixed_target_hit,
                search_used=True,
            )
            if gate_raw:
                subscription["raw_data"] = gate_raw
        if not candidates:
            self.database.add_update_event(
                subscription_id,
                run_id,
                "info",
                "未发现可检查的来源文件，本轮不自动入库",
            )
        health = self.record_source_health(
            subscription,
            run_id,
            subscription.get("sources") or [],
            candidates,
        )
        if health.get("updated"):
            subscription["raw_data"] = health.get("raw_data") or subscription.get("raw_data") or {}
            self.record_stage(
                run_id,
                "source_health",
                "记录追更源检查状态",
                health.get("summary"),
            )
        return UpdateSourceDiscoveryResult(candidates, source_plan, health, search_used, fixed_target_hit)
