from __future__ import annotations

from typing import Any, Callable


class RcloneCancelledTaskCleanupService:
    """Coordinates conservative cleanup after a task is cancelled."""

    def __init__(
        self,
        *,
        database: Any,
        cancel_job: Callable[..., dict[str, Any]],
        specs_from_events: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        specs_from_title: Callable[..., list[dict[str, Any]]],
        dedupe_specs: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        delete_remote_file: Callable[[str, str], dict[str, Any]],
        cleanup_remote_dirs: Callable[..., None],
        delete_temp_file: Callable[..., dict[str, Any]],
        cleanup_temp_dirs: Callable[[dict[str, Any]], None],
    ) -> None:
        self.database = database
        self.cancel_job = cancel_job
        self.specs_from_events = specs_from_events
        self.specs_from_title = specs_from_title
        self.dedupe_specs = dedupe_specs
        self.delete_remote_file = delete_remote_file
        self.cleanup_remote_dirs = cleanup_remote_dirs
        self.delete_temp_file = delete_temp_file
        self.cleanup_temp_dirs = cleanup_temp_dirs

    def cleanup(
        self,
        *,
        job: dict[str, Any] | None = None,
        request_item: dict[str, Any] | None = None,
        file_events: list[dict[str, Any]] | None = None,
        delete_source: bool = True,
        delete_temp: bool = True,
        delete_target_partial: bool = True,
        stop_running: bool = False,
        include_title_matches: bool = True,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "success": True,
            "message": "清理完成",
            "items": [],
            "warnings": [],
            "errors": [],
        }
        job_id = _safe_int((job or {}).get("id"))
        if job_id > 0:
            stopped = self.cancel_job(job_id, stop_running=bool(stop_running))
            result["items"].append({
                "type": "cancel_execution",
                "ok": bool(stopped.get("success")),
                "message": stopped.get("message") or "",
                "job_id": job_id,
                "active_match": bool(stopped.get("active_match")),
                "stop_sent": bool(stopped.get("stop_sent")),
                "removed_queue_count": int(stopped.get("removed_queue_count") or 0),
            })
            if not stopped.get("success"):
                result["warnings"].append(stopped.get("message") or "未能取消任务级 rclone 执行")
        elif stop_running:
            result["warnings"].append("缺少任务 ID，为避免误停其他搬运，未执行全局 rclone 停止")
        events = list(file_events or [])
        if not events and self.database and job and job.get("id"):
            events = self.database.list_rclone_file_events(job_id=int(job["id"]), limit=200)
        specs = self.specs_from_events(events)
        if include_title_matches:
            specs.extend(self.specs_from_title(job=job, request_item=request_item, known_specs=specs))
        specs = self.dedupe_specs(specs)
        if not specs:
            result["warnings"].append("未找到可精确清理的文件记录；仅更新任务状态，未执行远端删除")
            result["message"] = "已取消；未找到可精确清理的文件"
            return result

        if delete_source:
            self._delete_remote(specs, "source_path", "remote_source", result, errors=True)
            self.cleanup_remote_dirs(
                specs, source_key="source_path", item_type="remote_source_rmdirs", result=result
            )
        if delete_target_partial:
            self._delete_remote(specs, "target_path", "remote_target", result, errors=False)
        if delete_temp:
            for spec in specs:
                filename = str(spec.get("filename") or "").strip()
                if not filename:
                    continue
                try:
                    item = self.delete_temp_file(filename, job=job, spec=spec)
                except TypeError:
                    # 兼容旧的嵌入式清理适配器；生产 RcloneService 使用带任务
                    # 边界的新签名，绝不会再按文件名扫描整个共享 temp 根目录。
                    item = self.delete_temp_file(filename)
                result["items"].append(item)
                if not item["ok"]:
                    result["warnings"].append(item["message"])
            if specs:
                self.cleanup_temp_dirs(result)
        result["success"] = not result["errors"]
        if result["errors"]:
            result["message"] = "已取消，但部分源端文件清理失败"
        elif result["warnings"]:
            result["message"] = "已取消，清理完成但存在提示"
        else:
            result["message"] = "已取消并完成清理"
        return result

    def _delete_remote(
        self,
        specs: list[dict[str, Any]],
        path_key: str,
        item_type: str,
        result: dict[str, Any],
        *,
        errors: bool,
    ) -> None:
        bucket = result["errors"] if errors else result["warnings"]
        for spec in specs:
            path = str(spec.get(path_key) or "").strip()
            if not path:
                continue
            item = self.delete_remote_file(path, item_type)
            result["items"].append(item)
            if not item["ok"]:
                bucket.append(item["message"])


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
