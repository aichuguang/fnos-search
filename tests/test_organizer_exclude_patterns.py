from __future__ import annotations

import unittest

from fnos_media_import.organizer.openlist_client import OpenListItem
from fnos_media_import.organizer.service import OrganizerService
from tests._organizer_v1_fixtures import (
    attach_organizer_v1_task,
    bind_organizer_v1_execution_repository,
    organizer_v1_job,
    organizer_v1_plan,
)
from tests._repository_v1_fakes import RepositoryFakeMixin


def _video(name: str) -> OpenListItem:
    return OpenListItem(name=name, path=f"/staging/{name}", is_dir=False, size=1000)


def _service(patterns: list[str] | str) -> OrganizerService:
    plan = organizer_v1_plan(1, category="tv", organizer_root="/staging")
    job = organizer_v1_job(plan)
    task = attach_organizer_v1_task(
        {"id": 1, "title": "Show", "raw_data": {}, "status": "scanning"},
        plan,
    )

    class FakeDatabase(RepositoryFakeMixin):
        @staticmethod
        def get_job(_job_id: int) -> dict:
            return job

        @staticmethod
        def get_organizer_task(_task_id: int) -> dict:
            return task

    service = OrganizerService.__new__(OrganizerService)
    service.organizer_config = {"exclude_file_patterns": patterns}
    service.db = FakeDatabase()
    bind_organizer_v1_execution_repository(service, plan)
    return service, task


class ExcludeFilePatternsTests(unittest.TestCase):
    def test_videos_matching_patterns_are_excluded(self) -> None:
        service, task = _service(["广告", "推广", "trailer"])
        videos = [
            _video("Show.S01E01.mkv"),
            _video("最新网址广告.mp4"),
            _video("Show.S01E02.trailer.mkv"),
        ]
        kept, excluded = service._filter_videos_for_task(task, videos)
        self.assertEqual([item.name for item in kept], ["Show.S01E01.mkv"])
        self.assertEqual([item.name for item in excluded], ["最新网址广告.mp4", "Show.S01E02.trailer.mkv"])

    def test_pattern_matching_is_case_insensitive(self) -> None:
        service, task = _service(["SAMPLE"])
        kept, excluded = service._filter_videos_for_task(task, [_video("Show.S01E01.Sample.mkv")])
        self.assertEqual(kept, [])
        self.assertEqual(len(excluded), 1)

    def test_companions_are_filtered_by_pattern(self) -> None:
        service, task = _service(["扫码"])
        companions = [
            _video("Show.S01E01.srt"),
            _video("扫码领福利.txt"),
        ]
        kept, excluded = service._filter_companions_for_task(task, companions)
        self.assertEqual([item.name for item in kept], ["Show.S01E01.srt"])
        self.assertEqual([item.name for item in excluded], ["扫码领福利.txt"])

    def test_empty_config_keeps_everything(self) -> None:
        service, task = _service([])
        videos = [_video("最新网址广告.mp4")]
        kept, excluded = service._filter_videos_for_task(task, videos)
        self.assertEqual(kept, videos)
        self.assertEqual(excluded, [])

    def test_blank_pattern_entries_are_ignored(self) -> None:
        service, task = _service(["  ", ""])
        kept, excluded = service._filter_videos_for_task(task, [_video("广告.mp4")])
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])

    def test_non_list_config_is_treated_as_disabled(self) -> None:
        service, task = _service("广告")
        kept, excluded = service._filter_videos_for_task(task, [_video("广告.mp4")])
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])


if __name__ == "__main__":
    unittest.main()
