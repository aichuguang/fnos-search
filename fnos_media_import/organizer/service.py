from __future__ import annotations

import hashlib
import posixpath
import logging
import math
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote
from typing import Any

from ..constants import (
    CATEGORY_LABELS,
    EVENT_ERROR,
    EVENT_INFO,
    EVENT_WARN,
    JOB_CANCELLED,
    JOB_CONFIRMING,
    JOB_DONE,
    JOB_ORGANIZING,
    JOB_REVIEW,
    JOB_STATUS_LABELS,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
)
from ..database import Database
from ..media.fnos import FnosMediaRefresher
from ..media_path_rules import sanitize_resource_dir_name, split_title_year
from ..services.import_staging_service import validated_staging_plan_from_job
from .ai import AiCalibrator
from .openlist_client import (
    VIDEO_EXTENSIONS,
    OpenListClient,
    OpenListEndpointUnsupported,
    OpenListError,
    OpenListTransientError,
    basename,
    dirname,
    join_path,
    normalize_path,
)
from .parser import EPISODIC_CATEGORIES, category_target_root, evidence_title, extract_season_from_title, extract_version_tags, parse_file_name, sanitize_candidate, standard_target_path
from .run_lease import OrganizerRunLease, OrganizerRunLeaseLost, OrganizerScanLease, OrganizerScanLeaseLost
from .tmdb import TmdbClient, score_tmdb_result

AD_FILE_DELETE_THRESHOLD_BYTES = 50 * 1024 * 1024
OPENLIST_STRM_SCAN_ENDPOINT = "/api/admin/scan/start"
COMPANION_SUBTITLE_EXTENSIONS = {".ass", ".ssa", ".srt", ".sup", ".idx", ".sub", ".vtt"}
COMPANION_METADATA_EXTENSIONS = {".nfo", ".jpg", ".jpeg", ".png", ".webp"}
ORGANIZER_CANCELLATION_TERMINAL_STATUSES = frozenset({"done", "cancelled", "skipped"})
ORGANIZER_CANCELLATION_MAX_ATTEMPTS = 3
LINKED_JOB_IMMUTABLE_STATUSES = frozenset(
    {JOB_CANCELLED, JOB_DONE, "success", "skipped_existing", "unsupported", "rejected"}
)
logger = logging.getLogger(__name__)


class OrganizerTaskCancelled(RuntimeError):
    """Raised when an Organizer side-effect must stop for a cancelled/skipped task."""


