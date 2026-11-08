from __future__ import annotations

from types import MethodType

import pytest

from fnos_media_import.database import Database
from fnos_media_import.organizer.openlist_client import OpenListClient
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.services.organizer_admin_service import OrganizerAdminCommandDependencies, OrganizerAdminCommandService
from fnos_media_import.services.worker_task_dispatcher import WorkerTaskDispatcher
from tests import test_organizer_identity_service as identity_fixtures


@pytest.fixture
def authority_case():
    case = identity_fixtures.OrganizerIdentityApplyTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def _dispatcher(repo) -> WorkerTaskDispatcher:
    return WorkerTaskDispatcher(repository=repo.worker_tasks, enabled=lambda: True, config_revision=lambda: 1)


def _restart_service(case, job_id):
    service, _task = identity_fixtures._plan_service(case.cloud, job_id=job_id)
    # Reopen the real SQLite file and create a new Organizer instance, retaining
    # only the fake remote surface and immutable V1 binding supplied by fixtures.
    reopened = Database(case.database.path)
    service.db = RepositorySet.from_database(reopened)
    service.owner_id = "agent-preview-after-restart"
    service._background_suspended = False
    service._refresh_openlist_strm_for_task = lambda *_args: {}
    service._refresh_fnos_if_needed = lambda *_args: False
    case.cloud.scan_videos = MethodType(OpenListClient.scan_videos, case.cloud)
    return service


def test_request_agent_rescan_persists_review_hold_and_revokes_old_approval(authority_case) -> None:
    case = authority_case
    service, _job_id, task_id = case.create_task()
    before = case.repo.organizer.get_organizer_task(task_id)
    case.cloud.reads.clear()

    result = service.request_agent_rescan(task_id)

    assert result["success"] is True
    assert result["executed"] is False
    after = case.repo.organizer.get_organizer_task(task_id)
    assert after["status"] == "waiting_review"
    assert after["revision"] == before["revision"] + 1
    assert after["raw_data"]["agent_review_required"] is True
    for field in ("manual_approval", "execution_approval", "agent_confirmation"):
        assert field not in after["evidence"]
    assert after["mappings"] == before["mappings"]
    assert after["operations"] == before["operations"]
    assert case.cloud.mutations == []
    assert case.cloud.reads == []


def test_delayed_auto_apply_worker_after_restart_stays_in_review(authority_case) -> None:
    case = authority_case
    service, job_id, task_id = case.create_task()
    # A high-confidence, already approved task is the dangerous case: without
    # the persisted hold it normally flows from process_task directly to apply.
    assert service.request_agent_rescan(task_id)["success"]
    restarted = _restart_service(case, job_id)
    case.cloud.mutations.clear()

    result = restarted.process_task(task_id, auto_apply=True)

    assert result["success"], result
    assert result["status"] == "waiting_review"
    assert result["summary"]["auto_applicable"] is False
    after = case.repo.organizer.get_organizer_task(task_id)
    assert after["raw_data"]["agent_review_required"] is True
    assert after["evidence"]["agent_review_required"] is True
    assert after["status"] == "waiting_review"
    assert all(operation["status"] == "pending" for operation in after["operations"])
    assert case.cloud.mutations == []
    assert case.cloud.exists(f"/incoming/job-{job_id}/Example.S01E01.mkv")
    assert not case.cloud.exists(identity_fixtures.CANONICAL_ROOT)


def test_admin_rescan_only_uses_distinct_preview_queue_without_applying(authority_case) -> None:
    case = authority_case
    service, _job_id, task_id = case.create_task()
    dispatcher = _dispatcher(case.repo)
    old_auto = dispatcher.organizer_process(task_id, auto_apply=True)
    admin = OrganizerAdminCommandService(OrganizerAdminCommandDependencies(organizer=service, worker_dispatcher=dispatcher))
    case.cloud.mutations.clear()
    case.cloud.reads.clear()

    result, status = admin.rescan_only(task_id)

    assert status == 200
    assert result["retry_mode"] == "rescan_only"
    assert result["executed"] is False and result["requires_confirmation"] is True
    assert result["worker_task_id"] != old_auto["worker_task_id"]
    preview = case.repo.worker_tasks.get(result["worker_task_id"])
    automatic = case.repo.worker_tasks.get(old_auto["worker_task_id"])
    assert preview["idempotency_key"] == f"organizer-preview:{task_id}"
    assert automatic["idempotency_key"] == f"organizer-process:{task_id}"
    assert preview["payload"]["auto_apply"] is False
    assert preview["payload"]["respect_schedule"] is False
    assert automatic["payload"]["auto_apply"] is True
    assert case.repo.organizer.get_organizer_task(task_id)["raw_data"]["agent_review_required"] is True
    assert case.cloud.mutations == []
    assert case.cloud.reads == []


def test_preview_dispatch_is_idempotent_but_never_reuses_automatic_task(authority_case) -> None:
    dispatcher = _dispatcher(authority_case.repo)
    automatic = dispatcher.organizer_process(99, auto_apply=True)
    first = dispatcher.organizer_process(99, auto_apply=False, respect_schedule=False)
    repeated = dispatcher.organizer_process(99, auto_apply=False, respect_schedule=False)
    assert first["worker_task_id"] != automatic["worker_task_id"]
    assert repeated["worker_task_id"] == first["worker_task_id"]
    assert first["created"] is True and repeated["created"] is False


def test_partially_executed_real_journal_cannot_be_replaced_by_agent_rescan(authority_case) -> None:
    case = authority_case
    service, _job_id, task_id = case.create_task()
    case.cloud.fail_manifest_write = True
    applied = service.apply_task_from_worker(task_id)
    assert applied["success"] is False
    before = case.repo.organizer.get_organizer_task(task_id)
    assert any(operation["status"] != "pending" for operation in before["operations"])
    case.cloud.mutations.clear()
    case.cloud.reads.clear()

    result = service.request_agent_rescan(task_id)

    assert result["success"] is False
    assert result["code"] == "EXECUTION_CONFIRMATION_REQUIRED"
    assert case.repo.organizer.get_organizer_task(task_id) == before
    assert case.cloud.mutations == []
    assert case.cloud.reads == []


@pytest.mark.parametrize("rollback_status", ["running", "failed"])
def test_unfinished_persisted_rollback_rejects_rescan_without_writes(authority_case, rollback_status: str) -> None:
    case = authority_case
    service, _job_id, task_id = case.create_task()
    task = case.repo.organizer.get_organizer_task(task_id)
    # Model a crash/restart by persisting the actual recovery marker directly;
    # all operations remain pending, so this specifically covers rollback guard.
    evidence = {**task["evidence"], "rollback": {"status": rollback_status, "run_id": 8, "rollback_run_id": 9}}
    case.repo.organizer.update_organizer_task(task_id, status="waiting_review", evidence=evidence)
    before = case.repo.organizer.get_organizer_task(task_id)
    assert all(operation["status"] == "pending" for operation in before["operations"])
    dispatcher = _dispatcher(case.repo)
    admin = OrganizerAdminCommandService(OrganizerAdminCommandDependencies(organizer=service, worker_dispatcher=dispatcher))
    case.cloud.mutations.clear()
    case.cloud.reads.clear()

    result, status = admin.rescan_only(task_id)

    assert status == 409
    assert result["code"] == "EXECUTION_CONFIRMATION_REQUIRED"
    assert case.repo.organizer.get_organizer_task(task_id) == before
    with case.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM worker_tasks").fetchone()[0] == 0
    assert case.cloud.mutations == []
    assert case.cloud.reads == []
