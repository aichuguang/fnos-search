from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fnos_media_import.database import Database
from fnos_media_import.domain.models import DestinationDescriptor
from fnos_media_import.planning.source_resolver import SourceResolver
from fnos_media_import.public_web import (
    _public_request_payload,
    _safe_public_cloud139_selection,
    _safe_public_quark_selection,
    _safe_public_string_list,
    _safe_public_tianyi_selection,
)
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.services.import_job_service import (
    PUBLIC_REQUEST_OWNER_KEY,
    ImportJobCreationService,
    V1PlanningError,
    public_request_owner,
)
from fnos_media_import.services.job_cancellation_service import (
    JobCancellationDependencies,
    JobCancellationService,
)
from fnos_media_import.services.public_import_job_coordinator import PublicImportJobCoordinator
from fnos_media_import.services.public_submission_preparation_service import (
    PublicSubmissionPreparationError,
    PublicSubmissionPreparationService,
)
from fnos_media_import.services.public_submission_service import (
    PublicSubmissionDependencies,
    PublicSubmissionService,
)
from fnos_media_import.services.request_approval_service import (
    RequestApprovalDependencies,
    RequestApprovalService,
)
from fnos_media_import.web_input import _limited_text, _public_import_worker_result


class PublicIngressJobGuardsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="public-ingress-guards-")
        self.addCleanup(directory.cleanup)
        database = Database(Path(directory.name) / "requests.db")
        database.init_schema()
        self.db = RepositorySet.from_database(database)
        self.request_id = self.db.guest_request_commands.create({
            "request_token": "current-request", "title": "Example", "category": "movie",
            "source_url": "https://pan.quark.cn/s/example", "status": "submitted",
            "raw_data": {},
        })
        self.submissions = PublicSubmissionService(PublicSubmissionDependencies(
            queries=self.db.guest_request_queries, commands=self.db.guest_request_commands,
            sync_request=lambda value: value, public_status=lambda value: value,
            public_request=lambda value: value,
        ))
        self.cancel = Mock(side_effect=lambda job, **_kwargs: {
            "cancelled": True, "job": {**job, "status": "cancelled"},
        })
        self.start = Mock(return_value=None)

    def coordinator(self, creator) -> PublicImportJobCoordinator:
        return PublicImportJobCoordinator(
            import_service=lambda: SimpleNamespace(create_public_request_import_job=creator),
            submission_service=lambda: self.submissions,
            runtime_revision=lambda: 1, executor_id=lambda: "test", start_rclone=self.start,
            public_status=lambda value: value, safe_result=lambda value: value,
            warn=lambda *_args: None, cancel_unbound_job=self.cancel,
        )

    def execute(self, creator, *, mark_failure: bool = False):
        return self.coordinator(creator).execute_inline(
            guest_request_id=self.request_id, request_token="current-request",
            submit_payload={"url": "https://pan.quark.cn/s/example"}, mark_failure=mark_failure,
        )

    def result(self, owner: str = "current-request") -> dict:
        return {
            "created": False, "success": True, "idempotency_key": "same-resource",
            "job": {
                "id": 42, "status": "transferring", "idempotency_key": "same-resource",
                "raw_data": {PUBLIC_REQUEST_OWNER_KEY: public_request_owner(owner)} if owner else {},
            },
        }

    def cancel_during_creation(self, result):
        def create(_payload, *, request_token):
            self.assertEqual(request_token, "current-request")
            self.db.guest_request_commands.update(self.request_id, status="cancelled")
            return result
        return create

    def test_shared_resource_job_cannot_be_cancelled_by_another_request(self) -> None:
        outcome = self.execute(self.cancel_during_creation(self.result("original-request")))
        self.assertEqual(outcome.bind_outcome, "existing")
        self.cancel.assert_not_called()
        self.start.assert_not_called()

    def test_legacy_job_without_owner_is_not_assumed_owned(self) -> None:
        self.execute(self.cancel_during_creation(self.result("")))
        self.cancel.assert_not_called()
        self.start.assert_not_called()

    def test_same_request_replay_can_compensate_its_unbound_job(self) -> None:
        outcome = self.execute(self.cancel_during_creation(self.result()))
        self.assertEqual(outcome.bind_outcome, "cancelled")
        self.cancel.assert_called_once()
        self.assertEqual(self.cancel.call_args.args[0]["id"], 42)

    def test_shared_job_is_bound_without_retry_or_dispatch(self) -> None:
        result = self.creation_service().create_internal({
            "title": "Example", "url": "https://pan.quark.cn/s/example", "category": "movie",
        }, public_request_token="original-request")
        self.db.job_commands.update(result["job"]["id"], status="transferring")
        result = {**result, "created": False, "job": self.db.job_queries.get(result["job"]["id"])}
        outcome = self.execute(lambda *_args, **_kwargs: result)
        self.assertEqual(outcome.bind_outcome, "bound")
        self.assertEqual(self.db.guest_request_queries.get(self.request_id)["job_id"], result["job"]["id"])
        self.cancel.assert_not_called()
        self.start.assert_not_called()

    def test_structured_planning_failure_marks_request_failed_on_first_attempt(self) -> None:
        outcome = self.execute(lambda *_args, **_kwargs: {
            "success": False, "created": False, "job": None,
            "plan_error": True, "message": "invalid frozen plan",
        })
        self.assertEqual(outcome.bind_outcome, "failed")
        self.assertEqual(self.db.guest_request_queries.get(self.request_id)["status"], "failed")
        self.assertTrue(_public_import_worker_result(outcome)["terminal"])
        self.cancel.assert_not_called()
        self.start.assert_not_called()

    def test_planning_exception_marks_request_failed_before_final_attempt(self) -> None:
        def create(*_args, **_kwargs):
            raise V1PlanningError("missing assignment")
        outcome = self.execute(create)
        self.assertEqual(outcome.bind_outcome, "failed")
        self.assertEqual(self.db.guest_request_queries.get(self.request_id)["status"], "failed")

    def test_transient_creation_failure_is_retried_then_projected_at_exhaustion(self) -> None:
        create = lambda *_args, **_kwargs: {
            "success": False, "job": None, "retryable": True, "message": "queue unavailable",
        }
        outcome = self.execute(create)
        self.assertEqual(_public_import_worker_result(outcome)["worker_outcome"], "retryable")
        self.assertEqual(self.db.guest_request_queries.get(self.request_id)["status"], "submitted")
        outcome = self.execute(create, mark_failure=True)
        self.assertEqual(outcome.bind_outcome, "failed")
        self.assertEqual(self.db.guest_request_queries.get(self.request_id)["status"], "failed")

    def creation_service(self, *, enqueue=None) -> ImportJobCreationService:
        return ImportJobCreationService(
            database=self.db, config=SimpleNamespace(category=lambda _key: {}, raw={}),
            source_resolver=SourceResolver(lambda _url: {"provider_kind": "quark", "source_form": "share"}),
            destination_resolver=SimpleNamespace(
                resolve=lambda *_args, **_kwargs: DestinationDescriptor("target", "quark"),
                resolve_account=lambda *_args, **_kwargs: ("account", 1, 1),
            ),
            source_record_service=SimpleNamespace(capture=lambda source, _record, **_kwargs: replace(
                source, source_record_id="source", source_record_revision=1,
            )),
            plan_import=lambda **_kwargs: {"paths": {"destination_final": {"path": "/target"}}},
            validate_plan=lambda *_args, **_kwargs: None,
            execution_repository=SimpleNamespace(create_with_connection=lambda *_args: None),
            enqueue_transfer_with_connection=enqueue or (lambda *_args: None),
        )

    def test_owner_is_persisted_once_and_cannot_be_overwritten_by_duplicate_payload(self) -> None:
        service = self.creation_service()
        payload = {"title": "Example", "url": "https://pan.quark.cn/s/example", "category": "movie"}
        first = service.create_internal(payload, public_request_token="original-request")
        self.assertTrue(first["success"], first)
        second = service.create_internal(
            {**payload, PUBLIC_REQUEST_OWNER_KEY: public_request_owner("current-request")},
            public_request_token="current-request",
        )
        self.assertFalse(second["created"])
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        stored = self.db.job_queries.get(first["job"]["id"])
        self.assertEqual(stored["raw_data"][PUBLIC_REQUEST_OWNER_KEY], public_request_owner("original-request"))
        self.assertNotIn("original-request", str(stored["raw_data"]))

    def test_untrusted_create_cannot_inject_owner(self) -> None:
        result = self.creation_service().create({
            "title": "Example", "url": "https://pan.quark.cn/s/example", "category": "movie",
            PUBLIC_REQUEST_OWNER_KEY: public_request_owner("current-request"),
            "public_request_token": "current-request",
        })
        self.assertTrue(result["success"], result)
        self.assertNotIn(PUBLIC_REQUEST_OWNER_KEY, result["job"]["raw_data"])

    def test_queue_failure_returns_retryable_and_rolls_back_job(self) -> None:
        def enqueue(*_args):
            raise RuntimeError("queue unavailable")
        result = self.creation_service(enqueue=enqueue).create_internal({
            "title": "Example", "url": "https://pan.quark.cn/s/example", "category": "movie",
        }, public_request_token="current-request")
        self.assertTrue(result["retryable"])
        self.assertFalse(result["success"])
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0], 0)

    def cancellation_service(self):
        self.enqueue_cancel = Mock(return_value={"success": True})
        return JobCancellationService(JobCancellationDependencies(
            job_queries=self.db.job_queries, job_commands=self.db.job_commands,
            cleaner=Mock(), merge_raw_data=lambda a, b: {**(a or {}), **b},
            payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
            cancelled_status="cancelled", enqueue_transfer=self.enqueue_cancel,
        ))

    def owned_job_result(self):
        result = self.creation_service().create_internal({
            "title": "Example", "url": "https://pan.quark.cn/s/example", "category": "movie",
        }, public_request_token="current-request")
        self.db.job_commands.update(result["job"]["id"], status="transferring")
        return {**result, "job": self.db.job_queries.get(result["job"]["id"])}

    def test_compensation_guard_binding_first_retains_shared_job_without_retry(self) -> None:
        result = self.owned_job_result()
        job = result["job"]
        other_id = self.db.guest_request_commands.create({
            "request_token": "other-request", "title": "Other", "category": "movie",
            "source_url": "https://pan.quark.cn/s/example", "status": "submitted",
        })
        service = self.cancellation_service()
        self.cancel = lambda job, **kwargs: service.cancel(
            job, **kwargs, payload={"cleanup": False}, cleanup_default=False,
            stop_running_default=True, admin_username=None, require_unbound=True,
        )

        def create(*_args, **_kwargs):
            bound, _ = self.submissions.bind_import_job(
                other_id, job=job, public_status="transferring", safe_result={},
                rclone_start=None, success=True,
            )
            self.assertEqual(bound, "bound")
            self.db.guest_request_commands.update(self.request_id, status="cancelled")
            return result

        outcome = self.execute(create)
        self.assertEqual(outcome.result["worker_outcome"], "completed")
        self.assertTrue(outcome.result["job_retained_for_bound_request"])
        self.assertFalse(outcome.result["retryable"])
        self.assertEqual(self.db.job_queries.get(job["id"])["status"], "transferring")
        self.assertEqual(self.db.guest_request_queries.get(other_id)["job_id"], job["id"])
        self.assertNotIn("cancel", self.db.job_queries.get(job["id"])["raw_data"])
        self.enqueue_cancel.assert_not_called()

    def test_binding_guard_cancellation_first_rejects_stale_job_snapshot(self) -> None:
        stale_job = self.owned_job_result()["job"]
        cancelled = self.cancellation_service().cancel(
            stale_job, reason="orphan compensation", payload={"cleanup": False}, request_item=None,
            cleanup_default=False, stop_running_default=True, admin_username=None, require_unbound=True,
        )
        self.assertTrue(cancelled["cancelled"])
        outcome, _ = self.submissions.bind_import_job(
            self.request_id, job=stale_job, public_status="transferring", safe_result={},
            rclone_start=None, success=True,
        )
        self.assertEqual(outcome, "job_state_conflict")
        self.assertIsNone(self.db.guest_request_queries.get(self.request_id).get("job_id"))
        self.assertEqual(self.db.job_queries.get(stale_job["id"])["status"], "cancelled")

    def test_binding_guard_rejected_job_is_not_bound_but_failed_job_remains_bindable(self) -> None:
        stale_job = self.owned_job_result()["job"]
        for status, expected in (("rejected", "job_state_conflict"), ("failed", "bound")):
            with self.subTest(status=status):
                self.db.job_commands.update(stale_job["id"], status=status)
                job = self.db.job_queries.get(stale_job["id"])
                outcome, _ = self.submissions.bind_import_job(
                    self.request_id, job=job, public_status=status, safe_result={},
                    rclone_start=None, success=False,
                )
                self.assertEqual(outcome, expected)
        self.assertEqual(self.db.guest_request_queries.get(self.request_id)["status"], "failed")

    def test_binding_guard_conflict_is_projected_as_terminal_request_failure(self) -> None:
        result = self.owned_job_result()
        self.db.job_commands.update(result["job"]["id"], status="cancelled")
        # Another request's cached creation result cannot re-bind the cancelled job.
        result["job"]["raw_data"][PUBLIC_REQUEST_OWNER_KEY] = public_request_owner("other-request")
        outcome = self.execute(lambda *_args, **_kwargs: {**result, "created": False})
        self.assertEqual(outcome.bind_outcome, "failed")
        request = self.db.guest_request_queries.get(self.request_id)
        self.assertEqual(request["status"], "failed")
        self.assertIsNone(request.get("job_id"))
        self.assertTrue(outcome.result["terminal"])
        self.start.assert_not_called()

    def test_compensation_guard_is_trusted_argument_not_admin_payload(self) -> None:
        job = self.owned_job_result()["job"]
        self.submissions.bind_import_job(
            self.request_id, job=job, public_status="transferring", safe_result={},
            rclone_start=None, success=True,
        )
        cancelled = self.cancellation_service().cancel(
            job, reason="explicit administrator cancellation",
            payload={"cleanup": False, "require_unbound": True}, request_item=None,
            cleanup_default=False, stop_running_default=True, admin_username="admin",
        )
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(self.db.job_queries.get(job["id"])["status"], "cancelled")


