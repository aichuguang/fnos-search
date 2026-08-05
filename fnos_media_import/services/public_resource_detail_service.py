from __future__ import annotations

from typing import Any, Callable


class PublicResourceDetailService:
    """Builds the public-safe detail view for cached search resources."""

    def __init__(
        self,
        *,
        detect_link: Callable[..., Any],
        mask_url: Callable[[str], str],
        inspect_bt: Callable[..., dict[str, Any]],
        inspect_resource: Callable[..., dict[str, Any]],
        category_suggestion: Callable[[dict[str, Any]], Any],
        format_size: Callable[[Any], str],
        search_preview: Callable[..., Any],
        detail_capability: Callable[..., dict[str, Any]],
    ) -> None:
        self.detect_link = detect_link
        self.mask_url = mask_url
        self.inspect_bt = inspect_bt
        self.inspect_resource = inspect_resource
        self.category_suggestion = category_suggestion
        self.format_size = format_size
        self.search_preview = search_preview
        self.detail_capability = detail_capability

    def build(
        self,
        cached: dict[str, Any],
        routes: dict[str, Any],
        quark_importer: Any,
        cloud139_importer: Any,
        sixpan_importer: Any | None = None,
        btbtla_client: Any | None = None,
        hide_full_links: bool = True,
    ) -> dict[str, Any]:
        raw = cached.get("raw_data") if isinstance(cached.get("raw_data"), dict) else {}
        source_url = str(cached.get("source_url") or raw.get("url") or raw.get("source_url") or "")
        password = str(cached.get("password") or raw.get("password") or raw.get("pwd") or "")
        title = str(cached.get("title") or raw.get("title") or raw.get("note") or "未命名资源")
        source_type = str(cached.get("source_type") or raw.get("source_type") or "").strip().lower()
        if source_type == "bt_detail":
            return self._bt_detail(
                cached, raw, source_url, title, btbtla_client, hide_full_links
            )
        link = self.detect_link(source_url, routes, password=password)
        base_item = {
            **raw,
            "title": title,
            "url": source_url,
            "password": password,
            "source_type": link.source_type or cached.get("source_type") or raw.get("source_type"),
            "supported": link.supported,
            "route": link.route,
            "reason": link.reason,
            "size": cached.get("size") or raw.get("size") or raw.get("size_text") or "",
        }
        inspection = self.inspect_resource(
            link,
            title=title,
            raw=raw,
            quark_importer=quark_importer,
            cloud139_importer=cloud139_importer,
            sixpan_importer=sixpan_importer,
        )
        return {
            "public_id": cached.get("public_id"),
            "title": title,
            "source_type": link.source_type,
            "supported": link.supported,
            "reason": "已识别资源类型，提交时会检测可用性" if link.supported else link.reason,
            "route": link.route,
            "datetime": _datetime(raw, cached),
            "size_text": self.format_size(cached.get("size") or raw.get("size_text") or raw.get("size") or ""),
            "category_suggestion": self.category_suggestion(base_item),
            "instant_import": link.source_type == "cloud139",
            "speed_tag": "快速入库" if link.source_type == "cloud139" else "",
            "detail_capability": self.detail_capability(
                link.source_type,
                cloud139_importer=cloud139_importer,
                sixpan_importer=sixpan_importer,
            ),
            "link": {
                "source_type": link.source_type,
                "supported": link.supported,
                "route": link.route,
                "reason": link.reason,
                **_source_url_payload(source_url, hide_full_links, self.mask_url),
            },
            "inspection": inspection,
            "search_preview": self.search_preview(raw, hide_full_links=hide_full_links),
            "expires_at": cached.get("expires_at"),
        }

    def _bt_detail(
        self,
        cached: dict[str, Any],
        raw: dict[str, Any],
        source_url: str,
        title: str,
        client: Any,
        hide_full_links: bool,
    ) -> dict[str, Any]:
        inspection = self.inspect_bt(
            client,
            source_url,
            keyword=str(cached.get("keyword") or raw.get("keyword") or ""),
            title=title,
        )
        poster = str(raw.get("poster") or raw.get("cover") or raw.get("image_url") or "").strip()
        origin = str(raw.get("source_origin") or "").strip()
        return {
            "public_id": cached.get("public_id"), "title": title, "source_type": "bt_detail",
            "supported": True, "reason": "请选择下载资源，解析磁链后进入 BT 预览",
            "route": "btbtla_resolve", "datetime": _datetime(raw, cached),
            "size_text": self.format_size(cached.get("size") or raw.get("size_text") or raw.get("size") or ""),
            "poster": poster, "cover": poster, "image_url": poster,
            "source_origin": origin, "referer": origin,
            "category_suggestion": self.category_suggestion({**raw, "title": title}),
            "instant_import": False, "speed_tag": "",
            "detail_capability": {"available": True, "provider": "btbtla", "message": "请选择下载资源并解析磁链"},
            "link": {"source_type": "bt_detail", "supported": True, "route": "btbtla_resolve", "reason": "BTBTLA 影视详情", **_source_url_payload(source_url, hide_full_links, self.mask_url)},
            "inspection": inspection,
            "search_preview": self.search_preview(raw, hide_full_links=hide_full_links),
            "expires_at": cached.get("expires_at"),
        }


def _datetime(raw: dict[str, Any], cached: dict[str, Any]) -> Any:
    return raw.get("datetime") or raw.get("created_at") or raw.get("time") or cached.get("created_at") or ""


def _source_url_payload(url: str, hidden: bool, mask: Callable[[str], str]) -> dict[str, str]:
    return {"source_url_masked": mask(url)} if hidden else {"source_url": url}
