from __future__ import annotations

import unittest
from types import SimpleNamespace

from fnos_media_import.constants import JOB_REVIEW, ROUTE_QUARK_TO_MOBILE
from fnos_media_import.organizer.openlist_client import OpenListError, OpenListItem, OpenListTransientError
from fnos_media_import.organizer.service import OrganizerService, SkipOperation
from tests.test_rclone_persisted_staging_plan import _persisted_plan


JOB_ROOT = "/旧挂载/_入库暂存/电视剧/job-42"
FINAL_ROOT = "/旧挂载/电视剧"
RESOURCE_ROOT = f"{FINAL_ROOT}/藏海传 (2025)"
SEASON_ROOT = f"{RESOURCE_ROOT}/Season 01"


def _task(**raw_overrides: object) -> dict:
    raw_data = {
        "staging_plan": _persisted_plan(42),
        "target_root_path": FINAL_ROOT,
        **raw_overrides,
    }
    return {
        "id": 8,
        "job_id": 42,
        "category": "tv",
        "openlist_root_path": JOB_ROOT,
        "raw_data": raw_data,
    }


def _service(*, existing_paths: set[str] | None = None) -> OrganizerService:
    service = OrganizerService.__new__(OrganizerService)
    service.db = None
    service.organizer_config = {"max_scan_depth": 8, "max_files_per_task": 500}
    service.categories = {"tv": {"label": "电视剧", "openlist_root_path": FINAL_ROOT}}
    paths = existing_paths or set()
    service.openlist = SimpleNamespace(exists=lambda path: path in paths)
    return service


def _video_mapping() -> dict:
    return {
        "source_path": f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.mkv",
        "source_name": "ZangHaiZhuan.S01E01.mkv",
        "target_path": f"{SEASON_ROOT}/藏海传 (2025) - S01E01.mkv",
        "target_name": "藏海传 (2025) - S01E01.mkv",
        "status": "ready",
        "season": 1,
        "episode": 1,
        "raw_data": {"staging_file": True},
    }


class _ApplyBoundaryDatabase:
    def __init__(self, mapping: dict) -> None:
        plan = _persisted_plan(42)
        self.task = {
            **_task(),
            "status": "manual_confirmed",
            "mappings": [{"id": 91, **mapping}],
            "operations": [],
            "evidence": {},
        }
        self.job = {
            "id": 42,
            "category": "tv",
            "target_route": ROUTE_QUARK_TO_MOBILE,
            "status": "waiting_organizer",
            "target_path": plan["storage_job_root"],
            "raw_data": {"staging_plan": plan},
        }
        self.run_updates: list[tuple[int, str, dict]] = []
        self.events: list[tuple] = []

    def get_organizer_task(self, _task_id: int, include_children: bool = True) -> dict:
        return self.task

    def get_job(self, _job_id: int) -> dict:
        return self.job

    def replace_organizer_operations(self, _task_id: int, operations: list[dict]) -> None:
        self.task["operations"] = [
            {"id": index, **operation}
            for index, operation in enumerate(operations, start=1)
        ]

    def update_organizer_task(self, _task_id: int, **values) -> None:
        self.task.update(values)

    def update_organizer_run(self, run_id: int, status: str, **values) -> None:
        self.run_updates.append((run_id, status, values))

    def update_job(self, _job_id: int, **values) -> None:
        self.job.update(values)

    def add_event(self, *args) -> None:
        self.events.append(args)

    @staticmethod
    def release_organizer_locks(**_values) -> None:
        return None


