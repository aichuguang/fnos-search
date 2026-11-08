from __future__ import annotations

import copy
from types import MethodType, SimpleNamespace
from unittest.mock import patch

import pytest

from fnos_media_import.organizer.openlist_client import OpenListClient
from fnos_media_import.organizer.plan_contract import organizer_plan_hash
from fnos_media_import.services.organizer_admin_service import OrganizerAdminCommandDependencies, OrganizerAdminCommandService
from fnos_media_import.services.worker_task_dispatcher import WorkerTaskDispatcher
from tests import test_organizer_identity_service as identity_fixtures


@pytest.fixture
def autonomous_case():
    case = identity_fixtures.OrganizerIdentityApplyTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def admin_for(case, service):
    dispatcher = WorkerTaskDispatcher(repository=case.repo.worker_tasks, enabled=lambda: True, config_revision=lambda: 1)
    return OrganizerAdminCommandService(OrganizerAdminCommandDependencies(organizer=service, worker_dispatcher=dispatcher))


def enable_real_scan(case, service) -> None:
    service._background_suspended = False
    case.cloud.scan_videos = MethodType(OpenListClient.scan_videos, case.cloud)


def run_process_queue(case, service, response):
    worker = case.repo.worker_tasks.get(response["worker_task_id"])
    assert worker["task_type"] == "organizer_process"
    return service.process_task(worker["payload"]["task_id"], auto_apply=worker["payload"]["auto_apply"])


def fail_after_first_video(case):
    service, job_id, task_id = case.create_task(["Example.S01E01.mkv", "Example.S01E02.mkv"])
    service.organizer_config["bulk_operations_enabled"] = False
    real_move = case.cloud.move

    def fail_second(path, target_dir, **kwargs):
        if "S01E02" in path:
            raise RuntimeError("Simulated second video move failure")
        return real_move(path, target_dir, **kwargs)

    with patch.object(case.cloud, "move", side_effect=fail_second):
        result = service.apply_task_from_worker(task_id)
    assert result["success"] is False, result
    task = case.repo.organizer.get_organizer_task(task_id)
    video_ops = [operation for operation in task["operations"] if operation["type"] == "move_file"]
    assert [operation["status"] for operation in video_ops] == ["done", "failed"]
    assert case.cloud.exists(identity_fixtures.CANONICAL_ROOT + "/Season 01/Example (2026) - S01E01.mkv")
    return service, job_id, task_id, result


def test_operator_hold_then_authorized_retry_automatically_applies_high_confidence(autonomous_case) -> None:
    case = autonomous_case
    service, job_id, task_id = case.create_task()
    enable_real_scan(case, service)
    admin = admin_for(case, service)

    preview, status = admin.rescan_only(task_id)
    assert status == 200
    assert case.repo.worker_tasks.get(preview["worker_task_id"])["payload"]["auto_apply"] is False
    scanned = run_process_queue(case, service, preview)
    assert scanned["success"] and scanned["status"] == "waiting_review", scanned
    held = case.repo.organizer.get_organizer_task(task_id)
    assert held["raw_data"]["agent_review_required"] is True
    assert held["evidence"]["agent_review_required"] is True
    assert case.cloud.mutations == []

    authorized, status = admin.retry_authorized(task_id)
    assert status == 200, authorized
    queued = case.repo.worker_tasks.get(authorized["worker_task_id"])
    assert queued["payload"]["auto_apply"] is True
    assert queued["payload"]["respect_schedule"] is False
    released = case.repo.organizer.get_organizer_task(task_id)
    assert not released["raw_data"].get("agent_review_required")
    assert not released["evidence"].get("agent_review_required")

    applied = run_process_queue(case, service, authorized)
    assert applied["success"], applied
    assert case.repo.organizer.get_organizer_task(task_id)["status"] == "done"
    assert case.cloud.exists(identity_fixtures.CANONICAL_ROOT + "/Season 01/Example (2026) - S01E01.mkv")
    assert not case.cloud.exists(f"/incoming/job-{job_id}/Example.S01E01.mkv")


