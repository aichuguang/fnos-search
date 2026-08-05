from __future__ import annotations

from typing import Any, Callable


class PublicSubmissionPreflightService:
    """Performs provider-specific checks before a public request is persisted."""

    def __init__(
        self,
        *,
        quark_summary: Callable[..., dict[str, Any]],
        cloud139_summary: Callable[..., dict[str, Any]],
        detail_capability: Callable[..., dict[str, Any]],
        format_size: Callable[[Any], str],
    ) -> None:
        self.quark_summary = quark_summary
        self.cloud139_summary = cloud139_summary
        self.detail_capability = detail_capability
        self.format_size = format_size

    def check(
        self,
        link: Any,
        *,
        title: str,
        raw: dict[str, Any],
        quark_importer: Any,
        cloud139_importer: Any,
        sixpan_importer: Any | None = None,
    ) -> dict[str, Any]:
        source = str(getattr(link, "source_type", "") or "").strip().lower()
        fallback_size = raw.get("size") or raw.get("size_text") or ""
        if source in {"quark", "uc"}:
            ok, data = quark_importer.check_share(getattr(link, "url", ""), title)
            inspection = self.quark_summary(ok, data, fallback_size=fallback_size)
            message = inspection.get("message") or ("资源链接有效" if ok else "资源链接无效或已失效")
            return self._checked("quark", ok, message, inspection)
        if source == "cloud139":
            if bool(getattr(link, "supported", False)) and not cloud139_importer.configured:
                inspection = self.cloud139_summary(
                    False,
                    {"success": False, "message": "快速入库服务未配置"},
                    fallback_size=fallback_size,
                )
                return {
                    "allowed": False,
                    "checked": False,
                    "provider": "cloud139",
                    "message": "提交前检测失败：快速入库服务未配置，请联系管理员",
                    "inspection": inspection,
                }
            if cloud139_importer.configured and cloud139_importer.check_before_save:
                ok, data = cloud139_importer.check_share(
                    getattr(link, "url", ""),
                    title=title,
                    password=str(getattr(link, "password", "") or ""),
                )
                inspection = self.cloud139_summary(ok, data, fallback_size=fallback_size)
                message = inspection.get("message") or ("资源有效，可快速入库" if ok else "资源可能已失效，请更换资源")
                return self._checked("cloud139", ok, message, inspection)
        if source in {"magnet", "torrent"} and not bool(
            sixpan_importer and getattr(sixpan_importer, "configured", False)
        ):
            message = "快速入库服务暂不可用，请联系管理员"
            return {
                "allowed": False,
                "checked": False,
                "provider": "sixpan",
                "message": f"提交前检测失败：{message}",
                "inspection": {
                    "provider": "sixpan",
                    "status": "unconfigured",
                    "success": False,
                    "message": message,
                    "summary": {"title": title, "total_size_text": self.format_size(fallback_size)},
                    "items": [],
                },
            }
        capability = self.detail_capability(
            source,
            cloud139_importer=cloud139_importer,
            sixpan_importer=sixpan_importer,
        )
        provider = capability.get("provider") or source or "unknown"
        message = capability.get("message") or "该来源暂不支持提交前详情检测"
        return {
            "allowed": True,
            "checked": False,
            "provider": provider,
            "message": message,
            "inspection": {
                "provider": provider,
                "status": "reserved",
                "success": False,
                "message": message,
                "summary": {"title": title, "total_size_text": self.format_size(fallback_size)},
                "items": [],
            },
        }

    @staticmethod
    def _checked(provider: str, ok: bool, message: str, inspection: dict[str, Any]) -> dict[str, Any]:
        return {
            "allowed": bool(ok),
            "checked": True,
            "provider": provider,
            "message": message if ok else f"提交前检测失败：{message}",
            "inspection": inspection,
        }
