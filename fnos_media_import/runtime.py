from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from flask import Flask, g, has_request_context


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Immutable set of services that belong to one configuration revision."""

    config: Any
    database: Any
    pansou: Any
    btbtla: Any
    quark_importer: Any
    cloud139_importer: Any
    generic_importers: dict[str, Any]
    fnos: Any
    search_service: Any
    import_service: Any
    organizer_service: Any
    job_service: Any
    rclone_service: Any
    update_service: Any
    update_scheduler: Any


class RuntimeServices:
    """Atomically publishes the active immutable runtime snapshot."""

    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self._snapshot = snapshot
        self._lock = threading.RLock()
        self._revision = 1

    def get(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def swap(self, snapshot: RuntimeSnapshot) -> int:
        with self._lock:
            self._snapshot = snapshot
            self._revision += 1
            return self._revision

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision


def install_request_runtime(app: Flask, services: RuntimeServices) -> None:
    """Bind one immutable runtime snapshot for the complete request lifetime."""

    @app.before_request
    def bind_runtime_snapshot() -> None:
        g.runtime_snapshot = services.get()
        g.runtime_revision = services.revision


def current_runtime(services: RuntimeServices) -> RuntimeSnapshot:
    """Return the request-bound snapshot, or the current snapshot outside requests."""
    if has_request_context() and hasattr(g, "runtime_snapshot"):
        return g.runtime_snapshot
    return services.get()
