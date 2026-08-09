"""Test-only namespaces for focused in-memory repository fakes.

Production code receives concrete repositories through ``RepositorySet``.
Small unit-test fakes can inherit ``RepositoryFakeMixin`` and keep modelling
only the operations under test, without restoring a production Database
forwarding API.
"""

from __future__ import annotations

from typing import Any


class _AliasProxy:
    def __init__(self, target: Any, aliases: dict[str, str] | None = None) -> None:
        self._target = target
        self._aliases = aliases or {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, self._aliases.get(name, name))


class _Namespace:
    def __init__(self, name: str, aliases: dict[str, str] | None = None) -> None:
        self.name = name
        self.aliases = aliases or {}

    def __get__(self, instance: Any, _owner: Any) -> Any:
        if instance is None:
            return self
        override = instance.__dict__.get(self.name)
        if override is not None:
            return override
        return _AliasProxy(instance, self.aliases) if self.aliases else instance

    def __set__(self, instance: Any, value: Any) -> None:
        instance.__dict__[self.name] = value


class RepositoryFakeMixin:
    job_queries = _Namespace(
        "job_queries",
        {
            "get": "get_job",
            "get_by_idempotency_key": "get_job_by_idempotency_key",
            "get_many": "get_jobs_by_ids",
            "list": "list_jobs",
            "count": "count_jobs",
            "list_events": "list_events",
        },
    )
    job_commands = _Namespace(
        "job_commands",
        {
            "create": "create_job",
            "update": "update_job",
            "update_if_status": "update_job_if_status",
            "update_if_status_and_claim_token": "update_job_if_status_and_claim_token",
            "add_event": "add_event",
            "delete_if_status": "delete_job_if_status",
        },
    )
    guest_request_queries = _Namespace(
        "guest_request_queries",
        {
            "get": "get_guest_request",
            "get_by_token": "get_guest_request_by_token",
            "list_by_job": "list_guest_requests_by_job",
            "list_events": "list_guest_request_events",
            "list_events_for_requests": "list_guest_request_events_for_requests",
            "list": "list_guest_requests",
            "count": "count_guest_requests",
            "find_recent_by_url": "find_recent_guest_request_by_url",
        },
    )
    guest_request_commands = _Namespace(
        "guest_request_commands",
        {
            "create": "create_guest_request",
            "create_with_event": "create_guest_request_with_event",
            "update": "update_guest_request",
            "transition_with_event": "transition_guest_request_with_event",
            "add_event": "add_guest_request_event",
        },
    )
    scheduler_leases = _Namespace(
        "scheduler_leases",
        {
            "acquire": "acquire_scheduler_lease",
            "release": "release_scheduler_lease",
            "get": "scheduler_lease",
        },
    )
    search_cache_commands = _Namespace(
        "search_cache_commands",
        {
            "save": "save_search_cache",
            "save_many": "save_search_cache_many",
            "update": "update_search_cache_item",
            "prune_expired": "prune_expired_search_cache",
        },
    )
    search_cache_queries = _Namespace(
        "search_cache_queries",
        {
            "get_active": "get_search_cache",
            "find_active_by_urls": "find_active_search_cache_by_urls",
        },
    )
    resources = _Namespace(
        "resources",
        {
            "save": "save_resource",
            "save_many": "save_resources",
            "get": "get_resource",
            "find_by_url": "find_resource_by_url",
        },
    )
    app_settings = _Namespace(
        "app_settings",
        {
            "get_all": "get_app_settings",
            "set_many": "set_app_settings",
            "update_atomic": "update_app_setting_atomic",
            "mutate_all_atomic": "mutate_app_settings_atomic",
            "compare_and_set": "compare_and_set_app_setting",
        },
    )
    notification_deliveries = _Namespace(
        "notification_deliveries",
        {
            "record": "record_notification_delivery",
            "latest_status_by_channel": "latest_notification_delivery_status_by_task",
            "list_deliveries": "list_notification_deliveries",
            "summary": "notification_delivery_summary",
            "prune": "prune_notification_deliveries",
            "last_delivery_for_event": "last_notification_delivery_for_event",
        },
    )
    guest_notification_subscriptions = _Namespace(
        "guest_notification_subscriptions",
        {
            "create": "create_guest_notification_subscription",
            "get_for_request": "get_guest_notification_subscription",
            "verify": "verify_guest_notification_subscription",
            "opt_out": "opt_out_guest_notification_subscription",
            "anonymize_terminal": "anonymize_guest_notification_subscriptions",
        },
    )
    organizer = _Namespace("organizer")
    rclone = _Namespace("rclone")
    update = _Namespace("update")
    trending = _Namespace(
        "trending",
        {
            "create_run": "create_trending_run",
            "finish_run": "finish_trending_run",
            "get_run": "get_trending_run",
            "get_latest_run": "get_latest_trending_run",
            "list_runs": "list_trending_runs",
            "count_runs": "count_trending_runs",
            "upsert_snapshot": "upsert_trending_snapshot",
            "list_snapshots": "list_trending_snapshots",
            "upsert_candidate": "upsert_trending_candidate",
            "get_candidate": "get_trending_candidate",
            "list_candidates": "list_trending_candidates",
            "count_candidates": "count_trending_candidates",
            "update_candidate": "update_trending_candidate",
            "claim_candidate_for_initial_import": "claim_trending_candidate_for_initial_import",
            "bind_candidate_initial_import_job": "bind_trending_candidate_initial_import_job",
            "release_candidate_initial_import": "release_trending_candidate_initial_import",
        },
    )


__all__ = ["RepositoryFakeMixin"]
