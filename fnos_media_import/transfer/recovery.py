"""Durable recovery scanner for V1 transfer executions.

The queue is only a wake-up mechanism; the authoritative phase/receipt lives
in ``transfer_executions``.  A process restart, a lost queue row, or an
expired worker lease therefore must be repaired from that table rather than
from legacy ``raw_data`` fields.  This scanner is intentionally small and
side-effect free apart from idempotent queue insertion so it can run at
startup and periodically in the worker process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from datetime import datetime, timezone
from typing import Any

from ..time_utils import utc_now_iso
from . import PHASE_TRANSFERRING, TERMINAL_PHASES


@dataclass(frozen=True)
class RecoveryScanResult:
    scanned: int = 0
    enqueued: int = 0
    already_queued: int = 0
    skipped_terminal: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "enqueued": self.enqueued,
            "already_queued": self.already_queued,
            "skipped_terminal": self.skipped_terminal,
            "errors": list(self.errors),
        }


class TransferRecoveryScanner:
    """Requeue due, non-terminal executions exactly once per job."""

    TASK_TYPE = "import_transfer_execute"

    def __init__(
        self,
        executions: Any,
        worker_tasks: Any,
        *,
        max_attempts: int = 20,
        task_key_prefix: str = "import-transfer",
    ) -> None:
        self.executions = executions
        self.worker_tasks = worker_tasks
        self.max_attempts = max(1, int(max_attempts))
        self.task_key_prefix = str(task_key_prefix or "import-transfer").strip() or "import-transfer"

    def wake_after_rclone_run(self, job_id: int) -> dict[str, Any]:
        """Wake one execution whose transferring wait ended with the rclone run.

        rclone run 到达终态的那一刻，transferring 阶段的指数退避就不再有
        意义；不清退避的话，执行最长要再等 300 秒才会在下一次轮询中发现
        完成。只唤醒仍在 ``transferring`` 且非终态的执行，其他阶段（重试、
        人工审核等）的退避保持原样。
        """
        normalized = int(job_id or 0)
        if normalized <= 0:
            return {"woken": False, "reason": "invalid_job_id"}
        execution = self.executions.get(normalized)
        phase = str(getattr(execution, "phase", "") or "") if execution is not None else ""
        if execution is None or phase in TERMINAL_PHASES:
            return {"woken": False, "reason": "no_active_execution"}
        if phase != PHASE_TRANSFERRING:
            return {"woken": False, "reason": f"phase_not_transferring:{phase}"}
        self.executions.clear_resume_backoff(normalized, PHASE_TRANSFERRING)
        key = f"{self.task_key_prefix}:{normalized}"
        _task_id, created = self.worker_tasks.enqueue(
            self.TASK_TYPE,
            {"job_id": normalized, "rclone_terminal": True},
            key,
            max_attempts=self.max_attempts,
            reactivate_terminal=True,
        )
        return {"woken": True, "enqueued": bool(created), "job_id": normalized}

    def scan_once(self, *, now: str | None = None, limit: int = 100) -> RecoveryScanResult:
        due = self.executions.list_due(now=now or utc_now_iso(), limit=max(0, int(limit)))
        scanned = enqueued = already = skipped = 0
        errors: list[str] = []
        for execution in due:
            scanned += 1
            phase = str(getattr(execution, "phase", "") or "")
            job_id = int(getattr(execution, "job_id", 0) or 0)
            if job_id <= 0 or phase in TERMINAL_PHASES:
                skipped += 1
                continue
            key = f"{self.task_key_prefix}:{job_id}"
            try:
                _task_id, created = self.worker_tasks.enqueue(
                    self.TASK_TYPE,
                    {"job_id": job_id, "recovery": True},
                    key,
                    max_attempts=self.max_attempts,
                    reactivate_terminal=True,
                )
                if created:
                    enqueued += 1
                else:
                    already += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"job {job_id}: {exc}")
        return RecoveryScanResult(
            scanned=scanned,
            enqueued=enqueued,
            already_queued=already,
            skipped_terminal=skipped,
            errors=tuple(errors),
        )


class TransferRecoveryRuntime:
    """Run the V1 recovery scanner at startup and on a bounded interval.

    ``worker_tasks`` is only a wake-up channel, so relying on an existing
    pending row is insufficient after queue corruption, operator cleanup or a
    crash between execution persistence and queue finalization.  This runtime
    is intentionally independent from Flask and can be owned by the worker
    process composition root.
    """

    def __init__(
        self,
        scanner: TransferRecoveryScanner,
        *,
        interval_seconds: float = 30.0,
        batch_size: int = 100,
        log: Any = None,
    ) -> None:
        self.scanner = scanner
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.batch_size = max(1, int(batch_size))
        self.log = log if callable(log) else (lambda _message: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last_started_at = ""
        self._last_finished_at = ""
        self._last_result: dict[str, Any] = {}
        self._last_error = ""
        self._run_count = 0

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="transfer-recovery-scanner",
                daemon=True,
            )
            self._thread.start()
            return True

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def run_once(self) -> RecoveryScanResult:
        started = _utc_now()
        with self._state_lock:
            self._last_started_at = started
            self._last_error = ""
        try:
            result = self.scanner.scan_once(limit=self.batch_size)
        except Exception as exc:  # noqa: BLE001 - keep the sole recovery loop alive
            message = str(exc) or exc.__class__.__name__
            with self._state_lock:
                self._last_finished_at = _utc_now()
                self._last_error = message
                self._run_count += 1
            self._safe_log(f"transfer recovery scan failed: {message}")
            raise
        payload = result.to_dict()
        with self._state_lock:
            self._last_finished_at = _utc_now()
            self._last_result = payload
            self._last_error = "; ".join(result.errors)
            self._run_count += 1
        if result.enqueued or result.errors:
            self._safe_log(
                "transfer recovery scan: "
                f"scanned={result.scanned} enqueued={result.enqueued} "
                f"already_queued={result.already_queued} errors={len(result.errors)}"
            )
        return result

    def status(self) -> dict[str, Any]:
        thread = self._thread
        with self._state_lock:
            return {
                "running": bool(thread is not None and thread.is_alive()),
                "interval_seconds": self.interval_seconds,
                "batch_size": self.batch_size,
                "last_started_at": self._last_started_at,
                "last_finished_at": self._last_finished_at,
                "last_result": dict(self._last_result),
                "last_error": self._last_error,
                "run_count": self._run_count,
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - error already persisted/logged
                pass
            if self._stop.wait(self.interval_seconds):
                return

    def _safe_log(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:  # noqa: BLE001 - logging must not stop recovery
            return


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


__all__ = [
    "RecoveryScanResult",
    "TransferRecoveryRuntime",
    "TransferRecoveryScanner",
]
