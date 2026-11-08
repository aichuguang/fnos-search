"""Pure, shared fingerprints for reviewed Organizer write semantics.

Database-generated operation IDs and execution progress are excluded: approval
may rebuild operation rows without changing the ordered write plan. Revision is
still part of the full plan fingerprint as an independent concurrency guard.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .identity import identity_from_mapping, normalize_identity_key, parse_identity_manifest


_MAPPING_FIELDS = (
    "id", "source_path", "target_path", "target_name", "status", "media_type",
    "title", "year", "season", "episode", "tmdb_id",
)
_IDENTITY_CONTEXT_FIELDS = (
    "version", "scope_root", "intent", "requested_identity", "requested_aliases",
    "canonical_dir", "is_new", "manifest", "previous_manifest", "errors",
)
_OPERATION_PROGRESS_FIELDS = frozenset({
    "id", "run_id", "updated_at", "created_at", "started_at", "finished_at",
    "status", "progress", "attempts", "last_error", "error_message", "undo_data",
})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any, *, length: int = 32) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:length]


def _normalized_manifest(value: Any) -> Any:
    if value is None:
        return None
    try:
        return parse_identity_manifest(value).to_dict()
    except (TypeError, ValueError):
        # An invalid legacy plan still needs a distinct fingerprint. Never turn
        # all malformed payloads into the same empty identity or content hash.
        return {"invalid_manifest": value}


def _normalized_identity(value: Any) -> Any:
    try:
        return identity_from_mapping(value).to_dict()
    except (TypeError, ValueError, AttributeError):
        return value


def _normalized_aliases(value: Any) -> Any:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        return value
    try:
        return [list(key) for key in sorted({normalize_identity_key(key) for key in value})]
    except (TypeError, ValueError):
        return value


def organizer_identity_context(mapping: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize just the identity decision and its write preconditions."""
    raw = mapping.get("raw_data")
    value = raw.get("organizer_identity") if isinstance(raw, dict) else None
    if not isinstance(value, dict):
        return None
    result = {name: value.get(name) for name in _IDENTITY_CONTEXT_FIELDS}
    result["requested_identity"] = _normalized_identity(result["requested_identity"])
    result["requested_aliases"] = _normalized_aliases(result["requested_aliases"])
    for name in ("manifest", "previous_manifest"):
        result[name] = _normalized_manifest(result[name])
    return result


def organizer_operation_snapshot(operations: Any) -> list[dict[str, Any]]:
    """Canonical ordered write semantics, independent of operation-row IDs."""
    if not isinstance(operations, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            result.append({"invalid_operation": operation})
            continue
        raw = operation.get("raw_data")
        semantic_raw = {
            key: value for key, value in (raw.items() if isinstance(raw, dict) else ())
            if key not in _OPERATION_PROGRESS_FIELDS and key not in {"content", "previous_content"}
        }
        if operation.get("type") == "write_identity_manifest":
            source = raw if isinstance(raw, dict) else {}
            for name in ("content", "previous_content"):
                value = source.get(name)
                semantic_raw[name + "_sha256"] = _digest(_normalized_manifest(value), length=64) if value is not None else None
        elif isinstance(raw, dict):
            for name in ("content", "previous_content"):
                if name in raw:
                    semantic_raw[name + "_sha256"] = _digest(raw[name], length=64)
        result.append({
            "type": operation.get("type"),
            "source_path": operation.get("source_path") or "",
            "target_path": operation.get("target_path") or "",
            "raw_data": semantic_raw,
        })
    return result


def organizer_operation_digest(operations: Any) -> str:
    """Operation-only guard for equivalent approval-time operation rebuilds."""
    return _digest(organizer_operation_snapshot(operations))


def organizer_plan_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    mappings = []
    for row in task.get("mappings") or []:
        if not isinstance(row, dict):
            continue
        mapping = {name: row.get(name) for name in _MAPPING_FIELDS}
        mapping["organizer_identity"] = organizer_identity_context(row)
        raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
        parsed = raw.get("parsed") if isinstance(raw.get("parsed"), dict) else {}
        mapping["write_preconditions"] = {
            "companion_file": bool(raw.get("companion_file")),
            "staging_file": bool(raw.get("staging_file")),
            "manual_episode_override": bool(raw.get("manual_episode_override")),
            "has_explicit_season": bool(parsed.get("has_explicit_season")),
        }
        mappings.append(mapping)
    return {
        "task_id": task.get("id"),
        "revision": task.get("revision"),
        "category": task.get("category"),
        "openlist_root_path": task.get("openlist_root_path"),
        "mappings": mappings,
        "operations": organizer_operation_snapshot(task.get("operations") or []),
    }


def organizer_plan_hash(task: dict[str, Any]) -> str:
    return _digest(organizer_plan_snapshot(task))


__all__ = [
    "organizer_identity_context", "organizer_plan_hash", "organizer_plan_snapshot",
    "organizer_operation_snapshot", "organizer_operation_digest",
]