def test_authorized_partial_retry_uses_current_hash_and_preserves_completed_journal(autonomous_case) -> None:
    case = autonomous_case
    service, _job_id, task_id, first = fail_after_first_video(case)
    before = case.repo.organizer.get_organizer_task(task_id)
    old_run = case.repo.organizer.get_organizer_run(first["run_id"])
    completed = next(operation for operation in before["operations"] if operation["type"] == "move_file" and operation["status"] == "done")
    admin = admin_for(case, service)
    case.cloud.mutations.clear()

    with patch.object(service, "approve_task", wraps=service.approve_task) as approve, patch.object(service, "request_agent_rescan", wraps=service.request_agent_rescan) as rescan:
        result, status = admin.retry_authorized(task_id)
    assert status == 200 and result["retry_mode"] == "resume_apply", result
    approve.assert_called_once_with(task_id, expected_revision=before["revision"], expected_plan_hash=organizer_plan_hash(before))
    rescan.assert_not_called()
    queued = case.repo.worker_tasks.get(result["worker_task_id"])
    assert queued["task_type"] == "organizer_apply"
    approved = case.repo.organizer.get_organizer_task(task_id)
    preserved = next(operation for operation in approved["operations"] if operation["source_path"] == completed["source_path"] and operation["type"] == "move_file")
    for key in ("status", "run_id", "undo_data", "target_path"):
        assert preserved[key] == completed[key]
    assert case.repo.organizer.get_organizer_run(first["run_id"]) == old_run
    assert case.cloud.mutations == []

    applied = service.apply_task_from_worker(task_id)
    assert applied["success"], applied
    assert case.repo.organizer.get_organizer_task(task_id)["status"] == "done"
    assert len([mutation for mutation in case.cloud.mutations if mutation[0] == "move"]) == 1
    assert case.cloud.exists(identity_fixtures.CANONICAL_ROOT + "/Season 01/Example (2026) - S01E02.mkv")


def test_authorized_rescan_does_not_release_title_identity_conflicts(autonomous_case) -> None:
    case = autonomous_case
    service, job_id, task_id = case.create_task()
    enable_real_scan(case, service)
    # The source content changed to another show, while the explicit request
    # still names Example. Full execution authority does not change identity rules.
    case.cloud.files.clear()
    case.cloud.seed_video(f"/incoming/job-{job_id}/NCIS.S01E01.mkv")
    admin = admin_for(case, service)
    result, status = admin.retry_authorized(task_id)
    assert status == 200, result
    scanned = run_process_queue(case, service, result)
    assert scanned["success"] and scanned["status"] == "waiting_review", scanned
    task = case.repo.organizer.get_organizer_task(task_id)
    assert task["mappings"][0]["status"] == "need_edit"
    assert not task["raw_data"].get("agent_review_required")
    assert case.cloud.mutations == []


def test_authorized_partial_retry_still_rejects_out_of_scope_mapping(autonomous_case) -> None:
    case = autonomous_case
    service, _job_id, task_id, _first = fail_after_first_video(case)
    task = case.repo.organizer.get_organizer_task(task_id)
    failed = next(operation for operation in task["operations"] if operation["type"] == "move_file" and operation["status"] == "failed")
    mapping = next(mapping for mapping in task["mappings"] if mapping["source_path"] == failed["source_path"])
    case.repo.organizer.update_organizer_mapping(mapping["id"], target_path="/unrelated/Example - S01E02.mkv")
    before = case.repo.organizer.get_organizer_task(task_id)
    case.cloud.mutations.clear()

    result, status = admin_for(case, service).retry_authorized(task_id)
    assert status == 409 and result["success"] is False, result
    assert case.repo.organizer.get_organizer_task(task_id)["operations"] == before["operations"]
    with case.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM worker_tasks").fetchone()[0] == 0
    assert case.cloud.mutations == []


def test_even_authorized_rescan_cannot_replace_partially_executed_plan(autonomous_case) -> None:
    case = autonomous_case
    service, _job_id, task_id, _first = fail_after_first_video(case)
    before = case.repo.organizer.get_organizer_task(task_id)
    case.cloud.mutations.clear()
    result = service.request_agent_rescan(task_id, require_review=False)
    assert result["success"] is False and result["code"] == "EXECUTION_CONFIRMATION_REQUIRED", result
    assert case.repo.organizer.get_organizer_task(task_id) == before
    assert case.cloud.mutations == []


@pytest.mark.parametrize("rollback_status", ["running", "failed"])
def test_authorized_rescan_cannot_discard_unfinished_rollback(autonomous_case, rollback_status) -> None:
    case = autonomous_case
    service, _job_id, task_id = case.create_task()
    before = case.repo.organizer.get_organizer_task(task_id)
    evidence = {**before["evidence"], "rollback": {"status": rollback_status, "run_id": 12, "rollback_run_id": 13}}
    case.repo.organizer.update_organizer_task(task_id, status="waiting_review", evidence=evidence)
    before = case.repo.organizer.get_organizer_task(task_id)

    result = service.request_agent_rescan(task_id, require_review=False)
    assert result["success"] is False and result["code"] == "EXECUTION_CONFIRMATION_REQUIRED", result
    assert case.repo.organizer.get_organizer_task(task_id) == before
    assert case.cloud.mutations == []


