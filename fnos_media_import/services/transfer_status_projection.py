"""Join durable task state with read-only worker progress, never execute work."""

from __future__ import annotations

import json
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
        file_total, bytes_total = _expected_totals(record)
        files_done = _positive_int(observation.get("upload_files_done")) if preserve_metrics else None
        bytes_done = _positive_int(observation.get("upload_bytes_done")) if preserve_metrics else None
        if file_total is not None or bytes_total is not None or files_done is not None:
            file_value = min(files_done, file_total) if files_done is not None and file_total is not None else files_done
            byte_value = min(bytes_done, bytes_total) if bytes_done is not None and bytes_total is not None else bytes_done
            item["file_progress"] = {
                "done": file_value,
                "total": file_total,
                "text": _progress_text("已传", file_value, file_total, "个文件"),
            }
            item["bytes_progress"] = {
                "done": byte_value,
                "total": bytes_total,
                "text": _progress_text("已传", byte_value, bytes_total, "", is_bytes=True),
            }
            parts = [
                part for part in (item["file_progress"]["text"], item["bytes_progress"]["text"])
                if part
            ]
            item["progress_text"] = "，".join(parts)
        # An old download batch belongs to the observation, not a reconstructed
        # task after retry/rollback. Never attach it to a new attempt.
        if stage in {"planning", "retry_wait"} or observed_phase in _DONE and not completed:
            item["run_id"] = None
        result[job_id] = item
    return list(result.values())


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _expected_totals(record: dict[str, Any]) -> tuple[int | None, int | None]:
    sources: list[dict[str, Any]] = []
    raw_plan = record.get("execution_plan_json")
    if raw_plan:
        try:
            plan = json.loads(str(raw_plan))
        except (TypeError, ValueError, json.JSONDecodeError):
            plan = {}
        if isinstance(plan, dict):
            verification = plan.get("verification")
            if isinstance(verification, dict):
                sources.append(verification)
    raw_job = record.get("job_raw_data")
    if raw_job:
        try:
            job_data = json.loads(str(raw_job))
        except (TypeError, ValueError, json.JSONDecodeError):
            job_data = {}
        if isinstance(job_data, dict):
            sources.append(job_data)
    file_total = None
    bytes_total = None
    for source in sources:
        if file_total is None:
            file_total = _positive_int(
                source.get("expected_file_count", source.get("file_count"))
            )
        if bytes_total is None:
            bytes_total = _positive_int(
                source.get("expected_total_size", source.get("total_size", source.get("total_bytes")))
            )
    if not file_total:
        file_total = None
    if not bytes_total:
        bytes_total = None
    return file_total, bytes_total


def _format_bytes(value: int) -> str:
    number = float(max(0, value))
    for unit in ("B", "K", "M", "G", "T"):
        if number < 1024 or unit == "T":
            if unit == "B":
                return f"{int(number)}B"
            return f"{number:.1f}{unit}".rstrip("0").rstrip(".")
        number /= 1024
    return f"{number:.1f}T"


def _progress_text(
    prefix: str,
    done: int | None,
    total: int | None,
    suffix: str,
    *,
    is_bytes: bool = False,
) -> str:
    if total is None:
        return ""
    done_text = _format_bytes(done or 0) if is_bytes else str(done or 0)
    total_text = _format_bytes(total) if is_bytes else str(total)
    tail = f" {suffix}" if suffix else ""
    return f"{prefix} {done_text}/{total_text}{tail}"
