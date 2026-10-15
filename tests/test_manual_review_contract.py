from __future__ import annotations

import json
import unittest
from functools import wraps
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, jsonify, session

from fnos_media_import.blueprints.jobs import JobsRouteContext, create_jobs_blueprint
from fnos_media_import.openapi import get_openapi_spec
from fnos_media_import.services.job_admin_query_service import (
    JobAdminQueryDependencies,
    JobAdminQueryService,
    serialize_transfer_execution,
)
from fnos_media_import.services.rclone_admin_service import RcloneAdminCommandDependencies, RcloneAdminCommandService
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.transfer.execution_repository import ExecutionConflictError, TransferExecutionRepository


def _execution(**changes):
    values = {
        "phase": "manual_review", "strategy": "native_save", "row_version": 7,
        "receipt": None, "last_error_message": "Submission outcome unknown",
        "snapshot": {
            "review_from": "native_submitting",
            "submission_fence": {
                "state": "prepared", "phase": "native_submitting", "operation_key": "private-operation-key",
            },
        },
    }
    values.update(changes)
    return SimpleNamespace(**values)


class ManualReviewViewContractTests(unittest.TestCase):
    def test_unknown_submission_has_explicit_confirmation_not_ordinary_retry(self):
        result = serialize_transfer_execution(_execution())
        self.assertEqual(result["row_version"], 7)
        review = result["review"]
        self.assertTrue(review["required"])
        self.assertTrue(review["can_confirm_not_submitted"])
        self.assertFalse(review["can_resume"])
        self.assertEqual(review["resume_phase"], "native_submitting")
        self.assertEqual(review["reason"], "Submission outcome unknown")
        self.assertNotIn("private-operation-key", json.dumps(result))

    def test_known_receipt_only_offers_resume(self):
        result = serialize_transfer_execution(_execution(receipt={"status": "running", "operation_id": "private-id"}))
        self.assertTrue(result["review"]["can_resume"])
        self.assertFalse(result["review"]["can_confirm_not_submitted"])

    def test_confirmation_is_hidden_for_verified_stale_or_active_executions(self):
        for changes in ({"staging_verified": True}, {"final_verified": True}, {"app_acked": True},
                        {"row_version": 0}, {"phase": "completed"}):
            with self.subTest(changes=changes):
                review = serialize_transfer_execution(_execution(**changes))["review"]
                self.assertFalse(review["can_confirm_not_submitted"])
        for claim in ("malformed", {"lease_until": "invalid"},
                      {"owner_id": "worker", "lease_until": "2999-01-01T00:00:00+00:00"},
                      {"owner_id": "worker", "lease_until": "invalid"}):
            with self.subTest(claim=claim):
                execution = _execution()
                execution.snapshot["claim"] = claim
                review = serialize_transfer_execution(execution)["review"]
                self.assertFalse(review["can_confirm_not_submitted"])
                self.assertFalse(review["can_resume"])

    def test_unknown_fence_never_falls_back_to_planning_resume(self):
        for phase in (None, "planning", "completed", "retry_wait", "invalid"):
            with self.subTest(phase=phase):
                execution = _execution()
                execution.snapshot["review_from"] = phase
                review = serialize_transfer_execution(execution)["review"]
                self.assertFalse(review["can_resume"])
                self.assertFalse(review["can_confirm_not_submitted"])

    def test_missing_poll_receipt_is_not_advertised_as_resumable(self):
        for phase in ("native_waiting", "source_waiting_visible"):
            review = serialize_transfer_execution(_execution(snapshot={"review_from": phase}))["review"]
            self.assertFalse(review["can_resume"])
            self.assertFalse(review["can_confirm_not_submitted"])

    def test_detail_uses_frozen_paths_and_terminal_business_state_wins(self):
        job = {"id": 14, "status": "review"}
        service = JobAdminQueryService(JobAdminQueryDependencies(
            jobs=SimpleNamespace(), load_detail=lambda _id: dict(job),
            reconcile=lambda item, _reason: item, decorate=lambda item: item,
            load_transfer_execution=lambda _id: _execution(),
            load_transfer_plan=lambda _id: {"paths": {
                "source_landing": {"path": "/landing/job-14", "namespace": "openlist", "password": "secret"},
                "destination_final": {"path": "/movies", "namespace": "provider:mobile"},
                "local_temp": {"path": "/data/relay/job-14", "namespace": "local"},
            }},
        ))
        body, status = service.detail(14)
        self.assertEqual(status, 200)
        review = body["job"]["transfer_execution"]["review"]
        self.assertEqual([row["path"] for row in review["paths"]], ["/landing/job-14", "/movies", "/data/relay/job-14"])
        self.assertNotIn("secret", json.dumps(review))
        self.assertTrue(review["can_confirm_not_submitted"])
        job["status"] = "cancelled"
        review = service.detail(14)[0]["job"]["transfer_execution"]["review"]
        self.assertFalse(review["can_confirm_not_submitted"])
        self.assertFalse(review["can_resume"])

    def test_ordinary_recovery_cannot_bypass_fence_by_changing_resume_phase(self):
        repository = object.__new__(TransferExecutionRepository)
        execution = _execution()
        repository.get = Mock(return_value=execution)
        for phase in (None, "planning", "target_verifying", "ack_pending"):
            with self.subTest(phase=phase):
                with self.assertRaises(ExecutionConflictError):
                    repository.recover_manual_review(14, resume_phase=phase)


