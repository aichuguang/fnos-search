from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fnos_media_import.agent.errors import AgentError
from fnos_media_import.agent.projection import compact_job, compact_organizer_task, compact_subscription
from fnos_media_import.agent.registry import ToolRegistry
from fnos_media_import.agent.tools.organizer import _get_task, register


def call_context(task: dict) -> SimpleNamespace:
    return SimpleNamespace(services=SimpleNamespace(organizer_task_detail=Mock(return_value=({"success": True, "task": task}, 200))))


def paged_task(count: int) -> dict:
    return {
        "id": 7, "status": "waiting_review", "revision": 3,
        "files": [{"id": index, "path": f"/staging/{index}.mkv", "name": f"{index}.mkv", "parent_path": "/staging", "ext": ".mkv", "season": 1, "episode": index} for index in range(1, count + 1)],
        "mappings": [{"id": index, "file_id": index, "source_path": f"/staging/{index}.mkv", "target_path": f"/library/{index}.mkv", "status": "need_edit", "reason": ["需要确认身份"]} for index in range(1, count + 1)],
    }


def identity_context(path: str) -> dict:
    return {
        "version": 1, "scope_root": "/library", "canonical_dir": path,
        "intent": "update_existing", "is_new": False, "errors": ["目录身份需复核"],
        "requested_identity": {"media_type": "tv", "ns": "imdb", "value": "tt456", "title": "Example", "year": 2026},
        "manifest": {
            "version": 1,
            "identity": {"media_type": "tv", "ns": "tmdb", "value": "123", "title": "Example", "year": 2026},
            "aliases": [["tv", "imdb", "tt456"]],
        },
        "previous_manifest": {"secret": "TOPSECRET"}, "token": "TOPSECRET",
    }


def test_detail_collections_have_independent_real_pages_past_old_limits() -> None:
    task = paged_task(230)
    context = call_context(task)
    result = _get_task(context, {"task_id": 7, "files_page": 3, "mappings_page": 5, "blocked_page": 4})

    assert [row["id"] for row in result["files"]] == list(range(101, 151))
    assert [row["id"] for row in result["mappings"]] == list(range(201, 231))
    assert [row["mapping_id"] for row in result["blocked"]] == list(range(151, 201))
    assert result["files_pagination"] == {"total": 230, "page": 3, "per_page": 50, "has_more": True, "pages": 5}
    assert result["mappings_pagination"]["has_more"] is False
    assert result["blocked_pagination"]["has_more"] is True
    assert result["blocked_count"] == 230
    assert result["plan_summary"]["mapping_count"] == 230
    assert result["plan_summary"]["plan_hash"]
    first = _get_task(context, {"task_id": 7})
    assert first["plan_summary"]["plan_hash"] == result["plan_summary"]["plan_hash"]
    assert len(first["files"]) == len(first["mappings"]) == len(first["blocked"]) == 50


def test_pagination_bounds_and_empty_pages_are_truthful() -> None:
    context = call_context(paged_task(120))
    result = _get_task(context, {"task_id": 7, "per_page": 1000, "files_page": 9})
    assert result["files"] == []
    assert result["files_pagination"]["total"] == 120
    assert result["files_pagination"]["has_more"] is False
    assert result["files_pagination"]["per_page"] == 100
    assert len(result["mappings"]) == 100
    empty = _get_task(call_context(paged_task(0)), {"task_id": 7})
    assert empty["files_pagination"]["pages"] == 1
    assert empty["blocked_pagination"]["total"] == 0


def test_file_mapping_and_task_paths_are_exact_and_raw_data_is_not_exposed() -> None:
    long_path = "/library/" + "long folder  " * 50 + "Example (2026) [tmdb-123]"
    identity = identity_context(long_path)
    task = paged_task(1)
    task["openlist_root_path"] = long_path
    task["evidence"] = {"organizer_identity": identity, "provider_token": "TOPSECRET"}
    task["raw_data"] = {"password": "TOPSECRET"}
    task["files"][0].update({"path": long_path + "/original.mkv", "parent_path": long_path, "size": 1024, "raw_data": {"token": "TOPSECRET"}})
    task["mappings"][0].update({
        "source_path": long_path + "/original.mkv", "target_path": long_path + "/Season 00/Example - S00E01.srt",
        "target_name": "Example - S00E01.srt", "media_type": "tv", "tmdb_id": 123, "season": 0, "episode": 1,
        "raw_data": {"companion_file": True, "organizer_identity": identity, "parsed": {"review_reasons": ["文件季号与目录冲突"], "has_explicit_season": True}, "ai": {"prompt": "TOPSECRET"}},
    })

    result = _get_task(call_context(task), {"task_id": 7})

    assert result["task"]["openlist_root_path"] == long_path
    assert result["task"]["canonical_dir"] == long_path
    assert result["task"]["organizer_intent"] == "update_existing"
    assert result["files"][0]["path"] == task["files"][0]["path"]
    assert result["files"][0]["parent_path"] == long_path
    assert result["files"][0]["name"] == "1.mkv"
    assert result["files"][0]["ext"] == ".mkv"
    mapping = result["mappings"][0]
    assert mapping["target_path"] == task["mappings"][0]["target_path"]
    assert mapping["source_path"] == task["mappings"][0]["source_path"]
    assert (mapping["tmdb_id"], mapping["season"], mapping["episode"]) == (123, 0, 1)
    assert mapping["companion_file"] is True
    assert mapping["identity"]["aliases"] == [["tv", "imdb", "tt456"]]
    assert mapping["identity"]["canonical_dir"] == long_path
    assert "文件季号与目录冲突" in mapping["review_reasons"]
    assert "目录身份需复核" in mapping["review_reasons"]
    assert "raw_data" not in mapping and "raw_data" not in result["files"][0]
    assert "TOPSECRET" not in json.dumps(result, ensure_ascii=False)
    assert "改为 ready" not in result["next"]