class StagingCompanionMappingTests(unittest.TestCase):
    def test_direct_job_root_subtitle_follows_video_into_season_directory(self) -> None:
        service = _service()
        video = {
            **_video_mapping(),
            "source_path": f"{JOB_ROOT}/ZangHaiZhuan.S01E01.mkv",
        }
        subtitle = SimpleNamespace(
            name="ZangHaiZhuan.S01E01.srt",
            path=f"{JOB_ROOT}/ZangHaiZhuan.S01E01.srt",
            size=1024,
            raw={},
        )

        _files, mappings = service._build_companion_mappings(
            _task(),
            [subtitle],
            [video],
            target_index={video["target_path"]: 1},
            title="藏海传",
            year="2025",
            media_type="tv",
            tmdb_id=123,
            confidence=95,
        )

        self.assertEqual(
            mappings[0]["target_path"],
            f"{SEASON_ROOT}/藏海传 (2025) - S01E01.srt",
        )

    def test_only_strict_sidecars_are_mapped_and_language_suffix_is_preserved(self) -> None:
        service = _service()
        companions = [
            SimpleNamespace(
                name="ZangHaiZhuan.S01E01.zh-CN.forced.srt",
                path=f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.zh-CN.forced.srt",
                size=1024,
                raw={},
            ),
            SimpleNamespace(
                name="ZangHaiZhuan.S01E01.nfo",
                path=f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.nfo",
                size=2048,
                raw={},
            ),
            SimpleNamespace(
                name="sample.srt",
                path=f"{JOB_ROOT}/Season 01/sample.srt",
                size=512,
                raw={},
            ),
            SimpleNamespace(
                name="readme.txt",
                path=f"{JOB_ROOT}/Season 01/readme.txt",
                size=512,
                raw={},
            ),
            SimpleNamespace(
                name="扫码关注.jpg",
                path=f"{JOB_ROOT}/Season 01/扫码关注.jpg",
                size=4096,
                raw={},
            ),
            SimpleNamespace(
                name="random.nfo",
                path=f"{JOB_ROOT}/Season 01/random.nfo",
                size=1024,
                raw={},
            ),
        ]

        files, mappings = service._build_companion_mappings(
            _task(),
            companions,
            [_video_mapping()],
            target_index={_video_mapping()["target_path"]: 1},
            title="藏海传",
            year="2025",
            media_type="tv",
            tmdb_id=123,
            confidence=95,
        )

        self.assertEqual(
            [item["source_name"] for item in mappings],
            ["ZangHaiZhuan.S01E01.zh-CN.forced.srt", "ZangHaiZhuan.S01E01.nfo"],
        )
        self.assertEqual(
            [item["target_path"] for item in mappings],
            [
                f"{SEASON_ROOT}/藏海传 (2025) - S01E01.zh-CN.forced.srt",
                f"{SEASON_ROOT}/藏海传 (2025) - S01E01.nfo",
            ],
        )
        self.assertEqual(len(files), 2)

    def test_known_resource_metadata_and_artwork_move_to_resource_root(self) -> None:
        service = _service()
        companions = [
            SimpleNamespace(name="tvshow.nfo", path=f"{JOB_ROOT}/tvshow.nfo", size=2048, raw={}),
            SimpleNamespace(name="poster.jpg", path=f"{JOB_ROOT}/poster.jpg", size=4096, raw={}),
            SimpleNamespace(name="fanart.webp", path=f"{JOB_ROOT}/fanart.webp", size=4096, raw={}),
            SimpleNamespace(name="promotion.jpg", path=f"{JOB_ROOT}/promotion.jpg", size=4096, raw={}),
        ]

        _files, mappings = service._build_companion_mappings(
            _task(),
            companions,
            [_video_mapping()],
            target_index={_video_mapping()["target_path"]: 1},
            title="藏海传",
            year="2025",
            media_type="tv",
            tmdb_id=123,
            confidence=95,
        )

        self.assertEqual(
            [(item["source_name"], item["target_path"]) for item in mappings],
            [
                ("tvshow.nfo", f"{RESOURCE_ROOT}/tvshow.nfo"),
                ("poster.jpg", f"{RESOURCE_ROOT}/poster.jpg"),
                ("fanart.webp", f"{RESOURCE_ROOT}/fanart.webp"),
            ],
        )

    def test_companion_moves_precede_empty_directory_cleanup(self) -> None:
        service = _service()
        category = {
            "label": "电视剧",
            "openlist_root_path": FINAL_ROOT,
            "source_category_root_path": JOB_ROOT,
        }
        video = _video_mapping()
        subtitle = {
            "source_path": f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.srt",
            "target_path": f"{SEASON_ROOT}/藏海传 (2025) - S01E01.srt",
            "status": "ready",
            "reason": [],
            "raw_data": {"companion_file": True},
        }

        operations = service._operations_for_mappings([video, subtitle], category)
        move_indexes = [index for index, item in enumerate(operations) if item["type"] == "move_file"]
        cleanup_indexes = [index for index, item in enumerate(operations) if item["type"] == "cleanup_empty_dir"]

        self.assertTrue(move_indexes)
        self.assertTrue(cleanup_indexes)
        self.assertLess(max(move_indexes), min(cleanup_indexes))
        subtitle_move = next(item for item in operations if item.get("source_path") == subtitle["source_path"])
        self.assertTrue(subtitle_move["raw_data"]["fail_if_target_exists"])
        self.assertFalse(subtitle_move["raw_data"]["delete_source_if_target_exists"])

    def test_target_appearing_after_planning_never_silently_skips_companion(self) -> None:
        source = f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.srt"
        target = f"{SEASON_ROOT}/藏海传 (2025) - S01E01.srt"
        removed: list[str] = []
        service = _service(existing_paths={source, target})
        service.openlist.remove_file = lambda path: removed.append(path)

        with self.assertRaisesRegex(RuntimeError, "附件目标已存在"):
            service._execute_operation(
                {
                    "type": "move_file",
                    "source_path": source,
                    "target_path": target,
                    "raw_data": {
                        "fail_if_target_exists": True,
                        "delete_source_if_target_exists": False,
                    },
                }
            )

        self.assertEqual(removed, [])

    def test_apply_retry_accepts_companion_already_moved_when_source_is_gone(self) -> None:
        source = f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.srt"
        target = f"{SEASON_ROOT}/藏海传 (2025) - S01E01.srt"
        service = _service(existing_paths={target})

        with self.assertRaisesRegex(SkipOperation, "上次移动已完成"):
            service._execute_operation(
                {
                    "type": "move_file",
                    "source_path": source,
                    "target_path": target,
                    "raw_data": {
                        "fail_if_target_exists": True,
                        "delete_source_if_target_exists": False,
                    },
                }
            )



