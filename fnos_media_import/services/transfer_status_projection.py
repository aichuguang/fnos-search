"""Join durable task state with read-only worker progress, never execute work."""

from __future__ import annotations

from typing import Any


_DONE = frozenset({"done", "success", "completed"})
_CANCELLED = frozenset({"cancelled", "canceled", "rejected"})
_REVIEW = frozenset({"review", "manual_review"})
_TRANSFER_PROGRESS = frozenset({
    "download", "downloading", "waiting_download", "waiting_upload",
    "hash", "hashing", "preparing_upload", "upload", "uploading", "transferring",
})
_JOB_PHASES = {
    "created": "planning", "provider_submitting": "source_materializing",
    "submitted": "source_waiting_visible", "waiting_transfer": "waiting_download",
    "waiting_openlist": "target_verifying", "waiting_organizer": "organizing",
    "organizing": "organizing", "confirming": "final_verifying",
    "refreshing": "ack_pending", "retry_requested": "retry_wait",
    "transferring": "transferring",
}
_COPY = {
    "completed": "入库任务已完成，状态与正式任务一致。",
    "cancelled": "原入库任务已取消，已停止显示旧的传输进度。",
    "manual_review": "原入库任务等待人工处理，请查看任务详情。",
    "failed": "原入库任务失败，请查看任务详情。",
    "retry_wait": "原入库任务等待继续，旧进度不代表本次执行。",
    "restored": "状态已从入库任务记录恢复，等待实时传输反馈。",
    "conflict": "任务完成标记与执行校验记录不一致，请查看原任务；未将其视为入库成功。",
}


def transfer_job_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        result = int(value)
    except (ValueError, TypeError):
        return None
    return result if result > 0 else None


def project_transfer_status(
    observations: list[dict[str, Any]], records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Task identity/terminal state comes from SQL; bytes only from the worker.

    Records absent from SQL (for example deleted history) cannot be revived by
    an old worker log. Missing telemetry must never become a fabricated 0/100%.
    """
    live: dict[int, dict[str, Any]] = {}
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        job_id = transfer_job_id(observation.get("job_id"))
        if job_id is not None and (
            job_id not in live
            or str(observation.get("updated_at") or "") > str(live[job_id].get("updated_at") or "")
        ):
            live[job_id] = observation

    result: dict[int, dict[str, Any]] = {}
    for record in records:
        job_id = transfer_job_id(record.get("job_id"))
        if job_id is None:
            continue
        observation = live.get(job_id, {})
        job_status = str(record.get("job_status") or "").lower()
        phase = str(record.get("execution_phase") or "").lower()
        observed_phase = str(observation.get("stage") or "").lower()
        verified = phase == "completed" and bool(record.get("final_verified")) and bool(record.get("app_acked"))
        completed = job_status in _DONE and (not phase or verified)
        conflict = job_status in _DONE and bool(phase) and not verified

        if job_status in _CANCELLED:
            stage = "cancelled"
        elif job_status in _REVIEW or conflict:
            stage = "manual_review"
        elif job_status == "failed":
            stage = "failed"
        elif job_status == "retry_requested":
            stage = "retry_wait"
        elif completed:
            stage = "completed"
        elif phase == "transferring" and observed_phase in _TRANSFER_PROGRESS and observation.get("completed") is not True:
            stage = observed_phase
        else:
            stage = phase or _JOB_PHASES.get(job_status, "planning")

        preserve_metrics = (
            stage in _TRANSFER_PROGRESS
            and (phase == "transferring" or (not phase and job_status == "transferring"))
            and observed_phase in _TRANSFER_PROGRESS
            and observation.get("completed") is not True
        )
        item = dict(observation)
        for channel in ("download", "hash", "upload"):
            metric = observation.get(channel)
            item[channel] = dict(metric) if preserve_metrics and isinstance(metric, dict) else {
                "state": "unknown", "scope": "file", "filename": None,
                "percent": None, "bytes_done": None, "bytes_total": None,
                "speed_bps": None, "eta_seconds": None,
                "file_id": None, "run_id": None, "active": False, "stale": False, "age_seconds": None,
            }
        buffer = observation.get("relay_buffer")
        if preserve_metrics and isinstance(buffer, dict):
            item["relay_buffer"] = dict(buffer)
        else:
            item.pop("relay_buffer", None)
        if not preserve_metrics:
            item.update(
                updated_at=record.get("state_updated_at") or record.get("job_updated_at"),
                age_seconds=None, stale=False,
            )
        message = observation.get("message") if preserve_metrics else ""
        if conflict:
            message = _COPY["conflict"]
        elif job_status in _REVIEW or job_status == "failed":
            message = record.get("job_error") or record.get("execution_message") or _COPY[stage]
        elif not message:
            message = _COPY.get(stage) or record.get("execution_message") or _COPY["restored"]
        item.update(
            job_id=job_id, title=str(record.get("title") or ""), job_status=job_status,
            stage=stage, completed=completed, message=str(message), state_source="import_job",
            state_updated_at=record.get("state_updated_at") or record.get("job_updated_at"),
            state_conflict=conflict,
        )
        # An old download batch belongs to the observation, not a reconstructed
        # task after retry/rollback. Never attach it to a new attempt.
        if stage in {"planning", "retry_wait"} or observed_phase in _DONE and not completed:
            item["run_id"] = None
        result[job_id] = item
    return list(result.values())