def test_compact_subscription_exposes_binding_and_only_controlled_identity_fields() -> None:
    path = "/library/" + "Very Long Title " * 50 + "[tmdb-123]"
    row = {
        "id": 8, "title": "Example", "media_type": "tv", "category": "tv", "year": "2026",
        "identity_ns": "tmdb", "identity_value": "123", "canonical_dir": path,
        "raw_data": {"media_identity": {"media_type": "tv", "ns": "tmdb", "value": "123", "aliases": [["tv", "imdb", "tt456"]], "poster_url": "TOPSECRET", "overview": "TOPSECRET"}, "password": "TOPSECRET"},
    }
    result = compact_subscription(row)
    assert result["canonical_dir"] == path
    assert result["organizer_intent"] == "update_existing"
    assert result["identity"]["value"] == "123"
    assert result["identity"]["aliases"] == [["tv", "imdb", "tt456"]]
    assert "TOPSECRET" not in json.dumps(result, ensure_ascii=False)
    assert compact_job({"id": 9, "target_path": path})["target_path"] == path


def test_compact_task_can_read_identity_from_raw_without_exposing_manifest() -> None:
    result = compact_organizer_task({"id": 9, "raw_data": {"organizer_identity": identity_context("/library/Example [tmdb-123]")}})
    assert result["identity"]["ns"] == "tmdb"
    assert result["identity"]["requested_identity"]["ns"] == "imdb"
    assert "manifest" not in result["identity"]
    assert "previous_manifest" not in result["identity"]


def test_invalid_legacy_mapping_states_are_visible_in_blocked_page() -> None:
    task = paged_task(3)
    for row, status in zip(task["mappings"], ["ready", "delete_ad", "skipped_existing"]):
        row["status"] = status
    result = _get_task(call_context(task), {"task_id": 7})
    assert result["blocked_count"] == 2
    assert [row["status"] for row in result["blocked"]] == ["delete_ad", "skipped_existing"]
    assert result["legacy_delete_count"] == 1


def test_schema_advertises_pagination_without_new_tools() -> None:
    registry = ToolRegistry()
    register(registry)
    assert {tool.name for tool in registry.all()} == {"list_organizer_tasks", "get_organizer_task"}
    properties = registry.get("get_organizer_task").input_schema["properties"]
    for field in ("files_page", "mappings_page", "blocked_page"):
        assert properties[field]["default"] == 1
    assert properties["per_page"]["default"] == 50
    assert properties["per_page"]["maximum"] == 100


def test_missing_task_returns_not_found_without_projection_failure() -> None:
    context = call_context({})
    context.services.organizer_task_detail.return_value = ({"success": False}, 404)
    with pytest.raises(AgentError):
        _get_task(context, {"task_id": 7})


def test_current_mapping_identity_wins_over_stale_task_evidence() -> None:
    old = identity_context("/library/Example [tmdb-123]")
    current = identity_context("/library/Corrected [tmdb-789]")
    current["manifest"]["identity"].update({"value": "789", "title": "Corrected"})
    task = {"id": 7, "evidence": {"organizer_identity": old}, "mappings": [{"id": 1, "status": "ready", "raw_data": {"organizer_identity": current}}]}
    result = _get_task(call_context(task), {"task_id": 7})
    assert result["task"]["identity"]["value"] == "789"
    assert result["task"]["canonical_dir"] == result["plan_summary"]["identities"][0]["canonical_dir"]
    assert result["task"]["identity_source"] == "mappings"


def test_multiple_or_missing_mapping_identities_never_fall_back_to_cached_single_identity() -> None:
    first = identity_context("/library/Example [tmdb-123]")
    second = identity_context("/library/Second [tmdb-456]")
    second["manifest"]["identity"].update({"value": "456", "title": "Second"})
    mappings = [{"raw_data": {"organizer_identity": context}} for context in (first, second)]
    result = compact_organizer_task({"evidence": {"organizer_identity": first}, "mappings": mappings})
    assert result["identities_count"] == 2
    assert "identity" not in result and "canonical_dir" not in result
    empty = compact_organizer_task({"evidence": {"organizer_identity": first}, "mappings": []})
    assert empty["identities_count"] == 0 and "identity" not in empty
    cached = compact_organizer_task({"evidence": {"organizer_identity": first}})
    assert cached["identity_source"] == "cached"


def test_agent_review_hold_only_requests_confirmation_while_waiting_for_review() -> None:
    raw = {"agent_review_required": True}
    assert compact_organizer_task({"status": "waiting_review", "raw_data": raw})["requires_confirmation"] is True
    for status in ("done", "cancelled", "manual_confirmed", "executing"):
        assert "requires_confirmation" not in compact_organizer_task({"status": status, "raw_data": raw})
