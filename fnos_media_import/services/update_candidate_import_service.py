from __future__ import annotations

from typing import Any, Callable

from ..database import utc_now
from ..media_identity_contract import subscription_import_identity

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
        candidate_row = self.database.update.get_update_candidate(candidate_id)
        if not candidate_row:
            raise ValueError("候选不存在")
        subscription = self.database.update.get_update_subscription(
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
        payload["category"] = subscription.get("category") or "movie"
        # The candidate/subscription is not an infrastructure authority.
        # Destination, account, mount and organizer paths are resolved again
        # from the V1 source assignment when the import job is created.  Strip
        # historical or cached fields instead of letting a scheduler payload
        # override the current DB routing graph.
        for field in (
            "destination_id",
            "target_path",
            "target_path_override",
            "cloud139_target_path_override",
            "update_context",
            "organizer_context",
            "target_root_is_resource",
            "mount_id",
            "remote",
            "executor_id",
            "config_revision",
            "idempotency_key",
            "media_identity",
            "organizer_intent",
            "update_subscription_id",
        ):
            payload.pop(field, None)
        identity, intent = subscription_import_identity(subscription)
        if identity:
            payload["media_identity"] = identity
        payload["organizer_intent"] = intent
        payload["update_subscription_id"] = int(subscription["id"])
        imports = self.import_service()
        creator = getattr(imports, "create_internal_import_job", None)
        if not callable(creator):
            creator = imports.create_import_job
        result = creator(payload)
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
        self.database.update.update_update_candidate(
            candidate_id,
            # Persist the Job link before recording completion, so normal
            # completion sync can retry if the seen write fails or we crash.
            decision="submitted" if succeeded and completed else decision,
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
            self.database.update.update_update_candidate(candidate_id, decision="completed")

        if succeeded:
            event_message = "候选资源已完整整理入库" if completed else "候选资源已提交入库，等待 OpenList/Organizer 完整确认"
        else:
            event_message = "候选资源入库失败"
        self.database.update.add_update_event(
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
