"""Response shaping for agent tools.

MCP responses land in a model context window, so tools return a deliberate
projection of the domain row instead of the full database record.
"""

from __future__ import annotations

import json
from typing import Any

from ..media_identity_contract import normalize_media_identity, subscription_import_identity

TEXT_LIMIT = 400


def clip(value: Any, limit: int = TEXT_LIMIT) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pick(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: row[name] for name in fields if name in row and row[name] is not None}


def page_meta(total: int, page: int, per_page: int) -> dict[str, Any]:
    pages = max(1, (int(total) + int(per_page) - 1) // int(per_page)) if int(per_page) > 0 else 1
    return {
        "total": int(total),
        "page": int(page),
        "per_page": int(per_page),
        "has_more": int(page) < pages,
        "pages": pages,
    }


SEARCH_FIELDS = (
    "public_id",
    "title",
    "provider",
    "provider_kind",
    "source_type",
    "source_form",
    "size",
    "size_text",
    "datetime",
    "note",
    "url",
    "share_url",
    "supported",
    "category_suggestion",
    "is_collection",
)
SEARCH_LINK_FIELDS = ("provider_kind", "source_form", "supported", "url", "source_url", "source_url_masked")


def compact_search_item(item: dict[str, Any]) -> dict[str, Any]:
    projected = pick(item, SEARCH_FIELDS)
    link = item.get("link")
    if isinstance(link, dict):
        projected["link"] = pick(link, SEARCH_LINK_FIELDS)
    if "title" in projected:
        projected["title"] = clip(projected["title"], 200)
    return projected


JOB_FIELDS = (
    "id",
    "title",
    "category",
    "category_label",
    "status",
    "source_provider_kind",
    "source_form",
    "target_path",
    "destination_id",
    "external_task_id",
    "error_message",
    "created_at",
    "updated_at",
    "executor_id",
)


TRANSFER_EXECUTION_FIELDS = (
    "phase",
    "strategy",
    "attempts",
    "row_version",
    "staging_verified",
    "final_verified",
    "app_acked",
    "organizer_task_id",
    "receipt",
    "last_error",
)
REVIEW_FIELDS = ("required", "reason", "resume_phase", "can_resume", "can_confirm_not_submitted", "guidance")


def compact_transfer_execution(value: Any) -> dict[str, Any]:
    """Keep the execution subset a caller needs to act on manual review.

    ``row_version`` plus the review block are what ``confirm_job_not_submitted``
    requires; dropping them makes that tool impossible to call correctly.
    """

    if not isinstance(value, dict):
        return {}
    projected = pick(value, TRANSFER_EXECUTION_FIELDS)
    review = value.get("review")
    if isinstance(review, dict):
        review_view = pick(review, REVIEW_FIELDS)
        paths = review.get("paths")
        if isinstance(paths, list):
            review_view["paths"] = [
                {key: item[key] for key in ("label", "namespace", "path") if isinstance(item, dict) and item.get(key)}
                for item in paths[:10]
                if isinstance(item, dict)
            ]
        projected["review"] = review_view
    return projected


def compact_job(row: dict[str, Any]) -> dict[str, Any]:
    projected = pick(row, JOB_FIELDS)
    for name in ("title", "error_message"):
        if name in projected:
            projected[name] = clip(projected[name])
    execution = compact_transfer_execution(row.get("transfer_execution"))
    if execution:
        projected["transfer_execution"] = execution
    if isinstance(row.get("completion"), dict):
        projected["completion"] = row["completion"]
    if row.get("needs_review") is not None:
        projected["needs_review"] = bool(row.get("needs_review"))
    return projected


REQUEST_FIELDS = (
    "id",
    "request_token",
    "job_id",
    "title",
    "category",
    "category_label",
    "source_type",
    "status",
    "public_status",
    "note",
    "created_at",
    "updated_at",
)


def compact_request(row: dict[str, Any]) -> dict[str, Any]:
    projected = pick(row, REQUEST_FIELDS)
    for name in ("title", "note"):
        if name in projected:
            projected[name] = clip(projected[name])
    return projected


ORGANIZER_FIELDS = (
    "id",
    "job_id",
    "request_id",
    "status",
    "category",
    "category_label",
    "title",
    "source_keyword",
    "openlist_root_path",
    "confidence",
    "media_type",
    "tmdb_id",
    "tmdb_title",
    "tmdb_year",
    "error_message",
    "revision",
    "created_at",
    "updated_at",
)


def compact_media_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized = normalize_media_identity(value)
    projected = pick(normalized, ("media_type", "ns", "value", "title", "year", "aliases"))
    if isinstance(value.get("aliases"), list) and len(value["aliases"]) > 50:
        projected["aliases_truncated"] = True
    return projected


def compact_reasons(*values: Any) -> list[str]:
    reasons: list[str] = []
    for value in values:
        candidates = value if isinstance(value, (list, tuple)) else [value]
        for reason in candidates:
            if isinstance(reason, str) and reason.strip():
                text = clip(reason, 500)
                if text not in reasons:
                    reasons.append(text)
                    if len(reasons) == 20:
                        return reasons
    return reasons


def compact_organizer_identity(value: Any) -> dict[str, Any]:
    """Expose identity decisions without manifests, credentials or provider raw data."""
    if not isinstance(value, dict):
        return {}
    manifest = value.get("manifest") if isinstance(value.get("manifest"), dict) else {}
    primary = manifest.get("identity") if isinstance(manifest.get("identity"), dict) else {}
    projected = compact_media_identity({**primary, "aliases": manifest.get("aliases") or []})
    requested = compact_media_identity(value.get("requested_identity"))
    if requested:
        projected["requested_identity"] = requested
    for name in ("scope_root", "canonical_dir"):
        if isinstance(value.get(name), str):
            projected[name] = value[name]
    if value.get("intent") in {"create_or_match", "update_existing"}:
        projected["intent"] = value["intent"]
    if isinstance(value.get("is_new"), bool):
        projected["is_new"] = value["is_new"]
    errors = compact_reasons(value.get("errors"))
    if errors:
        projected["errors"] = errors
    return projected


def compact_organizer_task(row: dict[str, Any]) -> dict[str, Any]:
    """Scalar fields only.

    The nested ``files`` / ``mappings`` / ``operations`` lists are returned by the
    tool as dedicated, paginated fields; copying them here duplicated the same
    payload and wasted context.
    """

    projected = pick(row, ORGANIZER_FIELDS)
    for name in ("title", "tmdb_title", "error_message"):
        if name in projected:
            projected[name] = clip(projected[name])
    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    identity: dict[str, Any] = {}
    identity_errors: list[str] = []
    if isinstance(row.get("mappings"), list):
        # Details must describe the current editable plan, not scan evidence
        # that may predate a manual TMDB correction. Match plan-summary dedup.
        from ..organizer.plan_contract import organizer_identity_context

        contexts = {}
        for mapping in row["mappings"]:
            if not isinstance(mapping, dict):
                continue
            context = organizer_identity_context(mapping)
            if context is not None:
                key = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
                contexts[key] = compact_organizer_identity(context)
        projected["identities_count"] = len(contexts)
        projected["identity_source"] = "mappings"
        if len(contexts) == 1:
            identity = next(iter(contexts.values()))
        identity_errors = compact_reasons(*(context.get("errors") for context in contexts.values()))
    else:
        identity = compact_organizer_identity(evidence.get("organizer_identity") or raw.get("organizer_identity"))
        if identity:
            projected["identity_source"] = "cached"
    if identity.get("ns") and identity.get("value"):
        projected["identity"] = identity
        if identity.get("canonical_dir"):
            projected["canonical_dir"] = identity["canonical_dir"]
        if identity.get("intent"):
            projected["organizer_intent"] = identity["intent"]
    review_reasons = compact_reasons(row.get("review_reasons"), identity_errors, identity.get("errors"))
    if review_reasons:
        projected["review_reasons"] = review_reasons
    if raw.get("agent_review_required") and row.get("status") == "waiting_review":
        projected["requires_confirmation"] = True
    return projected


SUBSCRIPTION_FIELDS = (
    "id",
    "title",
    "category",
    "category_label",
    "media_type",
    "season",
    "year",
    "tmdb_id",
    "schedule_kind",
    "days_of_week",
    "time_of_day",
    "interval_minutes",
    "timezone",
    "next_run_at",
    "last_run_at",
    "last_success_at",
    "next_episode",
    "last_success_episode",
    "source_strategy",
    "auto_import_policy",
    "min_score",
    "status",
    "created_at",
    "updated_at",
)


def compact_subscription(row: dict[str, Any]) -> dict[str, Any]:
    projected = pick(row, SUBSCRIPTION_FIELDS)
    if "title" in projected:
        projected["title"] = clip(projected["title"], 200)
    identity, intent = subscription_import_identity(row)
    if identity:
        projected["identity"] = compact_media_identity(identity)
    projected["organizer_intent"] = intent
    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
    binding = raw.get("organized_binding") if isinstance(raw.get("organized_binding"), dict) else {}
    canonical_dir = row.get("canonical_dir") or binding.get("canonical_dir")
    if isinstance(canonical_dir, str) and canonical_dir:
        projected["canonical_dir"] = canonical_dir
    sources = row.get("sources")
    if isinstance(sources, list):
        projected["sources"] = [
            {
                key: value
                for key, value in (
                    ("type", item.get("type")),
                    ("name", clip(item.get("name"), 120)),
                    ("provider", item.get("provider")),
                    ("priority", item.get("priority")),
                    ("enabled", item.get("enabled")),
                )
                if value is not None
            }
            for item in sources
            if isinstance(item, dict)
        ]
    return projected


EVENT_FIELDS = ("id", "level", "message", "created_at")


def compact_event(row: dict[str, Any]) -> dict[str, Any]:
    projected = pick(row, EVENT_FIELDS)
    if "message" in projected:
        projected["message"] = clip(projected["message"])
    return projected
