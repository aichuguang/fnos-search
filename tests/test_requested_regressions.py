from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from fnos_media_import.services.update_candidate_batch_import_service import UpdateCandidateBatchImportService
from fnos_media_import.services.update_completion_sync_service import UpdateCompletionSyncService
from fnos_media_import.services.update_run_failure_service import UpdateRunFailureService
from fnos_media_import.services.update_next_run_policy import UpdateNextRunPolicy
from fnos_media_import.services.update_run_outcome_service import UpdateRunOutcomeInput, UpdateRunOutcomeService
from fnos_media_import.services.update_service import UpdateService


class AdminTabRefreshTests(unittest.TestCase):
    def test_switching_admin_tabs_forces_server_refresh(self) -> None:
        source = Path("static/admin-bootstrap.js").read_text(encoding="utf-8")
        self.assertIn('ensureTabLoaded(name, { force: true })', source)


class UpdateGapPlanningTests(unittest.TestCase):
    @staticmethod
    def _service(latest_due: int = 999) -> UpdateService:
        service = UpdateService.__new__(UpdateService)
        service.config = {"update_scheduler": {"max_episodes_per_run": 10}}
        service._tmdb_episode_due = lambda _subscription, _season, episode: episode <= latest_due
        return service

    def test_tmdb_jump_from_episode_150_to_152_targets_both_episodes(self) -> None:
        service = self._service(latest_due=152)
        subscription = {
            "schedule_kind": "tmdb",
            "season": 1,
            "last_success_episode": 150,
            "next_episode": 151,
            "missing_episodes": [],
            "raw_data": {
                "tmdb_schedule": {
                    "latest_aired_season": 1,
                    "latest_aired_episode": 152,
                }
            },
        }

        targets = service._target_episodes(subscription, {(1, 150)})

        self.assertEqual(targets, {(1, 151), (1, 152)})

    def test_old_missing_episode_survives_after_higher_episode_advances(self) -> None:
        service = self._service(latest_due=152)
        subscription = {
            "schedule_kind": "tmdb",
            "season": 1,
            "last_success_episode": 152,
            "next_episode": 153,
            "missing_episodes": [151, 152],
            "raw_data": {
                "tmdb_schedule": {
                    "latest_aired_season": 1,
                    "latest_aired_episode": 152,
                }
            },
        }

        targets = service._target_episodes(subscription, {(1, 152)})

        self.assertEqual(targets, {(1, 151)})


class UpdateBatchIsolationTests(unittest.TestCase):
    def test_one_episode_failure_does_not_stop_later_episode(self) -> None:
        calls: list[int] = []
        failed: list[tuple[int, str]] = []
        stages: list[tuple] = []

        def import_candidate(candidate_id: int, **_kwargs):
            calls.append(candidate_id)
            if candidate_id == 151:
                raise RuntimeError("E151 transfer failed")
            return {"success": True, "submitted": True, "completed": False, "candidate_id": candidate_id}

        service = UpdateCandidateBatchImportService(
            import_candidate=import_candidate,
            record_stage=lambda *args: stages.append(args),
            mark_failed=lambda candidate_id, message: failed.append((candidate_id, message)),
        )

        result = service.import_best(
            subscription_id=5,
            run_id=9,
            best_by_episode={
                (1, 151): (100, {"title": "151.mkv"}, 151),
                (1, 152): (100, {"title": "152.mkv"}, 152),
            },
        )

        self.assertEqual(calls, [151, 152])
        self.assertEqual(result.submitted_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.items[0]["episode"], 151)
        self.assertTrue(result.items[0]["retryable"])
        self.assertEqual(result.items[1]["candidate_id"], 152)
        self.assertEqual(failed, [(151, "E151 transfer failed")])
        self.assertTrue(any(stage[1] == "import_failed" for stage in stages))