class StagingMoveIdempotencyTests(unittest.TestCase):
    def test_video_target_and_source_existing_together_is_a_conflict(self) -> None:
        source = f"{JOB_ROOT}/E01.mkv"
        target = f"{SEASON_ROOT}/E01.mkv"
        service = _service(existing_paths={source, target})

        with self.assertRaisesRegex(RuntimeError, "同时存在"):
            service._execute_operation(
                {
                    "type": "move_file",
                    "source_path": source,
                    "target_path": target,
                    "raw_data": {"staging_file": True},
                }
            )

    def test_existing_video_target_with_absent_source_is_a_completed_retry(self) -> None:
        source = f"{JOB_ROOT}/E01.mkv"
        target = f"{SEASON_ROOT}/E01.mkv"
        service = _service(existing_paths={target})

        with self.assertRaisesRegex(SkipOperation, "此前移动已完成"):
            service._execute_operation(
                {
                    "type": "move_file",
                    "source_path": source,
                    "target_path": target,
                    "raw_data": {"staging_file": True},
                }
            )

    def test_successful_staging_move_must_confirm_source_disappeared(self) -> None:
        source = f"{JOB_ROOT}/E01.mkv"
        target = f"{SEASON_ROOT}/E01.mkv"
        service = _service(existing_paths={source, SEASON_ROOT})
        service.openlist.move = lambda *_args, **_kwargs: True
        service._wait_for_openlist_path = lambda _path, **_kwargs: True
        service._wait_for_openlist_absence = lambda _path, **_kwargs: False

        with self.assertRaisesRegex(RuntimeError, "源文件仍可见"):
            service._execute_operation(
                {
                    "type": "move_file",
                    "source_path": source,
                    "target_path": target,
                    "raw_data": {"staging_file": True},
                }
            )


class StagingCompanionVisibilityTests(unittest.TestCase):
    def test_exact_manifest_waits_for_supported_sidecars_but_ignores_unknown_files(self) -> None:
        service = _service()
        task = _task(
            transport_target_paths=[
                f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.srt",
                f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.nfo",
                f"{JOB_ROOT}/Season 01/readme.txt",
                f"{FINAL_ROOT}/越界字幕.srt",
            ]
        )
        rows = [
            OpenListItem(
                name="ZangHaiZhuan.S01E01.srt",
                path=f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.srt",
                is_dir=False,
            ),
            OpenListItem(
                name="ZangHaiZhuan.S01E01.nfo",
                path=f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.nfo",
                is_dir=False,
            ),
        ]
        service.openlist.list_dir = lambda _path, refresh=False: rows

        result = service._scan_staging_companion_files(task, JOB_ROOT)

        self.assertEqual([item.name for item in result], [rows[0].name, rows[1].name])

    def test_exact_manifest_retries_when_supported_sidecar_is_not_visible(self) -> None:
        service = _service()
        task = _task(
            transport_target_paths=[
                f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.srt",
                f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.nfo",
            ]
        )
        service.openlist.list_dir = lambda _path, refresh=False: [
            OpenListItem(
                name="ZangHaiZhuan.S01E01.srt",
                path=f"{JOB_ROOT}/Season 01/ZangHaiZhuan.S01E01.srt",
                is_dir=False,
            )
        ]

        with self.assertRaises(OpenListTransientError):
            service._scan_staging_companion_files(task, JOB_ROOT)


