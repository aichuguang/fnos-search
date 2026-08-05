from __future__ import annotations

from typing import Any, Callable


class PublicBtResolveService:
    """Resolves a BTBTLA download option into a cached magnet preview."""

    def __init__(
        self,
        *,
        cache: Any,
        btbtla: Callable[[], Any],
        present_item: Callable[..., dict[str, Any]],
        resource_detail: Callable[..., dict[str, Any]],
        routes: Callable[[], dict[str, Any]],
        quark_importer: Callable[[], Any],
        cloud139_importer: Callable[[], Any],
        sixpan_importer: Callable[[], Any],
        hide_full_links: Callable[[], bool],
    ) -> None:
        self.cache = cache
        self.btbtla = btbtla
        self.present_item = present_item
        self.resource_detail = resource_detail
        self.routes = routes
        self.quark_importer = quark_importer
        self.cloud139_importer = cloud139_importer
        self.sixpan_importer = sixpan_importer
        self.hide_full_links = hide_full_links

    def resolve(
        self,
        *,
        public_id: str,
        resource_id: str = "",
        resource_url: str = "",
        resource_title: str = "",
    ) -> tuple[dict[str, Any], int]:
        cached = self.cache.get_search_cache(public_id)
        if not cached:
            return {"success": False, "message": "资源详情不存在或已过期，请重新搜索"}, 404
        raw = cached.get("raw_data") if isinstance(cached.get("raw_data"), dict) else {}
        source_type = str(cached.get("source_type") or raw.get("source_type") or "").strip().lower()
        detail_url = str(raw.get("btbtla_detail_url") or cached.get("source_url") or raw.get("detail_url") or raw.get("url") or "")
        has_origin = bool(str(raw.get("btbtla_detail_url") or raw.get("detail_url") or "").strip())
        if source_type != "bt_detail" and not has_origin:
            return {"success": False, "message": "该资源不是可解析的 BTBTLA 详情"}, 400
        try:
            resolved = self.btbtla().resolve_magnet(
                detail_url,
                resource_id=resource_id,
                resource_url=resource_url,
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"磁链解析失败：{exc}"}, 502
        title = resource_title or str(cached.get("title") or raw.get("title") or "BT 入库资源")
        item = self._magnet_item(title, raw, detail_url, resource_id, resource_url, resolved)
        if not self.cache.update_search_cache_item(public_id, item, expires_minutes=60):
            return {"success": False, "message": "资源缓存已过期，请重新搜索"}, 404
        cached_after = self.cache.get_search_cache(public_id) or cached
        hidden = self.hide_full_links()
        client = self.btbtla()
        return {
            "success": True,
            "message": "磁链解析成功，正在预览可入库内容",
            "magnet": resolved["magnet"],
            "item": self.present_item(item, public_id=public_id, hide_full_links=hidden),
            "detail": self.resource_detail(
                cached_after,
                routes=self.routes(),
                quark_importer=self.quark_importer(),
                cloud139_importer=self.cloud139_importer(),
                sixpan_importer=self.sixpan_importer(),
                btbtla_client=client,
                hide_full_links=hidden,
            ),
            "resolved": resolved,
        }, 200

    @staticmethod
    def _magnet_item(title, raw, detail_url, resource_id, resource_url, resolved):
        return {
            "title": title,
            "url": resolved["magnet"],
            "source_url": resolved["magnet"],
            "source_type": "magnet",
            "source": "btbtla",
            "source_hint": "btbtla",
            "supported": bool(resolved.get("supported")),
            "route": resolved.get("route") or "sixpan_offline",
            "reason": "BTBTLA 下载资源已解析为磁链",
            "size_text": raw.get("size_text") or "",
            "raw_data": {
                **raw,
                "title": title,
                "url": resolved["magnet"],
                "source_url": resolved["magnet"],
                "source_type": "magnet",
                "btbtla_detail_url": detail_url,
                "btbtla_resource_id": resource_id,
                "btbtla_resource_url": resource_url or resolved.get("tdown_url") or "",
                "btbtla_resolved": resolved,
            },
        }
