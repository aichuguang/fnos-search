from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from fnos_media_import.services.update_episode_scan_service import UpdateEpisodeScanService
from fnos_media_import.services.update_service import UpdateService, _episode_in_set
from fnos_media_import.updater.discovery import UpdateDiscovery
from fnos_media_import.updater.matcher import UpdateMatcher, episode_from_text


class _CandidateDb:
    def __init__(self) -> None:
        self.seen = {(None, 1), (0, 2), (1, 3)}
        self.candidates = [
            {"decision": "submitted", "job_status": "waiting_openlist", "season": 0, "episode": 4},
            {"decision": "submitted", "job_status": "waiting_openlist", "season": 1, "episode": 4},
        ]
        self.seen_writes: list[dict[str, Any]] = []

    def list_update_seen_episodes(self, _subscription_id: int) -> set[tuple[int | None, int]]:
        return set(self.seen)

    def list_update_candidates(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.candidates)

    def upsert_update_seen_item(self, data: dict[str, Any]) -> bool:
        self.seen_writes.append(data)
        return True


class _Tmdb:
    configured = True

    def __init__(self) -> None:
        self.season_calls: list[int] = []

    def details(self, _tmdb_id: int, _media_type: str) -> dict[str, Any]:
        return {
            "title": "测试特别篇",
            "year": "2026",
            "status": "Returning Series",
            "next_episode_to_air": {
                "season_number": 1,
                "episode_number": 7,
                "air_date": "2027-01-01",
            },
            "last_episode_to_air": {
                "season_number": 1,
                "episode_number": 6,
                "air_date": "2026-12-01",
            },
            "seasons": [{"season_number": 0}, {"season_number": 1}],
        }

    def season_episodes(self, _tmdb_id: int, season: int) -> list[dict[str, Any]]:
        self.season_calls.append(season)
        if season == 0:
            return [
                {"season": 0, "episode": 1, "air_date": "2026-01-01"},
                {"season": 0, "episode": 2, "air_date": "2028-01-01"},
            ]
        return [{"season": season, "episode": 7, "air_date": "2027-01-01"}]


def _service(config: dict[str, Any] | None = None) -> UpdateService:
    service = UpdateService.__new__(UpdateService)
    service.config = {"update_scheduler": {"max_episodes_per_run": 10, **(config or {})}}
    return service


class SpecialsMatcherTests(unittest.TestCase):
    def test_episode_parser_preserves_s00(self) -> None:
        self.assertEqual(episode_from_text("测试剧.S00E01.mkv", 0), (0, 1))

    def test_s00_target_does_not_match_explicit_other_season(self) -> None:
        matcher = UpdateMatcher()
        match = matcher.match(
            {"title": "测试剧", "season": 0, "min_score": 75},
            {"title": "测试剧.S01E01.mkv", "file_level": True, "source_type": "magnet"},
            existing_episodes=set(),
            target_episodes={(0, 1)},
        )

        self.assertEqual(match.season, 1)
        self.assertNotEqual(match.decision, "auto_import")
        self.assertIn("季号不匹配", match.reason)
        self.assertFalse(_episode_in_set(1, 1, {(0, 1)}))

    def test_missing_season_candidate_inherits_explicit_s00(self) -> None:
        matcher = UpdateMatcher()
        match = matcher.match(
            {"title": "测试剧", "season": 0, "min_score": 75},
            {"title": "测试剧.E01.mkv", "file_level": True, "source_type": "magnet"},
            existing_episodes=set(),
            target_episodes={(0, 1)},
        )

        self.assertEqual(match.season, 0)
        self.assertEqual(match.episode, 1)
        self.assertEqual(match.decision, "auto_import")


class SpecialsDiscoveryTests(unittest.TestCase):
    def test_search_queries_include_s00_and_s00_episode(self) -> None:
        discovery = UpdateDiscovery(
            search_service=None,
            quark_importer=None,
            cloud139_importer=None,
            routes={},
        )

        queries = discovery._queries(
            {
                "title": "测试特别篇",
                "season": 0,
                "_target_episode_numbers": [2],
            }
        )

        self.assertIn("测试特别篇 S00", queries)
        self.assertIn("测试特别篇 S00E02", queries)

    def test_snapshot_scan_keeps_s00_separate_from_s01(self) -> None:
        service = _service()
        service.db = _CandidateDb()
        subscription = {"id": 1, "title": "测试特别篇", "aliases": [], "season": 0}

        episodes = service._episodes_from_snapshot(
            subscription,
            [
                {"name": "测试特别篇.S00E01.mkv", "path": "/动漫/测试特别篇/Season 00/测试特别篇.S00E01.mkv"},
                {"name": "测试特别篇.S01E01.mkv", "path": "/动漫/测试特别篇/Season 01/测试特别篇.S01E01.mkv"},
            ],
        )

        self.assertEqual(episodes, {(0, 1)})
        self.assertEqual([item["season"] for item in service.db.seen_writes], [0])


