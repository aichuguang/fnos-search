from __future__ import annotations

from typing import Any, Callable


class PublicManualPreviewService:
    """Caches and presents a manually entered share link for public preview."""

    def __init__(
        self,
        *,
        cache: Any,
        new_public_id: Callable[[], str],
        detect_link: Callable[..., Any],
        routes: Callable[[], dict[str, Any]],
        resource_detail: Callable[..., dict[str, Any]],
        present_item: Callable[..., dict[str, Any]],
        detail_capability: Callable[..., dict[str, Any]],
        quark_importer: Callable[[], Any],
        cloud139_importer: Callable[[], Any],
        sixpan_importer: Callable[[], Any],
        hide_full_links: Callable[[], bool],
    ) -> None:
        self.cache = cache
        self.new_public_id = new_public_id
        self.detect_link = detect_link
        self.routes = routes
        self.resource_detail = resource_detail
        self.present_item = present_item
        self.detail_capability = detail_capability
        self.quark_importer = quark_importer
        self.cloud139_importer = cloud139_importer
        self.sixpan_importer = sixpan_importer
        self.hide_full_links = hide_full_links

    def preview(self, *, url: str, password: str, title: str) -> dict[str, Any]:
        routes = self.routes()
        link = self.detect_link(url, routes, password=password)
        public_id = self.new_public_id()
        cache_item = {
            "title": title,
            "url": link.url,
            "password": link.password,
            "source_type": link.source_type,
            "source": link.source_type,
            "supported": link.supported,
            "route": link.route,
            "reason": link.reason,
            "raw_data": {
                "title": title,
                "url": link.url,
                "password": link.password,
                "source_type": link.source_type,
                "source": "manual_link",
            },
        }
        self.cache.save_search_cache(public_id, keyword="manual_link", item=cache_item, expires_minutes=60)
        cached = self.cache.get_search_cache(public_id) or {
            "public_id": public_id,
            "title": title,
            "source_type": link.source_type,
            "source_url": link.url,
            "password": link.password,
            "raw_data": cache_item["raw_data"],
        }
        hidden = self.hide_full_links()
        cloud139 = self.cloud139_importer()
        sixpan = self.sixpan_importer()
        detail = self.resource_detail(
            cached,
            routes=routes,
            quark_importer=self.quark_importer(),
            cloud139_importer=cloud139,
            sixpan_importer=sixpan,
            hide_full_links=hidden,
        )
        item = self.present_item(
            {**cache_item, "title": title, "url": link.url, "source_type": link.source_type, "supported": link.supported},
            public_id=public_id,
            hide_full_links=hidden,
        )
        return {
            "success": True,
            "public_id": public_id,
            "item": item,
            "detail": detail,
            "link": {
                "source_type": link.source_type,
                "supported": link.supported,
                "reason": "已识别资源类型，提交前请预览并确认内容" if link.supported else link.reason,
                "route": link.route,
                "detail_capability": self.detail_capability(
                    link.source_type,
                    cloud139_importer=cloud139,
                    sixpan_importer=sixpan,
                ),
            },
            "expires_in_minutes": 60,
        }