class OrganizerService:
    def __init__(
        self,
        db: Database,
        config: dict[str, Any],
        categories: dict[str, dict[str, Any]],
        fnos: FnosMediaRefresher | None = None,
        *,
        recover_on_startup: bool = True,
        owner_id: str = "",
    ) -> None:
        self.db = db
        self.config = config or {}
        self.categories = categories or {}
        self.fnos = fnos
        self.owner_id = str(owner_id or f"organizer:{os.getpid()}:{id(self)}")
        self.openlist = OpenListClient(self.config.get("openlist", {}))
        self.tmdb = TmdbClient(self.config.get("tmdb", {}))
        self.ai = AiCalibrator(self.config.get("ai", {}))
        self.organizer_config = self.config.get("organizer", {})
        self._timers: dict[int, threading.Timer] = {}
        self._worker_context = threading.local()
        self._background_suspended = False
        self._background_apply_lock = threading.Lock()
        self._background_apply_tasks: set[int] = set()
        try:
            scan_concurrency = int(self.organizer_config.get("max_concurrent_scans") or 1)
        except (TypeError, ValueError):
            scan_concurrency = 1
        self._scan_semaphore = threading.BoundedSemaphore(max(1, min(scan_concurrency, 4)))
        if recover_on_startup:
            self._recover_stale_runs_on_startup()
            self._recover_completed_linked_jobs_on_startup()
            self._recover_transient_scan_tasks_on_startup(include_scanning=True)

    @property
    def enabled(self) -> bool:
        return bool(self.organizer_config.get("enabled", False))

    def _recover_stale_runs_on_startup(self) -> None:
        try:
            try:
                result = self.db.recover_stale_organizer_runs(
                    owner_id=self.owner_id,
                )
            except TypeError:
                result = self.db.recover_stale_organizer_runs()
            count = int(result.get("count") or 0) if isinstance(result, dict) else int(result or 0)
            if count:
                logger.warning("recovered stale organizer runs on startup: %s", count)
            affected_jobs = result.get("jobs") if isinstance(result, dict) else []
            for item in affected_jobs or []:
                task_id = _safe_positive_int(item.get("task_id"))
                job_id = _safe_positive_int(item.get("job_id"))
                if not task_id or not job_id:
                    continue
                job = self.db.get_job(job_id) or {}
                if str(job.get("status") or "") in {JOB_DONE, JOB_REVIEW, JOB_CANCELLED}:
                    continue
                task = self.db.get_organizer_task(task_id, include_children=False) or {}
                self._sync_linked_job(
                    task,
                    status=JOB_REVIEW,
                    stage="review",
                    message="服务重启后检测到 Organizer 执行中断，已停止显示整理中，请重新扫描或重试整理",
                    level=EVENT_WARN,
                    error_message="服务重启后检测到 Organizer 执行中断",
                )
        except Exception:  # noqa: BLE001
            logger.debug("recover stale organizer runs failed", exc_info=True)

    def _recover_completed_linked_jobs_on_startup(self) -> dict[str, Any]:
        """Repair the crash window between Organizer completion and Job sync."""

        recovered_task_ids: list[int] = []
        failed_task_ids: list[int] = []
        try:
            for organizer_status in ("done", "skipped"):
                offset = 0
                while True:
                    page = self.db.list_organizer_tasks(limit=200, status=organizer_status, offset=offset)
                    if not page:
                        break
                    for task in page:
                        if organizer_status == "skipped" and not self._skipped_task_has_completion_evidence(task):
                            continue
                        task_id = _safe_positive_int(task.get("id"))
                        job_id = _safe_positive_int(task.get("job_id"))
                        if not task_id or not job_id:
                            continue
                        job = self.db.get_job(job_id) or {}
                        current_status = str(job.get("status") or "").strip().lower()
                        if not current_status:
                            continue
                        if current_status in LINKED_JOB_IMMUTABLE_STATUSES and current_status != JOB_DONE:
                            continue
                        evidence = task.get("evidence") if isinstance(task.get("evidence"), dict) else {}
                        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
                        confirmation = (
                            evidence.get("completion_confirmation")
                            if isinstance(evidence.get("completion_confirmation"), dict)
                            else {}
                        )
                        strm_completion = (
                            raw_data.get("strm_completion")
                            if isinstance(raw_data.get("strm_completion"), dict)
                            else evidence.get("strm_completion")
                            if isinstance(evidence.get("strm_completion"), dict)
                            else {}
                        )
                        extra: dict[str, Any] = {}
                        if confirmation:
                            extra["confirmation"] = confirmation
                        if strm_completion:
                            extra["strm_completion"] = strm_completion
                        organized_target_path = str(confirmation.get("organized_target_path") or "").strip()
                        target_dirs = confirmation.get("target_dirs") if isinstance(confirmation.get("target_dirs"), list) else []
                        if organized_target_path:
                            extra["organized_target_path"] = organized_target_path
                        if target_dirs:
                            extra["target_dirs"] = target_dirs
                        if organizer_status == "skipped":
                            extra["organizer_skipped"] = True
                            recovery_message = "服务重启后根据已跳过的 Organizer 任务补写入库完成状态"
                        else:
                            recovery_message = "服务重启后根据已完成的 Organizer 任务补写入库完成状态"
                        try:
                            recovered = self._sync_linked_job(
                                task,
                                status=JOB_DONE,
                                stage="done",
                                message=recovery_message,
                                extra=extra,
                            )
                        except Exception:  # noqa: BLE001
                            failed_task_ids.append(task_id)
                            logger.warning(
                                "organizer_completed_job_recovery_failed task_id=%s job_id=%s organizer_status=%s",
                                task_id,
                                job_id,
                                organizer_status,
                                exc_info=True,
                            )
                            continue
                        if recovered:
                            recovered_task_ids.append(task_id)
                    if len(page) < 200:
                        break
                    offset += len(page)
        except Exception:  # noqa: BLE001
            logger.debug("recover completed organizer linked jobs failed", exc_info=True)
        if recovered_task_ids:
            logger.warning(
                "recovered completed organizer linked jobs on startup: %s",
                len(recovered_task_ids),
            )
        return {
            "success": not failed_task_ids,
            "recovered_task_ids": recovered_task_ids,
            "failed_task_ids": failed_task_ids,
        }

    @staticmethod
    def _skipped_task_has_completion_evidence(task: dict[str, Any]) -> bool:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        staging_plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        if not staging_plan.get("enabled"):
            return True
        evidence = task.get("evidence") if isinstance(task.get("evidence"), dict) else {}
        confirmation = (
            evidence.get("completion_confirmation")
            if isinstance(evidence.get("completion_confirmation"), dict)
            else {}
        )
        return confirmation.get("success") is True

    def _recover_transient_scan_tasks_on_startup(self, *, include_scanning: bool = True) -> None:
        if not self.enabled or not self.openlist.configured:
            return
        try:
            tasks: list[dict[str, Any]] = []
            statuses = ["failed", "waiting_openlist", "stabilizing", "pending", "strm_pending"]
            if include_scanning:
                statuses.append("scanning")
            for status in statuses:
                offset = 0
                while True:
                    page = self.db.list_organizer_tasks(limit=100, status=status, offset=offset)
                    tasks.extend(page)
                    if len(page) < 100:
                        break
                    offset += len(page)
            for task in tasks:
                abort_reason = self._task_abort_reason(task)
                if abort_reason and "关联入库任务已取消" in abort_reason:
                    self._cancel_task_for_cancelled_job(task, abort_reason)
                    continue
                # 启用新任务暂存后，只自动恢复带固化 staging_plan 的新任务。
                # 历史 Organizer 任务仍可人工处理，但服务重启不能主动重新扫描。
                if self.organizer_config.get("staging_enabled", True) and not self._task_has_staging_plan(task):
                    continue
                status = str(task.get("status") or "").strip().lower()
                if status == "strm_pending":
                    raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
                    strm_state = raw_data.get("strm_completion") if isinstance(raw_data.get("strm_completion"), dict) else {}
                    if str(strm_state.get("status") or "") != "pending":
                        continue
                    task_id = _safe_positive_int(task.get("id"))
                    if task_id:
                        delay = _remaining_retry_delay_seconds(strm_state.get("next_retry_at"), fallback=5)
                        self._schedule_task_after(task_id, delay)
                    continue
                if status == "failed":
                    error = str(task.get("error_message") or "").lower()
                    if not self._task_retries_openlist_visibility(task):
                        continue
                    if not self._retryable_openlist_visibility_error(OpenListError(error)):
                        continue
                raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
                retry_state = raw_data.get("openlist_visibility_retry") if isinstance(raw_data.get("openlist_visibility_retry"), dict) else {}
                if retry_state.get("exhausted"):
                    continue
                task_id = _safe_positive_int(task.get("id"))
                if not task_id:
                    continue
                if status == "scanning":
                    lease_expires_at = str(task.get("scan_lease_expires_at") or "").strip()
                    if lease_expires_at and lease_expires_at > _utc_now_text():
                        # Another process still owns the scan.  Startup recovery
                        # must never turn a live cross-process claim back into a
                        # waiting task.
                        continue
                message = (
                    "服务重启后恢复任务级暂存扫描"
                    if status in {"waiting_openlist", "stabilizing", "pending", "scanning"}
                    else "检测到 OpenList 瞬时扫描故障，已自动恢复为等待扫描"
                )
                update_values: dict[str, Any] = {
                    "status": "waiting_openlist",
                    "error_message": "",
                    "expected_statuses": {status},
                }
                if status == "scanning":
                    update_values.update(
                        {
                            "expected_revision": _safe_positive_int(task.get("revision")),
                            "bump_revision": True,
                            "clear_scan_lease": True,
                        }
                    )
                try:
                    recovered = self.db.update_organizer_task(task_id, **update_values)
                except TypeError:
                    recovered = self.db.update_organizer_task(task_id, status="waiting_openlist", error_message="")
                if recovered is False:
                    continue
                self._sync_linked_job(
                    task,
                    status=JOB_WAITING_OPENLIST,
                    stage="waiting_openlist",
                    message=message,
                    level=EVENT_INFO,
                    error_message="",
                )
                delay = _remaining_retry_delay_seconds(retry_state.get("next_retry_at"), fallback=5)
                self._schedule_task_after(task_id, delay)
        except Exception:  # noqa: BLE001
            logger.debug("recover transient organizer scan tasks failed", exc_info=True)

    def status(self) -> dict[str, Any]:
        staging_enabled = bool(self.enabled and self.organizer_config.get("staging_enabled", True))
        return {
            "enabled": self.enabled,
            "staging_enabled": staging_enabled,
            "staging_dir_name": str(self.organizer_config.get("staging_dir_name") or "_入库暂存").strip(),
            "openlist_configured": self.openlist.configured,
            "tmdb_configured": self.tmdb.configured,
            "ai_configured": self.ai.configured,
            "strm_refresh_enabled": bool(self.organizer_config.get("strm_refresh_after_apply") or staging_enabled),
            # Organizer 只负责触发 OpenList 文件夹刷新。STRM 的生成、同步和
            # 旧目录维护全部交还 OpenList，后台不再把这些动作作为任务完成条件。
            "strm_cleanup_old_enabled": False,
            "local_strm_root_configured": bool(str(self.organizer_config.get("local_strm_root") or "").strip()),
            "fnos_refresh_after_apply": bool(self.organizer_config.get("refresh_fnos_after_apply")),
        }

    def _sync_linked_job(
        self,
        task: dict[str, Any],
        *,
        status: str,
        stage: str,
        message: str,
        level: str = EVENT_INFO,
        extra: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> bool:
        job_id = _safe_positive_int((task or {}).get("job_id"))
        if not job_id:
            return False
        job = self.db.get_job(job_id)
        current_status = str((job or {}).get("status") or "").strip()
        if not isinstance(job, dict) or not current_status:
            return False
        if current_status.lower() in LINKED_JOB_IMMUTABLE_STATUSES:
            if current_status.lower() == status.lower():
                return bool(
                    self._sync_linked_guest_requests(
                        job_id,
                        status=status,
                        organizer_task_id=_safe_positive_int((task or {}).get("id")),
                    )
                )
            logger.info(
                "organizer_linked_job_terminal_skip_sync task_id=%s job_id=%s current_status=%s requested_status=%s",
                (task or {}).get("id"),
                job_id,
                current_status,
                status,
            )
            return False
        raw_data = job.get("raw_data") if isinstance(job, dict) and isinstance(job.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        payload = {
            "stage": stage,
            "official_save_path": completion.get("official_save_path") or (job or {}).get("target_path") or "",
            "openlist_visible_path": completion.get("openlist_visible_path") or task.get("openlist_root_path") or "",
            "organizer_scan_path": completion.get("organizer_scan_path") or task.get("openlist_root_path") or "",
            "organized_target_path": completion.get("organized_target_path") or "",
            "checks": completion.get("checks") if isinstance(completion.get("checks"), list) else [],
            "organizer_task_id": task.get("id"),
            "message": message,
            **(extra or {}),
        }
        updates = {
            "status": status,
            "error_message": error_message,
            "raw_data": _merge_raw_data(raw_data, {"completion": payload}),
        }
        updater = getattr(self.db, "update_job_if_status", None)
        if callable(updater):
            updated = bool(updater(job_id, {current_status}, **updates))
        else:
            latest = self.db.get_job(job_id) or {}
            if str(latest.get("status") or "").strip() != current_status:
                updated = False
            else:
                self.db.update_job(job_id, **updates)
                updated = True
        if not updated:
            latest = self.db.get_job(job_id) or job
            logger.info(
                "organizer_linked_job_cas_conflict task_id=%s job_id=%s expected_status=%s current_status=%s requested_status=%s",
                (task or {}).get("id"),
                job_id,
                current_status,
                str(latest.get("status") or ""),
                status,
            )
            return False
        self.db.add_event(job_id, level, message, {"completion": payload})
        self._sync_linked_guest_requests(
            job_id,
            status=status,
            organizer_task_id=_safe_positive_int((task or {}).get("id")),
        )
        return True

    def _sync_linked_guest_requests(
        self,
        job_id: int,
        *,
        status: str,
        organizer_task_id: int | None,
    ) -> int:
        list_requests = getattr(self.db, "list_guest_requests_by_job", None)
        transition_request = getattr(self.db, "transition_guest_request_with_event", None)
        if not callable(list_requests) or not callable(transition_request):
            return 0
        public_status = JOB_STATUS_LABELS.get(status, status)
        changed = 0
        for item in list_requests(job_id):
            request_id = _safe_positive_int(item.get("id"))
            current_status = str(item.get("status") or "").strip().lower()
            if not request_id or current_status in {"rejected", "cancelled", "unsupported"}:
                continue
            if current_status == status and str(item.get("public_status") or "") == public_status:
                continue
            sync_data = {
                "job_id": job_id,
                "job_status": status,
                "organizer_task_id": organizer_task_id,
            }
            transitioned = transition_request(
                request_id,
                expected_statuses={current_status},
                status=status,
                public_status=public_status,
                raw_data=_merge_raw_data(item.get("raw_data"), {"status_sync": sync_data}),
                level=EVENT_INFO,
                message="系统同步关联正式任务状态",
                event_data={**sync_data, "previous_status": current_status},
            )
            if transitioned:
                changed += 1
        return changed

    def _task_abort_reason(
        self,
        task: dict[str, Any] | None,
        *,
        allowed_statuses: set[str] | None = None,
    ) -> str:
        if not isinstance(task, dict) or not task:
            return "Organizer 任务不存在"
        task_status = str(task.get("status") or "").strip().lower()
        if task_status in {"cancelled", "skipped"}:
            return f"Organizer 任务已{'取消' if task_status == 'cancelled' else '跳过'}"
        if allowed_statuses is not None and task_status not in allowed_statuses and "status" in task:
            return f"Organizer 任务状态已变化：{task_status or '未知'}"
        job_id = _safe_positive_int(task.get("job_id"))
        if job_id:
            get_job = getattr(self.db, "get_job", None)
            if callable(get_job):
                try:
                    job = get_job(job_id) or {}
                except Exception:  # noqa: BLE001
                    job = {}
                if str(job.get("status") or "").strip().lower() == JOB_CANCELLED:
                    return "关联入库任务已取消"
        return ""

    def _ensure_task_active(
        self,
        task_id: int,
        *,
        allowed_statuses: set[str] | None = None,
        expected_revision: int | None = None,
        scan_owner: str = "",
    ) -> dict[str, Any]:
        task = self.db.get_organizer_task(task_id)
        reason = self._task_abort_reason(task, allowed_statuses=allowed_statuses)
        if not reason and expected_revision is not None:
            current_revision = _safe_positive_int((task or {}).get("revision")) or 1
            if current_revision != max(1, int(expected_revision)):
                reason = "Organizer 任务版本已变化，停止写入旧扫描结果"
        if not reason and scan_owner:
            current_owner = str((task or {}).get("scan_owner") or "")
            if current_owner != str(scan_owner):
                reason = "Organizer 扫描已由其他执行器接管"
        if reason:
            raise OrganizerTaskCancelled(reason)
        return task or {}

    def _update_organizer_task_from_snapshot(
        self,
        task: dict[str, Any] | None,
        **updates: Any,
    ) -> bool:
        if not isinstance(task, dict):
            return False
        task_id = _safe_positive_int(task.get("id"))
        expected_status = str(task.get("status") or "").strip()
        expected_revision = _safe_positive_int(task.get("revision"))
        if not task_id or expected_status.lower() in ORGANIZER_CANCELLATION_TERMINAL_STATUSES:
            return False
        if not expected_status:
            # Lightweight/legacy adapters sometimes expose partial task rows
            # without status/revision.  Production Database rows always carry
            # both fields and therefore always use the CAS branch below.
            result = self.db.update_organizer_task(task_id, **updates)
            return result is not False
        guarded = {
            **updates,
            "expected_statuses": {expected_status},
        }
        if expected_revision:
            guarded["expected_revision"] = expected_revision
        try:
            result = self.db.update_organizer_task(task_id, **guarded)
        except TypeError:
            latest = self.db.get_organizer_task(task_id, include_children=False) or {}
            latest_status = str(latest.get("status") or "").strip()
            latest_revision = _safe_positive_int(latest.get("revision"))
            if latest_status != expected_status or (
                expected_revision and latest_revision and latest_revision != expected_revision
            ):
                return False
            result = self.db.update_organizer_task(task_id, **updates)
        return result is not False

    def _cancel_organizer_task(
        self,
        task: dict[str, Any],
        reason: str,
    ) -> tuple[str, dict[str, Any]]:
        """Cancel one task without allowing a stale snapshot to replace ``done``.

        The production database exposes a dedicated atomic cancellation update.
        The guarded CAS fallback keeps lightweight test/adaptor databases
        compatible and retries after re-reading a concurrently changed task.
        """

        task_id = _safe_positive_int(task.get("id"))
        if not task_id:
            return "missing", {}
        latest = task
        atomic_cancel = getattr(self.db, "cancel_organizer_task", None)
        getter = getattr(self.db, "get_organizer_task", None)
        for attempt in range(ORGANIZER_CANCELLATION_MAX_ATTEMPTS):
            if attempt and callable(getter):
                latest = getter(task_id, include_children=False) or {}
            status = str(latest.get("status") or "").strip().lower()
            if not latest:
                return "missing", {}
            if status in ORGANIZER_CANCELLATION_TERMINAL_STATUSES:
                return status, latest
            if callable(atomic_cancel):
                updated = atomic_cancel(task_id, reason=reason)
            else:
                updated = self.db.update_organizer_task(
                    task_id,
                    status="cancelled",
                    error_message=reason,
                    expected_revision=_safe_positive_int(latest.get("revision")) or 1,
                    expected_statuses={status},
                    bump_revision=True,
                    clear_scan_lease=True,
                )
            if updated:
                current = getter(task_id, include_children=False) if callable(getter) else None
                return "cancelled", current or {**latest, "status": "cancelled", "error_message": reason}
            if callable(getter):
                latest = getter(task_id, include_children=False) or {}
                current_status = str(latest.get("status") or "").strip().lower()
                if current_status in ORGANIZER_CANCELLATION_TERMINAL_STATUSES:
                    return current_status, latest
            else:
                break
        return "conflict", latest

    def _cancel_task_for_cancelled_job(self, task: dict[str, Any], reason: str) -> bool:
        outcome, _latest = self._cancel_organizer_task(task, reason)
        return outcome == "cancelled"

    def cancel_job_tasks(self, job_id: int, *, reason: str = "关联入库任务已取消") -> dict[str, Any]:
        """Cancel timers and atomically invalidate every non-terminal Organizer task for one job."""

        normalized_job_id = _safe_positive_int(job_id)
        if not normalized_job_id:
            return {"success": False, "message": "缺少有效任务 ID", "task_ids": []}
        tasks = self.db.list_organizer_tasks_by_job(normalized_job_id, limit=1000)
        task_ids: list[int] = []
        cancelled_ids: list[int] = []
        conflicts: list[int] = []
        for task in tasks:
            task_id = _safe_positive_int(task.get("id"))
            if not task_id:
                continue
            task_ids.append(task_id)
            timer = self._timers.pop(task_id, None)
            if timer:
                try:
                    timer.cancel()
                except Exception:  # noqa: BLE001
                    pass
            status = str(task.get("status") or "").strip().lower()
            if status in ORGANIZER_CANCELLATION_TERMINAL_STATUSES:
                continue
            outcome, _latest = self._cancel_organizer_task(task, reason)
            if outcome == "cancelled":
                cancelled_ids.append(task_id)
            elif outcome not in ORGANIZER_CANCELLATION_TERMINAL_STATUSES:
                conflicts.append(task_id)
        return {
            "success": not conflicts,
            "job_id": normalized_job_id,
            "task_ids": task_ids,
            "cancelled_task_ids": cancelled_ids,
            "conflict_task_ids": conflicts,
            "message": (
                f"已取消 {len(cancelled_ids)} 个 Organizer 任务"
                if not conflicts
                else f"已取消 {len(cancelled_ids)} 个 Organizer 任务，{len(conflicts)} 个任务状态已变化"
            ),
        }

    def _mark_linked_job_waiting(self, task_id: int) -> None:
        task = self.db.get_organizer_task(task_id, include_children=False) or {}
        self._sync_linked_job(
            task,
            status=JOB_WAITING_ORGANIZER,
            stage="waiting_organizer",
            message="已创建 OpenList 标准化任务，等待 Organizer 扫描整理",
        )

    def test_openlist(self) -> dict[str, Any]:
        return self.openlist.test()

    def test_tmdb(self) -> dict[str, Any]:
        return self.tmdb.test()

    def test_ai(self, config_override: dict[str, Any] | None = None) -> dict[str, Any]:
        if config_override:
            merged = dict(self.config.get("ai", {}))
            merged.update(config_override)
            return AiCalibrator(merged).test()
        return self.ai.test()

    def list_tasks(self, *, limit: int = 100, status: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        return self.db.list_organizer_tasks(limit=limit, status=status, offset=offset)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return self.db.get_organizer_task(task_id)

    def list_runs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self.db.list_organizer_runs(limit=limit, offset=offset)

    def create_manual_task(self, payload: dict[str, Any], *, defer_process: bool = False) -> dict[str, Any]:
        if not self.openlist.configured:
            return {"success": False, "message": "OpenList 未配置，无法扫描目录"}
        category_key = str(payload.get("category") or "movie").strip()
        category = self.categories.get(category_key, {})
        root_path = normalize_path(payload.get("openlist_root_path") or payload.get("path") or category_target_root(category))
        task_id = self.db.create_organizer_task(
            category=category_key,
            category_label=str(category.get("label") or CATEGORY_LABELS.get(category_key, category_key)),
            title=str(payload.get("title") or "").strip(),
            source_keyword=str(payload.get("source_keyword") or payload.get("keyword") or "").strip(),
            openlist_root_path=root_path,
            trigger_type="admin_manual",
            status="pending",
            evidence={"manual_payload": payload},
            raw_data=payload,
        )
        if defer_process:
            return {
                "success": True,
                "queued": True,
                "task_id": task_id,
                "status": "pending",
                "message": "已创建 OpenList 标准化任务，等待后台生成整理计划",
            }
        return self.process_task(task_id, auto_apply=bool(payload.get("auto_apply", False)))

    def enqueue_from_rclone_callback(
        self,
        *,
        run_id: int | None,
        job: dict[str, Any] | None,
        category_label: str,
        filename: str,
        source_path: str,
        target_path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"success": True, "skipped": True, "message": "Organizer 未启用"}
        if not self.openlist.configured:
            return {"success": False, "skipped": True, "message": "OpenList 未配置，无法创建标准化任务"}
        category_key = str((job or {}).get("category") or "").strip() or self._category_from_label_or_path(category_label, target_path)
        category = self.categories.get(category_key, {})
        root_path = self._root_path_from_target(category_key, target_path)
        if not root_path:
            return {"success": False, "skipped": True, "message": "缺少 target_path，无法定位 OpenList 目录"}
        job_id = int((job or {}).get("id") or 0) or None
        same_job_task = self._existing_task_for_job(job_id)
        if same_job_task:
            return self._existing_task_reuse_result(
                same_job_task,
                job=job,
                requested_root=root_path,
            )
        existing = self.db.find_recent_organizer_task(root_path, category_key)
        # 关联 Job 必须拥有自己的 Organizer task。复用同根目录下另一个 Job
        # 的任务会导致后者完成时只同步原 Job，当前 Job 永久停在 waiting。
        # 真正执行阶段已有 root/category 锁负责串行化，因此这里只保留无 Job
        # 的旧手工/回调任务去重语义。
        if existing and not job_id and not self._allows_same_root_task(payload, job):
            return {"success": True, "queued": True, "task_id": existing["id"], "message": "已存在待处理标准化任务"}
        guest_requests = self.db.list_guest_requests_by_job(job_id) if job_id else []
        request_id = int(guest_requests[0]["id"]) if guest_requests else None
        title = str((job or {}).get("title") or payload.get("title") or filename or "").strip()
        task_id, created = self._get_or_create_organizer_task(
            category=category_key,
            category_label=str(category.get("label") or category_label or CATEGORY_LABELS.get(category_key, category_key)),
            title=title,
            source_keyword=title,
            openlist_root_path=root_path,
            trigger_type="rclone_callback",
            job_id=job_id,
            request_id=request_id,
            rclone_run_id=run_id,
            status="stabilizing",
            evidence={
                "filename": filename,
                "source_path": source_path,
                "target_path": target_path,
                "job": {"id": (job or {}).get("id"), "title": (job or {}).get("title"), "source_type": (job or {}).get("source_type")},
                "naming_plan": payload.get("naming_plan"),
            },
            raw_data=payload,
        )
        if not created:
            same_job_task = self._existing_task_for_job(job_id) or {}
            return self._existing_task_reuse_result(
                {"id": task_id, **same_job_task},
                job=job,
                requested_root=root_path,
            )
        self._mark_linked_job_waiting(task_id)
        self._schedule(task_id)
        return {"success": True, "queued": True, "task_id": task_id, "root_path": root_path, "message": "已创建 OpenList 标准化任务"}

    def enqueue_from_completed_directory(
        self,
        *,
        job: dict[str, Any] | None,
        category_label: str = "",
        root_path: str,
        payload: dict[str, Any] | None = None,
        trigger_type: str = "direct_import_done",
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"success": True, "skipped": True, "message": "Organizer 未启用"}
        if not self.openlist.configured:
            return {"success": False, "skipped": True, "message": "OpenList 未配置，无法创建标准化任务"}
        payload = payload if isinstance(payload, dict) else {}
        category_key = str((job or {}).get("category") or "").strip() or self._category_from_label_or_path(category_label, root_path)
        category = self.categories.get(category_key, {})
        normalized_root = normalize_path(root_path)
        job_id = int((job or {}).get("id") or 0) or None
        if job_id:
            get_job = getattr(self.db, "get_job", None)
            latest_job = (get_job(job_id) if callable(get_job) else None) or job or {}
            if str(latest_job.get("status") or "").strip().lower() == JOB_CANCELLED:
                return {
                    "success": False,
                    "skipped": True,
                    "cancelled": True,
                    "message": "关联入库任务已取消，不创建 Organizer 任务",
                }
            job = latest_job
        same_job_task = self._existing_task_for_job(job_id)
        if same_job_task:
            return self._existing_task_reuse_result(
                same_job_task,
                job=job,
                requested_root=normalized_root,
            )
        existing = self.db.find_recent_organizer_task(normalized_root, category_key)
        if existing and not job_id and not self._allows_same_root_task(payload, job):
            return {"success": True, "queued": True, "task_id": existing["id"], "message": "已存在待处理标准化任务"}
        guest_requests = self.db.list_guest_requests_by_job(job_id) if job_id else []
        request_id = int(guest_requests[0]["id"]) if guest_requests else None
        title = _title_from_update_payload(payload, job or {}, normalized_root)
        task_id, created = self._get_or_create_organizer_task(
            category=category_key,
            category_label=str(category.get("label") or category_label or CATEGORY_LABELS.get(category_key, category_key)),
            title=title,
            source_keyword=title,
            openlist_root_path=normalized_root,
            trigger_type=trigger_type,
            job_id=job_id,
            request_id=request_id,
            status="stabilizing",
            evidence={"root_path": normalized_root, "job": {"id": job_id, "title": title, "source_type": (job or {}).get("source_type")}},
            raw_data=payload,
        )
        if not created:
            same_job_task = self._existing_task_for_job(job_id) or {}
            return self._existing_task_reuse_result(
                {"id": task_id, **same_job_task},
                job=job,
                requested_root=normalized_root,
            )
        if job_id:
            get_job = getattr(self.db, "get_job", None)
            latest_job = (get_job(job_id) if callable(get_job) else None) or job or {}
            if str(latest_job.get("status") or "").strip().lower() == JOB_CANCELLED:
                cancelled_task = self.db.get_organizer_task(task_id, include_children=False) or {
                    "id": task_id,
                    "job_id": job_id,
                    "status": "stabilizing",
                    "revision": 1,
                }
                self._cancel_task_for_cancelled_job(
                    cancelled_task,
                    "关联入库任务已取消",
                )
                return {
                    "success": False,
                    "skipped": True,
                    "cancelled": True,
                    "task_id": task_id,
                    "message": "关联入库任务已取消，已撤销新建 Organizer 任务",
                }
        self._mark_linked_job_waiting(task_id)
        if not self._schedule_initial_openlist_visibility_wait(task_id):
            self._schedule(task_id)
        return {"success": True, "queued": True, "task_id": task_id, "root_path": normalized_root, "message": "已创建 OpenList 标准化任务"}

    def _get_or_create_organizer_task(self, *, job_id: int | None, **task_values: Any) -> tuple[int, bool]:
        atomic_creator = getattr(self.db, "get_or_create_organizer_task_for_job", None)
        if job_id and callable(atomic_creator):
            return atomic_creator(job_id=int(job_id), **task_values)
        return self.db.create_organizer_task(job_id=job_id, **task_values), True

    def _existing_task_for_job(self, job_id: int | None) -> dict[str, Any] | None:
        if not job_id:
            return None
        try:
            rows = self.db.list_organizer_tasks_by_job(int(job_id), limit=1)
        except Exception:  # noqa: BLE001
            return None
        return rows[0] if rows else None

    def _existing_task_reuse_result(
        self,
        task: dict[str, Any],
        *,
        job: dict[str, Any] | None,
        requested_root: str,
    ) -> dict[str, Any]:
        mismatch = self._staging_task_reuse_mismatch(task, job=job, requested_root=requested_root)
        task_id = int(task.get("id") or 0)
        if mismatch:
            try:
                updated = self._update_organizer_task_from_snapshot(
                    task,
                    status="waiting_review",
                    error_message=mismatch,
                )
                if not updated:
                    return {
                        "success": False,
                        "queued": False,
                        "task_id": task_id,
                        "status": str((self.db.get_organizer_task(task_id, include_children=False) or task).get("status") or ""),
                        "message": "Organizer 任务状态已变化，未用复用校验结果覆盖当前状态",
                    }
            except Exception:  # noqa: BLE001
                logger.debug("mark inconsistent organizer task failed task_id=%s", task_id, exc_info=True)
            try:
                self._sync_linked_job(
                    task,
                    status=JOB_REVIEW,
                    stage="review",
                    message=mismatch,
                    level=EVENT_ERROR,
                    error_message=mismatch,
                )
            except Exception:  # noqa: BLE001
                logger.debug("sync inconsistent organizer task job failed task_id=%s", task_id, exc_info=True)
            return {
                "success": False,
                "queued": False,
                "task_id": task_id,
                "status": "waiting_review",
                "message": mismatch,
            }
        return {
            "success": True,
            "queued": True,
            "task_id": task_id,
            "status": task.get("status") or "",
            "message": "该入库任务已存在标准化任务",
        }

    @staticmethod
    def _staging_task_reuse_mismatch(
        task: dict[str, Any],
        *,
        job: dict[str, Any] | None,
        requested_root: str,
    ) -> str:
        job = job if isinstance(job, dict) else {}
        try:
            current_plan = validated_staging_plan_from_job(job)
        except (TypeError, ValueError) as exc:
            return f"当前入库任务的 staging_plan 校验失败，拒绝复用既有 Organizer 任务：{exc}"
        if not current_plan:
            return ""

        task_raw = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        task_plan = task_raw.get("staging_plan") if isinstance(task_raw.get("staging_plan"), dict) else {}
        validation_job = {
            "id": job.get("id"),
            "category": job.get("category"),
            "target_route": job.get("target_route"),
            "raw_data": {"staging_plan": task_plan},
        }
        try:
            validated_task_plan = validated_staging_plan_from_job(validation_job)
        except (TypeError, ValueError):
            validated_task_plan = {}
        if not validated_task_plan:
            return "既有 Organizer 任务缺少与当前入库任务一致的有效 staging_plan，已拒绝静默复用"

        expected_root = normalize_path(requested_root)
        existing_root = normalize_path(task.get("openlist_root_path") or "")
        if existing_root.casefold() != expected_root.casefold():
            return f"既有 Organizer 任务扫描目录与当前暂存任务不一致：existing={existing_root}，expected={expected_root}"

        if _staging_plan_identity(validated_task_plan) != _staging_plan_identity(current_plan):
            return "既有 Organizer 任务的 staging_plan 与当前入库任务不一致，已拒绝静默复用"
        return ""

    @staticmethod
    def _allows_same_root_task(payload: dict[str, Any], job: dict[str, Any] | None = None) -> bool:
        if _flag_enabled(payload.get("allow_same_root_task")):
            return True
        contexts: list[dict[str, Any]] = [payload]
        job_raw = (job or {}).get("raw_data") if isinstance((job or {}).get("raw_data"), dict) else {}
        request_payload = job_raw.get("request") if isinstance(job_raw.get("request"), dict) else {}
        contexts.extend([job_raw, request_payload])
        for container in contexts:
            for key in ("update_context", "organizer_context"):
                context = container.get(key) if isinstance(container.get(key), dict) else {}
                if context.get("subscription_id") or context.get("canonical_resource_root") or context.get("canonical_openlist_root"):
                    return True
        return False

    def _schedule(self, task_id: int) -> None:
        delay = max(0, int(self.organizer_config.get("stable_window_seconds") or 0))
        self._schedule_task_after(task_id, delay)

    def _schedule_task_after(self, task_id: int, delay: int | float) -> bool:
        if self._background_suspended:
            return False
        old_timer = self._timers.pop(task_id, None)
        if old_timer:
            try:
                old_timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        timer = threading.Timer(delay, self._process_task_safely, args=(task_id,))
        timer.daemon = True
        self._timers[task_id] = timer
        timer.start()
        return True

    def suspend_background(self) -> None:
        self._background_suspended = True
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass

    def activate_background_recovery(
        self,
        *,
        include_scanning: bool = False,
        recover_stale_runs: bool = False,
    ) -> None:
        self._background_suspended = False
        if recover_stale_runs:
            self._recover_stale_runs_on_startup()
        self._recover_completed_linked_jobs_on_startup()
        self._recover_transient_scan_tasks_on_startup(include_scanning=include_scanning)

    def shutdown(self) -> None:
        self.suspend_background()

    def _schedule_initial_openlist_visibility_wait(self, task_id: int) -> bool:
        task = self.db.get_organizer_task(task_id, include_children=False) or {}
        if not self._task_waits_for_openlist_visibility(task):
            return False
        delays = self._openlist_visibility_retry_delays()
        first_delay = delays[0] if delays else 120
        next_retry_at = _utc_after_seconds(first_delay)
        source_message = self._openlist_visibility_source_message(task)
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        raw_data = dict(raw_data)
        retry_state = {
            "attempts": 1,
            "max_attempts": len(delays),
            "delays_seconds": delays,
            "created_at": _utc_now_text(),
            "next_retry_at": next_retry_at,
            "root_path": normalize_path(task.get("openlist_root_path") or ""),
            "message": f"{source_message}，等待 OpenList 可见；系统将在 {max(1, int(first_delay / 60))} 分钟后自动检查（1/{len(delays)}）",
        }
        raw_data["openlist_visibility_retry"] = retry_state
        if not self._update_organizer_task_from_snapshot(
            task,
            status="waiting_openlist",
            error_message="",
            raw_data=raw_data,
        ):
            return True
        self._sync_linked_job(
            {**task, "raw_data": raw_data},
            status=JOB_WAITING_OPENLIST,
            stage="waiting_openlist",
            message=retry_state["message"],
            extra={"openlist_visibility_retry": retry_state},
        )
        self._schedule_task_after(task_id, first_delay)
        return True

    def _process_task_safely(self, task_id: int) -> None:
        task = self.db.get_organizer_task(task_id, include_children=False) or {}
        abort_reason = self._task_abort_reason(task)
        if abort_reason:
            if "关联入库任务已取消" in abort_reason:
                self._cancel_task_for_cancelled_job(task, abort_reason)
            return
        if str(task.get("status") or "") not in {"pending", "stabilizing", "waiting_openlist", "strm_pending"}:
            # 延时 Timer 在运行时配置重载后可能存在旧实例回调。任务已失败、
            # 已完成或正在扫描时直接忽略过期回调，避免同一错误被重复写入。
            return
        try:
            self.process_task(task_id, auto_apply=True)
        except Exception as exc:  # noqa: BLE001
            latest = self.db.get_organizer_task(task_id, include_children=False) or task
            self._update_organizer_task_from_snapshot(
                latest,
                status="failed",
                error_message=str(exc),
            )

    def _openlist_visibility_retry_delays(self) -> list[int]:
        configured = self.organizer_config.get("cloud139_visible_retry_delays_seconds") or self.organizer_config.get("openlist_visible_retry_delays_seconds")
        if isinstance(configured, str):
            values = [item.strip() for item in configured.replace("|", ",").split(",")]
        elif isinstance(configured, (list, tuple)):
            values = list(configured)
        else:
            values = [120, 300, 600]
        delays: list[int] = []
        for value in values:
            try:
                number = int(float(str(value).strip()))
            except (TypeError, ValueError):
                continue
            if number > 0:
                delays.append(min(number, 3600))
        return delays or [120, 300, 600]

    def _task_waits_for_openlist_visibility(self, task: dict[str, Any]) -> bool:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        evidence = task.get("evidence") if isinstance(task.get("evidence"), dict) else {}
        if raw_data.get("cloud139_openlist"):
            return True
        staging_plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        if staging_plan.get("enabled") and isinstance(raw_data.get("sixpan_openlist"), dict):
            return True
        job = raw_data.get("job") if isinstance(raw_data.get("job"), dict) else {}
        evidence_job = evidence.get("job") if isinstance(evidence.get("job"), dict) else {}
        source_type = str(job.get("source_type") or evidence_job.get("source_type") or "").strip().lower()
        job_raw = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        provider = str(job_raw.get("provider") or raw_data.get("provider") or "").strip().lower()
        return source_type == "cloud139" or provider == "cmcc_native"

    def _task_has_staging_plan(self, task: dict[str, Any]) -> bool:
        return bool(self._validated_task_staging_plan(task))

    def _validated_task_staging_plan(self, task: dict[str, Any]) -> dict[str, Any]:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        if not plan.get("enabled"):
            return {}
        job_id = _safe_positive_int(task.get("job_id") or plan.get("job_id"))
        if not job_id:
            return {}
        database = getattr(self, "db", None)
        getter = getattr(database, "get_job", None)
        linked_job = getter(job_id) if callable(getter) else None
        if callable(getter) and not isinstance(linked_job, dict):
            return {}
        validation_job = dict(linked_job or {})
        validation_job.update(
            {
                "id": job_id,
                "category": validation_job.get("category") or task.get("category") or plan.get("category"),
                "target_route": validation_job.get("target_route") or plan.get("route"),
                "raw_data": {"staging_plan": plan},
            }
        )
        try:
            return validated_staging_plan_from_job(validation_job)
        except (TypeError, ValueError):
            return {}

    def _validate_staging_task_boundaries(
        self,
        task: dict[str, Any],
        *,
        scan_root: str,
        target_category: dict[str, Any],
    ) -> None:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        raw_plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        plan = self._validated_task_staging_plan(task)
        if raw_plan.get("enabled") and not plan:
            raise OpenListError("任务携带的 staging_plan 已启用但校验失败，已拒绝按普通目录任务降级扫描")
        if not plan:
            return
        job_root = normalize_path(plan.get("openlist_job_root") or "")
        normalized_scan_root = normalize_path(scan_root)
        if not job_root or not _path_is_same_or_child(normalized_scan_root, job_root):
            raise OpenListError(
                f"任务级暂存扫描目录越界：scan={normalized_scan_root}，job_root={job_root or '<empty>'}"
            )
        final_category_root = normalize_path(plan.get("openlist_final_category_root") or "")
        target_root = normalize_path(category_target_root(target_category))
        resource_root = normalize_path(
            target_category.get("resource_root_path")
            or target_category.get("canonical_resource_root")
            or target_root
        )
        for label, value in (("target_root", target_root), ("resource_root", resource_root)):
            if not final_category_root or not _path_is_same_or_child(value, final_category_root):
                raise OpenListError(
                    f"任务级暂存最终目录越界：{label}={value}，final_root={final_category_root or '<empty>'}"
                )

    def _validate_staging_mapping_boundaries(self, task: dict[str, Any]) -> None:
        """Revalidate persisted staging mappings immediately before apply.

        Mapping paths are editable from the admin page after the scan plan is
        generated.  The task-level scan/target-root checks therefore cannot be
        the final trust boundary: every executable mapping must still stay
        inside the immutable per-job staging root and final category root.
        """

        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        raw_plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        plan = self._validated_task_staging_plan(task)
        if raw_plan.get("enabled") and not plan:
            raise OpenListError("任务携带的 staging_plan 已启用但校验失败，已拒绝执行整理")
        if not plan:
            return

        job_root = normalize_path(plan.get("openlist_job_root") or "")
        final_category_root = normalize_path(plan.get("openlist_final_category_root") or "")
        for mapping in task.get("mappings") or []:
            if not isinstance(mapping, dict):
                continue
            status = str(mapping.get("status") or "")
            if status not in {"ready", "skipped_existing"}:
                continue
            source_path = normalize_path(mapping.get("source_path") or "")
            target_path = normalize_path(mapping.get("target_path") or "")
            if not job_root or not _is_child_path(source_path, job_root):
                raise OpenListError(
                    "任务级暂存映射源路径越界："
                    f"mapping_id={mapping.get('id') or '<unknown>'}，source={source_path}，"
                    f"job_root={job_root or '<empty>'}"
                )
            if not final_category_root or not _is_child_path(target_path, final_category_root):
                raise OpenListError(
                    "任务级暂存映射目标路径越界："
                    f"mapping_id={mapping.get('id') or '<unknown>'}，target={target_path}，"
                    f"final_root={final_category_root or '<empty>'}"
                )

    @staticmethod
    def _openlist_visibility_source_message(task: dict[str, Any]) -> str:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        staging_plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        if staging_plan.get("enabled") and isinstance(raw_data.get("sixpan_openlist"), dict):
            return "六盘离线任务已完成"
        if raw_data.get("cloud139_openlist"):
            return "139 已提交成功"
        return "网盘任务已完成"

    def _task_retries_openlist_visibility(self, task: dict[str, Any]) -> bool:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        trigger_type = str(task.get("trigger_type") or "").strip().lower()
        return (
            self._task_waits_for_openlist_visibility(task)
            or trigger_type in {"rclone_category_done", "rclone_callback"}
            or isinstance(raw_data.get("rclone"), dict)
        )

    @staticmethod
    def _retryable_openlist_visibility_error(exc: Exception) -> bool:
        if isinstance(exc, OpenListTransientError):
            return True
        message = str(exc or "").strip().lower()
        markers = (
            "object not found",
            "failed get dir",
            "busy",
            "locked",
            "too many requests",
            "too many request",
            "rate limit",
            "try again later",
            "retry later",
            "please wait",
            "operation in progress",
            "task is running",
            "scan is running",
            "previous task",
            "another task",
            "connection reset",
            "connection aborted",
            "remote disconnected",
            "remote end closed",
            "broken pipe",
            "request timeout",
            "read timed out",
            "connect timeout",
            "context deadline exceeded",
            "context canceled",
            "temporarily unavailable",
            "openlist http 408",
            "openlist http 423",
            "openlist http 425",
            "openlist http 429",
            "openlist http 502",
            "openlist http 503",
            "openlist http 504",
            "OpenList 连接暂时中断".lower(),
            "繁忙",
            "忙碌",
            "请稍后",
            "稍后重试",
            "请求过多",
            "频率过高",
            "限流",
            "被锁定",
            "已锁定",
            "任务进行中",
            "任务正在运行",
            "已有任务",
            "上一个任务",
            "临时不可用",
            "连接被对端重置",
            "对端提前关闭连接",
            "请求超时",
        )
        return any(marker in message for marker in markers)

    def _schedule_openlist_visibility_retry(
        self,
        task: dict[str, Any],
        *,
        root_path: str,
        error: Exception | None = None,
        expected_revision: int | None = None,
        scan_owner: str = "",
    ) -> dict[str, Any]:
        if not self._task_retries_openlist_visibility(task):
            return {}
        task_id = _safe_positive_int(task.get("id"))
        if not task_id:
            return {}
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        raw_data = dict(raw_data)
        state = raw_data.get("openlist_visibility_retry") if isinstance(raw_data.get("openlist_visibility_retry"), dict) else {}
        delays = self._openlist_visibility_retry_delays()
        attempts = int(state.get("attempts") or 0)
        if attempts >= len(delays):
            raw_data["openlist_visibility_retry"] = {
                **state,
                "attempts": attempts,
                "max_attempts": len(delays),
                "exhausted": True,
                "last_check_at": _utc_now_text(),
                "message": "OpenList 可见性等待已超过自动重试次数",
            }
            if not self._update_claimed_scan_task(
                task_id,
                expected_revision,
                clear_scan_lease=False,
                raw_data=raw_data,
            ):
                raise OrganizerScanLeaseLost("Organizer 扫描状态已变化，拒绝更新重试计划")
            return {}
        delay = delays[attempts]
        next_retry_at = _utc_after_seconds(delay)
        next_attempt = attempts + 1
        if error is not None:
            detail = _stringify_message(error)
            message = (
                f"OpenList 扫描暂时失败（{detail}）；系统将在 "
                f"{max(1, int(delay / 60))} 分钟后自动重试（{next_attempt}/{len(delays)}）"
            )
        else:
            source_message = self._openlist_visibility_source_message(task)
            message = f"{source_message}，但 OpenList 暂未看到新文件；系统将在 {max(1, int(delay / 60))} 分钟后自动复查（{next_attempt}/{len(delays)}）"
        retry_state = {
            **state,
            "attempts": next_attempt,
            "max_attempts": len(delays),
            "delays_seconds": delays,
            "last_check_at": _utc_now_text(),
            "next_retry_at": next_retry_at,
            "root_path": normalize_path(root_path),
            "message": message,
        }
        if error is not None:
            retry_state["last_error"] = _stringify_message(error)
        raw_data["openlist_visibility_retry"] = retry_state
        if expected_revision is not None and scan_owner:
            current = self._ensure_task_active(
                task_id,
                allowed_statuses={"scanning"},
                expected_revision=expected_revision,
                scan_owner=scan_owner,
            )
            del current
        if not self._update_claimed_scan_task(
            task_id,
            expected_revision,
            status="waiting_openlist",
            error_message="",
            raw_data=raw_data,
        ):
            raise OrganizerScanLeaseLost("Organizer 扫描状态已变化，拒绝安排旧重试")
        worker_context = getattr(self, "_worker_context", None)
        durable_worker = bool(getattr(worker_context, "active", False))
        if durable_worker:
            return {
                "success": True,
                "queued": True,
                "deferred": True,
                "retry_after_seconds": delay,
                "waiting_openlist": True,
                "task_id": task_id,
                "status": "waiting_openlist",
                "next_retry_at": next_retry_at,
                "message": message,
            }
        if self._schedule_task_after(task_id, delay) is False:
            failure_message = "OpenList 暂时繁忙，但当前进程无法安排后台重试；请稍后手动重试"
            retry_state = {
                **retry_state,
                "schedule_failed": True,
                "message": failure_message,
            }
            raw_data["openlist_visibility_retry"] = retry_state
            update_values = {
                "status": "failed",
                "error_message": failure_message,
                "raw_data": raw_data,
            }
            try:
                updated = self.db.update_organizer_task(
                    task_id,
                    expected_statuses={"waiting_openlist"},
                    expected_revision=expected_revision,
                    **update_values,
                )
            except TypeError:
                updated = self.db.update_organizer_task(task_id, **update_values)
            if updated is False:
                raise OrganizerScanLeaseLost("Organizer 任务状态已变化，拒绝覆盖后台调度结果")
            self._sync_linked_job(
                {**task, "raw_data": raw_data},
                status=JOB_REVIEW,
                stage="review",
                message=failure_message,
                level=EVENT_WARN,
                error_message=failure_message,
                extra={"openlist_visibility_retry": retry_state},
            )
            return {
                "success": False,
                "queued": False,
                "waiting_openlist": False,
                "task_id": task_id,
                "status": "failed",
                "message": failure_message,
            }
        self._sync_linked_job(
            {**task, "raw_data": raw_data},
            status=JOB_WAITING_OPENLIST,
            stage="waiting_openlist",
            message=message,
            level=EVENT_INFO,
            extra={"openlist_visibility_retry": retry_state},
        )
        return {
            "success": True,
            "queued": True,
            "waiting_openlist": True,
            "task_id": task_id,
            "status": "waiting_openlist",
            "next_retry_at": next_retry_at,
            "message": message,
        }

    def process_task_from_worker(
        self,
        task_id: int,
        *,
        auto_apply: bool = True,
        respect_schedule: bool = True,
    ) -> dict[str, Any]:
        """Honor persisted visibility/stability deadlines before Worker execution."""

        if not respect_schedule:
            worker_context = getattr(self, "_worker_context", None)
            if worker_context is None:
                worker_context = threading.local()
                self._worker_context = worker_context
            worker_context.active = True
            try:
                return self.process_task(task_id, auto_apply=auto_apply)
            finally:
                worker_context.active = False
        task = self.db.get_organizer_task(task_id, include_children=False)
        if not task:
            return {"success": False, "message": "标准化任务不存在"}
        status = str(task.get("status") or "").strip().lower()
        delay, deadline = self._persisted_worker_delay(task, status=status)
        if delay <= 0:
            worker_context = getattr(self, "_worker_context", None)
            if worker_context is None:
                worker_context = threading.local()
                self._worker_context = worker_context
            worker_context.active = True
            try:
                return self.process_task(task_id, auto_apply=auto_apply)
            finally:
                worker_context.active = False
        # Compatibility for embedders that call the worker helper without a
        # DurableWorkerRuntime.  A normal OrganizerService always owns
        # ``_worker_context`` and lets the durable queue persist the delay.
        if not hasattr(self, "_worker_context"):
            self._schedule_task_after(task_id, delay)
        return {
            "success": True,
            "queued": True,
            "deferred": True,
            "retry_after_seconds": delay,
            "delay_seconds": delay,
            "task_id": task_id,
            "status": status,
            "next_retry_at": deadline,
            "message": f"Organizer 已按持久化等待计划延迟 {delay} 秒扫描",
        }

    def _persisted_worker_delay(self, task: dict[str, Any], *, status: str) -> tuple[int, str]:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        if status == "waiting_openlist":
            retry_state = (
                raw_data.get("openlist_visibility_retry")
                if isinstance(raw_data.get("openlist_visibility_retry"), dict)
                else {}
            )
            deadline = str(retry_state.get("next_retry_at") or "").strip()
            if deadline:
                return _remaining_retry_delay_seconds(deadline, fallback=0), deadline
            return 0, ""
        if status == "strm_pending":
            state = raw_data.get("strm_completion") if isinstance(raw_data.get("strm_completion"), dict) else {}
            deadline = str(state.get("next_retry_at") or "").strip()
            if deadline:
                return _remaining_retry_delay_seconds(deadline, fallback=0), deadline
            return 0, ""
        if status not in {"stabilizing", "pending"}:
            return 0, ""
        try:
            stable_window = max(0, int(self.organizer_config.get("stable_window_seconds") or 0))
        except (TypeError, ValueError):
            stable_window = 0
        if stable_window <= 0:
            return 0, ""
        created_at = str(task.get("created_at") or "").strip()
        delay = _remaining_window_delay_seconds(created_at, stable_window)
        return delay, _timestamp_after_seconds(created_at, stable_window)

    def _organizer_scan_lease_seconds(self) -> int:
        config = getattr(self, "organizer_config", {})
        try:
            value = int(config.get("scan_lease_seconds") or 180) if isinstance(config, dict) else 180
        except (TypeError, ValueError):
            value = 180
        return max(30, value)

    def _organizer_scan_heartbeat_interval(self) -> float | None:
        config = getattr(self, "organizer_config", {})
        value = config.get("scan_lease_heartbeat_seconds") if isinstance(config, dict) else None
        if value in {None, ""}:
            return None
        try:
            return max(0.05, float(value))
        except (TypeError, ValueError):
            return None

    def _update_claimed_scan_task(
        self,
        task_id: int,
        scan_revision: int | None,
        *,
        clear_scan_lease: bool = True,
        **updates: Any,
    ) -> bool:
        if scan_revision is None:
            result = self.db.update_organizer_task(task_id, **updates)
            return result is not False
        guarded = {
            **updates,
            "expected_statuses": {"scanning"},
            "expected_revision": scan_revision,
            "clear_scan_lease": clear_scan_lease,
        }
        try:
            result = self.db.update_organizer_task(task_id, **guarded)
        except TypeError:
            # Lightweight integrations predating scan revisions remain usable;
            # the production Database always enforces the guarded branch.
            result = self.db.update_organizer_task(task_id, **updates)
        return result is not False

    def process_task(self, task_id: int, *, auto_apply: bool = True) -> dict[str, Any]:
        task = self.db.get_organizer_task(task_id, include_children=False)
        if not task:
            return {"success": False, "message": "标准化任务不存在"}
        abort_reason = self._task_abort_reason(task)
        if abort_reason:
            if "关联入库任务已取消" in abort_reason:
                self._cancel_task_for_cancelled_job(task, abort_reason)
            return {
                "success": False,
                "skipped": True,
                "cancelled": True,
                "task_id": task_id,
                "status": str(task.get("status") or "cancelled"),
                "message": abort_reason,
            }
        if str(task.get("status") or "") == "done":
            return {"success": True, "task_id": task_id, "status": "done", "message": "标准化任务已完成，无需重复扫描"}
        if str(task.get("status") or "") == "strm_pending":
            if auto_apply:
                return self.apply_task(task_id)
            return {
                "success": True,
                "deferred": True,
                "task_id": task_id,
                "status": "strm_pending",
                "message": "历史任务已完成真实媒体整理，下一次执行将按新规则直接收尾，不再等待 STRM",
            }
        claimable_statuses = {
            "pending",
            "stabilizing",
            "waiting_openlist",
            "failed",
            "waiting_review",
            "auto_approved",
            "manual_confirmed",
        }
        claimer = getattr(self.db, "claim_organizer_task_for_scan", None)
        scan_revision: int | None = None
        scan_lease: OrganizerScanLease | None = None
        if callable(claimer):
            try:
                claimed = claimer(
                    task_id,
                    allowed_statuses=claimable_statuses,
                    owner_id=getattr(self, "owner_id", "organizer"),
                    lease_seconds=self._organizer_scan_lease_seconds(),
                )
            except TypeError:
                claimed = claimer(task_id, allowed_statuses=claimable_statuses)
            if not claimed:
                current = self.db.get_organizer_task(task_id, include_children=False) or task
                status = str(current.get("status") or "")
                return {
                    "success": True,
                    "skipped": True,
                    "task_id": task_id,
                    "status": status,
                    "message": "标准化任务已由另一执行器接管，本次跳过重复扫描",
                }
            task = self.db.get_organizer_task(task_id, include_children=False) or {**task, "status": "scanning"}
            scan_revision = _safe_positive_int(task.get("revision")) or 1
            scan_lease = OrganizerScanLease(
                database=self.db,
                task_id=task_id,
                owner_id=getattr(self, "owner_id", "organizer"),
                revision=scan_revision,
                lease_seconds=self._organizer_scan_lease_seconds(),
                heartbeat_interval_seconds=self._organizer_scan_heartbeat_interval(),
                log=lambda message: logger.warning("%s", message),
            )
        else:
            if not self._update_organizer_task_from_snapshot(
                task,
                status="scanning",
                error_message="",
            ):
                current = self.db.get_organizer_task(task_id, include_children=False) or task
                return {
                    "success": True,
                    "skipped": True,
                    "task_id": task_id,
                    "status": str(current.get("status") or ""),
                    "message": "标准化任务状态已变化，本次跳过重复扫描",
                }
            task = {**task, "status": "scanning"}
        try:
            if scan_lease is not None:
                scan_lease.start()
                scan_lease.ensure_owned()
            task = self._ensure_task_active(
                task_id,
                allowed_statuses={"scanning"} if scan_revision is not None else None,
                expected_revision=scan_revision,
                scan_owner=getattr(self, "owner_id", "organizer") if scan_revision is not None else "",
            )
            task = self._repair_task_title_from_linked_job(task)
            if scan_lease is not None:
                scan_lease.ensure_owned()
            self._sync_linked_job(task, status=JOB_ORGANIZING, stage="organizing", message="Organizer 正在扫描 OpenList 目录并生成整理计划")
            category_key = str(task.get("category") or "movie")
            category = self.categories.get(category_key, {})
            root_path = normalize_path(task.get("openlist_root_path"))
            self._validate_staging_task_boundaries(
                task,
                scan_root=root_path,
                target_category=self._target_category_for_task(task, category),
            )
            try:
                scan_semaphore = getattr(self, "_scan_semaphore", None)
                if scan_semaphore is None:
                    videos = self._scan_openlist_videos(task, root_path)
                    companion_files = self._scan_staging_companion_files(task, root_path)
                else:
                    # Organizer 定时任务可能在同一时刻触发多个扫描。串行化默认扫描
                    # 可避免多个递归 refresh 同时冲击 OpenList/底层云盘导致连接重置。
                    with scan_semaphore:
                        videos = self._scan_openlist_videos(task, root_path)
                        companion_files = self._scan_staging_companion_files(task, root_path)
            except Exception as exc:
                if self._retryable_openlist_visibility_error(exc):
                    if scan_lease is not None:
                        scan_lease.ensure_owned()
                    retry_result = self._schedule_openlist_visibility_retry(
                        task,
                        root_path=root_path,
                        error=exc,
                        expected_revision=scan_revision,
                        scan_owner=getattr(self, "owner_id", "organizer") if scan_revision is not None else "",
                    )
                    if retry_result:
                        return retry_result
                raise
            videos = self._filter_videos_for_task(task, videos)
            if scan_lease is not None:
                scan_lease.ensure_owned()
            self._ensure_task_active(
                task_id,
                allowed_statuses={"scanning"} if scan_revision is not None else None,
                expected_revision=scan_revision,
                scan_owner=getattr(self, "owner_id", "organizer") if scan_revision is not None else "",
            )
            incomplete_message = self._scan_completeness_message(task, videos, root_path)
            if not videos or incomplete_message:
                retry_result = self._schedule_openlist_visibility_retry(
                    task,
                    root_path=root_path,
                    expected_revision=scan_revision,
                    scan_owner=getattr(self, "owner_id", "organizer") if scan_revision is not None else "",
                )
                if retry_result:
                    return retry_result
                failure_message = incomplete_message or "OpenList 目录下没有视频文件"
                if not self._update_claimed_scan_task(
                    task_id,
                    scan_revision,
                    status="failed",
                    error_message=failure_message,
                ):
                    raise OrganizerScanLeaseLost("Organizer 扫描状态已变化，拒绝覆盖任务结果")
                self._sync_linked_job(
                    task,
                    status=JOB_REVIEW,
                    stage="review",
                    message=f"{failure_message}，无法确认完整入库",
                    level=EVENT_WARN,
                    error_message=failure_message,
                )
                return {"success": False, "message": failure_message, "task_id": task_id}

            files, mappings, operations, summary = self._build_plan(
                task,
                category_key,
                category,
                videos,
                companion_files=companion_files,
            )
            if scan_lease is not None:
                scan_lease.ensure_owned()
            self._ensure_task_active(
                task_id,
                allowed_statuses={"scanning"} if scan_revision is not None else None,
                expected_revision=scan_revision,
                scan_owner=getattr(self, "owner_id", "organizer") if scan_revision is not None else "",
            )
            try:
                replaced = self.db.replace_organizer_plan(
                    task_id,
                    files=files,
                    mappings=mappings,
                    operations=operations,
                    expected_revision=scan_revision,
                    owner_id=getattr(self, "owner_id", "organizer") if scan_revision is not None else "",
                )
            except TypeError:
                replaced = self.db.replace_organizer_plan(task_id, files=files, mappings=mappings, operations=operations)
            if replaced is False:
                raise OrganizerScanLeaseLost("Organizer 扫描状态已变化，拒绝写入旧计划")
            # 计划生成阶段必须保持只读。广告删除也作为持久化 operation 留到
            # 后续 apply 阶段执行；即使高置信计划会自动衔接 apply，也不会在
            # 扫描或写入计划的过程中直接修改远端文件。
            status = "auto_approved" if summary["auto_applicable"] else "waiting_review"
            if scan_lease is not None:
                scan_lease.ensure_owned()
            self._ensure_task_active(
                task_id,
                allowed_statuses={"scanning"} if scan_revision is not None else None,
                expected_revision=scan_revision,
                scan_owner=getattr(self, "owner_id", "organizer") if scan_revision is not None else "",
            )
            latest_task = self.db.get_organizer_task(task_id, include_children=False) or task
            latest_raw = latest_task.get("raw_data") if isinstance(latest_task.get("raw_data"), dict) else {}
            if "openlist_visibility_retry" in latest_raw:
                latest_raw = dict(latest_raw)
                latest_raw.pop("openlist_visibility_retry", None)
            updated = self._update_claimed_scan_task(
                task_id,
                scan_revision,
                status=status,
                confidence=summary["confidence"],
                media_type=summary["media_type"],
                tmdb_id=summary.get("tmdb_id"),
                tmdb_title=summary.get("tmdb_title") or "",
                tmdb_year=summary.get("tmdb_year") or "",
                evidence=summary.get("evidence"),
                raw_data=latest_raw,
            )
            if not updated:
                raise OrganizerScanLeaseLost("Organizer 扫描状态已变化，拒绝提交旧计划")
            if scan_lease is not None:
                scan_lease.stop()
                scan_lease = None
            if auto_apply and status == "auto_approved":
                return self.apply_task(task_id)
            if status == "waiting_review":
                review_message = _stringify_message(summary.get("problem_summary") or "Organizer 置信度不足或存在冲突，需要人工确认")
                self._sync_linked_job(
                    task,
                    status=JOB_REVIEW,
                    stage="review",
                    message=review_message,
                    level=EVENT_WARN,
                    error_message=review_message,
                )
            return {"success": True, "task_id": task_id, "status": status, "summary": summary}
        except OrganizerScanLeaseLost as exc:
            latest = self.db.get_organizer_task(task_id, include_children=False) or task
            return {
                "success": False,
                "skipped": True,
                "stale": True,
                "retryable": True,
                "task_id": task_id,
                "status": str(latest.get("status") or ""),
                "message": str(exc),
            }
        except OrganizerTaskCancelled as exc:
            latest = self.db.get_organizer_task(task_id, include_children=False) or task
            reason = str(exc)
            if "关联入库任务已取消" in reason:
                self._cancel_task_for_cancelled_job(latest, reason)
            return {
                "success": False,
                "skipped": True,
                "cancelled": True,
                "task_id": task_id,
                "status": str(latest.get("status") or "cancelled"),
                "message": reason,
            }
        except Exception as exc:  # noqa: BLE001
            latest = self.db.get_organizer_task(task_id, include_children=False) or task
            if str(latest.get("status") or "").strip().lower() in {"skipped", "cancelled"}:
                return {"success": False, "skipped": True, "cancelled": True, "task_id": task_id, "message": str(latest.get("error_message") or "任务已停止")}
            message = f"扫描或生成标准化计划失败：root={normalize_path(task.get('openlist_root_path'))}；{exc}"
            updated = self._update_claimed_scan_task(task_id, scan_revision, status="failed", error_message=message)
            if updated:
                self._sync_linked_job(task, status=JOB_REVIEW, stage="review", message=message, level=EVENT_ERROR, error_message=message)
            return {"success": False, "message": message, "task_id": task_id}
        finally:
            if scan_lease is not None:
                scan_lease.stop()

    def _scan_openlist_videos(self, task: dict[str, Any], root_path: str) -> list[Any]:
        filters = self._task_scan_filters(task)
        expected_names = [str(item or "").strip() for item in (filters.get("expected_names") or []) if str(item or "").strip()]
        expected_paths = [str(item or "").strip() for item in (filters.get("expected_paths") or []) if str(item or "").strip()]
        expected_count = _safe_non_negative_int(filters.get("expected_count"))
        max_depth = int(self.organizer_config.get("max_scan_depth") or 8)
        max_files = int(self.organizer_config.get("max_files_per_task") or 500)
        if self._task_has_staging_plan(task):
            # job-<id> 是本任务独占根目录。不能用 expected_names 的集合提前终止，
            # 否则不同季目录中的同名文件会只扫描到第一份；也不能用通用 500 文件
            # 上限截断大任务。若拿不到完整精确路径，就完整扫描整个 Job 根，再按
            # expected_count 做可见性完整性校验。
            normalized_root = normalize_path(root_path)
            scoped_paths = _dedupe_texts(
                normalize_path(item)
                for item in expected_paths
                if _path_is_same_or_child(normalize_path(item), normalized_root)
            )
            # 持久化 expected_count 可能把非 OpenList 命名空间路径（如 rclone 存储的
            # webdav/...）一并计入导致虚高，从而误判"精确路径不齐"而清空清单。
            # 对暂存任务以 root 下的精确路径数为准，缺失时才回退到持久化值。
            if scoped_paths:
                expected_count = len(scoped_paths)
            if expected_count > 0 and len(scoped_paths) < expected_count:
                scoped_paths = []
            expected_paths = scoped_paths
            if not expected_paths:
                expected_names = []
            max_depth = max(max_depth, 64)
            max_files = 0
        if self._is_automatic_category_root_scan(task, root_path):
            if not expected_names and not expected_paths:
                raise OpenListError("本次资源直接落在分类根目录，但缺少本次文件清单；已拒绝递归扫描整个分类目录")
            # 精确路径由 OpenListClient 直接读取；仅有文件名时也只检查分类根当前层，
            # 文件暂不可见就进入退避重试，绝不能继续遍历历史影视目录。
            max_depth = 0
        return self.openlist.scan_videos(
            root_path,
            max_depth=max_depth,
            max_files=max_files,
            refresh=True,
            expected_names=expected_names,
            expected_paths=expected_paths,
        )

    def _scan_staging_companion_files(self, task: dict[str, Any], root_path: str) -> list[Any]:
        if not self._task_has_staging_plan(task):
            return []
        normalized_root = normalize_path(root_path)
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        raw_transport_paths = (
            raw_data.get("transport_target_paths")
            if isinstance(raw_data.get("transport_target_paths"), list)
            else []
        )
        transport_paths = _dedupe_texts(
            normalize_path(item)
            for item in raw_transport_paths
            if str(item or "").strip()
            and _path_is_same_or_child(normalize_path(item), normalized_root)
            and _is_supported_companion_name(basename(normalize_path(item)))
        )
        if transport_paths:
            grouped: dict[str, set[str]] = {}
            for path in transport_paths:
                grouped.setdefault(dirname(path), set()).add(basename(path).casefold())
            found: dict[str, Any] = {}
            for parent, names in grouped.items():
                for item in self.openlist.list_dir(parent, refresh=True):
                    if item.is_dir or item.name.casefold() not in names:
                        continue
                    found[normalize_path(item.path).casefold()] = item
            missing = [path for path in transport_paths if path.casefold() not in found]
            if missing:
                raise OpenListTransientError(
                    f"OpenList 任务目录附件尚未完整可见：缺少 {len(missing)}/{len(transport_paths)} 个文件"
                )
            return [found[path.casefold()] for path in transport_paths]

        result: list[Any] = []
        visited: set[str] = set()
        max_depth = max(8, min(64, int(self.organizer_config.get("max_scan_depth") or 8)))
        max_files = max(1000, min(20000, int(self.organizer_config.get("max_files_per_task") or 500) * 20))

        def walk(path: str, depth: int) -> None:
            if depth > max_depth or len(result) >= max_files:
                return
            normalized = normalize_path(path)
            if normalized in visited:
                return
            visited.add(normalized)
            items = self.openlist.list_dir(normalized, refresh=depth == 0)
            for item in items:
                if item.is_dir:
                    continue
                if _is_supported_companion_name(item.name):
                    result.append(item)
                    if len(result) >= max_files:
                        return
            for item in items:
                if len(result) >= max_files:
                    return
                if not item.is_dir or item.name in {"@eaDir", "#recycle", ".Trash", "System Volume Information"}:
                    continue
                walk(item.path, depth + 1)

        walk(normalized_root, 0)
        return result

    def _scan_completeness_message(self, task: dict[str, Any], videos: list[Any], root_path: str) -> str:
        if not self._task_has_staging_plan(task):
            return ""
        filters = self._task_scan_filters(task)
        normalized_root = normalize_path(root_path)
        # 持久化 expected_count 可能把非 OpenList 命名空间路径（如 rclone 存储的
        # webdav/...）一并计入导致虚高，从而无限等待"完整可见"。对暂存任务以
        # root 下的精确路径数为准，缺失时才回退到持久化值。
        scoped_expected_paths = [
            normalize_path(item)
            for item in (filters.get("expected_paths") or [])
            if _path_is_same_or_child(normalize_path(item), normalized_root)
        ]
        expected_count = len(scoped_expected_paths) or _safe_non_negative_int(filters.get("expected_count"))
        if expected_count <= 0:
            return ""
        visible_paths = {
            normalize_path(getattr(item, "path", "")).casefold()
            for item in videos
            if _path_is_same_or_child(normalize_path(getattr(item, "path", "")), normalized_root)
        }
        visible_count = len([path for path in visible_paths if path])
        if visible_count >= expected_count:
            return ""
        return f"OpenList 任务目录文件尚未完整可见：已看到 {visible_count}/{expected_count} 个视频文件"

    def _repair_task_title_from_linked_job(self, task: dict[str, Any]) -> dict[str, Any]:
        """重试旧任务时，用关联入库任务恢复被路径分隔符误截断的标题。"""

        try:
            job_id = int(task.get("job_id") or 0)
        except (TypeError, ValueError):
            job_id = 0
        getter = getattr(self.db, "get_job", None)
        if not job_id or not callable(getter):
            return task
        try:
            job = getter(job_id)
            if not isinstance(job, dict):
                return task
            raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
            repaired = _title_from_update_payload(raw_data, job, normalize_path(task.get("openlist_root_path") or ""))
            current = str(task.get("title") or "").strip()
            if not repaired or repaired == current:
                return task
            evidence = task.get("evidence") if isinstance(task.get("evidence"), dict) else {}
            evidence = {
                **evidence,
                "title_repair": {
                    "from": current,
                    "to": repaired,
                    "source": "linked_import_job",
                },
            }
            if not self._update_organizer_task_from_snapshot(
                task,
                title=repaired,
                source_keyword=repaired,
                evidence=evidence,
            ):
                return task
            return {**task, "title": repaired, "source_keyword": repaired, "evidence": evidence}
        except Exception:  # noqa: BLE001
            # 这是旧数据兼容修复，不应因读取或写回关联任务失败而阻断主扫描流程。
            logger.warning(
                "organizer_task_title_repair_failed task_id=%s job_id=%s",
                task.get("id"),
                job_id,
                exc_info=True,
            )
            return task

    def _is_automatic_category_root_scan(self, task: dict[str, Any], root_path: str) -> bool:
        if str(task.get("trigger_type") or "").strip() == "admin_manual":
            return False
        category = self.categories.get(str(task.get("category") or ""), {})
        if not category:
            return False
        normalized_root = normalize_path(root_path)
        category_root = _source_category_root_for_path(normalized_root, category)
        return normalize_path(category_root).casefold() == normalized_root.casefold()

    def rebuild_task(self, task_id: int) -> dict[str, Any]:
        # “重建计划”仍会重新扫描并覆盖旧计划，但高置信、无冲突的结果应继续
        # 自动执行，避免任务停在 auto_approved 后还要求管理员再次点“执行”。
        return self.process_task(task_id, auto_apply=True)

    def approve_task(self, task_id: int) -> dict[str, Any]:
        task = self.db.get_organizer_task(task_id)
        if not task:
            return {"success": False, "message": "标准化任务不存在"}
        status = str(task.get("status") or "").strip().lower()
        if status in {"scanning", "executing"}:
            return {"success": False, "conflict": True, "message": f"任务正在{'扫描' if status == 'scanning' else '执行'}，不能修改或放行"}
        if status in {"done", "skipped", "cancelled"}:
            return {"success": False, "conflict": True, "message": f"任务已处于 {status}，不能再放行"}
        approved_mappings = [dict(mapping) for mapping in task.get("mappings") or []]
        blocked = [
            mapping
            for mapping in approved_mappings
            if str(mapping.get("status") or "") in {"need_edit", "conflict"}
        ]
        if blocked:
            summary = _mapping_problem_summary(
                approved_mappings,
                float(task.get("confidence") or 0),
                {},
            )
            return {
                "success": False,
                "conflict": True,
                "message": f"仍有 {len(blocked)} 项未解决，任务批准不会自动把问题项改为可执行",
                "blocked": len(blocked),
                "problem_summary": summary,
            }
        try:
            self._validate_approval_mappings(task, approved_mappings)
        except OpenListError as exc:
            return {"success": False, "conflict": True, "message": str(exc)}
        mapping_updates: list[dict[str, Any]] = []

        atomic_updater = getattr(self.db, "update_organizer_mappings_and_plan", None)
        expected_revision = _safe_positive_int(task.get("revision"))
        if callable(atomic_updater) and expected_revision:
            try:
                category = self.categories.get(str(task.get("category") or ""), {})
                target_category = self._target_category_for_task(task, category)
                operations = self._operations_for_mappings(
                    approved_mappings,
                    target_category,
                    include_auto_delete=True,
                )
                evidence = dict(task.get("evidence") or {}) if isinstance(task.get("evidence"), dict) else {}
                updated = atomic_updater(
                    task_id,
                    mapping_updates=mapping_updates,
                    operations=operations,
                    evidence=evidence,
                    expected_status=str(task.get("status") or ""),
                    expected_revision=expected_revision,
                    task_updates={
                        "status": "manual_confirmed",
                        "confidence": max(float(task.get("confidence") or 0), 88),
                    },
                    clear_scan_lease=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("organizer_approve_atomic_write_failed task_id=%s", task_id)
                return {"success": False, "message": f"任务放行失败且已回滚：{exc}"}
        else:
            # Compatibility fallback for lightweight adapters. Production
            # Database always uses the transaction above.
            for item in mapping_updates:
                self.db.update_organizer_mapping(int(item["id"]), **item["updates"])
            updated = self.db.update_organizer_task(
                task_id,
                status="manual_confirmed",
                confidence=max(float(task.get("confidence") or 0), 88),
                expected_revision=expected_revision,
                expected_statuses={status},
                bump_revision=True,
                clear_scan_lease=True,
            )
        if updated is False:
            return {"success": False, "conflict": True, "message": "任务状态已变化，请刷新后重试"}
        return {"success": True, "task": self.db.get_organizer_task(task_id)}

    def _validate_approval_mappings(
        self,
        task: dict[str, Any],
        mappings: list[dict[str, Any]],
    ) -> None:
        projected = {**task, "mappings": mappings}
        self._validate_staging_mapping_boundaries(projected)
        category = self.categories.get(str(task.get("category") or ""), {})
        target_category = self._target_category_for_task(task, category)
        final_root = normalize_path(category_target_root(target_category))
        task_root = normalize_path(task.get("openlist_root_path") or "")
        staging_root = task_root if self._task_has_staging_plan(task) else ""
        owners: dict[str, int] = {}
        for mapping in mappings:
            if str(mapping.get("status") or "") not in {"ready", "skipped_existing"}:
                continue
            source = normalize_path(mapping.get("source_path") or "")
            target = normalize_path(mapping.get("target_path") or "")
            if not source or not target:
                raise OpenListError("映射源路径或目标路径为空，不能批准")
            if task_root != "/" and not _path_is_same_or_child(source, task_root):
                raise OpenListError(f"映射源路径不在任务扫描目录：{source}")
            if final_root != "/" and not _is_child_path(target, final_root):
                raise OpenListError(f"映射目标路径不在最终分类目录：{target}")
            if staging_root and _path_is_same_or_child(target, staging_root):
                raise OpenListError(f"映射目标仍位于任务暂存目录：{target}")
            key = target.casefold()
            owners[key] = owners.get(key, 0) + 1
        duplicates = [path for path, count in owners.items() if count > 1]
        if duplicates:
            raise OpenListError(f"存在 {len(duplicates)} 个重复目标路径，不能批准")

    def skip_task(self, task_id: int) -> dict[str, Any]:
        task = self.db.get_organizer_task(task_id, include_children=False)
        if not task:
            return {"success": False, "message": "标准化任务不存在"}
        status = str(task.get("status") or "").strip().lower()
        if status == "executing":
            return {
                "success": False,
                "conflict": True,
                "message": "Organizer 已开始真实移动，不能用‘跳过’中断；请使用入库任务取消",
            }
        if status == "done":
            return {"success": False, "conflict": True, "message": "标准化任务已完成，不能跳过"}
        staging_plan = self._validated_task_staging_plan(task)
        if staging_plan:
            if status == "scanning":
                return {
                    "success": False,
                    "conflict": True,
                    "message": "Organizer 正在扫描暂存目录，请等待进入审核后再选择跳过整理并入库",
                }
            if status in {"cancelled", "skipped"}:
                return {
                    "success": False,
                    "conflict": True,
                    "message": f"任务已处于 {status}，不能再执行直通入库",
                }
            try:
                return self._prepare_staging_passthrough(task, staging_plan)
            except Exception as exc:  # noqa: BLE001
                logger.exception("organizer_passthrough_prepare_failed task_id=%s", task_id)
                return {
                    "success": False,
                    "message": f"生成原名直通入库计划失败：{exc}",
                }
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        updated = self.db.update_organizer_task(
            task_id,
            status="skipped",
            error_message="",
            raw_data={**raw_data, "skip_requested": {"at": _utc_now_text(), "previous_status": status}},
            expected_revision=_safe_positive_int(task.get("revision")),
            expected_statuses={status},
            bump_revision=True,
            clear_scan_lease=True,
        )
        if updated is False:
            return {"success": False, "conflict": True, "message": "任务状态已变化，请刷新后重试"}
        self._sync_linked_job(
            task,
            status=JOB_DONE,
            stage="done",
            message="资源已位于正式分类目录，已按管理员要求跳过命名整理",
            extra={"organizer_passthrough": False, "organizer_skipped": True},
        )
        return {
            "success": True,
            "skipped": True,
            "message": "资源已位于正式分类目录，已跳过命名整理",
            "task": self.db.get_organizer_task(task_id),
        }

    def delete_task(self, task_id: int) -> dict[str, Any]:
        task = self.db.get_organizer_task(task_id, include_children=False)
        if not task:
            return {"success": False, "not_found": True, "message": "记录不存在"}
        status = str(task.get("status") or "").strip().lower()
        deletable_statuses = {"done", "cancelled"}
        if status not in deletable_statuses:
            return {
                "success": False,
                "conflict": True,
                "message": "只能删除已完成或已取消的记录",
            }
        deleted = self.db.delete_organizer_task_if_status(task_id, deletable_statuses)
        if not deleted:
            return {
                "success": False,
                "conflict": True,
                "message": "任务状态已变化，删除未执行，请刷新后重试",
            }
        logger.info("organizer_record_deleted task_id=%s status=%s", task_id, status)
        return {
            "success": True,
            "deleted": True,
            "task_id": task_id,
            "message": "记录已删除",
        }

    def _prepare_staging_passthrough(
        self,
        task: dict[str, Any],
        staging_plan: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = _safe_positive_int(task.get("id"))
        if not task_id:
            raise OpenListError("Organizer 任务 ID 无效")
        category_key = str(task.get("category") or "").strip()
        category = self.categories.get(category_key, {})
        task_root = normalize_path(task.get("openlist_root_path") or "")
        staging_root = normalize_path(staging_plan.get("openlist_job_root") or "")
        final_root = normalize_path(staging_plan.get("openlist_final_category_root") or "")
        if not task_root or task_root == "/":
            raise OpenListError("暂存任务扫描目录为空")
        if not staging_root or staging_root == "/" or not _path_is_same_or_child(task_root, staging_root):
            raise OpenListError("暂存任务扫描目录不在固化的 job 目录内")
        if not final_root or final_root == "/":
            raise OpenListError("暂存任务缺少最终分类目录")
        staging_category_root = normalize_path(staging_plan.get("openlist_staging_category_root") or "")
        if staging_category_root and _path_is_same_or_child(final_root, staging_category_root):
            raise OpenListError("最终分类目录仍位于暂存区，拒绝直通入库")
        target_category = self._target_category_for_task(task, category)
        self._validate_staging_task_boundaries(
            task,
            scan_root=task_root,
            target_category=target_category,
        )

        try:
            configured_limit = int(self.organizer_config.get("skip_passthrough_max_files") or 5000)
        except (TypeError, ValueError):
            configured_limit = 5000
        passthrough_limit = max(1, min(configured_limit, 20000))
        source_paths = sorted(
            self._list_openlist_tree_files(
                task_root,
                refresh=True,
                limit=passthrough_limit,
            ),
            key=str.casefold,
        )
        if not source_paths:
            raise OpenListError("暂存目录为空，没有可直通入库的文件")
        if not any(posixpath.splitext(path)[1].lower() in VIDEO_EXTENSIONS for path in source_paths):
            raise OpenListError("暂存目录中没有视频文件，不能完成直通入库")

        files: list[dict[str, Any]] = []
        mappings: list[dict[str, Any]] = []
        target_owners: set[str] = set()
        staging_prefix = staging_root.rstrip("/")
        media_type = "movie" if category_key == "movie" else "tv"
        for source_path in source_paths:
            source = normalize_path(source_path)
            if not _is_child_path(source, staging_root):
                raise OpenListError(f"直通源文件不在任务暂存目录：{source}")
            relative = source[len(staging_prefix) :].lstrip("/")
            if not relative:
                raise OpenListError(f"无法计算直通相对路径：{source}")
            target = join_path(final_root, relative)
            if not _is_child_path(target, final_root):
                raise OpenListError(f"直通目标路径越界：{target}")
            target_key = target.casefold()
            if target_key in target_owners:
                raise OpenListError(f"直通计划存在重复目标路径：{target}")
            target_owners.add(target_key)
            name = basename(source)
            files.append(
                {
                    "path": source,
                    "name": name,
                    "parent_path": dirname(source),
                    "ext": posixpath.splitext(name)[1].lower(),
                    "raw_data": {"passthrough_import": True},
                }
            )
            mappings.append(
                {
                    "source_path": source,
                    "source_name": name,
                    "target_path": target,
                    "target_name": basename(target),
                    "media_type": media_type,
                    "title": str(task.get("title") or ""),
                    "confidence": 100,
                    "status": "ready",
                    "reason": ["管理员跳过命名整理，按暂存相对路径原名移入正式分类"],
                    "raw_data": {
                        "staging_file": True,
                        "passthrough_import": True,
                        "preserve_relative_path": relative,
                    },
                }
            )

        passthrough_category = {
            **target_category,
            "openlist_root_path": final_root,
            "source_category_root_path": staging_root,
        }
        passthrough_category.pop("resource_root_path", None)
        passthrough_category.pop("canonical_resource_root", None)
        operations = self._operations_for_mappings(
            mappings,
            passthrough_category,
            include_auto_delete=False,
        )
        now = _utc_now_text()
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        passthrough_state = {
            "enabled": True,
            "requested_at": now,
            "source_root": task_root,
            "staging_job_root": staging_root,
            "target_root": final_root,
            "file_count": len(files),
            "preserve_names": True,
            "skip_openlist_strm_refresh": True,
        }
        evidence = task.get("evidence") if isinstance(task.get("evidence"), dict) else {}
        replaced = self.db.replace_organizer_plan(
            task_id,
            files=files,
            mappings=mappings,
            operations=operations,
            expected_revision=_safe_positive_int(task.get("revision")),
            expected_status=str(task.get("status") or ""),
            task_updates={
                "status": "manual_confirmed",
                "confidence": 0,
                "error_message": "",
                "media_type": media_type,
                "tmdb_id": None,
                "tmdb_title": "",
                "tmdb_year": "",
                "evidence": {**evidence, "passthrough_import": passthrough_state},
                "raw_data": {
                    **raw_data,
                    "skip_requested": {"at": now, "previous_status": str(task.get("status") or "")},
                    "passthrough_import": passthrough_state,
                },
            },
        )
        if not replaced:
            return {
                "success": False,
                "conflict": True,
                "message": "任务状态已变化，原名直通入库计划未写入，请刷新后重试",
            }
        logger.info(
            "organizer_passthrough_prepared task_id=%s source_root=%s target_root=%s files=%s",
            task_id,
            task_root,
            final_root,
            len(files),
        )
        return {
            "success": True,
            "ready_for_apply": True,
            "passthrough": True,
            "task_id": task_id,
            "status": "manual_confirmed",
            "file_count": len(files),
            "target_root": final_root,
            "message": f"已生成原名直通入库计划，共 {len(files)} 个文件，正在移入正式分类目录",
            "task": self.db.get_organizer_task(task_id),
        }

    def start_apply_task(self, task_id: int) -> dict[str, Any]:
        """提交真实 OpenList 整理到后台线程执行，接口只负责快速确认已接收。"""

        task = self.db.get_organizer_task(task_id)
        if not task:
            return {"success": False, "message": "标准化任务不存在"}
        status = str(task.get("status") or "")
        if status in {"cancelled", "skipped"}:
            return {"success": False, "message": f"标准化任务已{'取消' if status == 'cancelled' else '跳过'}，不再执行"}
        if status == "done":
            return {"success": True, "task_id": task_id, "status": "done", "message": "标准化任务已完成，无需重复执行"}
        blocked = [m for m in task.get("mappings") or [] if str(m.get("status")) in {"conflict", "need_edit"}]
        if blocked:
            if not self._update_organizer_task_from_snapshot(
                task,
                status="waiting_review",
                error_message=f"仍有 {len(blocked)} 项需要审核",
            ):
                return {"success": False, "message": "任务状态已变化，请刷新后重试"}
            return {"success": False, "message": f"仍有 {len(blocked)} 项需要审核", "blocked": len(blocked)}
        with self._background_apply_lock:
            if task_id in self._background_apply_tasks:
                return {"success": True, "queued": True, "task_id": task_id, "status": "executing", "message": "Organizer 已在后台执行队列中"}
            self._background_apply_tasks.add(task_id)
        run_id = 0
        try:
            # 必须把 run 持久化与任务进入 executing 放在同一个数据库 claim 中。
            # 否则进程若在后台线程真正开始前退出，启动恢复只能看到一个没有 run
            # 的 executing 任务，无法安全回收，会永久停留在“整理中”。
            atomic_run_claim = callable(getattr(self.db, "claim_organizer_run", None))
            run_id, active_run = self._claim_organizer_run(task_id)
            if not run_id:
                with self._background_apply_lock:
                    self._background_apply_tasks.discard(task_id)
                current_status = str((active_run or {}).get("task_status") or "executing")
                if current_status == "missing":
                    return {"success": False, "task_id": task_id, "message": "标准化任务不存在"}
                if current_status == "done":
                    return {
                        "success": True,
                        "task_id": task_id,
                        "status": "done",
                        "message": "标准化任务已完成，无需重复执行",
                    }
                if current_status in {"cancelled", "skipped"}:
                    return {
                        "success": False,
                        "cancelled": current_status == "cancelled",
                        "skipped": True,
                        "task_id": task_id,
                        "status": current_status,
                        "message": f"Organizer 任务已{'取消' if current_status == 'cancelled' else '跳过'}，不再执行",
                    }
                return {
                    "success": True,
                    "queued": True,
                    "task_id": task_id,
                    "run_id": _safe_positive_int((active_run or {}).get("id")) or None,
                    "status": "executing",
                    "message": "Organizer 已由其他进程提交后台执行，请勿重复提交",
                }
            if run_id <= 0:
                raise RuntimeError("Organizer 运行记录创建失败")
            if atomic_run_claim:
                task = {
                    **task,
                    "status": "executing",
                    "revision": _safe_positive_int(task.get("revision")) or 1,
                }
            else:
                if not self._update_organizer_task_from_snapshot(
                    task,
                    status="executing",
                    error_message="",
                ):
                    raise OrganizerTaskCancelled("Organizer 任务状态已变化，拒绝启动后台执行")
                task = {**task, "status": "executing"}
            self._sync_linked_job(task, status=JOB_ORGANIZING, stage="organizing", message="Organizer 已提交后台执行，正在执行标准化移动/重命名")
            thread = threading.Thread(
                target=self._apply_task_safely,
                args=(task_id, run_id),
                name=f"organizer-apply-{task_id}",
                daemon=True,
            )
            thread.start()
        except OrganizerTaskCancelled as exc:
            message = str(exc)
            self._finish_organizer_run_failure(run_id, task_id, message)
            with self._background_apply_lock:
                self._background_apply_tasks.discard(task_id)
            current = self.db.get_organizer_task(task_id, include_children=False) or task
            return {
                "success": False,
                "cancelled": str(current.get("status") or "") == "cancelled",
                "skipped": True,
                "task_id": task_id,
                "run_id": run_id or None,
                "status": str(current.get("status") or ""),
                "message": message,
            }
        except Exception as exc:  # noqa: BLE001
            message = f"Organizer 后台执行启动失败：{exc}"
            try:
                latest = self.db.get_organizer_task(task_id, include_children=False) or task
                updated = self._update_organizer_task_from_snapshot(
                    latest,
                    status="failed",
                    error_message=message,
                )
                if not updated:
                    raise OrganizerTaskCancelled("Organizer 任务状态已变化，未覆盖后台启动结果")
                self._sync_linked_job(
                    task,
                    status=JOB_REVIEW,
                    stage="review",
                    message=message,
                    level=EVENT_ERROR,
                    error_message=message,
                )
            except Exception:  # noqa: BLE001
                logger.debug("mark organizer apply startup failure failed task_id=%s", task_id, exc_info=True)
            self._finish_organizer_run_failure(run_id, task_id, message)
            with self._background_apply_lock:
                self._background_apply_tasks.discard(task_id)
            logger.exception("organizer_background_apply_start_failed task_id=%s", task_id)
            return {
                "success": False,
                "task_id": task_id,
                "run_id": run_id or None,
                "status": "failed",
                "message": message,
            }
        return {
            "success": True,
            "queued": True,
            "task_id": task_id,
            "run_id": run_id,
            "status": "executing",
            "message": "已提交 OpenList 整理后台执行，可关闭弹窗稍后查看进度",
        }

    def _apply_task_safely(self, task_id: int, run_id: int | None = None) -> None:
        try:
            self.apply_task(task_id, run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("organizer_background_apply_failed task_id=%s", task_id)
            latest = self.db.get_organizer_task(task_id, include_children=False) or {}
            self._update_organizer_task_from_snapshot(
                latest,
                status="failed",
                error_message=str(exc),
            )
            self._finish_organizer_run_failure(run_id, task_id, str(exc))
        finally:
            with self._background_apply_lock:
                self._background_apply_tasks.discard(task_id)

    def update_mapping(self, mapping_id: int, payload: dict[str, Any], task_id: int | None = None) -> dict[str, Any]:
        updates = {key: payload[key] for key in ("target_path", "target_name", "media_type", "title", "year", "season", "episode", "tmdb_id", "confidence", "status") if key in payload}
        if "reason" in payload:
            updates["reason"] = payload.get("reason")
        recalculated_target = ""
        task_before: dict[str, Any] | None = None
        mapping_before: dict[str, Any] | None = None
        if task_id:
            task_before = self.db.get_organizer_task(task_id)
            if task_before:
                task_status = str(task_before.get("status") or "").strip().lower()
                if task_status in {"scanning", "executing", "done", "skipped", "cancelled"}:
                    return {
                        "success": False,
                        "conflict": True,
                        "message": f"Organizer 任务处于 {task_status}，已拒绝修改映射",
                    }
                mapping_before = next((item for item in task_before.get("mappings") or [] if int(item.get("id") or 0) == int(mapping_id)), None)
                if mapping_before:
                    category = self.categories.get(str(task_before.get("category") or ""), {})
                    target_category = self._target_category_for_task(task_before, category)
                    recalculated_target = self._recalculate_mapping_target_if_needed(mapping_before, updates, task_before, target_category)
                    if recalculated_target:
                        updates["target_path"] = recalculated_target
                        updates["target_name"] = basename(recalculated_target)
                        reason = updates.get("reason") if "reason" in updates else mapping_before.get("reason")
                        reason_list = [*(reason if isinstance(reason, list) else ([reason] if reason else []))]
                        note = "管理员修改标题/年份后已自动重算最终目标路径"
                        if note not in reason_list:
                            reason_list.append(note)
                        updates["reason"] = reason_list
                else:
                    return {"success": False, "message": "映射记录不属于当前任务"}

        atomic_updater = getattr(self.db, "update_organizer_mappings_and_plan", None)
        expected_revision = _safe_positive_int((task_before or {}).get("revision"))
        if task_id and task_before and mapping_before and callable(atomic_updater) and expected_revision:
            updated_mappings = [
                ({**item, **updates} if int(item.get("id") or 0) == int(mapping_id) else dict(item))
                for item in task_before.get("mappings") or []
            ]
            try:
                category = self.categories.get(str(task_before.get("category") or ""), {})
                target_category = self._target_category_for_task(task_before, category)
                operations = self._operations_for_mappings(
                    updated_mappings,
                    target_category,
                    include_auto_delete=False,
                )
                evidence = dict(task_before.get("evidence") or {}) if isinstance(task_before.get("evidence"), dict) else {}
                episode_completeness = _episode_completeness_report(
                    str(task_before.get("category") or ""),
                    updated_mappings,
                )
                if episode_completeness:
                    evidence["episode_completeness"] = episode_completeness
                else:
                    evidence.pop("episode_completeness", None)
                updated = atomic_updater(
                    task_id,
                    mapping_updates=[{"id": int(mapping_id), "updates": updates}],
                    operations=operations,
                    evidence=evidence,
                    expected_status=str(task_before.get("status") or ""),
                    expected_revision=expected_revision,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "organizer_mapping_atomic_write_failed task_id=%s mapping_id=%s",
                    task_id,
                    mapping_id,
                )
                return {"success": False, "message": f"映射修改失败且已回滚：{exc}"}
            if not updated:
                return {
                    "success": False,
                    "conflict": True,
                    "message": "Organizer 任务状态或版本已变化，映射修改未写入",
                }
            return {
                "success": True,
                "mapping_id": mapping_id,
                "target_path": recalculated_target or updates.get("target_path") or "",
            }

        # Compatibility fallback for lightweight/legacy adapters without the
        # atomic plan writer. Production Database never takes this path.
        self.db.update_organizer_mapping(mapping_id, **updates)
        if task_id:
            task = self.db.get_organizer_task(task_id)
            if task:
                category = self.categories.get(str(task.get("category") or ""), {})
                target_category = self._target_category_for_task(task, category)
                operations = self._operations_for_mappings(task.get("mappings") or [], target_category, include_auto_delete=False)
                self.db.replace_organizer_operations(task_id, operations)
                evidence = dict(task.get("evidence") or {}) if isinstance(task.get("evidence"), dict) else {}
                episode_completeness = _episode_completeness_report(
                    str(task.get("category") or ""),
                    task.get("mappings") or [],
                )
                if episode_completeness:
                    evidence["episode_completeness"] = episode_completeness
                else:
                    evidence.pop("episode_completeness", None)
                if not self._update_organizer_task_from_snapshot(task, evidence=evidence):
                    return {
                        "success": False,
                        "conflict": True,
                        "message": "Organizer 任务状态已变化，未覆盖当前完整性报告",
                    }
        return {"success": True, "mapping_id": mapping_id, "target_path": recalculated_target or updates.get("target_path") or ""}

    def batch_update_mappings(self, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Precompute and atomically persist a task-wide title/season edit."""

        task = self.db.get_organizer_task(task_id)
        if not task:
            return {"success": False, "message": "标准化任务不存在"}
        task_status = str(task.get("status") or "").strip().lower()
        if task_status in {"scanning", "executing", "done", "skipped", "cancelled"}:
            return {
                "success": False,
                "conflict": True,
                "message": f"Organizer 任务处于 {task_status}，已拒绝修改映射",
            }

        title_supplied = payload.get("title") is not None
        title = str(payload.get("title") or "").strip() if title_supplied else ""
        if title_supplied and not title:
            return {"success": False, "message": "片名不能为空"}
        season_supplied = payload.get("season") is not None
        season: int | None = None
        if season_supplied:
            try:
                season = int(payload.get("season"))
            except (TypeError, ValueError):
                return {"success": False, "message": "季号必须是数字"}
            if season < 0 or season > 99:
                return {"success": False, "message": "季号超出范围（0-99）"}
        if not title_supplied and not season_supplied:
            return {"success": False, "message": "请填写要修改的片名或季号"}

        mappings = [dict(item) for item in task.get("mappings") or [] if int(item.get("id") or 0) > 0]
        if not mappings:
            return {"success": False, "message": "该任务没有可修改的映射"}

        category = self.categories.get(str(task.get("category") or ""), {})
        target_category = self._target_category_for_task(task, category)
        mapping_updates: list[dict[str, Any]] = []
        updated_mappings: list[dict[str, Any]] = []
        for mapping in mappings:
            updates: dict[str, Any] = {}
            if title_supplied:
                updates["title"] = title
            if season_supplied:
                updates["season"] = season
            recalculated_target = self._recalculate_mapping_target_if_needed(mapping, updates, task, target_category)
            if recalculated_target:
                updates["target_path"] = recalculated_target
                updates["target_name"] = basename(recalculated_target)
                reason = mapping.get("reason")
                reason_list = [*(reason if isinstance(reason, list) else ([reason] if reason else []))]
                note = "管理员批量修改标题/季号后已自动重算最终目标路径"
                if note not in reason_list:
                    reason_list.append(note)
                updates["reason"] = reason_list
            updated = {**mapping, **updates}
            mapping_updates.append({"id": int(mapping["id"]), "updates": updates})
            updated_mappings.append(updated)

        target_owners: dict[str, int] = {}
        for mapping in updated_mappings:
            target_path = str(mapping.get("target_path") or "").strip()
            # Only ready mappings create/move a target. Multiple skipped_existing
            # duplicate sources may legitimately reference the same canonical file.
            if not target_path or str(mapping.get("status") or "") != "ready":
                continue
            target_key = normalize_path(target_path).casefold()
            mapping_id = int(mapping.get("id") or 0)
            previous = target_owners.get(target_key)
            if previous and previous != mapping_id:
                return {
                    "success": False,
                    "conflict": True,
                    "message": f"批量修改会让映射 #{previous} 与 #{mapping_id} 指向同一目标，已取消全部修改",
                }
            target_owners[target_key] = mapping_id

        try:
            operations = self._operations_for_mappings(updated_mappings, target_category, include_auto_delete=False)
            evidence = dict(task.get("evidence") or {}) if isinstance(task.get("evidence"), dict) else {}
            episode_completeness = _episode_completeness_report(
                str(task.get("category") or ""),
                updated_mappings,
            )
            if episode_completeness:
                evidence["episode_completeness"] = episode_completeness
            else:
                evidence.pop("episode_completeness", None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("organizer_batch_mapping_preflight_failed task_id=%s", task_id)
            return {"success": False, "message": f"批量修改预检失败，未写入任何映射：{exc}"}

        updater = getattr(self.db, "update_organizer_mappings_and_plan", None)
        if not callable(updater):
            return {"success": False, "message": "当前数据库适配器不支持原子批量修改，未写入任何映射"}
        try:
            updated = updater(
                task_id,
                mapping_updates=mapping_updates,
                operations=operations,
                evidence=evidence,
                expected_status=str(task.get("status") or ""),
                expected_revision=_safe_positive_int(task.get("revision")) or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("organizer_batch_mapping_write_failed task_id=%s", task_id)
            return {"success": False, "message": f"批量修改失败且已回滚：{exc}"}
        if not updated:
            return {
                "success": False,
                "conflict": True,
                "message": "Organizer 任务状态或版本已变化，批量修改未写入",
            }
        return {
            "success": True,
            "message": f"已批量更新 {len(mapping_updates)} 条映射",
            "changed": len(mapping_updates),
            "task": self.db.get_organizer_task(task_id),
        }

    def _recalculate_mapping_target_if_needed(self, mapping: dict[str, Any], updates: dict[str, Any], task: dict[str, Any], target_category: dict[str, Any]) -> str:
        identity_keys = {"media_type", "title", "year", "season", "episode"}
        if not (identity_keys & set(updates.keys())):
            return ""
        old_target = str(mapping.get("target_path") or "").strip()
        payload_target = str(updates.get("target_path") or "").strip() if "target_path" in updates else old_target
        if old_target and payload_target and normalize_path(payload_target) != normalize_path(old_target):
            # 用户直接改了目标路径时，以目标路径为准，不再用标题/年份覆盖。
            return ""
        title = str(updates.get("title") if "title" in updates else mapping.get("title") or "").strip()
        if not title:
            return ""
        media_type = str(updates.get("media_type") if "media_type" in updates else mapping.get("media_type") or "movie").strip().lower()
        year = str(updates.get("year") if "year" in updates else mapping.get("year") or "").strip()
        season = _optional_non_negative_int(updates.get("season") if "season" in updates else mapping.get("season"))
        episode = _safe_positive_int(updates.get("episode") if "episode" in updates else mapping.get("episode"))
        source_path = str(mapping.get("source_path") or "").strip()
        ext = posixpath.splitext(basename(source_path or old_target))[1]
        raw_data = mapping.get("raw_data") if isinstance(mapping.get("raw_data"), dict) else {}
        parsed = raw_data.get("parsed") if isinstance(raw_data.get("parsed"), dict) else {}
        version = _mapping_version(source_path or old_target, parsed.get("version") if isinstance(parsed, dict) else "")
        return standard_target_path(
            category_key="movie" if media_type == "movie" else "tv",
            category=target_category,
            title=title,
            year=year,
            season=season,
            episode=episode,
            ext=ext,
            version=version,
        )

    def apply_task_from_worker(self, task_id: int) -> dict[str, Any]:
        worker_context = getattr(self, "_worker_context", None)
        if worker_context is None:
            worker_context = threading.local()
            self._worker_context = worker_context
        worker_context.active = True
        try:
            return self.apply_task(task_id)
        finally:
            worker_context.active = False

    def apply_task(self, task_id: int, *, run_id: int | None = None) -> dict[str, Any]:
        run_id = _safe_positive_int(run_id)
        task = self.db.get_organizer_task(task_id)
        if not task:
            self._finish_organizer_run_failure(run_id, task_id, "标准化任务不存在")
            return {"success": False, "message": "标准化任务不存在"}
        if not run_id:
            run_id, active_run = self._claim_organizer_run(task_id)
            if not run_id:
                current_status = str((active_run or {}).get("task_status") or "executing")
                if current_status == "done":
                    return {
                        "success": True,
                        "task_id": task_id,
                        "status": "done",
                        "message": "标准化任务已完成，无需重复执行",
                    }
                if current_status in {"cancelled", "skipped"}:
                    return {
                        "success": False,
                        "cancelled": current_status == "cancelled",
                        "skipped": True,
                        "task_id": task_id,
                        "status": current_status,
                        "message": f"Organizer 任务已{'取消' if current_status == 'cancelled' else '跳过'}，不再执行",
                    }
                return {
                    "success": current_status != "missing",
                    "queued": current_status != "missing",
                    "task_id": task_id,
                    "run_id": _safe_positive_int((active_run or {}).get("id")) or None,
                    "status": current_status,
                    "message": "标准化任务不存在" if current_status == "missing" else "Organizer 已由其他进程执行",
                }
            task = self.db.get_organizer_task(task_id) or task
        # Cancellation always wins.  After that, validate the persisted staging
        # boundaries before reporting a generic state conflict so a tampered
        # manual plan is recorded as the actual security failure.  The normal
        # owned apply path repeats this validation immediately before locks and
        # side effects.
        abort_reason = self._task_abort_reason(task)
        if abort_reason:
            self._finish_organizer_run_failure(run_id, task_id, abort_reason)
            return {
                "success": False,
                "skipped": True,
                "cancelled": True,
                "task_id": task_id,
                "run_id": run_id,
                "message": abort_reason,
            }
        task_status = str(task.get("status") or "").strip().lower()
        if task_status in {"executing", "manual_confirmed"}:
            try:
                self._validate_staging_mapping_boundaries(task)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                self._update_organizer_run(
                    run_id,
                    "failed",
                    summary={"task_id": task_id},
                    error_message=message,
                )
                updated = self._update_organizer_task_from_snapshot(
                    task,
                    status="failed",
                    error_message=message,
                )
                if updated:
                    self._sync_linked_job(
                        task,
                        status=JOB_REVIEW,
                        stage="review",
                        message=f"Organizer 整理失败：{message}",
                        level=EVENT_ERROR,
                        error_message=message,
                    )
                return {
                    "success": False,
                    "skipped": not updated,
                    "task_id": task_id,
                    "run_id": run_id,
                    "message": message,
                }
        abort_reason = self._task_abort_reason(task, allowed_statuses={"executing"})
        if abort_reason:
            self._finish_organizer_run_failure(run_id, task_id, abort_reason)
            return {
                "success": False,
                "skipped": True,
                "cancelled": True,
                "task_id": task_id,
                "run_id": run_id,
                "message": abort_reason,
            }
        lease = OrganizerRunLease(
            database=self.db,
            run_id=run_id,
            owner_id=getattr(self, "owner_id", "organizer"),
            lease_seconds=self._organizer_run_lease_seconds(),
            heartbeat_interval_seconds=self._organizer_run_heartbeat_interval(),
            log=lambda message: logger.warning("%s", message),
        )
        try:
            with lease:
                lease.ensure_owned()
                return self._apply_task_owned(task_id, run_id, task, lease)
        except OrganizerRunLeaseLost as exc:
            message = str(exc)
            finalized = False
            try:
                self._finalize_organizer_run_and_task(
                    run_id,
                    task_id,
                    run_status="failed",
                    task_status="waiting_review",
                    summary={"task_id": task_id, "retryable": True, "lease_lost": True},
                    error_message=message,
                )
                finalized = True
            except OrganizerRunLeaseLost:
                # 已明确过期时只能走数据库原子回收；若已有新 owner 接管，回收
                # 条件不会命中，也不会覆盖新执行器的状态。
                self._recover_stale_runs_on_startup()
            except Exception:  # noqa: BLE001
                logger.debug("organizer lease-loss finalization failed task_id=%s", task_id, exc_info=True)
            if finalized:
                self._sync_linked_job(
                    task,
                    status=JOB_REVIEW,
                    stage="review",
                    message=f"Organizer 执行租约中断，已停止后续整理：{message}",
                    level=EVENT_WARN,
                    error_message=message,
                )
            logger.warning("organizer_apply_lease_lost task_id=%s run_id=%s", task_id, run_id)
            return {
                "success": False,
                "retryable": True,
                "task_id": task_id,
                "run_id": run_id,
                "message": message,
            }

    def _apply_task_owned(
        self,
        task_id: int,
        run_id: int,
        task: dict[str, Any],
        lease: OrganizerRunLease,
    ) -> dict[str, Any]:
        logger.info("organizer_apply_start task_id=%s root=%s title=%s", task_id, task.get("openlist_root_path"), task.get("title"))
        # 兼容旧版本已经进入 strm_pending 的任务：按当前规则直接收尾，
        # 不再等待或检查 STRM 文件是否生成。
        pending_strm = self._pending_strm_completion(task)
        if pending_strm:
            return self._resume_strm_completion(task_id, run_id, task, pending_strm, lease)
        category = self.categories.get(str(task.get("category") or ""), {})
        target_category = self._target_category_for_task(task, category)
        self._ensure_task_active(task_id, allowed_statuses={"executing"})
        # Scan/mapping edits already persist the exact operation plan.  Reuse it
        # after a crash so completed steps stay completed; only legacy tasks
        # without any operation rows need a one-time reconstruction.
        if not (task.get("operations") or []):
            operations = self._operations_for_mappings(
                task.get("mappings") or [],
                target_category,
                include_auto_delete=True,
            )
            self.db.replace_organizer_operations(task_id, operations)
            lease.ensure_owned()
            task = self.db.get_organizer_task(task_id) or task
        blocked = [m for m in task.get("mappings") or [] if str(m.get("status")) in {"conflict", "need_edit"}]
        if blocked:
            message = f"仍有 {len(blocked)} 项需要审核"
            self._finalize_organizer_run_and_task(
                run_id,
                task_id,
                run_status="failed",
                task_status="waiting_review",
                summary={"task_id": task_id},
                error_message=message,
            )
            self._sync_linked_job(
                task,
                status=JOB_REVIEW,
                stage="review",
                message=f"Organizer {message}，未确认完整入库",
                level=EVENT_WARN,
                error_message=message,
            )
            return {"success": False, "message": message, "blocked": len(blocked)}
        acquired: list[str] = []
        undo: list[dict[str, Any]] = []
        try:
            self._ensure_task_active(task_id, allowed_statuses={"executing"})
            lease.ensure_owned()
            self._validate_staging_mapping_boundaries(task)
            lock_keys = self._lock_keys(task)
            acquired = self._acquire_organizer_locks(task, run_id, lock_keys, lease=lease)
            lease.ensure_owned()
            task = self._ensure_task_active(task_id, allowed_statuses={"executing"})
            if not self._update_organizer_task_from_snapshot(task, status="executing", error_message=""):
                raise OrganizerTaskCancelled("Organizer 任务状态已变化，停止写入执行进度")
            self._sync_linked_job(task, status=JOB_ORGANIZING, stage="organizing", message="Organizer 正在执行标准化移动/重命名")
            done = 0
            skipped = 0
            failures: list[dict[str, Any]] = []
            operations = task.get("operations") or []
            op_index = 0
            while op_index < len(operations):
                op = operations[op_index]
                if str(op.get("status")) != "pending":
                    op_index += 1
                    continue
                batch = self._collect_move_file_batch(operations, op_index)
                if len(batch) >= 2:
                    self._ensure_task_active(task_id, allowed_statuses={"executing"})
                    lease.ensure_owned()
                    batch_verdicts = self._execute_move_file_batch(
                        batch,
                        staging_root=str(task.get("openlist_root_path") or ""),
                    )
                    lease.ensure_owned()
                    for batch_op, verdict, message, inverse in batch_verdicts:
                        if verdict == "done":
                            self.db.update_organizer_operation(int(batch_op["id"]), run_id=run_id, status="done", undo_data=inverse)
                            if inverse:
                                undo.insert(0, inverse)
                            done += 1
                        elif verdict == "skipped":
                            self.db.update_organizer_operation(int(batch_op["id"]), run_id=run_id, status="skipped", error_message=message)
                            skipped += 1
                        else:
                            self.db.update_organizer_operation(int(batch_op["id"]), run_id=run_id, status="failed", error_message=message)
                            failures.append(
                                {
                                    "operation_id": int(batch_op.get("id") or 0),
                                    "type": str(batch_op.get("type") or ""),
                                    "source_path": str(batch_op.get("source_path") or ""),
                                    "target_path": str(batch_op.get("target_path") or ""),
                                    "message": message,
                                }
                            )
                            logger.warning(
                                "organizer_operation_failed_continue task_id=%s operation_id=%s type=%s message=%s",
                                task_id,
                                batch_op.get("id"),
                                batch_op.get("type"),
                                message,
                            )
                    op_index += len(batch)
                    continue
                try:
                    self._ensure_task_active(task_id, allowed_statuses={"executing"})
                    lease.ensure_owned()
                    inverse = self._execute_operation(op)
                    lease.ensure_owned()
                    self.db.update_organizer_operation(int(op["id"]), run_id=run_id, status="done", undo_data=inverse)
                    if inverse:
                        undo.insert(0, inverse)
                    done += 1
                except OrganizerRunLeaseLost:
                    raise
                except SkipOperation as exc:
                    lease.ensure_owned()
                    self.db.update_organizer_operation(int(op["id"]), run_id=run_id, status="skipped", error_message=str(exc))
                    skipped += 1
                except Exception as exc:  # noqa: BLE001
                    lease.ensure_owned()
                    self.db.update_organizer_operation(int(op["id"]), run_id=run_id, status="failed", error_message=str(exc))
                    failures.append(
                        {
                            "operation_id": int(op.get("id") or 0),
                            "type": str(op.get("type") or ""),
                            "source_path": str(op.get("source_path") or ""),
                            "target_path": str(op.get("target_path") or ""),
                            "message": str(exc),
                        }
                    )
                    logger.warning(
                        "organizer_operation_failed_continue task_id=%s operation_id=%s type=%s message=%s",
                        task_id,
                        op.get("id"),
                        op.get("type"),
                        exc,
                    )
                op_index += 1
            lease.ensure_owned()
            summary = {"done": done, "skipped": skipped, "failed": len(failures), "task_id": task_id}
            if failures:
                summary["failures"] = failures[:20]
                first_message = failures[0].get("message") or "未知错误"
                message = f"Organizer 有 {len(failures)} 项整理操作失败，其余独立文件已继续处理；首个错误：{first_message}"
                self._finalize_organizer_run_and_task(
                    run_id,
                    task_id,
                    run_status="failed",
                    task_status="failed",
                    summary=summary,
                    undo_data=undo,
                    error_message=message,
                )
                self._sync_linked_job(
                    task,
                    status=JOB_REVIEW,
                    stage="review",
                    message=message,
                    level=EVENT_ERROR,
                    error_message=message,
                    extra={"operation_failures": failures[:20]},
                )
                logger.error("organizer_apply_partial_failure task_id=%s failed=%s done=%s skipped=%s", task_id, len(failures), done, skipped)
                return {"success": False, "task_id": task_id, "run_id": run_id, "message": message, "summary": summary}
            real_dir_cleanup = self._cleanup_source_empty_dirs_after_apply(task)
            self._ensure_task_active(task_id, allowed_statuses={"executing"})
            lease.ensure_owned()
            if real_dir_cleanup:
                summary["real_dir_cleanup"] = real_dir_cleanup
            self._sync_linked_job(task, status=JOB_CONFIRMING, stage="confirming", message="Organizer 已完成整理操作，正在复扫标准目录确认视频文件")
            confirmation = self._confirm_standardized_targets(task)
            self._ensure_task_active(task_id, allowed_statuses={"executing"})
            lease.ensure_owned()
            summary["completion_confirmation"] = confirmation
            task_evidence = task.get("evidence") if isinstance(task.get("evidence"), dict) else {}
            if real_dir_cleanup:
                task_evidence = {**task_evidence, "real_dir_cleanup": real_dir_cleanup}
            task_evidence = {**task_evidence, "completion_confirmation": confirmation}
            if not confirmation.get("success"):
                message = confirmation.get("message") or "标准目录复扫确认失败"
                self._finalize_organizer_run_and_task(
                    run_id,
                    task_id,
                    run_status="failed",
                    task_status="waiting_review",
                    summary=summary,
                    undo_data=undo,
                    error_message=message,
                    evidence=task_evidence,
                )
                self._sync_linked_job(task, status=JOB_REVIEW, stage="review", message=message, level=EVENT_WARN, error_message=message, extra={"confirmation": confirmation})
                logger.warning("organizer_apply_confirm_failed task_id=%s message=%s", task_id, message)
                return {"success": False, "task_id": task_id, "run_id": run_id, "message": message, "summary": summary}
            # 标准目录确认后只触发一次 OpenList 文件夹刷新。OpenList 接受刷新
            # 即表示 Organizer 的职责完成；STRM 生成、同步及后续维护由 OpenList
            # 自己异步处理，不再阻塞入库任务终态。
            return self._start_strm_completion(
                task_id,
                run_id,
                task,
                lease,
                summary=summary,
                undo=undo,
                task_evidence=task_evidence,
                confirmation=confirmation,
            )
        except OrganizerRunLeaseLost:
            raise
        except OrganizerTaskCancelled as exc:
            message = str(exc)
            cancelled = "关联入库任务已取消" in message
            try:
                self._finalize_organizer_run_and_task(
                    run_id,
                    task_id,
                    run_status="cancelled" if cancelled else "failed",
                    task_status="cancelled" if cancelled else "skipped",
                    summary={"task_id": task_id, "cancelled": cancelled},
                    undo_data=undo,
                    error_message=message,
                )
            except Exception:  # noqa: BLE001
                logger.debug("organizer cancellation finalization failed task_id=%s", task_id, exc_info=True)
            return {
                "success": False,
                "skipped": True,
                "cancelled": cancelled,
                "task_id": task_id,
                "run_id": run_id,
                "message": message,
            }
        except OrganizerLockTimeout as exc:
            self._finalize_organizer_run_and_task(
                run_id,
                task_id,
                run_status="failed",
                task_status="waiting_review",
                summary={"task_id": task_id, "retryable": True},
                error_message=str(exc),
            )
            self._sync_linked_job(
                task,
                status=JOB_REVIEW,
                stage="review",
                message=f"Organizer 等待路径锁超时，请稍后重试：{exc}",
                level=EVENT_WARN,
                error_message=str(exc),
            )
            logger.warning("organizer_apply_lock_timeout task_id=%s message=%s", task_id, exc)
            return {"success": False, "retryable": True, "task_id": task_id, "run_id": run_id, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            self._finalize_organizer_run_and_task(
                run_id,
                task_id,
                run_status="failed",
                task_status="failed",
                summary={"task_id": task_id},
                error_message=str(exc),
            )
            self._sync_linked_job(task, status=JOB_REVIEW, stage="review", message=f"Organizer 整理失败：{exc}", level=EVENT_ERROR, error_message=str(exc))
            logger.exception("organizer_apply_failed task_id=%s", task_id)
            return {"success": False, "task_id": task_id, "run_id": run_id, "message": str(exc)}
        finally:
            self.db.release_organizer_locks(run_id=run_id, lock_keys=acquired)

    def _create_organizer_run(self, task_id: int) -> int:
        creator = self.db.create_organizer_run
        try:
            return int(
                creator(
                    task_id,
                    owner_id=getattr(self, "owner_id", "organizer"),
                    lease_seconds=self._organizer_run_lease_seconds(),
                )
            )
        except TypeError:
            try:
                return int(creator(task_id, owner_id=getattr(self, "owner_id", "organizer")))
            except TypeError:
                # 保持轻量测试桩和旧扩展实现兼容；正式 Database 支持租约参数。
                return int(creator(task_id))

    def _claim_organizer_run(self, task_id: int) -> tuple[int | None, dict[str, Any] | None]:
        claimant = getattr(self.db, "claim_organizer_run", None)
        if not callable(claimant):
            # 保持轻量测试桩和旧扩展实现兼容；正式 Database 使用跨进程原子 claim。
            return self._create_organizer_run(task_id), None
        try:
            run_id, active_run = claimant(
                task_id,
                owner_id=getattr(self, "owner_id", "organizer"),
                lease_seconds=self._organizer_run_lease_seconds(),
            )
        except TypeError:
            try:
                run_id, active_run = claimant(task_id, owner_id=getattr(self, "owner_id", "organizer"))
            except TypeError:
                run_id, active_run = claimant(task_id)
        return _safe_positive_int(run_id), active_run if isinstance(active_run, dict) else None

    def _organizer_run_lease_seconds(self) -> int:
        config = getattr(self, "organizer_config", {})
        try:
            value = int(config.get("run_lease_seconds") or 120) if isinstance(config, dict) else 120
        except (TypeError, ValueError):
            value = 120
        return max(30, value)

    def _organizer_run_heartbeat_interval(self) -> float | None:
        config = getattr(self, "organizer_config", {})
        value = config.get("run_lease_heartbeat_seconds") if isinstance(config, dict) else None
        if value in {None, ""}:
            return None
        try:
            return max(0.05, float(value))
        except (TypeError, ValueError):
            return None

    def _update_organizer_run(self, run_id: int, status: str, **values: Any) -> None:
        updater = self.db.update_organizer_run
        try:
            updated = updater(
                run_id,
                status,
                owner_id=getattr(self, "owner_id", "organizer"),
                **values,
            )
        except TypeError:
            updated = updater(run_id, status, **values)
        if updated is False:
            raise OrganizerRunLeaseLost("Organizer 运行租约已失效，拒绝写入终态")

    def _finalize_organizer_run_and_task(
        self,
        run_id: int,
        task_id: int,
        *,
        run_status: str,
        task_status: str,
        summary: Any = None,
        undo_data: Any = None,
        error_message: str = "",
        evidence: Any = None,
        raw_data: Any = None,
    ) -> None:
        finalizer = getattr(self.db, "finalize_organizer_run_and_task", None)
        if callable(finalizer):
            values = {
                "owner_id": getattr(self, "owner_id", "organizer"),
                "run_status": run_status,
                "task_status": task_status,
                "summary": summary,
                "undo_data": undo_data,
                "error_message": error_message,
                "evidence": evidence,
                "raw_data": raw_data,
            }
            try:
                updated = finalizer(run_id, task_id, **values)
            except TypeError:
                values.pop("raw_data", None)
                updated = finalizer(run_id, task_id, **values)
            if updated is False:
                raise OrganizerRunLeaseLost("Organizer 运行租约已失效，拒绝写入任务终态")
            return
        self._update_organizer_run(
            run_id,
            run_status,
            summary=summary,
            undo_data=undo_data,
            error_message=error_message,
        )
        task_values: dict[str, Any] = {"status": task_status, "error_message": error_message}
        if evidence is not None:
            task_values["evidence"] = evidence
        if raw_data is not None:
            task_values["raw_data"] = raw_data
        latest = self.db.get_organizer_task(task_id, include_children=False) or {}
        if not self._update_organizer_task_from_snapshot(latest, **task_values):
            raise OrganizerRunLeaseLost("Organizer 任务状态已变化，拒绝写入任务终态")

    def _finish_organizer_run_failure(self, run_id: int | None, task_id: int, message: str) -> None:
        run_id = _safe_positive_int(run_id)
        if not run_id:
            return
        try:
            self._update_organizer_run(
                run_id,
                "failed",
                summary={"task_id": task_id},
                error_message=str(message or "Organizer 后台执行未完成"),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "finish organizer run failure failed task_id=%s run_id=%s",
                task_id,
                run_id,
                exc_info=True,
            )

    def _acquire_organizer_locks(
        self,
        task: dict[str, Any],
        run_id: int,
        lock_keys: list[str],
        *,
        lease: OrganizerRunLease | None = None,
    ) -> list[str]:
        """等待其它 Organizer 任务释放冲突路径。

        同一分类的大批量整理可能持续数分钟。这是正常串行场景，不应将后到任务
        立即标记为失败。每次重试前会释放本轮已取得的部分锁，避免锁顺序导致死锁。
        """

        task_id = int(task.get("id") or 0)
        wait_value = self.organizer_config.get("lock_wait_seconds", 900)
        poll_value = self.organizer_config.get("lock_poll_seconds", 2)
        wait_seconds = max(0.0, float(900 if wait_value is None else wait_value))
        poll_seconds = max(0.05, float(2 if poll_value is None else poll_value))
        deadline = time.monotonic() + wait_seconds
        waiting_key = ""
        waiting_logged = False

        while True:
            if lease is not None:
                lease.ensure_owned()
            acquired: list[str] = []
            waiting_key = ""
            for key in lock_keys:
                if self.db.acquire_organizer_lock(
                    key,
                    task_id=task_id,
                    run_id=run_id,
                    owner=getattr(self, "owner_id", "organizer"),
                ):
                    acquired.append(key)
                    continue
                waiting_key = key
                break
            if not waiting_key:
                if waiting_logged:
                    logger.info("organizer_lock_acquired_after_wait task_id=%s run_id=%s", task_id, run_id)
                return acquired

            if acquired:
                self.db.release_organizer_locks(run_id=run_id, lock_keys=acquired)
            if lease is not None:
                lease.ensure_owned()
            if time.monotonic() >= deadline:
                raise OrganizerLockTimeout(f"等待其它标准化任务释放路径超时：{waiting_key}")
            if not waiting_logged:
                message = f"路径正由其它标准化任务处理，当前任务已排队等待：{waiting_key}"
                current_task = self._ensure_task_active(task_id, allowed_statuses={"executing"})
                if not self._update_organizer_task_from_snapshot(
                    current_task,
                    status="executing",
                    error_message=message,
                ):
                    raise OrganizerTaskCancelled("Organizer 任务状态已变化，停止等待路径锁")
                self._sync_linked_job(current_task, status=JOB_ORGANIZING, stage="organizing", message=message)
                logger.info("organizer_lock_wait task_id=%s run_id=%s key=%s", task_id, run_id, waiting_key)
                waiting_logged = True
            time.sleep(min(poll_seconds, max(0.05, deadline - time.monotonic())))

    def _confirm_standardized_targets(self, task: dict[str, Any]) -> dict[str, Any]:
        ready_mappings = [m for m in task.get("mappings") or [] if str(m.get("status") or "") in {"ready", "skipped_existing"}]
        targets = _dedupe_texts(str(item.get("target_path") or "") for item in ready_mappings if str(item.get("target_path") or "").strip())
        video_targets = [path for path in targets if posixpath.splitext(basename(path))[1].lower() in VIDEO_EXTENSIONS]
        if not video_targets:
            return {"success": False, "message": "没有可确认的视频目标文件", "target_paths": targets}
        confirmed: list[str] = []
        failed: list[dict[str, Any]] = []
        target_dirs = _dedupe_texts(dirname(path) for path in video_targets)
        listed_by_dir: dict[str, set[str]] = {}
        for directory in target_dirs:
            try:
                listed_by_dir[directory] = {item.name for item in self.openlist.list_dir(directory, refresh=True)}
            except Exception as exc:  # noqa: BLE001
                failed.append({"path": directory, "type": "list_dir", "message": str(exc)})
                listed_by_dir[directory] = set()
        for target in video_targets:
            parent = dirname(target)
            name = basename(target)
            if name in listed_by_dir.get(parent, set()):
                confirmed.append(target)
                continue
            if self.openlist.exists(target):
                confirmed.append(target)
            else:
                failed.append({"path": target, "type": "target_exists", "message": "标准目标文件不存在"})
        success = bool(confirmed) and not [item for item in failed if item.get("type") == "target_exists"]
        organized_target_path = _common_parent_dir(confirmed) if confirmed else ""
        return {
            "success": success,
            "message": f"标准目录确认成功：{len(confirmed)} 个视频文件" if success else "标准目录复扫失败，未确认到全部目标视频文件",
            "confirmed_count": len(confirmed),
            "expected_count": len(video_targets),
            "confirmed_paths": confirmed,
            "failed": failed[:20],
            "target_dirs": target_dirs,
            "organized_target_path": organized_target_path,
        }

    def rollback_run(self, run_id: int) -> dict[str, Any]:
        wanted_run_id = _safe_positive_int(run_id)
        matched_run: dict[str, Any] | None = None
        offset = 0
        while wanted_run_id:
            legacy_pagination = False
            try:
                page = self.db.list_organizer_runs(limit=500, offset=offset)
            except TypeError:
                page = self.db.list_organizer_runs(limit=500)
                legacy_pagination = True
            matched_run = next(
                (run for run in page if _safe_positive_int(run.get("id")) == wanted_run_id),
                None,
            )
            if matched_run or len(page) < 500 or legacy_pagination:
                break
            offset += len(page)
        if not matched_run:
            return {"success": False, "message": "运行记录不存在"}
        undo_items = matched_run.get("undo_data") or []
        done = 0
        errors = []
        for op in undo_items:
            try:
                self._execute_inverse(op)
                done += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                break
        return {"success": not errors, "done": done, "errors": errors}

    def _build_plan(
        self,
        task: dict[str, Any],
        category_key: str,
        category: dict[str, Any],
        videos: list[Any],
        *,
        companion_files: list[Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        target_category = self._target_category_for_task(task, category)
        staging_task = self._task_has_staging_plan(task)
        root_name = basename(task.get("openlist_root_path") or "")
        parent_name = basename(dirname(task.get("openlist_root_path") or ""))
        names = [item.name for item in videos]
        parsed = [parse_file_name(item.name, current_dir=basename(dirname(item.path)), parent_dir=root_name, siblings=names) for item in videos]
        has_episodes = any(item.episode is not None for item in parsed)
        inferred_media_type = "movie" if category_key == "movie" else ("tv" if category_key in EPISODIC_CATEGORIES or has_episodes or len(videos) > 1 else "movie")
        context_title = _task_context_title(task)
        title_candidates = _lookup_title_candidates(
            context_title,
            task.get("source_keyword"),
            task.get("title"),
            root_name,
            parent_name,
            parsed[0].title if parsed else "",
            *(names[:8]),
        )
        title_candidate, year_candidate = _first_title_year(title_candidates)
        title_candidate = title_candidate or _clean_title_hint(context_title) or evidence_title(task.get("source_keyword"), task.get("title"), root_name, parsed[0].title if parsed else "")
        title_candidate, title_season = extract_season_from_title(title_candidate)
        year_candidate = year_candidate or next((item.year for item in parsed if item.year), "")
        parsed_season_hint = next((item.season for item in parsed if item.season is not None), None)
        root_base, root_season = extract_season_from_title(root_name)
        task_title_base, task_title_season = extract_season_from_title(task.get("title") or "")
        source_base, source_season = extract_season_from_title(task.get("source_keyword") or "")
        season_hint = _first_not_none(parsed_season_hint, root_season, title_season, task_title_season, source_season)
        confidence = max([item.confidence for item in parsed] or [40])
        tmdb_item = self._match_tmdb(
            task,
            title_candidate,
            year_candidate,
            category_key,
            inferred_media_type,
            has_episodes,
            extra_queries=[item.get("query") for item in title_candidates],
        )
        ai_suggestion: dict[str, Any] = {}
        ai_trace: dict[str, Any] = {"configured": self.ai.configured, "attempted": False, "used": False, "reason": ""}
        if (not tmdb_item or confidence < 75) and self.ai.configured:
            evidence = {
                "category": category_key,
                "root_dir": root_name,
                "parent_dir": parent_name,
                "title_candidates": [item.get("query") for item in title_candidates],
                "parsed_candidates": {
                    "base_title": _dedupe_texts([title_candidate, root_base, task_title_base, source_base, *(item.title for item in parsed)]),
                    "year": _dedupe_texts([year_candidate, *(item.year for item in parsed)]),
                    "season": _dedupe_texts([season_hint, *(item.season for item in parsed)]),
                    "episodes": _dedupe_texts(item.episode for item in parsed if item.episode is not None),
                },
                "raw_title_candidates": [task.get("source_keyword"), task.get("title"), root_name, parsed[0].title if parsed else ""],
                "files": [{"name": item.name, "path": item.path, "season": parsed_item.season, "episode": parsed_item.episode, "version": parsed_item.version} for item, parsed_item in list(zip(videos, parsed))[:80]],
                "tmdb_best": tmdb_item,
            }
            ai_trace["attempted"] = True
            try:
                ai_suggestion = self.ai.calibrate(evidence)
                ai_trace["used"] = bool(ai_suggestion)
                ai_trace["reason"] = "规则/TMDB 置信度不足，已调用 AI 校准"
                self.db.add_organizer_ai_suggestion(int(task["id"]), "openai-compatible", self.ai.model, evidence, {}, ai_suggestion)
            except Exception as exc:  # noqa: BLE001
                ai_suggestion = {"error": str(exc)}
                ai_trace["reason"] = f"AI 校准失败：{exc}"
                self.db.add_organizer_ai_suggestion(int(task["id"]), "openai-compatible", self.ai.model, evidence, {}, ai_suggestion)
        elif not self.ai.configured:
            ai_trace["reason"] = "AI 未启用或配置不完整，未调用"
        else:
            ai_trace["reason"] = "规则/TMDB 置信度已达标，未调用 AI"
        ai_title_raw = str(ai_suggestion.get("title") or "").strip()
        ai_title, ai_title_season = extract_season_from_title(ai_title_raw)
        ai_year = str(ai_suggestion.get("year") or "").strip()
        ai_season = _first_not_none(_optional_non_negative_int(ai_suggestion.get("season")), ai_title_season)
        season_hint = _first_not_none(season_hint, ai_season)
        if (not tmdb_item or score_tmdb_result(title_candidate, year_candidate, category_key, tmdb_item, has_episodes) < 85) and ai_title:
            ai_tmdb_item = self._match_tmdb(
                task,
                ai_title,
                ai_year or year_candidate,
                category_key,
                str(ai_suggestion.get("media_type") or inferred_media_type),
                has_episodes,
                extra_queries=[*(ai_suggestion.get("tmdb_queries") or []), ai_title, f"{ai_title} {ai_year}".strip()],
            )
            if ai_tmdb_item:
                tmdb_item = ai_tmdb_item
        title = str((tmdb_item or {}).get("title") or ai_title or title_candidate or task_title_base or source_base or root_name).strip()
        year = str((tmdb_item or {}).get("year") or ai_suggestion.get("year") or year_candidate or "").strip()
        media_type = str((tmdb_item or {}).get("media_type") or ai_suggestion.get("media_type") or inferred_media_type)
        if media_type not in {"movie", "tv"}:
            media_type = inferred_media_type
        if category_key == "movie":
            # 用户明确按电影目录整理时，AI/TMDB 或文件名里的编码数字不能把任务反推成剧集。
            # 例如 “H.265” 曾被误识别成 E265，导致电影落到 Season 01。
            media_type = "movie"
        if tmdb_item:
            confidence = max(confidence, score_tmdb_result(title_candidate, year_candidate, category_key, tmdb_item, has_episodes))
        if ai_suggestion.get("confidence"):
            try:
                confidence = max(confidence, _confidence_percent(ai_suggestion["confidence"]))
            except (TypeError, ValueError):
                pass
        files: list[dict[str, Any]] = []
        mappings: list[dict[str, Any]] = []
        target_index: dict[str, int] = {}
        episode_groups: dict[tuple[int | None, int | None], int] = {}
        for parsed_item in parsed:
            if parsed_item.episode is None:
                continue
            group_key = (_first_not_none(parsed_item.season, season_hint, 1 if media_type == "tv" else None), parsed_item.episode)
            episode_groups[group_key] = episode_groups.get(group_key, 0) + 1
        for item, parsed_item in zip(videos, parsed):
            ext = posixpath.splitext(item.name)[1]
            season = _first_not_none(parsed_item.season, season_hint, 1 if media_type == "tv" else None)
            episode = parsed_item.episode
            version = _mapping_version(item.path, parsed_item.version)
            if _should_auto_delete_ad_file(category_key, media_type, parsed_item, item.size):
                reason = [
                    *parsed_item.reasons,
                    f"未识别到剧集且文件小于 50MB（{_size_text(item.size)}），判定为广告小文件，自动彻底删除",
                ]
                files.append(
                    {
                        "path": item.path,
                        "name": item.name,
                        "parent_path": dirname(item.path),
                        "ext": ext,
                        "size": item.size,
                        "season": season,
                        "episode": episode,
                        "raw_data": item.raw,
                    }
                )
                mappings.append(
                    {
                        "source_path": item.path,
                        "source_name": item.name,
                        "target_path": item.path,
                        "target_name": item.name,
                        "media_type": media_type,
                        "title": title,
                        "year": year,
                        "season": season,
                        "episode": episode,
                        "tmdb_id": (tmdb_item or {}).get("id"),
                        "confidence": round(confidence, 2),
                        "status": "delete_ad",
                        "reason": reason,
                        "raw_data": {"parsed": parsed_item.__dict__, "tmdb": tmdb_item, "ai": ai_suggestion, "season_hint": season_hint, "auto_delete_ad": True},
                    }
                )
                continue
            use_version = bool(episode is not None and episode_groups.get((season, episode), 0) > 1 and version)
            target = standard_target_path(
                category_key="movie" if media_type == "movie" else "tv",
                category=target_category,
                title=title,
                year=year,
                season=season,
                episode=episode,
                ext=ext,
                version=version if use_version else "",
            )
            reason = list(parsed_item.reasons)
            if use_version:
                reason.append(f"同集多版本，追加版本后缀：{version}")
            status = "ready"
            if media_type == "tv" and episode is None:
                status = "need_edit"
                reason.append("未识别到集数，需要人工确认")
            if target in target_index:
                status = "conflict"
                reason.append("多个源文件指向同一目标路径")
            target_index[target] = target_index.get(target, 0) + 1
            if target_index[target] > 1 and version and not use_version:
                target = standard_target_path(category_key="tv", category=target_category, title=title, year=year, season=season, episode=episode, ext=ext, version=version)
                status = "ready" if target not in target_index else "conflict"
                reason.append("多版本文件追加版本后缀")
                target_index[target] = target_index.get(target, 0) + 1
            if self.openlist.exists(target) and normalize_path(item.path) != normalize_path(target):
                if self._update_target_already_exists_ok(task, season, episode) and not staging_task:
                    status = "skipped_existing"
                    reason.append("目标路径已存在，定时追更单集视为已在标准目录，不覆盖也不要求人工确认")
                else:
                    status = "conflict"
                    if staging_task:
                        reason.append("任务级暂存源文件仍存在且目标路径已存在，拒绝自动删除或覆盖")
                    else:
                        reason.append("目标路径已存在，不覆盖")
            files.append(
                {
                    "path": item.path,
                    "name": item.name,
                    "parent_path": dirname(item.path),
                    "ext": ext,
                    "size": item.size,
                    "season": season,
                    "episode": episode,
                    "raw_data": item.raw,
                }
            )
            mappings.append(
                {
                    "source_path": item.path,
                    "source_name": item.name,
                    "target_path": target,
                    "target_name": basename(target),
                    "media_type": media_type,
                    "title": title,
                    "year": year,
                    "season": season,
                    "episode": episode,
                    "tmdb_id": (tmdb_item or {}).get("id"),
                    "confidence": round(confidence, 2),
                    "status": status,
                    "reason": reason,
                    "raw_data": {
                        "parsed": parsed_item.__dict__,
                        "tmdb": tmdb_item,
                        "ai": ai_suggestion,
                        "season_hint": season_hint,
                        "staging_file": staging_task,
                    },
                }
            )
        companion_plan_files, companion_mappings = self._build_companion_mappings(
            task,
            companion_files or [],
            mappings,
            target_index=target_index,
            title=title,
            year=year,
            media_type=media_type,
            tmdb_id=(tmdb_item or {}).get("id"),
            confidence=confidence,
        )
        files.extend(companion_plan_files)
        mappings.extend(companion_mappings)
        operations = self._operations_for_mappings(mappings, target_category)
        blocked = [item for item in mappings if item["status"] in {"conflict", "need_edit"}]
        auto_delete_count = len([item for item in mappings if item["status"] == "delete_ad"])
        episode_completeness = _episode_completeness_report(category_key, mappings)
        evidence = {
            "title": title,
            "year": year,
            "season": season_hint,
            "title_candidates": title_candidates,
            "ai": ai_suggestion,
            "ai_trace": ai_trace,
            "tmdb": tmdb_item,
            "tmdb_trace": {"configured": self.tmdb.configured, "query_rule": "仅向 TMDB 传清洗后的基础片名，不携带 4K/1080P/编码/字幕等垃圾词"},
            "auto_delete_rule": {
                "enabled": True,
                "threshold_bytes": AD_FILE_DELETE_THRESHOLD_BYTES,
                "message": "剧集类整理中，未识别集数且小于 50MB 的视频/压缩包判定为广告并自动彻底删除",
                "count": auto_delete_count,
            },
            "problem_summary": _mapping_problem_summary(mappings, confidence, ai_suggestion),
        }
        if episode_completeness:
            evidence["episode_completeness"] = episode_completeness
        summary = {
            "confidence": round(confidence, 2),
            "media_type": media_type,
            "tmdb_id": (tmdb_item or {}).get("id"),
            "tmdb_title": (tmdb_item or {}).get("title") or "",
            "tmdb_year": (tmdb_item or {}).get("year") or "",
            "auto_applicable": confidence >= float(self.organizer_config.get("auto_apply_confidence") or 85) and not blocked and not bool(ai_suggestion.get("requires_review")),
            "blocked_count": len(blocked),
            "auto_delete_count": auto_delete_count,
            "companion_file_count": len(companion_plan_files),
            "file_count": len(files),
            "operation_count": len(operations),
            "problem_summary": _mapping_problem_summary(mappings, confidence, ai_suggestion),
            "evidence": evidence,
        }
        if episode_completeness:
            summary["episode_completeness"] = episode_completeness
        return files, mappings, operations, summary

    def _build_companion_mappings(
        self,
        task: dict[str, Any],
        companion_files: list[Any],
        video_mappings: list[dict[str, Any]],
        *,
        target_index: dict[str, int],
        title: str,
        year: str,
        media_type: str,
        tmdb_id: Any,
        confidence: float,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not companion_files:
            return [], []
        task_root = normalize_path(task.get("openlist_root_path") or "")
        usable_videos = [
            mapping
            for mapping in video_mappings
            if str(mapping.get("status") or "") in {"ready", "skipped_existing"}
            and posixpath.splitext(str(mapping.get("source_path") or ""))[1].lower() in VIDEO_EXTENSIONS
        ]
        files: list[dict[str, Any]] = []
        mappings: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        for item in companion_files:
            source_path = normalize_path(getattr(item, "path", ""))
            source_key = source_path.casefold()
            if not source_path or source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            source_name = str(getattr(item, "name", "") or basename(source_path)).strip()
            if not _is_supported_companion_name(source_name):
                continue
            target_dir, related_videos = self._companion_target_directory(
                source_path,
                usable_videos,
                task_root=task_root,
            )
            reason = ["任务级暂存附件随关联视频移动到最终目录"]
            status = "ready"
            season = None
            episode = None
            target_name = source_name
            if not target_dir:
                status = "need_edit"
                target_path = source_path
                reason.append("无法唯一判断附件对应的最终影视目录，需要人工确认")
            else:
                matched_video = _matching_video_for_companion(source_name, related_videos)
                if not _companion_is_safely_related(source_name, matched_video):
                    continue
                if matched_video:
                    # 同集字幕/NFO/缩略图必须与标准视频落在同一最终目录。
                    # 文件直接位于 job 根时，目录级附件默认会映射到资源根；
                    # 一旦已精确关联到某个视频，应以该视频的目标目录覆盖，
                    # 否则剧集字幕会被放到 Season 目录的上一层而无法被播放器识别。
                    target_dir = dirname(str(matched_video.get("target_path") or ""))
                    target_name = _companion_target_name(
                        source_name,
                        str(matched_video.get("source_name") or basename(matched_video.get("source_path") or "")),
                        str(matched_video.get("target_name") or basename(matched_video.get("target_path") or "")),
                    )
                    season = matched_video.get("season")
                    episode = matched_video.get("episode")
                target_path = join_path(target_dir, target_name)
                if target_index.get(target_path):
                    status = "conflict"
                    reason.append("多个源文件指向同一附件目标路径")
                target_index[target_path] = target_index.get(target_path, 0) + 1
                if self.openlist.exists(target_path) and source_path != normalize_path(target_path):
                    status = "conflict"
                    reason.append("附件目标路径已存在，不覆盖")
            files.append(
                {
                    "path": source_path,
                    "name": source_name,
                    "parent_path": dirname(source_path),
                    "ext": posixpath.splitext(source_name)[1],
                    "size": int(getattr(item, "size", 0) or 0),
                    "season": season,
                    "episode": episode,
                    "raw_data": getattr(item, "raw", {}) or {},
                }
            )
            mappings.append(
                {
                    "source_path": source_path,
                    "source_name": source_name,
                    "target_path": target_path,
                    "target_name": basename(target_path),
                    "media_type": media_type,
                    "title": title,
                    "year": year,
                    "season": season,
                    "episode": episode,
                    "tmdb_id": tmdb_id,
                    "confidence": round(confidence, 2),
                    "status": status,
                    "reason": reason,
                    "raw_data": {"companion_file": True, "staging_file": True},
                }
            )
        return files, mappings

    @staticmethod
    def _companion_target_directory(
        source_path: str,
        video_mappings: list[dict[str, Any]],
        *,
        task_root: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        source_dir = dirname(source_path)
        candidates: list[tuple[str, dict[str, Any]]] = []
        for mapping in video_mappings:
            video_source_dir = dirname(str(mapping.get("source_path") or ""))
            video_target_dir = dirname(str(mapping.get("target_path") or ""))
            if not video_source_dir or not video_target_dir:
                continue
            if source_dir == task_root:
                candidate = _resource_dir_from_video_target(video_target_dir)
            elif source_dir.casefold() == video_source_dir.casefold():
                candidate = video_target_dir
            elif _path_is_same_or_child(video_source_dir, source_dir):
                candidate = video_target_dir
                for _part in _relative_virtual_parts(video_source_dir, source_dir):
                    candidate = dirname(candidate)
            elif _path_is_same_or_child(source_dir, video_source_dir):
                candidate = video_target_dir
                relative = _relative_virtual_parts(source_dir, video_source_dir)
                if relative:
                    candidate = join_path(candidate, *relative)
            else:
                continue
            candidates.append((normalize_path(candidate), mapping))
        target_dirs = _dedupe_texts(candidate for candidate, _mapping in candidates if candidate)
        if len(target_dirs) != 1:
            return "", []
        target_dir = target_dirs[0]
        related = [mapping for candidate, mapping in candidates if candidate.casefold() == target_dir.casefold()]
        return target_dir, related

    def _apply_auto_delete_operations(self, task_id: int, *, expected_status: str = "") -> dict[str, Any]:
        task = self.db.get_organizer_task(task_id) or {}
        operations = [op for op in task.get("operations") or [] if str(op.get("type") or "") == "delete_file" and str(op.get("status") or "pending") == "pending"]
        if not operations:
            return {}
        done = 0
        skipped = 0
        failed = 0
        errors: list[str] = []
        for op in operations:
            op_id = int(op.get("id") or 0)
            try:
                self._ensure_task_active(
                    task_id,
                    allowed_statuses={expected_status} if expected_status else None,
                )
                self._execute_operation(op)
                if op_id:
                    self.db.update_organizer_operation(op_id, status="done")
                done += 1
            except SkipOperation as exc:
                if op_id:
                    self.db.update_organizer_operation(op_id, status="skipped", error_message=str(exc))
                skipped += 1
            except Exception as exc:  # noqa: BLE001
                if op_id:
                    self.db.update_organizer_operation(op_id, status="failed", error_message=str(exc))
                failed += 1
                errors.append(str(exc))
        return {"done": done, "skipped": skipped, "failed": failed, "errors": errors[:5]}

    def _cleanup_source_empty_dirs_after_apply(self, task: dict[str, Any]) -> dict[str, Any]:
        category = self.categories.get(str(task.get("category") or ""), {})
        target_category = self._target_category_for_task(task, category)
        category_root = normalize_path(
            target_category.get("source_category_root_path")
            or category_target_root(category)
        )
        candidates: list[str] = []
        task_root = normalize_path(task.get("openlist_root_path") or "")
        if self._safe_cleanup_dir(task_root, category_root):
            candidates.append(task_root)
        for mapping in task.get("mappings") or []:
            if str(mapping.get("status") or "") not in {"ready", "skipped_existing"}:
                continue
            source = str(mapping.get("source_path") or "")
            target = str(mapping.get("target_path") or "")
            if not source or normalize_path(source) == normalize_path(target):
                continue
            for item in self._source_cleanup_dirs(source, category_root):
                if item not in candidates and self._safe_cleanup_dir(item, category_root):
                    candidates.append(item)
        if not candidates:
            return {}
        candidates.sort(key=lambda item: item.count("/"), reverse=True)
        items = [self._remove_empty_dir_with_retry(path) for path in candidates]
        removed = len([item for item in items if item.get("success")])
        failed = len([item for item in items if item.get("failed")])
        skipped = len(items) - removed - failed
        return {
            "enabled": True,
            "count": len(items),
            "removed": removed,
            "skipped": skipped,
            "failed": failed,
            "items": items,
            "message": f"真实旧空目录清理完成：删除 {removed} 个，跳过 {skipped} 个，失败 {failed} 个",
        }

    @staticmethod
    def _safe_cleanup_dir(path: str, category_root: str) -> bool:
        normalized = normalize_path(path)
        root = normalize_path(category_root)
        if not normalized or normalized == "/" or normalized == root:
            return False
        if root == "/":
            return False
        return _path_is_same_or_child(normalized, root)

    def _remove_empty_dir_with_retry(self, path: str, *, attempts: int = 6, delay: float = 0.8) -> dict[str, Any]:
        normalized = normalize_path(path)
        last_error = ""
        for attempt in range(max(1, attempts)):
            try:
                if self.openlist.list_dir(normalized, refresh=True):
                    last_error = "目录非空，跳过删除"
                    if attempt < attempts - 1:
                        time.sleep(delay)
                        continue
                    return {"success": False, "skipped": True, "path": normalized, "message": last_error}
                self.openlist.remove_empty_directory(normalized)
                return {"success": True, "path": normalized, "message": "已删除空目录"}
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if attempt < attempts - 1:
                    time.sleep(delay)
                    continue
        return {"success": False, "failed": True, "path": normalized, "message": last_error or "删除失败"}

    @staticmethod
    def _pending_strm_completion(task: dict[str, Any]) -> dict[str, Any]:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        state = raw_data.get("strm_completion") if isinstance(raw_data.get("strm_completion"), dict) else {}
        return dict(state) if str(state.get("status") or "") == "pending" else {}

    def _strm_confirmation_delays(self) -> list[int]:
        configured = self.organizer_config.get("strm_confirm_retry_delays_seconds")
        if not isinstance(configured, (list, tuple)):
            configured = [5, 15, 30, 60, 120, 300]
        result: list[int] = []
        for value in configured:
            try:
                delay = max(1, min(3600, int(value)))
            except (TypeError, ValueError):
                continue
            if delay not in result:
                result.append(delay)
        return result or [5, 15, 30, 60, 120, 300]

    def _start_strm_completion(
        self,
        task_id: int,
        run_id: int,
        task: dict[str, Any],
        lease: OrganizerRunLease,
        *,
        summary: dict[str, Any],
        undo: list[dict[str, Any]],
        task_evidence: dict[str, Any],
        confirmation: dict[str, Any],
    ) -> dict[str, Any]:
        lease.ensure_owned()
        strm_refresh = self._refresh_openlist_strm_for_task(task_id, task)
        self._ensure_task_active(task_id, allowed_statuses={"executing"})
        lease.ensure_owned()
        if not strm_refresh:
            return self._finish_strm_completion(
                task_id,
                run_id,
                task,
                lease,
                state={},
                summary=summary,
                undo=undo,
                task_evidence=task_evidence,
                confirmation=confirmation,
                cleanup_enabled=False,
            )

        summary = {**summary, "strm_refresh": strm_refresh}
        task_evidence = {**task_evidence, "strm_refresh": strm_refresh}
        if _safe_positive_int(strm_refresh.get("failed")):
            failed_items = [
                item
                for item in (strm_refresh.get("items") or [])
                if isinstance(item, dict) and not item.get("success")
            ]
            detail = str((failed_items[0] if failed_items else {}).get("message") or "").strip()
            message = "最终文件已确认，但 OpenList 文件夹刷新失败"
            if detail:
                message = f"{message}：{detail[:300]}"
            return self._fail_strm_completion(
                task_id,
                run_id,
                task,
                lease,
                message=message,
                summary=summary,
                undo=undo,
                task_evidence=task_evidence,
                confirmation=confirmation,
                state={"status": "refresh_failed", "refresh": strm_refresh},
                retryable=True,
            )

        state = {
            "version": 2,
            "status": "refresh_accepted",
            "requested_at": str(strm_refresh.get("requested_at") or _utc_now_text()),
            "refresh_prefix": str(strm_refresh.get("refresh_prefix") or ""),
            "resource_names": [
                str(item or "").strip()
                for item in (strm_refresh.get("resource_names") or [])
                if str(item or "").strip()
            ],
            "refresh_paths": [
                str(item or "").strip()
                for item in (strm_refresh.get("refresh_paths") or [])
                if str(item or "").strip()
            ],
            "refresh": strm_refresh,
            "confirmation_required": False,
            "handled_by": "openlist",
        }
        return self._finish_strm_completion(
            task_id,
            run_id,
            task,
            lease,
            state=state,
            summary=summary,
            undo=undo,
            task_evidence=task_evidence,
            confirmation=confirmation,
            cleanup_enabled=False,
        )

    def _resume_strm_completion(
        self,
        task_id: int,
        run_id: int,
        task: dict[str, Any],
        state: dict[str, Any],
        lease: OrganizerRunLease,
    ) -> dict[str, Any]:
        lease.ensure_owned()
        self._ensure_task_active(task_id, allowed_statuses={"executing"})
        summary = state.get("summary") if isinstance(state.get("summary"), dict) else {"task_id": task_id}
        undo = state.get("undo") if isinstance(state.get("undo"), list) else []
        confirmation = state.get("confirmation") if isinstance(state.get("confirmation"), dict) else {}
        task_evidence = state.get("task_evidence") if isinstance(state.get("task_evidence"), dict) else {}
        accepted_state = {
            **state,
            "version": 2,
            "status": "refresh_accepted",
            "confirmation_required": False,
            "handled_by": "openlist",
            "legacy_pending_reconciled": True,
        }
        return self._finish_strm_completion(
            task_id,
            run_id,
            task,
            lease,
            state=accepted_state,
            summary=summary,
            undo=undo,
            task_evidence=task_evidence,
            confirmation=confirmation,
            cleanup_enabled=False,
        )

    def _defer_strm_completion(
        self,
        task_id: int,
        run_id: int,
        task: dict[str, Any],
        lease: OrganizerRunLease,
        state: dict[str, Any],
        *,
        delay_override: int | None = None,
        increment_attempt: bool = True,
    ) -> dict[str, Any]:
        lease.ensure_owned()
        delays = state.get("delays_seconds") if isinstance(state.get("delays_seconds"), list) else self._strm_confirmation_delays()
        attempts = max(0, int(state.get("attempts") or 0))
        delay_index = min(attempts, max(0, len(delays) - 1))
        delay = max(1, int(delay_override or delays[delay_index]))
        next_attempts = attempts + 1 if increment_attempt else attempts
        next_retry_at = _utc_after_seconds(delay)
        pending = {
            **state,
            "status": "pending",
            "attempts": next_attempts,
            "max_attempts": max(1, int(state.get("max_attempts") or len(delays))),
            "delays_seconds": delays,
            "next_retry_at": next_retry_at,
            "last_check_at": str(state.get("last_check_at") or _utc_now_text()),
        }
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        raw_data = {**raw_data, "strm_completion": pending}
        task_evidence = pending.get("task_evidence") if isinstance(pending.get("task_evidence"), dict) else {}
        evidence = {**task_evidence, "strm_completion": pending}
        summary = pending.get("summary") if isinstance(pending.get("summary"), dict) else {"task_id": task_id}
        undo = pending.get("undo") if isinstance(pending.get("undo"), list) else []
        self._finalize_organizer_run_and_task(
            run_id,
            task_id,
            run_status="deferred",
            task_status="strm_pending",
            summary=summary,
            undo_data=undo,
            evidence=evidence,
            raw_data=raw_data,
        )
        message = f"真实媒体已整理，等待 OpenList 生成并同步 STRM；{delay} 秒后复查"
        self._sync_linked_job(
            task,
            status=JOB_CONFIRMING,
            stage="strm_pending",
            message=message,
            extra={"strm_completion": {"attempts": next_attempts, "next_retry_at": next_retry_at}},
        )
        worker_context = getattr(self, "_worker_context", None)
        if not bool(getattr(worker_context, "active", False)):
            if self._schedule_task_after(task_id, delay) is False:
                failure_message = "新 STRM 尚未可见，且当前进程无法安排后续复查；旧 STRM 已保留，请手动重试"
                self.db.update_organizer_task(
                    task_id,
                    status="waiting_review",
                    error_message=failure_message,
                    expected_statuses={"strm_pending"},
                )
                self._sync_linked_job(
                    task,
                    status=JOB_REVIEW,
                    stage="review",
                    message=failure_message,
                    level=EVENT_WARN,
                    error_message=failure_message,
                )
                return {"success": False, "task_id": task_id, "run_id": run_id, "message": failure_message}
        return {
            "success": True,
            "deferred": True,
            "queued": True,
            "retry_after_seconds": delay,
            "delay_seconds": delay,
            "task_id": task_id,
            "run_id": run_id,
            "status": "strm_pending",
            "next_retry_at": next_retry_at,
            "message": message,
        }

    def _finish_strm_completion(
        self,
        task_id: int,
        run_id: int,
        task: dict[str, Any],
        lease: OrganizerRunLease,
        *,
        state: dict[str, Any],
        summary: dict[str, Any],
        undo: list[dict[str, Any]],
        task_evidence: dict[str, Any],
        confirmation: dict[str, Any],
        cleanup_enabled: bool,
    ) -> dict[str, Any]:
        lease.ensure_owned()
        # ``cleanup_enabled`` 仅为兼容旧调用签名保留。Organizer 不再检查或
        # 清理 STRM，避免把 OpenList 自己的异步工作重新变成入库阻塞条件。
        cleanup: dict[str, Any] = {}
        if cleanup_enabled:
            cleanup = {
                "enabled": False,
                "skipped": True,
                "message": "STRM 由 OpenList 后台维护，Organizer 不执行旧 STRM 清理",
            }

        completed_state = {
            **state,
            "version": 2,
            "status": "refresh_accepted",
            "completed_at": _utc_now_text(),
            "confirmation_required": False,
            "handled_by": "openlist",
            "cleanup": cleanup,
        } if state else {}
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        if completed_state:
            raw_data = {**raw_data, "strm_completion": completed_state}
            task_evidence = {**task_evidence, "strm_completion": completed_state}
            summary = {
                **summary,
                "strm_completion": completed_state,
                "openlist_refresh_completion": completed_state,
            }
        self._finalize_organizer_run_and_task(
            run_id,
            task_id,
            run_status="done",
            task_status="done",
            summary=summary,
            undo_data=undo,
            evidence=task_evidence,
            raw_data=raw_data,
        )
        self._sync_linked_job(
            task,
            status=JOB_DONE,
            stage="done",
            message=(
                "标准目录确认成功，OpenList 文件夹刷新已触发，整理入库完成；STRM 由 OpenList 后台处理"
                if completed_state
                else "标准目录复扫确认成功，完整整理入库完成"
            ),
            extra={
                "confirmation": confirmation,
                "strm_completion": completed_state,
                "organized_target_path": confirmation.get("organized_target_path") or "",
                "target_dirs": confirmation.get("target_dirs") or [],
            },
        )
        fnos_refresh_scheduled = False
        try:
            # 飞牛刷新只由开关决定，并且始终在 Organizer/关联 Job 已写入 done
            # 之后异步安排；调度失败或飞牛接口失败都不能回滚整理完成状态。
            fnos_refresh_scheduled = bool(self._refresh_fnos_if_needed(task_id))
        except Exception:  # noqa: BLE001
            logger.warning(
                "organizer_fnos_refresh_schedule_failed task_id=%s",
                task_id,
                exc_info=True,
            )
        logger.info(
            "organizer_apply_done task_id=%s openlist_refresh_accepted=%s fnos_refresh_scheduled=%s",
            task_id,
            bool(completed_state),
            fnos_refresh_scheduled,
        )
        return {
            "success": True,
            "task_id": task_id,
            "run_id": run_id,
            "status": "done",
            "summary": summary,
            "fnos_refresh_scheduled": fnos_refresh_scheduled,
        }

    def _fail_strm_completion(
        self,
        task_id: int,
        run_id: int,
        task: dict[str, Any],
        lease: OrganizerRunLease,
        *,
        message: str,
        summary: dict[str, Any],
        undo: list[dict[str, Any]],
        task_evidence: dict[str, Any],
        confirmation: dict[str, Any],
        state: dict[str, Any],
        retryable: bool,
    ) -> dict[str, Any]:
        lease.ensure_owned()
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        raw_data = {**raw_data, "strm_completion": state}
        evidence = {**task_evidence, "strm_completion": state}
        self._finalize_organizer_run_and_task(
            run_id,
            task_id,
            run_status="failed",
            task_status="waiting_review",
            summary=summary,
            undo_data=undo,
            error_message=message,
            evidence=evidence,
            raw_data=raw_data,
        )
        self._sync_linked_job(
            task,
            status=JOB_REVIEW,
            stage="review",
            message=message,
            level=EVENT_WARN,
            error_message=message,
            extra={"confirmation": confirmation, "strm_completion": state},
        )
        return {
            "success": False,
            "retryable": retryable,
            "task_id": task_id,
            "run_id": run_id,
            "status": "waiting_review",
            "message": message,
            "summary": summary,
        }

    def _changed_strm_resource_names(self, task: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for mapping in task.get("mappings") or []:
            if str(mapping.get("status") or "") != "ready":
                continue
            source = str(mapping.get("source_path") or "").strip()
            target = str(mapping.get("target_path") or "").strip()
            if not source or not target or normalize_path(source).casefold() == normalize_path(target).casefold():
                continue
            target_dir = dirname(target)
            resource_name = basename(target_dir)
            if resource_name.lower().startswith("season "):
                resource_name = basename(dirname(target_dir))
            if resource_name and resource_name not in result:
                result.append(resource_name)
        return result

    def _capture_strm_targets(
        self,
        task: dict[str, Any],
        resource_names: list[str],
        refresh_paths: list[str],
    ) -> dict[str, Any]:
        local_root_text = str(self.organizer_config.get("local_strm_root") or "").strip()
        local_root = Path(local_root_text).expanduser() if local_root_text else None
        use_local = bool(local_root is not None and local_root.exists())
        category_dir = self._local_strm_category_dir(str(task.get("category") or "").strip().lower())
        targets: dict[str, Any] = {}
        for index, refresh_path in enumerate(refresh_paths):
            name = resource_names[index] if index < len(resource_names) else basename(refresh_path)
            if use_local and local_root is not None:
                try:
                    local_path = self._safe_local_strm_child(str(local_root), category_dir, name)
                    snapshot = self._snapshot_local_strm(local_path)
                except Exception as exc:  # noqa: BLE001
                    snapshot = {"mode": "local", "visible": False, "files": {}, "error": str(exc)}
            else:
                snapshot = self._snapshot_openlist_strm(refresh_path)
            targets[refresh_path] = {"name": name, **snapshot}
        return targets

    @staticmethod
    def _snapshot_local_strm(path: Path, *, limit: int = 500) -> dict[str, Any]:
        files: dict[str, Any] = {}
        try:
            if path.is_file() and path.suffix.lower() == ".strm":
                candidates = [path]
                base = path.parent
            elif path.is_dir():
                candidates = sorted((item for item in path.rglob("*.strm") if item.is_file()), key=lambda item: str(item))[:limit]
                base = path
            else:
                candidates = []
                base = path
            for item in candidates:
                stat = item.stat()
                try:
                    digest = hashlib.sha256(item.read_bytes()).hexdigest()
                except OSError:
                    digest = ""
                try:
                    relative = item.relative_to(base).as_posix()
                except ValueError:
                    relative = item.name
                files[relative] = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns), "sha256": digest}
            return {"mode": "local", "path": str(path), "visible": bool(files), "files": files}
        except OSError as exc:
            return {"mode": "local", "path": str(path), "visible": False, "files": files, "error": str(exc)}

    def _snapshot_openlist_strm(self, path: str, *, max_depth: int = 4, limit: int = 500) -> dict[str, Any]:
        normalized = normalize_path(path)
        files: dict[str, Any] = {}
        try:
            root_item = self.openlist.get_item(normalized)
            if root_item is None:
                return {"mode": "openlist", "path": normalized, "visible": False, "files": {}}
            if not root_item.is_dir:
                if root_item.name.lower().endswith(".strm"):
                    files[root_item.name] = {"size": root_item.size, "modified": root_item.modified}
                return {"mode": "openlist", "path": normalized, "visible": bool(files), "files": files}
            queue: list[tuple[str, int]] = [(normalized, 0)]
            visited: set[str] = set()
            while queue and len(files) < limit:
                current, depth = queue.pop(0)
                if current in visited or depth > max_depth:
                    continue
                visited.add(current)
                for item in self.openlist.list_dir(current, refresh=depth == 0):
                    if item.is_dir and depth < max_depth:
                        queue.append((item.path, depth + 1))
                    elif not item.is_dir and item.name.lower().endswith(".strm"):
                        files[item.path] = {"size": item.size, "modified": item.modified}
                        if len(files) >= limit:
                            break
            return {"mode": "openlist", "path": normalized, "visible": bool(files), "files": files}
        except Exception as exc:  # noqa: BLE001
            return {"mode": "openlist", "path": normalized, "visible": False, "files": files, "error": str(exc)}

    def _confirm_strm_generation(self, task: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        names = [str(item or "") for item in (state.get("resource_names") or [])]
        paths = [str(item or "") for item in (state.get("refresh_paths") or [])]
        baseline = state.get("baseline") if isinstance(state.get("baseline"), dict) else {}
        current = self._capture_strm_targets(task, names, paths)
        changed_names = {str(item or "") for item in (state.get("changed_resource_names") or [])}
        checks: list[dict[str, Any]] = []
        for index, path in enumerate(paths):
            name = names[index] if index < len(names) else basename(path)
            before = baseline.get(path) if isinstance(baseline.get(path), dict) else {}
            after = current.get(path) if isinstance(current.get(path), dict) else {}
            visible = bool(after.get("visible"))
            changed = bool(visible and (not before.get("visible") or before.get("files") != after.get("files")))
            require_change = name in changed_names and bool(before.get("visible"))
            success = visible and (changed or not require_change)
            checks.append(
                {
                    "name": name,
                    "path": path,
                    "mode": after.get("mode") or before.get("mode") or "",
                    "visible": visible,
                    "changed": changed,
                    "require_change": require_change,
                    "success": success,
                    "error": after.get("error") or "",
                }
            )
        success = bool(checks) and all(item.get("success") for item in checks)
        return {
            "success": success,
            "checked_at": _utc_now_text(),
            "count": len(checks),
            "confirmed": len([item for item in checks if item.get("success")]),
            "checks": checks,
            "message": "新 STRM 已生成并可见" if success else "新 STRM 尚未全部生成或同步",
        }

    def _refresh_openlist_strm_for_task(self, task_id: int, task: dict[str, Any]) -> dict[str, Any]:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        passthrough = raw_data.get("passthrough_import") if isinstance(raw_data.get("passthrough_import"), dict) else {}
        if passthrough.get("enabled") and passthrough.get("skip_openlist_strm_refresh"):
            logger.info("organizer_passthrough_openlist_refresh_skipped task_id=%s", task_id)
            return {}
        staging_required = self._task_has_staging_plan(task)
        configured_refresh = bool(self.organizer_config.get("strm_refresh_after_apply"))
        if not configured_refresh and not staging_required:
            return {}
        endpoint = OPENLIST_STRM_SCAN_ENDPOINT
        category_key = str(task.get("category") or "").strip()
        names = self._final_resource_names(task)
        if not names:
            return {"enabled": True, "skipped": True, "message": "未找到可刷新的最终影视目录"}
        refresh_prefix = self._strm_refresh_prefix_for_task(task, category_key)
        if not refresh_prefix:
            return {
                "enabled": True,
                "failed": 1,
                "configuration_error": True,
                "endpoint": endpoint,
                "count": 0,
                "items": [],
                "message": "未配置 OpenList STRM 分类刷新前缀，已拒绝错误刷新 /<影视名>",
            }
        refresh_paths = [self._strm_refresh_path(category_key, name, prefix=refresh_prefix) for name in names]
        requested_at = _utc_now_text()
        result_items: list[dict[str, Any]] = []
        for name, refresh_path in zip(names, refresh_paths):
            try:
                result = self.openlist.refresh_strm(refresh_path, endpoint=endpoint, name=name)
                result_items.append({"success": True, "name": name, "path": refresh_path, "result": result})
            except Exception as exc:  # noqa: BLE001
                result_items.append({"success": False, "name": name, "path": refresh_path, "message": str(exc)})
        failed = [item for item in result_items if not item.get("success")]
        return {
            "enabled": True,
            "endpoint": endpoint,
            "requested_at": requested_at,
            "refresh_prefix": refresh_prefix,
            "resource_names": names,
            "refresh_paths": refresh_paths,
            "count": len(result_items),
            "failed": len(failed),
            "forced_by_staging": bool(staging_required and not configured_refresh),
            "items": result_items,
            "message": f"OpenList 文件夹刷新已触发 {len(result_items)} 个目录" if not failed else f"OpenList 文件夹刷新 {len(failed)}/{len(result_items)} 个目录失败",
        }

    def _cleanup_old_strm_dir_for_task(
        self,
        task: dict[str, Any],
        category_key: str,
        final_names: list[str],
        *,
        refresh_prefix: str = "",
    ) -> dict[str, Any]:
        if not self.organizer_config.get("strm_cleanup_old_before_refresh"):
            return {"enabled": False, "skipped": True, "message": "\u65e7 STRM \u76ee\u5f55\u6e05\u7406\u672a\u542f\u7528"}
        old_name = self._old_strm_resource_name(task)
        if not old_name:
            old_name = self._staging_job_strm_resource_name(task)
        normalized_category = str(category_key or "").strip().lower()
        if not old_name:
            return {
                "enabled": True,
                "skipped": True,
                "message": "未能从任务源路径唯一识别旧 STRM 资源目录，已跳过清理",
            }
        if old_name in set(final_names):
            return {"enabled": True, "skipped": True, "old_name": old_name, "message": "\u65e7\u540d\u4e0e\u6700\u7ec8\u540d\u4e00\u81f4\uff0c\u65e0\u9700\u6e05\u7406\u65e7 STRM \u76ee\u5f55"}

        try:
            openlist_cleanup = self._cleanup_old_openlist_strm_dir(
                old_name,
                normalized_category,
                prefix=refresh_prefix,
            )
        except TypeError:
            openlist_cleanup = self._cleanup_old_openlist_strm_dir(old_name, normalized_category)
        local_cleanup = self._cleanup_old_local_strm_dir(old_name, normalized_category)
        failed = bool(openlist_cleanup.get("failed") or local_cleanup.get("failed"))
        success = bool(openlist_cleanup.get("success") or local_cleanup.get("success")) and not failed
        return {
            "enabled": True,
            "success": success,
            "failed": failed,
            "old_name": old_name,
            "openlist": openlist_cleanup,
            "local": local_cleanup,
            "message": _strm_cleanup_message(openlist_cleanup, local_cleanup),
        }

    @staticmethod
    def _old_strm_resource_name(task: dict[str, Any]) -> str:
        root = normalize_path(task.get("openlist_root_path") or "")
        root_name = basename(root)
        if not re.fullmatch(r"job-\d+", root_name, flags=re.IGNORECASE):
            return root_name

        candidates: list[str] = []
        root_prefix = root.rstrip("/")
        for mapping in task.get("mappings") or []:
            if not isinstance(mapping, dict):
                continue
            if str(mapping.get("status") or "") not in {"ready", "skipped_existing"}:
                continue
            source = normalize_path(mapping.get("source_path") or "")
            if source == root or not _path_is_same_or_child(source, root):
                continue
            relative = source[len(root_prefix) :].lstrip("/")
            parts = [part for part in relative.split("/") if part]
            # Organizer 映射指向具体文件。只有至少包含“资源目录/文件”两层时，
            # 第一层才是旧 STRM 应清理的实际资源目录；文件直接落在 job 根时跳过。
            if len(parts) < 2:
                continue
            candidate = parts[0].strip()
            if not candidate or re.fullmatch(r"job-\d+", candidate, flags=re.IGNORECASE):
                continue
            # Season 01 / 第 1 季只是资源内部结构，不是旧影视目录名。把它
            # 当成旧名可能误删分类根下同名的正常 STRM 目录，宁可安全跳过。
            if _is_explicit_season_name(candidate):
                continue
            if _is_generic_media_wrapper_name(candidate):
                continue
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates[0] if len(candidates) == 1 else ""

    def _staging_job_strm_resource_name(self, task: dict[str, Any]) -> str:
        """Return a system-owned job directory only for a verified direct-file staging task."""

        root = normalize_path(task.get("openlist_root_path") or "")
        root_name = basename(root)
        if not re.fullmatch(r"job-\d+", root_name, flags=re.IGNORECASE):
            return ""
        if not self._task_has_staging_plan(task):
            return ""

        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        planned_root = normalize_path(plan.get("openlist_job_root") or "")
        if not planned_root or planned_root.casefold() != root.casefold():
            return ""

        direct_files = 0
        root_prefix = root.rstrip("/")
        for mapping in task.get("mappings") or []:
            if not isinstance(mapping, dict):
                continue
            if str(mapping.get("status") or "") not in {"ready", "skipped_existing"}:
                continue
            source = normalize_path(mapping.get("source_path") or "")
            if source == root or not _path_is_same_or_child(source, root):
                continue
            relative = source[len(root_prefix) :].lstrip("/")
            parts = [part for part in relative.split("/") if part]
            # 只有视频文件全部直接落在已验证的 job 根目录时，job-N 才是
            # OpenList 可能遗留的旧 STRM 资源名；出现任何子目录都继续安全跳过。
            if len(parts) != 1:
                return ""
            direct_files += 1
        return root_name if direct_files else ""

    def _cleanup_old_openlist_strm_dir(
        self,
        old_name: str,
        normalized_category: str,
        *,
        prefix: str = "",
    ) -> dict[str, Any]:
        prefix = normalize_path(prefix) if str(prefix or "").strip() else self._strm_refresh_prefix(normalized_category)
        if not prefix:
            return {"enabled": True, "skipped": True, "old_name": old_name, "message": "\u672a\u914d\u7f6e STRM \u5206\u7c7b\u524d\u7f00\uff0c\u8df3\u8fc7 OpenList \u65e7\u76ee\u5f55\u6e05\u7406"}
        old_path = join_path(prefix, old_name)
        if not _is_child_path(old_path, prefix):
            return {"enabled": True, "skipped": True, "old_name": old_name, "path": old_path, "message": "OpenList \u65e7 STRM \u8def\u5f84\u4e0d\u5728\u914d\u7f6e\u524d\u7f00\u4e0b\uff0c\u62d2\u7edd\u5220\u9664"}
        try:
            if not self.openlist.exists(old_path):
                return {"enabled": True, "skipped": True, "old_name": old_name, "path": old_path, "message": "OpenList \u65e7 STRM \u76ee\u5f55\u4e0d\u5b58\u5728\uff0c\u8df3\u8fc7"}
            self.openlist.remove_path(old_path)
            return {"enabled": True, "success": True, "old_name": old_name, "path": old_path, "message": "\u5df2\u5220\u9664 OpenList \u65e7 STRM \u76ee\u5f55"}
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "success": False, "failed": True, "old_name": old_name, "path": old_path, "message": f"OpenList \u65e7 STRM \u76ee\u5f55\u5220\u9664\u5931\u8d25\uff1a{exc}"}

    def _cleanup_old_local_strm_dir(self, old_name: str, normalized_category: str) -> dict[str, Any]:
        root = str(self.organizer_config.get("local_strm_root") or "").strip()
        if not root:
            return {"enabled": False, "skipped": True, "old_name": old_name, "message": "\u672a\u914d\u7f6e\u672c\u5730 STRM \u6839\u76ee\u5f55\uff0c\u8df3\u8fc7\u672c\u5730\u65e7\u76ee\u5f55\u6e05\u7406"}
        category_dir = self._local_strm_category_dir(normalized_category)
        if not category_dir:
            return {"enabled": True, "skipped": True, "old_name": old_name, "message": "\u672a\u8bc6\u522b\u672c\u5730 STRM \u5206\u7c7b\u76ee\u5f55\u540d\uff0c\u8df3\u8fc7\u672c\u5730\u65e7\u76ee\u5f55\u6e05\u7406"}
        checked: list[str] = []
        openlist_checked: list[str] = []
        errors: list[str] = []
        for candidate_name in _local_strm_old_name_variants(old_name):
            target: Path | None = None
            try:
                target = self._safe_local_strm_child(root, category_dir, candidate_name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{candidate_name}: {exc}")
            if target is not None:
                checked.append(str(target))
                try:
                    if target.exists() or target.is_symlink():
                        if target.is_symlink() or target.is_file():
                            target.unlink()
                        elif target.is_dir():
                            shutil.rmtree(target)
                        else:
                            errors.append(f"{target}: unsupported path type")
                            target = None
                        if target is not None:
                            exists_after = target.exists() or target.is_symlink()
                            return {
                                "enabled": True,
                                "success": not exists_after,
                                "failed": exists_after,
                                "old_name": old_name,
                                "matched_name": candidate_name,
                                "path": str(target),
                                "checked_paths": checked,
                                "exists_after": exists_after,
                                "message": "\u5df2\u5220\u9664\u672c\u5730\u65e7 STRM \u76ee\u5f55" if not exists_after else "\u672c\u5730\u65e7 STRM \u76ee\u5f55\u5220\u9664\u540e\u4ecd\u5b58\u5728",
                            }
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{target}: {exc}")
            try:
                openlist_target = self._safe_openlist_local_strm_child(root, category_dir, candidate_name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"OpenList:{candidate_name}: {exc}")
                continue
            openlist_checked.append(openlist_target)
            try:
                if not self.openlist.exists(openlist_target):
                    continue
                self.openlist.remove_path(openlist_target)
                exists_after = self.openlist.exists(openlist_target)
                return {
                    "enabled": True,
                    "success": not exists_after,
                    "failed": exists_after,
                    "old_name": old_name,
                    "matched_name": candidate_name,
                    "path": openlist_target,
                    "checked_paths": checked,
                    "openlist_checked_paths": openlist_checked,
                    "exists_after": exists_after,
                    "message": "\u5df2\u5220\u9664 OpenList \u6302\u8f7d\u7684\u672c\u5730\u65e7 STRM \u76ee\u5f55" if not exists_after else "OpenList \u6302\u8f7d\u7684\u672c\u5730\u65e7 STRM \u76ee\u5f55\u5220\u9664\u540e\u4ecd\u5b58\u5728",
                }
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{openlist_target}: {exc}")
        return {
            "enabled": True,
            "skipped": not errors,
            "failed": bool(errors),
            "old_name": old_name,
            "checked_paths": checked,
            "openlist_checked_paths": openlist_checked,
            "errors": errors[:5],
            "exists_after": False,
            "message": "\u672c\u5730\u65e7 STRM \u76ee\u5f55\u4e0d\u5b58\u5728\uff0c\u5df2\u540c\u65f6\u68c0\u67e5\u672c\u673a\u6302\u8f7d\u548c OpenList \u6302\u8f7d\u8def\u5f84" if not errors else f"\u672c\u5730\u65e7 STRM \u76ee\u5f55\u5220\u9664\u5931\u8d25\uff1a{errors[0]}",
        }

    def _local_strm_category_dir(self, normalized_category: str) -> str:
        prefix = self._strm_refresh_prefix(normalized_category)
        if prefix:
            category_dir = basename(prefix)
            if category_dir:
                return category_dir
        category = self.categories.get(normalized_category, {})
        label = str(category.get("label") or CATEGORY_LABELS.get(normalized_category) or normalized_category).strip()
        return label.replace("/", "").replace("\\", "").strip()

    @staticmethod
    def _safe_local_strm_child(root: str, category_dir: str, old_name: str) -> Path:
        clean_old_name = str(old_name or "").strip().replace("\\", "/")
        clean_category = str(category_dir or "").strip().replace("\\", "/").strip("/")
        if not clean_old_name or clean_old_name in {".", ".."} or "/" in clean_old_name:
            raise ValueError("\u65e7\u76ee\u5f55\u540d\u975e\u6cd5")
        if not clean_category or clean_category in {".", ".."} or "/" in clean_category:
            raise ValueError("\u5206\u7c7b\u76ee\u5f55\u540d\u975e\u6cd5")
        root_path = Path(root).expanduser()
        category_path = root_path / clean_category
        target = category_path / clean_old_name
        root_resolved = root_path.resolve(strict=False)
        category_resolved = category_path.resolve(strict=False)
        target_resolved = target.resolve(strict=False)
        if category_resolved == root_resolved:
            raise ValueError("\u5206\u7c7b\u76ee\u5f55\u4e0d\u80fd\u7b49\u4e8e\u672c\u5730 STRM \u6839\u76ee\u5f55")
        if not _path_is_relative_to(category_resolved, root_resolved):
            raise ValueError("\u5206\u7c7b\u76ee\u5f55\u4e0d\u5728\u672c\u5730 STRM \u6839\u76ee\u5f55\u5185")
        if not _path_is_relative_to(target_resolved, category_resolved):
            raise ValueError("\u76ee\u6807\u76ee\u5f55\u4e0d\u5728\u672c\u5730 STRM \u5206\u7c7b\u76ee\u5f55\u5185")
        if target_resolved in {root_resolved, category_resolved}:
            raise ValueError("\u62d2\u7edd\u5220\u9664\u672c\u5730 STRM \u6839\u76ee\u5f55\u6216\u5206\u7c7b\u76ee\u5f55")
        return target

    @staticmethod
    def _safe_openlist_local_strm_child(root: str, category_dir: str, old_name: str) -> str:
        clean_root = str(root or "").strip().replace("\\", "/")
        clean_old_name = str(old_name or "").strip().replace("\\", "/")
        clean_category = str(category_dir or "").strip().replace("\\", "/").strip("/")
        if not clean_root or "://" in clean_root or re.match(r"^[A-Za-z]:/", clean_root):
            raise ValueError("\u672c\u5730 STRM \u6839\u76ee\u5f55\u4e0d\u662f OpenList \u865a\u62df\u8def\u5f84")
        if not clean_old_name or clean_old_name in {".", ".."} or "/" in clean_old_name:
            raise ValueError("\u65e7\u76ee\u5f55\u540d\u975e\u6cd5")
        if not clean_category or clean_category in {".", ".."} or "/" in clean_category:
            raise ValueError("\u5206\u7c7b\u76ee\u5f55\u540d\u975e\u6cd5")
        root_path = normalize_path(clean_root)
        if root_path == "/":
            raise ValueError("\u672c\u5730 STRM \u6839\u76ee\u5f55\u4e0d\u80fd\u662f OpenList \u6839\u76ee\u5f55")
        category_path = join_path(root_path, clean_category)
        target = join_path(category_path, clean_old_name)
        if not _is_child_path(target, category_path):
            raise ValueError("\u76ee\u6807\u76ee\u5f55\u4e0d\u5728 OpenList \u672c\u5730 STRM \u5206\u7c7b\u76ee\u5f55\u5185")
        return target

    def _check_old_local_strm_after_refresh(self, old_name: Any, normalized_category: str) -> dict[str, Any]:
        root = str(self.organizer_config.get("local_strm_root") or "").strip()
        if not root or not old_name:
            return {"enabled": False, "skipped": True}
        category_dir = self._local_strm_category_dir(normalized_category)
        checked: list[str] = []
        openlist_checked: list[str] = []
        try:
            for candidate_name in _local_strm_old_name_variants(str(old_name)):
                target = self._safe_local_strm_child(root, category_dir, candidate_name)
                checked.append(str(target))
                if target.exists() or target.is_symlink():
                    return {"enabled": True, "path": str(target), "checked_paths": checked, "openlist_checked_paths": openlist_checked, "exists": True, "message": "\u672c\u5730\u65e7 STRM \u76ee\u5f55\u4ecd\u5b58\u5728"}
                try:
                    openlist_target = self._safe_openlist_local_strm_child(root, category_dir, candidate_name)
                    openlist_checked.append(openlist_target)
                    if self.openlist.exists(openlist_target):
                        return {"enabled": True, "path": openlist_target, "checked_paths": checked, "openlist_checked_paths": openlist_checked, "exists": True, "message": "OpenList \u6302\u8f7d\u7684\u672c\u5730\u65e7 STRM \u76ee\u5f55\u4ecd\u5b58\u5728"}
                except Exception:
                    pass
            return {"enabled": True, "checked_paths": checked, "openlist_checked_paths": openlist_checked, "exists": False, "message": "\u672c\u5730\u65e7 STRM \u76ee\u5f55\u5df2\u4e0d\u5b58\u5728"}
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "failed": True, "checked_paths": checked, "openlist_checked_paths": openlist_checked, "message": str(exc)}

    def _final_resource_names(self, task: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for mapping in task.get("mappings") or []:
            if str(mapping.get("status") or "") not in {"ready", "skipped_existing"}:
                continue
            target = str(mapping.get("target_path") or "").strip()
            if not target:
                continue
            target_dir = dirname(target)
            resource_name = basename(target_dir)
            if resource_name.lower().startswith("season "):
                resource_name = basename(dirname(target_dir))
            if resource_name and resource_name not in result:
                result.append(resource_name)
        return result

    def _strm_refresh_path(self, category_key: str, resource_name: str, *, prefix: str = "") -> str:
        normalized_category = str(category_key or "").strip().lower()
        effective_prefix = normalize_path(prefix) if str(prefix or "").strip() else self._strm_refresh_prefix(normalized_category)
        return join_path(effective_prefix, resource_name) if effective_prefix else ""

    def _strm_refresh_prefix_for_task(self, task: dict[str, Any], category_key: str) -> str:
        raw_data = task.get("raw_data") if isinstance(task.get("raw_data"), dict) else {}
        plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        if plan.get("enabled"):
            persisted = str(plan.get("openlist_refresh_prefix") or "").strip()
            if persisted:
                return normalize_path(persisted)
        return self._strm_refresh_prefix(str(category_key or "").strip().lower())

    def _strm_refresh_prefix(self, normalized_category: str) -> str:
        prefix = str(self.organizer_config.get(f"strm_refresh_prefix_{normalized_category}") or "").strip()
        if not prefix and normalized_category in {"anime", "variety"}:
            prefix = str(self.organizer_config.get("strm_refresh_prefix_tv") or "").strip()
        if not prefix:
            prefix = str(self.organizer_config.get("strm_refresh_prefix") or "").strip()
        return _dedupe_repeated_tail_path(normalize_path(prefix)) if prefix else ""

    def _filter_videos_for_task(self, task: dict[str, Any], videos: list[Any]) -> list[Any]:
        """按任务侧提供的文件证据收窄扫描结果。

        6盘离线完成时只能稳定知道分类根目录，例如 /清云/电影；如果直接扫描整
        个分类根目录，可能误把历史资源一起整理。调用方可在 raw_data/evidence
        里写入 scan_filters.expected_names / expected_paths，本方法只保留本次任务
        相关的视频文件。没有过滤条件时保持旧行为。
        """

        if self._task_has_staging_plan(task):
            return videos
        filters = self._task_scan_filters(task)
        expected_names = {_normalize_match_text(item) for item in filters.get("expected_names") or [] if _normalize_match_text(item)}
        expected_paths = {_normalize_match_text(item) for item in filters.get("expected_paths") or [] if _normalize_match_text(item)}
        if not expected_names and not expected_paths:
            return videos

        matched = []
        for item in videos:
            name = _normalize_match_text(getattr(item, "name", ""))
            path = _normalize_match_text(getattr(item, "path", ""))
            if name and name in expected_names:
                matched.append(item)
                continue
            if path and any(path.endswith(expected) or expected.endswith(path) or expected in path for expected in expected_paths):
                matched.append(item)
                continue
        return matched

    @staticmethod
    def _task_scan_filters(task: dict[str, Any]) -> dict[str, Any]:
        for container_key in ("raw_data", "evidence"):
            container = task.get(container_key) if isinstance(task.get(container_key), dict) else {}
            filters = container.get("scan_filters") if isinstance(container.get("scan_filters"), dict) else {}
            if filters:
                return filters
        return {}

    @staticmethod
    def _task_update_context(task: dict[str, Any]) -> dict[str, Any]:
        for container_key in ("raw_data", "evidence"):
            container = task.get(container_key) if isinstance(task.get(container_key), dict) else {}
            for key in ("update_context", "organizer_context"):
                context = container.get(key) if isinstance(container.get(key), dict) else {}
                if context:
                    return context
        return {}

    def _update_target_already_exists_ok(self, task: dict[str, Any], season: int | None, episode: int | None) -> bool:
        context = self._task_update_context(task)
        if not context or not episode:
            return False
        target_episode = _safe_positive_int(context.get("target_episode"))
        target_episodes = [
            _safe_positive_int(item)
            for item in (context.get("target_episodes") or [])
        ]
        target_episodes = [item for item in target_episodes if item]
        if target_episode and episode == target_episode:
            return True
        if target_episodes and episode in target_episodes:
            return True
        return not target_episode and not target_episodes and bool(context.get("subscription_id"))

    @staticmethod
    def _target_category_for_task(task: dict[str, Any], category: dict[str, Any]) -> dict[str, Any]:
        target_root = ""
        resource_root = ""
        target_root_is_resource = False
        for container_key in ("raw_data", "evidence"):
            container = task.get(container_key) if isinstance(task.get(container_key), dict) else {}
            target_root = str(container.get("target_root_path") or "").strip()
            resource_root = str(container.get("canonical_resource_root") or container.get("resource_root_path") or "").strip()
            target_root_is_resource = _flag_enabled(container.get("target_root_is_resource"))
            if target_root:
                break
        scoped = dict(category)
        scoped["source_category_root_path"] = _source_category_root_for_path(
            task.get("openlist_root_path"),
            category,
        )
        if not target_root:
            return scoped
        target_root = _resource_root_from_task_context(target_root)
        resource_root = _resource_root_from_task_context(resource_root)
        scoped["openlist_root_path"] = target_root
        if resource_root or target_root_is_resource:
            scoped["resource_root_path"] = resource_root or target_root
            scoped["canonical_resource_root"] = resource_root or target_root
        return scoped

    def _match_tmdb(
        self,
        task: dict[str, Any],
        title: str,
        year: str,
        category_key: str,
        media_type: str,
        has_episodes: bool,
        *,
        extra_queries: list[Any] | None = None,
    ) -> dict[str, Any] | None:
        queries = [title, f"{title} {year}".strip(), *(extra_queries or []), task.get("source_keyword"), task.get("title")]
        best: tuple[float, dict[str, Any] | None] = (0, None)
        for query in _dedupe_texts(str(item or "").strip() for item in queries if str(item or "").strip()):
            if not _is_valid_tmdb_query(query):
                continue
            query_title, query_year = _lookup_title_year(query)
            tmdb_query = query_title or sanitize_candidate(query)
            if not tmdb_query or not _is_valid_tmdb_query(tmdb_query):
                continue
            for item in self.tmdb.search(tmdb_query, "movie" if media_type == "movie" else "tv"):
                score = max(
                    score_tmdb_result(title, year, category_key, item, has_episodes),
                    score_tmdb_result(query_title or title, query_year or year, category_key, item, has_episodes),
                )
                self.db.add_organizer_tmdb_match(int(task["id"]), tmdb_query, media_type, item, score)
                if score > best[0]:
                    best = (score, item)
            if best[0] >= 85:
                break
        if best[0] < 85:
            # 分数不足的“命中”多半是通用词/垃圾词同名条目（如标题就叫“电视剧”），
            # 直接当作未命中返回，让上层走 AI 兜底，而不是拿虚高可信度挡在门外。
            return None
        return best[1]

    def _configured_category_root_for_path(self, path: str) -> str:
        normalized = normalize_path(path)
        explicit_candidates: list[str] = []
        inferred_candidates: list[str] = []
        for item in getattr(self, "categories", {}).values():
            roots = [
                item.get("openlist_root_path"),
                item.get("cloud139_fnos_target_path"),
                item.get("mobile_openlist_root_path"),
                item.get("mobile_target_path"),
                item.get("sixpan_fnos_target_path"),
            ]
            explicit = [value for value in roots if str(value or "").strip()]
            if not explicit:
                explicit = [category_target_root(item)]
            for value in explicit:
                root = normalize_path(value)
                if root == "/" or not _path_is_same_or_child(normalized, root):
                    continue
                if root not in explicit_candidates:
                    explicit_candidates.append(root)
            inferred = _source_category_root_for_path(normalized, item)
            if inferred != "/" and _path_is_same_or_child(normalized, inferred) and inferred not in inferred_candidates:
                inferred_candidates.append(inferred)
        candidates = explicit_candidates or inferred_candidates
        return max(candidates, key=lambda value: (value.count("/"), len(value)), default="")

    def _target_create_stop_root(self, target_path: str, category_root: str, resource_root: str) -> str:
        target_dir = dirname(target_path)
        if resource_root and resource_root != "/" and _path_is_same_or_child(target_dir, resource_root):
            return dirname(resource_root)
        configured_root = self._configured_category_root_for_path(target_dir)
        if configured_root:
            return configured_root
        if category_root != "/" and _path_is_same_or_child(target_dir, category_root):
            return category_root
        resource_dir = dirname(target_dir) if _is_explicit_season_name(basename(target_dir)) else target_dir
        return dirname(resource_dir)

    def _operations_for_mappings(self, mappings: list[dict[str, Any]], category: dict[str, Any], *, include_auto_delete: bool = True) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        dirs = []
        cleanup_dirs: list[str] = []
        category_root = normalize_path(category_target_root(category))
        source_category_root = normalize_path(category.get("source_category_root_path") or category_root)
        resource_root = normalize_path(category.get("resource_root_path") or category.get("canonical_resource_root") or "")
        for mapping in mappings:
            status = str(mapping.get("status") or "")
            if status == "delete_ad":
                if not include_auto_delete:
                    continue
                operations.append(
                    {
                        "type": "delete_file",
                        "source_path": mapping["source_path"],
                        "target_path": mapping["source_path"],
                        "description": f"彻底删除广告小文件：{mapping['source_path']}",
                        "status": "pending",
                        "reason": mapping.get("reason") or [],
                    }
                )
                continue
            source_path = str(mapping.get("source_path") or "")
            target_path = str(mapping.get("target_path") or "")
            if status == "skipped_existing":
                if source_path and target_path and normalize_path(source_path) != normalize_path(target_path):
                    operations.append(
                        {
                            "type": "delete_duplicate_file",
                            "source_path": source_path,
                            "target_path": target_path,
                            "description": f"标准目标已存在，删除本次重复入库文件：{source_path}",
                            "status": "pending",
                            "reason": mapping.get("reason") or ["标准目标已存在，保留标准文件并清理重复源文件"],
                        }
                    )
                    for cleanup_dir in self._source_cleanup_dirs(source_path, source_category_root):
                        if cleanup_dir not in cleanup_dirs:
                            cleanup_dirs.append(cleanup_dir)
                continue
            if status != "ready":
                continue
            target_dir = dirname(target_path)
            create_stop_root = self._target_create_stop_root(target_path, category_root, resource_root)
            for create_dir in _create_dir_chain(target_dir, create_stop_root):
                if create_dir not in dirs:
                    dirs.append(create_dir)
                    operations.append({"type": "create_dir", "target_path": create_dir, "description": f"创建目录：{create_dir}", "status": "pending", "reason": ["目标目录不存在时创建"]})
            if normalize_path(source_path) == normalize_path(target_path):
                continue
            mapping_raw_data = mapping.get("raw_data") if isinstance(mapping.get("raw_data"), dict) else {}
            staging_file = _flag_enabled(mapping_raw_data.get("staging_file"))
            companion_file = _flag_enabled(mapping_raw_data.get("companion_file"))
            operations.append(
                {
                    "type": "move_file",
                    "source_path": source_path,
                    "target_path": target_path,
                    "description": f"整理文件：{source_path} -> {target_path}",
                    "status": "pending",
                    "reason": mapping.get("reason") or [],
                    "raw_data": {
                        "staging_file": staging_file,
                        "delete_source_if_target_exists": bool(resource_root and resource_root != "/")
                        and not staging_file
                        and not companion_file,
                        "fail_if_target_exists": staging_file or companion_file,
                    },
                }
            )
            for cleanup_dir in self._source_cleanup_dirs(source_path, source_category_root):
                if cleanup_dir not in cleanup_dirs:
                    cleanup_dirs.append(cleanup_dir)
        cleanup_dirs.sort(key=lambda item: item.count("/"), reverse=True)
        for cleanup_dir in cleanup_dirs:
            operations.append(
                {
                    "type": "cleanup_empty_dir",
                    "target_path": cleanup_dir,
                    "description": f"删除搬空目录：{cleanup_dir}",
                    "status": "pending",
                    "reason": ["文件已移动后删除空的原目录"],
                }
            )
        return operations

    @staticmethod
    def _source_cleanup_dirs(source_path: str, category_root: str) -> list[str]:
        """Return source directories that may become empty after moving files.

        只清理文件原目录及其空父目录，不清理分类根目录，避免对整个 OpenList
        分类根执行递归空目录删除导致超时。
        """

        root = normalize_path(category_root)
        current = dirname(source_path)
        result: list[str] = []
        while current and current != "/" and current != root:
            if root != "/" and not _path_is_same_or_child(current, root):
                break
            result.append(current)
            parent = dirname(current)
            if parent == current:
                break
            current = parent
        return result

    def _wait_for_openlist_path(self, path: str, *, attempts: int | None = None, delay: float | None = None) -> bool:
        config = getattr(self, "organizer_config", {}) if isinstance(getattr(self, "organizer_config", {}), dict) else {}
        try:
            attempt_count = int(attempts if attempts is not None else config.get("operation_visibility_attempts", 6))
        except (TypeError, ValueError):
            attempt_count = 6
        try:
            delay_seconds = float(delay if delay is not None else config.get("operation_visibility_delay_seconds", 0.8))
        except (TypeError, ValueError):
            delay_seconds = 0.8
        attempt_count = max(1, min(attempt_count, 30))
        delay_seconds = max(0.0, min(delay_seconds, 5.0))
        normalized = normalize_path(path)
        for attempt in range(attempt_count):
            try:
                if self.openlist.exists(normalized):
                    return True
            except Exception:  # noqa: BLE001
                pass
            try:
                self.openlist.list_dir(dirname(normalized), refresh=True)
                if self.openlist.exists(normalized):
                    return True
            except Exception:  # noqa: BLE001
                pass
            if attempt < attempt_count - 1 and delay_seconds > 0:
                time.sleep(delay_seconds)
        return False

    def _wait_for_openlist_absence(self, path: str, *, attempts: int | None = None, delay: float | None = None) -> bool:
        config = getattr(self, "organizer_config", {}) if isinstance(getattr(self, "organizer_config", {}), dict) else {}
        try:
            attempt_count = int(attempts if attempts is not None else config.get("operation_visibility_attempts", 6))
        except (TypeError, ValueError):
            attempt_count = 6
        try:
            delay_seconds = float(delay if delay is not None else config.get("operation_visibility_delay_seconds", 0.8))
        except (TypeError, ValueError):
            delay_seconds = 0.8
        attempt_count = max(1, min(attempt_count, 30))
        delay_seconds = max(0.0, min(delay_seconds, 5.0))
        normalized = normalize_path(path)
        for attempt in range(attempt_count):
            try:
                self.openlist.list_dir(dirname(normalized), refresh=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                if not self.openlist.exists(normalized):
                    return True
            except Exception:  # noqa: BLE001
                pass
            if attempt < attempt_count - 1 and delay_seconds > 0:
                time.sleep(delay_seconds)
        return False

    def _execute_operation(self, op: dict[str, Any]) -> dict[str, Any] | None:
        op_type = str(op.get("type") or "")
        source = str(op.get("source_path") or "")
        target = str(op.get("target_path") or "")
        if op_type == "create_dir":
            if self.openlist.exists(target):
                raise SkipOperation("目录已存在")
            self.openlist.mkdir(target)
            if not self._wait_for_openlist_path(target):
                raise RuntimeError(f"创建目录后仍不可见：{target}")
            return {"type": "remove_empty_directory", "target_path": target}
        if op_type == "cleanup_empty_dir":
            if normalize_path(target) == "/":
                raise SkipOperation("跳过根目录清理")
            try:
                if self.openlist.list_dir(target, refresh=True):
                    raise SkipOperation("目录非空，跳过删除")
                self.openlist.remove_empty_directory(target)
            except SkipOperation:
                raise
            except Exception as exc:  # noqa: BLE001
                raise SkipOperation(f"空目录删除失败，不影响整理：{exc}") from exc
            return None
        if op_type == "move_file":
            raw_data = op.get("raw_data") if isinstance(op.get("raw_data"), dict) else {}
            staging_file = _flag_enabled(raw_data.get("staging_file"))
            renamed_source = join_path(dirname(source), basename(target))
            case_only_rename = (
                normalize_path(dirname(source)).casefold() == normalize_path(dirname(target)).casefold()
                and basename(source) != basename(target)
                and basename(source).casefold() == basename(target).casefold()
            )
            if case_only_rename:
                if not self._rename_file_case_safely(source, target):
                    raise SkipOperation("仅大小写改名已完成")
                return {"type": "move_file", "source_path": target, "target_path": source}
            if self.openlist.exists(target):
                if staging_file:
                    source_exists = self.openlist.exists(source)
                    renamed_exists = renamed_source != source and self.openlist.exists(renamed_source)
                    if not source_exists and not renamed_exists:
                        raise SkipOperation("任务级暂存目标已存在且源文件已消失，视为此前移动已完成")
                    raise RuntimeError(f"任务级暂存源文件与目标文件同时存在，拒绝自动删除或覆盖：{source} -> {target}")
                if _flag_enabled(raw_data.get("delete_source_if_target_exists")):
                    return self._execute_operation(
                        {
                            "type": "delete_duplicate_file",
                            "source_path": source,
                            "target_path": target,
                        }
                    )
                if _flag_enabled(raw_data.get("fail_if_target_exists")):
                    if not self.openlist.exists(source):
                        raise SkipOperation("附件已位于最终目标且源文件不存在，视为上次移动已完成")
                    raise RuntimeError(f"附件目标已存在，拒绝跳过源文件：{target}")
                raise SkipOperation("目标已存在，不覆盖")
            target_dir = dirname(target)
            if not self._wait_for_openlist_path(target_dir):
                raise RuntimeError(f"目标目录不存在或尚不可见：{target_dir}")
            source_exists = self.openlist.exists(source)
            renamed_exists = renamed_source != source and self.openlist.exists(renamed_source)
            if not source_exists and not renamed_exists:
                source_exists = self._wait_for_openlist_path(source)
                if not source_exists and renamed_source != source:
                    renamed_exists = self._wait_for_openlist_path(renamed_source)
            if not source_exists and not renamed_exists:
                raise RuntimeError(f"源文件不存在：{source}")
            if source_exists and renamed_exists and renamed_source != source:
                raise RuntimeError(f"源目录中已存在同名文件，拒绝把其它文件当作改名重试结果：{renamed_source}")
            source_to_move = source if source_exists else renamed_source
            if source_to_move == source and basename(source) != basename(target):
                self.openlist.rename(source, basename(target), overwrite=False)
                if not self._wait_for_openlist_path(renamed_source):
                    raise RuntimeError(f"源文件改名后仍不可见：{renamed_source}")
                source_to_move = renamed_source
            if dirname(source_to_move) != dirname(target):
                self.openlist.move(source_to_move, dirname(target), overwrite=False, skip_existing=True, merge=True)
            if not self._wait_for_openlist_path(target):
                raise RuntimeError(f"移动后未确认到目标文件：{target}")
            if staging_file:
                source_paths = _dedupe_texts([source, renamed_source])
                remaining_sources = [path for path in source_paths if not self._wait_for_openlist_absence(path)]
                if remaining_sources:
                    raise RuntimeError(f"移动后任务级暂存源文件仍可见：{', '.join(remaining_sources)}")
            return {"type": "move_file", "source_path": target, "target_path": source}
        if op_type == "delete_duplicate_file":
            if normalize_path(source) == normalize_path(target):
                raise SkipOperation("源文件与标准目标相同，无需删除")
            source_ext = posixpath.splitext(basename(source))[1].lower()
            target_ext = posixpath.splitext(basename(target))[1].lower()
            if source_ext not in VIDEO_EXTENSIONS or target_ext not in VIDEO_EXTENSIONS:
                raise RuntimeError("重复源清理只允许删除视频文件")
            if not self._wait_for_openlist_path(target):
                raise RuntimeError(f"标准目标文件不存在，拒绝删除重复源文件：{target}")
            get_item = getattr(self.openlist, "get_item", None)
            if callable(get_item):
                target_item = get_item(target)
                if target_item is not None and bool(getattr(target_item, "is_dir", False)):
                    raise RuntimeError(f"标准目标是目录，拒绝删除重复源文件：{target}")
            if not self.openlist.exists(source):
                raise SkipOperation("重复源文件已不存在")
            self.openlist.remove_file(source)
            if not self._wait_for_openlist_absence(source):
                raise RuntimeError(f"删除后重复源文件仍可见：{source}")
            return None
        if op_type == "delete_file":
            if not source:
                raise SkipOperation("缺少源文件路径")
            if not self.openlist.exists(source):
                raise SkipOperation("源文件已不存在，视为已删除")
            self.openlist.remove_file(source)
            return None
        raise RuntimeError(f"未知操作类型：{op_type}")

    def _collect_move_file_batch(self, operations: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
        """从 index 开始收集连续的同目标目录、可批量的 pending move_file 操作。

        同源目录由 ``move(names[])`` 处理；多个源目录会先判断是否满足安全聚合移动，
        不满足时按源目录降级。delete_source_if_target_exists 仍退回单条处理。
        """
        if not self._bulk_operations_enabled():
            return []
        first = operations[index]
        target_dir = dirname(str(first.get("target_path") or ""))
        if not target_dir:
            return []
        batch: list[dict[str, Any]] = []
        source_paths: set[str] = set()
        target_names: set[str] = set()
        for op in operations[index:]:
            if str(op.get("status") or "") != "pending":
                break
            if str(op.get("type") or "") != "move_file":
                break
            op_source = str(op.get("source_path") or "")
            op_target = str(op.get("target_path") or "")
            if not op_source or normalize_path(dirname(op_target)) != normalize_path(target_dir):
                break
            raw_data = op.get("raw_data") if isinstance(op.get("raw_data"), dict) else {}
            if _flag_enabled(raw_data.get("delete_source_if_target_exists")):
                break
            source_path = normalize_path(op_source).casefold()
            target_name = basename(op_target).casefold()
            if source_path in source_paths or target_name in target_names:
                break
            source_paths.add(source_path)
            target_names.add(target_name)
            batch.append(op)
        return batch

    def _bulk_operations_enabled(self) -> bool:
        value = (getattr(self, "organizer_config", {}) or {}).get("bulk_operations_enabled")
        return True if value is None else _flag_enabled(value)

    def _regex_rename_min_items(self) -> int:
        try:
            value = int((getattr(self, "organizer_config", {}) or {}).get("regex_rename_min_items", 10))
        except (TypeError, ValueError):
            value = 10
        return max(2, min(value, 500))

    def _bulk_reconcile_timeout_seconds(self) -> float:
        try:
            value = float((getattr(self, "organizer_config", {}) or {}).get("bulk_reconcile_timeout_seconds", 120))
        except (TypeError, ValueError):
            value = 120.0
        return max(0.0, min(value, 900.0))

    def _reconcile_expected_rename_state(
        self,
        source_dir: str,
        *,
        expected_names: set[str],
        absent_names: set[str],
    ) -> str:
        expected = {str(name or "") for name in expected_names if str(name or "")}
        absent = {str(name or "") for name in absent_names if str(name or "")} - expected
        started = time.monotonic()
        deadline = started + self._bulk_reconcile_timeout_seconds()
        delay_schedule = (1.0, 2.0, 4.0, 8.0)
        attempt = 0
        last_error = ""
        while True:
            try:
                names = {item.name for item in self.openlist.list_dir(source_dir, refresh=True)}
                missing = sorted(expected - names)
                unexpected = sorted(absent & names)
                if not missing and not unexpected:
                    logger.info(
                        "organizer_bulk_rename_reconcile source_dir=%s expected_count=%s absent_count=%s attempts=%s duration_seconds=%s",
                        source_dir,
                        len(expected),
                        len(absent),
                        attempt + 1,
                        round(time.monotonic() - started, 3),
                    )
                    return ""
                details: list[str] = []
                if missing:
                    details.append(f"缺少期望名称：{', '.join(missing[:5])}")
                if unexpected:
                    details.append(f"仍存在应消失名称：{', '.join(unexpected[:5])}")
                last_error = "；".join(details)
            except Exception as exc:  # noqa: BLE001
                last_error = f"读取源目录失败：{exc}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(delay_schedule[min(attempt, len(delay_schedule) - 1)], remaining))
            attempt += 1
        logger.warning(
            "organizer_bulk_rename_reconcile_failed source_dir=%s expected_count=%s absent_count=%s attempts=%s duration_seconds=%s message=%s",
            source_dir,
            len(expected),
            len(absent),
            attempt + 1,
            round(time.monotonic() - started, 3),
            last_error,
        )
        return last_error or "批量改名后的目录状态不符合预期"

    def _perform_exact_batch_rename(
        self,
        source_dir: str,
        renames: list[tuple[str, str]],
        *,
        prefer_bulk: bool = True,
    ) -> tuple[str, int]:
        pairs = [(str(src or "").strip(), str(dst or "").strip()) for src, dst in renames if src and dst and src != dst]
        if not pairs:
            return ("none", 0)
        batch_method = getattr(self.openlist, "batch_rename", None)
        bulk_attempts = 0
        if prefer_bulk and callable(batch_method):
            bulk_attempts = 1
            try:
                if not batch_method(source_dir, pairs):
                    raise OpenListError("OpenList 批量重命名未生效")
                return ("batch_rename", 1)
            except OpenListEndpointUnsupported as exc:
                logger.info(
                    "organizer_bulk_endpoint_fallback endpoint=batch_rename source_dir=%s count=%s message=%s",
                    source_dir,
                    len(pairs),
                    exc,
                )
        for old_name, new_name in pairs:
            self.openlist.rename(join_path(source_dir, old_name), new_name, overwrite=False)
        return ("legacy_rename", bulk_attempts + len(pairs))

    def _perform_regex_batch_rename(
        self,
        source_dir: str,
        regex_plan: dict[str, Any],
    ) -> tuple[bool, int]:
        method = getattr(self.openlist, "regex_rename", None)
        if not callable(method):
            return (False, 0)
        try:
            if not method(source_dir, regex_plan["source_regex"], regex_plan["replacement"]):
                raise OpenListError("OpenList 正则重命名未生效")
            return (True, 1)
        except OpenListEndpointUnsupported as exc:
            logger.info(
                "organizer_bulk_endpoint_fallback endpoint=regex_rename source_dir=%s count=%s message=%s",
                source_dir,
                regex_plan.get("count"),
                exc,
            )
            return (False, 1)

    def _apply_adaptive_batch_renames(
        self,
        source_dir: str,
        entries: list[dict[str, Any]],
        source_names: set[str],
    ) -> dict[str, Any]:
        rename_entries = [entry for entry in entries if entry.get("needs_rename")]
        if not rename_entries:
            return {"strategy": "none", "write_requests": 0, "count": 0}

        bulk_enabled = self._bulk_operations_enabled()
        source_keys = {name.casefold() for name in source_names}
        needs_two_phase = (not bulk_enabled) or any(
            str(entry["current"]).casefold() == str(entry["dst_name"]).casefold()
            or str(entry["dst_name"]).casefold() in source_keys
            for entry in rename_entries
        )
        started = time.monotonic()
        write_requests = 0
        strategies: list[str] = []

        if not needs_two_phase:
            for entry in rename_entries:
                entry["reconcile_candidates"] = [entry["dst_name"], entry["original"]]
            regex_plan = _safe_episode_regex_rename_plan(
                rename_entries,
                source_names,
                minimum_items=self._regex_rename_min_items(),
            )
            if regex_plan:
                used, writes = self._perform_regex_batch_rename(source_dir, regex_plan)
                write_requests += writes
                if used:
                    strategies.append("regex_rename")
            if not strategies:
                strategy, writes = self._perform_exact_batch_rename(
                    source_dir,
                    [(str(entry["current"]), str(entry["dst_name"])) for entry in rename_entries],
                    prefer_bulk=bulk_enabled,
                )
                strategies.append(strategy)
                write_requests += writes
            reconcile_error = self._reconcile_expected_rename_state(
                source_dir,
                expected_names={str(entry["dst_name"]) for entry in rename_entries},
                absent_names={str(entry["original"]) for entry in rename_entries},
            )
            if reconcile_error:
                raise OpenListError(f"批量改名后对账失败：{reconcile_error}")
            for entry in rename_entries:
                entry["current"] = entry["dst_name"]
                entry["needs_rename"] = False
        else:
            occupied_keys = set(source_keys)
            temp_renames: list[tuple[str, str]] = []
            for entry in rename_entries:
                seed = hashlib.sha256(
                    f"{entry['op'].get('id')}|{entry['original']}|{entry['dst_name']}".encode("utf-8")
                ).hexdigest()[:16]
                extension = posixpath.splitext(str(entry["original"]))[1]
                candidate = f".__fnos_organizer_{seed}{extension}"
                suffix = 0
                while candidate.casefold() in occupied_keys:
                    suffix += 1
                    candidate = f".__fnos_organizer_{seed}_{suffix}{extension}"
                occupied_keys.add(candidate.casefold())
                temp_renames.append((str(entry["current"]), candidate))
                entry["temp_name"] = candidate
                # 第一阶段失败时只能在“原名/本项临时名”之间判断身份。
                # 交叉改名的最终名可能仍属于另一项，不能提前把它当成本项文件。
                entry["reconcile_candidates"] = [candidate, entry["original"]]
            strategy, writes = self._perform_exact_batch_rename(
                source_dir,
                temp_renames,
                prefer_bulk=bulk_enabled,
            )
            strategies.append(f"temp:{strategy}")
            write_requests += writes
            reconcile_error = self._reconcile_expected_rename_state(
                source_dir,
                expected_names={candidate for _old_name, candidate in temp_renames},
                absent_names={str(entry["original"]) for entry in rename_entries},
            )
            if reconcile_error:
                raise OpenListError(f"批量改名第一阶段对账失败：{reconcile_error}")
            for (_old_name, candidate), entry in zip(temp_renames, rename_entries):
                entry["current"] = candidate
                # 第一阶段完整成功后原名均已腾空；第二阶段失败时只在临时名和
                # 本项最终名之间对账，可保持交叉改名的文件身份不串位。
                entry["reconcile_candidates"] = [candidate, entry["dst_name"]]
            strategy, writes = self._perform_exact_batch_rename(
                source_dir,
                [(str(entry["current"]), str(entry["dst_name"])) for entry in rename_entries],
                prefer_bulk=bulk_enabled,
            )
            strategies.append(f"final:{strategy}")
            write_requests += writes
            reconcile_error = self._reconcile_expected_rename_state(
                source_dir,
                expected_names={str(entry["dst_name"]) for entry in rename_entries},
                absent_names={
                    str(value or "")
                    for entry in rename_entries
                    for value in (entry.get("temp_name"), entry.get("original"))
                },
            )
            if reconcile_error:
                raise OpenListError(f"批量改名第二阶段对账失败：{reconcile_error}")
            for entry in rename_entries:
                entry["current"] = entry["dst_name"]
                entry["needs_rename"] = False

        result = {
            "strategy": "+".join(strategies),
            "write_requests": write_requests,
            "count": len(rename_entries),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        logger.info(
            "organizer_bulk_rename source_dir=%s count=%s strategy=%s write_requests=%s duration_seconds=%s",
            source_dir,
            result["count"],
            result["strategy"],
            result["write_requests"],
            result["duration_seconds"],
        )
        return result

    def _reconcile_batch_rename_entries(self, source_dir: str, entries: list[dict[str, Any]]) -> str:
        try:
            names = {item.name for item in self.openlist.list_dir(source_dir, refresh=True)}
        except Exception as exc:  # noqa: BLE001
            return f"改名异常后读取源目录失败：{exc}"
        claimed_names: dict[str, str] = {}
        ambiguous: list[str] = []
        for entry in entries:
            configured = entry.get("reconcile_candidates")
            raw_candidates = configured if isinstance(configured, list) else [
                entry.get("temp_name"),
                entry.get("dst_name"),
                entry.get("original"),
            ]
            candidates = list(dict.fromkeys(str(candidate or "") for candidate in raw_candidates if candidate))
            matches = [candidate for candidate in candidates if candidate in names]
            original = str(entry.get("original") or "")
            if len(matches) != 1:
                entry["current"] = ""
                if len(matches) > 1:
                    ambiguous.append(f"{original}（同时命中 {', '.join(matches)}）")
                continue
            current = matches[0]
            owner = claimed_names.get(current)
            if owner is not None:
                entry["current"] = ""
                ambiguous.append(f"{original}（与 {owner} 同时指向 {current}）")
                continue
            claimed_names[current] = original
            entry["current"] = current
            entry["needs_rename"] = current != str(entry.get("dst_name") or "")
        missing = [str(entry.get("original") or "") for entry in entries if not entry.get("current")]
        messages: list[str] = []
        if ambiguous:
            messages.append(f"改名后文件身份存在冲突：{'；'.join(ambiguous)}")
        if missing:
            messages.append(f"无法定位改名后的文件：{', '.join(missing)}")
        return "；".join(messages)

    def _execute_move_file_batch(
        self,
        ops: list[dict[str, Any]],
        *,
        all_or_nothing: bool = False,
        staging_root: str = "",
    ) -> list[tuple[dict[str, Any], str, str, dict[str, Any] | None]]:
        if not ops:
            return []
        source_dirs = {
            normalize_path(dirname(str(op.get("source_path") or ""))).casefold()
            for op in ops
        }
        target_dirs = {
            normalize_path(dirname(str(op.get("target_path") or ""))).casefold()
            for op in ops
        }
        if len(source_dirs) == 1 and len(target_dirs) == 1:
            return self._execute_single_source_move_file_batch(ops, all_or_nothing=all_or_nothing)
        return self._execute_multi_source_move_file_batch(
            ops,
            all_or_nothing=all_or_nothing,
            staging_root=staging_root,
        )

    def _execute_single_source_move_file_batch(
        self,
        ops: list[dict[str, Any]],
        *,
        all_or_nothing: bool = False,
    ) -> list[tuple[dict[str, Any], str, str, dict[str, Any] | None]]:
        """批量移动同一源目录到同一目标目录的 move_file 操作。

        独立名称直接批量改名；只有大小写变化或名称依赖才经过唯一临时名。移动后
        必须同时成功读取源/目标目录才能标记完成。目录状态未知一律失败待重试。
        """
        if not ops:
            return []
        source_dir = normalize_path(dirname(str(ops[0].get("source_path") or "")))
        target_dir = normalize_path(dirname(str(ops[0].get("target_path") or "")))
        same_dir = source_dir.casefold() == target_dir.casefold()
        results: dict[int, tuple[str, str, dict[str, Any] | None]] = {}

        def finish() -> list[tuple[dict[str, Any], str, str, dict[str, Any] | None]]:
            return [(op, *results.get(id(op), ("failed", "批量移动未生成对账结论", None))) for op in ops]

        def fail_all(message: str) -> list[tuple[dict[str, Any], str, str, dict[str, Any] | None]]:
            for item in ops:
                results[id(item)] = ("failed", message, None)
            return finish()

        for op in ops:
            if (
                normalize_path(dirname(str(op.get("source_path") or ""))).casefold() != source_dir.casefold()
                or normalize_path(dirname(str(op.get("target_path") or ""))).casefold() != target_dir.casefold()
            ):
                return fail_all("批量移动操作的源目录或目标目录不一致")

        source_keys = [basename(str(op.get("source_path") or "")).casefold() for op in ops]
        target_keys = [basename(str(op.get("target_path") or "")).casefold() for op in ops]
        source_key_set = set(source_keys)
        if len(source_key_set) != len(source_keys):
            return fail_all("批量移动包含重复源文件名（忽略大小写），拒绝执行")
        if len(set(target_keys)) != len(target_keys):
            return fail_all("批量移动包含重复目标文件名（忽略大小写），拒绝执行")
        if same_dir and any(source_key != target_key and target_key in source_key_set for source_key, target_key in zip(source_keys, target_keys)):
            # 同目录交换/链式改名的逐条 undo 无法可靠恢复。整体拒绝比把 a、b
            # 两个文件按最终名称互相冒认更安全。
            return fail_all("检测到同目录交叉改名，拒绝批量执行以避免文件内容互换")

        try:
            source_names = {item.name for item in self.openlist.list_dir(source_dir, refresh=True)}
        except Exception as exc:  # noqa: BLE001
            return [(op, "failed", f"批量前读取源目录失败：{exc}", None) for op in ops]
        if same_dir:
            target_names = set(source_names)
        else:
            try:
                target_names = {item.name for item in self.openlist.list_dir(target_dir, refresh=True)}
            except Exception as exc:  # noqa: BLE001
                return [(op, "failed", f"批量前读取目标目录失败：{exc}", None) for op in ops]

        def name_index(names: set[str]) -> dict[str, list[str]]:
            indexed: dict[str, list[str]] = {}
            for name in names:
                indexed.setdefault(name.casefold(), []).append(name)
            return indexed

        source_index = name_index(source_names)
        target_index = name_index(target_names)

        plan: list[dict[str, Any]] = []
        for op in ops:
            source = str(op.get("source_path") or "")
            target = str(op.get("target_path") or "")
            src_name = basename(source)
            dst_name = basename(target)
            src_key = src_name.casefold()
            dst_key = dst_name.casefold()
            source_matches = source_index.get(src_key, [])
            renamed_matches = source_index.get(dst_key, [])
            target_matches = target_index.get(dst_key, [])
            if len(source_matches) > 1 or len(renamed_matches) > 1 or len(target_matches) > 1:
                results[id(op)] = ("failed", f"目录中存在仅大小写不同的同名文件，无法安全判定：{source} -> {target}", None)
                continue
            source_actual = src_name if src_name in source_names else (source_matches[0] if source_matches else "")
            renamed_actual = dst_name if dst_name in source_names else (renamed_matches[0] if renamed_matches else "")
            target_actual = dst_name if dst_name in target_names else (target_matches[0] if target_matches else "")

            if same_dir and src_name == dst_name:
                results[id(op)] = (
                    "skipped" if source_actual else "failed",
                    "源文件已位于目标路径，无需移动" if source_actual else f"源文件不存在：{source}",
                    None,
                )
                continue

            if not same_dir and target_actual:
                source_state_present = bool(source_actual or (dst_key != src_key and renamed_actual))
                if source_state_present:
                    results[id(op)] = ("failed", f"目标已存在且源文件仍在，拒绝覆盖：{source}", None)
                elif dst_name == target_actual:
                    results[id(op)] = ("skipped", "目标已存在且源已消失，视为已完成", None)
                else:
                    results[id(op)] = ("failed", f"目标已存在但大小写不符合预期：{target}", None)
                continue

            if same_dir and src_key == dst_key and src_name != dst_name:
                if dst_name in source_names and src_name not in source_names:
                    results[id(op)] = ("skipped", "仅大小写改名已完成", None)
                    continue
                if not source_actual:
                    results[id(op)] = ("failed", f"源文件不存在：{source}", None)
                    continue
            elif not source_actual:
                if not same_dir and renamed_actual and dst_key not in source_key_set:
                    # 上次执行可能已完成改名、尚未完成批量移动。
                    source_actual = renamed_actual
                elif same_dir and renamed_actual:
                    results[id(op)] = ("skipped", "目标名称已存在且原名称已消失，视为改名完成", None)
                    continue
                else:
                    results[id(op)] = ("failed", f"源文件不存在：{source}", None)
                    continue

            if dst_key != src_key and renamed_actual and renamed_actual != source_actual:
                if dst_key not in source_key_set:
                    results[id(op)] = (
                        "failed",
                        f"源目录中原文件与目标名称文件同时存在，拒绝把其它文件当作改名结果：{source} -> {target}",
                        None,
                    )
                    continue
                # 不同目录的交叉改名由下面的两阶段临时名安全拆开。
            plan.append(
                {
                    "op": op,
                    "src_name": src_name,
                    "dst_name": dst_name,
                    "original": source_actual,
                    "current": source_actual,
                    "needs_rename": source_actual != dst_name,
                }
            )

        active = [entry for entry in plan if id(entry["op"]) not in results]
        if all_or_nothing and any(verdict == "failed" for verdict, _message, _inverse in results.values()):
            for entry in active:
                results[id(entry["op"])] = ("failed", "批量操作预检未全部通过，未执行任何文件变更", None)
            return finish()
        rename_entries = [entry for entry in active if entry["needs_rename"]]
        rename_info: dict[str, Any] = {"strategy": "none", "write_requests": 0, "duration_seconds": 0.0}
        try:
            rename_info = self._apply_adaptive_batch_renames(source_dir, rename_entries, source_names)
        except Exception as exc:  # noqa: BLE001
            reconcile_error = self._reconcile_batch_rename_entries(source_dir, rename_entries)
            rollback_errors = self._restore_batch_renames(rename_entries, source_dir)
            notes = []
            if reconcile_error:
                notes.append(reconcile_error)
            if rollback_errors:
                notes.append(f"回滚异常：{'；'.join(rollback_errors)}")
            elif reconcile_error:
                notes.append("已回滚可确认的改名项，状态未明项未做猜测性修改")
            else:
                notes.append("已回滚本轮改名")
            rollback_note = f"；{'；'.join(notes)}" if notes else ""
            for entry in active:
                results[id(entry["op"])] = ("failed", f"批量改名失败：{exc}{rollback_note}", None)
            return finish()

        move_names = [entry["current"] for entry in active]
        move_error = ""
        move_write_requests = 0
        move_started = time.monotonic()
        if move_names and not same_dir:
            move_write_requests = 1
            try:
                if not self.openlist.move_many(source_dir, target_dir, move_names, overwrite=False, skip_existing=True, merge=True):
                    move_error = "OpenList 批量移动返回未成功"
            except Exception as exc:  # noqa: BLE001
                move_error = str(exc)
                logger.warning("organizer_batch_move_failed source_dir=%s target_dir=%s count=%s message=%s", source_dir, target_dir, len(move_names), exc)

        def read_dir(path: str) -> tuple[set[str] | None, str]:
            try:
                return {item.name for item in self.openlist.list_dir(path, refresh=True)}, ""
            except Exception as exc:  # noqa: BLE001
                return None, str(exc)

        def reconcile() -> tuple[set[str] | None, set[str] | None, str, str]:
            source_after, source_after_error = read_dir(source_dir)
            if same_dir:
                target_after = source_after
                target_after_error = source_after_error
            else:
                target_after, target_after_error = read_dir(target_dir)
            return (source_after, target_after, source_after_error, target_after_error)

        def fully_settled(source_state: set[str], target_state: set[str]) -> bool:
            source_state_index = name_index(source_state)
            for entry in active:
                src_key = str(entry["src_name"]).casefold()
                dst_key = str(entry["dst_name"]).casefold()
                temp_key = str(entry.get("temp_name") or "").casefold()
                temp_remains = bool(temp_key and source_state_index.get(temp_key))
                if same_dir:
                    source_remains = (bool(source_state_index.get(src_key)) and src_key != dst_key) or temp_remains
                else:
                    source_remains = bool(source_state_index.get(src_key) or source_state_index.get(dst_key)) or temp_remains
                if str(entry["dst_name"]) not in target_state or source_remains:
                    return False
            return True

        reconcile_started = time.monotonic()
        timeout_seconds = self._bulk_reconcile_timeout_seconds()
        deadline = reconcile_started + timeout_seconds
        delay_schedule = (1.0, 2.0, 4.0, 8.0)
        attempt = 0
        source_after: set[str] | None = None
        target_after: set[str] | None = None
        source_after_error = ""
        target_after_error = ""
        while True:
            source_after, target_after, source_after_error, target_after_error = reconcile()
            if source_after is not None and target_after is not None and fully_settled(source_after, target_after):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = delay_schedule[min(attempt, len(delay_schedule) - 1)]
            time.sleep(min(delay, remaining))
            attempt += 1

        reconcile_duration = round(time.monotonic() - reconcile_started, 3)
        logger.info(
            "organizer_bulk_move_reconcile source_dir=%s target_dir=%s count=%s source_dirs=%s rename_strategy=%s rename_write_requests=%s move_write_requests=%s total_write_requests=%s move_duration_seconds=%s reconcile_duration_seconds=%s attempts=%s request_error=%s",
            source_dir,
            target_dir,
            len(move_names),
            1,
            rename_info.get("strategy") or "none",
            int(rename_info.get("write_requests") or 0),
            move_write_requests,
            int(rename_info.get("write_requests") or 0) + move_write_requests,
            round(reconcile_started - move_started, 3),
            reconcile_duration,
            attempt + 1,
            move_error,
        )
        if source_after is None or target_after is None:
            detail = "；".join(
                item
                for item in (
                    f"源目录：{source_after_error}" if source_after_error else "",
                    f"目标目录：{target_after_error}" if target_after_error else "",
                )
                if item
            )
            for entry in active:
                results[id(entry["op"])] = ("failed", f"批量移动后对账状态未知，拒绝标记完成（{detail}）", None)
            return finish()

        source_after_index = name_index(source_after)
        target_after_index = name_index(target_after)
        for entry in active:
            op = entry["op"]
            src_key = entry["src_name"].casefold()
            dst_key = entry["dst_name"].casefold()
            temp_key = str(entry.get("temp_name") or "").casefold()
            temp_remains = bool(temp_key and source_after_index.get(temp_key))
            target_exact = entry["dst_name"] in target_after
            if same_dir:
                source_remains = (bool(source_after_index.get(src_key)) and src_key != dst_key) or temp_remains
            else:
                source_remains = bool(source_after_index.get(src_key) or source_after_index.get(dst_key)) or temp_remains
            if target_exact and not source_remains:
                inverse = {"type": "move_file", "source_path": str(op.get("target_path") or ""), "target_path": str(op.get("source_path") or "")}
                results[id(op)] = ("done", "", inverse)
            elif target_exact:
                results[id(op)] = ("failed", f"移动后目标与源同时存在，拒绝自动处理：{op.get('source_path')} -> {op.get('target_path')}", None)
            elif target_after_index.get(dst_key):
                results[id(op)] = ("failed", f"移动后目标文件大小写不符合预期：{op.get('target_path')}", None)
            elif source_remains:
                detail = f"；请求异常：{move_error}" if move_error else ""
                results[id(op)] = (
                    "failed",
                    f"批量移动未生效，源文件仍存在：{op.get('source_path')}{detail}",
                    None,
                )
            else:
                detail = f"；请求异常：{move_error}" if move_error else ""
                results[id(op)] = (
                    "failed",
                    f"批量移动后源文件与目标文件均不存在，状态未知：{op.get('source_path')} -> {op.get('target_path')}{detail}",
                    None,
                )
        done_entries = [entry for entry in active if results.get(id(entry["op"]), ("", "", None))[0] == "done"]
        if len(done_entries) >= 2:
            batch_inverse = {
                "type": "move_file_batch",
                "items": [
                    {
                        "source_path": str(entry["op"].get("target_path") or ""),
                        "target_path": str(entry["op"].get("source_path") or ""),
                    }
                    for entry in done_entries
                ],
            }
            first_op = done_entries[0]["op"]
            first_verdict, first_message, _first_inverse = results[id(first_op)]
            results[id(first_op)] = (first_verdict, first_message, batch_inverse)
            for entry in done_entries[1:]:
                verdict, message, _inverse = results[id(entry["op"])]
                results[id(entry["op"])] = (verdict, message, None)
        done_count = sum(1 for verdict, _message, _inverse in results.values() if verdict == "done")
        skipped_count = sum(1 for verdict, _message, _inverse in results.values() if verdict == "skipped")
        failed_count = sum(1 for verdict, _message, _inverse in results.values() if verdict == "failed")
        logger.info(
            "organizer_bulk_move_result source_dir=%s target_dir=%s count=%s rename_strategy=%s done_count=%s skipped_count=%s failed_count=%s partial_success_count=%s request_error=%s",
            source_dir,
            target_dir,
            len(ops),
            rename_info.get("strategy") or "none",
            done_count,
            skipped_count,
            failed_count,
            done_count if done_count and failed_count else 0,
            move_error,
        )
        return finish()

    def _execute_multi_source_move_file_batch(
        self,
        ops: list[dict[str, Any]],
        *,
        all_or_nothing: bool,
        staging_root: str,
    ) -> list[tuple[dict[str, Any], str, str, dict[str, Any] | None]]:
        if not all_or_nothing and self._bulk_operations_enabled():
            recursive_verdicts = self._try_execute_recursive_move_file_batch(ops, staging_root=staging_root)
            if recursive_verdicts is not None:
                return recursive_verdicts

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for op in ops:
            key = (
                normalize_path(dirname(str(op.get("source_path") or ""))).casefold(),
                normalize_path(dirname(str(op.get("target_path") or ""))).casefold(),
            )
            grouped.setdefault(key, []).append(op)
        verdict_by_id: dict[int, tuple[dict[str, Any], str, str, dict[str, Any] | None]] = {}
        for group in grouped.values():
            verdicts = self._execute_single_source_move_file_batch(group, all_or_nothing=all_or_nothing)
            for verdict in verdicts:
                verdict_by_id[id(verdict[0])] = verdict
            if all_or_nothing and any(verdict[1] == "failed" for verdict in verdicts):
                for pending_group in grouped.values():
                    for op in pending_group:
                        verdict_by_id.setdefault(
                            id(op),
                            (op, "failed", "批量操作未全部通过，停止执行后续目录分组", None),
                        )
                break
        ordered = [
            verdict_by_id.get(id(op), (op, "failed", "多目录批量移动未生成对账结论", None))
            for op in ops
        ]
        if len(ordered) >= 2 and all(verdict == "done" for _op, verdict, _message, _inverse in ordered):
            batch_inverse = {
                "type": "move_file_batch",
                "items": [
                    {
                        "source_path": str(op.get("target_path") or ""),
                        "target_path": str(op.get("source_path") or ""),
                    }
                    for op, _verdict, _message, _inverse in reversed(ordered)
                ],
            }
            first_op = ordered[0][0]
            ordered[0] = (first_op, "done", "", batch_inverse)
            for index in range(1, len(ordered)):
                ordered[index] = (ordered[index][0], "done", "", None)
        return ordered

    def _try_execute_recursive_move_file_batch(
        self,
        ops: list[dict[str, Any]],
        *,
        staging_root: str,
    ) -> list[tuple[dict[str, Any], str, str, dict[str, Any] | None]] | None:
        target_dirs = {normalize_path(dirname(str(op.get("target_path") or ""))) for op in ops}
        source_dirs = {normalize_path(dirname(str(op.get("source_path") or ""))) for op in ops}
        if len(ops) < 2 or len(source_dirs) < 2 or len(target_dirs) != 1:
            return None
        target_keys = [basename(str(op.get("target_path") or "")).casefold() for op in ops]
        if len(set(target_keys)) != len(target_keys):
            return [
                (op, "failed", "聚合移动包含重复目标文件名（忽略大小写），拒绝执行", None)
                for op in ops
            ]
        normalized_staging_root = normalize_path(staging_root) if str(staging_root or "").strip() else ""
        if not normalized_staging_root or normalized_staging_root == "/":
            return None
        for op in ops:
            raw_data = op.get("raw_data") if isinstance(op.get("raw_data"), dict) else {}
            if not _flag_enabled(raw_data.get("staging_file")) or _flag_enabled(raw_data.get("delete_source_if_target_exists")):
                return None
            if not _path_is_same_or_child(normalize_path(str(op.get("source_path") or "")), normalized_staging_root):
                return None

        try:
            common_root = normalize_path(posixpath.commonpath(sorted(source_dirs)))
        except (TypeError, ValueError):
            return None
        if common_root == "/" or not _path_is_same_or_child(common_root, normalized_staging_root):
            return None

        expected_sources = {normalize_path(str(op.get("source_path") or "")).casefold() for op in ops}
        try:
            actual_sources = self._list_openlist_tree_files(common_root, refresh=True)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "organizer_recursive_move_fallback reason=source_inventory_failed root=%s count=%s message=%s",
                common_root,
                len(ops),
                exc,
            )
            return None
        if {path.casefold() for path in actual_sources} != expected_sources:
            logger.info(
                "organizer_recursive_move_fallback reason=source_inventory_mismatch root=%s expected=%s actual=%s",
                common_root,
                len(expected_sources),
                len(actual_sources),
            )
            return None

        target_dir = next(iter(target_dirs))
        try:
            target_names = {item.name for item in self.openlist.list_dir(target_dir, refresh=True)}
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "organizer_recursive_move_fallback reason=target_read_failed target_dir=%s count=%s message=%s",
                target_dir,
                len(ops),
                exc,
            )
            return None
        target_index = {name.casefold() for name in target_names}
        if any(basename(str(op.get("target_path") or "")).casefold() in target_index for op in ops):
            return None

        grouped_entries: dict[str, list[dict[str, Any]]] = {}
        grouped_source_names: dict[str, set[str]] = {}
        for source_dir in source_dirs:
            grouped_source_names[source_dir] = {
                basename(path)
                for path in actual_sources
                if normalize_path(dirname(path)).casefold() == source_dir.casefold()
            }
        for op in ops:
            source_path = normalize_path(str(op.get("source_path") or ""))
            source_dir = normalize_path(dirname(source_path))
            source_name = basename(source_path)
            target_name = basename(str(op.get("target_path") or ""))
            grouped_entries.setdefault(source_dir, []).append(
                {
                    "op": op,
                    "src_name": source_name,
                    "dst_name": target_name,
                    "original": source_name,
                    "current": source_name,
                    "needs_rename": source_name != target_name,
                }
            )

        renamed_groups: list[tuple[str, list[dict[str, Any]]]] = []
        rename_writes = 0
        rename_strategies: list[str] = []
        rename_started = time.monotonic()
        current_group: tuple[str, list[dict[str, Any]]] | None = None

        def restore_renamed_groups(groups: list[tuple[str, list[dict[str, Any]]]]) -> list[str]:
            errors: list[str] = []
            for restore_dir, restore_entries in reversed(groups):
                reconcile_error = self._reconcile_batch_rename_entries(restore_dir, restore_entries)
                if reconcile_error:
                    errors.append(reconcile_error)
                errors.extend(self._restore_batch_renames(restore_entries, restore_dir))
            return errors

        try:
            for source_dir, entries in grouped_entries.items():
                current_group = (source_dir, entries)
                info = self._apply_adaptive_batch_renames(source_dir, entries, grouped_source_names[source_dir])
                rename_writes += int(info.get("write_requests") or 0)
                rename_strategies.append(str(info.get("strategy") or "none"))
                renamed_groups.append(current_group)
                current_group = None
        except Exception as exc:  # noqa: BLE001
            rollback_groups = [*renamed_groups]
            if current_group is not None:
                rollback_groups.append(current_group)
            rollback_errors = restore_renamed_groups(rollback_groups)
            note = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else "；已回滚本轮改名"
            return [(op, "failed", f"聚合移动前批量改名失败：{exc}{note}", None) for op in ops]

        expected_renamed_sources = {
            join_path(source_dir, str(entry["dst_name"])).casefold()
            for source_dir, entries in grouped_entries.items()
            for entry in entries
        }
        post_rename_error = ""
        post_rename_deadline = time.monotonic() + self._bulk_reconcile_timeout_seconds()
        post_rename_delay_schedule = (1.0, 2.0, 4.0, 8.0)
        post_rename_attempt = 0
        while True:
            try:
                renamed_sources = self._list_openlist_tree_files(common_root, refresh=True)
                refreshed_target_names = {item.name for item in self.openlist.list_dir(target_dir, refresh=True)}
                refreshed_target_index = {name.casefold() for name in refreshed_target_names}
                if any(key in refreshed_target_index for key in target_keys):
                    post_rename_error = "批量改名期间目标目录出现同名文件，拒绝继续聚合移动"
                    break
                if {path.casefold() for path in renamed_sources} == expected_renamed_sources:
                    post_rename_error = ""
                    break
                post_rename_error = "批量改名后的递归盘点与计划最终文件集合不一致"
            except Exception as exc:  # noqa: BLE001
                post_rename_error = f"批量改名后安全盘点失败：{exc}"
            remaining = post_rename_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(
                min(
                    post_rename_delay_schedule[min(post_rename_attempt, len(post_rename_delay_schedule) - 1)],
                    remaining,
                )
            )
            post_rename_attempt += 1
        if post_rename_error:
            logger.warning(
                "organizer_recursive_move_aborted reason=post_rename_verification_failed root=%s target_dir=%s count=%s message=%s",
                common_root,
                target_dir,
                len(ops),
                post_rename_error,
            )
            rollback_errors = restore_renamed_groups(renamed_groups)
            note = f"；回滚异常：{'；'.join(rollback_errors)}" if rollback_errors else "；已回滚本轮改名"
            return [(op, "failed", f"聚合移动前验证失败：{post_rename_error}{note}", None) for op in ops]

        move_strategy = "recursive_move"
        move_writes = 0
        move_error = ""
        move_started = time.monotonic()
        recursive_method = getattr(self.openlist, "recursive_move", None)
        try:
            if not callable(recursive_method):
                raise OpenListEndpointUnsupported("OpenList 客户端未提供 recursive_move")
            move_writes += 1
            if not recursive_method(common_root, target_dir, conflict_policy="cancel"):
                move_error = "OpenList 聚合移动返回未成功"
        except OpenListEndpointUnsupported as exc:
            move_strategy = "grouped_move_many_fallback"
            move_error = str(exc)
            for source_dir, entries in grouped_entries.items():
                move_writes += 1
                try:
                    if not self.openlist.move_many(
                        source_dir,
                        target_dir,
                        [str(entry["current"]) for entry in entries],
                        overwrite=False,
                        skip_existing=True,
                        merge=True,
                    ):
                        move_error = f"{move_error}；目录 {source_dir} 批量移动返回未成功"
                except Exception as fallback_exc:  # noqa: BLE001
                    move_error = f"{move_error}；目录 {source_dir}：{fallback_exc}"
                    break
        except Exception as exc:  # noqa: BLE001
            move_error = str(exc)
            logger.warning(
                "organizer_recursive_move_failed root=%s target_dir=%s count=%s message=%s",
                common_root,
                target_dir,
                len(ops),
                exc,
            )

        expected_target_names = {basename(str(op.get("target_path") or "")) for op in ops}
        timeout_seconds = self._bulk_reconcile_timeout_seconds()
        reconcile_started = time.monotonic()
        deadline = reconcile_started + timeout_seconds
        delay_schedule = (1.0, 2.0, 4.0, 8.0)
        attempt = 0
        source_after: set[str] | None = None
        target_after: set[str] | None = None
        source_error = ""
        target_error = ""
        while True:
            try:
                source_after = self._list_openlist_tree_files(common_root, refresh=True, missing_ok=True)
                source_error = ""
            except Exception as exc:  # noqa: BLE001
                source_after = None
                source_error = str(exc)
            try:
                target_after = {item.name for item in self.openlist.list_dir(target_dir, refresh=True)}
                target_error = ""
            except Exception as exc:  # noqa: BLE001
                target_after = None
                target_error = str(exc)
            if (
                source_after is not None
                and target_after is not None
                and not source_after
                and expected_target_names.issubset(target_after)
            ):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(delay_schedule[min(attempt, len(delay_schedule) - 1)], remaining))
            attempt += 1

        logger.info(
            "organizer_recursive_move root=%s target_dir=%s count=%s source_dirs=%s rename_strategy=%s rename_write_requests=%s move_write_requests=%s total_write_requests=%s strategy=%s rename_duration_seconds=%s move_duration_seconds=%s reconcile_duration_seconds=%s attempts=%s request_error=%s",
            common_root,
            target_dir,
            len(ops),
            len(source_dirs),
            "+".join(rename_strategies) or "none",
            rename_writes,
            move_writes,
            rename_writes + move_writes,
            move_strategy,
            round(move_started - rename_started, 3),
            round(reconcile_started - move_started, 3),
            round(time.monotonic() - reconcile_started, 3),
            attempt + 1,
            move_error,
        )

        if source_after is None or target_after is None:
            detail = "；".join(item for item in (source_error, target_error) if item)
            return [(op, "failed", f"聚合移动后对账状态未知：{detail}", None) for op in ops]

        remaining_index = {path.casefold() for path in source_after}
        target_case_index = {name.casefold() for name in target_after}
        results: list[tuple[dict[str, Any], str, str, dict[str, Any] | None]] = []
        done_positions: list[int] = []
        for op in ops:
            source_dir = normalize_path(dirname(str(op.get("source_path") or "")))
            original_path = normalize_path(str(op.get("source_path") or ""))
            renamed_path = join_path(source_dir, basename(str(op.get("target_path") or "")))
            target_path = normalize_path(str(op.get("target_path") or ""))
            target_name = basename(target_path)
            source_remains = original_path.casefold() in remaining_index or renamed_path.casefold() in remaining_index
            if target_name in target_after and not source_remains:
                done_positions.append(len(results))
                results.append(
                    (
                        op,
                        "done",
                        "",
                        {"type": "move_file", "source_path": target_path, "target_path": original_path},
                    )
                )
            elif target_name in target_after:
                results.append((op, "failed", f"聚合移动后目标与源同时存在：{original_path} -> {target_path}", None))
            elif target_name.casefold() in target_case_index:
                results.append((op, "failed", f"聚合移动后目标文件大小写不符合预期：{target_path}", None))
            elif source_remains:
                detail = f"；请求异常：{move_error}" if move_error else ""
                results.append((op, "failed", f"聚合移动未生效，源文件仍存在：{original_path}{detail}", None))
            else:
                detail = f"；请求异常：{move_error}" if move_error else ""
                results.append(
                    (
                        op,
                        "failed",
                        f"聚合移动后源文件与目标文件均不存在，状态未知：{original_path} -> {target_path}{detail}",
                        None,
                    )
                )

        if len(done_positions) >= 2:
            batch_inverse = {
                "type": "move_file_batch",
                "items": [
                    {
                        "source_path": str(results[index][0].get("target_path") or ""),
                        "target_path": str(results[index][0].get("source_path") or ""),
                    }
                    for index in reversed(done_positions)
                ],
            }
            first = done_positions[0]
            results[first] = (results[first][0], "done", "", batch_inverse)
            for index in done_positions[1:]:
                results[index] = (results[index][0], "done", "", None)
        done_count = sum(1 for _op, verdict, _message, _inverse in results if verdict == "done")
        failed_count = sum(1 for _op, verdict, _message, _inverse in results if verdict == "failed")
        logger.info(
            "organizer_recursive_move_result root=%s target_dir=%s count=%s source_dirs=%s strategy=%s done_count=%s failed_count=%s partial_success_count=%s request_error=%s",
            common_root,
            target_dir,
            len(ops),
            len(source_dirs),
            move_strategy,
            done_count,
            failed_count,
            done_count if done_count and failed_count else 0,
            move_error,
        )
        return results

    def _list_openlist_tree_files(
        self,
        root_path: str,
        *,
        refresh: bool,
        missing_ok: bool = False,
        limit: int | None = None,
    ) -> set[str]:
        root = normalize_path(root_path)
        queue: list[tuple[str, int]] = [(root, 0)]
        visited: set[str] = set()
        files: set[str] = set()
        if limit is None:
            try:
                configured_limit = int((getattr(self, "organizer_config", {}) or {}).get("max_files_per_task", 500))
            except (TypeError, ValueError):
                configured_limit = 500
            file_limit = max(1, min(configured_limit, 5000))
        else:
            file_limit = max(1, min(int(limit), 20000))
        stop_at = file_limit + 1
        while queue:
            current, depth = queue.pop(0)
            folded = current.casefold()
            if folded in visited:
                continue
            visited.add(folded)
            try:
                items = self.openlist.list_dir(current, refresh=refresh and depth == 0)
            except Exception:
                if missing_ok and current == root:
                    try:
                        if not self.openlist.exists(root):
                            return set()
                    except Exception:  # noqa: BLE001
                        pass
                raise
            for item in items:
                item_path = normalize_path(getattr(item, "path", "") or join_path(current, getattr(item, "name", "")))
                if bool(getattr(item, "is_dir", False)):
                    queue.append((item_path, depth + 1))
                    continue
                files.add(item_path)
                if len(files) >= stop_at:
                    raise RuntimeError(f"递归盘点文件数超过安全上限：{file_limit}")
        return files

    def _restore_batch_renames(self, entries: list[dict[str, Any]], source_dir: str) -> list[str]:
        """Best-effort two-phase restore for a failed pre-move rename group."""

        errors: list[str] = []
        recovery: list[dict[str, Any]] = []
        occupied = {
            str(value).casefold()
            for entry in entries
            for value in (entry.get("original"), entry.get("current"), entry.get("dst_name"), entry.get("temp_name"))
            if value
        }
        for index, entry in enumerate(entries):
            current = str(entry.get("current") or "")
            original = str(entry.get("original") or "")
            if not current or not original or current == original:
                continue
            seed = hashlib.sha256(f"restore|{index}|{current}|{original}".encode("utf-8")).hexdigest()[:16]
            extension = posixpath.splitext(original)[1]
            temp_name = f".__fnos_restore_{seed}{extension}"
            suffix = 0
            while temp_name.casefold() in occupied:
                suffix += 1
                temp_name = f".__fnos_restore_{seed}_{suffix}{extension}"
            occupied.add(temp_name.casefold())
            try:
                self.openlist.rename(join_path(source_dir, current), temp_name, overwrite=False)
                entry["current"] = temp_name
                recovery.append(entry)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{current} 暂存失败：{exc}")
        for entry in recovery:
            try:
                self.openlist.rename(
                    join_path(source_dir, str(entry.get("current") or "")),
                    str(entry.get("original") or ""),
                    overwrite=False,
                )
                entry["current"] = entry["original"]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{entry.get('current')} 恢复为 {entry.get('original')} 失败：{exc}")
        return errors

    def _rename_file_case_safely(self, source: str, target: str) -> bool:
        """Rename only filename casing through a temporary name.

        Returns ``False`` when the exact target spelling is already visible.
        """

        source_dir = normalize_path(dirname(source))
        if source_dir.casefold() != normalize_path(dirname(target)).casefold():
            raise RuntimeError("仅大小写改名要求源文件与目标文件位于同一目录")
        source_name = basename(source)
        target_name = basename(target)
        if source_name.casefold() != target_name.casefold() or source_name == target_name:
            raise RuntimeError("仅大小写改名参数无效")
        names = {item.name for item in self.openlist.list_dir(source_dir, refresh=True)}
        source_exact = source_name in names
        target_exact = target_name in names
        if source_exact and target_exact:
            raise RuntimeError(f"源目录同时存在仅大小写不同的文件，拒绝改名：{source} -> {target}")
        if target_exact:
            return False
        if not source_exact:
            raise RuntimeError(f"源文件不存在或实际大小写不一致：{source}")
        occupied = {name.casefold() for name in names}
        seed = hashlib.sha256(f"case|{source_name}|{target_name}".encode("utf-8")).hexdigest()[:16]
        extension = posixpath.splitext(source_name)[1]
        temp_name = f".__fnos_case_{seed}{extension}"
        suffix = 0
        while temp_name.casefold() in occupied:
            suffix += 1
            temp_name = f".__fnos_case_{seed}_{suffix}{extension}"
        temp_path = join_path(source_dir, temp_name)
        self.openlist.rename(source, temp_name, overwrite=False)
        try:
            self.openlist.rename(temp_path, target_name, overwrite=False)
        except Exception:
            try:
                self.openlist.rename(temp_path, source_name, overwrite=False)
            except Exception:  # noqa: BLE001
                logger.exception("organizer_case_rename_rollback_failed source=%s target=%s", source, target)
            raise
        after = {item.name for item in self.openlist.list_dir(source_dir, refresh=True)}
        if target_name not in after or source_name in after:
            raise RuntimeError(f"仅大小写改名后未确认到精确目标名称：{target}")
        return True

    def _execute_split_target_batch_inverse(self, reverse_ops: list[dict[str, Any]]) -> None:
        """安全回滚“一个正式目录 -> 多个原目录”的批量移动。

        所有正式文件先进入唯一临时名，再按原目录分组恢复名称并移动。这样原始名称
        与另一项正式名称交叉、甚至形成循环时，也不会因分组执行顺序而互相占位。
        """

        source_dir = normalize_path(dirname(str(reverse_ops[0].get("source_path") or "")))
        try:
            source_names = {item.name for item in self.openlist.list_dir(source_dir, refresh=True)}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"批量回滚前读取源目录失败：{exc}") from exc

        source_index: dict[str, list[str]] = {}
        for name in source_names:
            source_index.setdefault(name.casefold(), []).append(name)
        planned_source_keys = {
            basename(str(op.get("source_path") or "")).casefold()
            for op in reverse_ops
        }
        if len(planned_source_keys) != len(reverse_ops):
            raise RuntimeError("批量回滚包含重复源文件名（忽略大小写）")

        target_names_by_dir: dict[str, set[str]] = {}
        target_keys_by_dir: dict[str, set[str]] = {}
        planned_target_paths: set[str] = set()
        for op in reverse_ops:
            source_name = basename(str(op.get("source_path") or ""))
            matches = source_index.get(source_name.casefold(), [])
            if source_name not in source_names:
                if matches:
                    raise RuntimeError(f"批量回滚源文件大小写不符合预期：{join_path(source_dir, matches[0])}")
                raise RuntimeError(f"批量回滚源文件不存在：{join_path(source_dir, source_name)}")

            target_path = normalize_path(str(op.get("target_path") or ""))
            target_key = target_path.casefold()
            if target_key in planned_target_paths:
                raise RuntimeError(f"批量回滚包含重复目标路径：{target_path}")
            planned_target_paths.add(target_key)
            target_dir = normalize_path(dirname(target_path))
            if target_dir not in target_names_by_dir:
                try:
                    names = {item.name for item in self.openlist.list_dir(target_dir, refresh=True)}
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"批量回滚前读取目标目录失败：{target_dir}：{exc}") from exc
                target_names_by_dir[target_dir] = names
                target_keys_by_dir[target_dir] = {name.casefold() for name in names}
            target_name = basename(target_path)
            target_name_key = target_name.casefold()
            target_is_planned_source = target_dir.casefold() == source_dir.casefold() and target_name_key in planned_source_keys
            if target_name in target_names_by_dir[target_dir] and not target_is_planned_source:
                raise RuntimeError(f"批量回滚目标已存在，不覆盖：{target_path}")
            if target_name_key in target_keys_by_dir[target_dir] and target_name not in target_names_by_dir[target_dir] and not target_is_planned_source:
                raise RuntimeError(f"批量回滚目标存在仅大小写冲突：{target_path}")

        occupied_keys = {name.casefold() for name in source_names}
        entries: list[dict[str, Any]] = []
        temp_renames: list[tuple[str, str]] = []
        for index, op in enumerate(reverse_ops):
            source_name = basename(str(op.get("source_path") or ""))
            extension = posixpath.splitext(source_name)[1]
            seed = hashlib.sha256(
                f"batch-undo|{index}|{op.get('source_path')}|{op.get('target_path')}".encode("utf-8")
            ).hexdigest()[:16]
            temp_name = f".__fnos_batch_undo_{seed}{extension}"
            suffix = 0
            while temp_name.casefold() in occupied_keys:
                suffix += 1
                temp_name = f".__fnos_batch_undo_{seed}_{suffix}{extension}"
            occupied_keys.add(temp_name.casefold())
            temp_renames.append((source_name, temp_name))
            entries.append(
                {
                    "op": op,
                    "src_name": source_name,
                    "dst_name": source_name,
                    "original": source_name,
                    "current": source_name,
                    "temp_name": temp_name,
                    "needs_rename": True,
                    "reconcile_candidates": [temp_name, source_name],
                }
            )

        try:
            self._perform_exact_batch_rename(
                source_dir,
                temp_renames,
                prefer_bulk=self._bulk_operations_enabled(),
            )
            reconcile_error = self._reconcile_expected_rename_state(
                source_dir,
                expected_names={temp_name for _source_name, temp_name in temp_renames},
                absent_names={source_name for source_name, _temp_name in temp_renames},
            )
            if reconcile_error:
                raise OpenListError(f"批量回滚暂存阶段对账失败：{reconcile_error}")
        except Exception as exc:  # noqa: BLE001
            state_error = self._reconcile_batch_rename_entries(source_dir, entries)
            restore_errors = self._restore_batch_renames(entries, source_dir)
            details = [item for item in (state_error, "；".join(restore_errors)) if item]
            note = f"；恢复异常：{'；'.join(details)}" if details else "；已恢复暂存改名"
            raise RuntimeError(f"批量回滚暂存失败：{exc}{note}") from exc

        staged_ops: list[dict[str, Any]] = []
        entry_by_op_id: dict[int, dict[str, Any]] = {}
        for entry in entries:
            original_op = entry["op"]
            staged_op = {
                **original_op,
                "source_path": join_path(source_dir, str(entry["temp_name"])),
            }
            staged_ops.append(staged_op)
            entry_by_op_id[id(staged_op)] = entry

        try:
            verdicts = self._execute_move_file_batch(staged_ops, all_or_nothing=True)
        except Exception as exc:  # noqa: BLE001
            for staged_op, entry in zip(staged_ops, entries):
                entry["reconcile_candidates"] = [
                    entry.get("temp_name"),
                    basename(str(staged_op.get("target_path") or "")),
                    entry.get("original"),
                ]
            state_error = self._reconcile_batch_rename_entries(source_dir, entries)
            restore_errors = self._restore_batch_renames(entries, source_dir)
            details = [item for item in (state_error, "；".join(restore_errors)) if item]
            note = f"；恢复异常：{'；'.join(details)}" if details else "；已恢复未移动文件"
            raise RuntimeError(f"批量文件回滚执行异常：{exc}{note}") from exc
        failures = [(op, message) for op, verdict, message, _inverse in verdicts if verdict == "failed"]
        if not failures:
            return

        restore_entries: list[dict[str, Any]] = []
        failed_ids = {id(op) for op, _message in failures}
        for staged_op in staged_ops:
            if id(staged_op) not in failed_ids:
                continue
            entry = entry_by_op_id[id(staged_op)]
            entry["reconcile_candidates"] = [
                entry.get("temp_name"),
                basename(str(staged_op.get("target_path") or "")),
                entry.get("original"),
            ]
            restore_entries.append(entry)
        state_error = self._reconcile_batch_rename_entries(source_dir, restore_entries) if restore_entries else ""
        restore_errors = self._restore_batch_renames(restore_entries, source_dir) if restore_entries else []
        details = [message for _op, message in failures]
        if state_error:
            details.append(state_error)
        details.extend(restore_errors)
        raise RuntimeError(f"批量文件回滚未全部完成：{'；'.join(details)}")

    def _execute_inverse(self, op: dict[str, Any]) -> None:
        op_type = str(op.get("type") or "")
        if op_type == "move_file_batch":
            items = op.get("items") if isinstance(op.get("items"), list) else []
            reverse_ops = [
                {
                    "id": index,
                    "type": "move_file",
                    "status": "pending",
                    "source_path": str(item.get("source_path") or ""),
                    "target_path": str(item.get("target_path") or ""),
                    "raw_data": {"staging_file": True, "fail_if_target_exists": True},
                }
                for index, item in enumerate(items, start=1)
                if isinstance(item, dict)
            ]
            if not reverse_ops or len(reverse_ops) != len(items):
                raise RuntimeError("批量回滚数据为空或格式不正确")
            source_dirs = {
                normalize_path(dirname(str(reverse_op.get("source_path") or ""))).casefold()
                for reverse_op in reverse_ops
            }
            target_dirs = {
                normalize_path(dirname(str(reverse_op.get("target_path") or ""))).casefold()
                for reverse_op in reverse_ops
            }
            if len(source_dirs) == 1 and len(target_dirs) > 1:
                self._execute_split_target_batch_inverse(reverse_ops)
                return
            verdicts = self._execute_move_file_batch(reverse_ops, all_or_nothing=True)
            failures = [message for _item, verdict, message, _inverse in verdicts if verdict == "failed"]
            if failures:
                raise RuntimeError(f"批量文件回滚未全部完成：{'；'.join(failures)}")
            return
        if op_type == "move_file":
            source = str(op.get("source_path") or "")
            target = str(op.get("target_path") or "")
            if (
                normalize_path(dirname(source)).casefold() == normalize_path(dirname(target)).casefold()
                and basename(source) != basename(target)
                and basename(source).casefold() == basename(target).casefold()
            ):
                self._rename_file_case_safely(source, target)
                return
            if self.openlist.exists(target):
                raise RuntimeError(f"回滚目标已存在，不覆盖：{target}")
            if not self.openlist.exists(source):
                raise RuntimeError(f"回滚源不存在：{source}")
            if basename(source) != basename(target):
                renamed = join_path(dirname(source), basename(target))
                self.openlist.rename(source, basename(target), overwrite=False)
                source = renamed
            self.openlist.move(source, dirname(target), overwrite=False, skip_existing=True, merge=True)
        elif op_type == "remove_empty_directory":
            self.openlist.remove_empty_directory(str(op.get("target_path") or ""))

    def _refresh_fnos_if_needed(self, task_id: int) -> bool:
        organizer_config = getattr(self, "organizer_config", {}) or {}
        fnos = getattr(self, "fnos", None)
        if not fnos or not organizer_config.get("refresh_fnos_after_apply"):
            return False
        task = self.db.get_organizer_task(task_id, include_children=False) or {}
        category = (getattr(self, "categories", {}) or {}).get(str(task.get("category") or ""), {})
        dir_list = category.get("strm_fnos_dir_list") or []
        if not dir_list:
            logger.info("organizer_fnos_refresh_skipped task_id=%s reason=no_dir_list", task_id)
            return False
        library = str(category.get("fnos_lib") or category.get("label") or "").strip()
        try:
            delay_seconds = max(0, int(organizer_config.get("refresh_delay_seconds") or 0))
        except (TypeError, ValueError):
            delay_seconds = 0
        scheduled_dirs = list(dir_list) if isinstance(dir_list, (list, tuple)) else dir_list

        def run() -> None:
            try:
                if delay_seconds:
                    time.sleep(delay_seconds)
                result = fnos.refresh(library, dir_list=scheduled_dirs)
                if isinstance(result, dict) and result.get("success") is False:
                    logger.warning(
                        "organizer_fnos_refresh_failed task_id=%s library=%s message=%s",
                        task_id,
                        library,
                        result.get("message") or "",
                    )
                else:
                    logger.info("organizer_fnos_refresh_done task_id=%s library=%s", task_id, library)
            except Exception:  # noqa: BLE001
                logger.exception("organizer_fnos_refresh_exception task_id=%s library=%s", task_id, library)

        threading.Thread(target=run, name=f"organizer-fnos-refresh-{task_id}", daemon=True).start()
        logger.info(
            "organizer_fnos_refresh_scheduled task_id=%s library=%s delay_seconds=%s",
            task_id,
            library,
            delay_seconds,
        )
        return True

    def _root_path_from_target(self, category_key: str, target_path: str) -> str:
        path = normalize_path(target_path)
        parent = dirname(path)
        if category_key in EPISODIC_CATEGORIES and basename(parent).lower().startswith("season "):
            return dirname(parent)
        return parent

    def _category_from_label_or_path(self, label: str, target_path: str) -> str:
        wanted = str(label or "").strip()
        for key, item in self.categories.items():
            if wanted and wanted in {str(item.get("label") or ""), str(item.get("fnos_lib") or "")}:
                return key
        normalized = normalize_path(target_path)
        matches: list[tuple[int, int, str]] = []
        for key, item in self.categories.items():
            for field in ("openlist_root_path", "mobile_openlist_root_path", "mobile_target_path", "cloud139_fnos_target_path", "sixpan_fnos_target_path", "cmcc_parent_path"):
                root = normalize_path(item.get(field) or "")
                if root != "/" and _path_is_same_or_child(normalized, root):
                    matches.append((root.count("/"), len(root), key))
        if matches:
            return max(matches)[2]
        parts = [part for part in normalized.strip("/").split("/") if part]
        anchor_matches: list[tuple[int, int, str]] = []
        for key, item in self.categories.items():
            anchors = _category_path_anchors(item)
            for index, part in enumerate(parts):
                if part.casefold() in anchors:
                    anchor_matches.append((-index, len(part), key))
                    break
        if anchor_matches:
            return max(anchor_matches)[2]
        return "movie"

    def _lock_keys(self, task: dict[str, Any]) -> list[str]:
        keys = {f"category:{task.get('category')}", f"root:{normalize_path(task.get('openlist_root_path'))}"}
        for op in task.get("operations") or []:
            if op.get("target_path"):
                keys.add(f"target:{dirname(op['target_path'])}")
            if op.get("source_path"):
                keys.add(f"source:{dirname(op['source_path'])}")
        return sorted(keys)


class SkipOperation(RuntimeError):
    pass


class OrganizerLockTimeout(RuntimeError):
    pass


def _local_strm_old_name_variants(old_name: Any) -> list[str]:
    raw = str(old_name or "").strip()
    variants: list[str] = []
    decoded = unquote(raw)
    for value in (raw, decoded, raw.replace(" ", "%20"), decoded.replace(" ", "%20"), raw.replace("%20", " "), decoded.replace("%20", " ")):
        value = str(value or "").strip()
        if value and value not in variants:
            variants.append(value)
    return variants


def _dedupe_repeated_tail_path(path: str) -> str:
    normalized = normalize_path(path)
    parts = [part for part in normalized.strip("/").split("/") if part]
    while len(parts) >= 2 and parts[-1] == parts[-2]:
        parts.pop()
    return normalize_path("/" + "/".join(parts)) if parts else "/"


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _strm_cleanup_message(openlist_cleanup: dict[str, Any], local_cleanup: dict[str, Any]) -> str:
    parts = []
    for item in (local_cleanup, openlist_cleanup):
        message = str(item.get("message") or "").strip()
        if message:
            parts.append(message)
    return "\uff1b".join(parts) if parts else "\u65e7 STRM \u76ee\u5f55\u6e05\u7406\u5b8c\u6210"


def _normalize_match_text(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().strip("/").casefold()


def _confidence_percent(value: Any) -> float:
    confidence = float(value)
    if 0 <= confidence <= 1:
        confidence *= 100
    return max(0, min(100, confidence))


def _mapping_version(path: Any, parsed_version: Any = "") -> str:
    version = str(parsed_version or "").strip()
    # 多版本通常体现在直接父目录（1080P/4K）或文件名，资源根目录里的“4K+1080P”
    # 只是资源说明，不能作为每个文件的版本后缀。
    path_version = extract_version_tags(basename(dirname(path)))
    tokens = _dedupe_texts([*(version.split() if version else []), *(path_version.split() if path_version else [])])
    return " ".join(tokens[:4])


def _should_auto_delete_ad_file(category_key: str, media_type: str, parsed_item: Any, size: Any) -> bool:
    if str(category_key or "") not in EPISODIC_CATEGORIES and str(media_type or "") != "tv":
        return False
    if getattr(parsed_item, "episode", None) is not None:
        return False
    try:
        size_value = int(size)
    except (TypeError, ValueError):
        return False
    return 0 < size_value < AD_FILE_DELETE_THRESHOLD_BYTES


def _size_text(size: Any) -> str:
    try:
        value = int(size)
    except (TypeError, ValueError):
        return "未知大小"
    if value >= 1024 * 1024 * 1024:
        return f"{value / 1024 / 1024 / 1024:.2f}GB"
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.2f}MB"
    if value >= 1024:
        return f"{value / 1024:.2f}KB"
    return f"{value}B"


def _mapping_problem_summary(mappings: list[dict[str, Any]], confidence: float, ai_suggestion: dict[str, Any]) -> dict[str, Any]:
    need_episode = [item for item in mappings if item.get("status") == "need_edit"]
    conflicts = [item for item in mappings if item.get("status") == "conflict"]
    auto_delete = [item for item in mappings if item.get("status") == "delete_ad"]
    issues: list[dict[str, Any]] = []
    if need_episode:
        issues.append({"type": "need_episode", "count": len(need_episode), "message": f"{len(need_episode)} 个文件没有识别出集数，需要补季/集或目标路径"})
    if conflicts:
        issues.append({"type": "conflict", "count": len(conflicts), "message": f"{len(conflicts)} 个文件目标路径冲突，需要确认是否同集多版本或改目标路径"})
    if auto_delete:
        issues.append({"type": "auto_delete_ad", "count": len(auto_delete), "message": f"{len(auto_delete)} 个未识别集数且小于 50MB 的广告小文件已自动彻底删除，无需人工处理"})
    if confidence < 75 or ai_suggestion.get("requires_review"):
        issues.append({"type": "low_confidence", "count": 1, "message": "标题/TMDB/AI 置信度偏低，建议确认标准标题、年份和季号"})
    return {"count": sum(int(item.get("count") or 0) for item in issues), "issues": issues}


def _episode_completeness_report(category_key: str, mappings: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a non-blocking completeness report from the generated video mappings.

    Missing episodes are deliberately limited to holes inside each season's
    observed minimum/maximum span.  The report therefore never invents an
    expected season length and is informational only.
    """

    normalized_category = str(category_key or "").strip().lower()
    if normalized_category not in EPISODIC_CATEGORIES:
        return {}

    episode_files: dict[int, dict[int, list[dict[str, Any]]]] = {}
    unrecognized_files: list[dict[str, Any]] = []
    total_video_count = 0
    advertisement_count = 0
    companion_file_count = 0
    ignored_video_count = 0

    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        raw_data = mapping.get("raw_data") if isinstance(mapping.get("raw_data"), dict) else {}
        if _flag_enabled(raw_data.get("companion_file")):
            companion_file_count += 1
            continue

        source_path = str(mapping.get("source_path") or "").strip()
        source_name = str(mapping.get("source_name") or "").strip()
        extension_source = source_name or source_path
        if posixpath.splitext(extension_source.replace("\\", "/"))[1].lower() not in VIDEO_EXTENSIONS:
            continue

        total_video_count += 1
        status = str(mapping.get("status") or "").strip().lower()
        if status == "delete_ad" or _flag_enabled(raw_data.get("auto_delete_ad")):
            advertisement_count += 1
            continue
        if status in {"ignored", "skip", "skipped", "cancelled"} or _flag_enabled(raw_data.get("ignored")):
            ignored_video_count += 1
            continue

        episode = _safe_positive_int(mapping.get("episode"))
        if episode is None:
            unrecognized_files.append(
                {
                    "name": source_name or posixpath.basename(source_path.replace("\\", "/")),
                    "path": source_path,
                    "status": status,
                    "reason": mapping.get("reason") or [],
                }
            )
            continue

        season = _optional_non_negative_int(mapping.get("season"))
        if season is None:
            season = 1
        episode_files.setdefault(season, {}).setdefault(episode, []).append(
            {
                "name": source_name or posixpath.basename(source_path.replace("\\", "/")),
                "path": source_path,
                "target_path": str(mapping.get("target_path") or "").strip(),
                "status": status,
            }
        )

    seasons: list[dict[str, Any]] = []
    recognized_file_count = 0
    recognized_episode_count = 0
    missing_count = 0
    duplicate_count = 0
    duplicate_file_count = 0
    for season_number in sorted(episode_files):
        by_episode = episode_files[season_number]
        episodes = sorted(by_episode)
        if not episodes:
            continue
        min_episode = episodes[0]
        max_episode = episodes[-1]
        observed = set(episodes)
        missing_episodes = [episode for episode in range(min_episode, max_episode + 1) if episode not in observed]
        duplicates: list[dict[str, Any]] = []
        for episode in episodes:
            file_details = sorted(by_episode[episode], key=lambda item: (str(item.get("path") or "").casefold(), str(item.get("name") or "").casefold()))
            if len(file_details) > 1:
                duplicates.append(
                    {
                        "episode": episode,
                        "count": len(file_details),
                        "files": [str(item.get("name") or item.get("path") or "") for item in file_details],
                        "file_paths": [str(item.get("path") or "") for item in file_details],
                    }
                )
                duplicate_file_count += len(file_details) - 1
        file_count = sum(len(items) for items in by_episode.values())
        recognized_file_count += file_count
        recognized_episode_count += len(episodes)
        missing_count += len(missing_episodes)
        duplicate_count += len(duplicates)
        seasons.append(
            {
                "season": season_number,
                "label": f"S{season_number:02d}",
                "is_special": season_number == 0,
                "file_count": file_count,
                "episode_count": len(episodes),
                "min_episode": min_episode,
                "max_episode": max_episode,
                "ranges": _episode_range_labels(episodes),
                "episodes": episodes,
                "missing_episodes": missing_episodes,
                "duplicates": duplicates,
            }
        )

    special_episodes = sorted(episode_files.get(0, {}))
    unrecognized_files.sort(key=lambda item: (str(item.get("path") or "").casefold(), str(item.get("name") or "").casefold()))
    message_parts = [f"识别到 {recognized_episode_count} 集（{len(seasons)} 个季/特别篇）"]
    if missing_count:
        message_parts.append(f"观察范围内缺失 {missing_count} 集")
    if duplicate_count:
        message_parts.append(f"重复 {duplicate_count} 集")
    if special_episodes:
        message_parts.append(f"特别篇 {len(special_episodes)} 集")
    if unrecognized_files:
        message_parts.append(f"未识别 {len(unrecognized_files)} 个视频")
    message = "，".join(message_parts) + "；缺失仅按各季已识别最小/最大集数之间计算，不据此阻断整理"
    return {
        "enabled": True,
        "category": normalized_category,
        "basis": "observed_span_only",
        "total_video_count": total_video_count,
        "recognized_count": recognized_file_count,
        "recognized_file_count": recognized_file_count,
        "recognized_episode_count": recognized_episode_count,
        "unrecognized_count": len(unrecognized_files),
        "unrecognized_video_count": len(unrecognized_files),
        "unrecognized_files": unrecognized_files,
        "special_count": len(special_episodes),
        "special_episodes": special_episodes,
        "missing_count": missing_count,
        "duplicate_count": duplicate_count,
        "duplicate_file_count": duplicate_file_count,
        "seasons": seasons,
        "excluded": {
            "advertisement_count": advertisement_count,
            "companion_file_count": companion_file_count,
            "ignored_video_count": ignored_video_count,
        },
        "message": message,
    }


def _episode_range_labels(episodes: list[int]) -> list[str]:
    values = sorted(set(episodes))
    if not values:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = values[0]
    for episode in values[1:]:
        if episode == previous + 1:
            previous = episode
            continue
        ranges.append((start, previous))
        start = previous = episode
    ranges.append((start, previous))
    return [f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}" for start, end in ranges]


def _first_not_none(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _safe_positive_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_non_negative_int(value: Any) -> int:
    try:
        number = int(str(value or 0).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_after_seconds(seconds: int | float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, float(seconds or 0)))).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _remaining_retry_delay_seconds(value: Any, *, fallback: int = 5) -> int:
    text = str(value or "").strip()
    if not text:
        return max(0, int(fallback))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return max(0, int(fallback))
    remaining = (parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(math.ceil(remaining)))


def _timestamp_after_seconds(value: Any, seconds: int | float) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return _utc_after_seconds(seconds)
    return (
        parsed.astimezone(timezone.utc) + timedelta(seconds=max(0, float(seconds or 0)))
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _remaining_window_delay_seconds(value: Any, seconds: int | float) -> int:
    deadline = _timestamp_after_seconds(value, seconds)
    return _remaining_retry_delay_seconds(deadline, fallback=max(0, int(seconds or 0)))


def _merge_raw_data(current: Any, patch: dict[str, Any]) -> dict[str, Any]:
    base = dict(current) if isinstance(current, dict) else {}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge_raw_data(base[key], value)
        else:
            base[key] = value
    return base


def _stringify_message(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        issues = value.get("issues") if isinstance(value.get("issues"), list) else []
        messages = [str(item.get("message") or "").strip() for item in issues if isinstance(item, dict) and str(item.get("message") or "").strip()]
        if messages:
            return "；".join(messages)
    return str(value or "").strip() or "需要人工确认"


def _common_parent_dir(paths: list[str]) -> str:
    parents = [dirname(path) for path in paths if str(path or "").strip()]
    if not parents:
        return ""
    split_paths = [normalize_path(path).strip("/").split("/") for path in parents]
    common: list[str] = []
    for parts in zip(*split_paths):
        first = parts[0]
        if all(part == first for part in parts):
            common.append(first)
        else:
            break
    return normalize_path("/" + "/".join(common)) if common else "/"


def _create_dir_chain(target_dir: str, stop_root: str) -> list[str]:
    target = normalize_path(target_dir)
    stop = normalize_path(stop_root)
    if not target or target == "/" or target == stop:
        return []
    if stop and stop != "/" and not _path_is_same_or_child(target, stop):
        return []
    chain: list[str] = []
    current = target
    while current and current != "/" and current != stop:
        chain.append(current)
        parent = dirname(current)
        if parent == current:
            break
        current = parent
    return list(reversed(chain))


def _is_explicit_season_name(value: Any) -> bool:
    text = basename(str(value or "").replace("\\", "/").strip()).strip()
    if not text:
        return False
    return bool(
        re.fullmatch(r"(?i)season\s*0*\d{1,2}", text)
        or re.fullmatch(r"(?i)s0*\d{1,2}", text)
        or re.fullmatch(r"第\s*(?:\d{1,2}|[零〇一二两三四五六七八九十百]+)\s*季", text)
    )


def _resource_root_from_task_context(value: Any) -> str:
    if not str(value or "").strip():
        return ""
    root = normalize_path(value)
    while root and root != "/" and _is_explicit_season_name(basename(root)):
        parent = dirname(root)
        if not parent or parent == root:
            break
        root = normalize_path(parent)
    return root


def _clean_title_hint(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _is_explicit_season_name(text):
        return ""
    if _looks_like_virtual_path(text):
        text = basename(text.replace("\\", "/"))
    else:
        # 资源标题常用“国语/粤语”“简中/繁中”描述版本，斜杠不是目录分隔符。
        # 旧逻辑对任意含 / 的文本调用 basename，导致“藏海…/6音轨”只剩“6音轨”。
        text = text.replace("/", " ").replace("\\", " ")
    text_without_release_group = re.sub(r"^[\s【\[]*[^】\]]*(?:发布|高清影视|HD|BT|字幕组|www\.)[^】\]]*[】\]]", " ", text, flags=re.I)
    # “百花杀（2026）更新至第31集”这类更新资源中，原有的宽泛 CJK
    # 规则会先命中“更新至”，把真实标题截掉。先识别更新进度标记，
    # 只对标记前的标题做年份/版本清理，再交给后续标准化流程。
    update_match = re.search(
        r"(?:已\s*)?(?:更新|更)(?:至|到)?\s*(?:第\s*)?(?:\d{1,4}|[零〇一二两三四五六七八九十百千]+)\s*[集话]",
        text_without_release_group,
        flags=re.I,
    )
    if update_match:
        prefix = text_without_release_group[: update_match.start()].strip(" .-_")
        prefix = re.sub(r"[（(]\s*(?:19|20)\d{2}\s*[）)]", " ", prefix)
        prefix = _strip_trailing_release_metadata(prefix)
        prefix, _year = split_title_year(prefix)
        prefix = sanitize_candidate(prefix) or prefix.strip()
        prefix, _season = extract_season_from_title(prefix)
        prefix = sanitize_candidate(prefix) or prefix.strip()
        prefix = _strip_trailing_release_metadata(prefix)
        if prefix and not _is_explicit_season_name(prefix):
            return prefix
    cjk_match = re.search(r"([\u4e00-\u9fff]{2,})(?=[\s._\-]*(?:第\s*(?:\d{1,3}|[零〇一二两三四五六七八九十百]+)\s*[季集话]|S\d{1,2}|Season|\[第?\d{1,4}[集话]))", text_without_release_group, flags=re.I)
    if cjk_match:
        return cjk_match.group(1).strip()
    title, _year = split_title_year(text)
    title = title or sanitize_candidate(text)
    title, _season = extract_season_from_title(title)
    title = sanitize_candidate(title) or title.strip()
    title = _strip_trailing_release_metadata(title)
    return "" if _is_explicit_season_name(title) else title


def _looks_like_virtual_path(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(
        text.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\/]", text)
    )


def _is_generic_media_wrapper_name(value: Any) -> bool:
    text = str(value or "").strip().replace("_", " ").replace("-", " ")
    compact = re.sub(r"\s+", "", text).casefold()
    if not compact:
        return True
    generic_tokens = {
        "4k",
        "8k",
        "2160p",
        "1080p",
        "720p",
        "480p",
        "uhd",
        "fhd",
        "hdr",
        "dv",
        "webdl",
        "webrip",
        "bluray",
        "合集",
        "全集",
        "正片",
        "字幕",
        "视频",
        "电影",
        "电视剧",
        "动漫",
        "综艺",
        "下载",
        "国剧",
        "美剧",
        "韩剧",
        "日剧",
        "英剧",
        "台剧",
        "港剧",
        "泰剧",
    }
    if compact in generic_tokens:
        return True
    if re.fullmatch(r"(?i)(?:4k|8k|2160p|1080p|720p|480p)(?:\+|\s*)(?:4k|8k|2160p|1080p|720p|480p)*", compact):
        return True
    if re.fullmatch(r"(?i)(?:版本|资源|画质|音轨|字幕)[a-z0-9一二三四五六七八九十]*", compact):
        return True
    return False


def _go_regex_quote(value: Any) -> str:
    """等价于 Go regexp.QuoteMeta 的最小转义集合。"""

    specials = set(r"\.+*?()|[]{}^$")
    return "".join(f"\\{char}" if char in specials else char for char in str(value or ""))


def _go_replacement_quote(value: Any) -> str:
    return str(value or "").replace("$", "$$")


def _safe_episode_regex_rename_plan(
    entries: list[dict[str, Any]],
    source_names: set[str],
    *,
    minimum_items: int,
) -> dict[str, Any] | None:
    """仅为可完全预演的一致集号命名生成 RE2 兼容规则。"""

    if len(entries) < max(2, int(minimum_items or 0)):
        return None
    target_parts: list[tuple[str, str, str]] = []
    for entry in entries:
        target_name = str(entry.get("dst_name") or "")
        match = re.fullmatch(r"(.*S\d{2}E)(\d{2,4})(.*)", target_name, flags=re.IGNORECASE)
        if not match:
            return None
        target_parts.append((match.group(1), match.group(2), match.group(3)))
    target_prefix, first_episode, target_suffix = target_parts[0]
    width = len(first_episode)
    if any(prefix != target_prefix or suffix != target_suffix or len(episode) != width for prefix, episode, suffix in target_parts):
        return None

    first_source = str(entries[0].get("current") or "")
    candidate_boundaries = [
        (match.start(), match.end())
        for match in re.finditer(re.escape(first_episode), first_source)
    ]
    source_prefix = ""
    source_suffix = ""
    for start, end in candidate_boundaries:
        prefix = first_source[:start]
        suffix = first_source[end:]
        if all(
            str(entry.get("current") or "") == f"{prefix}{episode}{suffix}"
            for entry, (_target_prefix, episode, _target_suffix) in zip(entries, target_parts)
        ):
            source_prefix = prefix
            source_suffix = suffix
            break
    else:
        return None

    source_regex = f"^{_go_regex_quote(source_prefix)}([0-9]{{{width}}}){_go_regex_quote(source_suffix)}$"
    try:
        compiled = re.compile(source_regex)
    except re.error:
        return None
    planned_sources = {str(entry.get("current") or "") for entry in entries}
    if {name for name in source_names if compiled.fullmatch(name)} != planned_sources:
        return None
    outputs: list[str] = []
    for entry in entries:
        source_name = str(entry.get("current") or "")
        match = compiled.fullmatch(source_name)
        if not match:
            return None
        generated = f"{target_prefix}{match.group(1)}{target_suffix}"
        if generated != str(entry.get("dst_name") or ""):
            return None
        outputs.append(generated)
    if len({name.casefold() for name in outputs}) != len(outputs):
        return None
    return {
        "source_regex": source_regex,
        # Go regexp replacement names consume following alphanumeric characters.
        # `${1}` keeps the capture boundary unambiguous even when a future
        # standard suffix starts with a letter or digit.
        "replacement": f"{_go_replacement_quote(target_prefix)}${{1}}{_go_replacement_quote(target_suffix)}",
        "count": len(entries),
    }


def _is_valid_tmdb_query(value: Any) -> bool:
    """TMDB 查询词必须是真正的影视标题。

    从路径/文件名拆出的“电视剧”“国剧”“job”“41”等通用分类词、纯数字或
    无意义短词会被用作查询词，命中同名垃圾条目后拿到虚高分数，反而把 AI 兜底
    挡在门外（service.py 中 AI 仅在 TMDB 缺席或低置信时介入）。这里统一过滤。
    """

    text = str(value or "").strip()
    if not text:
        return False
    if _is_generic_media_wrapper_name(text):
        return False
    compact = re.sub(r"\s+", "", text).casefold()
    if not compact:
        return False
    if re.fullmatch(r"(?i)[a-z0-9]{1,2}|[一-鿿]{1}|[0-9]+", compact):
        return False
    if compact in {"job", "movie", "tv", "anime", "series", "drama", "film"}:
        return False
    return True


def _strip_trailing_release_metadata(value: Any) -> str:
    text = str(value or "").strip()
    suffix = re.compile(
        r"(?:[\s._\-]+(?:"
        r"(?:\d+\s*|多|全)?音轨|"
        r"(?:内封|内嵌|外挂)?(?:简繁|简中|繁中|中英|字幕)|"
        r"(?:国语|粤语|英语|日语|韩语|泰语|越南语|西班牙语|葡萄牙语)(?:音轨)?"
        r"))+$",
        re.I,
    )
    while text:
        cleaned = suffix.sub("", text).strip(" .-_")
        if cleaned == text:
            break
        text = cleaned
    return text


def _title_from_update_payload(payload: dict[str, Any], job: dict[str, Any], normalized_root: str) -> str:
    request_payload = (job.get("raw_data") or {}).get("request") if isinstance(job.get("raw_data"), dict) else {}
    request_payload = request_payload if isinstance(request_payload, dict) else {}
    update_context = payload.get("update_context") if isinstance(payload.get("update_context"), dict) else {}
    organizer_context = payload.get("organizer_context") if isinstance(payload.get("organizer_context"), dict) else {}
    request_update_context = request_payload.get("update_context") if isinstance(request_payload.get("update_context"), dict) else {}
    request_organizer_context = request_payload.get("organizer_context") if isinstance(request_payload.get("organizer_context"), dict) else {}
    values = [
        update_context.get("canonical_title"),
        organizer_context.get("canonical_title"),
        request_update_context.get("canonical_title"),
        request_organizer_context.get("canonical_title"),
        payload.get("title"),
        request_payload.get("title"),
        job.get("title"),
        basename(normalized_root),
    ]
    for value in values:
        title = _clean_title_hint(value)
        if title:
            return title
    return basename(normalized_root) or "未命名资源"


def _task_context_title(task: dict[str, Any]) -> str:
    for container_key in ("raw_data", "evidence"):
        container = task.get(container_key) if isinstance(task.get(container_key), dict) else {}
        for key in ("update_context", "organizer_context"):
            context = container.get(key) if isinstance(container.get(key), dict) else {}
            title = _clean_title_hint(context.get("canonical_title") or context.get("title"))
            if title:
                return title
        title = _clean_title_hint(container.get("canonical_title") or container.get("title"))
        if title:
            return title
    return ""


def _is_child_path(path: str, parent: str) -> bool:
    normalized = normalize_path(path)
    root = normalize_path(parent)
    return bool(root and root != "/" and normalized.startswith(f"{root.rstrip('/')}/") and normalized != root)


def _path_is_same_or_child(path: str, parent: str) -> bool:
    normalized = normalize_path(path).rstrip("/") or "/"
    root = normalize_path(parent).rstrip("/") or "/"
    if root == "/":
        return True
    normalized_folded = normalized.casefold()
    root_folded = root.casefold()
    return normalized_folded == root_folded or normalized_folded.startswith(f"{root_folded}/")


def _staging_plan_identity(plan: dict[str, Any]) -> tuple[Any, ...]:
    path_fields = (
        "provider_target_path",
        "quark_source_category_root",
        "quark_job_root",
        "storage_final_category_root",
        "storage_staging_category_root",
        "storage_job_root",
        "openlist_final_category_root",
        "openlist_staging_category_root",
        "openlist_job_root",
        "openlist_refresh_prefix",
    )
    return (
        _safe_non_negative_int(plan.get("version")),
        _safe_non_negative_int(plan.get("job_id")),
        str(plan.get("route") or "").strip().casefold(),
        str(plan.get("category") or "").strip().casefold(),
        str(plan.get("job_dir_name") or "").strip().casefold(),
        str(plan.get("storage_backend") or "").strip().casefold(),
        *(normalize_path(plan.get(field) or "").casefold() for field in path_fields),
    )


def _relative_virtual_parts(path: str, parent: str) -> list[str]:
    normalized_path = normalize_path(path)
    normalized_parent = normalize_path(parent)
    if not _path_is_same_or_child(normalized_path, normalized_parent):
        return []
    if normalized_path.casefold() == normalized_parent.casefold():
        return []
    relative = normalized_path[len(normalized_parent.rstrip("/")) :].strip("/")
    return [part for part in relative.split("/") if part]


def _resource_dir_from_video_target(target_dir: str) -> str:
    normalized = normalize_path(target_dir)
    return dirname(normalized) if _is_explicit_season_name(basename(normalized)) else normalized


def _matching_video_for_companion(
    companion_name: str,
    video_mappings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    companion_stem = posixpath.splitext(companion_name)[0]
    companion_key = companion_stem.casefold()
    direct = []
    for mapping in video_mappings:
        source_name = str(mapping.get("source_name") or basename(mapping.get("source_path") or ""))
        source_stem = posixpath.splitext(source_name)[0]
        source_key = source_stem.casefold()
        if companion_key == source_key or any(
            companion_key.startswith(f"{source_key}{separator}")
            for separator in (".", "-", "_", " ")
        ):
            direct.append(mapping)
    if len(direct) == 1:
        return direct[0]
    parsed = parse_file_name(companion_name)
    if parsed.episode is None:
        return None
    episode_matches = [
        mapping
        for mapping in video_mappings
        if _safe_positive_int(mapping.get("episode")) == parsed.episode
        and (
            parsed.season is None
            or _safe_positive_int(mapping.get("season")) in {None, parsed.season}
        )
    ]
    return episode_matches[0] if len(episode_matches) == 1 else None


def _companion_target_name(companion_name: str, source_video_name: str, target_video_name: str) -> str:
    companion_stem, companion_ext = posixpath.splitext(companion_name)
    source_stem = posixpath.splitext(source_video_name)[0]
    target_stem = posixpath.splitext(target_video_name)[0]
    if not companion_stem or not source_stem or not target_stem:
        return companion_name
    if companion_stem.casefold().startswith(source_stem.casefold()):
        suffix = companion_stem[len(source_stem) :]
        return f"{target_stem}{suffix}{companion_ext}"
    companion_episode = re.search(r"(?i)(?:s\d{1,2}[ ._-]*)?e(?:p)?\d{1,3}", companion_stem)
    source_episode = re.search(r"(?i)(?:s\d{1,2}[ ._-]*)?e(?:p)?\d{1,3}", source_stem)
    if companion_episode and source_episode:
        companion_token = re.sub(r"[^0-9a-z]", "", companion_episode.group(0).casefold())
        source_token = re.sub(r"[^0-9a-z]", "", source_episode.group(0).casefold())
        if companion_token == source_token:
            suffix = companion_stem[companion_episode.end() :]
            return f"{target_stem}{suffix}{companion_ext}"
    return companion_name


def _is_supported_companion_name(value: str) -> bool:
    name = str(value or "").strip()
    lowered = name.casefold()
    if not name or any(marker in lowered for marker in ("广告", "推广", "二维码", "扫码", "最新网址")):
        return False
    extension = posixpath.splitext(name)[1].lower()
    return extension in COMPANION_SUBTITLE_EXTENSIONS or extension in COMPANION_METADATA_EXTENSIONS


def _companion_is_safely_related(name: str, matched_video: dict[str, Any] | None) -> bool:
    extension = posixpath.splitext(name)[1].lower()
    if extension in COMPANION_SUBTITLE_EXTENSIONS:
        return matched_video is not None
    if extension not in COMPANION_METADATA_EXTENSIONS:
        return False
    if matched_video is not None:
        return True
    stem = posixpath.splitext(name)[0].casefold().strip(" .-_")
    if stem in {
        "tvshow",
        "movie",
        "poster",
        "fanart",
        "folder",
        "banner",
        "landscape",
        "clearlogo",
        "logo",
        "clearart",
        "thumb",
    }:
        return True
    return bool(re.fullmatch(r"season[ ._-]*\d{1,2}(?:[ ._-]*(?:poster|banner|fanart|thumb))?", stem))


def _category_path_anchors(category: dict[str, Any]) -> set[str]:
    values: list[Any] = [category.get("label"), category.get("fnos_lib")]
    for field in (
        "openlist_root_path",
        "cloud139_fnos_target_path",
        "mobile_openlist_root_path",
        "mobile_target_path",
        "sixpan_fnos_target_path",
        "cmcc_parent_path",
    ):
        value = str(category.get(field) or "").replace("\\", "/").strip().strip("/")
        if value:
            values.append(value.split("/")[-1])
    return {str(value or "").strip().casefold() for value in values if str(value or "").strip()}


def _source_category_root_for_path(path: Any, category: dict[str, Any]) -> str:
    normalized = normalize_path(path)
    explicit_roots: list[str] = []
    for field in (
        "openlist_root_path",
        "cloud139_fnos_target_path",
        "mobile_openlist_root_path",
        "mobile_target_path",
        "sixpan_fnos_target_path",
    ):
        value = str(category.get(field) or "").strip()
        if not value:
            continue
        root = normalize_path(value)
        if root != "/" and _path_is_same_or_child(normalized, root):
            explicit_roots.append(root)
    if explicit_roots:
        return max(explicit_roots, key=lambda value: (value.count("/"), len(value)))
    anchors = _category_path_anchors(category)
    parts = [part for part in normalized.strip("/").split("/") if part]
    for index, part in enumerate(parts):
        if part.casefold() in anchors:
            return normalize_path("/" + "/".join(parts[: index + 1]))
    return normalize_path(category_target_root(category))


def _flag_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _lookup_title_candidates(*values: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        title, year = _lookup_title_year(value)
        if not title:
            continue
        query = f"{title} {year}".strip()
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append({"title": title, "year": year, "query": query})
    return result


def _first_title_year(candidates: list[dict[str, str]]) -> tuple[str, str]:
    if not candidates:
        return "", ""
    with_year = next((item for item in candidates if item.get("year")), candidates[0])
    return str(with_year.get("title") or ""), str(with_year.get("year") or "")


def _lookup_title_year(value: Any) -> tuple[str, str]:
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return "", ""
    text = basename(text) if "/" in text else text
    text = re.sub(r"^[\s【\[]*[^】\]]*(?:发布|高清影视|HD|BT|字幕组|www\.)[^】\]]*[】\]]", " ", text, flags=re.I)
    text = re.sub(r"【[^】]*(?:发布|高清影视|HD|BT|字幕组|www\.)[^】]*】|\[[^\]]*(?:发布|高清影视|HD|BT|字幕组|www\.)[^\]]*\]", " ", text, flags=re.I)
    text = re.sub(r"(?i)https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"^\s*[【\[]?\s*[^.\s]*(?:发布|高清影视|字幕组)[^.\s]*\s+", " ", text, flags=re.I)
    text = re.sub(r"(?i)\b(?:4k|8k|2160p|1080p|720p|480p|uhd|hdr10?|dv|sdr|hq|imax|web[- ]?dl|webrip|bluray|remux|hevc|h\.?265|x265|h\.?264|x264|dts\d?(?:\.\d)?|aac|ddp\d?(?:\.\d)?|60fps|120fps)\b", " ", text)
    text = re.sub(r"(?:内嵌|内封|外挂)?(?:简中|繁中|简体|繁体|中字|中文字幕|字幕|国语|国配|粤语|英语|双语).*$", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")

    year = ""
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
    if match:
        year = match.group(1)
        prefix = text[: match.start()].strip(" .-_()（）[]【】")
        suffix_removed = re.sub(rf"[\s._\-（(【\[]*{re.escape(year)}[\s._\-）)】\]]*", " ", text)
        title_source = prefix if len(prefix) >= 2 else suffix_removed
    else:
        title_source = text
    title, parsed_year = split_title_year(title_source)
    if not title:
        title = sanitize_candidate(title_source)
    year = year or parsed_year
    title = sanitize_candidate(title)
    title, _ = extract_season_from_title(title)
    title = re.sub(r"(?i)\b(?:4k|8k|hq|imax|fps)\b", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" .-_")
    return title, year


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
