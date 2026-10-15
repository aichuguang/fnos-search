from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, jsonify, request

from fnos_media_import.database import Database
from fnos_media_import.openapi import get_openapi_spec
from fnos_media_import.public_web import _public_request_payload
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.services.public_import_job_coordinator import PublicImportJobCoordinator
from fnos_media_import.services.public_submission_service import PublicSubmissionDependencies, PublicSubmissionService
from fnos_media_import.services.request_approval_service import RequestApprovalDependencies, RequestApprovalService
from fnos_media_import.services.worker_task_dispatcher import WorkerTaskDispatcher
from fnos_media_import.web_input import _merge_raw_data


class FailedPublicRequestRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="failed-request-retry-")
        self.addCleanup(directory.cleanup)
        database = Database(Path(directory.name) / "requests.db")
        database.init_schema()
        self.db = RepositorySet.from_database(database)
        self.request_id = self.db.guest_request_commands.create({
            "request_token": "same-request-token", "title": "Example", "category": "tv", "status": "failed",
            "source_url": "magnet:?xt=urn:btih:example", "public_status": "failed",
            "raw_data": {"request": {"ignore_files": ["ad.mkv", "ad.mkv"]}, "error": "old failure"},
        })
        self.dispatcher = WorkerTaskDispatcher(repository=self.db.worker_tasks, enabled=lambda: True, config_revision=lambda: 1)
        self.worker_id = self.dispatcher.public_import_create(
            guest_request_id=self.request_id, request_token="same-request-token", submit_payload={"title": "Example"},
        )["worker_task_id"]
        self.set_worker_status("failed")
        self.submissions = PublicSubmissionService(PublicSubmissionDependencies(
            queries=self.db.guest_request_queries, commands=self.db.guest_request_commands,
            sync_request=lambda value: value, public_status=lambda value: value, public_request=lambda value: value,
        ))
        self.imports = Mock()
        self.coordinator = PublicImportJobCoordinator(
            import_service=lambda: self.imports, submission_service=lambda: self.submissions,
            runtime_revision=lambda: 1, executor_id=lambda: "test", start_rclone=lambda *_args: None,
            public_status=lambda value: value, safe_result=lambda value: value, warn=lambda *_args: None,
            worker_dispatcher=self.dispatcher,
        )
        self.dependencies = RequestApprovalDependencies(
            requests=self.db.guest_request_queries, commands=self.db.guest_request_commands, jobs=self.db.job_queries,
            coordinate_import=self.coordinator.execute, public_status=lambda value: value, safe_result=lambda value: value,
            merge_raw_data=_merge_raw_data,
            sanitize_request_payload=_public_request_payload, category_label=lambda value: value, db=self.db,
        )
        self.approval = RequestApprovalService(self.dependencies)
        app = Flask(__name__)

        def approve(request_id):
            result, status = self.approval.approve(request_id, request.get_json() or {}, admin="test")
            return jsonify(result), status

        app.add_url_rule("/approve/<int:request_id>", view_func=approve, methods=["POST"])
        self.client = app.test_client()

    def set_worker_status(self, status: str) -> None:
        with self.db.connect() as connection:
            connection.execute("UPDATE worker_tasks SET status=?, attempts=3 WHERE id=?", (status, self.worker_id))

    def retry(self):
        return self.client.post(f"/approve/{self.request_id}", json={"retry": True})

    def test_explicit_retry_atomically_requeues_same_worker_and_is_claimable_once(self) -> None:
        response = self.retry()
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["queued"])
        stored = self.db.guest_request_queries.get(self.request_id)
        worker = self.db.worker_tasks.get(self.worker_id)
        self.assertEqual(stored["status"], "submitted")
        self.assertEqual(stored["raw_data"]["worker"]["status"], "queued")
        self.assertEqual(stored["raw_data"]["worker"]["task_id"], self.worker_id)
        self.assertIsNone(stored.get("job_id"))
        self.assertEqual((worker["status"], worker["attempts"]), ("pending", 0))
        self.assertEqual(worker["idempotency_key"], "public-import:same-request-token")
        self.assertEqual(worker["payload"]["submit_payload"]["ignore_files"], ["ad.mkv"])
        self.assertEqual(self.retry().status_code, 409)
        claimed = self.db.worker_tasks.claim("audit-worker")
        self.assertEqual(claimed["id"], self.worker_id)
        self.assertIsNone(self.db.worker_tasks.claim("another-worker"))
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM worker_tasks").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0], 0)
        self.imports.create_internal_import_job.assert_not_called()

    def test_worker_states_other_than_failed_are_never_reactivated_or_relabelled(self) -> None:
        before_request = self.db.guest_request_queries.get(self.request_id)
        for status in ("completed", "cancelled", "pending", "running", "unknown"):
            with self.subTest(status=status):
                self.set_worker_status(status)
                before_worker = self.db.worker_tasks.get(self.worker_id)
                response = self.retry()
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.db.guest_request_queries.get(self.request_id), before_request)
                self.assertEqual(self.db.worker_tasks.get(self.worker_id), before_worker)

    def test_failed_request_requires_explicit_admin_retry(self) -> None:
        response = self.client.post(f"/approve/{self.request_id}", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db.worker_tasks.get(self.worker_id)["status"], "failed")
        self.assertEqual(self.db.guest_request_queries.get(self.request_id)["status"], "failed")

    def test_normal_approval_preserves_persisted_selection_and_worker_metadata(self) -> None:
        self.db.guest_request_commands.update(self.request_id, status="pending_review")
        self.set_worker_status("pending")
        response = self.client.post(f"/approve/{self.request_id}", json={})
        self.assertEqual(response.status_code, 202)
        worker = self.db.worker_tasks.get(self.worker_id)
        self.assertEqual(worker["payload"]["submit_payload"]["ignore_files"], ["ad.mkv"])
        stored = self.db.guest_request_queries.get(self.request_id)
        self.assertEqual(stored["raw_data"]["approval"]["status"], "approved")
        self.assertEqual(stored["raw_data"]["worker"]["task_id"], self.worker_id)
        self.assertEqual(stored["raw_data"]["request"]["ignore_files"], ["ad.mkv", "ad.mkv"])

    def test_nonfailed_or_bound_requests_cannot_take_retry_path(self) -> None:
        original = self.db.guest_request_queries.get(self.request_id)
        for status, job_id in (("cancelled", None), ("rejected", None), ("pending_review", None), ("submitted", None), ("failed", 123)):
            with self.subTest(status=status, job_id=job_id):
                with patch.object(self.db.guest_request_queries, "get", return_value={**original, "status": status, "job_id": job_id}):
                    result, code = self.approval.approve(self.request_id, {"retry": True}, admin="test")
                self.assertEqual(code, 409, result)
        self.assertEqual(self.db.worker_tasks.get(self.worker_id)["status"], "failed")

    def test_failed_transition_rolls_back_worker_reactivation(self) -> None:
        before_request = self.db.guest_request_queries.get(self.request_id)
        before_worker = self.db.worker_tasks.get(self.worker_id)
        with patch.object(self.db.guest_request_commands, "transition_with_event", side_effect=RuntimeError("event persistence failed")):
            with self.assertRaisesRegex(RuntimeError, "event persistence failed"):
                self.approval.approve(self.request_id, {"retry": True}, admin="test")
        self.assertEqual(self.db.guest_request_queries.get(self.request_id), before_request)
        self.assertEqual(self.db.worker_tasks.get(self.worker_id), before_worker)

    def test_cancellation_after_initial_read_is_rechecked_under_transaction(self) -> None:
        def cancel_then_sanitize(payload):
            self.db.guest_request_commands.update(self.request_id, status="cancelled")
            return _public_request_payload(payload)

        service = RequestApprovalService(replace(self.dependencies, sanitize_request_payload=cancel_then_sanitize))
        result, status = service.approve(self.request_id, {"retry": True}, admin="test")
        self.assertEqual(status, 409, result)
        self.assertEqual(self.db.guest_request_queries.get(self.request_id)["status"], "cancelled")
        self.assertEqual(self.db.worker_tasks.get(self.worker_id)["status"], "failed")

    def test_plain_dispatch_keeps_existing_failed_task_terminal(self) -> None:
        repeated = self.dispatcher.public_import_create(
            guest_request_id=self.request_id, request_token="same-request-token", submit_payload={"title": "Example"},
        )
        self.assertFalse(repeated["created"])
        self.assertEqual(self.db.worker_tasks.get(self.worker_id)["status"], "failed")

    def test_disabled_dispatcher_keeps_request_and_worker_failed(self) -> None:
        self.dispatcher.enabled = lambda: False
        self.assertEqual(self.retry().status_code, 409)
        self.assertEqual(self.db.guest_request_queries.get(self.request_id)["status"], "failed")
        self.assertEqual(self.db.worker_tasks.get(self.worker_id)["status"], "failed")


class FailedPublicRequestRetryContractTests(unittest.TestCase):
    def test_approval_contract_documents_explicit_failed_unbound_retry(self) -> None:
        spec = get_openapi_spec()
        operation = spec["paths"]["/api/admin/requests/{request_id}/approve"]["post"]
        schema = spec["components"]["schemas"]["GuestRequestApprovalRequest"]
        self.assertEqual(operation["security"], [{"cookieAuth": []}])
        self.assertFalse(operation["requestBody"]["required"])
        self.assertFalse(schema["properties"]["retry"]["default"])
        self.assertIn("failed", schema["properties"]["retry"]["description"])
        self.assertIn("job_id", schema["properties"]["retry"]["description"])
        self.assertTrue({"202", "409"}.issubset(operation["responses"]))


if __name__ == "__main__":
    unittest.main()
