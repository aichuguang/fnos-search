from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint


@dataclass(frozen=True)
class SettingsRouteContext:
    admin_required: Callable[[Callable[..., Any]], Callable[..., Any]]
    config: Callable[..., Any]
    history_summary: Callable[..., Any]
    cleanup_history: Callable[..., Any]
    advanced_config: Callable[..., Any]
    advanced_config_update: Callable[..., Any]
    advanced_export: Callable[..., Any]
    settings: Callable[..., Any]
    settings_update: Callable[..., Any]
    settings_update_all: Callable[..., Any]
    notifications_config: Callable[..., Any] = lambda: ({}, 200)
    notifications_update: Callable[..., Any] = lambda payload: ({}, 200)
    notifications_test: Callable[..., Any] = lambda payload: ({}, 200)
    notifications_deliveries: Callable[..., Any] = lambda payload: ({}, 200)
    notifications_retry: Callable[..., Any] = lambda task_id: ({}, 200)


def create_settings_blueprint(context: SettingsRouteContext) -> Blueprint:
    blueprint = Blueprint("settings", __name__)
    protected = context.admin_required
    blueprint.add_url_rule("/api/admin/config", endpoint="config", view_func=protected(context.config), methods=["GET"])
    blueprint.add_url_rule("/api/admin/maintenance/history-summary", endpoint="history_summary", view_func=protected(context.history_summary), methods=["GET"])
    blueprint.add_url_rule("/api/admin/maintenance/cleanup-history", endpoint="cleanup_history", view_func=protected(context.cleanup_history), methods=["POST"])
    blueprint.add_url_rule("/api/admin/advanced-config", endpoint="advanced_config", view_func=protected(context.advanced_config), methods=["GET"])
    blueprint.add_url_rule("/api/admin/advanced-config/export", endpoint="advanced_export", view_func=protected(context.advanced_export), methods=["POST"])
    advanced_config_update = protected(context.advanced_config_update)
    blueprint.add_url_rule("/api/admin/advanced-config", endpoint="advanced_config_update", view_func=advanced_config_update, methods=["POST"])
    blueprint.add_url_rule("/api/admin/advanced-config", endpoint="advanced_config_update", view_func=advanced_config_update, methods=["PUT"])
    blueprint.add_url_rule("/api/admin/settings", endpoint="settings", view_func=protected(context.settings), methods=["GET"])
    settings_update = protected(context.settings_update)
    blueprint.add_url_rule("/api/admin/settings", endpoint="settings_update", view_func=settings_update, methods=["POST"])
    blueprint.add_url_rule("/api/admin/settings", endpoint="settings_update", view_func=settings_update, methods=["PUT"])
    blueprint.add_url_rule(
        "/api/admin/settings/all",
        endpoint="settings_update_all",
        view_func=protected(context.settings_update_all),
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/notifications",
        endpoint="notifications_config",
        view_func=protected(context.notifications_config),
        methods=["GET"],
    )
    notifications_update = protected(context.notifications_update)
    blueprint.add_url_rule("/api/admin/notifications", endpoint="notifications_update", view_func=notifications_update, methods=["POST"])
    blueprint.add_url_rule("/api/admin/notifications", endpoint="notifications_update", view_func=notifications_update, methods=["PUT"])
    blueprint.add_url_rule(
        "/api/admin/notifications/test",
        endpoint="notifications_test",
        view_func=protected(context.notifications_test),
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/admin/notifications/deliveries",
        endpoint="notifications_deliveries",
        view_func=protected(context.notifications_deliveries),
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/admin/notifications/tasks/<int:task_id>/retry",
        endpoint="notifications_retry",
        view_func=protected(context.notifications_retry),
        methods=["POST"],
    )
    return blueprint