class ManualReviewApiContractTests(unittest.TestCase):
    def test_review_endpoint_is_post_only_and_admin_protected(self):
        app = Flask(__name__)
        app.secret_key = "isolated-route-test"
        calls = []

        def protected(handler):
            @wraps(handler)
            def wrapped(*args, **kwargs):
                if not session.get("admin_username"):
                    return jsonify({"success": False}), 401
                return handler(*args, **kwargs)
            return wrapped

        def handler(**kwargs):
            calls.append(kwargs)
            return jsonify({"success": True})

        app.register_blueprint(create_jobs_blueprint(JobsRouteContext(
            admin_required=protected,
            handlers={name: handler for name in (
                "admin_jobs", "admin_job_detail", "admin_job_retry", "admin_job_review",
                "admin_job_cancel", "admin_job_delete", "admin_jobs_batch_retry",
            )},
        )))
        client = app.test_client()
        path = "/api/admin/jobs/14/review"
        self.assertEqual(client.get(path).status_code, 405)
        self.assertEqual(client.post(path, json={}).status_code, 401)
        self.assertEqual(calls, [])
        with client.session_transaction() as state:
            state["admin_username"] = "administrator"
        self.assertEqual(client.post(path, json={}).status_code, 200)
        self.assertEqual(calls, [{"job_id": 14}])

    def test_review_openapi_requires_explicit_versioned_confirmation(self):
        spec = get_openapi_spec()
        operation = spec["paths"]["/api/admin/jobs/{job_id}/review"]["post"]
        self.assertEqual(operation["security"], [{"cookieAuth": []}])
        schema = spec["components"]["schemas"]["TransferManualReviewRequest"]
        self.assertEqual(set(schema["required"]), {"action", "confirmed", "expected_row_version", "note"})
        self.assertEqual(schema["properties"]["confirmed"]["enum"], [True])
        self.assertEqual(schema["properties"]["expected_row_version"]["minimum"], 1)


class RcloneTaskEntryContractTests(unittest.TestCase):
    def test_legacy_admin_start_never_calls_global_runner(self):
        runner = Mock()
        service = RcloneAdminCommandService(RcloneAdminCommandDependencies(rclone=runner))
        for payload in ({}, {"job_id": 14, "staging_run": {"path": "/untrusted"}}):
            result = service.start(payload)
            self.assertFalse(result["success"])
            self.assertEqual(result["_http_status"], 409)
            self.assertEqual(result["code"], "RCLONE_JOB_REQUIRED")
            runner.start.assert_not_called()

    def test_worker_global_scan_guard_keeps_safety_and_returns_actionable_chinese(self):
        runner = object.__new__(RcloneService)
        runner.config = {"enabled": True}
        runner.enabled = True
        runner._append_log = Mock()
        runner.status = Mock(return_value={"running": False})
        result = runner.start()
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "RCLONE_JOB_REQUIRED")
        self.assertIn("入库任务", result["message"])
        self.assertNotIn("global rclone", result["message"])


if __name__ == "__main__":
    unittest.main()