def test_authorized_retry_resumes_the_actual_failed_rollback(autonomous_case) -> None:
    case = autonomous_case
    service, job_id, task_id = case.create_task()
    applied = service.apply_task_from_worker(task_id)
    assert applied["success"], applied
    source_run_id = applied["run_id"]
    with patch.object(case.cloud, "move", side_effect=RuntimeError("Simulated rollback move failure")):
        failed = service.rollback_run(source_run_id)
    assert failed["success"] is False, failed
    before = case.repo.organizer.get_organizer_task(task_id)
    assert before["evidence"]["rollback"]["run_id"] == source_run_id
    assert before["evidence"]["rollback"]["status"] == "failed"

    with patch.object(service, "rollback_run", wraps=service.rollback_run) as rollback, patch.object(service, "request_agent_rescan", wraps=service.request_agent_rescan) as rescan:
        resumed, status = admin_for(case, service).retry_authorized(task_id)
    assert status == 200 and resumed["success"], resumed
    assert resumed["retry_mode"] == "resume_rollback"
    rollback.assert_called_once_with(source_run_id)
    rescan.assert_not_called()
    assert case.repo.organizer.get_organizer_run(source_run_id)["summary"]["rollback"]["status"] == "completed"
    assert case.cloud.exists(f"/incoming/job-{job_id}/Example.S01E01.mkv")


def test_full_authorized_apply_executes_valid_local_review_plan_in_one_approval(autonomous_case) -> None:
    case = autonomous_case
    service, job_id, task_id = case.create_task()
    enable_real_scan(case, service)
    job = case.repo.job_queries.get(job_id)
    raw = copy.deepcopy(job["raw_data"])
    raw["request"].pop("media_identity", None)
    case.repo.job_commands.update(job_id, raw_data=raw)
    service.tmdb = SimpleNamespace(configured=False, search=lambda *_args, **_kwargs: [])
    service.organizer_config["auto_apply_confidence"] = 100
    assert service.request_agent_rescan(task_id, require_review=False)["success"]

    scanned = service.process_task(task_id, auto_apply=True)
    assert scanned["success"] and scanned["status"] == "waiting_review", scanned
    current = case.repo.organizer.get_organizer_task(task_id)
    assert all(mapping["status"] == "ready" for mapping in current["mappings"])
    context = current["mappings"][0]["raw_data"]["organizer_identity"]
    assert context["manifest"]["identity"]["ns"] == "local"
    assert not scanned["summary"]["auto_applicable"]
    assert case.cloud.mutations == []

    # Full-authority Agent has already assessed the plan against user rules.
    # One authorized backend apply snapshots/approves and enqueues it; no extra
    # human-confirmation round trip or weakening of ready/identity guards.
    admin = admin_for(case, service)
    with patch.object(service, "approve_task", wraps=service.approve_task) as approve:
        result, status = admin.apply(task_id, expected_revision=current["revision"], expected_plan_hash=organizer_plan_hash(current))
    assert status == 200 and result["success"], result
    approve.assert_called_once_with(task_id, expected_revision=current["revision"], expected_plan_hash=organizer_plan_hash(current))
    assert case.repo.worker_tasks.get(result["worker_task_id"])["task_type"] == "organizer_apply"
    applied = service.apply_task_from_worker(task_id)
    assert applied["success"], applied
    assert case.repo.organizer.get_organizer_task(task_id)["status"] == "done"
    assert case.cloud.exists(context["canonical_dir"] + "/Season 01/Example (2026) - S01E01.mkv")


def test_full_authorized_apply_does_not_promote_need_edit_to_ready(autonomous_case) -> None:
    case = autonomous_case
    service, _job_id, task_id = case.create_task()
    mapping = case.repo.organizer.get_organizer_task(task_id)["mappings"][0]
    case.repo.organizer.update_organizer_mapping(mapping["id"], status="need_edit", reason=["Unresolved episode ambiguity"])
    current = case.repo.organizer.get_organizer_task(task_id)

    result, status = admin_for(case, service).apply(task_id, expected_revision=current["revision"], expected_plan_hash=organizer_plan_hash(current))
    assert status == 409 and result["success"] is False, result
    assert case.repo.organizer.get_organizer_task(task_id)["mappings"][0]["status"] == "need_edit"
    with case.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM worker_tasks").fetchone()[0] == 0
    assert case.cloud.mutations == []
