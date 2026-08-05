from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class OrganizerQueries(Protocol):
    def status(self) -> dict[str, Any]: ...
    def list_tasks(self, *, limit: int, offset: int, status: str | None) -> list[dict[str, Any]]: ...
    def get_task(self, task_id: int) -> dict[str, Any] | None: ...
    def list_runs(self, *, limit: int, offset: int) -> list[dict[str, Any]]: ...


class OrganizerCounts(Protocol):
    def count_organizer_tasks(self, *, status: str | None = None) -> int: ...
    def count_organizer_runs(self) -> int: ...


@dataclass(frozen=True)
class OrganizerAdminQueryDependencies:
    organizer: OrganizerQueries
    counts: OrganizerCounts


class OrganizerAdminQueryService:
    def __init__(self, dependencies: OrganizerAdminQueryDependencies) -> None:
        self._deps = dependencies

    def tasks(self, *, limit: int, offset: int, status: str | None) -> dict[str, Any]:
        return {
            "status": self._deps.organizer.status(),
            "items": self._deps.organizer.list_tasks(limit=limit, offset=offset, status=status),
            "total": self._deps.counts.count_organizer_tasks(status=status),
        }

    def detail(self, task_id: int) -> tuple[dict[str, Any], int]:
        task = self._deps.organizer.get_task(task_id)
        if not task:
            return {"success": False, "message": "标准化任务不存在"}, 404
        return {"success": True, "task": task, "status": self._deps.organizer.status()}, 200

    def runs(self, *, limit: int, offset: int) -> dict[str, Any]:
        return {
            "items": self._deps.organizer.list_runs(limit=limit, offset=offset),
            "total": self._deps.counts.count_organizer_runs(),
        }


