from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from fnos_media_import.database import Database
from fnos_media_import.organizer.identity import parse_identity_manifest, serialize_identity_manifest, build_identity_manifest
from fnos_media_import.organizer.plan_contract import organizer_operation_digest, organizer_plan_hash
from fnos_media_import.repositories import RepositorySet
from tests.test_organizer_identity_service import CANONICAL_ROOT, _IdentityCloud, _build, _plan_service


@dataclass
class ConfirmationCase:
    database: Database
    repo: RepositorySet
    cloud: _IdentityCloud
    service: Any
    task_id: int
    job_id: int

    def snapshot(self) -> dict[str, Any]:
        return self.repo.organizer.get_organizer_task(self.task_id)

    def approve(self, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        snapshot = self.snapshot() if snapshot is None else snapshot
        return self.service.approve_task(
            self.task_id, expected_revision=snapshot["revision"], expected_plan_hash=organizer_plan_hash(snapshot),
        )

    def mutate_manifest_without_revision(self) -> None:
        snapshot = self.snapshot()
        operation = next(op for op in snapshot["operations"] if op["type"] == "write_identity_manifest")
        raw = copy.deepcopy(operation["raw_data"])
        manifest = parse_identity_manifest(raw["content"])
        raw["content"] = serialize_identity_manifest(build_identity_manifest(manifest.identity, [*manifest.aliases, ("tv", "imdb", "tt123")]))
        # This is the intentional race/tamper injection. It does not touch the
        # final service guard, the repository guard, or the task revision.
        with self.database.connect() as connection:
            connection.execute("UPDATE organizer_operations SET raw_data = ? WHERE id = ?", (json.dumps(raw, ensure_ascii=False), operation["id"]))
        assert self.snapshot()["revision"] == snapshot["revision"]

    def reorder_operations_without_revision(self) -> None:
        snapshot = self.snapshot()
        operations = copy.deepcopy(snapshot["operations"])
        manifest_index = next(index for index, op in enumerate(operations) if op["type"] == "write_identity_manifest")
        operations[manifest_index], operations[manifest_index + 1] = operations[manifest_index + 1], operations[manifest_index]
        self.repo.organizer.replace_organizer_operations(self.task_id, operations)
        assert self.snapshot()["revision"] == snapshot["revision"]


@pytest.fixture
def confirmation_case(tmp_path) -> ConfirmationCase:
    database = Database(tmp_path / "agent-confirmation.db")
    database.init_schema()
    repo = RepositorySet.from_database(database)
    cloud = _IdentityCloud()
    job_id, _ = repo.job_commands.create({
        "title": "Example (2026)", "category": "tv", "category_label": "TV", "status": "waiting_organizer",
        "source_provider_kind": "quark", "source_form": "share", "source_url": "source-record:agent-confirmation-test",
        "destination_id": "native-main", "plan_version": 1, "idempotency_key": "agent-confirmation-1",
    })
    service, task_fixture = _plan_service(cloud, job_id=job_id)
    repo.job_commands.update(job_id, raw_data=service.db.job["raw_data"])
    service.db = repo
    service.owner_id = "agent-confirmation-test"
    # External media-refresh services are outside this local fixture. Real
    # approval, atomic transactions, locks, leases and write guards stay active.
    service._refresh_openlist_strm_for_task = lambda *_args: {}
    service._refresh_fnos_if_needed = lambda *_args: False
    task_id = repo.organizer.create_organizer_task(
        job_id=job_id, category="tv", title="Example (2026)", source_keyword="Example (2026)",
        openlist_root_path=task_fixture["openlist_root_path"], status="waiting_review", raw_data=task_fixture["raw_data"],
    )
    task = repo.organizer.get_organizer_task(task_id)
    files, mappings, operations, summary = _build(service, task, cloud, ["Example.S01E01.mkv"])
    assert summary["auto_applicable"], summary
    assert repo.organizer.replace_organizer_plan(task_id, files=files, mappings=mappings, operations=operations)
    repo.organizer.update_organizer_task(task_id, confidence=summary["confidence"], evidence=summary["evidence"])
    assert cloud.mutations == []
    return ConfirmationCase(database, repo, cloud, service, task_id, job_id)


def test_matching_plan_hash_approves_and_applies_with_real_sqlite(confirmation_case: ConfirmationCase) -> None:
    case = confirmation_case
    before = case.snapshot()
    approved = case.approve(before)
    assert approved["success"], approved
    after = case.snapshot()
    confirmation = after["evidence"]["agent_confirmation"]
    assert confirmation["plan_hash"] == organizer_plan_hash(before)
    assert confirmation["revision"] == after["revision"] == before["revision"] + 1
    assert confirmation["operations_digest"] == organizer_operation_digest(after["operations"])
    assert case.cloud.mutations == []

    applied = case.service.apply_task_from_worker(case.task_id)
    assert applied["success"], applied
    assert case.snapshot()["status"] == "done"
    assert case.cloud.exists(CANONICAL_ROOT + "/Season 01/Example (2026) - S01E01.mkv")
    assert [call[0] for call in case.cloud.mutations].index("write_identity_manifest") < [call[0] for call in case.cloud.mutations].index("move")


@pytest.mark.parametrize("mutation", ["mutate_manifest_without_revision", "reorder_operations_without_revision"])
def test_stale_hash_rejects_same_revision_manifest_or_order_change(confirmation_case: ConfirmationCase, mutation: str) -> None:
    case = confirmation_case
    reviewed = case.snapshot()
    getattr(case, mutation)()
    result = case.approve(reviewed)
    assert result["success"] is False and result["code"] == "PLAN_CHANGED", result
    assert case.snapshot()["status"] == "waiting_review"
    assert case.snapshot()["revision"] == reviewed["revision"]
    assert "agent_confirmation" not in case.snapshot()["evidence"]
    assert case.cloud.mutations == []


def test_atomic_repository_rechecks_hash_after_service_snapshot_race(confirmation_case: ConfirmationCase) -> None:
    case = confirmation_case
    reviewed = case.snapshot()
    build_operations = case.service._operations_for_mappings
    injected = []

    def inject_after_read(*args, **kwargs):
        operations = build_operations(*args, **kwargs)
        # Approval already read and checked its reviewed task. Change only the
        # persisted operations before its genuine BEGIN IMMEDIATE transaction.
        case.mutate_manifest_without_revision()
        injected.append(True)
        return operations

    with patch.object(case.service, "_operations_for_mappings", side_effect=inject_after_read):
        result = case.approve(reviewed)
    assert injected == [True]
    assert result["success"] is False and result["code"] == "PLAN_CHANGED", result
    current = case.snapshot()
    assert current["revision"] == reviewed["revision"] and current["status"] == "waiting_review"
    assert "agent_confirmation" not in current["evidence"]
    assert organizer_plan_hash(current) != organizer_plan_hash(reviewed)
    assert case.cloud.mutations == []


def test_repository_rejects_stale_hash_before_atomic_approval_writes(confirmation_case: ConfirmationCase) -> None:
    case = confirmation_case
    reviewed = case.snapshot()
    case.mutate_manifest_without_revision()
    changed = case.repo.organizer.update_organizer_mappings_and_plan(
        case.task_id, mapping_updates=[], operations=reviewed["operations"], evidence=reviewed["evidence"],
        expected_status=reviewed["status"], expected_revision=reviewed["revision"],
        task_updates={"status": "manual_confirmed"}, expected_plan_hash=organizer_plan_hash(reviewed),
    )
    assert changed is False
    assert case.snapshot()["status"] == reviewed["status"]
    assert case.snapshot()["revision"] == reviewed["revision"]
    assert organizer_plan_hash(case.snapshot()) != organizer_plan_hash(reviewed)
    assert case.cloud.mutations == []


@pytest.mark.parametrize("mutation", ["mutate_manifest_without_revision", "reorder_operations_without_revision"])
def test_apply_rechecks_confirmed_operations_after_approval(confirmation_case: ConfirmationCase, mutation: str) -> None:
    case = confirmation_case
    result = case.approve()
    assert result["success"], result
    approved_revision = case.snapshot()["revision"]
    getattr(case, mutation)()
    assert case.snapshot()["revision"] == approved_revision

    applied = case.service.apply_task_from_worker(case.task_id)
    assert applied["success"] is False, applied
    assert "Agent 确认后的操作计划已变化" in applied["message"], applied
    assert case.cloud.mutations == []
    assert case.cloud.exists(f"/incoming/job-{case.job_id}/Example.S01E01.mkv")
