from __future__ import annotations

import posixpath
import re
from typing import Any


class RcloneJobFeasibilityEvaluator:
    """Determines whether rclone evidence is sufficient to complete an import job."""

    COMPLETED_STATUSES = {"done", "success", "skipped_existing"}
    FAILED_STATUSES = {
        "failed",
        "error",
        "upload_error",
        "upload_exception",
        "auth_expired",
        "auth_config_error",
        "rapid_miss",
        "stopped",
    }

    @classmethod
    def evaluate(cls, job: dict[str, Any], events: list[dict[str, Any]], exit_code: int) -> dict[str, Any]:
        manifest_required = cls.requires_staging_manifest(job)
        manifest_paths = cls.staging_manifest_paths(job) if manifest_required else []
        manifest_path_set = set(manifest_paths)
        statuses = [str(event.get("status") or "").strip().lower() for event in events]
        completed_files = {
            cls.completion_identity(event, prefer_source=manifest_required)
            for event in events
            if str(event.get("status") or "").strip().lower() in cls.COMPLETED_STATUSES
            and not cls.is_log_pollution(cls.completion_identity(event, prefer_source=manifest_required))
        }
        all_failed = [
            cls.completion_identity(event, prefer_source=manifest_required)
            for event in events
            if str(event.get("status") or "").strip().lower() in cls.FAILED_STATUSES
        ]
        ignored_failed = [item for item in all_failed if cls.is_log_pollution(item)]
        failed_files = [item for item in all_failed if item and not cls.is_log_pollution(item)]
        expected = cls.expected_file_count(job)
        completed_count = len([item for item in completed_files if item])
        missing_manifest_files = sorted(manifest_path_set.difference(completed_files)) if manifest_required else []
        common = {
            "expected_file_count": expected,
            "completed_file_count": completed_count,
            "expected_file_count_source": (
                "staging_manifest" if manifest_paths else "unknown" if manifest_required else "legacy_raw_data"
            ),
            "manifest_required": manifest_required,
            "missing_manifest_file_count": len(missing_manifest_files),
            "missing_manifest_files": missing_manifest_files[:20],
            "ignored_failed_file_count": len(ignored_failed),
            "ignored_failed_files": ignored_failed[:20],
            "statuses": statuses,
        }
        if exit_code != 0:
            # 非零退出只代表“有部分文件成功”，不能在总数未知时把整项资源
            # 判定为完整。否则进程中途退出且仅完成一集，也会被错误移交
            # Organizer，导致剩余文件永远不再搬运。
            if (
                completed_count > 0
                and not failed_files
                and expected > 0
                and completed_count >= expected
                and (not manifest_required or not missing_manifest_files)
            ):
                return {
                    "ready": True,
                    "status": "done",
                    "message": f"rclone run exit={exit_code}，但真实文件已经完成；忽略旧脚本日志污染后兜底通过：已完成 {completed_count} 个文件",
                    **common,
                    "failed_file_count": 0,
                }
            return {
                "ready": False,
                "status": "transferring",
                "message": f"rclone run 未正常结束，入库兜底未通过：exit={exit_code}",
                **common,
                "failed_file_count": len(failed_files),
            }
        if failed_files:
            return {
                "ready": False,
                "status": "failed",
                "message": f"rclone 存在失败文件，已阻止入库完成与媒体库刷新：失败 {len(failed_files)} 个",
                **common,
                "failed_file_count": len(failed_files),
                "failed_files": failed_files[:20],
            }
        if completed_count <= 0:
            return {
                "ready": False,
                "status": "waiting_transfer",
                "message": "rclone run 未产生已完成文件，继续等待下一次搬运",
                **common,
                "failed_file_count": 0,
            }
        if manifest_required and expected <= 0:
            return {
                "ready": False,
                "status": "transferring",
                "message": "任务级 rclone 未持久化可信文件清单，已阻止提前移交 Organizer",
                **common,
                "failed_file_count": 0,
            }
        if manifest_required and missing_manifest_files:
            return {
                "ready": False,
                "status": "transferring",
                "message": (
                    f"任务级 rclone 文件清单未全部完成："
                    f"已搬运 {completed_count}/{expected}，尚缺 {len(missing_manifest_files)} 个文件"
                ),
                **common,
                "failed_file_count": 0,
            }
        if expected > 0 and completed_count < expected:
            return {
                "ready": False,
                "status": "transferring",
                "message": f"资源完整性兜底未通过：已搬运 {completed_count}/{expected}，等待下一次 rclone 补齐",
                **common,
                "failed_file_count": 0,
            }
        return {
            "ready": True,
            "status": "done",
            "message": f"资源完整性兜底通过：已完成 {completed_count} 个文件",
            **common,
            "failed_file_count": 0,
        }

    @staticmethod
    def file_identity(event: dict[str, Any]) -> str:
        return str(event.get("target_path") or event.get("source_path") or event.get("filename") or "").strip()

    @classmethod
    def completion_identity(cls, event: dict[str, Any], *, prefer_source: bool = False) -> str:
        value = (
            event.get("source_path")
            if prefer_source and event.get("source_path")
            else event.get("target_path") or event.get("source_path") or event.get("filename")
        )
        return cls.normalize_manifest_path(value)

    @staticmethod
    def is_log_pollution(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        base = posixpath.basename(text)
        return (
            base.startswith("[20")
            or text.startswith("[20")
            or re.search(r"(^|/)\[20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]", text) is not None
            or ("源目录" in text and any(token in text for token in ("读取", "文件数", "稳定性兜底")))
        )

    @classmethod
    def expected_file_count(cls, job: dict[str, Any]) -> int:
        if cls.requires_staging_manifest(job):
            return len(cls.staging_manifest_paths(job))
        raw = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        candidates = [
            cls.find_first_value(raw, {"expected_file_count", "file_count", "file_num", "fileCount"}),
            cls.find_first_value(
                raw.get("check") if isinstance(raw.get("check"), dict) else {},
                {"file_count", "file_num", "fileCount"},
            ),
        ]
        for value in candidates:
            try:
                number = int(str(value).strip())
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
        return 0

    @classmethod
    def requires_staging_manifest(cls, job: dict[str, Any]) -> bool:
        raw = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        plan = raw.get("staging_plan") if isinstance(raw.get("staging_plan"), dict) else {}
        try:
            version = int(plan.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        route = str(plan.get("route") or job.get("target_route") or "").strip().lower()
        return bool(plan.get("enabled") and version >= 2 and route == "quark_to_mobile")

    @classmethod
    def staging_manifest_paths(cls, job: dict[str, Any]) -> list[str]:
        raw = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        manifest = raw.get("rclone_staging_manifest") if isinstance(raw.get("rclone_staging_manifest"), dict) else {}
        values = manifest.get("source_paths") if isinstance(manifest.get("source_paths"), list) else []
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = cls.normalize_manifest_path(value)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    @staticmethod
    def normalize_manifest_path(value: Any) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if not text:
            return ""
        parts = [part for part in text.split("/") if part and part != "."]
        normalized = "/".join(parts)
        return f"/{normalized}" if normalized else ""

    @classmethod
    def latest_terminal_events(
        cls,
        events: list[dict[str, Any]],
        *,
        prefer_source: bool = False,
    ) -> list[dict[str, Any]]:
        terminal_statuses = {*cls.COMPLETED_STATUSES, *cls.FAILED_STATUSES}
        selected: dict[str, dict[str, Any]] = {}
        ordered = sorted(events, key=lambda item: cls._event_id(item), reverse=True)
        for event in ordered:
            status = str(event.get("status") or "").strip().lower()
            identity = cls.completion_identity(event, prefer_source=prefer_source)
            if status not in terminal_statuses or not identity or identity in selected:
                continue
            selected[identity] = event
        return list(selected.values())

    @staticmethod
    def _event_id(event: dict[str, Any]) -> int:
        try:
            return int(event.get("id") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def find_first_value(cls, payload: Any, keys: set[str]) -> Any:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in keys and value not in (None, ""):
                    return value
            for value in payload.values():
                found = cls.find_first_value(value, keys)
                if found not in (None, ""):
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = cls.find_first_value(item, keys)
                if found not in (None, ""):
                    return found
        return None
