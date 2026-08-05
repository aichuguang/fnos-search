from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ..constants import (
    COMPLETION_STAGE_DONE,
    COMPLETION_STAGE_REVIEW,
    COMPLETION_STAGE_WAITING_ORGANIZER,
    JOB_DONE,
    JOB_FAILED,
    JOB_REFRESHING,
    JOB_REVIEW,
    JOB_SUBMITTED,
    JOB_WAITING_ORGANIZER,
    ROUTE_SIXPAN_OFFLINE,
)
from .import_staging_service import staging_plan_from_job


MEDIA_REFRESH_CLAIM_TIMEOUT_SECONDS = 15 * 60


class SixPanOfflineSyncService:
    """Synchronizes submitted SixPan offline tasks into import-job state."""

    def __init__(
        self,
        *,
        database: Any,
        importer: Callable[[], Any],
        poll_limit: Callable[[], int],
        category: Callable[[str], dict[str, Any]],
        enqueue_organizer: Callable[..., dict[str, Any] | None],
        record_completed: Callable[..., dict[str, Any]],
        sync_guest_requests: Callable[..., None],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.importer = importer
        self.poll_limit = poll_limit
        self.category = category
        self.enqueue_organizer = enqueue_organizer
        self.record_completed = record_completed
        self.sync_guest_requests = sync_guest_requests
        self.now = now or (lambda: datetime.now(timezone.utc))

    def sync(self, trigger: str = "poller") -> dict[str, Any]:
        importer = self.importer()
        if not importer or not getattr(importer, "configured", False) or not getattr(importer, "poll_enabled", False):
            return {"success": True, "skipped": True, "message": "六盘离线轮询未配置或未启用"}
        limit = _bounded_int(self.poll_limit(), 200, 1, 1000)
        jobs = self._submitted_jobs(limit)
        if not jobs:
            return {"success": True, "message": "没有待轮询的六盘离线任务", "checked": 0}

        checked = completed = failed = reviewed = skipped = missing = unknown = 0
        errors: list[dict[str, Any]] = []
        for job in jobs:
            job_id = _bounded_int(job.get("id"), 0, 0, 999999999)
            task_id = str(job.get("external_task_id") or "").strip()
            if not job_id:
                skipped += 1
                continue
            checked += 1
            if not task_id:
                message = "六盘离线任务缺少 external_task_id，无法继续轮询，已转人工检查"
                if self._mark_review(
                    job,
                    job_id=job_id,
                    task_id="",
                    trigger=trigger,
                    reason="missing_task_id",
                    message=message,
                ):
                    reviewed += 1
                else:
                    skipped += 1
                continue
            try:
                task = importer.find_task(task_id, limit=limit)
            except Exception as exc:  # noqa: BLE001
                errors.append({"job_id": job_id, "task_id": task_id, "message": str(exc)})
                continue
            if not task:
                missing += 1
                outcome = self._record_watchdog_state(
                    job,
                    importer=importer,
                    job_id=job_id,
                    task_id=task_id,
                    trigger=trigger,
                    state="missing",
                    message="六盘任务列表中未找到对应离线任务",
                )
                if outcome == "review":
                    reviewed += 1
                elif outcome == "skipped":
                    skipped += 1
                continue
            state = importer.task_state(task)
            state_payload = {
                "trigger": trigger,
                "task_id": task_id,
                "state": state.state,
                "message": state.message,
                "progress": state.progress,
                "bytes_total": state.bytes_total,
                "bytes_processed": state.bytes_processed,
                "task": task,
            }
            if state.completed:
                if self._complete_job(job, job_id, task_id, trigger, state_payload):
                    completed += 1
                else:
                    skipped += 1
            elif state.failed:
                message = state.message or "六盘离线任务失败"
                updated = self._update_job_if_status(
                    job_id,
                    {JOB_SUBMITTED},
                    status=JOB_FAILED,
                    error_message=message,
                    raw_data=_merge_raw(job.get("raw_data"), {"sixpan_poll": state_payload}),
                )
                if updated:
                    self.database.add_event(job_id, "error", message, state_payload)
                    self.sync_guest_requests(
                        job_id,
                        JOB_FAILED,
                        {"sixpan_task_id": task_id, "message": state.message},
                    )
                    failed += 1
                else:
                    skipped += 1
            elif str(state.state or "").strip().lower() == "unknown":
                unknown += 1
                outcome = self._record_watchdog_state(
                    job,
                    importer=importer,
                    job_id=job_id,
                    task_id=task_id,
                    trigger=trigger,
                    state="unknown",
                    message=state.message or "六盘离线任务返回未知状态",
                    state_payload=state_payload,
                )
                if outcome == "review":
                    reviewed += 1
                elif outcome == "skipped":
                    skipped += 1
            else:
                outcome = self._record_watchdog_state(
                    job,
                    importer=importer,
                    job_id=job_id,
                    task_id=task_id,
                    trigger=trigger,
                    state="running",
                    message=state.message or "六盘离线任务处理中",
                    state_payload=state_payload,
                )
                if outcome == "review":
                    reviewed += 1
                elif outcome == "skipped":
                    skipped += 1
        return {
            "success": not errors,
            "checked": checked,
            "completed": completed,
            "failed": failed,
            "reviewed": reviewed,
            "missing": missing,
            "unknown": unknown,
            "skipped": skipped,
            "errors": errors,
        }

    def retry_media_refresh(self, job_id: int, trigger: str = "admin_manual") -> dict[str, Any]:
        """Retry only the post-provider FNOS media-library refresh.

        A completed SixPan provider task must never be sent back through the
        generic import retry path.  This entry point is intentionally narrow:
        it accepts only review jobs that durably record both provider
        completion and the ``media_refresh_only`` retry action.
        """

        normalized_job_id = _bounded_int(job_id, 0, 0, 999999999)
        job = self.database.get_job(normalized_job_id) if normalized_job_id else None
        if not isinstance(job, dict):
            return {
                "success": False,
                "not_found": True,
                "job_id": normalized_job_id,
                "message": "六盘任务不存在",
            }
        if str(job.get("target_route") or "").strip().lower() != ROUTE_SIXPAN_OFFLINE:
            return {
                "success": False,
                "rejected": True,
                "job_id": normalized_job_id,
                "message": "仅六盘离线任务支持仅重试飞牛媒体库刷新",
            }

        raw = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        completion = raw.get("completion") if isinstance(raw.get("completion"), dict) else {}
        legacy_refresh = (
            raw.get("sixpan_legacy_refresh")
            if isinstance(raw.get("sixpan_legacy_refresh"), dict)
            else {}
        )
        refresh_only = any(
            bool(marker.get("provider_completed"))
            and str(marker.get("retry_action") or "").strip().lower() == "media_refresh_only"
            for marker in (completion, legacy_refresh)
        )
        if not refresh_only:
            return {
                "success": False,
                "rejected": True,
                "job_id": normalized_job_id,
                "message": "该任务没有可执行的仅媒体库刷新重试状态",
            }
        recovered_stale_claim = False
        current_status = str(job.get("status") or "").strip().lower()
        if current_status == JOB_REFRESHING:
            retry_state = (
                raw.get("sixpan_media_refresh_retry")
                if isinstance(raw.get("sixpan_media_refresh_retry"), dict)
                else {}
            )
            stale_claim_token = _claim_token(retry_state)
            started_at = _parse_datetime(retry_state.get("started_at"))
            claim_age_seconds = (
                max(0, int((self._now_utc() - started_at).total_seconds()))
                if started_at
                else None
            )
            if claim_age_seconds is not None and claim_age_seconds < MEDIA_REFRESH_CLAIM_TIMEOUT_SECONDS:
                return {
                    "success": False,
                    "conflict": True,
                    "job_id": normalized_job_id,
                    "retry_after_seconds": max(
                        1,
                        MEDIA_REFRESH_CLAIM_TIMEOUT_SECONDS - claim_age_seconds,
                    ),
                    "message": "该任务正在刷新飞牛媒体库，请勿重复提交",
                }

            recovered_at = _iso_utc(self._now_utc())
            recovery_message = "检测到上次媒体库刷新执行锁已超时，系统已安全回收并重新执行"
            recovered_raw = _merge_raw(
                raw,
                {
                    "completion": {
                        **completion,
                        "stage": COMPLETION_STAGE_REVIEW,
                        "message": recovery_message,
                        "provider_completed": True,
                        "retryable": False,
                        "retry_action": "media_refresh_only",
                    },
                    "sixpan_media_refresh_retry": {
                        **retry_state,
                        "status": "stale_recovered",
                        "recovered_at": recovered_at,
                        "recovery_message": recovery_message,
                        "provider_completed": True,
                        "retry_action": "media_refresh_only",
                    },
                },
            )
            if not self._update_job_if_status_and_claim_token(
                normalized_job_id,
                {JOB_REFRESHING},
                stale_claim_token,
                status=JOB_REVIEW,
                error_message=recovery_message,
                raw_data=recovered_raw,
            ):
                return {
                    "success": False,
                    "conflict": True,
                    "job_id": normalized_job_id,
                    "message": "媒体库刷新执行锁状态已变化，本次未重复执行",
                }
            recovered_stale_claim = True
            job = self.database.get_job(normalized_job_id) or {
                **job,
                "status": JOB_REVIEW,
                "raw_data": recovered_raw,
            }
            raw = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else recovered_raw
            completion = raw.get("completion") if isinstance(raw.get("completion"), dict) else completion
            legacy_refresh = (
                raw.get("sixpan_legacy_refresh")
                if isinstance(raw.get("sixpan_legacy_refresh"), dict)
                else legacy_refresh
            )
            current_status = JOB_REVIEW
        if current_status != JOB_REVIEW:
            return {
                "success": False,
                "rejected": True,
                "job_id": normalized_job_id,
                "message": "仅等待人工确认的六盘任务可以重试媒体库刷新",
            }

        started_at = _iso_utc(self._now_utc())
        claim_token = uuid.uuid4().hex
        refreshing_message = "六盘文件已完成，正在仅重试飞牛媒体库刷新"
        previous_retry_state = (
            raw.get("sixpan_media_refresh_retry")
            if isinstance(raw.get("sixpan_media_refresh_retry"), dict)
            else {}
        )
        claimed_raw = _merge_raw(
            raw,
            {
                "completion": {
                    **completion,
                    "stage": JOB_REFRESHING,
                    "message": refreshing_message,
                    "provider_completed": True,
                    "retryable": False,
                    "retry_action": "media_refresh_only",
                },
                "sixpan_media_refresh_retry": {
                    **previous_retry_state,
                    "status": "running",
                    "trigger": trigger,
                    "started_at": started_at,
                    "claim_token": claim_token,
                    "recovered_stale_claim": recovered_stale_claim,
                    "provider_completed": True,
                    "retry_action": "media_refresh_only",
                },
            },
        )
        # 先用数据库 CAS 把 review 占用为 refreshing，再触发外部刷新。
        # 多进程或重复点击只有一个请求能取得执行权，其余请求不会调用刷新 callback。
        if not self._update_job_if_status(
            normalized_job_id,
            {JOB_REVIEW},
            status=JOB_REFRESHING,
            error_message="",
            raw_data=claimed_raw,
        ):
            return {
                "success": False,
                "conflict": True,
                "job_id": normalized_job_id,
                "message": "任务状态已变化，媒体库刷新未重复执行",
            }

        claimed_job = self.database.get_job(normalized_job_id) or {
            **job,
            "status": JOB_REFRESHING,
            "raw_data": claimed_raw,
        }
        refresh_result = self._record_media_refresh(
            claimed_job,
            normalized_job_id,
            trigger=f"sixpan_refresh_retry:{trigger}",
        )
        refresh_ok = refresh_result.get("success") is True
        latest_job = self.database.get_job(normalized_job_id) or job
        latest_raw = (
            latest_job.get("raw_data")
            if isinstance(latest_job.get("raw_data"), dict)
            else raw
        )
        latest_completion = (
            latest_raw.get("completion")
            if isinstance(latest_raw.get("completion"), dict)
            else completion
        )
        latest_legacy_refresh = (
            latest_raw.get("sixpan_legacy_refresh")
            if isinstance(latest_raw.get("sixpan_legacy_refresh"), dict)
            else legacy_refresh
        )
        latest_retry = (
            latest_raw.get("sixpan_media_refresh_retry")
            if isinstance(latest_raw.get("sixpan_media_refresh_retry"), dict)
            else {}
        )
        retried_at = _iso_utc(self._now_utc())
        if refresh_ok:
            message = "六盘文件已完成，飞牛媒体库刷新重试成功"
            target_status = JOB_DONE
            error_message = ""
            retry_action = "none"
            completion_stage = COMPLETION_STAGE_DONE
        else:
            detail = str(refresh_result.get("message") or "飞牛媒体库刷新未明确成功").strip()
            message = (
                f"六盘文件已完成，但飞牛媒体库刷新失败：{detail}；"
                "请仅重试媒体库刷新，不要重复提交六盘离线任务"
            )
            target_status = JOB_REVIEW
            error_message = message
            retry_action = "media_refresh_only"
            completion_stage = COMPLETION_STAGE_REVIEW

        updated = self._update_job_if_status_and_claim_token(
            normalized_job_id,
            {JOB_REFRESHING},
            claim_token,
            status=target_status,
            error_message=error_message,
            raw_data=_merge_raw(
                latest_raw,
                {
                    "completion": {
                        **latest_completion,
                        "stage": completion_stage,
                        "message": message,
                        "provider_completed": True,
                        "retryable": False,
                        "retry_action": retry_action,
                    },
                    "sixpan_legacy_refresh": {
                        **latest_legacy_refresh,
                        **refresh_result,
                        "provider_completed": True,
                        "retry_action": retry_action,
                        "last_retry_trigger": trigger,
                        "last_retry_at": retried_at,
                    },
                    "sixpan_media_refresh_retry": {
                        **latest_retry,
                        "status": "done" if refresh_ok else "failed",
                        "finished_at": retried_at,
                        "provider_completed": True,
                        "retry_action": retry_action,
                        "media_refresh": refresh_result,
                    },
                },
            ),
        )
        if not updated:
            return {
                "success": False,
                "conflict": True,
                "job_id": normalized_job_id,
                "media_refresh": refresh_result,
                "message": "媒体库刷新已执行，但任务状态已变化，未覆盖最新状态",
            }

        self.database.add_event(
            normalized_job_id,
            "info" if refresh_ok else "warn",
            message,
            {
                "trigger": trigger,
                "provider_completed": True,
                "retry_action": retry_action,
                "media_refresh": refresh_result,
                "recovered_stale_claim": recovered_stale_claim,
            },
        )
        self.sync_guest_requests(
            normalized_job_id,
            target_status,
            {
                "provider_completed": True,
                "retry_action": retry_action,
                "media_refresh_only": True,
                "message": message,
            },
        )
        return {
            "success": refresh_ok,
            "job_id": normalized_job_id,
            "status": target_status,
            "provider_completed": True,
            "retry_action": retry_action,
            "media_refresh": refresh_result,
            "recovered_stale_claim": recovered_stale_claim,
            "message": message,
        }

    def _submitted_jobs(self, batch_size: int) -> list[dict[str, Any]]:
        """Snapshot all submitted SixPan jobs before updating any of them.

        Updating statuses while paginating a filtered query would shrink the
        result set and make OFFSET skip rows.  Building the snapshot first keeps
        old jobs from permanently falling outside the newest poll batch.
        """

        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        for source_type in ("magnet", "torrent"):
            offset = 0
            while True:
                page = self.database.list_jobs(
                    limit=batch_size,
                    offset=offset,
                    status=JOB_SUBMITTED,
                    source_type=source_type,
                )
                rows = [item for item in page if isinstance(item, dict)]
                added = 0
                for item in rows:
                    job_id = _bounded_int(item.get("id"), 0, 0, 999999999)
                    if job_id and job_id not in seen:
                        seen.add(job_id)
                        result.append(item)
                        added += 1
                if len(rows) < batch_size:
                    break
                # Be defensive around lightweight/legacy repositories that
                # accept offset via **kwargs but still return the first page.
                # Without this guard a full duplicate page would spin forever.
                if not added:
                    break
                offset += len(rows)
        return result

    def _record_watchdog_state(
        self,
        job: dict[str, Any],
        *,
        importer: Any,
        job_id: int,
        task_id: str,
        trigger: str,
        state: str,
        message: str,
        state_payload: dict[str, Any] | None = None,
    ) -> str:
        latest = self.database.get_job(job_id) or job
        if str(latest.get("status") or "").strip().lower() != JOB_SUBMITTED:
            return "skipped"
        raw = latest.get("raw_data") if isinstance(latest.get("raw_data"), dict) else {}
        previous = raw.get("sixpan_watchdog") if isinstance(raw.get("sixpan_watchdog"), dict) else {}
        missing_count = _bounded_int(previous.get("missing_count"), 0, 0, 1_000_000)
        unknown_count = _bounded_int(previous.get("unknown_count"), 0, 0, 1_000_000)
        if state == "missing":
            missing_count += 1
            unknown_count = 0
        elif state == "unknown":
            unknown_count += 1
            missing_count = 0
        else:
            missing_count = 0
            unknown_count = 0

        now = self._now_utc()
        age_seconds = _job_age_seconds(latest, now)
        missing_limit = _bounded_int(getattr(importer, "task_missing_poll_limit", 5), 5, 1, 100)
        unknown_limit = _bounded_int(getattr(importer, "task_unknown_poll_limit", 5), 5, 1, 100)
        timeout_seconds = _bounded_int(
            getattr(importer, "submitted_timeout_seconds", 7 * 24 * 3600),
            7 * 24 * 3600,
            0,
            90 * 24 * 3600,
        )
        watchdog = {
            **previous,
            "task_id": task_id,
            "last_checked_at": _iso_utc(now),
            "last_state": state,
            "last_message": message,
            "missing_count": missing_count,
            "unknown_count": unknown_count,
            "submitted_age_seconds": age_seconds,
        }
        if state != "missing":
            watchdog["last_seen_at"] = _iso_utc(now)
        if state_payload:
            watchdog["last_progress"] = state_payload.get("progress")
            watchdog["last_bytes_processed"] = state_payload.get("bytes_processed")
            watchdog["last_bytes_total"] = state_payload.get("bytes_total")

        reason = ""
        review_message = ""
        if state == "missing" and missing_count >= missing_limit:
            reason = "task_missing"
            review_message = (
                f"连续 {missing_count} 次未在六盘任务列表中找到离线任务 {task_id}，"
                "可能已被清理或任务标识失效，已转人工检查"
            )
        elif state == "unknown" and unknown_count >= unknown_limit:
            reason = "unknown_state"
            review_message = (
                f"六盘离线任务 {task_id} 连续 {unknown_count} 次返回未知状态，"
                "已停止自动等待并转人工检查"
            )
        elif timeout_seconds > 0 and age_seconds >= timeout_seconds:
            reason = "submitted_timeout"
            review_message = (
                f"六盘离线任务 {task_id} 已等待 {age_seconds} 秒，超过 {timeout_seconds} 秒上限，"
                "已转人工检查"
            )

        if reason:
            watchdog["review_reason"] = reason
            watchdog["reviewed_at"] = _iso_utc(now)
            return "review" if self._mark_review(
                latest,
                job_id=job_id,
                task_id=task_id,
                trigger=trigger,
                reason=reason,
                message=review_message,
                watchdog=watchdog,
                state_payload=state_payload,
            ) else "skipped"

        patch: dict[str, Any] = {"sixpan_watchdog": watchdog}
        if state_payload:
            patch["sixpan_poll"] = state_payload
        return "waiting" if self._update_job_if_status(
            job_id,
            {JOB_SUBMITTED},
            raw_data=_merge_raw(raw, patch),
        ) else "skipped"

    def _mark_review(
        self,
        job: dict[str, Any],
        *,
        job_id: int,
        task_id: str,
        trigger: str,
        reason: str,
        message: str,
        watchdog: dict[str, Any] | None = None,
        state_payload: dict[str, Any] | None = None,
    ) -> bool:
        latest = self.database.get_job(job_id) or job
        if str(latest.get("status") or "").strip().lower() != JOB_SUBMITTED:
            return False
        raw = latest.get("raw_data") if isinstance(latest.get("raw_data"), dict) else {}
        patch: dict[str, Any] = {
            "sixpan_watchdog": watchdog
            or {
                "task_id": task_id,
                "last_checked_at": _iso_utc(self._now_utc()),
                "last_state": reason,
                "review_reason": reason,
            }
        }
        if state_payload:
            patch["sixpan_poll"] = state_payload
        event_data = {
            "trigger": trigger,
            "task_id": task_id,
            "reason": reason,
            "watchdog": patch["sixpan_watchdog"],
        }
        updated = self._update_job_if_status(
            job_id,
            {JOB_SUBMITTED},
            status=JOB_REVIEW,
            error_message=message,
            raw_data=_merge_raw(raw, patch),
        )
        if not updated:
            return False
        self.database.add_event(job_id, "warn", message, event_data)
        self.sync_guest_requests(
            job_id,
            JOB_REVIEW,
            {"sixpan_task_id": task_id, "message": message, "reason": reason},
        )
        return True

    def _now_utc(self) -> datetime:
        value = self.now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _complete_job(
        self,
        job: dict[str, Any],
        job_id: int,
        task_id: str,
        trigger: str,
        state_payload: dict[str, Any],
    ) -> bool:
        if not self._update_job_if_status(
            job_id,
            {JOB_SUBMITTED},
            status=JOB_WAITING_ORGANIZER,
            error_message="",
            raw_data=_merge_raw(
                job.get("raw_data"),
                {
                    "sixpan_poll": state_payload,
                    "completion": {
                        "stage": COMPLETION_STAGE_WAITING_ORGANIZER,
                        "official_save_path": job.get("target_path") or "",
                        "message": "六盘离线任务已完成，等待 Organizer 标准化整理与标准目录确认",
                    },
                },
            ),
        ):
            return False
        latest_job = self.database.get_job(job_id) or job
        if str(latest_job.get("status") or "").strip().lower() != JOB_WAITING_ORGANIZER:
            return False
        staging_required = bool(staging_plan_from_job(latest_job))
        try:
            organizer_result = self.enqueue_organizer(
                {"success": True, "job": latest_job},
                f"sixpan_poll:{trigger}",
            )
        except Exception as exc:  # noqa: BLE001
            organizer_result = {
                "success": False,
                "retryable": True,
                "message": f"Organizer 入队异常：{exc}",
            }
        organizer_queued = bool(
            isinstance(organizer_result, dict)
            and organizer_result.get("queued")
            and not organizer_result.get("skipped")
            and organizer_result.get("success") is not False
        )
        post_enqueue_job = self.database.get_job(job_id) or latest_job
        post_enqueue_status = str(post_enqueue_job.get("status") or "").strip().lower()
        if post_enqueue_status in {"cancelled", "done", "success", "skipped_existing"}:
            return False
        if not organizer_queued and not staging_required:
            fallback_job = post_enqueue_job
            latest_raw = fallback_job.get("raw_data") if isinstance(fallback_job.get("raw_data"), dict) else {}
            latest_completion = latest_raw.get("completion") if isinstance(latest_raw.get("completion"), dict) else {}
            refresh_result = self._record_media_refresh(
                fallback_job,
                job_id,
                trigger=f"sixpan_poll:{trigger}",
            )
            refresh_ok = refresh_result.get("success") is True
            if refresh_ok:
                message = "六盘离线任务已完成，已按旧流程触发媒体库刷新"
                error_message = ""
                completion_patch = {
                    "stage": COMPLETION_STAGE_DONE,
                    "message": message,
                    "provider_completed": True,
                    "retryable": False,
                }
            else:
                refresh_detail = str(refresh_result.get("message") or "六盘媒体库刷新失败").strip()
                message = (
                    f"六盘离线任务已完成，但媒体库刷新失败：{refresh_detail}；"
                    "网盘文件已经完成，请仅重试媒体库刷新，不要重复提交六盘离线任务"
                )
                error_message = message
                completion_patch = {
                    "stage": COMPLETION_STAGE_REVIEW,
                    "message": message,
                    "provider_completed": True,
                    "retryable": False,
                    "retry_action": "media_refresh_only",
                }
            # 旧流程回退只触发媒体库刷新、不建 Organizer 任务：必须把 job 从
            # waiting_organizer 迁走，否则会永久卡住（启动恢复也会跳过无 plan 任务）。
            updated = self._update_job_if_status(
                job_id,
                {JOB_WAITING_ORGANIZER},
                # Provider 已经成功完成。刷新失败属于后置人工处理，不能标成可
                # 重提 Provider 的普通 failed，否则旧任务会再次创建六盘离线任务。
                status=JOB_DONE if refresh_ok else JOB_REVIEW,
                error_message=error_message,
                raw_data=_merge_raw(
                    latest_raw,
                    {
                        "completion": {**latest_completion, **completion_patch},
                        "sixpan_legacy_refresh": {
                            **refresh_result,
                            "provider_completed": True,
                            "retry_action": "none" if refresh_ok else "media_refresh_only",
                        },
                    },
                ),
            )
            if not updated:
                return False
            self.database.add_event(
                job_id,
                "info" if refresh_ok else "warn",
                message,
                {"sixpan": state_payload, "media_refresh": refresh_result, "organizer": organizer_result},
            )
            current_job = self.database.get_job(job_id) or latest_job
            current_status = str(current_job.get("status") or JOB_WAITING_ORGANIZER)
            self.sync_guest_requests(
                job_id,
                current_status,
                {
                    "sixpan_task_id": task_id,
                    "legacy_refresh_fallback": True,
                    "provider_completed": True,
                    "retry_action": "none" if refresh_ok else "media_refresh_only",
                },
            )
            return True
        if not organizer_queued:
            current_job = self.database.get_job(job_id) or latest_job
            current_raw = current_job.get("raw_data") if isinstance(current_job.get("raw_data"), dict) else {}
            completion = current_raw.get("completion") if isinstance(current_raw.get("completion"), dict) else {}
            detail = str(
                organizer_result.get("message")
                if isinstance(organizer_result, dict)
                else "Organizer 未返回可用的入队结果"
            ).strip() or "Organizer 未成功创建标准化任务"
            message = (
                f"六盘离线任务已完成，但 Organizer 未成功接管：{detail}；"
                "请在 Organizer 后台重试接管或人工创建整理任务，不要重复提交六盘离线"
            )
            failure = {
                "success": False,
                "retryable": True,
                "message": message,
                "organizer": organizer_result if isinstance(organizer_result, dict) else {},
            }
            updated = self._update_job_if_status(
                job_id,
                {JOB_WAITING_ORGANIZER},
                status=JOB_REVIEW,
                error_message=message,
                raw_data=_merge_raw(
                    current_raw,
                    {
                        "completion": {
                            **completion,
                            "stage": COMPLETION_STAGE_REVIEW,
                            "message": message,
                            "retryable": True,
                        },
                        "sixpan_organizer_enqueue": failure,
                    },
                ),
            )
            if not updated:
                return False
            self.database.add_event(
                job_id,
                "warn",
                message,
                {"sixpan": state_payload, "organizer": organizer_result, "retryable": True},
            )
            self.sync_guest_requests(
                job_id,
                JOB_REVIEW,
                {"sixpan_task_id": task_id, "message": message, "retryable": True},
            )
            return True

        refresh_result = {
            "success": True,
            "skipped": True,
            "deferred_to_organizer": True,
            "message": "OpenList 标准化已启用，等待 Organizer 整理完成后刷新 OpenList 文件夹",
        }
        message = "六盘离线任务已完成，已移交 OpenList 标准化"
        self.database.add_event(
            job_id,
            "info",
            message,
            {"sixpan": state_payload, "media_refresh": refresh_result, "organizer": organizer_result},
        )
        current_job = self.database.get_job(job_id) or post_enqueue_job
        current_status = str(current_job.get("status") or JOB_WAITING_ORGANIZER).strip()
        if current_status.lower() in {"cancelled", "done", "success", "skipped_existing"}:
            return False
        self.sync_guest_requests(job_id, current_status, {"sixpan_task_id": task_id})
        return True

    def _record_media_refresh(
        self,
        job: dict[str, Any],
        job_id: int,
        *,
        trigger: str,
    ) -> dict[str, Any]:
        category_key = str(job.get("category") or "").strip()
        try:
            category = self.category(category_key)
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "message": f"读取分类配置失败：{exc}",
            }
        if not isinstance(category, dict) or not category:
            label = category_key or "未指定分类"
            return {
                "success": False,
                "message": f"未找到分类配置：{label}，未执行飞牛媒体库刷新",
            }
        try:
            result = self.record_completed(
                job_id,
                category,
                str(job.get("target_path") or ""),
                trigger=trigger,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "message": f"飞牛媒体库刷新异常：{exc}",
            }
        if not isinstance(result, dict):
            return {
                "success": False,
                "message": "飞牛媒体库刷新未返回有效结果",
            }
        normalized = dict(result)
        if normalized.get("success") is not True:
            normalized["success"] = False
            normalized["message"] = str(
                normalized.get("message") or "飞牛媒体库刷新未明确成功"
            ).strip()
        return normalized

    def _update_job_if_status(
        self,
        job_id: int,
        expected_statuses: set[str],
        **updates: Any,
    ) -> bool:
        updater = getattr(self.database, "update_job_if_status", None)
        if callable(updater):
            return bool(updater(job_id, expected_statuses, **updates))
        latest = self.database.get_job(job_id) or {}
        current_status = str(latest.get("status") or "").strip()
        # Production repositories provide the atomic updater above and always
        # persist a status.  Legacy/lightweight adapters may expose only
        # update_job/get_job and omit status from their partial record; retain
        # compatibility without weakening the production CAS path.
        if current_status and current_status not in expected_statuses:
            return False
        self.database.update_job(job_id, **updates)
        return True

    def _update_job_if_status_and_claim_token(
        self,
        job_id: int,
        expected_statuses: set[str],
        expected_claim_token: str | None,
        **updates: Any,
    ) -> bool:
        updater = getattr(self.database, "update_job_if_status_and_claim_token", None)
        if callable(updater):
            return bool(
                updater(
                    job_id,
                    expected_statuses,
                    expected_claim_token,
                    **updates,
                )
            )
        latest = self.database.get_job(job_id) or {}
        raw = latest.get("raw_data") if isinstance(latest.get("raw_data"), dict) else {}
        retry_state = (
            raw.get("sixpan_media_refresh_retry")
            if isinstance(raw.get("sixpan_media_refresh_retry"), dict)
            else {}
        )
        if _claim_token(retry_state) != expected_claim_token:
            return False
        return self._update_job_if_status(job_id, expected_statuses, **updates)


def _merge_raw(current: Any, patch: dict[str, Any]) -> dict[str, Any]:
    return {**(current if isinstance(current, dict) else {}), **patch}


def _claim_token(retry_state: dict[str, Any]) -> str | None:
    value = str(retry_state.get("claim_token") or "").strip()
    return value or None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _iso_utc(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _job_age_seconds(job: dict[str, Any], now: datetime) -> int:
    created = _parse_datetime(job.get("created_at")) or _parse_datetime(job.get("updated_at"))
    if not created:
        return 0
    return max(0, int((now - created).total_seconds()))
