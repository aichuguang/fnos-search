from __future__ import annotations

import unittest
from typing import Any

from fnos_media_import.services.update_completion_sync_service import UpdateCompletionSyncService


class _FakeDb:
    def __init__(
        self,
        subscription: dict[str, Any],
        candidates: list[dict[str, Any]],
        jobs: dict[int, dict[str, Any]],
        *,
        seen_episodes: set[tuple[int | None, int]] | None = None,
    ) -> None:
        self._subscription = subscription
        self._candidates = candidates
        self._jobs = jobs
        self._seen_episodes = set(seen_episodes or set())
        self.subscription_updates: dict[str, Any] = {}
        self.candidate_updates: list[tuple[int, dict[str, Any]]] = []
        self.events: list[tuple[Any, ...]] = []
        self.fail_subscription_updates = 0
        self.fail_completed_candidate_updates = 0

    def get_update_subscription(self, subscription_id: int, include_sources: bool = True) -> dict[str, Any] | None:
        return dict(self._subscription)

    def list_update_candidates(self, subscription_id: int, limit: int = 500) -> list[dict[str, Any]]:
        return [dict(item) for item in self._candidates]

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return self._jobs.get(int(job_id))

    def update_update_candidate(self, candidate_id: int, **updates: Any) -> None:
        if updates.get("decision") == "completed" and self.fail_completed_candidate_updates > 0:
            self.fail_completed_candidate_updates -= 1
            raise RuntimeError("candidate completion update failed")
        self.candidate_updates.append((candidate_id, dict(updates)))
        for candidate in self._candidates:
            if int(candidate.get("id") or 0) == int(candidate_id):
                candidate.update(updates)
                return

    def list_update_seen_episodes(self, subscription_id: int) -> set[tuple[int | None, int]]:
        return set(self._seen_episodes)

    def update_update_subscription(self, subscription_id: int, updates: dict[str, Any]) -> None:
        if self.fail_subscription_updates > 0:
            self.fail_subscription_updates -= 1
            raise RuntimeError("subscription update failed")
        self.subscription_updates = dict(updates)
        self._subscription.update(updates)

    def add_update_event(self, *args: Any) -> None:
        self.events.append(args)

    def get_update_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return None


def _make(subscription: dict[str, Any]) -> tuple[UpdateCompletionSyncService, _FakeDb]:
    db = _FakeDb(
        subscription,
        [
            {"id": 11, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}},
            {"id": 12, "episode": 5, "decision": "submitted", "job_id": 102, "raw_data": {}},
        ],
        {
            101: {"id": 101, "status": "submitted", "raw_data": {}},
            102: {"id": 102, "status": "done", "raw_data": {"completion": {"status": "done"}}},
        },
    )
    service = UpdateCompletionSyncService(database=db, mark_seen=lambda *_args, **_kwargs: None)
    return service, db


def test_completion_sync_reads_candidates_beyond_first_500_rows() -> None:
    candidates = [{"id": index} for index in range(1, 751)]
    calls: list[tuple[int, int]] = []

    class PaginatedDatabase:
        @staticmethod
        def list_update_candidates(*, subscription_id: int, limit: int, offset: int = 0) -> list[dict[str, Any]]:
            assert subscription_id == 7
            calls.append((limit, offset))
            return candidates[offset : offset + limit]

    service = UpdateCompletionSyncService(
        database=PaginatedDatabase(),
        mark_seen=lambda *_args, **_kwargs: None,
    )

    loaded = service._list_all_candidates(7)

    assert len(loaded) == 750
    assert {item["id"] for item in loaded} == set(range(1, 751))
    assert calls == [(500, 0), (500, 500)]


