"""Deprecated compatibility facade for the former database domain mixin.

The persistence implementation now lives in dedicated repositories.  This
module intentionally contains no database logic; it keeps historical imports
working and forwards inherited method calls to repository instances installed
on the database object.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from ..time_utils import utc_now_iso, utc_now_iso_offset
from .organizer_repository import OrganizerRepository
from .rclone_repository import (
    RcloneRepository,
    _callback_path_is_same_or_child,
    _job_matches_staging_callback_paths,
    _job_owns_staging_directory,
    _match_score,
    _normalize_callback_path,
    _normalize_match_text,
    _rclone_category_match_values,
    _rclone_job_id_from_paths,
)
from .update_repository import (
    HISTORY_CLEANUP_TABLES,
    HISTORY_PRESERVED_TABLES,
    UpdateRepository,
    _hash_text,
    _json_text,
)
from .update_run_repository import UpdateRunRepository
from .update_subscription_query_repository import UpdateSubscriptionQueryRepository


def utc_now() -> str:
    return utc_now_iso()


def utc_minutes_from_now(minutes: int) -> str:
    return utc_now_iso_offset(minutes=minutes)


def utc_seconds_from_now(seconds: int) -> str:
    return utc_now_iso_offset(seconds=seconds)


def _decode_json_fields(item: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    for field in fields:
        if item.get(field):
            try:
                item[field] = json.loads(item[field])
            except (TypeError, json.JSONDecodeError):
                pass
    return item


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _decode_update_subscription(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = _row_to_dict(row)
    return _decode_json_fields(
        item,
        (
            "aliases",
            "days_of_week",
            "missing_episodes",
            "include_keywords",
            "exclude_keywords",
            "raw_data",
        ),
    ) if item else None


def _decode_update_source(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = _row_to_dict(row)
    return _decode_json_fields(item, ("options",)) if item else None


def _decode_update_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = _row_to_dict(row)
    return _decode_json_fields(item, ("summary", "raw_data", "run_log")) if item else None


def _decode_update_candidate(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = _row_to_dict(row)
    return _decode_json_fields(item, ("raw_data",)) if item else None


def _missing_backup(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise AttributeError("旧式 DatabaseDomainMixin 子类未实现 backup_database")


def _compat_repository(instance: Any, repository_attribute: str) -> Any:
    repository = getattr(instance, repository_attribute, None)
    if repository is not None:
        return repository

    connection_factory = getattr(instance, "connect", None)
    if not callable(connection_factory):
        raise AttributeError(
            f"{type(instance).__name__} 未实现兼容仓储所需的 connect()"
        )

    if repository_attribute == "rclone":
        row_decoder = getattr(instance, "row_to_dict", None)
        repository = RcloneRepository(
            connection_factory,
            row_decoder if callable(row_decoder) else _row_to_dict,
        )
    elif repository_attribute == "organizer":
        repository = OrganizerRepository(connection_factory)
    elif repository_attribute == "update":
        subscription_decoder = getattr(instance, "_decode_update_subscription", None)
        source_decoder = getattr(instance, "_decode_update_source", None)
        run_decoder = getattr(instance, "_decode_update_run", None)
        candidate_decoder = getattr(instance, "_decode_update_candidate", None)
        subscription_queries = getattr(instance, "update_subscription_queries", None)
        if subscription_queries is None:
            subscription_queries = UpdateSubscriptionQueryRepository(
                connection_factory,
                subscription_decoder if callable(subscription_decoder) else _decode_update_subscription,
                source_decoder if callable(source_decoder) else _decode_update_source,
            )
            setattr(instance, "update_subscription_queries", subscription_queries)
        update_runs = getattr(instance, "update_runs", None)
        if update_runs is None:
            update_runs = UpdateRunRepository(
                connection_factory,
                run_decoder if callable(run_decoder) else _decode_update_run,
            )
            setattr(instance, "update_runs", update_runs)
        backup = getattr(instance, "backup_database", None)
        repository = UpdateRepository(
            connection_factory,
            subscription_queries=subscription_queries,
            update_runs=update_runs,
            decode_run=run_decoder if callable(run_decoder) else _decode_update_run,
            decode_candidate=candidate_decoder if callable(candidate_decoder) else _decode_update_candidate,
            backup=backup if callable(backup) else _missing_backup,
        )
    else:  # pragma: no cover - only the fixed repository map below calls this helper
        raise AttributeError(f"未知兼容仓储：{repository_attribute}")

    setattr(instance, repository_attribute, repository)
    return repository


def _repository_delegate(repository_attribute: str, method_name: str) -> Callable[..., Any]:
    def delegated(self: Any, *args: Any, **kwargs: Any) -> Any:
        repository = _compat_repository(self, repository_attribute)
        return getattr(repository, method_name)(*args, **kwargs)

    delegated.__name__ = method_name
    delegated.__qualname__ = f"DatabaseDomainMixin.{method_name}"
    delegated.__doc__ = (
        f"Deprecated forwarding method for ``{repository_attribute}.{method_name}``."
    )
    return delegated


class DatabaseDomainMixin:
    """Compatibility-only mixin that delegates to split repository objects."""

    _decode_json_fields = staticmethod(_decode_json_fields)
    _decode_update_subscription = staticmethod(_decode_update_subscription)
    _decode_update_source = staticmethod(_decode_update_source)
    _decode_update_run = staticmethod(_decode_update_run)
    _decode_update_candidate = staticmethod(_decode_update_candidate)


for _repository_attribute, _repository_type in (
    ("rclone", RcloneRepository),
    ("organizer", OrganizerRepository),
    ("update", UpdateRepository),
):
    for _method_name, _method in vars(_repository_type).items():
        if _method_name.startswith("__") or not callable(_method):
            continue
        if not hasattr(DatabaseDomainMixin, _method_name):
            setattr(
                DatabaseDomainMixin,
                _method_name,
                _repository_delegate(_repository_attribute, _method_name),
            )


del _method, _method_name, _repository_attribute, _repository_type


__all__ = [
    "DatabaseDomainMixin",
    "HISTORY_CLEANUP_TABLES",
    "HISTORY_PRESERVED_TABLES",
    "OrganizerRepository",
    "RcloneRepository",
    "UpdateRepository",
    "utc_now",
    "utc_minutes_from_now",
    "utc_seconds_from_now",
    "_callback_path_is_same_or_child",
    "_decode_json_fields",
    "_hash_text",
    "_job_matches_staging_callback_paths",
    "_job_owns_staging_directory",
    "_json_text",
    "_match_score",
    "_normalize_callback_path",
    "_normalize_match_text",
    "_rclone_category_match_values",
    "_rclone_job_id_from_paths",
]
