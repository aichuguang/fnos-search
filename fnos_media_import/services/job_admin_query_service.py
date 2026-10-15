from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


class JobQueries(Protocol):
    def count(self, **filters: Any) -> int: ...
    def list(self, *, limit: int, offset: int, **filters: Any) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class JobAdminQueryDependencies:
    jobs: JobQueries
    load_detail: Callable[[int], dict[str, Any] | None]
    reconcile: Callable[[dict[str, Any], str], dict[str, Any]]
    decorate: Callable[[dict[str, Any]], dict[str, Any]]
    load_transfer_execution: Callable[[int], Any] | None = None
    load_transfer_plan: Callable[[int], dict[str, Any] | None] | None = None


class JobAdminQueryService:
    """Read-side orchestration for administrator import-job views."""

    def __init__(self, dependencies: JobAdminQueryDependencies) -> None:
        self._deps = dependencies

    def list_jobs(self, *, limit: int, offset: int, reconcile_reason: str = "admin_jobs_list", **filters: Any) -> dict[str, Any]:
        normalized = {key: value for key, value in filters.items() if value}
        total = self._deps.jobs.count(**normalized)
        items = self._deps.jobs.list(limit=limit, offset=offset, **normalized)
        return {
            "total": total,
            "items": [
                self._deps.decorate(self._deps.reconcile(item, reconcile_reason)) for item in items
            ],
        }

    def detail(self, job_id: int, *, reconcile_reason: str = "admin_job_detail") -> tuple[dict[str, Any], int]:
        job = self._deps.load_detail(job_id)
        if not job:
            return {"success": False, "message": "任务不存在"}, 404
        reconciled = self._deps.reconcile(job, reconcile_reason)
        if str(reconciled.get("status") or "") != str(job.get("status") or ""):
            job = self._deps.load_detail(job_id) or reconciled
        decorated = self._deps.decorate(job)
        if self._deps.load_transfer_execution is not None:
            execution = self._deps.load_transfer_execution(job_id)
            decorated["transfer_execution"] = serialize_transfer_execution(execution)
            if decorated["transfer_execution"] and decorated["transfer_execution"]["review"]["required"]:
                if self._deps.load_transfer_plan is not None:
                    decorated["transfer_execution"]["review"]["paths"] = _review_paths(self._deps.load_transfer_plan(job_id))
            if decorated["transfer_execution"] and str(decorated.get("status") or "") not in {"review", "manual_review"}:
                review = decorated["transfer_execution"]["review"]
                review["can_resume"] = False
                review["can_confirm_not_submitted"] = False
        return {"success": True, "job": decorated}, 200


def serialize_transfer_execution(execution: Any) -> dict[str, Any] | None:
    """Serialize durable execution state without exposing frozen locators."""

    if execution is None:
        return None
    cleanup = _field(execution, "cleanup", {})
    cleanup = cleanup if isinstance(cleanup, dict) else {}
    receipt = _field(execution, "receipt", None)
    receipt_summary = _receipt_summary(receipt if isinstance(receipt, dict) else None)
    return {
        "phase": str(_field(execution, "phase", "") or ""),
        "strategy": str(_field(execution, "strategy", "") or ""),
        "attempts": int(_field(execution, "attempts", 0) or 0),
        "resume_at": _field(execution, "resume_at", None),
        "staging_verified": bool(_field(execution, "staging_verified", False)),
        "final_verified": bool(_field(execution, "final_verified", False)),
        "app_acked": bool(_field(execution, "app_acked", False)),
        "organizer_task_id": _field(execution, "organizer_task_id", None),
        "cleanup": {
            key: cleanup[key]
            for key in ("status", "success", "completed", "pending", "message")
            if key in cleanup
        },
        "last_error": {
            "code": str(_field(execution, "last_error_code", "") or ""),
            "message": str(_field(execution, "last_error_message", "") or ""),
        },
        "receipt": receipt_summary,
        "row_version": int(_field(execution, "row_version", 0) or 0),
        "review": _review_actions(execution),
    }