class OutOfOrderCompletionSyncTests(unittest.TestCase):
    def test_in_flight_episode_not_marked_missing_when_out_of_order(self) -> None:
        # 第 5 集先完成，第 4 集仍在途（submitted）：不应把 4 写入 missing_episodes
        service, db = _make(
            {"id": 1, "title": "测试剧", "last_success_episode": 3, "next_episode": 4, "missing_episodes": [], "raw_data": {}}
        )
        service.sync(1)
        self.assertEqual(db.subscription_updates.get("missing_episodes"), [])

    def test_older_completed_episode_advances_last_success(self) -> None:
        # 4 先完成、5 后完成的顺序：missing 不应包含已完成的 4
        service, db = _make(
            {"id": 1, "title": "测试剧", "last_success_episode": 3, "next_episode": 4, "missing_episodes": [], "raw_data": {}}
        )
        service.sync(1)
        # 已完成 5，last_success_episode 推进到 5，但 missing 为空（4 在途、5 已知）
        self.assertEqual(db.subscription_updates.get("last_success_episode"), 5)
        self.assertEqual(db.subscription_updates.get("missing_episodes"), [])

    def test_in_flight_episode_is_requeued_if_it_fails_on_later_sync(self) -> None:
        service, db = _make(
            {"id": 1, "title": "测试剧", "last_success_episode": 3, "next_episode": 4, "missing_episodes": [], "raw_data": {}}
        )

        first = service.sync(1)
        self.assertEqual(first["completed_episodes"], [5])
        self.assertEqual(db._subscription["last_success_episode"], 5)
        self.assertEqual(db._subscription["next_episode"], 6)
        self.assertEqual(db._subscription["missing_episodes"], [])

        # 第二轮才得知 E4 失败；即使 next_episode 已越过 E4，也必须恢复为缺集。
        db._jobs[101]["status"] = "failed"
        second = service.sync(1)

        self.assertEqual(second["failed_episodes"], [4])
        self.assertEqual(db._subscription["last_success_episode"], 5)
        self.assertEqual(db._subscription["next_episode"], 6)
        self.assertEqual(db._subscription["missing_episodes"], [4])

    def test_review_or_cancelled_episode_is_requeued_on_later_sync(self) -> None:
        for terminal_status in ("review", "cancelled"):
            with self.subTest(status=terminal_status):
                service, db = _make(
                    {
                        "id": 1,
                        "title": "测试剧",
                        "last_success_episode": 3,
                        "next_episode": 4,
                        "missing_episodes": [],
                        "raw_data": {},
                    }
                )
                service.sync(1)
                db._jobs[101]["status"] = terminal_status

                service.sync(1)

                self.assertEqual(db._subscription["missing_episodes"], [4])

    def test_provider_completed_refresh_review_does_not_create_duplicate_missing_episode(self) -> None:
        subscription = {
            "id": 1,
            "title": "测试剧",
            "last_success_episode": 3,
            "next_episode": 4,
            "missing_episodes": [],
            "raw_data": {},
        }
        db = _FakeDb(
            subscription,
            [{"id": 11, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}}],
            {
                101: {
                    "id": 101,
                    "status": "review",
                    "raw_data": {
                        "completion": {
                            "stage": "review",
                            "provider_completed": True,
                            "retry_action": "media_refresh_only",
                        }
                    },
                }
            },
        )
        service = UpdateCompletionSyncService(database=db, mark_seen=lambda *_args, **_kwargs: None)

        result = service.sync(1)

        self.assertEqual(result["completed_episodes"], [4])
        self.assertEqual(db._subscription["missing_episodes"], [])
        self.assertEqual(db._subscription["last_success_episode"], 4)
        self.assertEqual(db._candidates[0]["decision"], "completed")