class OrganizerCommands(Protocol):
    def create_manual_task(self, payload: dict[str, Any], *, defer_process: bool = False) -> dict[str, Any]: ...
    def get_task(self, task_id: int) -> dict[str, Any] | None: ...
    def rebuild_task(self, task_id: int) -> dict[str, Any]: ...
    def update_mapping(self, mapping_id: int, payload: dict[str, Any], *, task_id: int) -> dict[str, Any]: ...
    def batch_update_mappings(self, task_id: int, payload: dict[str, Any]) -> dict[str, Any]: ...
    def approve_task(self, task_id: int) -> dict[str, Any]: ...
    def start_apply_task(self, task_id: int) -> dict[str, Any]: ...
    def skip_task(self, task_id: int) -> dict[str, Any]: ...
    def delete_task(self, task_id: int) -> dict[str, Any]: ...
    def process_task(self, task_id: int, *, auto_apply: bool) -> dict[str, Any]: ...
    def rollback_run(self, run_id: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OrganizerAdminCommandDependencies:
    organizer: OrganizerCommands
    worker_dispatcher: Any | None = None


class OrganizerAdminCommandService:
    def __init__(self, dependencies: OrganizerAdminCommandDependencies) -> None:
        self._organizer = dependencies.organizer
        self._worker_dispatcher = dependencies.worker_dispatcher

    def scan(self, payload: dict[str, Any]):
        if self._worker_dispatcher:
            created = self._organizer.create_manual_task(payload, defer_process=True)
            if created.get("success") is False:
                return self._with_status(created)
            task_id = int(created.get("task_id") or 0)
            if not task_id:
                return {"success": False, "message": "标准化任务已创建但未返回 task_id"}, 500
            queued = self._worker_dispatcher.organizer_process(
                task_id,
                auto_apply=bool(payload.get("auto_apply", False)),
                respect_schedule=False,
            )
            if queued:
                return {
                    **created,
                    "queued": True,
                    "worker_dispatch": queued,
                    "message": "标准化任务已加入持久化 Worker 队列",
                }, 200
            return self._with_status(
                self._organizer.process_task(
                    task_id,
                    auto_apply=bool(payload.get("auto_apply", False)),
                )
            )
        return self._with_status(self._organizer.create_manual_task(payload))

    def rebuild(self, task_id: int):
        if self._worker_dispatcher:
            queued = self._worker_dispatcher.organizer_process(
                task_id,
                auto_apply=True,
                respect_schedule=False,
            )
            if queued:
                return queued, 200
        return self._with_status(self._organizer.rebuild_task(task_id))

    def approve(self, task_id: int):
        return self._with_status(self._organizer.approve_task(task_id))

    def apply(self, task_id: int):
        if self._worker_dispatcher:
            queued = self._worker_dispatcher.organizer_apply(task_id)
            if queued:
                return queued, 200
        return self._with_status(self._organizer.start_apply_task(task_id))

    def skip(self, task_id: int):
        result = self._organizer.skip_task(task_id)
        if result.get("success") and result.get("ready_for_apply"):
            if self._worker_dispatcher:
                queued = self._worker_dispatcher.organizer_apply(task_id)
                if queued:
                    return {
                        **result,
                        **queued,
                        "passthrough": True,
                        "message": result.get("message") or "原名直通入库已加入后台队列",
                    }, 200
            started = self._organizer.start_apply_task(task_id)
            return {
                **result,
                **started,
                "passthrough": True,
                "message": (
                    result.get("message")
                    if started.get("success")
                    else started.get("message") or result.get("message")
                ),
            }, (200 if started.get("success") else 400)
        return result, (409 if result.get("conflict") else (200 if result.get("success", True) else 400))

    def delete(self, task_id: int):
        result = self._organizer.delete_task(task_id)
        if result.get("not_found"):
            return result, 404
        return result, (409 if result.get("conflict") else (200 if result.get("success") else 400))

    def retry(self, task_id: int):
        getter = getattr(self._organizer, "get_task", None)
        task = getter(task_id) if callable(getter) else None
        resume_apply = _retry_should_resume_apply(task)
        if self._worker_dispatcher:
            queued = (
                self._worker_dispatcher.organizer_apply(task_id)
                if resume_apply
                else self._worker_dispatcher.organizer_process(
                    task_id,
                    auto_apply=True,
                    respect_schedule=False,
                )
            )
            if queued:
                return {
                    **queued,
                    "retry_mode": "resume_apply" if resume_apply else "rescan",
                }, 200
        result = (
            self._organizer.start_apply_task(task_id)
            if resume_apply
            else self._organizer.process_task(task_id, auto_apply=True)
        )
        if isinstance(result, dict):
            result.setdefault("retry_mode", "resume_apply" if resume_apply else "rescan")
        return self._with_status(result)

    def rollback(self, run_id: int):
        return self._with_status(self._organizer.rollback_run(run_id))

    def update_mapping(self, task_id: int, mapping_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        task = self._organizer.get_task(task_id)
        if not task:
            return {"success": False, "message": "标准化任务不存在"}, 404
        mapping_ids = {int(item.get("id") or 0) for item in task.get("mappings") or []}
        if mapping_id not in mapping_ids:
            return {"success": False, "message": "映射记录不属于当前任务"}, 404
        result = self._organizer.update_mapping(mapping_id, payload, task_id=task_id)
        if result.get("success") is False:
            result["task"] = self._organizer.get_task(task_id)
            return result, (409 if result.get("conflict") else 400)
        result["task"] = self._organizer.get_task(task_id)
        return result, 200

    def batch_update_mappings(self, task_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """统一修改任务全部映射的片名 / 季号，并由 Organizer 原子提交。"""
        task = self._organizer.get_task(task_id)
        if not task:
            return {"success": False, "message": "标准化任务不存在"}, 404
        has_title = payload.get("title") is not None and str(payload.get("title") or "").strip()
        season = payload.get("season")
        if not has_title and season is None:
            return {"success": False, "message": "请填写要修改的片名或季号"}, 400
        season_int: int | None = None
        if season is not None:
            try:
                season_int = int(season)
            except (TypeError, ValueError):
                return {"success": False, "message": "季号必须是数字"}, 400
            if season_int < 0 or season_int > 99:
                return {"success": False, "message": "季号超出范围（0-99）"}, 400
        mappings = [item for item in task.get("mappings") or [] if int(item.get("id") or 0) > 0]
        if not mappings:
            return {"success": False, "message": "该任务没有可修改的映射"}, 400
        title = str(payload.get("title") or "").strip() if has_title else ""
        mapping_payload: dict[str, Any] = {}
        if title:
            mapping_payload["title"] = title
        if season_int is not None:
            mapping_payload["season"] = season_int
        updater = getattr(self._organizer, "batch_update_mappings", None)
        if not callable(updater):
            return {"success": False, "message": "当前 Organizer 不支持原子批量修改，未写入任何映射"}, 500
        result = updater(task_id, mapping_payload)
        if result.get("success") is False:
            return result, (409 if result.get("conflict") else 400)
        result.setdefault("task", self._organizer.get_task(task_id))
        result.setdefault("changed", len(mappings))
        return result, 200

    @staticmethod
    def _with_status(result: dict[str, Any]) -> tuple[dict[str, Any], int]:
        return result, (200 if result.get("success", True) else 400)


def _retry_should_resume_apply(task: dict[str, Any] | None) -> bool:
    if not isinstance(task, dict):
        return False
    status = str(task.get("status") or "").strip().lower()
    if status not in {"failed", "waiting_review", "auto_approved", "manual_confirmed"}:
        return False
    mappings = task.get("mappings") if isinstance(task.get("mappings"), list) else []
    if any(str(item.get("status") or "").strip().lower() in {"conflict", "need_edit"} for item in mappings):
        return False
    operations = task.get("operations") if isinstance(task.get("operations"), list) else []
    if not operations:
        return False
    operation_statuses = {
        str(item.get("status") or "pending").strip().lower()
        for item in operations
    }
    if operation_statuses - {"pending", "done", "skipped"}:
        return False
    evidence = task.get("evidence") if isinstance(task.get("evidence"), dict) else {}
    confirmation = evidence.get("completion_confirmation")
    error_message = str(task.get("error_message") or "").strip().lower()
    apply_was_started = bool(
        operation_statuses & {"done", "skipped"}
        or isinstance(confirmation, dict)
        or "租约" in error_message
        or "lease" in error_message
    )
    return apply_was_started
