from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExternalDiagnosticsDependencies:
    organizer: Any


class ExternalDiagnosticsService:
    def __init__(self, dependencies: ExternalDiagnosticsDependencies) -> None:
        self._organizer = dependencies.organizer

    def openlist_test(self):
        return self._organizer.test_openlist()

    def openlist_dirs(self, path: str) -> tuple[dict[str, Any], int]:
        try:
            items = self._organizer.openlist.list_dir(path)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": str(exc), "path": path, "items": []}, 400
        dirs = [{"name": item.name, "path": item.path} for item in items if item.is_dir]
        return {"success": True, "path": path, "items": dirs, "count": len(dirs)}, 200

    def tmdb_test(self):
        return self._organizer.test_tmdb()

    def tmdb_search(self, query: str, media_type: str) -> dict[str, Any]:
        items = self._organizer.tmdb.search(query, media_type)
        return {"success": True, "items": items, "count": len(items)}

    def tmdb_detail(self, media_type: str, tmdb_id: int, season: int) -> tuple[dict[str, Any], int]:
        details = self._organizer.tmdb.details(tmdb_id, media_type)
        if not details:
            return {"success": False, "message": "TMDB 条目不存在或 Token 未配置"}, 404
        episodes = self._organizer.tmdb.season_episodes(tmdb_id, season) if media_type == "tv" and season else []
        return {"success": True, "item": details, "episodes": episodes}, 200

    def ai_test(self, payload: dict[str, Any]):
        override = payload.get("ai") if isinstance(payload.get("ai"), dict) else payload
        return self._organizer.test_ai(override if isinstance(override, dict) else None)
