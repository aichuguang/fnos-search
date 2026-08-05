from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PublicResourceDependencies:
    cache_get: Callable[[str], dict[str, Any] | None]
    routes: dict[str, Any]
    quark_importer: Any
    cloud139_importer: Any
    sixpan_importer: Any
    btbtla_client: Any
    build_detail: Callable[..., dict[str, Any]]
    child_files: Callable[..., dict[str, Any]]


class PublicResourceService:
    def __init__(self, dependencies: PublicResourceDependencies) -> None:
        self._deps = dependencies

    def detail(self, public_id: str, *, hide_full_links: bool) -> tuple[dict[str, Any], int]:
        cached = self._deps.cache_get(public_id)
        if not cached:
            return {"success": False, "message": "资源详情不存在或已过期，请重新搜索"}, 404
        detail = self._deps.build_detail(
            cached,
            routes=self._deps.routes,
            quark_importer=self._deps.quark_importer,
            cloud139_importer=self._deps.cloud139_importer,
            sixpan_importer=self._deps.sixpan_importer,
            btbtla_client=self._deps.btbtla_client,
            hide_full_links=hide_full_links,
        )
        return {"success": True, "detail": detail}, 200

    def files(self, public_id: str, *, fid: str) -> tuple[dict[str, Any], int]:
        cached = self._deps.cache_get(public_id)
        if not cached:
            return {"success": False, "message": "资源详情不存在或已过期，请重新搜索"}, 404
        result = self._deps.child_files(
            cached,
            fid=fid,
            routes=self._deps.routes,
            quark_importer=self._deps.quark_importer,
            cloud139_importer=self._deps.cloud139_importer,
        )
        return result, (200 if result.get("success", False) else 400)
