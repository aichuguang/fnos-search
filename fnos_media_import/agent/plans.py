"""Bounded, explicit review views for the shared Organizer plan contract."""

from __future__ import annotations

import json
import re
from typing import Any

from ..organizer.plan_contract import (
    organizer_identity_context,
    organizer_operation_digest,
    organizer_operation_snapshot,
    organizer_plan_hash,
    organizer_plan_snapshot,
)

_SAMPLE_TARGETS = 30
_SAMPLE_ALIASES = 20
_INVALID_MAPPING_STATUSES = frozenset({"delete_ad", "skipped_existing"})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _basename(value: Any) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").split("/")[-1]


def _dirname(value: Any) -> str:
    parts = str(value or "").replace("\\", "/").rstrip("/").split("/")
    return "/".join(parts[:-1])


def mapping_action(row: dict[str, Any]) -> str:
    """Review classification; old delete states are invalid, not executable."""
    status = str(row.get("status") or "").strip().lower()
    if status in _INVALID_MAPPING_STATUSES:
        return "invalid"
    if status == "skipped":
        return "skip"
    if status != "ready":
        return "blocked"
    source_path = str(row.get("source_path") or "")
    target_path = str(row.get("target_path") or "")
    if not target_path or source_path == target_path:
        return "keep"
    if _dirname(source_path) != _dirname(target_path):
        return "move"
    if _basename(source_path) != _basename(target_path):
        return "rename"
    return "keep"


def _identity_summaries(mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mapping in mappings:
        context = organizer_identity_context(mapping)
        if context is None:
            continue
        manifest = context.get("manifest")
        valid = isinstance(manifest, dict) and "identity" in manifest and "invalid_manifest" not in manifest
        primary = manifest.get("identity") if valid else None
        aliases = (manifest.get("aliases") or []) if valid else []
        previous = context.get("previous_manifest")
        previous_aliases = (previous.get("aliases") or []) if isinstance(previous, dict) else []
        previous_keys = {_json(alias) for alias in previous_aliases}
        if isinstance(previous, dict) and isinstance(previous.get("identity"), dict):
            identity = previous["identity"]
            previous_keys.add(_json([identity.get("media_type"), identity.get("ns"), identity.get("value")]))
        added_aliases = [alias for alias in aliases if _json(alias) not in previous_keys]
        errors = context.get("errors") or []
        errors = errors if isinstance(errors, list) else [errors]
        summary = {
            "canonical_dir": context.get("canonical_dir"),
            "intent": context.get("intent"),
            "is_new": bool(context.get("is_new")),
            "identity": primary,
            "aliases": aliases[:_SAMPLE_ALIASES],
            "aliases_total": len(aliases),
            "aliases_truncated": len(aliases) > _SAMPLE_ALIASES,
            "added_aliases": added_aliases[:_SAMPLE_ALIASES],
            "added_aliases_total": len(added_aliases),
            "added_aliases_truncated": len(added_aliases) > _SAMPLE_ALIASES,
            "manifest_valid": valid,
            "errors": [" ".join(str(error).split())[:400] for error in errors[:10]],
            "errors_total": len(errors),
            "errors_truncated": len(errors) > 10,
        }
        key = _json(context)
        if key not in seen:
            seen.add(key)
            summaries.append(summary)
    return summaries


def organizer_plan_summary(task: dict[str, Any]) -> dict[str, Any]:
    """Small user-review view: identities, prerequisites, and sampled targets."""
    mappings = [row for row in (task.get("mappings") or []) if isinstance(row, dict)]
    operations = [row for row in (task.get("operations") or []) if isinstance(row, dict)]
    actions = [mapping_action(row) for row in mappings]
    identities = _identity_summaries(mappings)
    roots = {identity["canonical_dir"] for identity in identities if identity.get("canonical_dir")}
    legacy_delete_count = sum(row.get("status") == "delete_ad" for row in mappings)
    invalid_status_count = actions.count("invalid")
    invalid_operations = sum(op.get("type") in {"delete_file", "delete_duplicate_file"} for op in operations)
    warnings = []
    if invalid_status_count or invalid_operations:
        warnings.append("计划包含旧版删除/重复源清理状态，新身份整理禁止执行；请重新识别或修正映射。")
    missing_identity_count = sum(
        action in {"move", "rename", "keep"} and organizer_identity_context(row) is None
        for row, action in zip(mappings, actions)
    )
    if missing_identity_count:
        warnings.append("部分待整理文件缺少作品身份计划，必须重新识别，不能沿用旧计划入库。")
    return {
        "task_id": task.get("id"),
        "title": task.get("title"),
        "category": task.get("category"),
        "status": task.get("status"),
        "confidence": task.get("confidence"),
        "openlist_root_path": task.get("openlist_root_path"),
        "revision": task.get("revision"),
        "mapping_count": len(mappings),
        "rename_count": actions.count("rename"),
        "move_count": actions.count("move"),
        "delete_count": 0,
        "legacy_delete_count": legacy_delete_count,
        "invalid_status_count": invalid_status_count,
        "invalid_operation_count": invalid_operations,
        "skip_count": actions.count("skip"),
        "blocked_count": actions.count("blocked") + invalid_status_count,
        "missing_identity_count": missing_identity_count,
        "operation_count": len(operations),
        "create_work_root_count": sum(op.get("type") == "create_dir" and op.get("target_path") in roots for op in operations),
        "create_season_dir_count": sum(op.get("type") == "create_dir" and _dirname(op.get("target_path")) in roots and bool(re.fullmatch(r"Season \d{2}", _basename(op.get("target_path")))) for op in operations),
        "manifest_write_count": sum(op.get("type") == "write_identity_manifest" for op in operations),
        "identity_count": len(identities),
        "added_alias_count": sum(item["added_aliases_total"] for item in identities),
        "identities": identities[:_SAMPLE_TARGETS],
        "identities_total": len(identities),
        "identities_truncated": len(identities) > _SAMPLE_TARGETS,
        "warnings": warnings,
        "plan_hash": organizer_plan_hash(task),
        "operation_digest": organizer_operation_digest(task.get("operations") or []),
        "targets_total": len(mappings),
        "targets_truncated": len(mappings) > _SAMPLE_TARGETS,
        "targets": [
            {
                "mapping_id": row.get("id"),
                "status": row.get("status"),
                "action": actions[index],
                "source_path": row.get("source_path"),
                "target_path": row.get("target_path"),
                "target_name": row.get("target_name"),
                "media_type": row.get("media_type"),
                "season": row.get("season"),
                "episode": row.get("episode"),
                "companion_file": bool((row.get("raw_data") or {}).get("companion_file")) if isinstance(row.get("raw_data"), dict) else False,
                "confidence": row.get("confidence"),
            }
            for index, row in enumerate(mappings[:_SAMPLE_TARGETS])
        ],
    }


__all__ = [
    "organizer_plan_hash", "organizer_plan_snapshot", "organizer_plan_summary",
    "organizer_operation_snapshot", "organizer_operation_digest",
]