class _OutcomeDatabase:
    def __init__(self) -> None:
        self.subscription_updates: list[tuple[int, dict]] = []
        self.run_updates: list[tuple[int, dict]] = []
        self.events: list[tuple] = []

    def update_update_subscription(self, subscription_id: int, values: dict) -> None:
        self.subscription_updates.append((subscription_id, values))

    def update_update_run(self, run_id: int, **values) -> None:
        self.run_updates.append((run_id, values))

    def add_update_event(self, *args) -> None:
        self.events.append(args)


class UpdateOutcomeGapTests(unittest.TestCase):
    def test_higher_episode_success_keeps_lower_failed_episode_missing(self) -> None:
        database = _OutcomeDatabase()
        next_run_values: dict = {}
        service = UpdateRunOutcomeService(
            database=database,
            last_success_episode=lambda _subscription, _existing, _imports: 152,
            finish_reason=lambda **_values: "完成",
            next_run=lambda _subscription, **values: next_run_values.update(values) or "next-run",
            record_stage=lambda *_args: None,
        )
        data = UpdateRunOutcomeInput(
            subscription_id=5,
            run_id=9,
            subscription={
                "next_episode": 151,
                "last_success_episode": 150,
                "last_success_at": "",
                "missing_episodes": [],
                "raw_data": {},
            },
            existing={(1, 150)},
            inflight=set(),
            target_episodes={(1, 151), (1, 152)},
            import_results=[
                {"success": False, "completed": False, "episode": 151},
                {"success": True, "completed": True, "episode": 152},
            ],
            sync_result={"checked": 0},
            source_health_result={},
            candidate_filter={},
            source_plan={},
            root_context={},
            candidate_count=2,
            discovered_count=2,
            submitted=1,
            completed=1,
            skipped=1,
            search_used=False,
            fixed_target_hit=True,
            previous_last_success_episode=150,
            latest_existing_episode=150,
            baseline_advanced=False,
        )

        result = service.finalize(data)

        update = database.subscription_updates[0][1]
        self.assertEqual(update["last_success_episode"], 152)
        self.assertEqual(update["next_episode"], 153)
        self.assertEqual(update["missing_episodes"], [151])
        self.assertEqual(result["failed_episodes"], [151])
        self.assertEqual(next_run_values["unresolved_episodes"], {151})


class UpdatePartialFailureRetryTests(unittest.TestCase):
    def test_partial_success_still_uses_short_failure_retry(self) -> None:
        policy = UpdateNextRunPolicy(
            scheduler_config=lambda: {"failure_retry_interval_minutes": 20},
            compute_next_run=lambda *_args, **_kwargs: "regular-next-run",
            now=lambda: datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc),
        )
        raw_data: dict = {}

        next_run = policy.next_run(
            {"schedule_kind": "tmdb"},
            next_episode_value=153,
            completed=1,
            submitted=1,
            failed_episodes={151},
            target_episodes={(1, 151), (1, 152)},
            raw_data=raw_data,
        )

        self.assertEqual(next_run, "2026-07-28T00:20:00Z")
        self.assertEqual(raw_data["failed_import_retry"]["episodes"], [151])

    def test_partial_completion_with_pending_episode_stays_on_short_check(self) -> None:
        policy = UpdateNextRunPolicy(
            scheduler_config=lambda: {"pending_import_check_interval_minutes": 15},
            compute_next_run=lambda *_args, **_kwargs: "regular-next-run",
            now=lambda: datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc),
        )
        raw_data: dict = {}

        next_run = policy.next_run(
            {"schedule_kind": "tmdb"},
            next_episode_value=153,
            completed=1,
            submitted=2,
            pending_count=1,
            target_episodes={(1, 151), (1, 152)},
            raw_data=raw_data,
        )

        self.assertEqual(next_run, "2026-07-28T00:15:00Z")
        self.assertIn("pending_import", raw_data)

    def test_partial_completion_with_unresolved_gap_stays_on_short_retry(self) -> None:
        policy = UpdateNextRunPolicy(
            scheduler_config=lambda: {"failure_retry_interval_minutes": 25},
            compute_next_run=lambda *_args, **_kwargs: "regular-next-run",
            now=lambda: datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc),
        )
        raw_data: dict = {}

        next_run = policy.next_run(
            {"schedule_kind": "tmdb"},
            next_episode_value=153,
            completed=1,
            submitted=1,
            unresolved_episodes={151},
            target_episodes={(1, 151), (1, 152)},
            raw_data=raw_data,
        )

        self.assertEqual(next_run, "2026-07-28T00:25:00Z")
        self.assertEqual(raw_data["missing_episode_retry"]["episodes"], [151])


