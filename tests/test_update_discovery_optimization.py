from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fnos_media_import.services.update_next_run_policy import UpdateNextRunPolicy
from fnos_media_import.services.update_service import UpdateService


class _FakeDb:
    def __init__(self) -> None:
        self.saved: dict[int, dict[str, Any]] = {}
        self.events: list[tuple[Any, ...]] = []

    def update_update_subscription(self, subscription_id: int, updates: dict[str, Any]) -> None:
        self.saved[subscription_id] = updates

    def add_update_event(self, *args: Any) -> None:
        self.events.append(args)


def _service(config: dict[str, Any] | None = None) -> tuple[UpdateService, _FakeDb]:
    service = UpdateService.__new__(UpdateService)
    service.config = {"update_scheduler": dict(config or {})}
    service.db = _FakeDb()
    return service, service.db


def _subscription(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 1,
        "title": "测试剧",
        "schedule_kind": "tmdb",
        "timezone": "Asia/Shanghai",
        "time_of_day": "00:00",
        "raw_data": {},
    }
    data.update(overrides)
    return data


class FallbackThresholdTests(unittest.TestCase):
    def test_default_threshold_without_health(self) -> None:
        service, _ = _service()
        self.assertEqual(service._fixed_source_fallback_threshold(), 4)
        self.assertEqual(service._fixed_source_fallback_threshold(_subscription()), 4)

    def test_unhealthy_fixed_source_lowers_threshold(self) -> None:
        service, _ = _service()
        subscription = _subscription(
            raw_data={
                "source_health": {
                    "k1": {"type": "cloud139", "consecutive_error": 4, "consecutive_empty": 0},
                }
            }
        )
        self.assertEqual(service._fixed_source_fallback_threshold(subscription), 2)

    def test_consecutive_empty_also_lowers_threshold(self) -> None:
        service, _ = _service()
        subscription = _subscription(
            raw_data={
                "source_health": {
                    "k1": {"type": "quark", "consecutive_error": 0, "consecutive_empty": 5},
                }
            }
        )
        self.assertEqual(service._fixed_source_fallback_threshold(subscription), 2)

    def test_search_source_health_does_not_lower_threshold(self) -> None:
        service, _ = _service()
        subscription = _subscription(
            raw_data={
                "source_health": {
                    "k1": {"type": "search", "consecutive_error": 10, "consecutive_empty": 10},
                }
            }
        )
        self.assertEqual(service._fixed_source_fallback_threshold(subscription), 4)

    def test_below_warn_does_not_lower_threshold(self) -> None:
        service, _ = _service()
        subscription = _subscription(
            raw_data={
                "source_health": {
                    "k1": {"type": "cloud139", "consecutive_error": 2, "consecutive_empty": 0},
                }
            }
        )
        self.assertEqual(service._fixed_source_fallback_threshold(subscription), 4)

    def test_config_empty_retry_max_attempts_override(self) -> None:
        service, _ = _service({"empty_retry_max_attempts": 6})
        self.assertEqual(service._fixed_source_fallback_threshold(), 6)


