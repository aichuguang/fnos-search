from __future__ import annotations

from typing import Any, Callable

from ..database import utc_now

ImportServiceProvider = Callable[[], Any]
ImportHandlerProvider = Callable[[], Callable[[dict[str, Any], str], Any] | None]
MarkSeen = Callable[..., None]


class UpdateCandidateImportService:
    """Creates an idempotent import job for a discovered update candidate."""

    def __init__(
        self,
        *,
        database: Any,
        import_service: ImportServiceProvider,
        import_handler: ImportHandlerProvider,
        mark_seen: MarkSeen,
    ) -> None:
        self.database = database
        self.import_service = import_service
        self.import_handler = import_handler
        self.mark_seen = mark_seen

    def import_candidate(
        self,
        candidate_id: int,
        *,
        reason: str = "manual_update_candidate",
        auto: bool = False,
        candidate_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidate_row = self.database.get_update_candidate(candidate_id)
        if not candidate_row:
            raise ValueError("候选不存在")
        subscription = self.database.get_update_subscription(
            int(candidate_row["subscription_id"]),
            include_sources=True,
        )
        if not subscription:
            raise ValueError("候选关联订阅不存在")

        raw = candidate_row.get("raw_data") if isinstance(candidate_row.get("raw_data"), dict) else {}
        candidate = candidate_override or raw.get("candidate") or {}
        payload = dict(candidate.get("import_payload") or {})
        payload.setdefault("title", candidate_row.get("title") or subscription.get("title") or "未命名资源")
        payload.setdefault("url", candidate_row.get("source_url") or "")
        payload.setdefault("password", candidate_row.get("password") or "")
        payload.setdefault("category", subscription.get("category") or "movie")
        payload.setdefault("idempotency_key", f"update-candidate:{candidate_id}")
        payload.setdefault("executor_id", "scheduler" if auto else "admin")
        payload.setdefault("config_revision", 1)

        result = self.import_service().create_import_job(payload)
        job = result.get("job") if isinstance(result.get("job"), dict) else {}
        handler = self.import_handler()
        if handler:
            try:
                result["rclone_start"] = handler(result, reason)
            except Exception as exc:  # noqa: BLE001
                result["rclone_start"] = {"success": False, "message": str(exc)}

        job_status = str(job.get("status") or "")
        completed = job_status in {"done", "success"}
        succeeded = bool(result.get("success", True))
        decision = ("completed" if completed else "submitted") if succeeded else "failed"
        candidate_raw = dict(raw)
        candidate_raw["import_result"] = {
            "job_id": job.get("id"),
            "job_status": job_status,
            "completion_stage": (job.get("raw_data") or {}).get("completion", {}).get("stage")
            if isinstance(job.get("raw_data"), dict)
            else "",
            "submitted_at": utc_now(),
            "auto": auto,
        }
        self.database.update_update_candidate(
            candidate_id,
            decision=decision,
            job_id=job.get("id"),
            reason=result.get("message") or candidate_row.get("reason") or "",
            raw_data=candidate_raw,
        )
        if succeeded and completed:
            self.mark_seen(
                subscription,
                candidate_row,
                candidate,
                candidate_id,
                job,
                auto=auto,
                completion_state="done",
            )

        if succeeded:
            event_message = "候选资源已完整整理入库" if completed else "候选资源已提交入库，等待 OpenList/Organizer 完整确认"
        else:
            event_message = "候选资源入库失败"
        self.database.add_update_event(
            int(subscription["id"]),
            candidate_row.get("run_id"),
            "info" if succeeded else "error",
            event_message,
            {"candidate_id": candidate_id, "result": result, "decision": decision},
        )
        return {
            "success": succeeded,
            "submitted": succeeded,
            "completed": completed,
            "candidate_id": candidate_id,
            "job": job,
            "message": result.get("message") or "已提交入库",
            "result": result,
        }
