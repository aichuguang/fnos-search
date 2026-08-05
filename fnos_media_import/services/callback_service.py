from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..constants import (
    CALLBACK_STATUS_AUTH_CONFIG_ERROR,
    CALLBACK_STATUS_AUTH_EXPIRED,
    CALLBACK_STATUS_CATEGORY_DONE,
    CALLBACK_STATUS_CATEGORY_FAILED,
    CALLBACK_STATUS_DONE,
    CALLBACK_STATUS_ERROR,
    CALLBACK_STATUS_FAILED,
    CALLBACK_STATUS_NAMING_PLAN,
    CALLBACK_STATUS_PROCESSING,
    CALLBACK_STATUS_RAPID_MISS,
    CALLBACK_STATUS_RESOLVE_FOLDER,
    CALLBACK_STATUS_SKIPPED_EXISTING,
    CALLBACK_STATUS_STAGING_MANIFEST,
    CALLBACK_STATUS_STOPPED,
    CALLBACK_STATUS_SUCCESS,
    CALLBACK_STATUS_TRANSFERRING,
    CALLBACK_STATUS_UPLOAD_ERROR,
    CALLBACK_STATUS_UPLOAD_EXCEPTION,
    CALLBACK_STATUS_UPLOAD_PENDING,
    COMPLETION_CALLBACK_STATUSES,
    JOB_CANCELLED,
    JOB_CONFIRMING,
    JOB_DONE,
    JOB_ORGANIZING,
    JOB_REFRESHING,
    JOB_REVIEW,
    JOB_UNSUPPORTED,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
)
from .import_staging_service import validated_staging_plan_from_job


_IRREVERSIBLE_POST_TRANSFER_STATUSES = frozenset(
    {
        JOB_WAITING_OPENLIST,
        JOB_WAITING_ORGANIZER,
        JOB_ORGANIZING,
        JOB_CONFIRMING,
        JOB_REVIEW,
        JOB_REFRESHING,
        JOB_DONE,
    }
)

_LATE_FILE_CALLBACK_STATUSES = frozenset(
    {
        CALLBACK_STATUS_TRANSFERRING,
        CALLBACK_STATUS_PROCESSING,
        CALLBACK_STATUS_DONE,
        CALLBACK_STATUS_SUCCESS,
        CALLBACK_STATUS_SKIPPED_EXISTING,
        CALLBACK_STATUS_FAILED,
        CALLBACK_STATUS_ERROR,
        CALLBACK_STATUS_UPLOAD_ERROR,
        CALLBACK_STATUS_UPLOAD_EXCEPTION,
        CALLBACK_STATUS_AUTH_EXPIRED,
        CALLBACK_STATUS_AUTH_CONFIG_ERROR,
        CALLBACK_STATUS_RAPID_MISS,
        CALLBACK_STATUS_UPLOAD_PENDING,
        CALLBACK_STATUS_STOPPED,
    }
)

_COMPLETION_CALLBACK_STATUSES = COMPLETION_CALLBACK_STATUSES

_IMMUTABLE_JOB_STATUSES = frozenset(
    {
        JOB_CANCELLED,
        JOB_DONE,
        CALLBACK_STATUS_SUCCESS,
        CALLBACK_STATUS_SKIPPED_EXISTING,
        JOB_UNSUPPORTED,
        "rejected",
    }
)


@dataclass(frozen=True)
class CallbackDependencies:
    db: Any
    rclone: Any
    safe_int: Callable[[Any, int, int, int], int]
    callback_level: Callable[[str], str]
    enqueue_organizer: Callable[[dict[str, Any], dict[str, Any]], Any]
    cancelled_status: str