class TmdbProbeTests(unittest.TestCase):
    def test_air_date_reached_respects_lead(self) -> None:
        service, _ = _service({"tmdb_probe_lead_minutes": 1440})
        subscription = _subscription(time_of_day="00:00")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        # 不含 lead：明天 00:00 未到
        self.assertFalse(service._tmdb_air_date_reached(subscription, tomorrow, lead_minutes=0))
        # 带 lead(24h)：检查点回到今天 00:00，已到
        self.assertTrue(service._tmdb_air_date_reached(subscription, tomorrow))

    def test_air_date_reached_without_lead_unchanged(self) -> None:
        service, _ = _service()  # lead 默认 0
        subscription = _subscription(time_of_day="00:00")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertFalse(service._tmdb_air_date_reached(subscription, tomorrow))

    def test_pre_air_probe_true_for_upcoming_episode(self) -> None:
        service, _ = _service({"tmdb_probe_lead_minutes": 120})
        subscription = _subscription(
            raw_data={
                "tmdb_schedule": {
                    "next_air_episode": 5,
                    "next_air_date": "2999-01-01",
                    "latest_aired_episode": 4,
                }
            }
        )
        self.assertTrue(service._is_tmdb_pre_air_probe(subscription, {(None, 5)}))

    def test_pre_air_probe_false_when_target_already_aired(self) -> None:
        service, _ = _service({"tmdb_probe_lead_minutes": 120})
        subscription = _subscription(
            raw_data={
                "tmdb_schedule": {
                    "next_air_episode": 5,
                    "next_air_date": "2999-01-01",
                    "latest_aired_episode": 5,
                }
            }
        )
        self.assertFalse(service._is_tmdb_pre_air_probe(subscription, {(None, 4)}))

    def test_pre_air_probe_false_for_mixed_historical_and_upcoming_targets(self) -> None:
        service, _ = _service({"tmdb_probe_lead_minutes": 120})
        subscription = _subscription(
            raw_data={
                "tmdb_schedule": {
                    "next_air_episode": 5,
                    "next_air_date": "2999-01-01",
                    "latest_aired_episode": 4,
                }
            }
        )
        # E4 是已播历史缺集，E5 才是提前探测；不能因为最大集数是 E5 就压制整轮预算。
        self.assertFalse(service._is_tmdb_pre_air_probe(subscription, {(None, 4), (None, 5)}))

    def test_pre_air_probe_false_when_lead_disabled(self) -> None:
        service, _ = _service()
        subscription = _subscription(
            raw_data={
                "tmdb_schedule": {
                    "next_air_episode": 5,
                    "next_air_date": "2999-01-01",
                    "latest_aired_episode": 4,
                }
            }
        )
        self.assertFalse(service._is_tmdb_pre_air_probe(subscription, {(None, 5)}))


class FixedSourceGateProbeGuardTests(unittest.TestCase):
    def test_pre_air_probe_miss_does_not_increment_attempts(self) -> None:
        service, db = _service({"tmdb_probe_lead_minutes": 120})
        subscription = _subscription(
            raw_data={
                "tmdb_schedule": {
                    "next_air_episode": 5,
                    "next_air_date": "2999-01-01",
                    "latest_aired_episode": 4,
                }
            }
        )
        source_plan = {
            "fixed_sources": [{"id": 1, "type": "cloud139", "enabled": True}],
            "threshold": 4,
            "target_key": "5",
        }
        raw_data = service._record_fixed_source_gate(
            subscription,
            run_id=10,
            target_episodes={(None, 5)},
            candidates=[],
            source_plan=source_plan,
            target_hit=False,
            search_used=False,
        )
        gate = raw_data.get("fixed_source_gate") or {}
        self.assertEqual(gate.get("attempts"), 0)
        self.assertFalse(gate.get("search_allowed"))

    def test_normal_miss_still_increments_attempts(self) -> None:
        service, db = _service()  # lead 默认 0
        subscription = _subscription()
        source_plan = {
            "fixed_sources": [{"id": 1, "type": "cloud139", "enabled": True}],
            "threshold": 4,
            "target_key": "5",
        }
        raw_data = service._record_fixed_source_gate(
            subscription,
            run_id=10,
            target_episodes={(None, 5)},
            candidates=[],
            source_plan=source_plan,
            target_hit=False,
            search_used=False,
        )
        gate = raw_data.get("fixed_source_gate") or {}
        self.assertEqual(gate.get("attempts"), 1)

    def test_mixed_target_miss_still_increments_attempts(self) -> None:
        service, _ = _service({"tmdb_probe_lead_minutes": 120})
        subscription = _subscription(
            raw_data={
                "tmdb_schedule": {
                    "next_air_episode": 5,
                    "next_air_date": "2999-01-01",
                    "latest_aired_episode": 4,
                }
            }
        )
        source_plan = {
            "fixed_sources": [{"id": 1, "type": "cloud139", "enabled": True}],
            "threshold": 4,
            "target_key": "4,5",
        }
        raw_data = service._record_fixed_source_gate(
            subscription,
            run_id=10,
            target_episodes={(None, 4), (None, 5)},
            candidates=[],
            source_plan=source_plan,
            target_hit=False,
            search_used=False,
        )
        self.assertEqual(raw_data["fixed_source_gate"]["attempts"], 1)

    def test_pre_air_probe_clears_stale_fixed_source_attempts(self) -> None:
        service, _ = _service({"tmdb_probe_lead_minutes": 120})
        subscription = _subscription(
            raw_data={
                "tmdb_schedule": {
                    "next_air_episode": 5,
                    "next_air_date": "2999-01-01",
                    "latest_aired_episode": 4,
                },
                "fixed_source_gate": {
                    "target_key": "5",
                    "attempts": 4,
                    "search_allowed": True,
                },
            }
        )
        source_plan = {
            "fixed_sources": [{"id": 1, "type": "cloud139", "enabled": True}],
            "threshold": 4,
            "target_key": "5",
        }
        raw_data = service._record_fixed_source_gate(
            subscription,
            run_id=10,
            target_episodes={(None, 5)},
            candidates=[],
            source_plan=source_plan,
            target_hit=False,
            search_used=False,
        )
        gate = raw_data["fixed_source_gate"]
        self.assertEqual(gate["attempts"], 0)
        self.assertFalse(gate["search_allowed"])