class SeasonAwareMissingEpisodeTests(unittest.TestCase):
    @staticmethod
    def _subscription() -> dict[str, Any]:
        return {
            "id": 1,
            "title": "测试剧",
            "season": 2,
            "last_success_episode": 3,
            "next_episode": 4,
            "missing_episodes": [],
            "raw_data": {},
        }

    def test_seen_episode_from_other_season_does_not_clear_failed_episode(self) -> None:
        db = _FakeDb(
            self._subscription(),
            [{"id": 11, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}}],
            {101: {"id": 101, "status": "failed", "raw_data": {}}},
            seen_episodes={(1, 4)},
        )
        service = UpdateCompletionSyncService(database=db, mark_seen=lambda *_args, **_kwargs: None)

        service.sync(1)

        self.assertEqual(db._subscription["missing_episodes"], [4])
        self.assertEqual(
            db._subscription["raw_data"]["missing_episode_keys"],
            [{"season": 2, "episode": 4}],
        )

    def test_pending_episode_from_other_season_does_not_suppress_failed_episode(self) -> None:
        db = _FakeDb(
            self._subscription(),
            [
                {"id": 11, "season": 2, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}},
                {"id": 12, "season": 1, "episode": 4, "decision": "submitted", "job_id": 102, "raw_data": {}},
            ],
            {
                101: {"id": 101, "status": "failed", "raw_data": {}},
                102: {"id": 102, "status": "submitted", "raw_data": {}},
            },
        )
        service = UpdateCompletionSyncService(database=db, mark_seen=lambda *_args, **_kwargs: None)

        service.sync(1)

        self.assertEqual(db._subscription["missing_episodes"], [4])
        self.assertEqual(
            db._subscription["raw_data"]["missing_episode_keys"],
            [{"season": 2, "episode": 4}],
        )

    def test_seen_episode_from_same_season_clears_failed_episode(self) -> None:
        db = _FakeDb(
            self._subscription(),
            [{"id": 11, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}}],
            {101: {"id": 101, "status": "failed", "raw_data": {}}},
            seen_episodes={(2, 4)},
        )
        service = UpdateCompletionSyncService(database=db, mark_seen=lambda *_args, **_kwargs: None)

        service.sync(1)

        self.assertEqual(db._subscription["missing_episodes"], [])
        self.assertEqual(db._subscription["raw_data"]["missing_episode_keys"], [])

    def test_pending_episode_from_same_season_suppresses_failed_episode(self) -> None:
        db = _FakeDb(
            self._subscription(),
            [
                {"id": 11, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}},
                {"id": 12, "season": 2, "episode": 4, "decision": "submitted", "job_id": 102, "raw_data": {}},
            ],
            {
                101: {"id": 101, "status": "failed", "raw_data": {}},
                102: {"id": 102, "status": "submitted", "raw_data": {}},
            },
        )
        service = UpdateCompletionSyncService(database=db, mark_seen=lambda *_args, **_kwargs: None)

        service.sync(1)

        self.assertEqual(db._subscription["missing_episodes"], [])
        self.assertEqual(db._subscription["raw_data"]["missing_episode_keys"], [])

    def test_specials_and_season_one_same_episode_remain_distinct(self) -> None:
        cases = (
            {
                "name": "subscription_specials_fallback",
                "subscription_season": 0,
                "failed_season": None,
                "expected_missing_episodes": [4],
            },
            {
                "name": "explicit_specials_candidate",
                "subscription_season": 1,
                "failed_season": 0,
                "expected_missing_episodes": [],
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                failed_candidate = {
                    "id": 11,
                    "episode": 4,
                    "decision": "submitted",
                    "job_id": 101,
                    "raw_data": {},
                }
                if case["failed_season"] is not None:
                    failed_candidate["season"] = case["failed_season"]
                subscription = self._subscription()
                subscription["season"] = case["subscription_season"]
                db = _FakeDb(
                    subscription,
                    [
                        failed_candidate,
                        {
                            "id": 12,
                            "season": 1,
                            "episode": 4,
                            "decision": "submitted",
                            "job_id": 102,
                            "raw_data": {},
                        },
                    ],
                    {
                        101: {"id": 101, "status": "failed", "raw_data": {}},
                        102: {"id": 102, "status": "submitted", "raw_data": {}},
                    },
                    seen_episodes={(1, 4)},
                )
                service = UpdateCompletionSyncService(database=db, mark_seen=lambda *_args, **_kwargs: None)

                service.sync(1)

                self.assertEqual(db._subscription["missing_episodes"], case["expected_missing_episodes"])
                self.assertEqual(
                    db._subscription["raw_data"]["missing_episode_keys"],
                    [{"season": 0, "episode": 4}],
                )


class CompletionRecoveryTests(unittest.TestCase):
    @staticmethod
    def _database() -> _FakeDb:
        return _FakeDb(
            {
                "id": 1,
                "title": "测试剧",
                "season": 2,
                "last_success_episode": 3,
                "next_episode": 4,
                "missing_episodes": [4],
                "raw_data": {},
            },
            [{"id": 11, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}}],
            {101: {"id": 101, "status": "done", "raw_data": {"completion": {"stage": "done"}}}},
        )

    @staticmethod
    def _mark_seen(db: _FakeDb, *, fail_first: bool = False):
        calls = {"count": 0}

        def mark_seen(
            _subscription: dict[str, Any],
            candidate: dict[str, Any],
            *_args: Any,
            **_kwargs: Any,
        ) -> None:
            calls["count"] += 1
            if fail_first and calls["count"] == 1:
                raise RuntimeError("mark seen failed")
            db._seen_episodes.add((candidate.get("season"), int(candidate["episode"])))

        return mark_seen, calls

    def test_mark_seen_failure_keeps_candidate_retryable(self) -> None:
        db = self._database()
        mark_seen, calls = self._mark_seen(db, fail_first=True)
        service = UpdateCompletionSyncService(database=db, mark_seen=mark_seen)

        with self.assertRaisesRegex(RuntimeError, "mark seen failed"):
            service.sync(1)

        self.assertEqual(db._candidates[0]["decision"], "submitted")
        self.assertEqual(db._subscription["last_success_episode"], 3)

        result = service.sync(1)

        self.assertEqual(result["completed_episodes"], [4])
        self.assertEqual(db._candidates[0]["decision"], "completed")
        self.assertEqual(db._subscription["last_success_episode"], 4)
        self.assertEqual(calls["count"], 2)

    def test_subscription_update_failure_keeps_candidate_retryable(self) -> None:
        db = self._database()
        db.fail_subscription_updates = 1
        mark_seen, calls = self._mark_seen(db)
        service = UpdateCompletionSyncService(database=db, mark_seen=mark_seen)

        with self.assertRaisesRegex(RuntimeError, "subscription update failed"):
            service.sync(1)

        self.assertEqual(db._candidates[0]["decision"], "submitted")
        self.assertEqual(db._subscription["last_success_episode"], 3)

        service.sync(1)

        self.assertEqual(db._candidates[0]["decision"], "completed")
        self.assertEqual(db._subscription["last_success_episode"], 4)
        self.assertEqual(calls["count"], 2)

    def test_seen_retry_preserves_structured_missing_episode_seasons(self) -> None:
        db = _FakeDb(
            {
                "id": 1,
                "title": "测试剧",
                "season": 2,
                "last_success_episode": 3,
                "next_episode": 4,
                "missing_episodes": [4],
                "raw_data": {"missing_episode_keys": [{"season": 1, "episode": 4}]},
            },
            [{"id": 11, "season": 2, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}}],
            {101: {"id": 101, "status": "done", "raw_data": {"completion": {"stage": "done"}}}},
        )
        db.fail_subscription_updates = 1
        mark_seen, calls = self._mark_seen(db)
        service = UpdateCompletionSyncService(database=db, mark_seen=mark_seen)

        with self.assertRaisesRegex(RuntimeError, "subscription update failed"):
            service.sync(1)

        self.assertEqual(db._candidates[0]["decision"], "submitted")
        self.assertEqual(
            db._subscription["raw_data"]["missing_episode_keys"],
            [{"season": 1, "episode": 4}],
        )

        service.sync(1)

        self.assertEqual(db._subscription["missing_episodes"], [])
        self.assertEqual(
            db._subscription["raw_data"]["missing_episode_keys"],
            [{"season": 1, "episode": 4}],
        )
        self.assertEqual(db._candidates[0]["decision"], "completed")
        self.assertEqual(calls["count"], 2)

    def test_failed_candidate_waits_for_subscription_update_and_recovers(self) -> None:
        db = _FakeDb(
            {
                "id": 1,
                "title": "测试剧",
                "season": 2,
                "last_success_episode": 5,
                "next_episode": 6,
                "missing_episodes": [],
                "raw_data": {},
            },
            [{"id": 11, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}}],
            {101: {"id": 101, "status": "failed", "raw_data": {}}},
        )
        db.fail_subscription_updates = 1
        service = UpdateCompletionSyncService(database=db, mark_seen=lambda *_args, **_kwargs: None)

        with self.assertRaisesRegex(RuntimeError, "subscription update failed"):
            service.sync(1)

        self.assertEqual(db._candidates[0]["decision"], "submitted")
        self.assertFalse(any(update.get("decision") == "failed" for _candidate_id, update in db.candidate_updates))

        result = service.sync(1)

        self.assertEqual(result["failed_episodes"], [4])
        self.assertEqual(db._candidates[0]["decision"], "failed")
        self.assertEqual(db._subscription["missing_episodes"], [4])
        self.assertEqual(
            db._subscription["raw_data"]["missing_episode_keys"],
            [{"season": 2, "episode": 4}],
        )

    def test_review_candidate_waits_for_subscription_update_and_recovers(self) -> None:
        db = _FakeDb(
            {
                "id": 1,
                "title": "测试剧",
                "season": 2,
                "last_success_episode": 5,
                "next_episode": 6,
                "missing_episodes": [],
                "raw_data": {},
            },
            [{"id": 11, "episode": 4, "decision": "submitted", "job_id": 101, "raw_data": {}}],
            {101: {"id": 101, "status": "review", "raw_data": {}}},
        )
        db.fail_subscription_updates = 1
        service = UpdateCompletionSyncService(database=db, mark_seen=lambda *_args, **_kwargs: None)

        with self.assertRaisesRegex(RuntimeError, "subscription update failed"):
            service.sync(1)

        self.assertEqual(db._candidates[0]["decision"], "submitted")
        self.assertFalse(any(update.get("decision") == "review" for _candidate_id, update in db.candidate_updates))

        result = service.sync(1)

        self.assertEqual(result["review_episodes"], [4])
        self.assertEqual(db._candidates[0]["decision"], "review")
        self.assertEqual(db._subscription["missing_episodes"], [4])
        self.assertEqual(
            db._subscription["raw_data"]["missing_episode_keys"],
            [{"season": 2, "episode": 4}],
        )

    def test_candidate_completion_update_failure_recovers_on_next_sync(self) -> None:
        db = self._database()
        db.fail_completed_candidate_updates = 1
        mark_seen, calls = self._mark_seen(db)
        service = UpdateCompletionSyncService(database=db, mark_seen=mark_seen)

        with self.assertRaisesRegex(RuntimeError, "candidate completion update failed"):
            service.sync(1)

        self.assertEqual(db._candidates[0]["decision"], "submitted")
        self.assertEqual(db._subscription["last_success_episode"], 4)
        self.assertEqual(db._subscription["missing_episodes"], [])

        service.sync(1)

        self.assertEqual(db._candidates[0]["decision"], "completed")
        self.assertEqual(db._subscription["last_success_episode"], 4)
        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
