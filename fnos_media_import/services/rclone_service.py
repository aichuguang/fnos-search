from __future__ import annotations

import os
import posixpath
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..constants import (
    COMPLETION_STAGE_REVIEW,
    COMPLETION_STAGE_WAITING_ORGANIZER,
    EVENT_ERROR,
    EVENT_INFO,
    EVENT_WARN,
    JOB_FAILED,
    JOB_REVIEW,
    JOB_WAITING_ORGANIZER,
    JOB_WAITING_TRANSFER,
    RCLONE_RUN_FAILED,
    RCLONE_RUN_SUCCESS,
    ROUTE_QUARK_TO_MOBILE,
)
from ..database import Database
from ..fnos_paths import match_actual_dirs, match_library, normalize_library_name, normalize_remote_hint, path_tails, split_values
from ..media_path_rules import EPISODIC_CATEGORY_KEYS, FALLBACK_ORGANIZE_DIR, build_standard_naming_plan, sanitize_resource_dir_name
from ..media.fnos import FnosMediaRefresher
from ..storage_paths import cmcc_upload_root
from .import_staging_service import (
    rclone_staging_run_from_job,
    staging_category_root,
    validated_staging_plan_from_job,
)
from .rclone_environment import RcloneEnvironmentChecker
from .rclone_directory_mapping import RcloneDirectoryMappingValidator
from .rclone_log_sink import RcloneLogSink
from .rclone_process_runner import RcloneProcessRunner
from .rclone_process_controller import RcloneProcessController
from .rclone_run_queue import RcloneRunQueue
from .rclone_run_state import RcloneRunState
from .rclone_run_completion_service import RcloneRunCompletionService
from .rclone_job_feasibility import RcloneJobFeasibilityEvaluator
from .rclone_scheduler import RcloneScheduler
from .rclone_worker_command import RcloneWorkerCommandBuilder
from .rclone_category_finalizer import RcloneCategoryFinalizer
from .rclone_ready_items_completion_service import RcloneReadyItemsCompletionService
from .rclone_cancelled_task_cleanup_service import RcloneCancelledTaskCleanupService
from .rclone_waiting_job_recovery_service import RcloneWaitingJobRecoveryService
from .rclone_run_import_finalizer import RcloneRunImportFinalizer