class NextRunPreAirProbeTests(unittest.TestCase):
    FIXED_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def _policy(self) -> UpdateNextRunPolicy:
        return UpdateNextRunPolicy(
            scheduler_config=lambda: {
                "empty_retry_interval_minutes": 30,
                "empty_retry_max_attempts": 4,
                "empty_retry_exhausted_interval_hours": 6,
            },
            compute_next_run=lambda *_args, **_kwargs: "2099-01-01T00:00:00Z",
            now=lambda: self.FIXED_NOW,
        )

    def test_pre_air_probe_does_not_consume_retry_budget(self) -> None:
        policy = self._policy()
        raw_data: dict[str, Any] = {
            "tmdb_retry": {"episode": 5, "attempts": 3},
            "tmdb_wait": {"episode": 5},
        }
        result = policy.next_run(
            {"schedule_kind": "tmdb", "id": 1},
            next_episode_value=5,
            completed=0,
            submitted=0,
            target_episodes={(None, 5)},
            raw_data=raw_data,
            pre_air_probe=True,
        )
        # 不消耗 tmdb_retry 预算，改为 tmdb_probe 探测间隔
        self.assertNotIn("tmdb_retry", raw_data)
        self.assertIn("tmdb_probe", raw_data)
        self.assertNotIn("tmdb_wait", raw_data)
        self.assertEqual(result, _utc_text(self.FIXED_NOW + timedelta(minutes=30)))

    def test_post_air_miss_still_uses_retry_budget(self) -> None:
        policy = self._policy()
        raw_data: dict[str, Any] = {"tmdb_probe": {"episode": 5}}
        result = policy.next_run(
            {"schedule_kind": "tmdb", "id": 1},
            next_episode_value=5,
            completed=0,
            submitted=0,
            target_episodes={(None, 5)},
            raw_data=raw_data,
            pre_air_probe=False,
        )
        self.assertIn("tmdb_retry", raw_data)
        self.assertEqual(raw_data["tmdb_retry"]["attempts"], 1)
        self.assertNotIn("tmdb_probe", raw_data)

    def test_mixed_target_miss_consumes_retry_budget_for_historical_gap(self) -> None:
        policy = self._policy()
        raw_data: dict[str, Any] = {"tmdb_probe": {"episode": 5}}
        result = policy.next_run(
            {"schedule_kind": "tmdb", "id": 1},
            next_episode_value=6,
            completed=0,
            submitted=0,
            target_episodes={(None, 4), (None, 5)},
            raw_data=raw_data,
            pre_air_probe=False,
        )
        self.assertEqual(raw_data["tmdb_retry"]["episode"], 4)
        self.assertEqual(raw_data["tmdb_retry"]["attempts"], 1)
        self.assertNotIn("tmdb_probe", raw_data)
        self.assertEqual(result, _utc_text(self.FIXED_NOW + timedelta(minutes=30)))


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    unittest.main()
