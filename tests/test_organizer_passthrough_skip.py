from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fnos_media_import.constants import ROUTE_QUARK_TO_MOBILE
from fnos_media_import.database import Database
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.services.organizer_admin_service import (
    OrganizerAdminCommandDependencies,
    OrganizerAdminCommandService,
)
from tests.test_rclone_persisted_staging_plan import _persisted_plan


def _movie_plan(job_id: int) -> dict[str, Any]:
    plan = _persisted_plan(job_id)
    result: dict[str, Any] = {}
    for key, value in plan.items():
        result[key] = value.replace("电视剧", "电影") if isinstance(value, str) else value
    result.update(
        {
            "category": "movie",
            "category_label": "电影",
            "route": ROUTE_QUARK_TO_MOBILE,
        }
    )
    return result


class _TreeOpenList:
    def __init__(self, rows: dict[str, list[tuple[str, bool]]]) -> None:
        self.rows = rows

    def list_dir(self, path: str, refresh: bool = False):  # noqa: ARG002
        return [
            SimpleNamespace(
                name=name,
                path=f"{path.rstrip('/')}/{name}",
                is_dir=is_dir,
                size=0,
                modified="",
            )
            for name, is_dir in self.rows.get(path, [])
        ]


class _MemoryOpenList:
    def __init__(self, *, files: set[str], directories: set[str]) -> None:
        self.files = set(files)
        self.directories = set(directories)
        self.refresh_calls: list[str] = []

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.directories

    def list_dir(self, path: str, refresh: bool = False):  # noqa: ARG002
        prefix = f"{path.rstrip('/')}/"
        rows: list[SimpleNamespace] = []
        for directory in sorted(self.directories):
            if directory.startswith(prefix) and "/" not in directory[len(prefix) :]:
                rows.append(
                    SimpleNamespace(
                        name=directory[len(prefix) :],
                        path=directory,
                        is_dir=True,
                        size=0,
                        modified="",
                    )
                )
        for file_path in sorted(self.files):
            if file_path.startswith(prefix) and "/" not in file_path[len(prefix) :]:
                rows.append(
                    SimpleNamespace(
                        name=file_path[len(prefix) :],
                        path=file_path,
                        is_dir=False,
                        size=1,
                        modified="",
                    )
                )
        return rows

    def mkdir(self, path: str) -> bool:
        self.directories.add(path)
        return True

    def rename(self, source_path: str, new_name: str, overwrite: bool = False) -> bool:  # noqa: ARG002
        target = f"{source_path.rsplit('/', 1)[0]}/{new_name}"
        if source_path not in self.files or target in self.files:
            raise RuntimeError("rename conflict")
        self.files.remove(source_path)
        self.files.add(target)
        return True

    def move(self, source_path: str, target_dir: str, **_kwargs: Any) -> bool:
        target = f"{target_dir.rstrip('/')}/{source_path.rsplit('/', 1)[-1]}"
        if source_path not in self.files or target in self.files:
            raise RuntimeError("move conflict")
        self.files.remove(source_path)
        self.files.add(target)
        return True

    def remove_empty_directory(self, path: str) -> bool:
        if self.list_dir(path):
            raise RuntimeError("directory not empty")
        self.directories.discard(path)
        return True

    def refresh_strm(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        self.refresh_calls.append(path)
        raise AssertionError("直通入库不应触发 OpenList 全局扫描")


class OrganizerPassthroughSkipTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"organizer-passthrough-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def _create_staging_task(self, rows: dict[str, list[tuple[str, bool]]]) -> tuple[OrganizerService, int]:
        plan = _movie_plan(1)
        job_id, _created = self.database.create_job(
            {
                "title": "电影资源",
                "category": "movie",
                "category_label": "电影",
                "source_type": "quark",
                "source_url": "https://pan.quark.cn/s/passthrough",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": plan["storage_job_root"],
                "status": "review",
                "raw_data": {"staging_plan": plan},
            }
        )
        self.assertEqual(job_id, 1)
        task_id = self.database.create_organizer_task(
            category="movie",
            category_label="电影",
            title="电影资源",
            openlist_root_path=plan["openlist_job_root"],
            job_id=job_id,
            status="waiting_review",
            raw_data={"staging_plan": plan},
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database
        service.categories = {
            "movie": {
                "label": "电影",
                "openlist_root_path": plan["openlist_final_category_root"],
            }
        }
        service.organizer_config = {"max_files_per_task": 500, "skip_passthrough_max_files": 5000}
        service.openlist = _TreeOpenList(rows)
        return service, task_id

    def test_collection_directories_keep_names_and_relative_paths(self) -> None:
        root = "/旧挂载/_入库暂存/电影/job-1"
        service, task_id = self._create_staging_task(
            {
                root: [("警察故事", True), ("A计划", True), ("合集说明.txt", False)],
                f"{root}/警察故事": [("Police Story.mkv", False), ("poster.jpg", False)],
                f"{root}/A计划": [("Project A.mp4", False), ("movie.nfo", False)],
            }
        )

        result = service.skip_task(task_id)

        self.assertTrue(result["success"], result)
        self.assertTrue(result["ready_for_apply"])
        self.assertTrue(result["passthrough"])
        self.assertEqual(result["file_count"], 5)
        task = self.database.get_organizer_task(task_id)
        self.assertEqual(task["status"], "manual_confirmed")
        targets = {item["target_path"] for item in task["mappings"]}
        self.assertEqual(
            targets,
            {
                "/旧挂载/电影/警察故事/Police Story.mkv",
                "/旧挂载/电影/警察故事/poster.jpg",
                "/旧挂载/电影/A计划/Project A.mp4",
                "/旧挂载/电影/A计划/movie.nfo",
                "/旧挂载/电影/合集说明.txt",
            },
        )
        self.assertTrue(all(item["status"] == "ready" for item in task["mappings"]))
        self.assertTrue(all(item["raw_data"]["passthrough_import"] for item in task["mappings"]))
        move_operations = [item for item in task["operations"] if item["type"] == "move_file"]
        self.assertEqual(len(move_operations), 5)
        self.assertTrue(all(item["raw_data"]["fail_if_target_exists"] for item in move_operations))
        self.assertTrue(task["raw_data"]["passthrough_import"]["skip_openlist_strm_refresh"])

    def test_single_file_at_job_root_moves_to_category_root_without_rename(self) -> None:
        root = "/旧挂载/_入库暂存/电影/job-1"
        service, task_id = self._create_staging_task(
            {root: [("未识别电影.2026.mkv", False), ("未识别电影.2026.srt", False)]}
        )

        result = service.skip_task(task_id)

        self.assertTrue(result["success"], result)
        task = self.database.get_organizer_task(task_id)
        self.assertEqual(
            {item["target_path"] for item in task["mappings"]},
            {
                "/旧挂载/电影/未识别电影.2026.mkv",
                "/旧挂载/电影/未识别电影.2026.srt",
            },
        )

    def test_task_state_race_rejects_passthrough_without_replacing_plan(self) -> None:
        root = "/旧挂载/_入库暂存/电影/job-1"
        service, task_id = self._create_staging_task({root: [("电影.mkv", False)]})
        original_replace = self.database.replace_organizer_plan

        def racing_replace(*args: Any, **kwargs: Any) -> bool:
            self.database.update_organizer_task(
                task_id,
                status="cancelled",
                expected_statuses={"waiting_review"},
                bump_revision=True,
            )
            return original_replace(*args, **kwargs)

        self.database.replace_organizer_plan = racing_replace  # type: ignore[method-assign]

        result = service.skip_task(task_id)

        self.assertFalse(result["success"], result)
        self.assertTrue(result["conflict"])
        task = self.database.get_organizer_task(task_id)
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["mappings"], [])
        self.assertEqual(task["operations"], [])

    def test_non_staging_task_keeps_legacy_skip_without_openlist_scan(self) -> None:
        task_id = self.database.create_organizer_task(
            category="movie",
            openlist_root_path="/旧挂载/电影/已入库电影",
            status="waiting_review",
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database
        service.categories = {"movie": {"openlist_root_path": "/旧挂载/电影"}}

        result = service.skip_task(task_id)

        self.assertTrue(result["success"], result)
        self.assertTrue(result["skipped"])
        self.assertEqual(self.database.get_organizer_task(task_id, include_children=False)["status"], "skipped")

    def test_non_staging_skip_completes_linked_job_and_guest_request(self) -> None:
        job_id, _created = self.database.create_job(
            {
                "title": "已在正式目录的电影",
                "category": "movie",
                "category_label": "电影",
                "source_type": "magnet",
                "source_url": "magnet:?xt=urn:btih:skip-linked-status",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/媒体/电影/已在正式目录的电影",
                "status": "review",
            }
        )
        request_id = self.database.create_guest_request(
            {
                "request_token": uuid.uuid4().hex,
                "job_id": job_id,
                "title": "已在正式目录的电影",
                "category": "movie",
                "source_type": "magnet",
                "source_url": "magnet:?xt=urn:btih:skip-linked-status",
                "status": "review",
                "public_status": "等待审核",
            }
        )
        task_id = self.database.create_organizer_task(
            job_id=job_id,
            category="movie",
            openlist_root_path="/媒体/电影/已在正式目录的电影",
            status="waiting_review",
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database
        service.categories = {"movie": {"openlist_root_path": "/媒体/电影"}}

        result = service.skip_task(task_id)

        self.assertTrue(result["success"], result)
        self.assertEqual(self.database.get_job(job_id)["status"], "done")
        guest_request = self.database.get_guest_request(request_id)
        self.assertEqual(guest_request["status"], "done")
        self.assertEqual(guest_request["public_status"], "已完成")
        self.assertEqual(guest_request["raw_data"]["status_sync"]["organizer_task_id"], task_id)
        self.assertTrue(
            any(
                event["message"] == "系统同步关联正式任务状态"
                for event in self.database.list_guest_request_events(request_id)
            )
        )

    def test_startup_repairs_confirmed_skipped_task_but_not_unconfirmed_staging_task(self) -> None:
        repaired_job_id, _created = self.database.create_job(
            {
                "title": "已确认完成的合集",
                "category": "movie",
                "category_label": "电影",
                "source_type": "magnet",
                "source_url": "magnet:?xt=urn:btih:confirmed-skipped",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/暂存/电影/job-confirmed",
                "status": "review",
            }
        )
        repaired_request_id = self.database.create_guest_request(
            {
                "request_token": uuid.uuid4().hex,
                "job_id": repaired_job_id,
                "title": "已确认完成的合集",
                "category": "movie",
                "source_type": "magnet",
                "source_url": "magnet:?xt=urn:btih:confirmed-skipped",
                "status": "review",
                "public_status": "等待审核",
            }
        )
        repaired_task_id = self.database.create_organizer_task(
            job_id=repaired_job_id,
            category="movie",
            openlist_root_path="/暂存/电影/job-confirmed",
            status="skipped",
            raw_data={"staging_plan": {"enabled": True}},
            evidence={"completion_confirmation": {"success": True, "confirmed_count": 3}},
        )

        blocked_job_id, _created = self.database.create_job(
            {
                "title": "仍在暂存区的合集",
                "category": "movie",
                "category_label": "电影",
                "source_type": "magnet",
                "source_url": "magnet:?xt=urn:btih:unconfirmed-skipped",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/暂存/电影/job-unconfirmed",
                "status": "review",
            }
        )
        self.database.create_organizer_task(
            job_id=blocked_job_id,
            category="movie",
            openlist_root_path="/暂存/电影/job-unconfirmed",
            status="skipped",
            raw_data={"staging_plan": {"enabled": True}},
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database

        result = service._recover_completed_linked_jobs_on_startup()

        self.assertIn(repaired_task_id, result["recovered_task_ids"])
        self.assertEqual(self.database.get_job(repaired_job_id)["status"], "done")
        self.assertEqual(self.database.get_guest_request(repaired_request_id)["status"], "done")
        self.assertEqual(self.database.get_guest_request(repaired_request_id)["public_status"], "已完成")
        self.assertEqual(self.database.get_job(blocked_job_id)["status"], "review")

    def test_startup_repairs_guest_when_linked_job_is_already_done(self) -> None:
        job_id, _created = self.database.create_job(
            {
                "title": "状态补偿测试",
                "category": "movie",
                "category_label": "电影",
                "source_type": "magnet",
                "source_url": "magnet:?xt=urn:btih:guest-only-recovery",
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/媒体/电影/状态补偿测试",
                "status": "done",
            }
        )
        request_id = self.database.create_guest_request(
            {
                "request_token": uuid.uuid4().hex,
                "job_id": job_id,
                "title": "状态补偿测试",
                "category": "movie",
                "source_type": "magnet",
                "source_url": "magnet:?xt=urn:btih:guest-only-recovery",
                "status": "review",
                "public_status": "等待审核",
            }
        )
        task_id = self.database.create_organizer_task(
            job_id=job_id,
            category="movie",
            openlist_root_path="/媒体/电影/状态补偿测试",
            status="skipped",
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database

        result = service._recover_completed_linked_jobs_on_startup()

        self.assertIn(task_id, result["recovered_task_ids"])
        self.assertEqual(self.database.get_guest_request(request_id)["status"], "done")
        self.assertEqual(self.database.get_guest_request(request_id)["public_status"], "已完成")

    def test_passthrough_completion_never_starts_openlist_global_scan(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.organizer_config = {"strm_refresh_after_apply": True}
        service.openlist = SimpleNamespace(
            refresh_strm=lambda *_args, **_kwargs: self.fail("直通入库不应触发 OpenList 扫描")
        )
        task = {
            "category": "movie",
            "raw_data": {
                "passthrough_import": {
                    "enabled": True,
                    "skip_openlist_strm_refresh": True,
                }
            },
        }

        self.assertEqual(service._refresh_openlist_strm_for_task(1, task), {})

    def test_single_file_passthrough_executes_to_done_and_completes_linked_job(self) -> None:
        root = "/旧挂载/_入库暂存/电影/job-1"
        source = f"{root}/原始文件.mkv"
        target = "/旧挂载/电影/原始文件.mkv"
        service, task_id = self._create_staging_task({root: [("原始文件.mkv", False)]})
        memory = _MemoryOpenList(
            files={source},
            directories={
                "/旧挂载",
                "/旧挂载/电影",
                "/旧挂载/_入库暂存",
                "/旧挂载/_入库暂存/电影",
                root,
            },
        )
        service.openlist = memory
        service.owner_id = "passthrough-test"
        service.fnos = None
        service.organizer_config.update(
            {
                "operation_visibility_attempts": 1,
                "operation_visibility_delay_seconds": 0,
                "run_lease_seconds": 60,
                "run_heartbeat_interval_seconds": 10,
                "lock_wait_seconds": 1,
                "lock_poll_seconds": 0.01,
                "refresh_fnos_after_apply": False,
                "strm_refresh_after_apply": True,
            }
        )

        prepared = service.skip_task(task_id)
        result = service.apply_task_from_worker(task_id)

        self.assertTrue(prepared["success"], prepared)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["status"], "done")
        self.assertNotIn(source, memory.files)
        self.assertIn(target, memory.files)
        self.assertEqual(memory.refresh_calls, [])
        task = self.database.get_organizer_task(task_id, include_children=False)
        self.assertEqual(task["status"], "done")
        self.assertEqual(self.database.get_job(1)["status"], "done")


class OrganizerPassthroughDispatchTests(unittest.TestCase):
    def test_admin_skip_dispatches_prepared_passthrough_to_durable_worker(self) -> None:
        class Organizer:
            def skip_task(self, task_id: int) -> dict[str, Any]:
                return {
                    "success": True,
                    "ready_for_apply": True,
                    "passthrough": True,
                    "task_id": task_id,
                    "file_count": 7,
                    "message": "已生成原名直通入库计划",
                }

            def start_apply_task(self, _task_id: int) -> dict[str, Any]:
                raise AssertionError("存在 durable worker 时不应启动进程内线程")

        class Dispatcher:
            def __init__(self) -> None:
                self.task_ids: list[int] = []

            def organizer_apply(self, task_id: int) -> dict[str, Any]:
                self.task_ids.append(task_id)
                return {"success": True, "queued": True, "worker_task_id": 99}

        dispatcher = Dispatcher()
        service = OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(
                organizer=Organizer(),
                worker_dispatcher=dispatcher,
            )
        )

        result, status = service.skip(12)

        self.assertEqual(status, 200)
        self.assertTrue(result["queued"])
        self.assertTrue(result["passthrough"])
        self.assertEqual(result["file_count"], 7)
        self.assertEqual(dispatcher.task_ids, [12])


class OrganizerPassthroughUiTests(unittest.TestCase):
    def test_skip_copy_describes_preserved_name_import(self) -> None:
        source = Path("static/admin-organizer.js").read_text(encoding="utf-8")

        self.assertIn("跳过整理", source)
        self.assertIn('["done", "skipped", "cancelled"].includes(status)', source)

        template = Path("templates/admin.html").read_text(encoding="utf-8")
        self.assertIn('class="data-table table-organizer"', template)
        self.assertIn("保留原目录和文件名", source)
        self.assertNotIn("放弃这个任务，不移动或删除文件", source)


if __name__ == "__main__":
    unittest.main()
