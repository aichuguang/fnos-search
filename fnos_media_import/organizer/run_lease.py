from __future__ import annotations

import threading
from typing import Any, Callable


class OrganizerRunLeaseLost(RuntimeError):
    pass


class OrganizerScanLeaseLost(OrganizerRunLeaseLost):
    """Raised when a scan worker no longer owns its durable task lease."""


class OrganizerScanLease:
    """Keep a durable Organizer scan claim alive while OpenList is read.

    Scanning can involve many remote directory requests.  The lease prevents a
    second worker from starting the same scan while the first one is still
    making progress, and the owner/revision checks fence stale writers after a
    takeover or an explicit skip/cancel.
    """

    def __init__(
        self,
        *,
        database: Any,
        task_id: int,
        owner_id: str,
        revision: int,
        lease_seconds: int,
        heartbeat_interval_seconds: float | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.database = database
        self.task_id = int(task_id)
        self.owner_id = str(owner_id)
        self.revision = max(1, int(revision or 1))
        self.lease_seconds = max(30, int(lease_seconds or 120))
        default_interval = max(0.1, self.lease_seconds / 3)
        requested_interval = float(default_interval if heartbeat_interval_seconds is None else heartbeat_interval_seconds)
        self.heartbeat_interval_seconds = max(0.05, min(requested_interval, max(0.05, self.lease_seconds / 3)))
        self.log = log or (lambda _message: None)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._renew_supported = callable(getattr(database, "renew_organizer_scan", None))
        self._owns_supported = callable(getattr(database, "owns_organizer_scan", None))

    def start(self) -> None:
        if not self._renew_supported:
            return
        if not self._renew():
            raise OrganizerScanLeaseLost("Organizer 扫描租约已失效，停止继续扫描")

        def heartbeat() -> None:
            while not self._stop.wait(self.heartbeat_interval_seconds):
                if self._renew():
                    continue
                self._lost.set()
                self.log(f"Organizer scan task #{self.task_id} 租约续期失败")
                return

        self._thread = threading.Thread(
            target=heartbeat,
            name=f"organizer-scan-heartbeat-{self.task_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, min(5.0, self.heartbeat_interval_seconds + 1.0)))

    def ensure_owned(self) -> None:
        if not (self._renew_supported and self._owns_supported):
            return
        try:
            owned = bool(
                self.database.owns_organizer_scan(
                    self.task_id,
                    self.owner_id,
                    expected_revision=self.revision,
                )
            )
        except TypeError:
            try:
                owned = bool(self.database.owns_organizer_scan(self.task_id, self.owner_id))
            except Exception:  # noqa: BLE001
                owned = False
        except Exception:  # noqa: BLE001
            owned = False
        if self._lost.is_set() or not owned:
            self._lost.set()
            raise OrganizerScanLeaseLost("Organizer 扫描租约已失效，拒绝写入旧计划")

    def _renew(self) -> bool:
        try:
            return bool(
                self.database.renew_organizer_scan(
                    self.task_id,
                    self.owner_id,
                    lease_seconds=self.lease_seconds,
                    expected_revision=self.revision,
                )
            )
        except TypeError:
            try:
                return bool(
                    self.database.renew_organizer_scan(
                        self.task_id,
                        self.owner_id,
                        lease_seconds=self.lease_seconds,
                    )
                )
            except Exception:  # noqa: BLE001
                return False
        except Exception:  # noqa: BLE001
            return False


class OrganizerRunLease:
    """Keep one durable Organizer run owned while apply performs remote I/O."""

    def __init__(
        self,
        *,
        database: Any,
        run_id: int,
        owner_id: str,
        lease_seconds: int,
        heartbeat_interval_seconds: float | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.database = database
        self.run_id = int(run_id)
        self.owner_id = str(owner_id)
        self.lease_seconds = max(1, int(lease_seconds))
        default_interval = max(0.1, self.lease_seconds / 3)
        requested_interval = float(default_interval if heartbeat_interval_seconds is None else heartbeat_interval_seconds)
        self.heartbeat_interval_seconds = max(0.05, min(requested_interval, max(0.05, self.lease_seconds / 3)))
        self.log = log or (lambda _message: None)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._supported = callable(getattr(database, "renew_organizer_run", None)) and callable(
            getattr(database, "owns_organizer_run", None)
        )

    def __enter__(self) -> "OrganizerRunLease":
        if not self._supported:
            return self
        if not self._renew():
            raise OrganizerRunLeaseLost("Organizer 运行租约已失效，停止继续整理")

        def heartbeat() -> None:
            while not self._stop.wait(self.heartbeat_interval_seconds):
                if self._renew():
                    continue
                self._lost.set()
                self.log(f"Organizer run #{self.run_id} 租约续期失败")
                return

        self._thread = threading.Thread(
            target=heartbeat,
            name=f"organizer-run-heartbeat-{self.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, min(5.0, self.heartbeat_interval_seconds + 1.0)))

    def ensure_owned(self) -> None:
        if not self._supported:
            return
        owns = getattr(self.database, "owns_organizer_run")
        try:
            owned = bool(owns(self.run_id, self.owner_id))
        except Exception:  # noqa: BLE001
            owned = False
        if self._lost.is_set() or not owned:
            self._lost.set()
            raise OrganizerRunLeaseLost("Organizer 运行租约已失效，停止继续整理")

    def _renew(self) -> bool:
        renew = getattr(self.database, "renew_organizer_run")
        try:
            return bool(
                renew(
                    self.run_id,
                    self.owner_id,
                    lease_seconds=self.lease_seconds,
                )
            )
        except Exception:  # noqa: BLE001
            return False
