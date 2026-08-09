from __future__ import annotations

from typing import Any, Callable

from ..constants import COMPLETION_STAGE_REVIEW, COMPLETION_STAGE_WAITING_ORGANIZER, EVENT_ERROR, EVENT_INFO, EVENT_WARN, JOB_REVIEW, JOB_WAITING_ORGANIZER
from ..planning.job_plan import plan_strategy, validated_v1_execution_plan_from_job


class RcloneReadyItemsCompletionService:
    """Updates ready jobs after rclone and media-refresh completion."""

    def __init__(
        self,
        *,
        database: Any,
        config: Callable[[], dict[str, Any]],
        refresh_media: Callable[..., dict[str, Any]],
    ) -> None:
        self.database = database
        self.config = config
        self.refresh_media = refresh_media

    def finish(
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
        # Only completed V1 rclone_copy jobs may enter the media-refresh and
        # completion path. Legacy staging/v3 jobs are moved to review and are
        # never allowed to become done through this service.
        valid_items: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
        rejected: list[int] = []
        valid_events: list[dict[str, Any]] = []
        for job, job_events, verdict in items:
            try:
                plan = validated_v1_execution_plan_from_job(job)
                if plan_strategy(plan) != "rclone_copy":
                    raise ValueError("completion requires a V1 rclone_copy plan")
            except Exception as exc:  # noqa: BLE001
                job_id = _safe_job_id(job)
                if job_id:
                    rejected.append(job_id)
                    _mark_job_review(self.database, job, f"rclone completion rejected: {exc}")
                continue
            valid_items.append((job, job_events, verdict))
            valid_events.extend(item for item in job_events if isinstance(item, dict))
        if rejected and not valid_items:
            return {
                "success": False,
                "manual_review": True,
                "status": "manual_review",
                "review_job_ids": rejected,
                "completed_items": [],
                "message": "legacy or unsupported rclone jobs were moved to manual review",
            }
        refresh = self._refresh(category_key, category, valid_events, trigger)
        trigger_text = "category completion" if trigger == "rclone_category_done" else "run completion"
        action = "已移交 OpenList 标准化" if refresh.get("deferred_to_organizer") else "刷新媒体库"
        self.database.add_rclone_event(
            run_id,
            EVENT_INFO if refresh.get("success") else EVENT_WARN,
            f"rclone {trigger_text}{action}：{refresh.get('library') or category_key}",
            refresh,
        )
        completed = []
        for job, job_events, verdict in valid_items:
            item = self._finish_job(
                run_id, category_key, category, job, job_events, verdict, refresh, trigger
            )
            if item:
                completed.append(item)
        return {**refresh, "completed_items": completed} if completed else refresh

    def _refresh(
        self,
        category_key: str,
        category: dict[str, Any],
        events: list[dict[str, Any]],
        trigger: str,
    ) -> dict[str, Any]:
        if self.config().get("defer_media_refresh_to_organizer"):
            return {
                "success": True,
                "skipped": True,
                "deferred_to_organizer": True,
                "library": category.get("fnos_lib") or category.get("label") or category_key,
                "message": "OpenList 标准化已启用，等待 Organizer 完成后刷新 OpenList 文件夹",
            }
        return self.refresh_media(category_key, category, events, trigger=trigger)

    def _official_save_path(self, job: dict[str, Any]) -> str:
        """Return the frozen V1 destination path; never consult legacy fields."""
        try:
            plan = validated_v1_execution_plan_from_job(job)
        except Exception:  # noqa: BLE001
            return ""
        paths = plan.get("paths") if isinstance(plan.get("paths"), dict) else {}
        final = paths.get("destination_final") if isinstance(paths.get("destination_final"), dict) else {}
        return str(final.get("path") or "").strip()

    def _finish_job(
        self,
        run_id: int,
        category_key: str,
        category: dict[str, Any],
        job: dict[str, Any],
        events: list[dict[str, Any]],
        verdict: dict[str, Any],
        refresh: dict[str, Any],
        trigger: str,
    ) -> dict[str, Any] | None:
        job_id = int(job["id"])
        event_payload = {
            "verdict": verdict,
            "refresh": refresh,
            "files": len(events),
            "run_id": run_id,
            "trigger": trigger,
        }
        if not refresh.get("success"):
            message = refresh.get("message") or "rclone 已搬运完成，但飞牛媒体库刷新失败，未标记为完成"
            if not _update_job_from_snapshot(
                self.database,
                job,
                status=JOB_REVIEW,
                error_message=message,
            ):
                return None
            self.database.add_event(job_id, EVENT_WARN, message, event_payload)
            return None

        deferred = bool(refresh.get("deferred_to_organizer"))
        next_status = JOB_WAITING_ORGANIZER if deferred else JOB_REVIEW
        completion_message = (
            "rclone 搬运完成，等待 Organizer 标准化整理与标准目录确认"
            if deferred
            else "rclone 搬运完成，但未启用 Organizer 接管，不能确认完整整理入库"
        )
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        completion = {
            **completion,
            "stage": COMPLETION_STAGE_WAITING_ORGANIZER if deferred else COMPLETION_STAGE_REVIEW,
            "message": completion_message,
            "official_save_path": self._official_save_path(job) or completion.get("official_save_path") or job.get("target_path") or "",
            "rclone_run_id": run_id,
            "rclone_trigger": trigger,
        }
        if not _update_job_from_snapshot(
            self.database,
            job,
            status=next_status,
            error_message="" if deferred else completion_message,
            raw_data={**raw_data, "completion": completion},
        ):
            return None
        message = (
            "rclone 搬运完成，后续整理与 OpenList 文件夹刷新由 Organizer 接管"
            if deferred
            else "rclone 搬运完成，但未启用 Organizer 接管，已转入人工确认"
        )
        self.database.add_event(job_id, EVENT_INFO, message, event_payload)
        return {
            "job_id": job_id,
            "job": job,
            "category": category_key,
            "category_label": category.get("label") or category_key,
            "target_paths": _dedupe(event.get("target_path") for event in events),
            "source_paths": _dedupe(event.get("source_path") for event in events),
            "file_count": len(events),
            "verdict": verdict,
        }


def _safe_job_id(job: dict[str, Any] | None) -> int:
    try:
        return int((job or {}).get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _mark_job_review(database: Any, job: dict[str, Any], message: str) -> None:
    job_id = _safe_job_id(job)
    if job_id <= 0 or database is None:
        return
    updater = getattr(database, "update_job_if_status", None)
    expected = str(job.get("status") or "").strip()
    updates = {"status": JOB_REVIEW, "error_message": message}
    if callable(updater) and expected:
        try:
            updater(job_id, {expected}, **updates)
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            database.update_job(job_id, **updates)
        except Exception:  # noqa: BLE001
            pass
    try:
        database.add_event(job_id, EVENT_ERROR, message, {"stage": "review", "v1_only_guard": True})
    except Exception:  # noqa: BLE001
        pass


def _dedupe(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _update_job_from_snapshot(database: Any, job: dict[str, Any], **updates: Any) -> bool:
    try:
        job_id = int(job.get("id") or 0)
    except (TypeError, ValueError):
        job_id = 0
    expected_status = str(job.get("status") or "").strip()
    if job_id <= 0 or not expected_status:
        return False
    updater = getattr(database, "update_job_if_status", None)
    if callable(updater):
        return bool(updater(job_id, {expected_status}, **updates))
    latest = database.get_job(job_id) or {}
    if str(latest.get("status") or "").strip() != expected_status:
        return False
    database.update_job(job_id, **updates)
    return True
