from __future__ import annotations

import unittest

from fnos_media_import.domain.models import SelectionSnapshot
from fnos_media_import.organizer.openlist_client import OpenListError
from fnos_media_import.organizer.parser import category_target_root, standard_target_path
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.planning.job_plan import attach_plan_integrity, compute_operation_key
from tests._organizer_v1_fixtures import (
    attach_organizer_v1_task,
    bind_organizer_v1_execution_repository,
    organizer_v1_job,
)


def _plan(job_id: int = 7) -> dict:
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


class _Database:
    def __init__(self, task: dict | None = None, job: dict | None = None) -> None:
        self.task = task or {}
        self.job = job
        self.updates: list[dict] = []

    def get_organizer_task(self, _task_id: int, **_kwargs):
        return self.task

    def get_job(self, _job_id: int):
        return self.job

    def update_organizer_task(self, _task_id: int, **updates):
        self.updates.append(updates)
        for key in ("status", "error_message", "raw_data"):
            if key in updates:
                self.task[key] = updates[key]
        return True


class OrganizerV1HardCutoverTests(unittest.TestCase):
    def test_category_root_rejects_legacy_paths_outside_diagnostics(self) -> None:
        with self.assertRaises(ValueError):
            category_target_root({"openlist_root_path": "/旧分类"})
        self.assertEqual(
            category_target_root(
                {"openlist_root_path": "/旧分类"},
                allow_legacy_diagnostic=True,
            ),
            "/旧分类",
        )

    def test_target_category_uses_frozen_plan_and_ignores_runtime_paths(self) -> None:
        plan = _plan()
        job = organizer_v1_job(plan)
        service = OrganizerService.__new__(OrganizerService)
        service.db = _Database(job=job)
        task = attach_organizer_v1_task(
            {
                "id": 1,
                "raw_data": {"organizer_context": {"target_root_path": "/被篡改"}},
            },
            plan,
        )
        bind_organizer_v1_execution_repository(service, plan)
        category = service._target_category_for_task(
            task,
            {"label": "电影", "openlist_root_path": "/旧分类"},
        )
        self.assertEqual(category["_v1_destination_final"], "/媒体")
        self.assertEqual(category["source_category_root_path"], "/入库/job-7")
        self.assertNotIn("openlist_root_path", category)
        self.assertEqual(
            standard_target_path(
                category_key="movie",
                category=category,
                title="片名",
                year="2026",
                ext=".mkv",
            ),
            "/媒体/片名 (2026).mkv",
        )

    def test_unlinked_task_is_moved_to_review_without_openlist_execution(self) -> None:
        task = {
            "id": 4,
            "job_id": None,
            "category": "movie",
            "status": "pending",
            "revision": 1,
            "openlist_root_path": "/旧分类",
            "raw_data": {"staging_plan": {"root": "/旧分类"}},
        }
        database = _Database(task=task)
        service = OrganizerService.__new__(OrganizerService)
        service.db = database
        result = service.process_task(4)
        self.assertTrue(result.get("manual_review"))
        self.assertEqual(task["status"], "waiting_review")
        self.assertTrue(database.updates)

    def test_operations_reject_category_paths_without_v1_root(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        with self.assertRaises(ValueError):
            service._operations_for_mappings(
                [
                    {
                        "source_path": "/旧/a.mkv",
                        "target_path": "/旧/b.mkv",
                        "status": "ready",
                    }
                ],
                {"openlist_root_path": "/旧分类"},
            )

    def test_tampered_scan_root_is_rejected_against_v1_plan(self) -> None:
        plan = _plan()
        job = organizer_v1_job(plan)
        service = OrganizerService.__new__(OrganizerService)
        service.db = _Database(job=job)
        task = attach_organizer_v1_task({"id": 1}, plan)
        task["openlist_root_path"] = "/篡改/job-7"
        bind_organizer_v1_execution_repository(service, plan)
        with self.assertRaises(OpenListError):
            service._validate_v1_task_boundaries(
                task,
                scan_root=task["openlist_root_path"],
                target_category={},
            )


if __name__ == "__main__":
    unittest.main()
