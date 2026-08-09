from __future__ import annotations

import unittest
from types import SimpleNamespace

from fnos_media_import.organizer.openlist_client import OpenListItem
from fnos_media_import.organizer.parser import (
    V1_DESTINATION_FINAL_FIELD,
    V1_DESTINATION_IS_RESOURCE_FIELD,
    parse_file_name,
    standard_target_path,
)
from fnos_media_import.organizer.service import OrganizerService, _episode_completeness_report
from tests._organizer_v1_fixtures import (
    attach_organizer_v1_task,
    bind_organizer_v1_execution_repository,
    organizer_v1_job,
    organizer_v1_plan,
)
from tests._repository_v1_fakes import RepositoryFakeMixin


def _mapping(
    name: str,
    *,
    season: int | None,
    episode: int | None,
    status: str = "ready",
    raw_data: dict | None = None,
) -> dict:
    return {
        "source_path": f"/staging/{name}",
        "source_name": name,
        "target_path": f"/library/{name}",
        "target_name": name,
        "season": season,
        "episode": episode,
        "status": status,
        "reason": [],
        "raw_data": raw_data or {},
    }


class EpisodeCompletenessReportTests(unittest.TestCase):
    def test_report_tracks_ranges_internal_gaps_duplicates_specials_and_unrecognized(self) -> None:
        mappings = [
            _mapping("Show.S01E01.mkv", season=1, episode=1),
            _mapping("Show.S01E02.1080p.mkv", season=1, episode=2),
            _mapping("Show.S01E02.2160p.mkv", season=1, episode=2),
            _mapping("Show.S01E04.mkv", season=1, episode=4, status="conflict"),
            _mapping("Show.S00E01.mkv", season=0, episode=1),
            _mapping("Show.unknown.mkv", season=1, episode=None, status="need_edit"),
            _mapping("ad.mp4", season=1, episode=None, status="delete_ad", raw_data={"auto_delete_ad": True}),
            _mapping("ignored.mkv", season=1, episode=5, status="ignored"),
            _mapping("Show.S01E01.srt", season=1, episode=1, raw_data={"companion_file": True}),
        ]

        report = _episode_completeness_report("tv", mappings)

        self.assertTrue(report["enabled"])
        self.assertEqual(report["basis"], "observed_span_only")
        self.assertEqual(report["total_video_count"], 8)
        self.assertEqual(report["recognized_file_count"], 5)
        self.assertEqual(report["recognized_episode_count"], 4)
        self.assertEqual(report["unrecognized_count"], 1)
        self.assertEqual(report["special_count"], 1)
        self.assertEqual(report["special_episodes"], [1])
        self.assertEqual(report["missing_count"], 1)
        self.assertEqual(report["duplicate_count"], 1)
        self.assertEqual(report["duplicate_file_count"], 1)
        self.assertEqual(
            report["excluded"],
            {"advertisement_count": 1, "companion_file_count": 1, "ignored_video_count": 1},
        )

        special, season_one = report["seasons"]
        self.assertEqual((special["season"], special["label"], special["ranges"]), (0, "S00", ["E01"]))
        self.assertTrue(special["is_special"])
        self.assertEqual(season_one["ranges"], ["E01-E02", "E04"])
        self.assertEqual(season_one["missing_episodes"], [3])
        self.assertEqual(season_one["duplicates"][0]["episode"], 2)
        self.assertEqual(season_one["duplicates"][0]["count"], 2)
        self.assertEqual(
            season_one["duplicates"][0]["files"],
            ["Show.S01E02.1080p.mkv", "Show.S01E02.2160p.mkv"],
        )
        self.assertEqual(report["unrecognized_files"][0]["name"], "Show.unknown.mkv")

    def test_missing_episodes_only_cover_the_observed_span(self) -> None:
        report = _episode_completeness_report(
            "anime",
            [
                _mapping("Anime.S02E10.mkv", season=2, episode=10),
                _mapping("Anime.S02E12.mkv", season=2, episode=12),
            ],
        )

        season = report["seasons"][0]
        self.assertEqual(season["min_episode"], 10)
        self.assertEqual(season["max_episode"], 12)
        self.assertEqual(season["missing_episodes"], [11])
        self.assertNotIn(1, season["missing_episodes"])

    def test_report_is_not_generated_for_movies(self) -> None:
        self.assertEqual(
            _episode_completeness_report(
                "movie",
                [_mapping("Movie.2025.mkv", season=None, episode=None, status="ready")],
            ),
            {},
        )

    def test_s00_is_preserved_by_parser_and_standard_target_path(self) -> None:
        parsed = parse_file_name(
            "Show.S00E01.mkv",
            current_dir="Season 00",
            parent_dir="Show (2025)",
        )
        self.assertEqual(parsed.season, 0)
        self.assertEqual(parsed.episode, 1)

        target = standard_target_path(
            category_key="tv",
            category={
                V1_DESTINATION_FINAL_FIELD: "/library/tv",
                V1_DESTINATION_IS_RESOURCE_FIELD: False,
            },
            title="Show",
            year="2025",
            season=0,
            episode=1,
            ext=".mkv",
        )
        self.assertEqual(
            target,
            "/library/tv/Show (2025)/Season 00/Show (2025) - S00E01.mkv",
        )

    def test_mapping_update_preserves_s00_and_refreshes_completeness_evidence(self) -> None:
        final_root = "/library/tv/Show (2025)"
        plan = organizer_v1_plan(
            3,
            category="tv",
            organizer_root="/staging/Show",
            destination_final=final_root,
        )
        job = organizer_v1_job(plan)
        mapping = {
            **_mapping("Show.S01E01.mkv", season=1, episode=1),
            "id": 7,
            "media_type": "tv",
            "title": "Show",
            "year": "2025",
            "source_path": "/staging/Show/Show.S01E01.mkv",
            "target_path": f"{final_root}/Season 01/Show (2025) - S01E01.mkv",
        }
        task = attach_organizer_v1_task(
            {
                "id": 3,
                "status": "waiting_review",
                "revision": 1,
                "evidence": {},
                "mappings": [mapping],
            },
            plan,
        )

        class FakeDatabase(RepositoryFakeMixin):
            def get_job(self, _job_id: int) -> dict:
                return job

            def update_organizer_mapping(self, _mapping_id: int, **updates) -> None:
                mapping.update(updates)

            def get_organizer_task(self, _task_id: int) -> dict:
                return task

            def replace_organizer_operations(self, _task_id: int, _operations: list[dict]) -> None:
                return None

            def update_organizer_task(self, _task_id: int, **updates) -> None:
                task.update(updates)

            def update_organizer_mappings_and_plan(
                self,
                _task_id: int,
                *,
                mapping_updates: list[dict],
                operations: list[dict],
                evidence: dict,
                expected_status: str,
                expected_revision: int,
                **_kwargs,
            ) -> bool:
                if task["status"] != expected_status or task["revision"] != expected_revision:
                    return False
                for item in mapping_updates:
                    if int(item["id"]) == int(mapping["id"]):
                        mapping.update(item["updates"])
                task["operations"] = operations
                task["evidence"] = evidence
                task["revision"] += 1
                return True

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.categories = {"tv": {"label": "TV"}}
        bind_organizer_v1_execution_repository(service, plan)

        result = service.update_mapping(
            7,
            {
                "target_path": mapping["target_path"],
                "media_type": "tv",
                "title": "Show",
                "year": "2025",
                "season": 0,
                "episode": 1,
                "status": "ready",
            },
            task_id=3,
        )

        self.assertTrue(result["success"])
        self.assertIn("Season 00", result["target_path"])
        self.assertIn("S00E01", result["target_path"])
        report = task["evidence"]["episode_completeness"]
        self.assertEqual(report["seasons"][0]["season"], 0)
        self.assertEqual(report["special_episodes"], [1])

    def test_build_plan_persists_report_in_summary_evidence(self) -> None:
        plan = organizer_v1_plan(
            1,
            category="variety",
            organizer_root="/staging/Example Show",
            destination_final="/library/variety",
        )
        job = organizer_v1_job(plan)
        task = attach_organizer_v1_task(
            {
                "id": 1,
                "title": "Example Show",
                "source_keyword": "Example Show",
                "raw_data": {},
            },
            plan,
        )
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {"auto_apply_confidence": 85}
        service.openlist = SimpleNamespace(exists=lambda _path: False)
        service.tmdb = SimpleNamespace(configured=False, search=lambda *_args, **_kwargs: [])
        service.ai = SimpleNamespace(configured=False)
        class FakeDatabase(RepositoryFakeMixin):
            @staticmethod
            def get_job(_job_id: int) -> dict:
                return job

            @staticmethod
            def add_organizer_tmdb_match(*_args, **_kwargs) -> None:
                return None

            @staticmethod
            def add_organizer_ai_suggestion(*_args, **_kwargs) -> None:
                return None

        service.db = FakeDatabase()
        bind_organizer_v1_execution_repository(service, plan)
        videos = [
            OpenListItem(
                name="Example.Show.S01E01.mkv",
                path="/staging/Example Show/Example.Show.S01E01.mkv",
                is_dir=False,
                size=1000,
            ),
            OpenListItem(
                name="Example.Show.S01E03.mkv",
                path="/staging/Example Show/Example.Show.S01E03.mkv",
                is_dir=False,
                size=1000,
            ),
        ]

        _files, _mappings, _operations, summary = service._build_plan(
            task,
            "variety",
            {"label": "Variety"},
            videos,
        )

        report = summary["evidence"]["episode_completeness"]
        self.assertIs(summary["episode_completeness"], report)
        self.assertEqual(report["category"], "variety")
        self.assertEqual(report["missing_count"], 1)
        self.assertEqual(report["seasons"][0]["missing_episodes"], [2])


if __name__ == "__main__":
    unittest.main()