class RcloneCallbackService:
    def __init__(self, dependencies: CallbackDependencies) -> None:
        self.deps = dependencies

    def handle(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        db, rclone = self.deps.db, self.deps.rclone
        run_id = self.deps.safe_int(payload.get("run_id"), 0, 0, 999999999)
        job_id = self.deps.safe_int(payload.get("job_id"), 0, 0, 999999999)
        status = str(payload.get("status") or "").strip()
        if not status:
            return {"success": False, "message": "缺少 rclone 回调状态"}, 400
        message = str(payload.get("message") or "rclone 回调").strip()
        category = str(payload.get("category") or "").strip()
        filename = str(payload.get("filename") or "").strip()
        source_path = str(payload.get("source_path") or "").strip()
        target_path = str(payload.get("target_path") or "").strip()
        level = self.deps.callback_level(status)
        normalized_status = status.lower()
        explicit_job = db.get_job(job_id) if job_id else None
        if explicit_job and str(explicit_job.get("status") or "").strip().lower() == self.deps.cancelled_status:
            # A category summary can arrive after an administrator cancelled the
            # job.  Do not hand it to the finalizer/Organizer, and most
            # importantly never issue an ACK that permits source deletion.
            if normalized_status in {CALLBACK_STATUS_CATEGORY_DONE, CALLBACK_STATUS_CATEGORY_FAILED}:
                db.add_event(job_id, "warn", f"已忽略取消任务的 rclone 分类汇总回调：{message}", payload)
                return {
                    "success": True,
                    "message": "任务已取消，分类汇总仅记录不执行后续处理",
                    "disposition": "ignored_cancelled",
                    "delete_source_allowed": False,
                    "job": db.get_job(job_id) or explicit_job,
                }, 200
        if normalized_status == CALLBACK_STATUS_RESOLVE_FOLDER:
            return {"success": True, **rclone.resolve_resource_folder(category_label=category, filename=filename, source_path=source_path, target_path=target_path)}, 200
        if normalized_status == CALLBACK_STATUS_NAMING_PLAN:
            return {"success": True, "naming_plan": rclone.build_upload_naming_plan(category_label=category, filename=filename, source_path=source_path, target_path=target_path)}, 200
        rclone_event_payload = payload
        if normalized_status == CALLBACK_STATUS_STAGING_MANIFEST:
            rclone_event_payload = {
                key: value
                for key, value in payload.items()
                if key != "manifest_paths"
            }
        db.add_rclone_event(run_id or None, level, message, rclone_event_payload)
        if normalized_status in {CALLBACK_STATUS_CATEGORY_DONE, CALLBACK_STATUS_CATEGORY_FAILED}:
            category_refresh = rclone.finalize_category_imports(run_id, category, payload) if normalized_status == CALLBACK_STATUS_CATEGORY_DONE else {"success": True, "skipped": True, "message": "分类存在失败文件，等待下一轮或 run 结束兜底"}
            latest_explicit = db.get_job(job_id) if job_id else None
            cancelled_during_finalize = bool(
                latest_explicit
                and str(latest_explicit.get("status") or "").strip().lower()
                == self.deps.cancelled_status
            )
            organizer = (
                self.deps.enqueue_organizer(category_refresh, payload)
                if normalized_status == CALLBACK_STATUS_CATEGORY_DONE and not cancelled_during_finalize
                else None
            )
            return {
                "success": True,
                "message": (
                    "分类处理期间任务已取消，未继续分发 Organizer"
                    if cancelled_during_finalize
                    else "已收到 rclone 分类汇总回调"
                ),
                "category_refresh": category_refresh,
                "organizer": organizer,
                "payload": payload,
                "disposition": "ignored_cancelled" if cancelled_during_finalize else "accepted",
                "delete_source_allowed": False,
            }, 200
        if not job_id:
            matched = db.find_job_for_rclone_callback(category=category, filename=filename, source_path=source_path, target_path=target_path)
            if matched:
                job_id = int(matched["id"])
            else:
                file_event_id = db.add_rclone_file_event(run_id=run_id or None, job_id=None, status=status or "unknown", level=level, category=category, filename=filename, source_path=source_path, target_path=target_path, message=message, raw_data=payload) if filename else None
                if bool(payload.get("require_job_match")) and filename:
                    return {
                        "success": False,
                        "error_code": "rclone_job_not_matched",
                        "message": "rclone 文件未匹配到等待搬运任务，已拒绝确认，源文件应保留等待重试",
                        "file_event_id": file_event_id,
                        "payload": payload,
                    }, 409
                return {
                    "success": True,
                    "message": "已收到回调，但未匹配到具体任务",
                    "file_event_id": file_event_id,
                    "payload": payload,
                    "disposition": "unmatched",
                    "delete_source_allowed": False,
                }, 200
        job = explicit_job or (db.get_job(job_id) if job_id else None)
        if job and filename and normalized_status != CALLBACK_STATUS_STAGING_MANIFEST:
            validation_error = self._staging_file_callback_error(job, source_path, target_path)
            if validation_error:
                return {
                    "success": False,
                    "error_code": "rclone_staging_path_mismatch",
                    "message": validation_error,
                    "payload": payload,
                }, 409
        if job and str(job.get("status") or "") == self.deps.cancelled_status:
            if normalized_status == CALLBACK_STATUS_STAGING_MANIFEST:
                return {
                    "success": False,
                    "message": "任务已取消，已拒绝确认 rclone 文件清单",
                    "disposition": "ignored_cancelled",
                    "delete_source_allowed": False,
                }, 409
            db.add_event(job_id, "warn", f"已忽略取消任务的 rclone 回调：{message}", payload)
            if filename:
                db.add_rclone_file_event(run_id=run_id or None, job_id=int(job["id"]), status="ignored_cancelled", level="warn", category=category, filename=filename, source_path=source_path, target_path=target_path, message=message, raw_data=payload)
            return {
                "success": True,
                "message": "任务已取消，回调仅记录不更新状态",
                "disposition": "ignored_cancelled",
                "delete_source_allowed": False,
                "job": db.get_job(job_id),
            }, 200
        if normalized_status == CALLBACK_STATUS_STAGING_MANIFEST:
            if not job:
                return {"success": False, "message": "任务不存在，已拒绝确认 rclone 文件清单"}, 404
            return self._record_staging_manifest(job, payload, run_id)
        current_job_status = str(job.get("status") or "").strip().lower() if job else ""
        if (
            job
            and filename
            and current_job_status in _IRREVERSIBLE_POST_TRANSFER_STATUSES
            and normalized_status in _LATE_FILE_CALLBACK_STATUSES
        ):
            ignored_payload = {
                **payload,
                "callback_disposition": "ignored_terminal",
                "ignored_callback_status": normalized_status,
                "preserved_job_status": current_job_status,
            }
            ignored_message = (
                "[ignored_terminal] 已忽略迟到的 rclone 文件回调，"
                f"任务保持 {current_job_status}：{message}"
            )
            db.add_rclone_file_event(
                run_id=run_id or None,
                job_id=int(job["id"]),
                status="ignored_terminal",
                level="warn",
                category=category,
                filename=filename,
                source_path=source_path,
                target_path=target_path,
                message=ignored_message,
                raw_data=ignored_payload,
            )
            db.add_event(job_id, "warn", ignored_message, ignored_payload)
            return {
                "success": True,
                "message": "任务已进入后续不可回退阶段，迟到文件回调已幂等确认",
                "ignored": True,
                "disposition": "ignored_terminal",
                "delete_source_allowed": normalized_status in _COMPLETION_CALLBACK_STATUSES,
                "job": db.get_job(job_id) or job,
            }, 200
        if filename:
            db.add_rclone_file_event(run_id=run_id or None, job_id=int(job["id"]) if job else None, status=status or "unknown", level=level, category=category, filename=filename, source_path=source_path, target_path=target_path, message=message, raw_data=payload)
        if not job:
            return {"success": False, "message": "任务不存在"}, 404
        db.add_event(job_id, level, message, payload)
        updates: dict[str, Any] | None = None
        if normalized_status in {
            CALLBACK_STATUS_TRANSFERRING,
            CALLBACK_STATUS_PROCESSING,
            CALLBACK_STATUS_DONE,
            CALLBACK_STATUS_SUCCESS,
            CALLBACK_STATUS_SKIPPED_EXISTING,
        }:
            updates = {"status": "transferring", "error_message": ""}
        elif normalized_status in {
            CALLBACK_STATUS_UPLOAD_ERROR,
            CALLBACK_STATUS_UPLOAD_EXCEPTION,
            CALLBACK_STATUS_AUTH_EXPIRED,
            CALLBACK_STATUS_AUTH_CONFIG_ERROR,
            CALLBACK_STATUS_RAPID_MISS,
        }:
            updates = {"status": "failed", "error_message": message or "上传异常"}
        elif normalized_status in {CALLBACK_STATUS_FAILED, CALLBACK_STATUS_ERROR}:
            updates = {"status": "transferring", "error_message": message}
        elif normalized_status in {CALLBACK_STATUS_CATEGORY_DONE, CALLBACK_STATUS_CATEGORY_FAILED}:
            db.add_event(job_id, "info" if normalized_status == CALLBACK_STATUS_CATEGORY_DONE else "warn", "rclone 分类搬运结束，等待 run 完整结束兜底判定", payload)
        else:
            updates = {"status": normalized_status, "error_message": message}
        if updates is not None:
            updated = self._update_job_from_snapshot(job, **updates)
            if not updated:
                return self._state_changed_response(job_id, job, payload, normalized_status, message)
        return {
            "success": True,
            "job": db.get_job(job_id) or job,
            "organizer": None,
            "disposition": "accepted",
            "delete_source_allowed": normalized_status in _COMPLETION_CALLBACK_STATUSES,
        }, 200

    @staticmethod
    def _staging_file_callback_error(
        job: dict[str, Any],
        source_path: str,
        target_path: str,
    ) -> str:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        raw_plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        if not raw_plan.get("enabled"):
            return ""
        try:
            plan = validated_staging_plan_from_job(job)
        except ValueError as exc:
            return f"持久化暂存计划无效，已拒绝 rclone 文件回调：{exc}"
        if str(plan.get("route") or "").strip().lower() != "quark_to_mobile":
            return ""
        source_job_root = _normalize_manifest_path(plan.get("quark_job_root"))
        target_job_root = _normalize_manifest_path(plan.get("storage_job_root"))
        normalized_source = _normalize_manifest_path(source_path)
        normalized_target = _normalize_manifest_path(target_path)
        if not normalized_source or not _path_strictly_under(normalized_source, source_job_root):
            return (
                "rclone 文件回调越出当前任务源目录，已拒绝确认，源文件应保留等待重试："
                f"{normalized_source or source_path or '<empty>'}"
            )
        if not normalized_target or not _path_strictly_under(normalized_target, target_job_root):
            return (
                "rclone 文件回调越出当前任务目标目录，已拒绝确认，源文件应保留等待重试："
                f"{normalized_target or target_path or '<empty>'}"
            )
        return ""

    def _record_staging_manifest(
        self,
        job: dict[str, Any],
        payload: dict[str, Any],
        run_id: int,
    ) -> tuple[dict[str, Any], int]:
        try:
            plan = validated_staging_plan_from_job(job)
        except ValueError as exc:
            return {"success": False, "message": f"持久化暂存计划无效，已拒绝 rclone 文件清单：{exc}"}, 409
        if str(plan.get("route") or "").strip().lower() != "quark_to_mobile":
            return {"success": False, "message": "当前任务不是 Quark 任务级 rclone 暂存线路"}, 409

        job_root = _normalize_manifest_path(plan.get("quark_job_root"))
        raw_paths = payload.get("manifest_paths") if isinstance(payload.get("manifest_paths"), list) else []
        incoming: list[str] = []
        incoming_seen: set[str] = set()
        for value in raw_paths:
            normalized = _normalize_manifest_path(value)
            if not normalized or normalized in incoming_seen:
                continue
            if not _path_under(normalized, job_root):
                return {
                    "success": False,
                    "message": f"rclone 文件清单越出当前任务目录，已拒绝：{normalized}",
                }, 409
            incoming_seen.add(normalized)
            incoming.append(normalized)
        if not incoming:
            return {"success": False, "message": "rclone 文件清单为空，已拒绝确认"}, 409

        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        existing = (
            raw_data.get("rclone_staging_manifest")
            if isinstance(raw_data.get("rclone_staging_manifest"), dict)
            else {}
        )
        existing_paths = existing.get("source_paths") if isinstance(existing.get("source_paths"), list) else []
        merged_paths: list[str] = []
        merged_seen: set[str] = set()
        for value in [*existing_paths, *incoming]:
            normalized = _normalize_manifest_path(value)
            if not normalized or normalized in merged_seen or not _path_under(normalized, job_root):
                continue
            merged_seen.add(normalized)
            merged_paths.append(normalized)
        run_ids = []
        existing_run_ids = existing.get("run_ids") if isinstance(existing.get("run_ids"), list) else []
        for value in [*existing_run_ids, run_id]:
            try:
                parsed = int(value or 0)
            except (TypeError, ValueError):
                parsed = 0
            if parsed > 0 and parsed not in run_ids:
                run_ids.append(parsed)
        manifest = {
            "version": 1,
            "source_paths": merged_paths,
            "expected_file_count": len(merged_paths),
            "run_ids": run_ids,
            "last_run_id": run_id or 0,
            "job_root": job_root,
        }
        job_id = int(job["id"])
        if not self._update_job_from_snapshot(
            job,
            raw_data={**raw_data, "rclone_staging_manifest": manifest},
        ):
            latest = self.deps.db.get_job(job_id) or job
            latest_status = str(latest.get("status") or "").strip().lower()
            cancelled = latest_status == self.deps.cancelled_status
            return {
                "success": False,
                "message": (
                    "任务已取消，已拒绝确认 rclone 文件清单"
                    if cancelled
                    else "任务状态已变化，已拒绝用过期快照覆盖 rclone 文件清单"
                ),
                "disposition": "ignored_cancelled" if cancelled else "state_conflict",
                "delete_source_allowed": False,
                "job": latest,
            }, 409
        self.deps.db.add_event(
            job_id,
            "info",
            f"已固化任务级 rclone 文件清单：累计 {len(merged_paths)} 个文件",
            {
                "rclone_staging_manifest_version": manifest["version"],
                "expected_file_count": manifest["expected_file_count"],
                "incoming_file_count": len(incoming),
                "run_id": run_id or 0,
            },
        )
        return {
            "success": True,
            "message": "已确认任务级 rclone 文件清单",
            "manifest": manifest,
            "job": self.deps.db.get_job(job_id) or job,
        }, 200

    def _update_job_from_snapshot(self, job: dict[str, Any], **updates: Any) -> bool:
        job_id = int(job.get("id") or 0)
        expected_status = str(job.get("status") or "").strip()
        if job_id <= 0 or not expected_status or expected_status.lower() in _IMMUTABLE_JOB_STATUSES:
            return False
        updater = getattr(self.deps.db, "update_job_if_status", None)
        if callable(updater):
            return bool(updater(job_id, {expected_status}, **updates))
        latest = self.deps.db.get_job(job_id) or {}
        if str(latest.get("status") or "").strip() != expected_status:
            return False
        self.deps.db.update_job(job_id, **updates)
        return True

    def _state_changed_response(
        self,
        job_id: int,
        stale_job: dict[str, Any],
        payload: dict[str, Any],
        callback_status: str,
        message: str,
    ) -> tuple[dict[str, Any], int]:
        latest = self.deps.db.get_job(job_id) or stale_job
        latest_status = str(latest.get("status") or "").strip().lower()
        cancelled = latest_status == self.deps.cancelled_status
        disposition = "ignored_cancelled" if cancelled else "ignored_state_changed"
        ignored_payload = {
            **payload,
            "callback_disposition": disposition,
            "ignored_callback_status": callback_status,
            "preserved_job_status": latest_status,
        }
        self.deps.db.add_event(
            job_id,
            "warn",
            (
                f"任务已取消，已忽略并发到达的 rclone 回调：{message}"
                if cancelled
                else f"任务状态已变化为 {latest_status or '未知'}，已忽略过期 rclone 回调：{message}"
            ),
            ignored_payload,
        )
        delete_source_allowed = bool(
            not cancelled
            and latest_status in _IRREVERSIBLE_POST_TRANSFER_STATUSES
            and callback_status in _COMPLETION_CALLBACK_STATUSES
        )
        return {
            "success": True,
            "message": "任务状态已变化，回调仅记录不覆盖当前状态",
            "ignored": True,
            "disposition": disposition,
            "delete_source_allowed": delete_source_allowed,
            "job": latest,
        }, 200


def _normalize_manifest_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    parts: list[str] = []
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            return ""
        parts.append(part)
    return f"/{'/'.join(parts)}" if parts else ""


def _path_under(path: str, root: str) -> bool:
    path_key = str(path or "").rstrip("/").casefold()
    root_key = str(root or "").rstrip("/").casefold()
    return bool(path_key and root_key and (path_key == root_key or path_key.startswith(f"{root_key}/")))


def _path_strictly_under(path: str, root: str) -> bool:
    path_key = str(path or "").rstrip("/").casefold()
    root_key = str(root or "").rstrip("/").casefold()
    return bool(path_key and root_key and path_key != root_key and path_key.startswith(f"{root_key}/"))
