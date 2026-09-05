from __future__ import annotations

import unittest
from types import SimpleNamespace

from fnos_media_import.organizer.openlist_client import OpenListItem
from fnos_media_import.organizer.service import OrganizerService
from tests._organizer_v1_fixtures import (
    attach_organizer_v1_task,
    bind_organizer_v1_execution_repository,
    organizer_v1_job,
    organizer_v1_plan,
)
from tests._repository_v1_fakes import RepositoryFakeMixin


def _tmdb_movie(title: str, year: str, tmdb_id: int) -> dict:
    return {
        "id": tmdb_id,
        "media_type": "movie",
        "title": title,
        "original_title": title,
        "year": year,
        "overview": "",
        "poster_path": "",
    }


class AiTmdbConfidenceTests(unittest.TestCase):
    """AI 校准标题经 TMDB 精确确认后，置信度必须按命中分数计入。

    回归场景：文件名是英文/混淆标题（“Dear You (2026)”），规则与直查 TMDB
    无法命中，AI 校准给出正确中文标题“给阿嬷的情书”，AI 标题再查 TMDB 精确
    命中。旧逻辑只按原始候选回算置信度（77 分），永远过不了 85 的自动执行
    线，导致每个正确任务都进人工确认。
    """

    def test_ai_title_confirmed_by_tmdb_exact_match_becomes_auto_applicable(self) -> None:
        plan = organizer_v1_plan(
            3,
            category="movie",
            organizer_root="/移动云/_入库暂存/电影/job-3",
            destination_final="/移动云/电影",
        )
        job = organizer_v1_job(plan)
        task = attach_organizer_v1_task(
            {
                "id": 3,
                "title": "Dear You (2026)",
                "source_keyword": "Dear You (2026)",
                "raw_data": {},
            },
            plan,
        )

        def fake_search(query: str, media_type: str = "auto") -> list[dict]:
            # 英文文件名查不到（返回空），AI 中文标题精确命中。
            if "给阿嬷的情书" in str(query):
                return [_tmdb_movie("给阿嬷的情书", "2026", 990123)]
            return []

        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {"auto_apply_confidence": 85}
        service.openlist = SimpleNamespace(exists=lambda _path: False)
        service.tmdb = SimpleNamespace(configured=True, search=fake_search)
        service.ai = SimpleNamespace(
            configured=True,
            calibrate=lambda _evidence: {
                "media_type": "movie",
                "title": "给阿嬷的情书",
                "year": "2026",
                "season": None,
                "episode_rule": "",
                "tmdb_queries": [],
                "confidence": 0.7,
                "requires_review": False,
                "reasons": [],
            },
            model="test-model",
        )

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
                name="Dear You (2026) - 1080p H264.mkv",
                path="/移动云/_入库暂存/电影/job-3/Dear You (2026) - 1080p H264.mkv",
                is_dir=False,
                size=1000,
            )
        ]

        _files, _mappings, _operations, summary = service._build_plan(
            task,
            "movie",
            {"label": "电影"},
            videos,
        )

        self.assertGreaterEqual(summary["confidence"], 85)
        self.assertTrue(summary["auto_applicable"])

    def test_ai_title_without_tmdb_hit_stays_below_auto_threshold(self) -> None:
        plan = organizer_v1_plan(
            3,
            category="movie",
            organizer_root="/移动云/_入库暂存/电影/job-3",
            destination_final="/移动云/电影",
        )
        job = organizer_v1_job(plan)
        task = attach_organizer_v1_task(
            {
                "id": 3,
                "title": "Dear You (2026)",
                "source_keyword": "Dear You (2026)",
                "raw_data": {},
            },
            plan,
        )

        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {"auto_apply_confidence": 85}
        service.openlist = SimpleNamespace(exists=lambda _path: False)
        service.tmdb = SimpleNamespace(configured=True, search=lambda *_a, **_k: [])
        service.ai = SimpleNamespace(
            configured=True,
            calibrate=lambda _evidence: {
                "media_type": "movie",
                "title": "给阿嬷的情书",
                "year": "2026",
                "confidence": 0.7,
                "requires_review": False,
            },
            model="test-model",
        )

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
                name="Dear You (2026) - 1080p H264.mkv",
                path="/移动云/_入库暂存/电影/job-3/Dear You (2026) - 1080p H264.mkv",
                is_dir=False,
                size=1000,
            )
        ]

        _files, _mappings, _operations, summary = service._build_plan(
            task,
            "movie",
            {"label": "电影"},
            videos,
        )

        # 没有 TMDB 确认时，仅凭 AI 自报置信度不得越过自动执行线。
        self.assertLess(summary["confidence"], 85)
        self.assertFalse(summary["auto_applicable"])


if __name__ == "__main__":
    unittest.main()
