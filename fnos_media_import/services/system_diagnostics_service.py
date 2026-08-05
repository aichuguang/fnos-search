from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class LogTail(Protocol):
    def tail(self, *, limit: int, logger_prefix: str = "") -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class SystemDiagnosticsDependencies:
    logs: LogTail
    database: Any
    recent_events: Callable[..., dict[str, Any] | list[dict[str, Any]]]
    task_log_summaries: Callable[..., dict[str, Any]]


class SystemDiagnosticsService:
    def __init__(self, dependencies: SystemDiagnosticsDependencies) -> None:
        self._deps = dependencies

    def logs(self, *, limit: int, logger_prefix: str) -> dict[str, Any]:
        items = self._deps.logs.tail(limit=limit, logger_prefix=logger_prefix)
        return {"success": True, "items": items, "lines": [str(item.get("line") or "") for item in items]}

    def events(
        self,
        *,
        page: int = 1,
        per_page: int | None = None,
        limit: int | None = None,
        keyword: str = "",
        source: str = "",
        job_id: int | None = None,
    ) -> dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_per_page = max(1, min(int(per_page or limit or 50), 500))
        result = self._deps.recent_events(
            self._deps.database,
            safe_per_page,
            offset=(safe_page - 1) * safe_per_page,
            keyword=keyword,
            source=source,
            job_id=job_id,
        )
        if isinstance(result, list):
            items = result
            total = len(items)
        else:
            items = result.get("items") or []
            total = max(0, int(result.get("total") or 0))
        pages = max(1, (total + safe_per_page - 1) // safe_per_page)
        return {
            "success": True,
            "items": items,
            "pagination": {
                "page": safe_page,
                "per_page": safe_per_page,
                "total": total,
                "pages": pages,
                "has_prev": safe_page > 1,
                "has_next": safe_page < pages,
            },
        }

    def task_logs(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        keyword: str = "",
        status: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_per_page = max(1, min(int(per_page or 20), 200))
        result = self._deps.task_log_summaries(
            self._deps.database,
            safe_per_page,
            offset=(safe_page - 1) * safe_per_page,
            keyword=keyword,
            status=status,
            date_from=date_from,
            date_to=date_to,
        )
        total = max(0, int(result.get("total") or 0))
        pages = max(1, (total + safe_per_page - 1) // safe_per_page)
        return {
            "success": True,
            "items": result.get("items") or [],
            "pagination": {
                "page": safe_page,
                "per_page": safe_per_page,
                "total": total,
                "pages": pages,
                "has_prev": safe_page > 1,
                "has_next": safe_page < pages,
            },
        }
