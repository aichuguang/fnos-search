from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint


@dataclass(frozen=True)
class CallbackRouteContext:
    rclone_callback: Callable[..., Any]


def create_callbacks_blueprint(context: CallbackRouteContext) -> Blueprint:
    blueprint = Blueprint("callback_routes", __name__)
    blueprint.add_url_rule(
        "/api/callback/rclone",
        endpoint="rclone_callback",
        view_func=context.rclone_callback,
        methods=["POST"],
    )
    return blueprint
