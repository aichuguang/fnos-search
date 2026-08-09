from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from fnos_media_import.web_input import _worker_dispatch_enabled_for_role
from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.domain.models import SelectionSnapshot
from fnos_media_import.organizer.service import OrganizerService, _utc_after_seconds, _utc_now_text
from fnos_media_import.planning.job_plan import attach_plan_integrity, compute_operation_key
from fnos_media_import.services.organizer_admin_service import (
    OrganizerAdminCommandDependencies,
    OrganizerAdminCommandService,
)
from fnos_media_import.services.worker_task_dispatcher import WorkerTaskDispatcher
from tests._organizer_v1_fixtures import (
    attach_organizer_v1_task,
    bind_organizer_v1_execution_repository,
    organizer_v1_job,
)
from tests._repository_v1_fakes import RepositoryFakeMixin


def _worker_schedule_plan(job_id: int) -> dict:
    selection = SelectionSnapshot.from_payload("quark", {"share_id": "share-1"}).to_dict()
    namespace = "openlist:main:media"
    storage_identity_id = "provider:quark:account:acc-quark"
    plan = {
        "version": 1,
        "plan_schema_version": 1,
        "job_id": job_id,
        "category": "movie",
        "source": {
            "provider_kind": "quark",
            "source_form": "share",
            "canonical_source_id": "quark:share:1",
            "search_source_id": "pansou",
            "record_id": "record-1",
            "record_revision": 1,
            "account_id": "acc-quark",
            "account_revision": 1,
            "account_selection_revision": 1,
            "selection": selection,
            "selection_hash": selection["selection_hash"],
        },
        "destination": {
            "id": "native-main",
            "provider_kind": "mobile",
            "target_revision": 1,
            "binding_id": "binding-1",
            "binding_revision": 1,
        },
        "transfer": {
            "strategy": "native_save",
            "executor": "native",
            "idempotency_key": "",
        },
        "bindings": {
            "organizer_mount_id": "media",
            "organizer_mount_revision": 2,
            "organizer_mount_namespace_id": namespace,
            "organizer_mount_storage_identity_id": storage_identity_id,
            "organizer_mount_identity_root": "/入库",
            "organizer_connection_id": "main",
            "organizer_connection_revision": 3,
        },
        "paths": {
            "destination_final": {
                "kind": "native",
                "namespace_id": "native:mobile:native-main",
                "path": "/媒体",
            },
            "organizer": {
                "kind": "openlist",
                "namespace_id": namespace,
                "path": f"/入库/job-{job_id}",
                "mount_id": "media",
                "mount_revision": 2,
                "connection_id": "main",
                "connection_revision": 3,
                "storage_identity_id": storage_identity_id,
                "identity_root": "/入库",
            },
        },
        "target_category": {},
        "routing": {
            "assignment_id": "assignment-1",
            "assignment_revision": 1,
            "search_source_id": "pansou",
            "category_key": "movie",
            "destination_id": "native-main",
            "enabled": True,
        },
        "verification": {
            "minimum": ["file_count", "total_size"],
            "expected_file_count": 1,
            "expected_total_size": 1,
            "selection_hash": selection["selection_hash"],
        },
        "postprocess": {"organizer_enabled": True},
    }
    plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
    return attach_plan_integrity(plan)




class WorkerDispatchRoleTests(unittest.TestCase):
    def test_split_producer_roles_enable_durable_dispatch_without_manual_flag(self) -> None:
        self.assertTrue(_worker_dispatch_enabled_for_role({}, "web"))
        self.assertTrue(_worker_dispatch_enabled_for_role({}, "scheduler"))

    def test_worker_and_combined_roles_keep_configuration_switch(self) -> None:
        self.assertFalse(_worker_dispatch_enabled_for_role({}, "worker"))
        self.assertFalse(_worker_dispatch_enabled_for_role({}, "all"))
        self.assertTrue(_worker_dispatch_enabled_for_role({"durable_dispatch_enabled": True}, "all"))


class OrganizerWorkerRedispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"organizer-worker-redispatch-{uuid.uuid4().hex}.db"
        self._database_infrastructure = Database(self.db_path)
        self._database_infrastructure.init_schema()
        self.database = RepositorySet.from_database(self._database_infrastructure)

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
        job_id = 701
        plan = _worker_schedule_plan(job_id)
        attach_organizer_v1_task(task, plan)
        job = organizer_v1_job(plan)

        class FakeDatabase(RepositoryFakeMixin):
            @staticmethod
            def get_organizer_task(_task_id: int, include_children: bool = False) -> dict:
                return task

            @staticmethod
            def get_job(_job_id: int) -> dict:
                return job

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {"stable_window_seconds": 90}
        service._background_suspended = False
        bind_organizer_v1_execution_repository(service, plan)
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
