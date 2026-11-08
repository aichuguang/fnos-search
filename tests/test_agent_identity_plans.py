from __future__ import annotations

import copy
import json

import pytest

from fnos_media_import.agent.plans import (
    mapping_action,
    organizer_operation_digest,
    organizer_operation_snapshot,
    organizer_plan_hash,
    organizer_plan_snapshot,
    organizer_plan_summary,
)
from fnos_media_import.organizer.identity import (
    IDENTITY_MANIFEST_NAME,
    Identity,
    build_identity_manifest,
    format_dir_name,
    make_local_identity,
    serialize_identity_manifest,
)
from fnos_media_import.organizer.plan_contract import organizer_plan_hash as core_plan_hash


def task() -> dict:
    identity = Identity("tv", "tmdb", "123", "测试剧", 2026)
    alias = make_local_identity(identity.title, identity.year, identity.media_type).key
    manifest = build_identity_manifest(identity, [alias])
    root = "/移动云/电视剧"
    canonical = root + "/" + format_dir_name(identity)
    source = "/移动云/_入库暂存/电视剧/job-1/01.mkv"
    target = canonical + "/Season 01/测试剧 (2026) - S01E01.mkv"
    context = {
        "version": 1, "scope_root": root, "intent": "create_or_match", "requested_identity": identity.to_dict(),
        "requested_aliases": [list(alias)], "canonical_dir": canonical, "is_new": True,
        "manifest": manifest.to_dict(), "previous_manifest": None, "errors": [],
    }
    return {
        "id": 1, "revision": 3, "status": "waiting_review", "category": "tv", "title": identity.title,
        "openlist_root_path": source.rsplit("/", 1)[0], "updated_at": "2026-11-08T10:00:00Z",
        "mappings": [{
            "id": 10, "source_path": source, "target_path": target, "target_name": target.rsplit("/", 1)[1],
            "status": "ready", "media_type": "tv", "title": identity.title, "year": 2026, "season": 1,
            "episode": 1, "tmdb_id": 123, "raw_data": {"organizer_identity": context, "staging_file": True,
            "parsed": {"has_explicit_season": True}},
        }],
        "operations": [
            {"id": 20, "type": "create_dir", "target_path": canonical, "status": "pending", "raw_data": {"identity_root": True, "allow_create": True}},
            {"id": 21, "type": "write_identity_manifest", "target_path": canonical + "/" + IDENTITY_MANIFEST_NAME,
             "status": "pending", "raw_data": {"content": serialize_identity_manifest(manifest), "previous_content": None}},
            {"id": 22, "type": "create_dir", "target_path": canonical + "/Season 01", "status": "pending"},
            {"id": 23, "type": "move_file", "source_path": source, "target_path": target, "status": "pending",
             "raw_data": {"staging_file": True, "fail_if_target_exists": True, "delete_source_if_target_exists": False}},
        ],
    }


def identity_context(value: dict) -> dict:
    return value["mappings"][0]["raw_data"]["organizer_identity"]


@pytest.mark.parametrize("field,value", [
    ("intent", "update_existing"), ("canonical_dir", "/移动云/电视剧/另一目录 (2026) [tmdb-123]"),
    ("scope_root", "/另一挂载/电视剧"), ("is_new", False),
])
def test_same_revision_identity_write_precondition_changes_plan_hash(field: str, value: object) -> None:
    original = task()
    changed = copy.deepcopy(original)
    identity_context(changed)[field] = value
    assert changed["revision"] == original["revision"]
    assert organizer_plan_hash(changed) != organizer_plan_hash(original)


def test_same_revision_primary_or_alias_change_alters_hash() -> None:
    original = task()
    primary_changed = copy.deepcopy(original)
    identity_context(primary_changed)["manifest"]["identity"]["value"] = "456"
    alias_changed = copy.deepcopy(original)
    identity_context(alias_changed)["manifest"]["aliases"].append(["tv", "imdb", "tt123"])
    assert organizer_plan_hash(primary_changed) != organizer_plan_hash(original)
    assert organizer_plan_hash(alias_changed) != organizer_plan_hash(original)


@pytest.mark.parametrize("field", ["content", "previous_content"])
def test_manifest_operation_payload_changes_hash_without_mapping_or_revision_change(field: str) -> None:
    original = task()
    changed = copy.deepcopy(original)
    manifest = build_identity_manifest(Identity("tv", "tmdb", "123", "测试剧", 2026), [("tv", "imdb", "tt123")])
    changed["operations"][1]["raw_data"][field] = serialize_identity_manifest(manifest)
    assert organizer_plan_hash(changed) != organizer_plan_hash(original)
    assert organizer_operation_digest(changed["operations"]) != organizer_operation_digest(original["operations"])


def test_operation_order_and_write_flags_are_hashed() -> None:
    original = task()
    reordered = copy.deepcopy(original)
    reordered["operations"][1], reordered["operations"][2] = reordered["operations"][2], reordered["operations"][1]
    changed_flag = copy.deepcopy(original)
    changed_flag["operations"][3]["raw_data"]["fail_if_target_exists"] = False
    assert organizer_plan_hash(reordered) != organizer_plan_hash(original)
    assert organizer_plan_hash(changed_flag) != organizer_plan_hash(original)