def _review_actions(execution: Any) -> dict[str, Any]:
    snapshot = _field(execution, "snapshot", {})
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    fence = snapshot.get("submission_fence")
    fence = fence if isinstance(fence, dict) else {}
    phase = str(_field(execution, "phase", "") or "")
    review_from = str(snapshot.get("review_from") or "").strip()
    resume_phase = review_from or "planning"
    required = phase == "manual_review"
    has_receipt = _field(execution, "receipt", None) is not None or snapshot.get("receipt") is not None
    fenced = str(fence.get("state") or "") in {"prepared", "committed"}
    submission_phase = resume_phase in {"source_materializing", "native_submitting", "transferring"}
    unknown_submission = fenced and not has_receipt
    verified = any(
        bool(_field(execution, key, False)) or bool(snapshot.get(key))
        for key in ("staging_verified", "final_verified", "app_acked", "target_verified")
    )
    raw_claim = snapshot.get("claim")
    claim = raw_claim if isinstance(raw_claim, dict) else {}
    live_claim = False
    if raw_claim and not str(claim.get("owner_id") or "").strip():
        live_claim = True
    elif claim.get("owner_id"):
        try:
            lease_until = datetime.fromisoformat(str(claim.get("lease_until") or "").replace("Z", "+00:00"))
            if lease_until.tzinfo is None:
                lease_until = lease_until.replace(tzinfo=timezone.utc)
            live_claim = lease_until > datetime.now(timezone.utc)
        except (ValueError, TypeError):
            live_claim = True
    can_confirm = (
        required and unknown_submission and submission_phase and bool(review_from) and not verified and not live_claim
        and str(fence.get("phase") or "") in {"", resume_phase}
        and int(_field(execution, "row_version", 0) or 0) > 0
    )
    guidance = "修复提示的问题后，可保留现有回执，从暂停阶段继续；不会新建入库任务。"
    if unknown_submission:
        guidance = (
            "请先在网盘中核对本任务的来源暂存目录、目标目录和转存/离线任务列表。"
            "只有确认没有本次文件、没有进行中或稍后可能完成的操作，才可填写核对说明并重新提交。"
            "若已有文件、已有外部任务或无法确定，请保留原任务和证据，不要重新提交；核对错误可能重复转存。"
        )
    if live_claim:
        guidance = "原执行仍可能持有有效租约，请等待其结束后刷新页面，再核对网盘结果。"
    resume_supported = resume_phase in {
        "planning", "source_materializing", "source_waiting_visible", "native_submitting",
        "native_waiting", "transferring", "target_verifying", "organizing", "finalizing",
        "final_verifying", "ack_pending", "cleanup_pending",
    }
    missing_poll_receipt = resume_phase in {"native_waiting", "source_waiting_visible"} and not has_receipt
    if not live_claim and (not resume_supported or missing_poll_receipt or (has_receipt and not review_from)):
        guidance = "缺少可安全续跑的执行阶段或轮询回执，请保留原任务和网盘执行证据；不能直接重提或标记成功。"
    return {
        "required": required,
        "reason": str(_field(execution, "last_error_message", "") or snapshot.get("message") or ""),
        "resume_phase": resume_phase,
        "can_resume": (
            required and resume_supported and not unknown_submission and not live_claim
            and not missing_poll_receipt and (bool(review_from) or not has_receipt)
        ),
        "can_confirm_not_submitted": can_confirm,
        "guidance": guidance,
    }


def _review_paths(plan: dict[str, Any] | None) -> list[dict[str, str]]:
    paths = plan.get("paths") if isinstance(plan, dict) else None
    if not isinstance(paths, dict):
        return []
    labels = {
        "source_landing": "来源暂存目录",
        "destination_staging": "目标暂存目录",
        "destination_final": "最终目标目录",
        "local_temp": "本地中继目录",
    }
    return [
        {"label": label, "namespace": str(paths[key].get("namespace") or ""), "path": str(paths[key]["path"])}
        for key, label in labels.items()
        if isinstance(paths.get(key), dict) and paths[key].get("path")
    ]


def _field(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _receipt_summary(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not receipt:
        return None
    metadata = receipt.get("metadata") if isinstance(receipt.get("metadata"), dict) else {}
    operation_key = str(
        metadata.get("operation_key")
        or metadata.get("idempotency_key")
        or receipt.get("operation_key")
        or ""
    )
    return {
        "provider_kind": str(receipt.get("provider_kind") or ""),
        "status": str(receipt.get("status") or ""),
        "operation_id_hint": _identifier_hint(receipt.get("operation_id")),
        "run_id_hint": _identifier_hint(metadata.get("run_id")),
        "operation_key_hash": (
            hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:16]
            if operation_key
            else ""
        ),
    }


def _identifier_hint(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 6:
        return "***"
    return f"{text[:3]}…{text[-3:]}"
