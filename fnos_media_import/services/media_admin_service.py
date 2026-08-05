from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class MediaClient(Protocol):
    log_path: Any
    def running_tasks(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class MediaAdminQueryDependencies:
    client: MediaClient
    categories: dict[str, Any]
    build_dashboard: Callable[[Any, dict[str, Any]], dict[str, Any]]
    read_log_tail: Callable[[Any, int], list[dict[str, Any]]]


class MediaAdminQueryService:
    def __init__(self, dependencies: MediaAdminQueryDependencies) -> None:
        self._deps = dependencies

    def libraries(self) -> dict[str, Any]:
        return self._deps.build_dashboard(self._deps.client, self._deps.categories)

    def running(self) -> dict[str, Any]:
        try:
            return self._deps.client.running_tasks()
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"飞牛刷新任务状态获取异常：{exc}", "items": []}

    def refresh_logs(self, limit: int) -> dict[str, Any]:
        path = getattr(self._deps.client, "log_path", None)
        return {"success": True, "path": str(path or ""), "items": self._deps.read_log_tail(path, limit)}


class ImportCommands(Protocol):
    def refresh_media(self, category: str) -> dict[str, Any]: ...


class MediaCommandClient(Protocol):
    def refresh_guid(self, guid: str, *, library: str, dir_list: Any) -> dict[str, Any]: ...
    def refresh(self, library: str, *, dir_list: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class MediaAdminCommandDependencies:
    imports: ImportCommands
    client: MediaCommandClient
    directory_required_message: str
    worker_dispatcher: Any | None = None


class MediaAdminCommandService:
    def __init__(self, dependencies: MediaAdminCommandDependencies) -> None:
        self._deps = dependencies

    def refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        category = str(payload.get("category") or "").strip()
        guid = str(payload.get("guid") or payload.get("library_guid") or "").strip()
        library = str(payload.get("library") or payload.get("title") or payload.get("name") or "").strip()
        dir_list = payload.get("dir_list")
        if self._deps.worker_dispatcher and category:
            queued = self._deps.worker_dispatcher.media_category_refresh(category)
            if queued:
                return queued
        if self._deps.worker_dispatcher and library and dir_list:
            queued = self._deps.worker_dispatcher.media_refresh(library, dir_list, guid=guid)
            if queued:
                return queued
        if category:
            return self._deps.imports.refresh_media(category)
        if guid:
            if not dir_list:
                return self._missing_dirs(library=library, guid=guid)
            try:
                return self._deps.client.refresh_guid(guid, library=library, dir_list=dir_list)
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "message": f"飞牛媒体库刷新异常：{exc}", "library": library, "guid": guid, "dir_list": dir_list or []}
        if library:
            if not dir_list:
                return self._missing_dirs(library=library)
            try:
                return self._deps.client.refresh(library, dir_list=dir_list)
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "message": f"飞牛媒体库刷新异常：{exc}", "library": library, "dir_list": dir_list or []}
        return self._deps.imports.refresh_media("movie")

    def _missing_dirs(self, *, library: str, guid: str | None = None) -> dict[str, Any]:
        result = {"success": False, "message": self._deps.directory_required_message, "library": library, "dir_list": [], "skipped": True}
        if guid is not None:
            result["guid"] = guid
        return result