class StagingBoundaryTests(unittest.TestCase):
    def _assert_apply_rejects_mapping(self, mapping: dict, expected_message: str) -> None:
        database = _ApplyBoundaryDatabase(mapping)
        service = _service()
        service.db = database
        calls: list[str] = []
        service._operations_for_mappings = lambda *_args, **_kwargs: [
            {
                "type": "move_file",
                "source_path": mapping["source_path"],
                "target_path": mapping["target_path"],
                "status": "pending",
            }
        ]
        service._lock_keys = lambda _task: calls.append("lock") or []
        service._execute_operation = lambda _operation: calls.append("execute")

        result = service.apply_task(8, run_id=71)

        self.assertFalse(result["success"])
        self.assertIn(expected_message, result["message"])
        self.assertEqual(calls, [])
        self.assertEqual(database.task["status"], "failed")
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertEqual(database.run_updates[-1][0:2], (71, "failed"))
        self.assertTrue(all(item["status"] == "pending" for item in database.task["operations"]))

    def test_scan_root_must_stay_within_persisted_job_root(self) -> None:
        service = _service()
        target_category = service._target_category_for_task(
            _task(),
            service.categories["tv"],
        )

        service._validate_staging_task_boundaries(
            _task(),
            scan_root=f"{JOB_ROOT}/资源目录",
            target_category=target_category,
        )
        with self.assertRaises(OpenListError):
            service._validate_staging_task_boundaries(
                _task(),
                scan_root=FINAL_ROOT,
                target_category=target_category,
            )

    def test_target_root_must_stay_within_persisted_final_category(self) -> None:
        service = _service()
        unsafe_task = _task(target_root_path="/其它挂载/电视剧")
        target_category = service._target_category_for_task(
            unsafe_task,
            service.categories["tv"],
        )

        with self.assertRaises(OpenListError):
            service._validate_staging_task_boundaries(
                unsafe_task,
                scan_root=JOB_ROOT,
                target_category=target_category,
            )

    def test_enabled_but_invalid_plan_cannot_fall_back_to_normal_scan(self) -> None:
        service = _service()
        invalid_task = _task(
            staging_plan={**_persisted_plan(42), "openlist_job_root": "/错误目录/job-42"}
        )
        target_category = service._target_category_for_task(
            invalid_task,
            service.categories["tv"],
        )

        with self.assertRaisesRegex(OpenListError, "校验失败"):
            service._validate_staging_task_boundaries(
                invalid_task,
                scan_root=JOB_ROOT,
                target_category=target_category,
            )

    def test_apply_rejects_admin_tampered_target_outside_final_category_root(self) -> None:
        self._assert_apply_rejects_mapping(
            {
                **_video_mapping(),
                "target_path": "/其它挂载/电视剧/藏海传 (2025)/Season 01/藏海传 (2025) - S01E01.mkv",
            },
            "映射目标路径越界",
        )

    def test_apply_rejects_skipped_mapping_source_outside_job_root(self) -> None:
        self._assert_apply_rejects_mapping(
            {
                **_video_mapping(),
                "source_path": f"{FINAL_ROOT}/其它资源/越界文件.mkv",
                "status": "skipped_existing",
            },
            "映射源路径越界",
        )

    def test_apply_requires_file_paths_below_staging_roots_not_the_roots_themselves(self) -> None:
        with self.subTest(path="source_root"):
            self._assert_apply_rejects_mapping(
                {
                    **_video_mapping(),
                    "source_path": JOB_ROOT,
                },
                "映射源路径越界",
            )
        with self.subTest(path="target_root"):
            self._assert_apply_rejects_mapping(
                {
                    **_video_mapping(),
                    "target_path": FINAL_ROOT,
                },
                "映射目标路径越界",
            )


if __name__ == "__main__":
    unittest.main()