class _CompletionDatabase:
    def __init__(self) -> None:
        self.subscription = {
            "id": 5,
            "last_success_episode": 150,
            "next_episode": 151,
            "missing_episodes": [151, 152],
            "raw_data": {},
        }
        self.candidate = {"id": 12, "job_id": 20, "decision": "submitted", "episode": 152, "season": 1, "raw_data": {}}
        self.updates: list[dict] = []

    def get_update_subscription(self, subscription_id: int, *, include_sources: bool = False):
        return dict(self.subscription) if subscription_id == 5 and include_sources else None

    def list_update_candidates(self, **_kwargs):
        return [dict(self.candidate)]

    def get_job(self, job_id: int):
        return {"id": job_id, "status": "done", "raw_data": {"completion": {"stage": "done"}}}

    def update_update_candidate(self, _candidate_id: int, **_values) -> None:
        return None

    def update_update_subscription(self, _subscription_id: int, values: dict) -> None:
        self.updates.append(values)

    def add_update_event(self, *_args) -> None:
        return None

    def list_update_seen_episodes(self, _subscription_id: int):
        return {(1, 152)}


class UpdateCompletionGapTests(unittest.TestCase):
    def test_out_of_order_completion_keeps_intermediate_gap(self) -> None:
        database = _CompletionDatabase()
        service = UpdateCompletionSyncService(database=database, mark_seen=lambda *_args, **_kwargs: None)

        service.sync(5)

        final_update = database.updates[-1]
        self.assertEqual(final_update["last_success_episode"], 152)
        self.assertEqual(final_update["next_episode"], 153)
        self.assertEqual(final_update["missing_episodes"], [151])


class _FailureDatabase:
    def __init__(self) -> None:
        self.run_updates: list[tuple[int, dict]] = []
        self.subscription_updates: list[tuple[int, dict]] = []
        self.events: list[tuple] = []

    def update_update_run(self, run_id: int, **values) -> None:
        self.run_updates.append((run_id, values))

    def get_update_subscription(self, subscription_id: int, *, include_sources: bool = False):
        if subscription_id != 5 or include_sources:
            return None
        return {"id": 5, "status": "enabled", "schedule_kind": "tmdb", "raw_data": {}}

    def update_update_subscription(self, subscription_id: int, values: dict) -> None:
        self.subscription_updates.append((subscription_id, values))

    def add_update_event(self, *args) -> None:
        self.events.append(args)


class UpdateFailureRetryTests(unittest.TestCase):
    def test_failed_scheduled_run_gets_a_future_retry_time(self) -> None:
        database = _FailureDatabase()
        service = UpdateRunFailureService(
            database=database,
            record_stage=lambda *_args: None,
            next_retry_at=lambda _subscription, _trigger: "2026-07-28T08:00:00Z",
        )

        service.record(
            subscription_id=5,
            run_id=9,
            error=RuntimeError("provider failed"),
            candidate_count=1,
            submitted_count=0,
            skipped_count=0,
            trigger_type="schedule",
        )

        update = database.subscription_updates[0][1]
        self.assertEqual(update["next_run_at"], "2026-07-28T08:00:00Z")
        self.assertEqual(update["raw_data"]["last_run_failure"]["run_id"], 9)
        self.assertIn("自动重试", database.events[0][3])


if __name__ == "__main__":
    unittest.main()
