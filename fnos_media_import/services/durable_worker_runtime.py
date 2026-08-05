from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable


TaskHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]

WORKER_COMPLETED = "completed"
WORKER_DEFERRED = "deferred"
WORKER_RETRYABLE = "retryable"
WORKER_BUSINESS_FAILED = "business_failed"


class WorkerLeaseLostError(RuntimeError):
    """Raised when a task can no longer be safely finalized by this worker."""


class DurableWorkerRuntime:
    """Poll durable tasks without allowing transient queue failures to kill the worker.

    Handlers may return an explicit ``worker_outcome`` with one of:

    * ``completed``: persist the result as completed.
    * ``deferred``: release it to pending at ``delay_seconds`` or
      ``retry_after_seconds`` without consuming an attempt.
    * ``retryable``: consume the attempt and retry after the requested delay.
    * ``business_failed``: persist a terminal business failure immediately.

    For backwards compatibility, ``deferred=True``, ``retryable=True`` and
    ``success=False`` are mapped to those outcomes when no explicit outcome is
    present.
    """

    def __init__(
        self,
        *,
        repository: Any,
        owner_id: str,
        handlers: dict[str, TaskHandler] | None = None,
        poll_seconds: float = 1.0,
        lease_seconds: int = 120,
        retry_delay_seconds: int = 30,
        log: Callable[[str], None] | None = None,
        retention_days: int = 7,
        cleanup_interval_seconds: int = 3600,
        error_backoff_seconds: float = 0.5,
        max_error_backoff_seconds: float = 30.0,
    ) -> None:
        self.repository = repository
        self.owner_id = owner_id
        self.handlers = dict(handlers or {})
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.lease_seconds = max(1, int(lease_seconds))
        self.retry_delay_seconds = max(0, int(retry_delay_seconds))
        self.log = log or (lambda _message: None)
        self.retention_days = max(1, int(retention_days))
        self.cleanup_interval_seconds = max(60, int(cleanup_interval_seconds))
        self.error_backoff_seconds = max(0.05, float(error_backoff_seconds))
        self.max_error_backoff_seconds = max(
            self.error_backoff_seconds,
            float(max_error_backoff_seconds),
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._started_at = ""
        self._stopped_at = ""
        self._last_heartbeat_at = ""
        self._last_heartbeat_monotonic: float | None = None
        self._last_claim_at = ""
        self._last_task_at = ""
        self._last_error: dict[str, str] = {}
        self._last_task_error: dict[str, str] = {}
        self._consecutive_runtime_errors = 0
        self._error_generation = 0
        self._processed_count = 0
        self._completed_count = 0
        self._deferred_count = 0
        self._retryable_count = 0
        self._business_failed_count = 0

    def register(self, task_type: str, handler: TaskHandler) -> None:
        self.handlers[str(task_type).strip()] = handler

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"durable-worker-{self.owner_id}",
                daemon=True,
            )
            self._thread.start()
            return True

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def status(self) -> dict[str, Any]:
        thread = self._thread
        running = bool(thread and thread.is_alive())
        now = time.monotonic()
        health_timeout = max(
            10.0,
            self.poll_seconds * 10,
            self.max_error_backoff_seconds * 2,
            float(self.lease_seconds),
        )
        with self._state_lock:
            heartbeat_age = (
                max(0.0, now - self._last_heartbeat_monotonic)
                if self._last_heartbeat_monotonic is not None
                else None
            )
            stale = heartbeat_age is None or heartbeat_age > health_timeout
            return {
                "runtime_running": running,
                "stop_requested": self._stop.is_set(),
                "owner_id": self.owner_id,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "last_heartbeat_at": self._last_heartbeat_at,
                "heartbeat_age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
                "heartbeat_timeout_seconds": round(health_timeout, 3),
                "heartbeat_stale": stale,
                "last_claim_at": self._last_claim_at,
                "last_task_at": self._last_task_at,
                "last_error": dict(self._last_error),
                "last_task_error": dict(self._last_task_error),
                "consecutive_runtime_errors": self._consecutive_runtime_errors,
                "processed_count": self._processed_count,
                "completed_count": self._completed_count,
                "deferred_count": self._deferred_count,
                "retryable_count": self._retryable_count,
                "business_failed_count": self._business_failed_count,
                "healthy": running and not stale and self._consecutive_runtime_errors == 0,
            }

    def run_once(self) -> bool:
        try:
            task = self.repository.claim(
                self.owner_id,
                lease_seconds=self.lease_seconds,
                task_types=list(self.handlers) or None,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_runtime_error("claim", exc)
            return False
        if not task:
            return False

        task_id = int(task["id"])
        task_type = str(task.get("task_type") or "")
        self._record_claim()
        handler = self.handlers.get(task_type)
        if not handler:
            transitioned = self._transition_failure(
                task_id,
                f"No Worker handler registered for task type: {task_type}",
                terminal=True,
                result={"worker_outcome": WORKER_BUSINESS_FAILED, "task_type": task_type},
            )
            if transitioned:
                self._record_outcome(WORKER_BUSINESS_FAILED)
            return True

        try:
            raw_result = self._execute_with_heartbeat(task_id, handler, task)
            if raw_result is None:
                result: dict[str, Any] = {}
            elif isinstance(raw_result, dict):
                result = raw_result
            else:
                raise TypeError("Worker handler result must be a dictionary or None")
        except Exception as exc:  # noqa: BLE001
            self._record_task_error(task_id, task_type, exc)
            transitioned = self._transition_failure(
                task_id,
                str(exc),
                retry_delay_seconds=self.retry_delay_seconds,
                result={"worker_outcome": WORKER_RETRYABLE, "message": str(exc)},
            )
            if transitioned:
                self._record_outcome(WORKER_RETRYABLE)
            return True

        outcome = _worker_outcome(result)
        delay = _result_delay_seconds(result, self.retry_delay_seconds)
        if outcome == WORKER_DEFERRED:
            transitioned = self._transition_deferred(task_id, result, delay)
        elif outcome == WORKER_RETRYABLE:
            transitioned = self._transition_failure(
                task_id,
                _result_error(result, "Worker task requested a retry"),
                retry_delay_seconds=delay,
                result=result,
            )
        elif outcome == WORKER_BUSINESS_FAILED:
            transitioned = self._transition_failure(
                task_id,
                _result_error(result, "Worker task reported a business failure"),
                terminal=True,
                result=result,
            )
        else:
            transitioned = self._transition_completed(task_id, result)
        if transitioned:
            self._record_outcome(outcome)
        return True

    def _execute_with_heartbeat(
        self,
        task_id: int,
        handler: TaskHandler,
        task: dict[str, Any],
    ) -> dict[str, Any] | None:
        renew = getattr(self.repository, "renew", None)
        if not callable(renew):
            return handler(task.get("payload") or {}, task)
        stopped = threading.Event()
        lost = threading.Event()
        interval = max(0.1, min(30.0, self.lease_seconds / 3))

        def heartbeat() -> None:
            while not stopped.wait(interval):
                renewed = False
                for attempt in range(1, 4):
                    try:
                        renewed = bool(
                            renew(task_id, self.owner_id, lease_seconds=self.lease_seconds)
                        )
                        break
                    except Exception as exc:  # noqa: BLE001
                        self._record_runtime_error("heartbeat", exc)
                        if attempt >= 3:
                            lost.set()
                            return
                        delay = min(
                            max(0.05, interval / 2),
                            self.error_backoff_seconds * (2 ** (attempt - 1)),
                        )
                        if stopped.wait(delay):
                            return
                if not renewed:
                    lost.set()
                    self._record_runtime_error(
                        "heartbeat",
                        WorkerLeaseLostError(f"Worker task #{task_id} lease renewal was rejected"),
                    )
                    return
                self._touch_runtime_heartbeat()

        thread = threading.Thread(
            target=heartbeat,
            name=f"worker-heartbeat-{task_id}",
            daemon=True,
        )
        thread.start()
        try:
            result = handler(task.get("payload") or {}, task)
            if lost.is_set():
                raise WorkerLeaseLostError(f"Worker task #{task_id} lost its lease during execution")
            return result
        finally:
            stopped.set()
            thread.join(timeout=min(1.0, interval + 0.1))

    def _transition_completed(self, task_id: int, result: dict[str, Any]) -> bool:
        return self._repository_transition(
            "complete",
            task_id,
            lambda: self.repository.complete(task_id, self.owner_id, result),
        )

    def _transition_deferred(self, task_id: int, result: dict[str, Any], delay_seconds: int) -> bool:
        defer = getattr(self.repository, "defer", None)
        if not callable(defer):
            return self._transition_failure(
                task_id,
                "Worker repository does not support deferred tasks",
                retry_delay_seconds=delay_seconds,
                result=result,
            )
        return self._repository_transition(
            "defer",
            task_id,
            lambda: defer(
                task_id,
                self.owner_id,
                delay_seconds=delay_seconds,
                result=result,
            ),
        )

    def _transition_failure(
        self,
        task_id: int,
        error: str,
        *,
        retry_delay_seconds: int | None = None,
        terminal: bool = False,
        result: dict[str, Any] | None = None,
    ) -> bool:
        delay = self.retry_delay_seconds if retry_delay_seconds is None else retry_delay_seconds

        def fail() -> bool:
            try:
                return bool(
                    self.repository.fail(
                        task_id,
                        self.owner_id,
                        error,
                        retry_delay_seconds=max(0, int(delay)),
                        terminal=terminal,
                        result=result,
                    )
                )
            except TypeError as exc:
                # Preserve compatibility with repository doubles and older
                # integrations that have not added the terminal/result options.
                if "unexpected keyword" not in str(exc):
                    raise
                return bool(
                    self.repository.fail(
                        task_id,
                        self.owner_id,
                        error,
                        retry_delay_seconds=0 if terminal else max(0, int(delay)),
                    )
                )

        return self._repository_transition("fail", task_id, fail)

    def _repository_transition(
        self,
        stage: str,
        task_id: int,
        transition: Callable[[], bool],
    ) -> bool:
        for attempt in range(1, 4):
            try:
                updated = bool(transition())
            except Exception as exc:  # noqa: BLE001
                self._record_runtime_error(stage, exc)
                if attempt >= 3:
                    return False
                delay = min(
                    self.max_error_backoff_seconds,
                    self.error_backoff_seconds * (2 ** (attempt - 1)),
                )
                if self._stop.wait(delay):
                    return False
                continue
            if not updated:
                self._record_runtime_error(
                    stage,
                    WorkerLeaseLostError(
                        f"Worker task #{task_id} {stage} transition was rejected; lease may be lost"
                    ),
                )
            return updated
        return False

    def _run(self) -> None:
        self._mark_started()
        self._safe_log(f"durable worker started: owner={self.owner_id}")
        next_cleanup = 0.0
        try:
            while not self._stop.is_set():
                self._touch_runtime_heartbeat()
                error_generation = self._current_error_generation()
                handled = False
                try:
                    now = time.monotonic()
                    if now >= next_cleanup:
                        self._cleanup_history()
                        next_cleanup = now + self.cleanup_interval_seconds
                    handled = self.run_once()
                except Exception as exc:  # noqa: BLE001
                    # A programming error in an iteration is still isolated so
                    # the only worker thread is not silently lost.
                    self._record_runtime_error("run_loop", exc)
                self._touch_runtime_heartbeat()
                if self._current_error_generation() == error_generation:
                    self._reset_runtime_errors()
                if self._stop.is_set():
                    break
                if self._current_error_generation() != error_generation:
                    self._stop.wait(self._error_backoff_delay())
                elif not handled:
                    self._stop.wait(self.poll_seconds)
        finally:
            self._mark_stopped()
            self._safe_log(f"durable worker stopped: owner={self.owner_id}")

    def _cleanup_history(self) -> int:
        prune = getattr(self.repository, "prune_terminal", None)
        if not callable(prune):
            return 0
        try:
            deleted = int(prune(retention_days=self.retention_days, limit=500))
            if deleted:
                self._safe_log(f"durable worker pruned {deleted} terminal tasks")
            return deleted
        except Exception as exc:  # noqa: BLE001
            self._record_runtime_error("cleanup", exc)
            return 0

    def _mark_started(self) -> None:
        now = _utc_now_text()
        with self._state_lock:
            self._started_at = now
            self._stopped_at = ""
            self._last_heartbeat_at = now
            self._last_heartbeat_monotonic = time.monotonic()

    def _mark_stopped(self) -> None:
        with self._state_lock:
            self._stopped_at = _utc_now_text()

    def _touch_runtime_heartbeat(self) -> None:
        with self._state_lock:
            self._last_heartbeat_at = _utc_now_text()
            self._last_heartbeat_monotonic = time.monotonic()

    def _record_claim(self) -> None:
        with self._state_lock:
            self._last_claim_at = _utc_now_text()

    def _record_outcome(self, outcome: str) -> None:
        with self._state_lock:
            self._processed_count += 1
            self._last_task_at = _utc_now_text()
            if outcome == WORKER_DEFERRED:
                self._deferred_count += 1
            elif outcome == WORKER_RETRYABLE:
                self._retryable_count += 1
            elif outcome == WORKER_BUSINESS_FAILED:
                self._business_failed_count += 1
            else:
                self._completed_count += 1

    def _record_runtime_error(self, stage: str, exc: BaseException) -> None:
        message = str(exc) or exc.__class__.__name__
        error = {"stage": stage, "message": message, "at": _utc_now_text()}
        with self._state_lock:
            self._last_error = error
            self._consecutive_runtime_errors += 1
            self._error_generation += 1
        self._safe_log(f"durable worker {stage} error: {message}")

    def _record_task_error(self, task_id: int, task_type: str, exc: BaseException) -> None:
        message = str(exc) or exc.__class__.__name__
        with self._state_lock:
            self._last_task_error = {
                "task_id": str(task_id),
                "task_type": task_type,
                "message": message,
                "at": _utc_now_text(),
            }
        self._safe_log(f"worker task #{task_id} execution failed: {message}")

    def _current_error_generation(self) -> int:
        with self._state_lock:
            return self._error_generation

    def _reset_runtime_errors(self) -> None:
        with self._state_lock:
            self._consecutive_runtime_errors = 0

    def _error_backoff_delay(self) -> float:
        with self._state_lock:
            consecutive = max(1, self._consecutive_runtime_errors)
        exponent = min(10, consecutive - 1)
        return min(self.max_error_backoff_seconds, self.error_backoff_seconds * (2**exponent))

    def _safe_log(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:  # noqa: BLE001
            # Logging must never be able to terminate the sole worker thread.
            return


def _worker_outcome(result: dict[str, Any]) -> str:
    explicit = ""
    for key in ("worker_outcome", "outcome", "status"):
        candidate = str(result.get(key) or "").strip().lower()
        if candidate in {
            WORKER_COMPLETED,
            WORKER_DEFERRED,
            WORKER_RETRYABLE,
            WORKER_BUSINESS_FAILED,
            "complete",
            "success",
            "retry",
            "failed",
            "failure",
            "terminal_failed",
        }:
            explicit = candidate
            break
    aliases = {
        "complete": WORKER_COMPLETED,
        "success": WORKER_COMPLETED,
        "retry": WORKER_RETRYABLE,
        "failed": WORKER_BUSINESS_FAILED,
        "failure": WORKER_BUSINESS_FAILED,
        "terminal_failed": WORKER_BUSINESS_FAILED,
    }
    explicit = aliases.get(explicit, explicit)
    if explicit == WORKER_COMPLETED and result.get("success") is False:
        return WORKER_BUSINESS_FAILED
    if explicit in {
        WORKER_COMPLETED,
        WORKER_DEFERRED,
        WORKER_RETRYABLE,
        WORKER_BUSINESS_FAILED,
    }:
        return explicit
    if bool(result.get("deferred")):
        return WORKER_DEFERRED
    if str(result.get("status") or "").strip().lower() in {
        "deferred",
        "waiting",
        "waiting_openlist",
        "stabilizing",
        "pending",
    } and bool(result.get("queued", True)):
        return WORKER_DEFERRED
    if bool(result.get("retryable")):
        return WORKER_RETRYABLE
    if result.get("success") is False:
        return WORKER_BUSINESS_FAILED
    return WORKER_COMPLETED


def _result_delay_seconds(result: dict[str, Any], default: int) -> int:
    for key in ("retry_after_seconds", "delay_seconds"):
        value = result.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return max(1, int(float(value)))
        except (TypeError, ValueError, OverflowError):
            continue
    return max(1, int(default or 1))


def _result_error(result: dict[str, Any], default: str) -> str:
    for key in ("error_message", "error", "message"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    return default


def _utc_now_text() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
