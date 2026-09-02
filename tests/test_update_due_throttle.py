from __future__ import annotations

import unittest
from typing import Any

from fnos_media_import.services.update_due_run_service import UpdateDueRunService
from fnos_media_import.services.update_scheduler import UpdateScheduler


class _FakeUpdateRepo:
    def __init__(self, subscriptions: list[dict[str, Any]]) -> None:
        self.subscriptions = subscriptions

    def list_update_subscriptions(self, *, limit: int, due_before: Any, include_sources: bool) -> list[dict[str, Any]]:
        return self.subscriptions[:limit]


class _FakeDatabase:
    def __init__(self, subscriptions: list[dict[str, Any]]) -> None:
        self.update = _FakeUpdateRepo(subscriptions)


def _subscriptions(count: int) -> list[dict[str, Any]]:
    return [{"id": index, "title": f"剧 {index}"} for index in range(1, count + 1)]


class UpdateDueThrottleTests(unittest.TestCase):
    def test_run_due_sleeps_between_subscriptions_with_jitter(self) -> None:
        sleeps: list[float] = []
        ran: list[int] = []
        service = UpdateDueRunService(
            database=_FakeDatabase(_subscriptions(3)),
            scheduler_config=lambda: {
                "enabled": True,
                "per_subscription_interval_seconds": 7,
                "per_subscription_jitter_seconds": 4,
            },
            run_subscription=lambda subscription_id, trigger_type: ran.append(subscription_id) or {"success": True},
            record_result=lambda result: None,
            sleep=sleeps.append,
        )
        result = service.run_due(trigger_type="schedule")
        self.assertTrue(result["success"])
        self.assertEqual(ran, [1, 2, 3])
        # 3 个订阅 -> 2 次间隔（首条不等待）
        self.assertEqual(len(sleeps), 2)
        for delay in sleeps:
            self.assertGreaterEqual(delay, 7)
            self.assertLessEqual(delay, 11)

    def test_single_subscription_runs_without_delay(self) -> None:
        sleeps: list[float] = []
        service = UpdateDueRunService(
            database=_FakeDatabase(_subscriptions(1)),
            scheduler_config=lambda: {"enabled": True},
            run_subscription=lambda subscription_id, trigger_type: {"success": True},
            record_result=lambda result: None,
            sleep=sleeps.append,
        )
        service.run_due(trigger_type="schedule")
        self.assertEqual(sleeps, [])

    def test_manual_trigger_skips_throttle(self) -> None:
        sleeps: list[float] = []
        service = UpdateDueRunService(
            database=_FakeDatabase(_subscriptions(3)),
            scheduler_config=lambda: {"enabled": True},
            run_subscription=lambda subscription_id, trigger_type: {"success": True},
            record_result=lambda result: None,
            sleep=sleeps.append,
        )
        service.run_due(trigger_type="manual")
        self.assertEqual(sleeps, [])

    def test_disabled_throttle_config_sleeps_zero(self) -> None:
        sleeps: list[float] = []
        service = UpdateDueRunService(
            database=_FakeDatabase(_subscriptions(2)),
            scheduler_config=lambda: {
                "enabled": True,
                "per_subscription_interval_seconds": 0,
                "per_subscription_jitter_seconds": 0,
            },
            run_subscription=lambda subscription_id, trigger_type: {"success": True},
            record_result=lambda result: None,
            sleep=sleeps.append,
        )
        service.run_due(trigger_type="schedule")
        self.assertEqual(sleeps, [])

    def test_subscription_failure_isolated_and_next_still_throttled(self) -> None:
        sleeps: list[float] = []

        def run_subscription(subscription_id: int, trigger_type: str) -> dict[str, Any]:
            if subscription_id == 1:
                raise RuntimeError("boom")
            return {"success": True}

        service = UpdateDueRunService(
            database=_FakeDatabase(_subscriptions(2)),
            scheduler_config=lambda: {"enabled": True},
            run_subscription=run_subscription,
            record_result=lambda result: None,
            sleep=sleeps.append,
        )
        result = service.run_due(trigger_type="schedule")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 2)
        self.assertFalse(result["items"][0]["success"])
        self.assertTrue(result["items"][1]["success"])
        self.assertEqual(len(sleeps), 1)


class SchedulerTickJitterTests(unittest.TestCase):
    def test_next_tick_adds_at_most_ten_percent_jitter(self) -> None:
        scheduler = UpdateScheduler(service=_SchedulerServiceStub(), interval_seconds=100)
        for _ in range(50):
            tick = scheduler._next_tick_seconds()
            self.assertGreaterEqual(tick, 100)
            self.assertLessEqual(tick, 110)

    def test_next_tick_respects_minimum_interval(self) -> None:
        scheduler = UpdateScheduler(service=_SchedulerServiceStub(), interval_seconds=30)
        for _ in range(20):
            tick = scheduler._next_tick_seconds()
            self.assertGreaterEqual(tick, 30)
            self.assertLessEqual(tick, 33)


class _SchedulerServiceStub:
    def __init__(self) -> None:
        self.db = _SchedulerDbStub()


class _SchedulerDbStub:
    class scheduler_leases:
        @staticmethod
        def release(name: str, owner_id: str) -> None:
            return None


if __name__ == "__main__":
    unittest.main()
