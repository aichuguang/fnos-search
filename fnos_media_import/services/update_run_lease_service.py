from __future__ import annotations

import threading
from typing import Any, Callable


class UpdateRunLeaseLost(RuntimeError):
    pass


class UpdateRunAlreadyActive(RuntimeError):
    def __init__(self, active_run: dict[str, Any] | None = None) -> None:
        self.active_run = dict(active_run or {})
        super().__init__("该订阅已有追更任务正在运行")


class UpdateRunLease:
    """Renews one durable update-run lease while its synchronous work executes."""

    def __init__(
        self,
        *,
        database: Any,
        run_id: int,
        owner_id: str,
        lease_seconds: int,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.database = database
        self.run_id = int(run_id)
        self.owner_id = str(owner_id)
        self.lease_seconds = max(30, int(lease_seconds))
        self.log = log or (lambda _message: None)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "UpdateRunLease":
        interval = max(5.0, self.lease_seconds / 3)

        def heartbeat() -> None:
            while not self._stop.wait(interval):
                if not self.database.renew_update_run(
                    self.run_id,
                    self.owner_id,
                    lease_seconds=self.lease_seconds,
                ):
                    self._lost.set()
                    self.log(f"update run #{self.run_id} lease renewal failed")
                    return

        self._thread = threading.Thread(
            target=heartbeat,
            name=f"update-run-heartbeat-{self.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def ensure_owned(self) -> None:
        if self._lost.is_set() or not self.database.owns_update_run(self.run_id, self.owner_id):
            self._lost.set()
            raise UpdateRunLeaseLost("追更运行租约已失效，停止继续处理")
