from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.domain.models import SelectionSnapshot
from fnos_media_import.planning.job_plan import attach_plan_integrity, compute_operation_key
from fnos_media_import.services.callback_service import CallbackDependencies, RcloneCallbackService
from fnos_media_import.services.import_completion_dispatcher import ImportCompletionDispatcher


def _plan(job_id: int, strategy: str) -> dict:
    selection = SelectionSnapshot.from_payload("quark", {}).to_dict()
    selection_hash = selection["selection_hash"]
    paths = {
        "destination_final": {
            "kind": "native",
            "namespace_id": "native:mobile:mobile-main",
            "path": "/Media",
        },
        "source_landing": {
            "kind": "openlist",
            "namespace_id": "openlist:ol:source",
            "path": f"/_fnos_ingress/job-{job_id}",
        },
        "local_temp": {
            "kind": "relay",
            "namespace_id": "local:relay",
            "path": f"/data/relay/job-{job_id}",
        },
    }
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
            "account_id": "account-quark",
            "account_revision": 1,
            "account_selection_revision": 1,
            "selection": selection,
            "selection_hash": selection_hash,
        },
        "destination": {
            "id": "mobile-main",
            "provider_kind": "mobile",
            "target_revision": 1,
            "binding_id": "binding-1",
            "binding_revision": 1,
        },
        "transfer": {
            "strategy": strategy,
            "executor": "provider_relay" if strategy == "relay_upload_provider" else "native",
            "idempotency_key": "",
        },
        "paths": paths,
        "target_category": {},
        "routing": {
            "assignment_id": "assignment-1",
            "assignment_revision": 1,
            "search_source_id": "pansou",
            "category_key": "movie",
            "destination_id": "mobile-main",
            "enabled": True,
        },
        "verification": {
            "minimum": ["file_count", "total_size"],
            "expected_file_count": 0,
            "expected_total_size": 0,
            "selection_hash": selection_hash,
        },
    }
    plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
    return attach_plan_integrity(plan)


class _Database(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def add_event(self, *args) -> None:
        self.events.append(args)

    def add_rclone_event(self, *args) -> None:
        self.events.append(args)

    def get_job(self, _job_id: int):
        return None

    @staticmethod
    def update_job_if_status(*_args, **_kwargs) -> bool:
        return True


class DispatcherGuardTests(unittest.TestCase):
    def test_dispatch_rejects_partial_v1_envelope_before_queueing(self) -> None:
        plan = _plan(7, "relay_upload_provider")
        plan.pop("plan_schema_version")
        queued: list[int] = []
        job = {
            "id": 7,
            "status": "waiting_transfer",
            "category": "movie",
            "destination_id": "mobile-main",
            "plan_version": 1,
            "_canonical_execution_plan": plan,
        }
        dispatcher = ImportCompletionDispatcher(
            database=_Database(),
            enqueue_transfer=lambda job_id: queued.append(job_id),
        )
        result = dispatcher.dispatch({"success": True, "job": job}, "provider_done")

        self.assertTrue(result["rejected"])
        self.assertEqual(queued, [])

    def test_relay_waiting_job_uses_durable_transfer_callback(self) -> None:
        database = _Database()
        queued: list[int] = []
        dispatcher = ImportCompletionDispatcher(
            database=database,
            enqueue_transfer=lambda job_id: queued.append(job_id) or {"success": True, "queued": True},
        )
        plan = _plan(7, "relay_upload_provider")
        job = {
            "id": 7,
            "status": "waiting_transfer",
            "category": "movie",
            "plan_version": 1,
            "destination_id": "mobile-main",
            "_canonical_execution_plan": plan,
            "raw_data": {},
        }

        result = dispatcher.dispatch({"success": True, "job": job}, "provider_done")

        self.assertEqual(queued, [7])
        self.assertTrue(result["success"])
        self.assertTrue(result["queued"])

    def test_unowned_category_summary_is_rejected(self) -> None:
        database = _Database()
        service = RcloneCallbackService(
            CallbackDependencies(
                db=database,
                rclone=SimpleNamespace(),
                safe_int=lambda value, default, _minimum, _maximum: int(value or default),
                callback_level=lambda _status: "info",
                cancelled_status="cancelled",
            )
        )

        response, status_code = service.handle(
            {"run_id": 9, "status": "category_done", "category": "movie"}
        )

        self.assertEqual(status_code, 409)
        self.assertTrue(response["rejected"])

    def test_unowned_resolve_callback_cannot_invoke_path_helper(self) -> None:
        database = _Database()
        resolve_calls: list[tuple] = []
        service = RcloneCallbackService(
            CallbackDependencies(
                db=database,
                rclone=SimpleNamespace(
                    resolve_resource_folder=lambda *args, **kwargs: resolve_calls.append(
                        (args, kwargs)
                    ),
                ),
                safe_int=lambda value, default, _minimum, _maximum: int(value or default),
                callback_level=lambda _status: "info",
                cancelled_status="cancelled",
            )
        )

        response, status_code = service.handle(
            {
                "run_id": 9,
                "status": "resolve_folder",
                "category": "tv",
                "filename": "E01.mkv",
                "source_path": "/source/job-7/E01.mkv",
                "target_path": "/target/job-7/E01.mkv",
            }
        )

        self.assertEqual(status_code, 409)
        self.assertEqual(response["error_code"], "rclone_job_required")
        self.assertEqual(resolve_calls, [])

    def test_worker_callbacks_are_strictly_job_bound(self) -> None:
        content = (
            Path(__file__).resolve().parents[1] / "scripts" / "fnos_rclone_worker.sh"
        ).read_text(encoding="utf-8")

        self.assertIn('payload["job_id"] = int(job_dir_match.group(1))', content)
        self.assertIn('payload["require_job_match"] = True', content)
        self.assertIn("if not job_dir_match:", content)
        self.assertNotIn('"status": "resolve_folder"', content)
        self.assertNotIn('"status": "naming_plan"', content)


if __name__ == "__main__":
    unittest.main()
