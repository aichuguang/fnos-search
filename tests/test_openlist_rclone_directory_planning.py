from __future__ import annotations

import unittest
from types import SimpleNamespace

from fnos_media_import.app import _rclone_organizer_target_plan
from fnos_media_import.organizer.parser import standard_target_path
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.services.update_service import _resource_root_from_openlist_root


class RcloneOrganizerDirectoryPlanningTests(unittest.TestCase):
    @staticmethod
    def _target_category(category: dict, target_plan: dict) -> dict:
        return OrganizerService._target_category_for_task(
            {"raw_data": dict(target_plan)},
            category,
        )

    def test_regular_upload_targets_category_root_instead_of_nested_scan_root(self) -> None:
        category = {"label": "电视剧", "openlist_root_path": "/移动云/电视剧"}
        scan_root = "/移动云/电视剧/影视名 4K/影视名（2026）"

        target_plan = _rclone_organizer_target_plan(category["openlist_root_path"])
        target_category = self._target_category(category, target_plan)
        target_path = standard_target_path(
            category_key="tv",
            category=target_category,
            title="影视名",
            year="2026",
            season=1,
            episode=1,
            ext=".mp4",
        )

        self.assertEqual(target_plan["target_root_path"], "/移动云/电视剧")
        self.assertFalse(target_plan["target_root_is_resource"])
        self.assertEqual(
            target_path,
            "/移动云/电视剧/影视名 (2026)/Season 01/影视名 (2026) - S01E01.mp4",
        )
        self.assertNotIn(scan_root, target_path)
        self.assertNotIn("/影视名 4K/", target_path)

    def test_nested_title_and_version_directories_are_cleaned_after_move(self) -> None:
        category = {"label": "电视剧", "openlist_root_path": "/移动云/电视剧"}
        target_plan = _rclone_organizer_target_plan(category["openlist_root_path"])
        target_category = self._target_category(category, target_plan)
        source_path = "/移动云/电视剧/影视名 4K/影视名（2026）/4K/Season 01/原剧集.mp4"
        target_path = standard_target_path(
            category_key="tv",
            category=target_category,
            title="影视名",
            year="2026",
            season=1,
            episode=1,
            ext=".mp4",
        )
        service = OrganizerService.__new__(OrganizerService)

        operations = service._operations_for_mappings(
            [
                {
                    "source_path": source_path,
                    "target_path": target_path,
                    "status": "ready",
                    "reason": [],
                }
            ],
            target_category,
        )

        moves = [item for item in operations if item["type"] == "move_file"]
        cleanup_paths = [item["target_path"] for item in operations if item["type"] == "cleanup_empty_dir"]
        self.assertEqual(moves[0]["target_path"], target_path)
        self.assertEqual(
            cleanup_paths,
            [
                "/移动云/电视剧/影视名 4K/影视名（2026）/4K/Season 01",
                "/移动云/电视剧/影视名 4K/影视名（2026）/4K",
                "/移动云/电视剧/影视名 4K/影视名（2026）",
                "/移动云/电视剧/影视名 4K",
            ],
        )
        self.assertNotIn("/移动云/电视剧", cleanup_paths)

    def test_update_upload_reuses_existing_resource_root_without_duplicate_title_dir(self) -> None:
        category = {"label": "动漫", "openlist_root_path": "/移动云/动漫"}
        target_plan = _rclone_organizer_target_plan(
            category["openlist_root_path"],
            "/清云/动漫/仙逆 (2023)/Season 01",
        )
        target_category = self._target_category(category, target_plan)
        target_path = standard_target_path(
            category_key="tv",
            category=target_category,
            title="仙逆",
            year="2023",
            season=1,
            episode=152,
            ext=".mkv",
        )

        self.assertEqual(target_plan["target_root_path"], "/移动云/动漫/仙逆 (2023)")
        self.assertEqual(target_plan["canonical_resource_root"], "/移动云/动漫/仙逆 (2023)")
        self.assertTrue(target_plan["target_root_is_resource"])
        self.assertEqual(
            target_path,
            "/移动云/动漫/仙逆 (2023)/Season 01/仙逆 (2023) - S01E152.mkv",
        )
        self.assertNotIn("/仙逆 (2023)/仙逆 (2023)/", target_path)

    def test_legacy_duplicate_update_root_is_collapsed_before_next_import(self) -> None:
        self.assertEqual(
            _resource_root_from_openlist_root(
                "/移动云/动漫/完美世界/完美世界（2021）/Season 01"
            ),
            "/移动云/动漫/完美世界（2021）",
        )

    def test_update_wrapper_directories_are_cleaned_using_original_category_root(self) -> None:
        category = {"label": "动漫", "openlist_root_path": "/移动云/动漫"}
        target_plan = _rclone_organizer_target_plan(
            category["openlist_root_path"],
            "/移动云/动漫/仙逆 (2023)",
        )
        target_category = self._target_category(category, target_plan)
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {"anime": category}

        operations = service._operations_for_mappings(
            [
                {
                    "source_path": "/移动云/动漫/仙逆 4K/仙逆（2023）/Season 01/原剧集.mkv",
                    "target_path": "/移动云/动漫/仙逆 (2023)/Season 01/仙逆 (2023) - S01E152.mkv",
                    "status": "ready",
                    "reason": [],
                }
            ],
            target_category,
        )

        cleanup_paths = [item["target_path"] for item in operations if item["type"] == "cleanup_empty_dir"]
        self.assertIn("/移动云/动漫/仙逆 4K/仙逆（2023）", cleanup_paths)
        self.assertIn("/移动云/动漫/仙逆 4K", cleanup_paths)
        self.assertNotIn("/移动云/动漫", cleanup_paths)

    def test_cross_category_target_creates_destination_directory_chain(self) -> None:
        movie = {"label": "电影", "openlist_root_path": "/移动云/电影"}
        tv = {"label": "电视剧", "openlist_root_path": "/移动云/电视剧"}
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {"movie": movie, "tv": tv}

        operations = service._operations_for_mappings(
            [
                {
                    "source_path": "/移动云/电影/误分类资源/原剧集.mp4",
                    "target_path": "/移动云/电视剧/正确剧名 (2026)/Season 01/正确剧名 (2026) - S01E01.mp4",
                    "status": "ready",
                    "reason": [],
                }
            ],
            movie,
        )

        create_paths = [item["target_path"] for item in operations if item["type"] == "create_dir"]
        self.assertEqual(
            create_paths,
            [
                "/移动云/电视剧/正确剧名 (2026)",
                "/移动云/电视剧/正确剧名 (2026)/Season 01",
            ],
        )

    def test_old_task_paths_still_create_and_cleanup_after_mount_name_changes(self) -> None:
        movie = {"label": "电影", "mobile_target_path": "/移动云盘A/电影"}
        tv = {"label": "电视剧", "mobile_target_path": "/移动云盘A/电视剧"}
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {"movie": movie, "tv": tv}
        task = {
            "category": "movie",
            "openlist_root_path": "/移动云/电影/旧包装目录",
            "raw_data": {},
        }
        target_category = service._target_category_for_task(task, movie)

        operations = service._operations_for_mappings(
            [
                {
                    "source_path": "/移动云/电影/旧包装目录/720P/原剧集.mp4",
                    "target_path": "/移动云/电视剧/正确剧名 (2026)/Season 01/正确剧名 (2026) - S01E01.mp4",
                    "status": "ready",
                    "reason": [],
                }
            ],
            target_category,
        )

        self.assertEqual(target_category["source_category_root_path"], "/移动云/电影")
        self.assertEqual(
            [item["target_path"] for item in operations if item["type"] == "create_dir"],
            [
                "/移动云/电视剧/正确剧名 (2026)",
                "/移动云/电视剧/正确剧名 (2026)/Season 01",
            ],
        )
        self.assertIn(
            "/移动云/电影/旧包装目录",
            [item["target_path"] for item in operations if item["type"] == "cleanup_empty_dir"],
        )

    def test_move_retry_uses_already_renamed_source_file(self) -> None:
        class FakeOpenList:
            def __init__(self) -> None:
                self.paths = {"/源目录/标准名.mkv", "/目标目录"}
                self.rename_calls = 0
                self.move_calls: list[tuple[str, str]] = []

            def exists(self, path: str) -> bool:
                return path in self.paths

            def list_dir(self, _path: str, *, refresh: bool = False) -> list:
                return []

            def rename(self, *_args, **_kwargs) -> bool:
                self.rename_calls += 1
                return True

            def move(self, source: str, target_dir: str, **_kwargs) -> bool:
                self.move_calls.append((source, target_dir))
                self.paths.discard(source)
                self.paths.add(f"{target_dir}/标准名.mkv")
                return True

        service = OrganizerService.__new__(OrganizerService)
        service.openlist = FakeOpenList()
        service.organizer_config = {"operation_visibility_attempts": 1, "operation_visibility_delay_seconds": 0}

        inverse = service._execute_operation(
            {
                "type": "move_file",
                "source_path": "/源目录/原名.mkv",
                "target_path": "/目标目录/标准名.mkv",
            }
        )

        self.assertEqual(service.openlist.rename_calls, 0)
        self.assertEqual(service.openlist.move_calls, [("/源目录/标准名.mkv", "/目标目录")])
        self.assertEqual(
            inverse,
            {
                "type": "move_file",
                "source_path": "/目标目录/标准名.mkv",
                "target_path": "/源目录/原名.mkv",
            },
        )

    def test_move_retry_never_uses_a_different_same_name_file_while_original_still_exists(self) -> None:
        class FakeOpenList:
            def __init__(self) -> None:
                self.paths = {"/源目录/原名.mkv", "/源目录/标准名.mkv", "/目标目录"}
                self.rename_calls = 0
                self.move_calls = 0

            def exists(self, path: str) -> bool:
                return path in self.paths

            def list_dir(self, _path: str, *, refresh: bool = False) -> list:
                return []

            def rename(self, *_args, **_kwargs) -> bool:
                self.rename_calls += 1
                return True

            def move(self, *_args, **_kwargs) -> bool:
                self.move_calls += 1
                return True

        service = OrganizerService.__new__(OrganizerService)
        service.openlist = FakeOpenList()
        service.organizer_config = {"operation_visibility_attempts": 1, "operation_visibility_delay_seconds": 0}

        with self.assertRaisesRegex(RuntimeError, "拒绝把其它文件当作改名重试结果"):
            service._execute_operation(
                {
                    "type": "move_file",
                    "source_path": "/源目录/原名.mkv",
                    "target_path": "/目标目录/标准名.mkv",
                }
            )

        self.assertEqual(service.openlist.rename_calls, 0)
        self.assertEqual(service.openlist.move_calls, 0)

    def test_move_does_not_rename_source_before_destination_directory_is_visible(self) -> None:
        class FakeOpenList:
            def __init__(self) -> None:
                self.rename_calls = 0

            def exists(self, path: str) -> bool:
                return path == "/源目录/原名.mkv"

            def list_dir(self, _path: str, *, refresh: bool = False) -> list:
                return []

            def rename(self, *_args, **_kwargs) -> bool:
                self.rename_calls += 1
                return True

        service = OrganizerService.__new__(OrganizerService)
        service.openlist = FakeOpenList()
        service.organizer_config = {"operation_visibility_attempts": 1, "operation_visibility_delay_seconds": 0}

        with self.assertRaisesRegex(RuntimeError, "目标目录不存在"):
            service._execute_operation(
                {
                    "type": "move_file",
                    "source_path": "/源目录/原名.mkv",
                    "target_path": "/目标目录/标准名.mkv",
                }
            )

        self.assertEqual(service.openlist.rename_calls, 0)

    def test_existing_standard_target_removes_duplicate_update_source(self) -> None:
        category = {"label": "动漫", "openlist_root_path": "/移动云/动漫"}
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {"anime": category}

        operations = service._operations_for_mappings(
            [
                {
                    "source_path": "/移动云/动漫/仙逆 4K/重复剧集.mkv",
                    "target_path": "/移动云/动漫/仙逆 (2023)/Season 01/仙逆 (2023) - S01E152.mkv",
                    "status": "skipped_existing",
                    "reason": ["目标路径已存在"],
                }
            ],
            category,
        )

        self.assertEqual(operations[0]["type"], "delete_duplicate_file")
        self.assertIn(
            "/移动云/动漫/仙逆 4K",
            [item["target_path"] for item in operations if item["type"] == "cleanup_empty_dir"],
        )

    def test_duplicate_source_is_not_deleted_when_standard_target_is_missing(self) -> None:
        class FakeOpenList:
            def __init__(self) -> None:
                self.paths = {"/来源/重复集.mkv"}
                self.remove_calls: list[str] = []

            def exists(self, path: str) -> bool:
                return path in self.paths

            def list_dir(self, _path: str, *, refresh: bool = False) -> list:
                return []

            def remove_file(self, path: str) -> bool:
                self.remove_calls.append(path)
                self.paths.discard(path)
                return True

        service = OrganizerService.__new__(OrganizerService)
        service.openlist = FakeOpenList()
        service.organizer_config = {"operation_visibility_attempts": 1, "operation_visibility_delay_seconds": 0}

        with self.assertRaisesRegex(RuntimeError, "标准目标文件不存在"):
            service._execute_operation(
                {
                    "type": "delete_duplicate_file",
                    "source_path": "/来源/重复集.mkv",
                    "target_path": "/标准/剧集.mkv",
                }
            )

        self.assertEqual(service.openlist.remove_calls, [])
        self.assertIn("/来源/重复集.mkv", service.openlist.paths)

    def test_duplicate_source_is_deleted_only_after_standard_video_is_confirmed(self) -> None:
        class FakeOpenList:
            def __init__(self) -> None:
                self.paths = {"/来源/重复集.mkv", "/标准/剧集.mkv"}
                self.remove_calls: list[str] = []

            def exists(self, path: str) -> bool:
                return path in self.paths

            def list_dir(self, _path: str, *, refresh: bool = False) -> list:
                return []

            def get_item(self, path: str):
                return SimpleNamespace(is_dir=False) if path in self.paths else None

            def remove_file(self, path: str) -> bool:
                self.remove_calls.append(path)
                self.paths.discard(path)
                return True

        service = OrganizerService.__new__(OrganizerService)
        service.openlist = FakeOpenList()
        service.organizer_config = {"operation_visibility_attempts": 1, "operation_visibility_delay_seconds": 0}

        service._execute_operation(
            {
                "type": "delete_duplicate_file",
                "source_path": "/来源/重复集.mkv",
                "target_path": "/标准/剧集.mkv",
            }
        )

        self.assertEqual(service.openlist.remove_calls, ["/来源/重复集.mkv"])
        self.assertNotIn("/来源/重复集.mkv", service.openlist.paths)
        self.assertIn("/标准/剧集.mkv", service.openlist.paths)

    def test_update_move_race_cleans_source_when_another_task_created_the_target(self) -> None:
        class FakeOpenList:
            def __init__(self) -> None:
                self.paths = {"/资源根/重复集.mkv", "/资源根/Season 01/标准集.mkv"}
                self.remove_calls: list[str] = []

            def exists(self, path: str) -> bool:
                return path in self.paths

            def list_dir(self, _path: str, *, refresh: bool = False) -> list:
                return []

            def get_item(self, path: str):
                return SimpleNamespace(is_dir=False) if path in self.paths else None

            def remove_file(self, path: str) -> bool:
                self.remove_calls.append(path)
                self.paths.discard(path)
                return True

        service = OrganizerService.__new__(OrganizerService)
        service.openlist = FakeOpenList()
        service.organizer_config = {"operation_visibility_attempts": 1, "operation_visibility_delay_seconds": 0}

        service._execute_operation(
            {
                "type": "move_file",
                "source_path": "/资源根/重复集.mkv",
                "target_path": "/资源根/Season 01/标准集.mkv",
                "raw_data": {"delete_source_if_target_exists": True},
            }
        )

        self.assertEqual(service.openlist.remove_calls, ["/资源根/重复集.mkv"])
        self.assertNotIn("/资源根/重复集.mkv", service.openlist.paths)

    def test_duplicate_source_is_not_deleted_when_target_path_is_a_directory(self) -> None:
        class FakeOpenList:
            def __init__(self) -> None:
                self.paths = {"/来源/重复集.mkv", "/标准/剧集.mkv"}
                self.remove_calls: list[str] = []

            def exists(self, path: str) -> bool:
                return path in self.paths

            def list_dir(self, _path: str, *, refresh: bool = False) -> list:
                return []

            def get_item(self, path: str):
                return SimpleNamespace(is_dir=path == "/标准/剧集.mkv") if path in self.paths else None

            def remove_file(self, path: str) -> bool:
                self.remove_calls.append(path)
                return True

        service = OrganizerService.__new__(OrganizerService)
        service.openlist = FakeOpenList()
        service.organizer_config = {"operation_visibility_attempts": 1, "operation_visibility_delay_seconds": 0}

        with self.assertRaisesRegex(RuntimeError, "标准目标是目录"):
            service._execute_operation(
                {
                    "type": "delete_duplicate_file",
                    "source_path": "/来源/重复集.mkv",
                    "target_path": "/标准/剧集.mkv",
                }
            )

        self.assertEqual(service.openlist.remove_calls, [])

    def test_category_path_matching_requires_a_directory_boundary(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {
            "movie": {"openlist_root_path": "/mount/movie"},
            "backup": {"openlist_root_path": "/mount/movie-backup"},
        }

        category = service._category_from_label_or_path("", "/mount/movie-backup/title/file.mkv")

        self.assertEqual(category, "backup")

    def test_category_path_matching_prefers_the_most_specific_root(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {
            "media": {"openlist_root_path": "/mount/media"},
            "tv": {"mobile_openlist_root_path": "/mount/media/tv"},
        }

        category = service._category_from_label_or_path("", "/mount/media/tv/title/file.mkv")

        self.assertEqual(category, "tv")

    def test_category_path_matching_survives_an_old_mount_name(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {
            "movie": {"label": "电影", "mobile_target_path": "/移动云盘A/电影"},
            "tv": {"label": "电视剧", "mobile_target_path": "/移动云盘A/电视剧"},
        }

        category = service._category_from_label_or_path("", "/移动云/电视剧/剧名/Season 01/剧集.mkv")

        self.assertEqual(category, "tv")

    def test_configured_category_root_wins_over_a_title_named_like_another_category(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {
            "movie": {"label": "电影", "openlist_root_path": "/mount/movie"},
            "tv": {"label": "电视剧", "openlist_root_path": "/mount/tv"},
        }

        root = service._configured_category_root_for_path("/mount/movie/电视剧")

        self.assertEqual(root, "/mount/movie")

    def test_update_job_can_create_a_new_task_on_an_existing_resource_root(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.created: list[dict] = []

            def list_organizer_tasks_by_job(self, _job_id: int, limit: int = 1) -> list:
                return []

            def find_recent_organizer_task(self, _root: str, _category: str):
                return {"id": 7, "job_id": 70, "status": "auto_approved"}

            def list_guest_requests_by_job(self, _job_id: int) -> list:
                return []

            def create_organizer_task(self, **values) -> int:
                self.created.append(values)
                return 8

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {"enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service.categories = {"anime": {"label": "动漫", "openlist_root_path": "/移动云/动漫"}}
        service._mark_linked_job_waiting = lambda _task_id: None
        service._schedule_initial_openlist_visibility_wait = lambda _task_id: False
        service._schedule = lambda _task_id: None

        result = service.enqueue_from_completed_directory(
            job={"id": 80, "category": "anime", "title": "仙逆"},
            root_path="/移动云/动漫/仙逆 (2023)",
            payload={
                "update_context": {
                    "subscription_id": 5,
                    "canonical_resource_root": "/移动云/动漫/仙逆 (2023)",
                }
            },
        )

        self.assertEqual(result["task_id"], 8)
        self.assertEqual(len(service.db.created), 1)
        self.assertEqual(service.db.created[0]["job_id"], 80)

    def test_duplicate_callback_for_the_same_job_reuses_its_existing_task(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.created = 0

            def list_organizer_tasks_by_job(self, _job_id: int, limit: int = 1) -> list:
                return [{"id": 9, "status": "waiting_openlist"}]

            def create_organizer_task(self, **_values) -> int:
                self.created += 1
                return 10

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {"enabled": True}
        service.openlist = SimpleNamespace(configured=True)
        service.categories = {"anime": {"label": "动漫", "openlist_root_path": "/移动云/动漫"}}

        result = service.enqueue_from_completed_directory(
            job={"id": 80, "category": "anime", "title": "仙逆"},
            root_path="/移动云/动漫/仙逆 (2023)",
            payload={"allow_same_root_task": True},
        )

        self.assertEqual(result["task_id"], 9)
        self.assertEqual(service.db.created, 0)

    def test_one_failed_organizer_operation_does_not_stop_later_files(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.task = {
                    "id": 1,
                    "category": "anime",
                    "openlist_root_path": "/移动云/动漫/仙逆 (2023)",
                    "mappings": [],
                    "operations": [],
                }
                self.operation_updates: list[tuple[int, dict]] = []
                self.run_updates: list[tuple[int, str, dict]] = []
                self.task_updates: list[tuple[int, dict]] = []

            def get_organizer_task(self, _task_id: int, include_children: bool = True):
                return self.task

            def replace_organizer_operations(self, _task_id: int, operations: list[dict]) -> None:
                self.task["operations"] = [{"id": index + 1, **item} for index, item in enumerate(operations)]

            def create_organizer_run(self, _task_id: int) -> int:
                return 11

            def update_organizer_task(self, task_id: int, **values) -> None:
                self.task_updates.append((task_id, values))

            def update_organizer_operation(self, operation_id: int, **values) -> None:
                self.operation_updates.append((operation_id, values))

            def update_organizer_run(self, run_id: int, status: str, **values) -> None:
                self.run_updates.append((run_id, status, values))

            def release_organizer_locks(self, **_values) -> None:
                return None

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.categories = {"anime": {"openlist_root_path": "/移动云/动漫"}}
        service._operations_for_mappings = lambda *_args, **_kwargs: [
            {"type": "move_file", "source_path": "/来源/E151.mkv", "target_path": "/标准/E151.mkv", "status": "pending"},
            {"type": "move_file", "source_path": "/来源/E152.mkv", "target_path": "/标准2/E152.mkv", "status": "pending"},
        ]
        service._acquire_organizer_locks = lambda *_args, **_kwargs: []
        service._sync_linked_job = lambda *_args, **_kwargs: None
        executed: list[str] = []

        def execute(op: dict):
            executed.append(op["source_path"])
            if op["source_path"].endswith("E151.mkv"):
                raise RuntimeError("E151 move failed")
            return {"type": "move_file", "source_path": op["target_path"], "target_path": op["source_path"]}

        service._execute_operation = execute

        result = service.apply_task(1)

        self.assertFalse(result["success"])
        self.assertEqual(executed, ["/来源/E151.mkv", "/来源/E152.mkv"])
        statuses = {operation_id: values["status"] for operation_id, values in service.db.operation_updates}
        self.assertEqual(statuses, {1: "failed", 2: "done"})
        self.assertEqual(service.db.run_updates[-1][1], "failed")
        self.assertEqual(service.db.run_updates[-1][2]["summary"]["done"], 1)
        self.assertEqual(service.db.run_updates[-1][2]["summary"]["failed"], 1)

    def test_failed_final_confirmation_does_not_mark_the_run_done(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.task = {
                    "id": 1,
                    "category": "anime",
                    "openlist_root_path": "/移动云/动漫/仙逆 (2023)",
                    "mappings": [],
                    "operations": [],
                    "evidence": {},
                }
                self.run_updates: list[tuple[int, str, dict]] = []

            def get_organizer_task(self, _task_id: int, include_children: bool = True):
                return self.task

            def replace_organizer_operations(self, _task_id: int, operations: list[dict]) -> None:
                self.task["operations"] = operations

            def create_organizer_run(self, _task_id: int) -> int:
                return 12

            def update_organizer_task(self, _task_id: int, **_values) -> None:
                return None

            def update_organizer_run(self, run_id: int, status: str, **values) -> None:
                self.run_updates.append((run_id, status, values))

            def release_organizer_locks(self, **_values) -> None:
                return None

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.categories = {"anime": {"openlist_root_path": "/移动云/动漫"}}
        service._operations_for_mappings = lambda *_args, **_kwargs: []
        service._acquire_organizer_locks = lambda *_args, **_kwargs: []
        service._sync_linked_job = lambda *_args, **_kwargs: None
        service._cleanup_source_empty_dirs_after_apply = lambda _task: {}
        service._refresh_openlist_strm_for_task = lambda _task_id, _task: {}
        service._confirm_standardized_targets = lambda _task: {"success": False, "message": "目标文件不可见"}

        result = service.apply_task(1)

        self.assertFalse(result["success"])
        self.assertEqual(service.db.run_updates[-1][1], "failed")
        self.assertFalse(any(status == "done" for _run_id, status, _values in service.db.run_updates))


if __name__ == "__main__":
    unittest.main()