def test_revision_remains_an_independent_hash_guard() -> None:
    original = task()
    changed = copy.deepcopy(original)
    changed["revision"] += 1
    assert organizer_plan_hash(changed) != organizer_plan_hash(original)
    assert organizer_operation_digest(changed["operations"]) == organizer_operation_digest(original["operations"])


def test_run_progress_and_rebuilt_operation_row_ids_do_not_change_hash() -> None:
    original = task()
    changed = copy.deepcopy(original)
    changed.update(status="executing", updated_at="later", run_id=999, progress={"done": 1})
    changed["mappings"][0]["updated_at"] = "later"
    for index, operation in enumerate(changed["operations"]):
        operation.update(id=100 + index, mapping_id=10, run_id=999, status="done", updated_at="later", undo_data={"anything": True})
        operation.setdefault("raw_data", {}).update(updated_at="later", progress={"done": 1}, attempts=2)
    assert organizer_plan_hash(changed) == organizer_plan_hash(original)
    assert organizer_operation_digest(changed["operations"]) == organizer_operation_digest(original["operations"])


def test_manifest_json_whitespace_and_alias_order_are_not_semantic_changes() -> None:
    original = task()
    context = identity_context(original)
    context["manifest"]["aliases"].append(["tv", "imdb", "tt123"])
    context["requested_aliases"].append(["tv", "imdb", "tt123"])
    original["operations"][1]["raw_data"]["content"] = json.dumps(context["manifest"], ensure_ascii=False)
    changed = copy.deepcopy(original)
    identity_context(changed)["manifest"]["aliases"].reverse()
    identity_context(changed)["requested_aliases"].reverse()
    changed["operations"][1]["raw_data"]["content"] = json.dumps(identity_context(changed)["manifest"], ensure_ascii=True, sort_keys=True, indent=4)
    assert organizer_plan_hash(changed) == organizer_plan_hash(original)


def test_operation_snapshot_has_content_digests_not_manifest_bodies() -> None:
    snapshot = organizer_operation_snapshot(task()["operations"])
    manifest_raw = snapshot[1]["raw_data"]
    assert "content" not in manifest_raw and "previous_content" not in manifest_raw
    assert len(manifest_raw["content_sha256"]) == 64
    assert manifest_raw["previous_content_sha256"] is None


def test_summary_exposes_identity_and_prerequisite_counts_without_raw_payloads() -> None:
    source = task()
    summary = organizer_plan_summary(source)
    assert summary["create_work_root_count"] == 1
    assert summary["create_season_dir_count"] == 1
    assert summary["manifest_write_count"] == 1
    assert summary["identity_count"] == 1 and summary["added_alias_count"] == 1
    assert summary["identities"][0]["identity"]["value"] == "123"
    assert summary["identities"][0]["intent"] == "create_or_match"
    assert summary["targets_total"] == 1 and summary["targets_truncated"] is False
    assert "raw_data" not in json.dumps(summary)
    assert summary["plan_hash"] == core_plan_hash(source)


def test_summary_added_aliases_exclude_existing_primary_and_aliases() -> None:
    source = task()
    context = identity_context(source)
    context["previous_manifest"] = copy.deepcopy(context["manifest"])
    context["manifest"]["aliases"].append(["tv", "imdb", "tt123"])
    summary = organizer_plan_summary(source)
    assert summary["added_alias_count"] == 1
    assert summary["identities"][0]["added_aliases"] == [["tv", "imdb", "tt123"]]


def test_summary_samples_targets_with_explicit_truncation_and_total() -> None:
    source = task()
    original_mapping = source["mappings"][0]
    source["mappings"] = [{**copy.deepcopy(original_mapping), "id": index + 1} for index in range(35)]
    summary = organizer_plan_summary(source)
    assert len(summary["targets"]) == 30
    assert summary["targets_total"] == 35 and summary["targets_truncated"] is True
    assert summary["identity_count"] == 1


@pytest.mark.parametrize("status", ["delete_ad", "skipped_existing"])
def test_old_destructive_mapping_states_are_warnings_not_allowed_actions(status: str) -> None:
    source = task()
    source["mappings"][0]["status"] = status
    summary = organizer_plan_summary(source)
    assert summary["delete_count"] == 0 and summary["move_count"] == 0
    assert summary["invalid_status_count"] == 1 and summary["blocked_count"] == 1
    assert summary["targets"][0]["action"] == "invalid"
    assert summary["warnings"]


def test_blocked_mapping_is_not_advertised_as_a_move() -> None:
    source = task()
    source["mappings"][0]["status"] = "need_edit"
    assert mapping_action(source["mappings"][0]) == "blocked"
    assert organizer_plan_summary(source)["move_count"] == 0


def test_snapshot_keeps_identity_errors_and_episode_evidence() -> None:
    source = task()
    changed = copy.deepcopy(source)
    changed["mappings"][0]["raw_data"]["parsed"]["has_explicit_season"] = False
    assert organizer_plan_hash(changed) != organizer_plan_hash(source)
    assert organizer_plan_snapshot(source)["mappings"][0]["organizer_identity"]["manifest"] == identity_context(source)["manifest"]
