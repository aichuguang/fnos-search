from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from fnos_media_import.app import _worker_dispatch_enabled_for_role
from fnos_media_import.constants import JOB_DONE, JOB_REVIEW
from fnos_media_import.database import Database
from fnos_media_import.organizer.service import OrganizerService, _utc_after_seconds, _utc_now_text
from fnos_media_import.services.organizer_admin_service import (
    OrganizerAdminCommandDependencies,
    OrganizerAdminCommandService,
)
from fnos_media_import.services.organizer_dispatch_service import OrganizerDispatchService
from fnos_media_import.services.worker_task_dispatcher import WorkerTaskDispatcher


class OrganizerDurableDispatchTests(unittest.TestCase):
    @staticmethod
    def _build_service(*, dispatch_process, task_id: int = 81):
        jobs = {
            11: {
                "id": 11,
                "title": "测试剧",
                "category": "tv",
                "category_label": "电视剧",
                "status": JOB_DONE,
                "raw_data": {},
            }
        }
        stage_updates: list[tuple[str, str, str]] = []

        class FakeDatabase:
            @staticmethod
            def get_job(job_id: int) -> dict:
                return dict(jobs.get(job_id) or {})

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

        class FakeOrganizer:
            enabled = True
            openlist = type("OpenListState", (), {"configured": True})()

            @staticmethod
            def enqueue_from_completed_directory(**_kwargs) -> dict:
                return {
                    "success": True,
                    "queued": True,
                    "task_id": task_id,
                    "message": "已创建 OpenList 标准化任务",
                }

        def set_completion_stage(job, status, stage, message, *_args, **_kwargs):
            updated = {**job, "status": status}
            if job.get("id"):
                jobs[int(job["id"])] = updated
            stage_updates.append((status, stage, message))
            return updated

        service = OrganizerDispatchService(
            database=FakeDatabase(),
            organizer=FakeOrganizer(),
            resolve_plan=lambda _job: {"root_path": "/移动云/_入库暂存/电视剧/job-11"},
            resolve_rclone_plan=lambda _item: {"root_path": "/移动云/_入库暂存/电视剧/job-11"},
            set_completion_stage=set_completion_stage,
            invalid_virtual_path=lambda _path: False,
            dispatch_process=dispatch_process,
        )
        return service, jobs, stage_updates

    def test_direct_auto_task_is_handed_to_durable_worker(self) -> None:
        dispatches: list[tuple[int, bool]] = []

        def dispatch_process(task_id: int, *, auto_apply: bool) -> dict:
            dispatches.append((task_id, auto_apply))
            return {"success": True, "queued": True, "worker_task_id": 501}

        service, jobs, _updates = self._build_service(dispatch_process=dispatch_process)
        result = {"success": True, "job": jobs[11]}

        organizer = service.enqueue_completed_import(result, "sixpan_poll")

        self.assertEqual(dispatches, [(81, True)])
        self.assertTrue(organizer["success"])
        self.assertEqual(organizer["worker_dispatch"]["worker_task_id"], 501)

    def test_rclone_auto_task_is_handed_to_durable_worker(self) -> None:
        dispatches: list[tuple[int, bool]] = []

        def dispatch_process(task_id: int, *, auto_apply: bool) -> dict:
            dispatches.append((task_id, auto_apply))
            return {"success": True, "queued": True, "worker_task_id": 502}

        service, jobs, _updates = self._build_service(dispatch_process=dispatch_process, task_id=82)

        organizer = service.enqueue_rclone_completed_items(
            {
                "completed_items": [
                    {
                        "job_id": 11,
                        "job": jobs[11],
                        "category": "tv",
                        "category_label": "电视剧",
                    }
                ]
            },
            {"run_id": 7},
        )

        self.assertEqual(dispatches, [(82, True)])
        self.assertTrue(organizer["success"])
        self.assertEqual(organizer["items"][0]["worker_dispatch"]["worker_task_id"], 502)

    def test_required_queue_failure_moves_job_to_review(self) -> None:
        service, jobs, updates = self._build_service(dispatch_process=lambda *_args, **_kwargs: None)
        result = {"success": True, "job": jobs[11]}

        organizer = service.enqueue_completed_import(result, "cloud139_reconcile")

        self.assertFalse(organizer["success"])
        self.assertEqual(updates[-1][0:2], (JOB_REVIEW, "review"))
        self.assertIn("持久化 Worker 投递失败", updates[-1][2])
        self.assertEqual(result["job"]["status"], JOB_REVIEW)


class WorkerDispatchRoleTests(unittest.TestCase):
    def test_split_producer_roles_enable_durable_dispatch_without_manual_flag(self) -> None:
        self.assertTrue(_worker_dispatch_enabled_for_role({}, "web"))
        self.assertTrue(_worker_dispatch_enabled_for_role({}, "scheduler"))

    def test_worker_and_legacy_all_roles_keep_configuration_switch(self) -> None:
        self.assertFalse(_worker_dispatch_enabled_for_role({}, "worker"))
        self.assertFalse(_worker_dispatch_enabled_for_role({}, "all"))
        self.assertTrue(_worker_dispatch_enabled_for_role({"durable_dispatch_enabled": True}, "all"))


class OrganizerWorkerRedispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"organizer-worker-redispatch-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_terminal_worker_task_is_atomically_reactivated_for_same_organizer_task(self) -> None:
        dispatcher = WorkerTaskDispatcher(
            repository=self.database.worker_tasks,
            enabled=lambda: True,
            config_revision=lambda: 3,
        )
        first = dispatcher.organizer_process(81, auto_apply=True)
        claimed = self.database.worker_tasks.claim("worker-a")
        self.assertEqual(claimed["id"], first["worker_task_id"])
        self.assertTrue(self.database.worker_tasks.complete(claimed["id"], "worker-a", {"success": True}))

        second = dispatcher.organizer_process(81, auto_apply=True)
        third = dispatcher.organizer_process(81, auto_apply=True)
        stored = self.database.worker_tasks.get(first["worker_task_id"])

        self.assertEqual(second["worker_task_id"], first["worker_task_id"])
        self.assertTrue(second["created"])
        self.assertFalse(third["created"])
        self.assertEqual(stored["status"], "pending")
        self.assertEqual(stored["attempts"], 0)
        self.assertTrue(stored["payload"]["respect_schedule"])


class OrganizerWorkerScheduleTests(unittest.TestCase):
    @staticmethod
    def _service(task: dict) -> tuple[OrganizerService, list[tuple[int, int | float]]]:
        class FakeDatabase:
            @staticmethod
            def get_organizer_task(_task_id: int, include_children: bool = False) -> dict:
                return task

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {"stable_window_seconds": 90}
        service._background_suspended = False
        scheduled: list[tuple[int, int | float]] = []
        service._schedule_task_after = lambda task_id, delay: scheduled.append((task_id, delay)) or True
        return service, scheduled

    def test_waiting_openlist_worker_handoff_preserves_persisted_next_retry(self) -> None:
        task = {
            "id": 91,
            "status": "waiting_openlist",
            "raw_data": {
                "openlist_visibility_retry": {
                    "attempts": 1,
                    "next_retry_at": _utc_after_seconds(120),
                }
            },
        }
        service, scheduled = self._service(task)
        service.process_task = lambda *_args, **_kwargs: self.fail("deadline 到达前不应扫描")

        result = service.process_task_from_worker(91, auto_apply=True, respect_schedule=True)

        self.assertTrue(result["deferred"])
        self.assertGreaterEqual(result["delay_seconds"], 119)
        self.assertEqual(scheduled[0][0], 91)
        self.assertEqual(task["raw_data"]["openlist_visibility_retry"]["attempts"], 1)

    def test_stabilizing_worker_handoff_preserves_remaining_stable_window(self) -> None:
        task = {"id": 92, "status": "stabilizing", "created_at": _utc_now_text(), "raw_data": {}}
        service, scheduled = self._service(task)
        service.process_task = lambda *_args, **_kwargs: self.fail("稳定窗口结束前不应扫描")

        result = service.process_task_from_worker(92, auto_apply=True, respect_schedule=True)

        self.assertTrue(result["deferred"])
        self.assertGreaterEqual(result["delay_seconds"], 89)
        self.assertEqual(scheduled[0][0], 92)

    def test_manual_retry_bypasses_persisted_wait_but_is_durably_requeued(self) -> None:
        calls: list[tuple] = []

        class FakeOrganizer:
            @staticmethod
            def process_task(*args, **kwargs) -> dict:
                calls.append((args, kwargs))
                return {"success": True}

            @staticmethod
            def status() -> dict:
                return {}

        class FakeDispatcher:
            @staticmethod
            def organizer_process(*args, **kwargs) -> dict:
                calls.append((args, kwargs))
                return {"success": True, "queued": True, "created": True}

        service = OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(
                organizer=FakeOrganizer(),
                worker_dispatcher=FakeDispatcher(),
            )
        )

        result, status_code = service.retry(93)

        self.assertEqual(status_code, 200)
        self.assertTrue(result["queued"])
        self.assertEqual(calls, [((93,), {"auto_apply": True, "respect_schedule": False})])

    def test_confirmation_failure_retry_resumes_persisted_apply_plan(self) -> None:
        calls: list[tuple[str, int]] = []
        task = {
            "id": 93,
            "status": "waiting_review",
            "evidence": {"completion_confirmation": {"success": False}},
            "mappings": [{"id": 1, "status": "ready"}],
            "operations": [
                {"id": 1, "type": "move_file", "status": "done"},
                {"id": 2, "type": "cleanup_empty_dir", "status": "skipped"},
            ],
        }

        class FakeOrganizer:
            @staticmethod
            def get_task(_task_id: int) -> dict:
                return task

        class FakeDispatcher:
            @staticmethod
            def organizer_apply(task_id: int) -> dict:
                calls.append(("apply", task_id))
                return {"success": True, "queued": True, "worker_task_id": 702}

            @staticmethod
            def organizer_process(*_args, **_kwargs) -> dict:
                raise AssertionError("确认失败不应重新扫描已清空的暂存目录")

        service = OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(
                organizer=FakeOrganizer(),
                worker_dispatcher=FakeDispatcher(),
            )
        )

        result, status_code = service.retry(93)

        self.assertEqual(status_code, 200)
        self.assertEqual(result["retry_mode"], "resume_apply")
        self.assertEqual(calls, [("apply", 93)])

    def test_failed_operation_retry_keeps_rescan_path(self) -> None:
        calls: list[tuple] = []
        task = {
            "id": 94,
            "status": "failed",
            "mappings": [{"id": 1, "status": "ready"}],
            "operations": [{"id": 1, "type": "move_file", "status": "failed"}],
        }

        class FakeOrganizer:
            @staticmethod
            def get_task(_task_id: int) -> dict:
                return task

        class FakeDispatcher:
            @staticmethod
            def organizer_process(*args, **kwargs) -> dict:
                calls.append((args, kwargs))
                return {"success": True, "queued": True}

        service = OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(
                organizer=FakeOrganizer(),
                worker_dispatcher=FakeDispatcher(),
            )
        )

        result, status_code = service.retry(94)

        self.assertEqual(status_code, 200)
        self.assertEqual(result["retry_mode"], "rescan")
        self.assertEqual(calls, [((94,), {"auto_apply": True, "respect_schedule": False})])

    def test_lease_loss_before_operation_status_write_resumes_apply_for_reconciliation(self) -> None:
        task = {
            "id": 95,
            "status": "failed",
            "error_message": "Organizer 运行租约已过期，已中止遗留执行",
            "mappings": [{"id": 1, "status": "ready"}],
            "operations": [{"id": 1, "type": "move_file", "status": "pending"}],
        }
        calls: list[int] = []

        class FakeOrganizer:
            @staticmethod
            def get_task(_task_id: int) -> dict:
                return task

        class FakeDispatcher:
            @staticmethod
            def organizer_apply(task_id: int) -> dict:
                calls.append(task_id)
                return {"success": True, "queued": True}

        service = OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(
                organizer=FakeOrganizer(),
                worker_dispatcher=FakeDispatcher(),
            )
        )

        result, _status_code = service.retry(95)

        self.assertEqual(result["retry_mode"], "resume_apply")
        self.assertEqual(calls, [95])

    def test_manual_rebuild_is_durably_queued_and_keeps_auto_apply(self) -> None:
        calls: list[tuple] = []

        class FakeOrganizer:
            @staticmethod
            def rebuild_task(*_args, **_kwargs) -> dict:
                raise AssertionError("split 部署不应在 Web 进程本地重建")

        class FakeDispatcher:
            @staticmethod
            def organizer_process(*args, **kwargs) -> dict:
                calls.append((args, kwargs))
                return {"success": True, "queued": True, "worker_task_id": 701}

        service = OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(
                organizer=FakeOrganizer(),
                worker_dispatcher=FakeDispatcher(),
            )
        )

        result, status_code = service.rebuild(94)

        self.assertEqual(status_code, 200)
        self.assertEqual(result["worker_task_id"], 701)
        self.assertEqual(calls, [((94,), {"auto_apply": True, "respect_schedule": False})])

    def test_local_rebuild_keeps_auto_apply(self) -> None:
        calls: list[tuple] = []
        service = OrganizerService.__new__(OrganizerService)
        service.process_task = lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True}

        result = service.rebuild_task(94)

        self.assertTrue(result["success"])
        self.assertEqual(calls, [((94,), {"auto_apply": True})])

    def test_manual_scan_creates_task_then_hands_it_to_durable_worker(self) -> None:
        calls: list[tuple] = []

        class FakeOrganizer:
            @staticmethod
            def create_manual_task(payload, *, defer_process=False) -> dict:
                calls.append(((payload,), {"defer_process": defer_process}))
                return {"success": True, "task_id": 95, "status": "pending"}

            @staticmethod
            def process_task(*_args, **_kwargs) -> dict:
                raise AssertionError("split 部署不应在 Web 进程本地扫描")

        class FakeDispatcher:
            @staticmethod
            def organizer_process(*args, **kwargs) -> dict:
                calls.append((args, kwargs))
                return {"success": True, "queued": True, "worker_task_id": 702}

        service = OrganizerAdminCommandService(
            OrganizerAdminCommandDependencies(
                organizer=FakeOrganizer(),
                worker_dispatcher=FakeDispatcher(),
            )
        )

        payload = {"category": "tv", "openlist_root_path": "/移动云/电视剧", "auto_apply": False}
        result, status_code = service.scan(payload)

        self.assertEqual(status_code, 200)
        self.assertEqual(result["task_id"], 95)
        self.assertEqual(result["worker_dispatch"]["worker_task_id"], 702)
        self.assertEqual(
            calls,
            [
                ((payload,), {"defer_process": True}),
                ((95,), {"auto_apply": False, "respect_schedule": False}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
