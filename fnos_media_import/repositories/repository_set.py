"""Explicit application repository graph.

``Database`` owns only SQLite infrastructure.  Business services receive this
repository graph (or one of its concrete repository members) so persistence
dependencies remain visible and no domain method is forwarded through the
database object.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .admin_profile_repository import AdminProfileRepository
from .app_settings_repository import AppSettingsRepository
from .guest_notification_subscription_repository import (
    GuestNotificationSubscriptionRepository,
)
from .guest_request_command_repository import GuestRequestCommandRepository
from .guest_request_query_repository import GuestRequestQueryRepository
from .job_command_repository import JobCommandRepository
from .job_query_repository import JobQueryRepository
from .notification_delivery_repository import NotificationDeliveryRepository
from .organizer_repository import OrganizerRepository
from .rate_limit_repository import RateLimitRepository
from .rclone_repository import RcloneRepository
from .resource_repository import ResourceRepository
from .scheduler_lease_repository import SchedulerLeaseRepository
from .search_cache_command_repository import SearchCacheCommandRepository
from .search_cache_query_repository import SearchCacheQueryRepository
from .trending_repository import TrendingRepository
from .update_repository import UpdateRepository
from .update_run_repository import UpdateRunRepository
from .update_subscription_query_repository import UpdateSubscriptionQueryRepository
from .worker_task_repository import WorkerTaskRepository


ConnectionFactory = Callable[[], Any]
BackupDatabase = Callable[..., dict[str, Any]]


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _decode_json_fields(
    item: dict[str, Any], fields: tuple[str, ...]
) -> dict[str, Any]:
    for field in fields:
        if item.get(field):
            try:
                item[field] = json.loads(item[field])
            except (TypeError, json.JSONDecodeError):
                pass
    return item


def decode_update_subscription(
    row: sqlite3.Row | None,
) -> dict[str, Any] | None:
    item = row_to_dict(row)
    return (
        _decode_json_fields(
            item,
            (
                "aliases",
                "days_of_week",
                "missing_episodes",
                "include_keywords",
                "exclude_keywords",
                "raw_data",
            ),
        )
        if item
        else None
    )


def decode_update_source(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = row_to_dict(row)
    if item is None:
        return None
    item["enabled"] = bool(item.get("enabled"))
    return _decode_json_fields(item, ("options",))


def decode_update_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = row_to_dict(row)
    return _decode_json_fields(item, ("summary", "raw_data", "run_log")) if item else None


def decode_update_candidate(
    row: sqlite3.Row | None,
) -> dict[str, Any] | None:
    item = row_to_dict(row)
    return _decode_json_fields(item, ("raw_data",)) if item else None


def terminal_duplicate_status(*values: Any) -> bool:
    text = " ".join(str(value or "").strip().lower() for value in values)
    terminal_words = (
        "cancelled",
        "canceled",
        "rejected",
        "failed",
        "unsupported",
        "skipped",
        "取消",
        "拒绝",
        "未通过",
        "失败",
        "暂不支持",
    )
    return any(word in text for word in terminal_words)


@dataclass(frozen=True, slots=True)
class RepositorySet:
    """All repositories built from one SQLite connection factory."""

    connection_factory: ConnectionFactory
    backup: BackupDatabase
    app_settings: AppSettingsRepository
    admin_profiles: AdminProfileRepository
    guest_request_commands: GuestRequestCommandRepository
    guest_request_queries: GuestRequestQueryRepository
    guest_notification_subscriptions: GuestNotificationSubscriptionRepository
    job_commands: JobCommandRepository
    job_queries: JobQueryRepository
    resources: ResourceRepository
    scheduler_leases: SchedulerLeaseRepository
    rate_limits: RateLimitRepository
    worker_tasks: WorkerTaskRepository
    notification_deliveries: NotificationDeliveryRepository
    search_cache_commands: SearchCacheCommandRepository
    search_cache_queries: SearchCacheQueryRepository
    update_subscription_queries: UpdateSubscriptionQueryRepository
    update_runs: UpdateRunRepository
    trending: TrendingRepository
    rclone: RcloneRepository
    organizer: OrganizerRepository
    update: UpdateRepository

    @classmethod
    def build(
        cls,
        connection_factory: ConnectionFactory,
        backup: BackupDatabase,
    ) -> "RepositorySet":
        app_settings = AppSettingsRepository(connection_factory)
        update_subscription_queries = UpdateSubscriptionQueryRepository(
            connection_factory,
            decode_update_subscription,
            decode_update_source,
        )
        update_runs = UpdateRunRepository(connection_factory, decode_update_run)
        return cls(
            connection_factory=connection_factory,
            backup=backup,
            app_settings=app_settings,
            admin_profiles=AdminProfileRepository(app_settings),
            guest_request_commands=GuestRequestCommandRepository(connection_factory),
            guest_request_queries=GuestRequestQueryRepository(
                connection_factory, row_to_dict
            ),
            guest_notification_subscriptions=GuestNotificationSubscriptionRepository(
                connection_factory
            ),
            job_commands=JobCommandRepository(
                connection_factory, terminal_duplicate_status
            ),
            job_queries=JobQueryRepository(connection_factory, row_to_dict),
            resources=ResourceRepository(connection_factory, row_to_dict),
            scheduler_leases=SchedulerLeaseRepository(connection_factory),
            rate_limits=RateLimitRepository(connection_factory),
            worker_tasks=WorkerTaskRepository(connection_factory, row_to_dict),
            notification_deliveries=NotificationDeliveryRepository(
                connection_factory
            ),
            search_cache_commands=SearchCacheCommandRepository(connection_factory),
            search_cache_queries=SearchCacheQueryRepository(
                connection_factory, row_to_dict
            ),
            update_subscription_queries=update_subscription_queries,
            update_runs=update_runs,
            trending=TrendingRepository(connection_factory),
            rclone=RcloneRepository(connection_factory, row_to_dict),
            organizer=OrganizerRepository(connection_factory),
            update=UpdateRepository(
                connection_factory,
                subscription_queries=update_subscription_queries,
                update_runs=update_runs,
                decode_run=decode_update_run,
                decode_candidate=decode_update_candidate,
                backup=backup,
            ),
        )

    @classmethod
    def from_database(cls, database: Any) -> "RepositorySet":
        connection_factory = getattr(database, "connect", None)
        backup = getattr(database, "backup_database", None)
        if not callable(connection_factory) or not callable(backup):
            raise TypeError("RepositorySet 需要数据库 connect/backup 工厂")
        return cls.build(connection_factory, backup)

    def connect(self) -> Any:
        """Expose the shared connection factory to transaction-owning services."""

        return self.connection_factory()

    def backup_database(self, **kwargs: Any) -> dict[str, Any]:
        return self.backup(**kwargs)


@dataclass(frozen=True, slots=True)
class OrganizerRepositories:
    """Persistence dependencies used by :class:`OrganizerService` only."""

    organizer: OrganizerRepository
    job_queries: JobQueryRepository
    job_commands: JobCommandRepository
    guest_request_queries: GuestRequestQueryRepository
    guest_request_commands: GuestRequestCommandRepository
    update: UpdateRepository | None = None

    @classmethod
    def from_set(cls, repositories: RepositorySet) -> "OrganizerRepositories":
        return cls(
            organizer=repositories.organizer,
            job_queries=repositories.job_queries,
            job_commands=repositories.job_commands,
            guest_request_queries=repositories.guest_request_queries,
            guest_request_commands=repositories.guest_request_commands,
            update=repositories.update,
        )


@dataclass(frozen=True, slots=True)
class RcloneRepositories:
    """Persistence dependencies used by rclone orchestration and recovery."""

    connection_factory: ConnectionFactory
    rclone: RcloneRepository
    job_queries: JobQueryRepository
    job_commands: JobCommandRepository

    @classmethod
    def from_set(cls, repositories: RepositorySet) -> "RcloneRepositories":
        return cls(
            connection_factory=repositories.connection_factory,
            rclone=repositories.rclone,
            job_queries=repositories.job_queries,
            job_commands=repositories.job_commands,
        )

    def connect(self) -> Any:
        return self.connection_factory()


@dataclass(frozen=True, slots=True)
class JobRepositories:
    """Read repositories needed to assemble the administrator job timeline."""

    job_queries: JobQueryRepository
    guest_request_queries: GuestRequestQueryRepository
    rclone: RcloneRepository
    organizer: OrganizerRepository
    worker_tasks: WorkerTaskRepository

    @classmethod
    def from_set(cls, repositories: RepositorySet) -> "JobRepositories":
        return cls(
            job_queries=repositories.job_queries,
            guest_request_queries=repositories.guest_request_queries,
            rclone=repositories.rclone,
            organizer=repositories.organizer,
            worker_tasks=repositories.worker_tasks,
        )


@dataclass(frozen=True, slots=True)
class UpdateRepositories:
    """Persistence dependencies used by update subscriptions and runs."""

    update: UpdateRepository
    job_queries: JobQueryRepository
    scheduler_leases: SchedulerLeaseRepository

    @classmethod
    def from_set(cls, repositories: RepositorySet) -> "UpdateRepositories":
        return cls(
            update=repositories.update,
            job_queries=repositories.job_queries,
            scheduler_leases=repositories.scheduler_leases,
        )


@dataclass(frozen=True, slots=True)
class NotificationRepositories:
    """Persistence dependencies used by notification services and workers."""

    connection_factory: ConnectionFactory
    app_settings: AppSettingsRepository
    scheduler_leases: SchedulerLeaseRepository
    notification_deliveries: NotificationDeliveryRepository
    guest_notification_subscriptions: GuestNotificationSubscriptionRepository
    worker_tasks: WorkerTaskRepository

    @classmethod
    def from_set(cls, repositories: RepositorySet) -> "NotificationRepositories":
        return cls(
            connection_factory=repositories.connection_factory,
            app_settings=repositories.app_settings,
            scheduler_leases=repositories.scheduler_leases,
            notification_deliveries=repositories.notification_deliveries,
            guest_notification_subscriptions=repositories.guest_notification_subscriptions,
            worker_tasks=repositories.worker_tasks,
        )

    def connect(self) -> Any:
        return self.connection_factory()


__all__ = [
    "JobRepositories",
    "NotificationRepositories",
    "OrganizerRepositories",
    "RcloneRepositories",
    "RepositorySet",
    "UpdateRepositories",
    "decode_update_candidate",
    "decode_update_run",
    "decode_update_source",
    "decode_update_subscription",
    "row_to_dict",
    "terminal_duplicate_status",
]