class SpecialsEpisodePlanningTests(unittest.TestCase):
    def test_target_generation_ignores_existing_episode_from_other_season(self) -> None:
        service = _service()
        service._tmdb_episode_due = lambda _subscription, _season, _episode: True
        subscription = {
            "schedule_kind": "tmdb",
            "season": 0,
            "last_success_episode": 1,
            "next_episode": 2,
            "missing_episodes": [],
            "raw_data": {
                "tmdb_schedule": {
                    "latest_aired_season": 0,
                    "latest_aired_episode": 2,
                }
            },
        }

        targets = service._target_episodes(subscription, {(0, 1), (1, 2)})

        self.assertEqual(targets, {(0, 2)})

    def test_seen_and_inflight_rows_preserve_explicit_s00(self) -> None:
        service = _service()
        service.db = _CandidateDb()

        self.assertEqual(service._seen_episodes({"id": 1, "season": 0}), {(0, 2)})
        self.assertEqual(service._inflight_episodes(1), {(0, 4), (1, 4)})

    def test_episode_scan_filters_other_seasons_before_progress_calculation(self) -> None:
        captured: list[set[tuple[int | None, int]]] = []

        def targets(_subscription: dict[str, Any], existing: set[tuple[int | None, int]]) -> set[tuple[int | None, int]]:
            captured.append(set(existing))
            return {(0, 3)}

        service = UpdateEpisodeScanService(
            database=type("Db", (), {"add_update_event": lambda *_args, **_kwargs: None})(),
            refresh_tmdb=lambda subscription, _run_id: subscription,
            resolve_root=lambda _subscription: {},
            inflight_episodes=lambda _subscription_id: {(0, 2), (1, 99)},
            seen_episodes=lambda _subscription: {(0, 1), (1, 98)},
            target_episodes=targets,
            scan_existing=lambda *_args, **_kwargs: {(0, 1), (0, 2), (1, 100)},
            allow_full_scan=lambda *_args: False,
            record_stage=lambda *_args: None,
        )

        result = service.scan(
            subscription_id=1,
            run_id=2,
            subscription={"id": 1, "season": 0, "last_success_episode": 1},
        )

        self.assertEqual(result.inflight, {(0, 2)})
        self.assertEqual(result.indexed_existing, {(0, 1)})
        self.assertEqual(result.existing, {(0, 1), (0, 2)})
        self.assertEqual(result.latest_existing_episode, 2)
        self.assertTrue(all(all(season == 0 for season, _episode in rows) for rows in captured))


class SpecialsTmdbScheduleTests(unittest.TestCase):
    def test_pre_air_probe_uses_matching_s00_schedule(self) -> None:
        service = _service({"tmdb_probe_lead_minutes": 120})
        subscription = {
            "schedule_kind": "tmdb",
            "season": 0,
            "timezone": "Asia/Shanghai",
            "time_of_day": "00:00",
            "raw_data": {
                "tmdb_schedule": {
                    "next_air_season": 1,
                    "next_air_episode": 7,
                    "next_air_date": "2998-01-01",
                    "season": 0,
                    "episode": 2,
                    "air_date": "2999-01-01",
                    "latest_aired_season": 0,
                    "latest_aired_episode": 1,
                }
            },
        }

        self.assertTrue(service._is_tmdb_pre_air_probe(subscription, {(0, 2)}))
        self.assertFalse(service._is_tmdb_pre_air_probe(subscription, {(1, 2)}))

    def test_other_season_future_date_does_not_block_s00_historical_gap(self) -> None:
        service = _service()
        subscription = {
            "schedule_kind": "tmdb",
            "season": 0,
            "timezone": "Asia/Shanghai",
            "time_of_day": "00:00",
            "raw_data": {
                "tmdb_schedule": {
                    "next_air_season": 1,
                    "next_air_episode": 2,
                    "next_air_date": "2999-01-01",
                    "latest_aired_season": 0,
                    "latest_aired_episode": 1,
                }
            },
        }

        self.assertTrue(service._tmdb_episode_due(subscription, 0, 2))

    def test_schedule_hint_queries_season_zero_instead_of_global_next_season(self) -> None:
        service = _service()
        service.tmdb = _Tmdb()

        hint = service._tmdb_schedule_hint(99, "tv", {"season": 0, "next_episode": 2})

        self.assertEqual(service.tmdb.season_calls, [0])
        self.assertEqual(hint["season"], 0)
        self.assertEqual(hint["episode"], 2)
        self.assertEqual(hint["air_date"], "2028-01-01")
        self.assertEqual(hint["latest_aired_season"], 0)
        self.assertEqual(hint["latest_aired_episode"], 1)
        self.assertEqual(hint["next_air_season"], 1)

    def test_next_run_for_s00_uses_specials_schedule_not_other_season(self) -> None:
        service = _service()
        service.tmdb = _Tmdb()

        next_run = service._compute_tmdb_next_run(
            {
                "tmdb_id": 99,
                "media_type": "tv",
                "season": 0,
                "next_episode": 2,
                "timezone": "Asia/Shanghai",
                "time_of_day": "00:00",
                "raw_data": {},
            },
            after=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

        self.assertIn(0, service.tmdb.season_calls)
        self.assertEqual(next_run, "2027-12-31T16:00:00Z")


if __name__ == "__main__":
    unittest.main()
