from __future__ import annotations

from typing import Any, Callable


class PublicSixpanPreviewService:
    """Resolves cached or manual magnet/torrent input into a SixPan preview."""

    def __init__(
        self,
        *,
        cache: Any,
        importer: Callable[[], Any],
        validate_url: Callable[[Any], str],
        detect_link: Callable[..., Any],
        routes: Callable[[], dict[str, Any]],
        summarize: Callable[[Any], dict[str, Any]],
    ) -> None:
        self.cache = cache
        self.importer = importer
        self.validate_url = validate_url
        self.detect_link = detect_link
        self.routes = routes
        self.summarize = summarize

    def preview(
        self,
        *,
        public_id: str = "",
        url: Any = "",
        title: str = "",
        password: str = "",
    ) -> tuple[dict[str, Any], int]:
        importer = self.importer()
        if not importer or not getattr(importer, "configured", False):
            return {"success": False, "message": "快速入库服务暂不可用，请联系管理员", "items": []}, 400
        cached = self.cache.get_search_cache(public_id) if public_id else None
        raw = cached.get("raw_data") if cached and isinstance(cached.get("raw_data"), dict) else {}
        source_url = str(cached.get("source_url") or raw.get("url") or raw.get("source_url") or "") if cached else ""
        if not source_url:
            try:
                source_url = self.validate_url(url)
            except Exception as exc:
                return {"success": False, "message": str(exc), "items": []}, 400
        resolved_title = title or (
            str(cached.get("title") or raw.get("title") or raw.get("note") or "BT 入库资源")
            if cached else "BT 入库资源"
        )
        link = self.detect_link(source_url, self.routes(), password=password)
        if str(link.source_type or "").lower() not in {"magnet", "torrent"}:
            return {"success": False, "message": "该资源暂不支持内容预览", "items": []}, 400
        try:
            data = importer.parse_resource(
                title=resolved_title,
                source_url=link.url,
                source_type=link.source_type,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "message": f"内容预览失败：{exc}",
                "items": [],
                "fallback_submit_all": True,
            }, 502
        return {"success": True, **self.summarize(data)}, 200