class RcloneService:
    """在 Web 系统内触发 rclone 搬运脚本。

    第一版采用“脚本托管”方式：Web 负责启动、互斥、状态和日志，真正搬运逻辑
    仍由 scripts/fnos_rclone_worker.sh 执行。这样后续可以逐步把脚本逻辑迁移到
    Python，而不会破坏当前已经验证过的 rclone 命令链路。
    """

    def __init__(
        self,
        config: dict[str, Any],
        base_dir: Path,
        fnos_config: dict[str, Any],
        db: Database | None = None,
        categories: dict[str, dict[str, Any]] | None = None,
        cmcc_upload_config: dict[str, Any] | None = None,
        cloud139_config: dict[str, Any] | None = None,
        owner_id: str = "",
    ):
        self.config = config
        self.base_dir = base_dir
        self.fnos_config = fnos_config
        self.db = db
        self.categories = categories or {}
        self.cmcc_upload_config = cmcc_upload_config or {}
        self.cloud139_config = cloud139_config or {}
        self.enabled = bool(config.get("enabled", True))
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.worker_thread: threading.Thread | None = None
        self.run_state = RcloneRunState()
        self.log_sink = RcloneLogSink(
            database=self.db,
            current_run_id=lambda: self.run_state.current_run_id,
            max_lines=config.get("log_lines", 500),
        )
        self.run_queue = RcloneRunQueue()
        self._run_ready_handler: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None
        self._direct_ready_handler: Callable[[dict[str, Any], str], Any] | None = None
        self._pending_run_ready_results: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self._startup_recovery_activated = False
        self._staging_retry_timers: dict[int, threading.Timer] = {}
        self._staging_retry_attempts: dict[int, int] = {}
        self._active_staging_job_id = 0
        self._active_full_staging_job_id = 0
        # A queued full-staging run is briefly removed from ``run_queue``
        # while its persisted plan and latest Job status are revalidated.
        # Keep that hand-off visible so an idempotent retry cannot enqueue the
        # same Job again in the pop-before-start window.
        self._reserved_full_staging_job_id = 0
        self._active_run_reason = ""
        self._stop_requested_job_ids: set[int] = set()
        self._shutdown_requested = False
        self.owner_id = str(owner_id or f"rclone-scheduler-{os.getpid()}-{id(self)}")
        self.scheduler = RcloneScheduler(
            database=self.db,
            owner_id=self.owner_id,
            submit=self.start,
            log=self._append_log,
        )
        self.environment_checker = RcloneEnvironmentChecker(self.config)
        self.process_runner = RcloneProcessRunner()
        self.process_controller = RcloneProcessController()
        self.worker_command = RcloneWorkerCommandBuilder(self.config, self.base_dir)
        self.run_completion = RcloneRunCompletionService(
            database=self.db,
            state=self.run_state,
            state_lock=self.lock,
            log=self._append_log,
            now=self._now,
            finalize_imports=self._finalize_run_imports,
        )
        self.category_finalizer = RcloneCategoryFinalizer(
            database=self.db,
            categories=lambda: self.categories,
            category_key=self._rclone_category_key_from_callback,
            event_matches=self._rclone_event_matches_category,
            feasibility=self._rclone_job_feasibility,
            finish_ready=self._finish_ready_rclone_items,
        )
        self.ready_items_completion = RcloneReadyItemsCompletionService(
            database=self.db,
            config=lambda: self.config,
            refresh_media=self._refresh_media_after_rclone,
        )
        self.cancelled_task_cleanup = RcloneCancelledTaskCleanupService(
            database=self.db,
            cancel_job=self.cancel_job,
            specs_from_events=self._cleanup_specs_from_events,
            specs_from_title=self._cleanup_specs_from_title,
            dedupe_specs=self._dedupe_cleanup_specs,
            delete_remote_file=self._rclone_deletefile,
            cleanup_remote_dirs=self._cleanup_empty_remote_dirs,
            delete_temp_file=self._delete_local_temp_file,
            cleanup_temp_dirs=self._delete_empty_local_temp_dirs,
        )
        self.waiting_job_recovery = RcloneWaitingJobRecoveryService(
            database=self.db,
            state_lock=self.lock,
            is_running=self.is_running_locked,
            file_identity=self._rclone_event_file_identity,
            is_pollution_file=self._is_rclone_log_pollution_file,
            finalize_run=self._finalize_run_imports,
        )
        self.run_import_finalizer = RcloneRunImportFinalizer(
            database=self.db,
            categories=lambda: self.categories,
            log=self._append_log,
            feasibility=self._rclone_job_feasibility,
            finish_ready=self._finish_ready_rclone_items,
            dispatch_ready=self._dispatch_run_ready_to_organizer,
        )

    def set_run_ready_handler(
        self,
        handler: Callable[[dict[str, Any], dict[str, Any]], Any],
        *,
        direct_handler: Callable[[dict[str, Any], str], Any] | None = None,
    ) -> None:
        """Connect run-end fallback completion to the app-level Organizer dispatcher."""

        with self.lock:
            self._run_ready_handler = handler
            self._direct_ready_handler = direct_handler
            pending = list(self._pending_run_ready_results)
            self._pending_run_ready_results.clear()
        for category_refresh, payload in pending:
            self._dispatch_run_ready_to_organizer(category_refresh, payload)

    def activate_startup_recovery(self) -> dict[str, Any]:
        """Run durable staging recovery once after the app is fully assembled."""

        with self.lock:
            if getattr(self, "_startup_recovery_activated", False):
                return {
                    "success": True,
                    "skipped": True,
                    "message": "rclone 启动恢复已执行，无需重复触发",
                }
            self._startup_recovery_activated = True
        try:
            cloud139 = self.recover_submitted_cloud139_staging_dispatches()
            organizer = self.recover_waiting_organizer_dispatches()
            transfer = self.recover_unstarted_staging_jobs()
        except Exception:
            # 初始化期间若数据库暂时不可用，允许上层稍后显式重试；正常成功后
            # 始终保持一次性语义，避免重复启动同一批持久化任务。
            with self.lock:
                self._startup_recovery_activated = False
            raise
        success = (
            cloud139.get("success") is not False
            and organizer.get("success") is not False
            and transfer.get("success") is not False
        )
        return {
            "success": success,
            "message": "rclone 启动恢复已完成" if success else "rclone 启动恢复存在未完成任务",
            "submitted_cloud139": cloud139,
            "waiting_organizer": organizer,
            "waiting_transfer": transfer,
        }

    def recover_submitted_cloud139_staging_dispatches(self, limit: int = 200) -> dict[str, Any]:
        """Recover native 139 saves that committed before Organizer handoff was persisted."""

        if not getattr(self, "db", None):
            return {
                "success": True,
                "skipped": True,
                "message": "数据库未初始化，已跳过 139 暂存补投扫描",
                "recovered_job_ids": [],
            }
        direct_handler = getattr(self, "_direct_ready_handler", None)
        if not callable(direct_handler):
            return {
                "success": True,
                "skipped": True,
                "message": "Organizer 直转分发器尚未注册",
                "recovered_job_ids": [],
            }
        jobs: list[dict[str, Any]] = []
        offset = 0
        wanted = max(1, min(int(limit or 200), 2000))
        while len(jobs) < wanted:
            page_size = min(200, wanted - len(jobs))
            page = self.db.list_jobs(limit=page_size, status="submitted", offset=offset)
            jobs.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)

        recovered: list[int] = []
        reviewed: list[int] = []
        for job in jobs:
            if str(job.get("source_type") or "").strip().lower() != "cloud139":
                continue
            raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
            if not isinstance(raw_data.get("save"), dict) or not raw_data.get("save"):
                continue
            job_id = self._int_value(job.get("id"))
            if job_id <= 0:
                continue
            try:
                plan = validated_staging_plan_from_job(job)
            except ValueError as exc:
                self._mark_invalid_staging_plan_review(
                    job,
                    context="服务重启恢复 139 暂存任务",
                    error=exc,
                )
                reviewed.append(job_id)
                continue
            if str(plan.get("route") or "").strip().lower() != "cloud139_direct":
                continue
            try:
                dispatch_result = direct_handler(
                    {"success": True, "job": job, "created": False},
                    "startup_recover_cloud139_staging",
                )
            except Exception as exc:  # noqa: BLE001
                dispatch_result = {"success": False, "message": f"服务重启后补投 139 Organizer 异常：{exc}"}
            queued = bool(
                isinstance(dispatch_result, dict)
                and dispatch_result.get("success") is True
                and dispatch_result.get("queued")
                and not dispatch_result.get("skipped")
            )
            if not queued:
                message = str(
                    dispatch_result.get("message")
                    if isinstance(dispatch_result, dict)
                    else "Organizer 未返回可用的入队结果"
                ).strip() or "Organizer 未成功创建标准化任务"
                self._mark_run_ready_items_review(
                    [{"job_id": job_id, "job": job}],
                    f"服务重启后补投 139 Organizer 失败：{message}",
                )
                reviewed.append(job_id)
                continue
            self.db.add_event(
                job_id,
                EVENT_INFO,
                "服务重启后已恢复 139 暂存任务并补投 Organizer",
                {"startup_recovery": True, "staging_plan_version": plan.get("version")},
            )
            recovered.append(job_id)
        return {
            "success": not reviewed,
            "message": f"已恢复 {len(recovered)} 个 139 暂存任务",
            "recovered_job_ids": recovered,
            "review_job_ids": reviewed,
        }

    def recover_unstarted_staging_jobs(self, limit: int = 500) -> dict[str, Any]:
        """Requeue durable staged jobs that were lost with the in-memory FIFO."""

        if not getattr(self, "db", None):
            return {"success": False, "message": "数据库未初始化", "requeued_job_ids": []}
        jobs: list[dict[str, Any]] = []
        wanted = max(1, min(int(limit or 500), 5000))
        for status in (JOB_WAITING_TRANSFER, "transferring", JOB_FAILED):
            offset = 0
            while len(jobs) < wanted:
                page_size = min(200, wanted - len(jobs))
                page = self.db.list_jobs(
                    limit=page_size,
                    status=status,
                    offset=offset,
                )
                jobs.extend(page)
                if len(page) < page_size:
                    break
                offset += len(page)
            if len(jobs) >= wanted:
                break

        requeued: list[int] = []
        skipped_with_completion: list[int] = []
        organizer_recovered: list[int] = []
        organizer_reviewed: list[int] = []
        invalid_plan_reviewed: list[int] = []
        exhausted_retry_job_ids: list[int] = []
        requeue_failed_job_ids: list[int] = []
        failed_without_staging_evidence: list[int] = []
        unique_jobs: dict[int, dict[str, Any]] = {}
        for item in jobs:
            item_id = self._int_value(item.get("id"))
            if item_id > 0:
                unique_jobs[item_id] = item
        for job in reversed(list(unique_jobs.values())):
            source_type = str(job.get("source_type") or "").strip().lower()
            target_route = str(job.get("target_route") or "").strip().lower()
            if source_type not in {"quark", "uc"} and target_route != "quark_to_mobile":
                continue
            job_id = self._int_value(job.get("id"))
            if job_id <= 0:
                continue
            try:
                staging_run = rclone_staging_run_from_job(job)
            except ValueError as exc:
                self._mark_invalid_staging_plan_review(
                    job,
                    context="服务重启恢复 rclone 暂存任务",
                    error=exc,
                )
                invalid_plan_reviewed.append(job_id)
                continue
            if not staging_run:
                continue
            events = self._all_rclone_file_events(job_id=job_id)
            if (
                str(job.get("status") or "").strip().lower() == JOB_FAILED
                and not self._failed_staging_job_has_recovery_evidence(job, events)
            ):
                failed_without_staging_evidence.append(job_id)
                continue
            terminal_events = self._latest_terminal_file_events(events, job=job)
            verdict = self._rclone_job_feasibility(job, terminal_events, 0)
            expected_count = self._int_value(verdict.get("expected_file_count"))
            completed_count = self._int_value(verdict.get("completed_file_count"))
            if verdict.get("ready") and expected_count > 0 and completed_count >= expected_count:
                skipped_with_completion.append(job_id)
                handoff = self._handoff_completed_staging_job_to_organizer(
                    job,
                    terminal_events,
                    verdict,
                    trigger="startup_recover_completed_staging",
                    run_id=0,
                )
                if handoff.get("success"):
                    organizer_recovered.append(job_id)
                else:
                    organizer_reviewed.append(job_id)
                continue
            retry_attempts = self._staging_retry_attempts_from_job(job)
            retry_max_attempts = self._staging_retry_max_attempts()
            if retry_attempts >= retry_max_attempts:
                self._mark_staging_retry_exhausted(
                    job,
                    attempts=retry_attempts,
                    max_attempts=retry_max_attempts,
                    run_id=None,
                    exit_code=0,
                    verdict={"message": "服务重启时发现任务级 rclone 自动补跑次数已经耗尽"},
                )
                exhausted_retry_job_ids.append(job_id)
                continue
            result = self.start(
                reason=f"startup_recover_staging_job:{job_id}",
                category_filter=staging_run["category"],
                staging_run=staging_run,
            )
            self.db.add_event(
                job_id,
                EVENT_INFO if result.get("success") else EVENT_WARN,
                result.get("message") or "服务重启后已重新提交任务级 rclone 搬运",
                {"startup_recovery": True, "staging_run": staging_run, "rclone": result},
            )
            if result.get("success"):
                requeued.append(job_id)
            else:
                requeue_failed_job_ids.append(job_id)
        return {
            "success": not organizer_reviewed and not invalid_plan_reviewed and not requeue_failed_job_ids,
            "message": f"已重新提交 {len(requeued)} 个任务级 rclone 搬运",
            "requeued_job_ids": requeued,
            "requeue_failed_job_ids": requeue_failed_job_ids,
            "completed_evidence_job_ids": skipped_with_completion,
            "organizer_recovered_job_ids": organizer_recovered,
            "organizer_review_job_ids": organizer_reviewed,
            "invalid_plan_review_job_ids": invalid_plan_reviewed,
            "exhausted_retry_job_ids": exhausted_retry_job_ids,
            "failed_without_staging_evidence_job_ids": failed_without_staging_evidence,
        }

    def recover_waiting_organizer_dispatches(self, limit: int = 200) -> dict[str, Any]:
        """Recreate Organizer handoffs lost after the durable status update."""

        if not getattr(self, "db", None):
            return {"success": False, "message": "数据库未初始化", "recovered_job_ids": []}
        jobs: list[dict[str, Any]] = []
        offset = 0
        page_size = max(1, min(int(limit or 200), 500))
        while len(jobs) < max(1, int(limit or 200)):
            page = self.db.list_jobs(
                limit=min(page_size, max(1, int(limit or 200)) - len(jobs)),
                status=JOB_WAITING_ORGANIZER,
                offset=offset,
            )
            jobs.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)

        recovered: list[int] = []
        reviewed: list[int] = []
        for job in jobs:
            job_id = self._int_value(job.get("id"))
            if job_id <= 0:
                continue
            try:
                plan = validated_staging_plan_from_job(job)
            except ValueError as exc:
                self._mark_invalid_staging_plan_review(
                    job,
                    context="服务重启恢复等待 Organizer 的任务",
                    error=exc,
                )
                reviewed.append(job_id)
                continue
            existing_tasks = self.db.list_organizer_tasks_by_job(job_id, limit=1)
            if existing_tasks:
                continue
            raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
            completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
            run_id = self._int_value(completion.get("rclone_run_id"))
            persisted_route = str(job.get("target_route") or "").strip().lower()
            is_rclone_route = str((plan or {}).get("route") or persisted_route).strip().lower() == ROUTE_QUARK_TO_MOBILE
            if not plan and is_rclone_route:
                # Legacy Quark jobs have no immutable task root. Treating them as
                # direct/offline imports can make Organizer scan a shared category
                # directory and attach unrelated files. Stop the silent stall, but
                # require an administrator to verify the exact historical paths.
                self._mark_run_ready_items_review(
                    [{"job_id": job_id, "job": job}],
                    "服务重启后发现历史夸克任务缺少 staging_plan，无法安全确认任务级目录，请人工核对后重试整理",
                )
                reviewed.append(job_id)
                continue
            if not is_rclone_route:
                direct_handler = getattr(self, "_direct_ready_handler", None)
                if not callable(direct_handler):
                    dispatch_result = {
                        "success": False,
                        "message": "Organizer 直转分发器尚未注册",
                    }
                else:
                    try:
                        dispatch_result = direct_handler(
                            {"success": True, "job": job, "created": False},
                            "waiting_organizer_startup_recovery",
                        )
                    except Exception as exc:  # noqa: BLE001
                        dispatch_result = {
                            "success": False,
                            "message": f"服务重启后补投 Organizer 异常：{exc}",
                        }
                queued = bool(
                    isinstance(dispatch_result, dict)
                    and dispatch_result.get("success") is True
                    and dispatch_result.get("queued")
                    and not dispatch_result.get("skipped")
                )
                if not queued:
                    message = str(
                        dispatch_result.get("message")
                        if isinstance(dispatch_result, dict)
                        else "Organizer 未返回可用的入队结果"
                    ).strip() or "Organizer 未成功创建标准化任务"
                    self._mark_run_ready_items_review(
                        [{"job_id": job_id, "job": job}],
                        f"服务重启后补投 Organizer 失败：{message}",
                    )
                    reviewed.append(job_id)
                    continue
                self.db.add_event(
                    job_id,
                    EVENT_INFO,
                    (
                        "服务重启后已按历史直转/离线任务证据补投 Organizer 标准化任务"
                        if not plan
                        else "服务重启后已按原直转/离线计划补投 Organizer 标准化任务"
                    ),
                    {"staging_plan_version": plan.get("version") if plan else None},
                )
                recovered.append(job_id)
                continue
            events = self._all_rclone_file_events(job_id=job_id) if is_rclone_route else []
            terminal_events = self._latest_terminal_file_events(events, job=job)
            verdict = (
                self._rclone_job_feasibility(job, terminal_events, 0)
                if is_rclone_route
                else {
                    "ready": True,
                    "status": JOB_WAITING_ORGANIZER,
                    "message": "直转/离线任务已完成，服务重启后补投 Organizer",
                }
            )
            if not verdict.get("ready"):
                message = f"服务重启后无法确认 rclone 完整搬运证据：{verdict.get('message') or '文件事件不完整'}"
                self._mark_run_ready_items_review([{"job_id": job_id}], message)
                reviewed.append(job_id)
                continue
            completed_events = [
                event
                for event in terminal_events
                if str(event.get("status") or "").strip().lower()
                in RcloneJobFeasibilityEvaluator.COMPLETED_STATUSES
            ]
            category_key = str(job.get("category") or plan.get("category") or "movie").strip()
            category = self.categories.get(category_key, {})
            item = {
                "job_id": job_id,
                "job": job,
                "category": category_key,
                "category_label": category.get("label") or job.get("category_label") or category_key,
                "target_paths": _dedupe_texts(
                    event.get("target_path") for event in completed_events
                ),
                "source_paths": _dedupe_texts(event.get("source_path") for event in completed_events),
                "file_count": len(completed_events),
                "verdict": verdict,
            }
            category_refresh = {
                "success": True,
                "skipped": True,
                "deferred_to_organizer": True,
                "library": category.get("fnos_lib") or category.get("label") or category_key,
                "message": "服务重启后补投 Organizer 接管",
                "completed_items": [item],
            }
            dispatch_result = self._dispatch_run_ready_to_organizer(
                category_refresh,
                {
                    "run_id": run_id,
                    "status": "startup_recovery",
                    "category": category_key,
                    "trigger": "waiting_organizer_startup_recovery",
                },
            )
            if not isinstance(dispatch_result, dict) or dispatch_result.get("success") is not True:
                message = str(
                    dispatch_result.get("message")
                    if isinstance(dispatch_result, dict)
                    else "Organizer 分发器未返回有效结果"
                ).strip() or "Organizer 未确认标准化任务创建成功"
                self._mark_run_ready_items_review(
                    [item],
                    f"服务重启后补投 Organizer 失败：{message}",
                )
                reviewed.append(job_id)
                continue
            self.db.add_event(
                job_id,
                EVENT_INFO,
                "服务重启后已补投 Organizer 标准化任务",
                {"run_id": run_id, "staging_plan_version": plan.get("version")},
            )
            recovered.append(job_id)
        return {
            "success": not reviewed,
            "message": f"已补投 {len(recovered)} 个等待 Organizer 的任务",
            "recovered_job_ids": recovered,
            "review_job_ids": reviewed,
        }

    def _all_rclone_file_events(self, **filters: Any) -> list[dict[str, Any]]:
        if not self.db:
            return []
        loader = getattr(self.db, "list_all_rclone_file_events", None)
        if callable(loader):
            return loader(**filters)
        return self.db.list_rclone_file_events(limit=1000, **filters)

    @classmethod
    def _latest_terminal_file_events(
        cls,
        events: list[dict[str, Any]],
        *,
        job: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return RcloneJobFeasibilityEvaluator.latest_terminal_events(
            events,
            prefer_source=RcloneJobFeasibilityEvaluator.requires_staging_manifest(job or {}),
        )

    def _dispatch_run_ready_to_organizer(
        self,
        category_refresh: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        completed_items = category_refresh.get("completed_items") if isinstance(category_refresh, dict) else None
        if not isinstance(completed_items, list) or not completed_items:
            return None
        queued = False
        with self.lock:
            handler = self._run_ready_handler
            if handler is None:
                self._pending_run_ready_results.append((dict(category_refresh), dict(payload)))
                queued = True
        if queued:
            self._append_log("rclone run 兜底已确认任务，等待 Organizer 分发器就绪")
            return {"success": True, "queued": True, "message": "等待 Organizer 分发器就绪"}
        try:
            return handler(category_refresh, payload)
        except Exception as exc:  # noqa: BLE001
            message = f"rclone run 兜底移交 Organizer 异常：{exc}"
            self._append_log(message)
            self._mark_run_ready_items_review(completed_items, message)
            return {"success": False, "message": message}

    def _handoff_completed_staging_job_to_organizer(
        self,
        job: dict[str, Any],
        terminal_events: list[dict[str, Any]],
        verdict: dict[str, Any],
        *,
        trigger: str,
        run_id: int = 0,
    ) -> dict[str, Any]:
        """Promote durable completion evidence into the Organizer pipeline.

        A process can stop after the final file ACK was persisted but before
        the run/category finalizer updates the Job and creates an Organizer
        task.  Recovery and delayed retry callbacks share this idempotent
        handoff so a complete manifest never remains stranded in a transfer
        state.
        """

        if not self.db:
            return {"success": False, "message": "数据库未初始化，无法补投 Organizer"}
        job_id = self._int_value(job.get("id"))
        if job_id <= 0:
            return {"success": False, "message": "任务 ID 无效，无法补投 Organizer"}
        latest = self.db.get_job(job_id) or job
        status = str(latest.get("status") or "").strip().lower()
        if status == JOB_WAITING_ORGANIZER:
            existing_tasks = self.db.list_organizer_tasks_by_job(job_id, limit=1)
            if existing_tasks:
                return {
                    "success": True,
                    "skipped": True,
                    "job_id": job_id,
                    "message": "任务已存在 Organizer 标准化任务，无需重复补投",
                }
        if status in {
            "organizing",
            "confirming",
            JOB_REVIEW,
            "done",
            "success",
            "skipped_existing",
            "cancelled",
        }:
            return {
                "success": status not in {JOB_REVIEW, "cancelled"},
                "skipped": True,
                "job_id": job_id,
                "message": f"任务状态 {status} 已离开搬运阶段，无需重复补投",
            }

        completed_events = [
            event
            for event in terminal_events
            if str(event.get("status") or "").strip().lower()
            in RcloneJobFeasibilityEvaluator.COMPLETED_STATUSES
        ]
        if not verdict.get("ready") or not completed_events:
            message = str(verdict.get("message") or "rclone 完整性证据不足，无法补投 Organizer")
            self._mark_run_ready_items_review([{"job_id": job_id, "job": latest}], message)
            return {"success": False, "review": True, "job_id": job_id, "message": message}

        category_key = str(latest.get("category") or "movie").strip() or "movie"
        category = self.categories.get(category_key, {})
        resolved_run_id = self._int_value(run_id) or max(
            (self._int_value(event.get("run_id")) for event in completed_events),
            default=0,
        )
        completion_message = "rclone 完整搬运证据已确认，等待 Organizer 标准化整理与标准目录确认"
        raw_data = latest.get("raw_data") if isinstance(latest.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        completion = {
            **completion,
            "stage": COMPLETION_STAGE_WAITING_ORGANIZER,
            "message": completion_message,
            "official_save_path": latest.get("target_path") or completion.get("official_save_path") or "",
            "rclone_run_id": resolved_run_id,
            "rclone_trigger": trigger,
        }
        if not self._update_job_from_snapshot(
            latest,
            status=JOB_WAITING_ORGANIZER,
            error_message="",
            raw_data={**raw_data, "completion": completion},
        ):
            current = self.db.get_job(job_id) or latest
            return {
                "success": False,
                "skipped": True,
                "job_id": job_id,
                "message": f"任务状态已变化为 {current.get('status') or '未知'}，未用过期 rclone 证据补投 Organizer",
            }
        promoted_job = self.db.get_job(job_id) or {
            **latest,
            "status": JOB_WAITING_ORGANIZER,
            "error_message": "",
            "raw_data": {**raw_data, "completion": completion},
        }
        item = {
            "job_id": job_id,
            "job": promoted_job,
            "category": category_key,
            "category_label": category.get("label") or promoted_job.get("category_label") or category_key,
            "target_paths": _dedupe_texts(event.get("target_path") for event in completed_events),
            "source_paths": _dedupe_texts(event.get("source_path") for event in completed_events),
            "file_count": len(completed_events),
            "verdict": verdict,
        }
        category_refresh = {
            "success": True,
            "skipped": True,
            "deferred_to_organizer": True,
            "library": category.get("fnos_lib") or category.get("label") or category_key,
            "message": "rclone 完整证据恢复后补投 Organizer 接管",
            "completed_items": [item],
        }
        dispatch_result = self._dispatch_run_ready_to_organizer(
            category_refresh,
            {
                "run_id": resolved_run_id,
                "status": "completion_evidence_recovery",
                "category": category_key,
                "trigger": trigger,
            },
        )
        if not isinstance(dispatch_result, dict) or dispatch_result.get("success") is not True:
            detail = str(
                dispatch_result.get("message")
                if isinstance(dispatch_result, dict)
                else "Organizer 分发器未返回可用结果"
            ).strip()
            message = f"rclone 完整证据已确认，但补投 Organizer 失败：{detail or '未知错误'}"
            self._mark_run_ready_items_review([item], message)
            return {
                "success": False,
                "review": True,
                "job_id": job_id,
                "message": message,
                "organizer": dispatch_result,
            }
        self.db.add_event(
            job_id,
            EVENT_INFO,
            "rclone 完整搬运证据已恢复并补投 Organizer",
            {
                "trigger": trigger,
                "run_id": resolved_run_id,
                "expected_file_count": verdict.get("expected_file_count"),
                "completed_file_count": verdict.get("completed_file_count"),
                "organizer": dispatch_result,
            },
        )
        return {
            "success": True,
            "queued": True,
            "job_id": job_id,
            "organizer": dispatch_result,
        }

    def _mark_run_ready_items_review(self, completed_items: list[dict[str, Any]], message: str) -> None:
        if not self.db:
            return
        for item in completed_items:
            if not isinstance(item, dict):
                continue
            try:
                job_id = int(item.get("job_id") or (item.get("job") or {}).get("id") or 0)
            except (TypeError, ValueError):
                job_id = 0
            if job_id <= 0:
                continue
            job = self.db.get_job(job_id) or {}
            raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
            completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
            if not self._update_job_from_snapshot(
                job,
                status=JOB_REVIEW,
                error_message=message,
                raw_data={
                    **raw_data,
                    "completion": {
                        **completion,
                        "stage": COMPLETION_STAGE_REVIEW,
                        "message": message,
                        "retryable": True,
                    },
                },
            ):
                continue
            self.db.add_event(
                job_id,
                EVENT_WARN,
                message,
                {"rclone_run_fallback": True, "retryable": True},
            )

    def _mark_invalid_staging_plan_review(
        self,
        job: dict[str, Any],
        *,
        context: str,
        error: Exception,
    ) -> None:
        job_id = self._int_value(job.get("id"))
        if job_id <= 0:
            return
        message = f"{context}时发现任务固化 staging_plan 无效，已转人工审核：{error}"
        self._mark_run_ready_items_review([{"job_id": job_id, "job": job}], message)

    @classmethod
    def _failed_staging_job_has_recovery_evidence(
        cls,
        job: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> bool:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        manifest = (
            raw_data.get("rclone_staging_manifest")
            if isinstance(raw_data.get("rclone_staging_manifest"), dict)
            else {}
        )
        if manifest or cls._staging_retry_attempts_from_job(job) > 0:
            return True
        return any(
            isinstance(event, dict)
            and bool(
                str(event.get("source_path") or "").strip()
                or str(event.get("target_path") or "").strip()
                or str(event.get("filename") or "").strip()
            )
            for event in events
        )

    def apply_runtime_config(
        self,
        config: dict[str, Any],
        fnos_config: dict[str, Any],
        categories: dict[str, dict[str, Any]] | None = None,
        cmcc_upload_config: dict[str, Any] | None = None,
        cloud139_config: dict[str, Any] | None = None,
    ) -> None:
        """更新运行时配置，不中断正在执行的搬运任务。"""

        old_interval = int(self.config.get("auto_interval_minutes", 0) or 0)
        new_interval = int(config.get("auto_interval_minutes", 0) or 0)
        self.config = config
        self.fnos_config = fnos_config
        self.categories = categories or {}
        self.cmcc_upload_config = cmcc_upload_config or {}
        self.cloud139_config = cloud139_config or {}
        self.enabled = bool(config.get("enabled", True))
        self.environment_checker.apply_config(config)
        self.worker_command.apply_config(config)
        self.log_sink.resize(config.get("log_lines", 500))

        if old_interval == new_interval:
            return
        self.scheduler.restart(new_interval)

    def start(
        self,
        reason: str = "manual",
        file_retry: dict[str, Any] | None = None,
        category_filter: str = "",
        staging_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"success": False, "message": "rclone 集成未启用", "status": self.status()}
        if self.config.get("staging_enabled") and staging_run is None:
            message = "任务级暂存已启用，通用目录扫描缺少持久化任务计划，已拒绝启动"
            self._append_log(message)
            return {"success": False, "message": message, "status": self.status()}
        if staging_run is not None:
            persisted_staging_run, staging_error = self._database_bound_staging_run(staging_run)
            if not persisted_staging_run:
                return {
                    "success": False,
                    "message": staging_error
                    or "持久化暂存搬运参数无效，已拒绝按当前后台配置降级搬运",
                    "status": self.status(),
                }
            # 后续队列、线程和环境变量只使用数据库固化计划重新派生的参数，
            # 不能继续信任调用方传入的目录或上传后端。
            staging_run = persisted_staging_run

        with self.lock:
            if getattr(self, "_shutdown_requested", False):
                return {
                    "success": False,
                    "message": "rclone 服务正在关闭，已拒绝启动新的搬运任务",
                    "status": self.status_locked(),
                }
            staging_job_id = self._int_value((staging_run or {}).get("job_id"))
            execution_job_id = staging_job_id or self._int_value((file_retry or {}).get("job_id"))
            if execution_job_id > 0 and self.db:
                latest_job = self.db.get_job(execution_job_id) or {}
                latest_status = str(latest_job.get("status") or "").strip().lower()
                if self._staging_status_blocks_execution(latest_status):
                    return {
                        "success": False,
                        "message": f"任务 #{execution_job_id} 当前状态为 {latest_status}，已拒绝启动 rclone 搬运",
                        "status": self.status_locked(),
                    }
            full_staging_run = staging_job_id > 0 and file_retry is None
            if staging_run:
                self._cancel_staging_retry_locked(staging_job_id)
            if self.is_running_locked():
                active_staging_job_id = self._int_value(
                    getattr(self, "_active_full_staging_job_id", 0)
                )
                if full_staging_run and active_staging_job_id == staging_job_id:
                    self._append_log(
                        f"任务 #{staging_job_id} 的 rclone 搬运已在运行，忽略重复启动请求：{reason}"
                    )
                    return {
                        "success": True,
                        "queued": False,
                        "already_running": True,
                        "deduplicated": True,
                        "job_id": staging_job_id,
                        "message": f"任务 #{staging_job_id} 的 rclone 搬运已在运行，无需重复启动",
                        "status": self.status_locked(),
                    }
                reserved_staging_job_id = self._int_value(
                    getattr(self, "_reserved_full_staging_job_id", 0)
                )
                if full_staging_run and reserved_staging_job_id == staging_job_id:
                    self._append_log(
                        f"任务 #{staging_job_id} 的 rclone 搬运已从队列取出并准备启动，忽略重复请求：{reason}"
                    )
                    return {
                        "success": True,
                        "queued": True,
                        "already_queued": True,
                        "starting": True,
                        "deduplicated": True,
                        "job_id": staging_job_id,
                        "message": f"任务 #{staging_job_id} 的 rclone 搬运正在准备启动，无需重复入队",
                        "status": self.status_locked(),
                    }
                if full_staging_run:
                    _, enqueued = self.run_queue.enqueue_if_staging_job_absent(
                        reason=reason,
                        file_retry=file_retry,
                        category_filter=category_filter,
                        queued_at=self._now(),
                        staging_run=staging_run,
                    )
                else:
                    self.run_queue.enqueue(
                        reason=reason,
                        file_retry=file_retry,
                        category_filter=category_filter,
                        queued_at=self._now(),
                        staging_run=staging_run,
                    )
                    enqueued = True
                if not enqueued:
                    self._append_log(
                        f"任务 #{staging_job_id} 已在 rclone 搬运队列中，忽略重复入队请求：{reason}"
                    )
                    return {
                        "success": True,
                        "queued": True,
                        "already_queued": True,
                        "deduplicated": True,
                        "job_id": staging_job_id,
                        "message": f"任务 #{staging_job_id} 已在 rclone 搬运队列中，无需重复入队",
                        "status": self.status_locked(),
                    }
                self._append_log(f"rclone 搬运任务正在运行，本次请求已加入队列：{reason}")
                return {"success": True, "queued": True, "message": "rclone 正在运行，已加入自动搬运队列", "status": self.status_locked()}

            self.run_queue.begin_direct_run()
            self._start_worker_locked(
                reason,
                file_retry=file_retry,
                category_filter=category_filter,
                staging_run=staging_run,
            )
            return {"success": True, "message": "已启动 rclone 搬运任务", "status": self.status_locked()}

    def _start_worker_locked(
        self,
        reason: str,
        file_retry: dict[str, Any] | None = None,
        category_filter: str = "",
        staging_run: dict[str, Any] | None = None,
    ) -> None:
        persisted_staging_run = dict(staging_run) if isinstance(staging_run, dict) else None
        if persisted_staging_run:
            self._cancel_staging_retry_locked(self._int_value(persisted_staging_run.get("job_id")))
        previous_job_id = self._int_value(getattr(self, "_active_staging_job_id", 0))
        next_job_id = self._int_value((persisted_staging_run or {}).get("job_id"))
        if next_job_id <= 0 and isinstance(file_retry, dict):
            next_job_id = self._int_value(file_retry.get("job_id"))
        stop_requests = getattr(self, "_stop_requested_job_ids", None)
        if isinstance(stop_requests, set) and previous_job_id > 0 and previous_job_id != next_job_id:
            stop_requests.discard(previous_job_id)
        self._active_staging_job_id = next_job_id
        self._active_full_staging_job_id = (
            self._int_value((persisted_staging_run or {}).get("job_id"))
            if persisted_staging_run and file_retry is None
            else 0
        )
        self._reserved_full_staging_job_id = 0
        self._active_run_reason = str(reason or "")
        self.worker_thread = threading.Thread(
            target=self._run_script,
            args=(reason, file_retry, str(category_filter or "").strip(), persisted_staging_run),
            daemon=True,
        )
        self.run_state.mark_starting()
        self.worker_thread.start()

    def start_file_retry(self, file_event: dict[str, Any]) -> dict[str, Any]:
        filename = str(file_event.get("filename") or "").strip()
        if not filename:
            return {"success": False, "message": "文件记录缺少文件名，无法单独重试", "status": self.status()}
        category = str(file_event.get("category") or "").strip()
        if category in self.categories:
            category_config = self.categories.get(category) or {}
            category = str(category_config.get("fnos_lib") or category_config.get("label") or category)
        retry_filter = {
            "event_id": file_event.get("id") or "",
            "job_id": file_event.get("job_id") or "",
            "category": category,
            "filename": filename,
            "source_path": str(file_event.get("source_path") or "").strip(),
        }
        reason = f"file_retry:{retry_filter['event_id']}:{filename}"
        staging_run = None
        try:
            job_id = int(file_event.get("job_id") or 0)
        except (TypeError, ValueError):
            job_id = 0
        get_job = getattr(self.db, "get_job", None) if self.db else None
        job: dict[str, Any] = {}
        if job_id > 0 and callable(get_job):
            try:
                job = get_job(job_id) or {}
                if str(job.get("status") or "").strip().lower() in {
                    "cancelled",
                    "done",
                    "success",
                    "skipped_existing",
                    JOB_WAITING_ORGANIZER,
                    "organizing",
                    "confirming",
                }:
                    return {
                        "success": False,
                        "message": "任务已离开可搬运阶段，不能重试单个文件",
                        "status": self.status(),
                    }
                staging_run = rclone_staging_run_from_job(job) or None
            except ValueError as exc:
                return {
                    "success": False,
                    "message": f"单文件重试拒绝启动：{exc}",
                    "status": self.status(),
                }
        previous_status = str(job.get("status") or "")
        previous_error = str(job.get("error_message") or "")
        if job_id > 0 and self.db and job:
            if not self._update_job_from_snapshot(
                job,
                status=JOB_WAITING_TRANSFER,
                error_message="",
            ):
                return {
                    "success": False,
                    "message": "任务状态已变化，已拒绝启动过期的单文件重试",
                    "status": self.status(),
                }
        result = self.start(reason=reason, file_retry=retry_filter, staging_run=staging_run)
        if job_id > 0 and self.db and not result.get("success"):
            waiting_snapshot = {**job, "status": JOB_WAITING_TRANSFER}
            self._update_job_from_snapshot(
                waiting_snapshot,
                status=previous_status,
                error_message=previous_error,
            )
        return result

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if not self.process_controller.is_active(self.process):
                return {
                    "success": False,
                    "message": "\u5f53\u524d\u6ca1\u6709\u8fd0\u884c\u4e2d\u7684 rclone \u642c\u8fd0\u4efb\u52a1",
                    "status": self.status_locked(),
                }
            self.run_queue.request_stop()
            self._append_log("\u6536\u5230\u505c\u6b62\u8bf7\u6c42\uff0c\u6b63\u5728\u7ec8\u6b62 rclone \u642c\u8fd0\u811a\u672c")
            self.process_controller.terminate(self.process)
            return {
                "success": True,
                "message": "\u5df2\u53d1\u9001\u505c\u6b62\u4fe1\u53f7",
                "status": self.status_locked(),
            }

    def cancel_job(self, job_id: int, *, stop_running: bool = False) -> dict[str, Any]:
        """Cancel only the queued/active rclone execution owned by one job.

        Legacy directory-wide runs have no durable job identity.  They are never
        terminated by a single-job cancellation because doing so would interrupt
        unrelated imports.
        """

        normalized_job_id = self._int_value(job_id)
        if normalized_job_id <= 0:
            return {
                "success": False,
                "job_id": normalized_job_id,
                "message": "缺少有效任务 ID，未停止任何 rclone 运行",
                "removed_queue_count": 0,
                "active_match": False,
                "stop_sent": False,
            }

        process_to_stop: subprocess.Popen[str] | None = None
        with self.lock:
            self._cancel_staging_retry_locked(normalized_job_id)
            removed = self.run_queue.remove_job(normalized_job_id)
            active_job_id = self._int_value(getattr(self, "_active_staging_job_id", 0))
            active_match = active_job_id == normalized_job_id
            stop_sent = False
            if active_match and stop_running:
                stop_requests = getattr(self, "_stop_requested_job_ids", None)
                if not isinstance(stop_requests, set):
                    stop_requests = set()
                    self._stop_requested_job_ids = stop_requests
                stop_requests.add(normalized_job_id)
                if self.process_controller.is_active(self.process):
                    process_to_stop = self.process
            status = self.status_locked()

        if process_to_stop is not None:
            stop_sent = bool(self.process_controller.terminate(process_to_stop))
        removed_count = len(removed)
        if stop_sent:
            message = f"已仅停止任务 #{normalized_job_id} 的 rclone 运行"
        elif active_match and stop_running:
            message = f"任务 #{normalized_job_id} 已设置定向停止栏栅，不会清空其他队列"
        elif removed_count:
            message = f"已从 rclone 队列移除任务 #{normalized_job_id}，其他任务保持不变"
        else:
            message = f"任务 #{normalized_job_id} 当前未占用 rclone，未停止其他任务"
        return {
            "success": True,
            "job_id": normalized_job_id,
            "message": message,
            "removed_queue_count": removed_count,
            "active_match": active_match,
            "stop_requested": bool(stop_running),
            "stop_sent": stop_sent,
            "status": status,
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self.status_locked()

    def status_locked(self) -> dict[str, Any]:
        queue_status = self.run_queue.snapshot(limit=10)
        return {
            "enabled": self.enabled,
            "running": self.is_running_locked(),
            **self.run_state.snapshot(),
            **queue_status,
            "script_path": str(self._script_path()),
            "auto_interval_minutes": int(self.config.get("auto_interval_minutes", 0) or 0),
            "directory_mapping": self._category_dir_env(os.environ.copy()),
        }

    def get_logs(self, limit: int = 200) -> list[str]:
        return self.log_sink.list(limit)

    def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if not self.db:
            return []
        return self.db.list_rclone_runs(limit=limit, offset=offset)

    def list_events(self, run_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if not self.db:
            return []
        return self.db.list_rclone_events(run_id=run_id, limit=limit)

    def list_file_events(
        self,
        *,
        run_id: int | None = None,
        job_id: int | None = None,
        status: str | None = None,
        category: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if not self.db:
            return []
        return self.db.list_rclone_file_events(
            run_id=run_id,
            job_id=job_id,
            status=status,
            category=category,
            limit=limit,
            offset=offset,
        )

    def cleanup_cancelled_task(
        self,
        *,
        job: dict[str, Any] | None = None,
        request_item: dict[str, Any] | None = None,
        file_events: list[dict[str, Any]] | None = None,
        delete_source: bool = True,
        delete_temp: bool = True,
        delete_target_partial: bool = True,
        stop_running: bool = False,
        include_title_matches: bool = True,
    ) -> dict[str, Any]:
        return self.cancelled_task_cleanup.cleanup(
            job=job,
            request_item=request_item,
            file_events=file_events,
            delete_source=delete_source,
            delete_temp=delete_temp,
            delete_target_partial=delete_target_partial,
            stop_running=stop_running,
            include_title_matches=include_title_matches,
        )

    def check_environment(self) -> dict[str, Any]:
        return self.environment_checker.check(
            self._script_path(),
            self._category_dir_env(os.environ.copy()),
        )

    def _cleanup_specs_from_events(self, file_events: list[dict[str, Any]]) -> list[dict[str, str]]:
        specs: list[dict[str, str]] = []
        for event in file_events[:200]:
            source_path = self._clean_remote_file_path(event.get("source_path"))
            target_path = self._clean_remote_file_path(event.get("target_path"))
            filename = str(event.get("filename") or "").strip()
            if not filename:
                filename = posixpath.basename(source_path or target_path)
            if not filename:
                continue
            specs.append(
                {
                    "filename": filename,
                    "source_path": source_path,
                    "target_path": target_path,
                    "matched_by": "file_event",
                }
            )
        return specs

    def _cleanup_specs_from_title(
        self,
        *,
        job: dict[str, Any] | None,
        request_item: dict[str, Any] | None,
        known_specs: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        subject = job or request_item or {}
        title = str(subject.get("title") or "").strip()
        category = str(subject.get("category") or (request_item or {}).get("category") or "").strip()
        plan: dict[str, Any] = {}
        if isinstance(job, dict) and job:
            try:
                plan = validated_staging_plan_from_job(job)
            except ValueError:
                # 已损坏的固化边界不能降级成分类目录模糊扫描，否则取消一个任务
                # 可能清理到其他任务。状态修复由主取消/恢复流程负责。
                return []
        exclusive_job_root = bool(plan)
        if exclusive_job_root:
            if str(plan.get("route") or "").strip().lower() != ROUTE_QUARK_TO_MOBILE:
                return []
            source_dir = self._clean_remote_file_path(plan.get("quark_job_root"))
        else:
            if not self._is_legacy_quark_cleanup_subject(subject):
                return []
            if not title or len(self._normalize_match_text(title)) < 4:
                return []
            dirs = self._category_dirs_for_key(category)
            source_dir = dirs.get("source_dir") or ""
        if not source_dir:
            return []

        known_sources = {self._clean_remote_file_path(item.get("source_path")) for item in known_specs if item.get("source_path")}
        list_item = self._rclone_lsf(source_dir)
        if not list_item["ok"]:
            return []

        specs: list[dict[str, str]] = []
        for relative_file in list_item["files"]:
            if len(specs) >= (200 if exclusive_job_root else 50):
                break
            if not exclusive_job_root and not self._title_matches_file(title, relative_file):
                continue
            source_path = self._join_remote_path(source_dir, relative_file)
            if source_path in known_sources:
                continue
            specs.append(
                {
                    "filename": posixpath.basename(relative_file),
                    "source_path": source_path,
                    # 标题匹配只能证明源端待搬运文件与任务相关，不能证明目标端同名文件一定是本次残留。
                    # 因此未产生文件级事件时不按标题删除目标端，避免误删媒体库中已有资源。
                    "target_path": "",
                    "matched_by": "staging_job_root" if exclusive_job_root else "title_match",
                }
            )
        return specs

    @staticmethod
    def _is_legacy_quark_cleanup_subject(subject: dict[str, Any]) -> bool:
        route = str(subject.get("target_route") or subject.get("route") or "").strip().lower()
        source_type = str(subject.get("source_type") or "").strip().lower()
        raw_data = subject.get("raw_data") if isinstance(subject.get("raw_data"), dict) else {}
        request_payload = raw_data.get("request") if isinstance(raw_data.get("request"), dict) else {}
        source_url = str(
            subject.get("source_url")
            or subject.get("url")
            or request_payload.get("url")
            or request_payload.get("source_url")
            or ""
        ).strip().lower()
        return bool(
            route == ROUTE_QUARK_TO_MOBILE
            or source_type == "quark"
            or "pan.quark.cn/" in source_url
        )

    def _category_dirs_for_key(self, category_key: str) -> dict[str, str]:
        suffix_map = {
            "movie": "MOVIE",
            "tv": "TV",
            "anime": "ANIME",
            "variety": "VARIETY",
            "other": "OTHER",
        }
        suffix = suffix_map.get(str(category_key or "").strip()) or "MOVIE"
        env_dirs = self._category_dir_env(os.environ.copy())
        return {
            "source_dir": env_dirs.get(f"RCLONE_SRC_{suffix}_DIR", ""),
            "target_dir": env_dirs.get(f"RCLONE_DST_{suffix}_DIR", ""),
        }

    @staticmethod
    def _dedupe_cleanup_specs(specs: list[dict[str, str]]) -> list[dict[str, str]]:
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for spec in specs:
            key = (
                str(spec.get("filename") or ""),
                str(spec.get("source_path") or ""),
                str(spec.get("target_path") or ""),
            )
            if not key[0] or key in seen:
                continue
            seen.add(key)
            deduped.append(spec)
            if len(deduped) >= 200:
                break
        return deduped

    def _rclone_lsf(self, remote_dir: str) -> dict[str, Any]:
        remote = self._remote_path(remote_dir)
        item = self._run_cleanup_command(
            "列出远端源目录",
            ["docker", "exec", str(self.config.get("container_name", "rclone-server")), "rclone", "lsf", remote, "-R", "--files-only"],
            timeout=30,
            path=remote,
        )
        if not item["ok"]:
            return {"ok": False, "files": [], "message": item["message"]}
        files = [line.strip() for line in str(item.get("stdout") or "").splitlines() if line.strip()]
        return {"ok": True, "files": files, "message": item["message"]}

    def _rclone_deletefile(self, remote_file: str, item_type: str) -> dict[str, Any]:
        remote = self._remote_path(remote_file)
        return self._run_cleanup_command(
            "删除远端文件",
            ["docker", "exec", str(self.config.get("container_name", "rclone-server")), "rclone", "deletefile", remote],
            timeout=60,
            path=remote,
            item_type=item_type,
        )

    def _cleanup_empty_remote_dirs(self, specs: list[dict[str, str]], *, source_key: str, item_type: str, result: dict[str, Any]) -> None:
        dirs = sorted({posixpath.dirname(self._clean_remote_file_path(spec.get(source_key))) for spec in specs if spec.get(source_key)})
        for directory in dirs:
            if not directory or directory == ".":
                continue
            item = self._run_cleanup_command(
                "清理远端空目录",
                [
                    "docker",
                    "exec",
                    str(self.config.get("container_name", "rclone-server")),
                    "rclone",
                    "rmdirs",
                    self._remote_path(directory),
                    "--leave-root",
                ],
                timeout=30,
                path=self._remote_path(directory),
                item_type=item_type,
            )
            result["items"].append(item)

    def _delete_local_temp_file(
        self,
        filename: str,
        *,
        job: dict[str, Any] | None = None,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        exact_path = self._local_temp_cleanup_path(job=job, spec=spec)
        if not exact_path:
            return {
                "type": "local_temp",
                "ok": True,
                "skipped": True,
                "path": "",
                "message": f"缺少可验证的任务级 temp 路径，已跳过共享缓存中的 {filename}",
                "exit_code": 0,
            }
        return self._run_cleanup_command(
            "删除本地临时缓存",
            [
                "docker",
                "exec",
                str(self.config.get("container_name", "rclone-server")),
                "rm",
                "-f",
                "--",
                exact_path,
            ],
            timeout=60,
            path=exact_path,
            item_type="local_temp",
        )

    def _local_temp_cleanup_path(
        self,
        *,
        job: dict[str, Any] | None,
        spec: dict[str, Any] | None,
    ) -> str:
        job = job if isinstance(job, dict) else {}
        spec = spec if isinstance(spec, dict) else {}
        source_path = self._clean_remote_file_path(spec.get("source_path"))
        if not source_path:
            return ""
        try:
            plan = validated_staging_plan_from_job(job)
        except ValueError:
            return ""
        if plan and str(plan.get("route") or "").strip().lower() != ROUTE_QUARK_TO_MOBILE:
            return ""

        category = str(job.get("category") or "").strip().lower()
        local_job_names = {
            "movie": "离线电影",
            # Must match scripts/fnos_rclone_worker.sh JOBS exactly: local
            # download roots use the worker job name, not the media label.
            "tv": "离线剧集",
            "anime": "离线动漫",
            "variety": "离线综艺",
            "other": "离线其他",
        }
        local_job_name = local_job_names.get(category)
        if not local_job_name:
            return ""
        source_root = self._clean_remote_file_path(
            plan.get("quark_source_category_root")
            if plan
            else self._category_dirs_for_key(category).get("source_dir")
        )
        source_path = self._without_remote_prefix(source_path)
        source_root = self._without_remote_prefix(source_root)
        if not source_root or not source_path.casefold().startswith(f"{source_root.casefold()}/"):
            return ""
        relative_path = source_path[len(source_root) + 1 :].strip("/")
        parts = [part for part in relative_path.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            return ""
        local_temp = _safe_container_temp_root(self.config.get("local_temp") or "/temp/fnos-media-import")
        if not local_temp:
            return ""
        return posixpath.join(local_temp, local_job_name, *parts)

    @staticmethod
    def _without_remote_prefix(path: str) -> str:
        text = str(path or "").strip()
        if ":" in text.split("/", 1)[0]:
            return text.split(":", 1)[1].strip("/")
        return text.strip("/")

    def _delete_empty_local_temp_dirs(self, result: dict[str, Any]) -> None:
        local_temp = _safe_container_temp_root(self.config.get("local_temp") or "/temp/fnos-media-import")
        if not local_temp:
            return
        job_roots: set[str] = set()
        for item in result.get("items") or []:
            if not isinstance(item, dict) or str(item.get("type") or "") != "local_temp":
                continue
            exact_path = str(item.get("path") or "").replace("\\", "/")
            prefix = f"{local_temp}/"
            if not exact_path.startswith(prefix):
                continue
            parts = [part for part in exact_path[len(prefix) :].split("/") if part]
            if len(parts) < 3 or re.fullmatch(r"job-[1-9][0-9]*", parts[1], flags=re.IGNORECASE) is None:
                continue
            job_roots.add(posixpath.join(local_temp, parts[0], parts[1]))

        for job_root in sorted(job_roots):
            item = self._run_cleanup_command(
                "清理任务级本地临时空目录",
                [
                    "docker",
                    "exec",
                    str(self.config.get("container_name", "rclone-server")),
                    "find",
                    job_root,
                    "-depth",
                    "-type",
                    "d",
                    "-empty",
                    "-delete",
                ],
                timeout=30,
                path=job_root,
                item_type="local_temp_rmdirs",
            )
            result["items"].append(item)

    def _run_cleanup_command(
        self,
        label: str,
        command: list[str],
        *,
        timeout: int,
        path: str = "",
        item_type: str = "command",
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
            stdout = (completed.stdout or "").strip()
            stderr = (completed.stderr or "").strip()
            message = stdout or stderr or f"exit={completed.returncode}"
            ok = completed.returncode == 0
            lower_message = message.lower()
            if not ok and item_type in {"remote_source", "remote_target"} and ("not found" in lower_message or "doesn't exist" in lower_message):
                ok = True
                message = f"{path} 不存在，视为已清理"
            return {
                "type": item_type,
                "ok": ok,
                "path": path,
                "message": message,
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        except FileNotFoundError as exc:
            return {"type": item_type, "ok": False, "path": path, "message": f"{label}失败：命令不存在 {exc}", "exit_code": 127}
        except subprocess.TimeoutExpired:
            return {"type": item_type, "ok": False, "path": path, "message": f"{label}超时：{path}", "exit_code": 124}

    def _remote_path(self, value: str) -> str:
        path = self._clean_remote_file_path(value)
        if ":" in path.split("/", 1)[0]:
            return path
        return f"{self.config.get('remote_name', 'MP')}:{path}"

    @staticmethod
    def _clean_remote_file_path(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if ":" in text.split("/", 1)[0]:
            remote, path = text.split(":", 1)
            return f"{remote}:{path.strip('/')}"
        return text.strip("/")

    @staticmethod
    def _join_remote_path(directory: str, relative_file: str) -> str:
        directory = str(directory or "").strip().strip("/")
        relative_file = str(relative_file or "").strip().strip("/")
        if not directory:
            return relative_file
        if not relative_file:
            return directory
        return f"{directory}/{relative_file}"

    @staticmethod
    def _normalize_match_text(value: str) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value or "").lower()

    def _title_matches_file(self, title: str, relative_file: str) -> bool:
        normalized_title = self._normalize_match_text(title)
        normalized_file = self._normalize_match_text(relative_file)
        stem = posixpath.splitext(posixpath.basename(relative_file))[0]
        normalized_stem = self._normalize_match_text(stem)
        if len(normalized_title) < 4:
            return False
        if normalized_title in normalized_file:
            return True
        return len(normalized_stem) >= 4 and normalized_stem in normalized_title

    def start_scheduler(self) -> None:
        with self.lock:
            self._shutdown_requested = False
        self.scheduler.start(int(self.config.get("auto_interval_minutes", 0) or 0))

    def shutdown_scheduler(self) -> None:
        with self.lock:
            self._shutdown_requested = True
        self.run_queue.request_stop()
        self._cancel_all_staging_retries()
        self.scheduler.shutdown()

    def is_running_locked(self) -> bool:
        return bool(self.worker_thread and self.worker_thread.is_alive())

    def _start_next_queued_run(self) -> None:
        while True:
            with self.lock:
                if getattr(self, "_shutdown_requested", False):
                    self.run_queue.request_stop()
                    self._release_current_worker_locked()
                    return
                next_run = self.run_queue.pop_next()
                if not next_run:
                    self._release_current_worker_locked()
                    return
                reserved_job_id = self._int_value(
                    (next_run.staging_run or {}).get("job_id")
                    if isinstance(next_run.staging_run, dict) and next_run.file_retry is None
                    else 0
                )
                self._reserved_full_staging_job_id = reserved_job_id

            staging_run = next_run.staging_run
            queued_file_retry = next_run.file_retry if isinstance(next_run.file_retry, dict) else {}
            queued_file_job_id = self._int_value(queued_file_retry.get("job_id"))
            if queued_file_job_id > 0 and self.db:
                latest_file_job = self.db.get_job(queued_file_job_id)
                file_job_status = str((latest_file_job or {}).get("status") or "").strip().lower()
                if self._staging_status_blocks_execution(file_job_status):
                    self._append_log(
                        f"已跳过队列中的 rclone 单文件重试：任务 #{queued_file_job_id} 当前状态为 {file_job_status}"
                    )
                    self._clear_reserved_full_staging_job(reserved_job_id)
                    continue
            if staging_run:
                persisted, staging_error = self._database_bound_staging_run(staging_run)
                job_id = self._int_value(staging_run.get("job_id"))
                latest = self.db.get_job(job_id) if self.db and job_id > 0 else None
                status = str((latest or {}).get("status") or "").strip().lower()
                if self._staging_status_blocks_execution(status):
                    self._append_log(
                        f"已跳过队列中的任务级 rclone 搬运：任务 #{job_id} 当前状态为 {status}"
                    )
                    self._clear_reserved_full_staging_job(reserved_job_id)
                    continue
                if not persisted:
                    message = staging_error or "队列中的任务级 staging 参数已失效"
                    if latest:
                        self._mark_run_ready_items_review(
                            [{"job_id": job_id, "job": latest}],
                            f"队列中的 rclone 搬运在执行前复核失败，已转人工审核：{message}",
                        )
                    self._append_log(f"已拒绝队列中的任务级 rclone 搬运：{message}")
                    self._clear_reserved_full_staging_job(reserved_job_id)
                    continue
                staging_run = persisted

            with self.lock:
                if getattr(self, "_shutdown_requested", False):
                    self.run_queue.request_stop()
                    self._reserved_full_staging_job_id = 0
                    self._release_current_worker_locked()
                    return
                reason = next_run.reason
                self._append_log(f"自动启动队列中的 rclone 搬运任务：{reason}")
                self._start_worker_locked(
                    reason,
                    file_retry=next_run.file_retry,
                    category_filter=next_run.category_filter,
                    staging_run=staging_run,
                )
                return

    def _clear_reserved_full_staging_job(self, job_id: int) -> None:
        normalized_job_id = self._int_value(job_id)
        if normalized_job_id <= 0:
            return
        with self.lock:
            if self._int_value(getattr(self, "_reserved_full_staging_job_id", 0)) == normalized_job_id:
                self._reserved_full_staging_job_id = 0

    def _release_current_worker_locked(self) -> None:
        if getattr(self, "worker_thread", None) is threading.current_thread():
            self.worker_thread = None
            completed_job_id = self._int_value(getattr(self, "_active_staging_job_id", 0))
            self._active_staging_job_id = 0
            self._active_full_staging_job_id = 0
            self._reserved_full_staging_job_id = 0
            self._active_run_reason = ""
            stop_requests = getattr(self, "_stop_requested_job_ids", None)
            if isinstance(stop_requests, set) and completed_job_id > 0:
                stop_requests.discard(completed_job_id)

    @staticmethod
    def _staging_status_blocks_execution(status: str) -> bool:
        return str(status or "").strip().lower() in {
            "cancelled",
            "done",
            "success",
            "skipped_existing",
            JOB_REVIEW,
            JOB_WAITING_ORGANIZER,
            "organizing",
            "confirming",
            "unsupported",
        }

    def _schedule_incomplete_staging_retry(
        self,
        staging_run: dict[str, Any] | None,
        *,
        run_id: int | None,
        exit_code: int,
    ) -> dict[str, Any]:
        persisted = self._validated_staging_run(staging_run)
        job_id = self._int_value(persisted.get("job_id"))
        if not persisted or job_id <= 0 or not self.db:
            return {"success": True, "skipped": True, "message": "当前 run 不是任务级 staging"}
        if getattr(self, "_shutdown_requested", False):
            self._clear_staging_retry(job_id)
            return {"success": True, "skipped": True, "message": "rclone 服务正在关闭，已停止 staging 自动补跑"}
        job = self.db.get_job(job_id)
        if not job:
            self._clear_staging_retry(job_id)
            return {"success": True, "skipped": True, "message": "任务已不存在"}
        status = str(job.get("status") or "").strip().lower()
        if status in {
            "done",
            "success",
            "skipped_existing",
            "cancelled",
            JOB_REVIEW,
            JOB_WAITING_ORGANIZER,
            "organizing",
            "confirming",
        }:
            self._clear_staging_retry(job_id)
            return {"success": True, "skipped": True, "message": f"任务状态 {status} 不再自动补跑"}

        events = self._all_rclone_file_events(job_id=job_id)
        terminal_events = self._latest_terminal_file_events(events, job=job)
        verdict = self._rclone_job_feasibility(job, terminal_events, exit_code)
        if verdict.get("ready"):
            handoff = self._handoff_completed_staging_job_to_organizer(
                job,
                terminal_events,
                verdict,
                trigger="staging_retry_schedule_completion_recovery",
                run_id=self._int_value(run_id),
            )
            self._clear_staging_retry(job_id)
            return {
                **handoff,
                "skipped": True,
                "message": handoff.get("message") or "rclone 完整性已通过并补投 Organizer",
            }

        max_attempts = self._staging_retry_max_attempts()
        persisted_attempts = self._staging_retry_attempts_from_job(job)
        with self.lock:
            if getattr(self, "_shutdown_requested", False):
                self._cancel_staging_retry_locked(job_id)
                return {
                    "success": True,
                    "skipped": True,
                    "message": "rclone 服务正在关闭，已停止 staging 自动补跑",
                }
            timers = getattr(self, "_staging_retry_timers", None)
            if not isinstance(timers, dict):
                timers = {}
                self._staging_retry_timers = timers
            existing = timers.get(job_id)
            if existing is not None and existing.is_alive():
                return {"success": True, "queued": True, "message": "当前任务已有 staging 补跑计时器"}
            attempts = getattr(self, "_staging_retry_attempts", None)
            if not isinstance(attempts, dict):
                attempts = {}
                self._staging_retry_attempts = attempts
            completed_attempts = max(persisted_attempts, self._int_value(attempts.get(job_id)))
            if completed_attempts >= max_attempts:
                attempts[job_id] = completed_attempts
                self._cancel_staging_retry_locked(job_id)
                exhausted = True
                attempt = completed_attempts
                delay_seconds = 0
                timer = None
            else:
                exhausted = False
                attempt = completed_attempts + 1
                delay_seconds = self._staging_retry_delay_seconds(attempt)
            attempts[job_id] = attempt
            if not exhausted:
                self._persist_staging_retry_attempt(
                    job,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    delay_seconds=delay_seconds,
                    run_id=run_id,
                    exit_code=exit_code,
                )
                timer = threading.Timer(
                    delay_seconds,
                    self._retry_incomplete_staging_job,
                    args=(dict(persisted), attempt),
                )
                timer.daemon = True
                timers[job_id] = timer
                timer.start()
        if exhausted:
            return self._mark_staging_retry_exhausted(
                job,
                attempts=attempt,
                max_attempts=max_attempts,
                run_id=run_id,
                exit_code=exit_code,
                verdict=verdict,
            )
        message = (
            f"任务级 rclone 尚未完整，{delay_seconds}s 后自动补跑"
            f"（第 {attempt}/{max_attempts} 次）：{verdict.get('message') or 'completion not ready'}"
        )
        self.db.add_event(
            job_id,
            EVENT_WARN,
            message,
            {
                "staging_retry": True,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "delay_seconds": delay_seconds,
                "previous_run_id": run_id or 0,
                "previous_exit_code": exit_code,
                "verdict": verdict,
            },
        )
        self._append_log(message)
        return {
            "success": True,
            "queued": True,
            "job_id": job_id,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "delay_seconds": delay_seconds,
        }

    def _retry_incomplete_staging_job(self, staging_run: dict[str, Any], attempt: int) -> None:
        job_id = self._int_value(staging_run.get("job_id"))
        with self.lock:
            timers = getattr(self, "_staging_retry_timers", {})
            timers.pop(job_id, None)
            if getattr(self, "_shutdown_requested", False):
                attempts = getattr(self, "_staging_retry_attempts", {})
                attempts.pop(job_id, None)
                return
        if job_id <= 0 or not self.db:
            return
        job = self.db.get_job(job_id)
        if not job:
            self._clear_staging_retry(job_id)
            return
        status = str(job.get("status") or "").strip().lower()
        if status in {
            "done",
            "success",
            "skipped_existing",
            "cancelled",
            JOB_REVIEW,
            JOB_WAITING_ORGANIZER,
            "organizing",
            "confirming",
        }:
            self._clear_staging_retry(job_id)
            return
        events = self._all_rclone_file_events(job_id=job_id)
        terminal_events = self._latest_terminal_file_events(events, job=job)
        verdict = self._rclone_job_feasibility(job, terminal_events, 0)
        if verdict.get("ready"):
            self._handoff_completed_staging_job_to_organizer(
                job,
                terminal_events,
                verdict,
                trigger="staging_retry_timer_completion_recovery",
                run_id=0,
            )
            self._clear_staging_retry(job_id)
            return
        result = self.start(
            reason=f"staging_retry:{job_id}:attempt-{attempt}",
            category_filter=str(staging_run.get("category") or "").strip(),
            staging_run=staging_run,
        )
        self.db.add_event(
            job_id,
            EVENT_INFO if result.get("success") else EVENT_WARN,
            result.get("message") or "已触发任务级 rclone 延迟补跑",
            {"staging_retry": True, "attempt": attempt, "rclone": result},
        )
        if result.get("success"):
            return
        self._schedule_incomplete_staging_retry(staging_run, run_id=None, exit_code=1)

    def _staging_retry_delay_seconds(self, attempt: int) -> int:
        base = max(5, min(3600, self._int_value(self.config.get("staging_retry_delay_seconds"), 30)))
        maximum = max(
            base,
            min(21600, self._int_value(self.config.get("staging_retry_max_delay_seconds"), 300)),
        )
        return min(maximum, base * (2 ** min(max(0, int(attempt) - 1), 10)))

    def _staging_retry_max_attempts(self) -> int:
        config = getattr(self, "config", {})
        return max(0, min(1000, self._int_value(config.get("staging_retry_max_attempts"), 8)))

    @classmethod
    def _staging_retry_attempts_from_job(cls, job: dict[str, Any] | None) -> int:
        raw_data = job.get("raw_data") if isinstance(job, dict) and isinstance(job.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        return max(0, cls._int_value(completion.get("staging_retry_attempts")))

    def _persist_staging_retry_attempt(
        self,
        job: dict[str, Any],
        *,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        run_id: int | None,
        exit_code: int,
    ) -> None:
        job_id = self._int_value(job.get("id"))
        latest = self.db.get_job(job_id) or job
        raw_data = latest.get("raw_data") if isinstance(latest.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        self._update_job_from_snapshot(
            latest,
            raw_data={
                **raw_data,
                "completion": {
                    **completion,
                    "staging_retry_attempts": attempt,
                    "staging_retry_max_attempts": max_attempts,
                    "staging_retry_exhausted": False,
                    "staging_retry_last_scheduled_at": self._now(),
                    "staging_retry_last_delay_seconds": delay_seconds,
                    "staging_retry_last_run_id": run_id or 0,
                    "staging_retry_last_exit_code": exit_code,
                },
            },
        )

    def _mark_staging_retry_exhausted(
        self,
        job: dict[str, Any],
        *,
        attempts: int,
        max_attempts: int,
        run_id: int | None,
        exit_code: int,
        verdict: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = self._int_value(job.get("id"))
        if job_id <= 0 or not self.db:
            return {"success": False, "queued": False, "message": "任务级 staging 自动补跑已耗尽，但任务不存在"}
        latest = self.db.get_job(job_id) or job
        status = str(latest.get("status") or "").strip().lower()
        if status in {
            "done",
            "success",
            "skipped_existing",
            "cancelled",
            JOB_REVIEW,
            JOB_WAITING_ORGANIZER,
            "organizing",
            "confirming",
        }:
            self._clear_staging_retry(job_id)
            return {
                "success": True,
                "skipped": True,
                "queued": False,
                "message": f"任务状态 {status} 不再处理 staging 补跑耗尽状态",
            }
        detail = str(verdict.get("message") or "rclone 搬运完整性仍未通过").strip()
        message = (
            f"任务级 rclone 自动补跑已达到上限 {max_attempts} 次，仍未确认完整搬运，"
            f"已转人工审核：{detail}"
        )
        raw_data = latest.get("raw_data") if isinstance(latest.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        if not self._update_job_from_snapshot(
            latest,
            status=JOB_REVIEW,
            error_message=message,
            raw_data={
                **raw_data,
                "completion": {
                    **completion,
                    "stage": COMPLETION_STAGE_REVIEW,
                    "message": message,
                    "retryable": True,
                    "staging_retry_attempts": max(0, attempts),
                    "staging_retry_max_attempts": max_attempts,
                    "staging_retry_exhausted": True,
                    "staging_retry_exhausted_at": self._now(),
                    "staging_retry_last_run_id": run_id or 0,
                    "staging_retry_last_exit_code": exit_code,
                },
            },
        ):
            current = self.db.get_job(job_id) or latest
            self._clear_staging_retry(job_id)
            return {
                "success": True,
                "skipped": True,
                "queued": False,
                "message": f"任务状态已变化为 {current.get('status') or '未知'}，未覆盖 staging 补跑结果",
            }
        self.db.add_event(
            job_id,
            EVENT_ERROR,
            message,
            {
                "staging_retry": True,
                "staging_retry_exhausted": True,
                "terminal": True,
                "retryable": True,
                "attempts": max(0, attempts),
                "max_attempts": max_attempts,
                "previous_run_id": run_id or 0,
                "previous_exit_code": exit_code,
                "verdict": verdict,
            },
        )
        self._clear_staging_retry(job_id)
        self._append_log(message)
        return {
            "success": False,
            "queued": False,
            "review": True,
            "job_id": job_id,
            "attempts": max(0, attempts),
            "max_attempts": max_attempts,
            "message": message,
        }

    def _cancel_staging_retry_locked(self, job_id: int) -> None:
        if job_id <= 0:
            return
        timers = getattr(self, "_staging_retry_timers", {})
        timer = timers.pop(job_id, None)
        if timer is not None:
            timer.cancel()

    def _clear_staging_retry(self, job_id: int) -> None:
        with self.lock:
            self._cancel_staging_retry_locked(job_id)
            attempts = getattr(self, "_staging_retry_attempts", {})
            attempts.pop(job_id, None)

    def _cancel_all_staging_retries(self) -> None:
        with self.lock:
            timers = list(getattr(self, "_staging_retry_timers", {}).values())
            self._staging_retry_timers = {}
            self._staging_retry_attempts = {}
        for timer in timers:
            timer.cancel()

    def _run_script(
        self,
        reason: str,
        file_retry: dict[str, Any] | None = None,
        category_filter: str = "",
        staging_run: dict[str, Any] | None = None,
    ) -> None:
        started_at = self._now()
        run_id = self.db.create_rclone_run(reason) if self.db else None
        with self.lock:
            self.run_state.mark_running(run_id, started_at)
            self._append_log(f"开始执行 rclone 搬运任务，触发来源：{reason}")

        script_path = self._script_path()
        if not script_path.exists():
            self._finish(127, f"rclone 搬运脚本不存在：{script_path}")
            self._schedule_incomplete_staging_retry(staging_run, run_id=run_id, exit_code=127)
            self.process = None
            self._start_next_queued_run()
            return

        command = self._command(script_path)
        try:
            env = self._env(
                run_id=run_id,
                file_retry=file_retry,
                category_filter=category_filter,
                trigger_reason=reason,
                staging_run=staging_run,
            )
        except ValueError as exc:
            self._finish(2, f"rclone 持久化暂存参数错误：{exc}")
            self._schedule_incomplete_staging_retry(staging_run, run_id=run_id, exit_code=2)
            self.process = None
            self._start_next_queued_run()
            return
        mapping = {
            env_name: self._remote_dir(env.get(env_name, ""))
            for _key, _label, _job_name, suffix in RcloneDirectoryMappingValidator.CATEGORIES
            for env_name in (f"RCLONE_SRC_{suffix}_DIR", f"RCLONE_DST_{suffix}_DIR")
        }
        effective_category_filter = str(env.get("RCLONE_ONLY_CATEGORY") or category_filter or "").strip()
        mapping_errors = self._validate_rclone_directory_mapping(mapping, category_filter=effective_category_filter)
        with self.lock:
            self._append_log(
                "rclone 目录映射："
                f"电影 {mapping['RCLONE_SRC_MOVIE_DIR']} -> {mapping['RCLONE_DST_MOVIE_DIR']}；"
                f"剧集 {mapping['RCLONE_SRC_TV_DIR']} -> {mapping['RCLONE_DST_TV_DIR']}；"
                f"动漫 {mapping['RCLONE_SRC_ANIME_DIR']} -> {mapping['RCLONE_DST_ANIME_DIR']}；"
                f"综艺 {mapping['RCLONE_SRC_VARIETY_DIR']} -> {mapping['RCLONE_DST_VARIETY_DIR']}；"
                f"其他 {mapping['RCLONE_SRC_OTHER_DIR']} -> {mapping['RCLONE_DST_OTHER_DIR']}"
            )
            if file_retry:
                self._append_log(
                    "rclone 单文件重试过滤："
                    f"分类 {file_retry.get('category') or '不限'}；"
                    f"文件 {file_retry.get('filename') or ''}"
                )
            elif effective_category_filter:
                self._append_log(f"rclone 自动搬运过滤分类：{effective_category_filter}")

        if mapping_errors:
            self._finish(2, "rclone 目录映射配置错误：" + "；".join(mapping_errors))
            self._schedule_incomplete_staging_retry(staging_run, run_id=run_id, exit_code=2)
            self.process = None
            self._start_next_queued_run()
            return

        try:
            result = self.process_runner.run(
                command,
                cwd=self.base_dir,
                env=env,
                on_line=self._append_log,
                on_started=self._set_process,
            )
            self._finish(result.exit_code, result.error)
            self._schedule_incomplete_staging_retry(
                staging_run,
                run_id=run_id,
                exit_code=result.exit_code,
            )
        finally:
            self.process = None
            self._start_next_queued_run()

    def _set_process(self, process: subprocess.Popen[str]) -> None:
        should_stop = False
        with self.lock:
            self.process = process
            active_job_id = self._int_value(getattr(self, "_active_staging_job_id", 0))
            stop_requests = getattr(self, "_stop_requested_job_ids", set())
            should_stop = active_job_id > 0 and active_job_id in stop_requests
        if should_stop:
            self._append_log(
                f"任务 #{active_job_id} 在 rclone 进程启动前已被取消，正在定向终止该进程"
            )
            self.process_controller.terminate(process)

    def _finish(self, exit_code: int, error: str) -> None:
        self.run_completion.finish(exit_code, error)

    def repair_waiting_jobs_from_history(self, limit: int = 50) -> dict[str, Any]:
        return self.waiting_job_recovery.repair(limit)

    def _finalize_run_imports(self, run_id: int, exit_code: int) -> None:
        self.run_import_finalizer.finalize(run_id, exit_code)

    def finalize_category_imports(
        self,
        run_id: int,
        category_label: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self.category_finalizer.finalize(run_id, category_label, payload)

    def resolve_resource_folder(self, category_label: str, filename: str, source_path: str = "", target_path: str = "") -> dict[str, Any]:
        """worker 遇到剧集类根目录文件时，尽量用任务标题给它补资源目录。"""

        category_key = self._rclone_category_key_from_callback(category_label, target_path)
        if category_key not in EPISODIC_CATEGORY_KEYS:
            return {"success": True, "skipped": True, "category": category_key, "resource_dir": ""}

        matched = None
        if self.db:
            matched = self.db.find_job_for_rclone_callback(
                category=category_label,
                filename=filename,
                source_path=source_path,
                target_path=target_path,
            )
        if matched:
            title = str(matched.get("title") or "").strip()
            context_dir = self._resource_dir_from_job_context(matched, category_key)
            resource_dir = context_dir or sanitize_resource_dir_name(title)
            return {
                "success": True,
                "category": category_key,
                "resource_dir": resource_dir,
                "strategy": "matched_job_context" if context_dir else "matched_job_title",
                "job_id": matched.get("id"),
                "title": title,
            }

        stem = posixpath.splitext(posixpath.basename(str(filename or source_path or target_path or "未命名资源")))[0]
        fallback = sanitize_resource_dir_name(stem)
        return {
            "success": True,
            "category": category_key,
            "resource_dir": f"{FALLBACK_ORGANIZE_DIR}/{fallback}",
            "strategy": "fallback_pending",
            "title": "",
        }

    def build_upload_naming_plan(self, category_label: str, filename: str, source_path: str = "", target_path: str = "") -> dict[str, Any]:
        """给 worker 返回上传前标准命名计划。"""

        category_key = self._rclone_category_key_from_callback(category_label, target_path)
        matched = None
        if self.db:
            matched = self.db.find_job_for_rclone_callback(
                category=category_label,
                filename=filename,
                source_path=source_path,
                target_path=target_path,
            )
        resource_title = str((matched or {}).get("title") or "").strip()
        context_dir = self._resource_dir_from_job_context(matched, category_key) if matched else ""
        if context_dir:
            leaf = posixpath.basename(context_dir.rstrip("/"))
            resource_title = leaf or resource_title
        plan = build_standard_naming_plan(
            category_key=category_key,
            filename=filename,
            target_file=self._target_file_relative_to_category(category_key, target_path),
            resource_title=resource_title,
            source_file=source_path,
        )
        if context_dir and category_key in EPISODIC_CATEGORY_KEYS:
            plan["target_relative_dir"] = context_dir
            plan["target_file"] = "/".join(part for part in [context_dir.strip("/"), str(plan.get("target_name") or filename).strip()] if part)
            plan["resource_dir_from_context"] = True
        if matched:
            plan["job_id"] = matched.get("id")
            plan["resource_title"] = resource_title
        return plan

    def _resource_dir_from_job_context(self, job: dict[str, Any] | None, category_key: str) -> str:
        if not isinstance(job, dict):
            return ""
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        request_payload = raw_data.get("request") if isinstance(raw_data.get("request"), dict) else {}
        contexts: list[dict[str, Any]] = []
        for container in (request_payload, raw_data):
            if not isinstance(container, dict):
                continue
            for key in ("update_context", "organizer_context"):
                value = container.get(key) if isinstance(container.get(key), dict) else {}
                if value:
                    contexts.append(value)
        values: list[Any] = []
        for context in contexts:
            values.extend(
                [
                    context.get("canonical_openlist_root"),
                    context.get("canonical_resource_root"),
                    context.get("target_root_path"),
                    context.get("resource_root_path"),
                ]
            )
        category = self.categories.get(category_key, {})
        category_roots = [
            self._cmcc_parent_path_for_category(category_key, category),
            category.get("cloud139_target_path"),
            category.get("cmcc_parent_path"),
            category.get("mobile_target_path"),
            category.get("openlist_root_path"),
            category.get("mobile_openlist_root_path"),
            category.get("cloud139_fnos_target_path"),
            category.get("sixpan_fnos_target_path"),
            category.get("fnos_target_path"),
            category.get("label"),
        ]
        for value in values:
            root = self._normalize_remote_hint(value)
            if not root:
                continue
            for category_root in category_roots:
                prefix = self._normalize_remote_hint(category_root)
                if prefix and root == prefix:
                    break
                if prefix and root.startswith(f"{prefix.rstrip('/')}/"):
                    suffix = root[len(prefix.rstrip("/")) + 1 :].strip("/")
                    if suffix:
                        return suffix
                    break
            else:
                leaf = posixpath.basename(root.rstrip("/"))
                if leaf:
                    return sanitize_resource_dir_name(leaf)
        return ""

    def _target_file_relative_to_category(self, category_key: str, target_path: str = "") -> str:
        target_norm = self._normalize_remote_hint(target_path)
        category = self.categories.get(category_key, {})
        prefixes = [
            self._cmcc_parent_path_for_category(category_key, category),
            category.get("cloud139_target_path"),
            category.get("mobile_target_path"),
            category.get("fnos_target_path"),
            category.get("cloud139_fnos_target_path"),
            category.get("cmcc_parent_path"),
        ]
        for prefix in prefixes:
            prefix_norm = self._normalize_remote_hint(prefix or "")
            if prefix_norm and target_norm == prefix_norm:
                return ""
            if prefix_norm and target_norm.startswith(f"{prefix_norm.rstrip('/')}/"):
                return target_norm[len(prefix_norm.rstrip('/')) + 1 :]
        return target_norm

    def _finish_ready_rclone_items(
        self,
        run_id: int,
        category_key: str,
        category: dict[str, Any],
        items: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]],
        events: list[dict[str, Any]],
        *,
        trigger: str,
        success_message: str,
    ) -> dict[str, Any]:
        return self.ready_items_completion.finish(
            run_id,
            category_key,
            category,
            items,
            events,
            trigger=trigger,
            success_message=success_message,
        )

    def _rclone_job_feasibility(
        self,
        job: dict[str, Any],
        events: list[dict[str, Any]],
        exit_code: int,
    ) -> dict[str, Any]:
        return RcloneJobFeasibilityEvaluator.evaluate(job, events, exit_code)

    def _update_job_from_snapshot(self, job: dict[str, Any], **updates: Any) -> bool:
        if not self.db:
            return False
        job_id = self._int_value(job.get("id"))
        expected_status = str(job.get("status") or "").strip()
        if job_id <= 0 or not expected_status or expected_status.lower() in {
            "cancelled",
            "done",
            "success",
            "skipped_existing",
            "unsupported",
            "rejected",
        }:
            return False
        updater = getattr(self.db, "update_job_if_status", None)
        if callable(updater):
            return bool(updater(job_id, {expected_status}, **updates))
        latest = self.db.get_job(job_id) or {}
        if str(latest.get("status") or "").strip() != expected_status:
            return False
        self.db.update_job(job_id, **updates)
        return True

    @staticmethod
    def _rclone_event_file_identity(event: dict[str, Any]) -> str:
        return RcloneJobFeasibilityEvaluator.file_identity(event)

    @staticmethod
    def _int_value(value: Any, default: int = 0) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def _rclone_category_key_from_callback(self, category_label: str, target_path: str = "") -> str:
        wanted = self._normalize_fnos_library_name(category_label)
        ordered_keys = ["movie", "tv", "anime", "variety", "other"]
        ordered_keys.extend(key for key in self.categories.keys() if key not in ordered_keys)
        for key in ordered_keys:
            category = self.categories.get(key, {})
            aliases = self._rclone_category_aliases(key, category)
            if wanted and wanted in aliases:
                return key

        target_norm = self._normalize_remote_hint(target_path)
        if target_norm:
            for key in ordered_keys:
                category = self.categories.get(key, {})
                if self._rclone_target_path_matches_category(target_norm, category):
                    return key
        return "movie"

    def _rclone_event_matches_category(
        self,
        event: dict[str, Any],
        *,
        category_label: str,
        category_key: str,
        category: dict[str, Any],
        target_path: str = "",
    ) -> bool:
        event_category = self._normalize_fnos_library_name(event.get("category") or "")
        aliases = self._rclone_category_aliases(category_key, category)
        wanted = self._normalize_fnos_library_name(category_label)
        if event_category and (event_category in aliases or (wanted and event_category == wanted)):
            return True
        event_target = self._normalize_remote_hint(event.get("target_path") or "")
        if event_target and self._rclone_target_path_matches_category(event_target, category):
            return True
        target_norm = self._normalize_remote_hint(target_path)
        return bool(event_target and target_norm and (event_target == target_norm or event_target.startswith(f"{target_norm.rstrip('/')}/")))

    @classmethod
    def _rclone_category_aliases(cls, category_key: str, category: dict[str, Any]) -> set[str]:
        defaults = {
            "movie": ["movie", "电影", "离线电影"],
            "tv": ["tv", "电视剧", "剧集", "离线剧集"],
            "anime": ["anime", "动漫", "动画", "离线动漫"],
            "variety": ["variety", "综艺", "离线综艺"],
            "other": ["other", "其他", "离线其他"],
        }
        values: list[Any] = [category_key, *defaults.get(category_key, [])]
        for key in ("label", "fnos_lib", "category", "name"):
            values.append(category.get(key))
        return {cls._normalize_fnos_library_name(value) for value in values if cls._normalize_fnos_library_name(value)}

    @classmethod
    def _rclone_target_path_matches_category(cls, target_path: str, category: dict[str, Any]) -> bool:
        target_norm = cls._normalize_remote_hint(target_path)
        if not target_norm:
            return False
        for key in ("mobile_target_path", "fnos_target_path", "cloud139_target_path", "cloud139_fnos_target_path", "cmcc_parent_path"):
            hint = cls._normalize_remote_hint(category.get(key) or "")
            if hint and (target_norm == hint or target_norm.startswith(f"{hint.rstrip('/')}/") or hint.startswith(f"{target_norm.rstrip('/')}/")):
                return True
        return False

    @staticmethod
    def _is_rclone_log_pollution_file(value: str) -> bool:
        return RcloneJobFeasibilityEvaluator.is_log_pollution(value)

    def _expected_file_count(self, job: dict[str, Any]) -> int:
        return RcloneJobFeasibilityEvaluator.expected_file_count(job)

    def _refresh_media_after_rclone(self, category_key: str, category: dict[str, Any], events: list[dict[str, Any]], *, trigger: str = "rclone_run_finished") -> dict[str, Any]:
        library_info = self._rclone_fnos_library_info(category_key, category, events)
        library = library_info["library"]
        dir_list = library_info["dir_list"]
        if not dir_list:
            return {
                "success": False,
                "message": "未匹配到飞牛媒体库真实刷新目录，已跳过扫描；请先在媒体库管理中确认分类 dir_list",
                "library": library,
                "dir_list": [],
                "target_hints": library_info.get("target_hints") or [],
            }
        request_payload = {
            "library": library,
            "guid": library_info.get("guid") or "",
            "dir_list": dir_list,
            "target_hints": library_info.get("target_hints") or [],
            "trigger": trigger,
        }
        try:
            refresher = FnosMediaRefresher(self.fnos_config or {})
            if library_info.get("guid"):
                result = refresher.refresh_guid(str(library_info["guid"]), library=library, dir_list=dir_list)
            else:
                result = refresher.refresh(library, dir_list=dir_list)
        except Exception as exc:  # noqa: BLE001
            result = {"success": False, "message": f"飞牛媒体库刷新异常：{exc}", **request_payload}
        else:
            result = {**request_payload, **(result if isinstance(result, dict) else {"success": False, "message": str(result)})}
        trigger_text = "分类完成后" if trigger == "rclone_category_done" else "run 结束后"
        self._append_log(f"rclone {trigger_text}刷新媒体库：{library} dir_list={dir_list} success={result.get('success')}")
        return result

    def _rclone_fnos_library_info(self, category_key: str, category: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        library = str(category.get("fnos_lib") or category.get("label") or category_key).strip()
        target_hints = self._rclone_target_hints(category_key, category, events)
        fallback_dirs = self._absolute_fnos_dirs(category.get("fnos_dir_list") or category.get("fnos_dirs") or [])
        try:
            libraries = FnosMediaRefresher(self.fnos_config or {}).refresh_libraries()
        except Exception:  # noqa: BLE001
            libraries = {}
        items = libraries.get("items") if isinstance(libraries, dict) and isinstance(libraries.get("items"), list) else []
        matched = self._match_fnos_library(items, library)
        if matched:
            actual_dirs = self._match_fnos_target_dirs(matched.get("dir_list"), target_hints)
            return {
                "library": str(matched.get("name") or matched.get("title") or library),
                "guid": str(matched.get("guid") or ""),
                "dir_list": actual_dirs or fallback_dirs,
                "target_hints": target_hints,
            }
        return {"library": library, "guid": "", "dir_list": fallback_dirs, "target_hints": target_hints}

    def _rclone_target_hints(self, category_key: str, category: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
        # 先用本次实际完成文件的父目录作为扫描目录，避免先命中分类根目录后触发整类扫描。
        specific_values: list[Any] = []
        fallback_values: list[Any] = [
            category.get("fnos_target_path"),
            category.get("mobile_target_path"),
            category.get("cmcc_parent_path"),
            category.get("label"),
        ]
        for event in events:
            target_path = str(event.get("target_path") or "").strip()
            if target_path:
                status = str(event.get("status") or "").strip().lower()
                filename = str(event.get("filename") or "").strip()
                if status.startswith("category_") and not filename:
                    fallback_values.append(target_path)
                else:
                    refresh_dir = self._rclone_refresh_dir_for_file(category_key, target_path)
                    if refresh_dir and status in {"done", "success", "skipped_existing"}:
                        specific_values.extend(self._rclone_mount_refresh_hint_variants(refresh_dir, category))
                        specific_values.extend(self._rclone_refresh_hint_variants(refresh_dir, category))
                    elif refresh_dir:
                        fallback_values.extend(self._rclone_mount_refresh_hint_variants(refresh_dir, category))
                        fallback_values.extend(self._rclone_refresh_hint_variants(refresh_dir, category))
        result: list[str] = []
        values = specific_values or fallback_values
        for value in values:
            for item in self._split_fnos_values(value):
                normalized = self._normalize_remote_hint(item)
                if normalized and normalized not in result:
                    result.append(normalized)
        return result

    @classmethod
    def _rclone_refresh_dir_for_file(cls, category_key: str, target_path: str) -> str:
        target_norm = cls._normalize_remote_hint(target_path)
        if not target_norm:
            return ""
        parent = posixpath.dirname(target_norm.strip("/"))
        if not parent or parent == ".":
            return ""
        if str(category_key or "").strip().lower() in EPISODIC_CATEGORY_KEYS:
            basename = posixpath.basename(parent.rstrip("/"))
            if re.fullmatch(r"(?i)season\s+\d{1,4}", basename):
                show_dir = posixpath.dirname(parent.rstrip("/"))
                if show_dir and show_dir != ".":
                    return show_dir.strip("/")
        return parent.strip("/")

    @classmethod
    def _rclone_refresh_hint_variants(cls, refresh_dir: str, category: dict[str, Any]) -> list[str]:
        normalized = cls._normalize_remote_hint(refresh_dir)
        if not normalized:
            return []
        variants: list[str] = [normalized]
        roots = [
            category.get("mobile_target_path"),
            category.get("fnos_target_path"),
            category.get("cloud139_fnos_target_path"),
            category.get("cloud139_mount_path"),
            category.get("cmcc_parent_path"),
            category.get("label"),
        ]
        for root in roots:
            root_norm = cls._normalize_remote_hint(root)
            if not root_norm:
                continue
            root_rel = root_norm.strip("/")
            if normalized == root_rel:
                continue
            if normalized.startswith(f"{root_rel}/"):
                suffix = normalized[len(root_rel) + 1 :].strip("/")
                for tail in cls._path_tails(root_rel):
                    candidate = f"{tail}/{suffix}".strip("/") if suffix else tail
                    if candidate and candidate not in variants:
                        variants.append(candidate)
        label = cls._normalize_remote_hint(category.get("label"))
        if label:
            parts = [part for part in normalized.strip("/").split("/") if part]
            for index, part in enumerate(parts):
                if part == label:
                    candidate = "/".join(parts[index:])
                    if candidate and candidate not in variants:
                        variants.append(candidate)
                    break
        return variants

    def _rclone_mount_refresh_hint_variants(self, refresh_dir: str, category: dict[str, Any]) -> list[str]:
        mount_name = self._normalize_remote_hint(
            self.cloud139_config.get("fnos_mount_name")
            or self.cloud139_config.get("mount_name")
            or self.config.get("fnos_mount_name")
            or ""
        ).strip("/")
        normalized = self._normalize_remote_hint(refresh_dir).strip("/")
        if not mount_name or not normalized:
            return []

        result: list[str] = []
        roots = [
            category.get("cmcc_parent_path"),
            category.get("mobile_target_path"),
            category.get("cloud139_target_path"),
            category.get("fnos_target_path"),
        ]
        for root in roots:
            root_norm = self._normalize_remote_hint(root).strip("/")
            if not root_norm:
                continue
            if normalized == root_norm or normalized.startswith(f"{root_norm}/"):
                suffix = normalized[len(root_norm) :].strip("/")
                category_dir = posixpath.basename(root_norm.rstrip("/"))
                candidate = f"{mount_name}/{category_dir}/{suffix}".strip("/") if suffix else f"{mount_name}/{category_dir}".strip("/")
                if candidate and candidate not in result:
                    result.append(candidate)

        label = self._normalize_remote_hint(category.get("label")).strip("/")
        if label:
            parts = [part for part in normalized.split("/") if part]
            for index, part in enumerate(parts):
                if part == label:
                    suffix = "/".join(parts[index:])
                    candidate = f"{mount_name}/{suffix}".strip("/")
                    if candidate and candidate not in result:
                        result.append(candidate)
                    break
        return result

    @classmethod
    def _match_fnos_library(cls, items: list[Any], library: str) -> dict[str, Any] | None:
        return match_library(items, library)

    @staticmethod
    def _normalize_fnos_library_name(value: Any) -> str:
        return normalize_library_name(value)

    @classmethod
    def _match_fnos_target_dirs(cls, actual_value: Any, target_hints: list[str]) -> list[str]:
        actual_dirs = cls._absolute_fnos_dirs(actual_value)
        if not actual_dirs or not target_hints:
            return []
        for hint in target_hints:
            matches = cls._match_actual_fnos_dirs(actual_dirs, hint)
            if matches and (cls._hint_segment_count(hint) >= 2 or len(matches) == 1 or hint.startswith("/")):
                return matches[:1] if len(matches) > 1 else matches
        return []

    @classmethod
    def _match_actual_fnos_dirs(cls, actual_dirs: list[str], hint: str) -> list[str]:
        return match_actual_dirs(actual_dirs, hint)

    @staticmethod
    def _path_tails(path: str) -> list[str]:
        return path_tails(path)

    @classmethod
    def _hint_segment_count(cls, value: str) -> int:
        return len([part for part in cls._normalize_remote_hint(value).strip("/").split("/") if part])

    @staticmethod
    def _normalize_remote_hint(value: Any) -> str:
        return normalize_remote_hint(value)

    @staticmethod
    def _split_fnos_values(value: Any) -> list[str]:
        return split_values(value)

    @classmethod
    def _absolute_fnos_dirs(cls, value: Any) -> list[str]:
        result: list[str] = []
        for item in cls._split_fnos_values(value):
            normalized = cls._normalize_remote_hint(item)
            if normalized.startswith("/") and normalized not in result:
                result.append(normalized)
        return result

    @classmethod
    def _find_first_value(cls, payload: Any, keys: set[str]) -> Any:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in keys and value not in (None, ""):
                    return value
            for value in payload.values():
                found = cls._find_first_value(value, keys)
                if found not in (None, ""):
                    return found
        if isinstance(payload, list):
            for item in payload:
                found = cls._find_first_value(item, keys)
                if found not in (None, ""):
                    return found
        return None

    def _append_log(self, message: str) -> None:
        self.log_sink.append(message)

    def _script_path(self) -> Path:
        return self.worker_command.script_path()

    def _command(self, script_path: Path) -> list[str]:
        return self.worker_command.command(script_path)

    def _env(
        self,
        run_id: int | None = None,
        file_retry: dict[str, Any] | None = None,
        category_filter: str = "",
        trigger_reason: str = "",
        staging_run: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        app_callback_url = str(self.config.get("app_callback_url") or "")
        category_dirs = self._category_dir_env(env)
        cmcc_env = self._cmcc_env()
        persisted_staging_run = self._validated_staging_run(staging_run)
        if staging_run is not None and not persisted_staging_run:
            raise ValueError("持久化暂存搬运参数无效，禁止按当前后台配置降级")
        if persisted_staging_run:
            suffix = persisted_staging_run["category_suffix"]
            source_root = self._remote_dir(persisted_staging_run["source_category_root"])
            destination_root = self._remote_dir(persisted_staging_run["storage_staging_category_root"])
            category_dirs[f"RCLONE_SRC_{suffix}_DIR"] = source_root
            category_dirs[f"RCLONE_DST_{suffix}_DIR"] = destination_root
            cmcc_env["RCLONE_UPLOAD_BACKEND"] = persisted_staging_run["storage_backend"]
            cmcc_env[f"CMCC_TARGET_{suffix}_PARENT_FILE_ID"] = ""
            cmcc_env[f"CMCC_TARGET_{suffix}_PARENT_PATH"] = destination_root
        retry_file_filter = (file_retry or {}).get("filename", "")
        if persisted_staging_run and (file_retry or {}).get("source_path"):
            source_path = self._remote_dir((file_retry or {}).get("source_path"))
            source_root = self._remote_dir(persisted_staging_run.get("source_category_root"))
            if source_root and source_path.casefold().startswith(f"{source_root.casefold()}/"):
                retry_file_filter = source_path[len(source_root) + 1 :]
        only_category = (file_retry or {}).get("category", "") or str(category_filter or "").strip()
        if persisted_staging_run:
            only_category = persisted_staging_run["category"]
        values = {
            "CONTAINER_NAME": self.config.get("container_name", "rclone-server"),
            "RCLONE_EXEC_USER": self.config.get("exec_user", "10001:10001"),
            "REMOTE_NAME": self.config.get("remote_name", "MP"),
            "RCLONE_UPLOAD_BACKEND": (
                persisted_staging_run.get("storage_backend")
                if persisted_staging_run
                else self.config.get("upload_backend") or cmcc_env.get("RCLONE_UPLOAD_BACKEND") or "cmcc_api"
            ),
            "LOCAL_TEMP": self.config.get("local_temp", "/temp/fnos-media-import"),
            "BUFFER_SIZE": self.config.get("buffer_size", "128M"),
            "DOWNLOAD_MULTI_THREAD": self.config.get("download_multi_thread", 16),
            "UPLOAD_MULTI_THREAD": self.config.get("upload_multi_thread", 1),
            "MAX_RETRIES": self.config.get("max_retries", 3),
            "DOWNLOAD_MAX_RETRIES": self.config.get("download_max_retries", self.config.get("max_retries", 3)),
            "UPLOAD_MAX_RETRIES": self.config.get("upload_max_retries", 1),
            "RETRY_DELAY": self.config.get("retry_delay", 5),
            "RCLONE_DOWNLOAD_RETRIES": self.config.get("download_retries", 2),
            "RCLONE_UPLOAD_RETRIES": self.config.get("upload_retries", 1),
            "RCLONE_LOW_LEVEL_RETRIES": self.config.get("low_level_retries", 3),
            "RCLONE_TIMEOUT": self.config.get("timeout", "10m"),
            "RCLONE_CONNECT_TIMEOUT": self.config.get("connect_timeout", "30s"),
            "RCLONE_REPLACE_SIZE_MISMATCH": self.config.get("replace_size_mismatch", "true"),
            "RCLONE_SOURCE_SETTLE_SECONDS": self.config.get("source_settle_seconds", 30),
            "RCLONE_MANUAL_SOURCE_SETTLE_SECONDS": self.config.get("manual_source_settle_seconds", 0),
            "RCLONE_SOURCE_SETTLE_ROUNDS": self.config.get("source_settle_rounds", 2),
            "RCLONE_SOURCE_APPEAR_WAIT_SECONDS": self.config.get("source_appear_wait_seconds", 180),
            "RCLONE_SOURCE_APPEAR_POLL_SECONDS": self.config.get("source_appear_poll_seconds", 15),
            "RCLONE_REFRESH_IN_WORKER": self.config.get("refresh_in_worker", "false"),
            "RCLONE_TEMP_RETENTION_HOURS": self.config.get("temp_retention_hours", 72),
            "RCLONE_TEMP_CLEANUP_TIMEOUT": self.config.get("temp_cleanup_timeout", 20),
            "RCLONE_UPLOAD_MAX_DURATION": self.config.get("upload_max_duration", "3h"),
            "RCLONE_DOWNLOAD_MAX_DURATION": self.config.get("download_max_duration", "3h"),
            "RCLONE_UPLOAD_STALL_TIMEOUT": self.config.get("upload_stall_timeout", 600),
            "RCLONE_UPLOAD_COMPLETE_STALL_TIMEOUT": self.config.get("upload_complete_stall_timeout", 120),
            "RCLONE_UPLOAD_PENDING_VERIFY_SECONDS": self.config.get("upload_pending_verify_seconds", 600),
            "RCLONE_UPLOAD_PENDING_VERIFY_POLL_SECONDS": self.config.get("upload_pending_verify_poll_seconds", 30),
            "RCLONE_UPLOAD_PENDING_TTL": self.config.get("upload_pending_ttl", 7200),
            "RCLONE_DELETE_PARTIAL_ON_UPLOAD_FAIL": self.config.get("delete_partial_on_upload_fail", "true"),
            "RCLONE_VERIFY_SIZE_RETRIES": self.config.get("verify_size_retries", 6),
            "RCLONE_VERIFY_SIZE_DELAY": self.config.get("verify_size_delay", 5),
            "RCLONE_LOCAL_OVERSIZE_BYTES": self.config.get("local_oversize_bytes", 10 * 1024 * 1024),
            "PYTHON_BIN": self.config.get("python_bin", "python"),
            "APP_CALLBACK_URL": app_callback_url,
            "RCLONE_RUN_ID": run_id or "",
            "RCLONE_TRIGGER_REASON": trigger_reason,
            "RCLONE_ONLY_CATEGORY": only_category,
            "RCLONE_ONLY_FILE": retry_file_filter,
            "RCLONE_RETRY_EVENT_ID": (file_retry or {}).get("event_id", ""),
            "RCLONE_ONLY_JOB_DIR": persisted_staging_run.get("job_dir_name", "") if persisted_staging_run else "",
            "RCLONE_STAGING_ENABLED": "true" if persisted_staging_run or self.config.get("staging_enabled") else "false",
            "LOCK_FILE": self.config.get("lock_file", "/tmp/fnos_media_import_rclone.lock"),
            **category_dirs,
            **cmcc_env,
        }
        for key, value in values.items():
            env[key] = str(value)
        return env

    @classmethod
    def _validated_staging_run(cls, staging_run: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(staging_run, dict):
            return {}
        try:
            job_id = int(staging_run.get("job_id") or 0)
        except (TypeError, ValueError):
            return {}
        category = cls._normalize_filter_text(staging_run.get("category"))
        category_suffix = ""
        for key, label, job_name, suffix in RcloneDirectoryMappingValidator.CATEGORIES:
            aliases = {cls._normalize_filter_text(value) for value in (key, label, job_name, suffix)}
            if category in aliases:
                category_suffix = suffix
                category = key
                break
        job_dir_name = str(staging_run.get("job_dir_name") or "").strip()
        source_root = str(staging_run.get("source_category_root") or "").strip().replace("\\", "/")
        destination_root = str(staging_run.get("storage_staging_category_root") or "").strip().replace("\\", "/")
        backend = str(staging_run.get("storage_backend") or "").strip().lower()
        if (
            job_id <= 0
            or not category_suffix
            or job_dir_name != f"job-{job_id}"
            or not source_root
            or not destination_root
            or backend not in {"cmcc_api", "webdav"}
        ):
            return {}
        return {
            "job_id": job_id,
            "category": category,
            "category_suffix": category_suffix,
            "job_dir_name": job_dir_name,
            "source_category_root": source_root,
            "storage_staging_category_root": destination_root,
            "storage_backend": backend,
        }

    def _database_bound_staging_run(
        self,
        staging_run: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str]:
        requested = self._validated_staging_run(staging_run)
        if not requested:
            return {}, "持久化暂存搬运参数无效，已拒绝按当前后台配置降级搬运"
        if not self.db:
            return {}, "数据库未初始化，无法核对任务固化的 staging_plan，已拒绝启动搬运"

        job_id = self._int_value(requested.get("job_id"))
        job = self.db.get_job(job_id) if job_id > 0 else None
        if not isinstance(job, dict):
            return {}, f"任务 #{job_id or '?'} 不存在，无法核对 staging_plan，已拒绝启动搬运"
        status = str(job.get("status") or "").strip().lower()
        if self._staging_status_blocks_execution(status):
            return {}, f"任务 #{job_id} 当前状态为 {status}，已拒绝启动搬运"
        try:
            persisted = rclone_staging_run_from_job(job)
        except ValueError as exc:
            return {}, f"任务 #{job_id} 的固化 staging_plan 无效，已拒绝启动搬运：{exc}"
        persisted = self._validated_staging_run(persisted)
        if not persisted:
            return {}, f"任务 #{job_id} 无法从固化 staging_plan 派生 rclone 参数，已拒绝启动搬运"
        if self._staging_run_identity(requested) != self._staging_run_identity(persisted):
            return {}, f"任务 #{job_id} 的搬运参数与数据库固化 staging_plan 不一致，已拒绝启动"
        return persisted, ""

    @staticmethod
    def _staging_run_identity(staging_run: dict[str, Any]) -> tuple[Any, ...]:
        return (
            int(staging_run.get("job_id") or 0),
            str(staging_run.get("category") or "").strip().casefold(),
            str(staging_run.get("job_dir_name") or "").strip().casefold(),
            str(staging_run.get("source_category_root") or "")
            .strip()
            .replace("\\", "/")
            .strip("/")
            .casefold(),
            str(staging_run.get("storage_staging_category_root") or "")
            .strip()
            .replace("\\", "/")
            .strip("/")
            .casefold(),
            str(staging_run.get("storage_backend") or "").strip().casefold(),
        )

    def _category_dir_env(self, env: dict[str, str]) -> dict[str, str]:
        mapping = {
            "movie": ("MOVIE", "离线下载/电影", "移动云盘A/电影"),
            "tv": ("TV", "离线下载/电视剧", "移动云盘A/电视剧"),
            "anime": ("ANIME", "离线下载/动漫", "移动云盘A/动漫"),
            "variety": ("VARIETY", "离线下载/综艺", "移动云盘A/综艺"),
            "other": ("OTHER", "离线下载/其他", "移动云盘A/其他"),
        }
        result: dict[str, str] = {}
        for category_key, (env_suffix, default_src, default_dst) in mapping.items():
            category = self.categories.get(category_key, {})
            src_env = f"RCLONE_SRC_{env_suffix}_DIR"
            dst_env = f"RCLONE_DST_{env_suffix}_DIR"
            result[src_env] = self._remote_dir(category.get("quark_save_path", "")) or self._remote_dir(env.get(src_env)) or default_src
            # RCLONE_DST_* only describes the WebDAV/rclone destination.  The
            # cmcc_api backend gets its separate official path from _cmcc_env.
            final_destination = self._remote_dir(category.get("mobile_target_path", "")) or self._remote_dir(env.get(dst_env)) or default_dst
            if self.config.get("staging_enabled"):
                final_destination = staging_category_root(
                    final_destination,
                    category_label=category.get("label") or category_key,
                    staging_dir_name=self.config.get("staging_dir_name"),
                )
            result[dst_env] = self._remote_dir(final_destination)
        return result

    def _cmcc_env(self) -> dict[str, str]:
        cmcc = self.cmcc_upload_config if isinstance(self.cmcc_upload_config, dict) else {}
        result: dict[str, str] = {}

        def put(name: str, value: Any) -> None:
            text = str(value or "").strip()
            if text:
                result[name] = text

        enabled = str(cmcc.get("enabled", True)).strip().lower()
        backend = self.config.get("upload_backend") or cmcc.get("backend") or ("webdav" if enabled in {"0", "false", "no", "off"} else "cmcc_api")
        put("RCLONE_UPLOAD_BACKEND", backend)
        put("CMCC_UPLOAD_MODE", cmcc.get("mode") or "rapid_first")
        put("CMCC_UPLOAD_RENAME_MODE", cmcc.get("rename_mode") or "auto_rename")
        put("CMCC_UPLOAD_PUT_TIMEOUT", cmcc.get("put_timeout"))
        put("CMCC_HOST", cmcc.get("host"))
        put("CM_CLOUD_HOST", cmcc.get("host"))
        put("CMCC_AUTH_MODE", "web_basic")
        put("CMCC_ACCESS_TOKEN", cmcc.get("access_token"))
        put("CMCC_PHONE", cmcc.get("phone"))
        put("CM_CLOUD_ACCESS_TOKEN", cmcc.get("access_token"))
        put("CM_CLOUD_PHONE", cmcc.get("phone"))

        category_mapping = {
            "movie": "MOVIE",
            "tv": "TV",
            "anime": "ANIME",
            "variety": "VARIETY",
            "other": "OTHER",
        }
        for category_key, suffix in category_mapping.items():
            category = self.categories.get(category_key, {})
            # 文件仍由 rclone 下载到本地，但 CMCC API 的真实写入目录必须
            # 与 139 官方直转目录一致。OpenList 挂载路径属于另一命名空间，
            # 不能拿来调用 CMCC ensure_path。
            put(f"CMCC_TARGET_{suffix}_PARENT_FILE_ID", "")
            put(f"CMCC_TARGET_{suffix}_PARENT_PATH", self._cmcc_parent_path_for_category(category_key, category))
        return result

    def _cmcc_parent_path_for_category(self, category_key: str, category: dict[str, Any]) -> str:
        """返回 rclone + 移动云 API 上传父目录。"""

        final_root = cmcc_upload_root(category, self.cloud139_config) or str(category_key or "").strip()
        if not self.config.get("staging_enabled"):
            return final_root
        return staging_category_root(
            final_root,
            category_label=category.get("label") or category_key,
            staging_dir_name=self.config.get("staging_dir_name"),
        )

    @classmethod
    def _validate_rclone_directory_mapping(cls, mapping: dict[str, str], *, category_filter: str = "") -> list[str]:
        return RcloneDirectoryMappingValidator.validate(mapping, category_filter=category_filter)

    @classmethod
    def _remote_dirs_overlap(cls, left: Any, right: Any) -> bool:
        return RcloneDirectoryMappingValidator.overlap(left, right)

    @staticmethod
    def _normalize_remote_dir(value: Any) -> str:
        return RcloneDirectoryMappingValidator.normalize(value)

    @staticmethod
    def _normalize_filter_text(value: Any) -> str:
        return RcloneDirectoryMappingValidator.normalize_filter(value)

    @staticmethod
    def _remote_dir(value: Any) -> str:
        return RcloneDirectoryMappingValidator.clean(value)

    @staticmethod
    def _now() -> str:
        return datetime.now().replace(microsecond=0).isoformat()


def _safe_container_temp_root(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text.startswith("/"):
        return ""
    raw_parts = [part for part in text.split("/") if part]
    if any(part in {".", ".."} for part in raw_parts):
        return ""
    normalized = posixpath.normpath(text)
    return normalized if normalized not in {"", ".", "/"} else ""


def _dedupe_texts(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result
