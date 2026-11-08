"""Callables the Flask application injects for the agent tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class AgentWriteServices:
    """Mutating capabilities.

    Kept in a separate object so read-only deployments (and read-only tests) can
    construct ``AgentServices`` without wiring every write path.
    """

    submit_import: Callable[..., tuple[dict[str, Any], int]]
    preview_import: Callable[[dict[str, Any]], tuple[dict[str, Any], int]]
    approve_request: Callable[..., tuple[dict[str, Any], int]]
    reject_request: Callable[..., tuple[dict[str, Any], int]]
    retry_job: Callable[[int], tuple[dict[str, Any], int]]
    cancel_job: Callable[..., tuple[dict[str, Any], int]]
    review_job: Callable[..., tuple[dict[str, Any], int]]
    update_organizer_mappings: Callable[..., tuple[dict[str, Any], int]]
    apply_organizer_task: Callable[..., tuple[dict[str, Any], int]]
    retry_organizer_task: Callable[[int], tuple[dict[str, Any], int]]
    rollback_organizer_run: Callable[[int], tuple[dict[str, Any], int]]
    create_subscription: Callable[[dict[str, Any]], tuple[dict[str, Any], int]]
    preview_subscription: Callable[[dict[str, Any]], tuple[dict[str, Any], int]]
    update_subscription: Callable[[int, dict[str, Any]], tuple[dict[str, Any], int]]
    set_subscription_status: Callable[[int, str], tuple[dict[str, Any], int]]
    run_subscription: Callable[[int], tuple[dict[str, Any], int]]
    delete_subscription: Callable[[int], tuple[dict[str, Any], int]]
    list_media_libraries: Callable[[], dict[str, Any]]
    search_media_library: Callable[..., dict[str, Any]]
    refresh_media_library: Callable[[dict[str, Any]], tuple[dict[str, Any], int]]
    list_trending_candidates: Callable[..., dict[str, Any]]
    import_trending_candidate: Callable[..., tuple[dict[str, Any], int]]
    subscribe_trending_candidate: Callable[[int], tuple[dict[str, Any], int]]


@dataclass(frozen=True)
class AgentServices:
    """Read/write capabilities as plain callables.

    The application owns the wiring so this package never imports Flask or the
    service graph, which keeps tools unit-testable with small fakes.
    """

    database: Any
    search: Callable[..., dict[str, Any]]
    resource_detail: Callable[[str], tuple[dict[str, Any], int]]
    resource_files: Callable[[str, str], tuple[dict[str, Any], int]]
    detect_link: Callable[[str, str], dict[str, Any]]
    tmdb_search: Callable[[str], dict[str, Any]]
    list_jobs: Callable[..., dict[str, Any]]
    job_detail: Callable[[int], tuple[dict[str, Any], int]]
    list_requests: Callable[..., dict[str, Any]]
    request_detail: Callable[[int], tuple[dict[str, Any], int]]
    list_organizer_tasks: Callable[..., dict[str, Any]]
    organizer_task_detail: Callable[[int], tuple[dict[str, Any], int]]
    list_subscriptions: Callable[..., dict[str, Any]]
    subscription_detail: Callable[[int], tuple[dict[str, Any], int]]
    scheduler_status: Callable[[], dict[str, Any]]
    system_status: Callable[[], dict[str, Any]]
    categories: Callable[[], dict[str, Any]]
    writes: AgentWriteServices | None = None