class PublicSelectionGuardsTests(unittest.TestCase):
    def service(self):
        return PublicSubmissionPreparationService(
            search_cache=lambda _key: None, categories=lambda: {"movie": {}}, category=lambda _key: {},
            limited_text=_limited_text, validate_url=lambda value, _config: value,
            safe_string_list=_safe_public_string_list, safe_quark_selection=_safe_public_quark_selection,
            safe_cloud139_selection=_safe_public_cloud139_selection,
            safe_tianyi_selection=_safe_public_tianyi_selection,
            detect_link=lambda *_args, **_kwargs: SimpleNamespace(provider_kind="tianyi"),
            security_config=lambda: {}, config_int=lambda _config, _key, default: default,
        )

    def prepare(self, extra):
        return self.service().prepare({
            "title": "Example", "url": "https://cloud.189.cn/t/example", "category": "movie", **extra,
        })

    def test_malformed_or_explicitly_empty_selection_is_rejected(self) -> None:
        for field in ("cloud139_selection", "tianyi_selection"):
            for selection in (
                {"mode": "items", "selected_files": []},
                {"mode": "items", "selected_files": [{"name": "missing-id.mkv"}]},
                {"mode": "invalid", "selected_files": [{"fid": "file-1"}]},
                [], "invalid",
            ):
                with self.subTest(field=field, selection=selection):
                    with self.assertRaises(PublicSubmissionPreparationError):
                        self.prepare({field: selection})

    def test_absent_or_default_empty_object_preserves_full_share(self) -> None:
        for extra in ({}, {"cloud139_selection": {}}, {"tianyi_selection": {}}):
            with self.subTest(extra=extra):
                self.assertFalse(self.prepare(extra).scoped_selection)

    def test_valid_file_and_folder_selection_is_preserved(self) -> None:
        for field in ("cloud139_selection", "tianyi_selection"):
            with self.subTest(field=field):
                prepared = self.prepare({field: {
                    "mode": "items", "selected_files": [{"fid": "file-1"}],
                    "selected_folders": [{"fid": "folder-1"}],
                }})
                self.assertTrue(prepared.scoped_selection)
                self.assertEqual(prepared.payload[field]["selected_files"][0]["fid"], "file-1")
                self.assertTrue(prepared.payload[field]["selected_folders"][0]["is_dir"])

    def test_cloud139_episodes_with_same_parent_path_are_not_deduplicated(self) -> None:
        prepared = self.prepare({"cloud139_selection": {
            "mode": "items",
            "selected_files": [
                {"fid": "episode-01", "name": "01.mkv", "path": "show/season-01"},
                {"fid": "episode-02", "name": "02.mkv", "path": "show/season-01"},
            ],
        }})
        rows = prepared.payload["cloud139_selection"]["selected_files"]
        self.assertEqual([row["fid"] for row in rows], ["episode-01", "episode-02"])

    def test_approval_rejects_invalid_saved_provider_selection_without_transition(self) -> None:
        for field in ("cloud139_selection", "tianyi_selection"):
            with self.subTest(field=field):
                commands = Mock()
                coordinate = Mock()
                item = {"id": 1, "status": "pending_review", "raw_data": {
                    "request": {field: {"mode": "items", "selected_files": [{"name": "missing-id"}]}},
                }}
                service = RequestApprovalService(RequestApprovalDependencies(
                    requests=SimpleNamespace(get=lambda _id: item), commands=commands, jobs=Mock(),
                    coordinate_import=coordinate, public_status=lambda value: value,
                    safe_result=lambda value: value, merge_raw_data=lambda a, b: {**(a or {}), **b},
                    sanitize_request_payload=_public_request_payload, category_label=lambda value: value,
                ))
                result, status = service.approve(1, {}, admin="test")
                self.assertEqual(status, 400)
                self.assertFalse(result["success"])
                commands.transition_with_event.assert_not_called()
                coordinate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
