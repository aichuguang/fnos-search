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
        tianyi_summary: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.quark_summary = quark_summary
        self.cloud139_summary = cloud139_summary
        self.detail_capability = detail_capability
        self.format_size = format_size
        self.tianyi_summary = tianyi_summary

    def check(
        self,
        link: Any,
        *,
        title: str,
        raw: dict[str, Any],
        quark_importer: Any,
        cloud139_importer: Any,
        sixpan_importer: Any | None = None,
        tianyi_importer: Any | None = None,
    ) -> dict[str, Any]:
        source = str(getattr(link, "provider_kind", "") or "").strip().lower()
        fallback_size = raw.get("size") or raw.get("size_text") or ""
        if source == "quark":
            ok, data = quark_importer.check_share(getattr(link, "url", ""), title)
            inspection = self.quark_summary(ok, data, fallback_size=fallback_size)
            message = inspection.get("message") or ("资源链接有效" if ok else "资源链接无效或已失效")
            return self._checked("quark", ok, message, inspection)
        if source == "mobile":
            if bool(getattr(link, "supported", False)) and not cloud139_importer.configured:
                inspection = self.cloud139_summary(
                    False,
                    {"success": False, "message": "快速入库服务未配置"},
                    fallback_size=fallback_size,
                )
                return {
                    "allowed": False,
                    "checked": False,
                    "provider": "mobile",
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
                return self._checked("mobile", ok, message, inspection)
        if source == "tianyi":
            summary = self.tianyi_summary
            configured = self._configured(tianyi_importer)
            if not configured:
                message = "\u5929\u7ffc\u4e91\u8d26\u53f7\u672a\u914d\u7f6e\u6216\u672a\u6fc0\u6d3b\uff0c\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458"
                inspection = (
                    summary(False, {"success": False, "message": message}, fallback_size=fallback_size)
                    if callable(summary)
                    else {
                        "provider": "tianyi",
                        "status": "unconfigured",
                        "success": False,
                        "message": message,
                        "summary": {"title": title, "total_size_text": self.format_size(fallback_size)},
                        "items": [],
                    }
                )
                inspection = {
                    **inspection,
                    "provider": "tianyi",
                    "status": "unconfigured",
                    "success": False,
                    "message": message,
                }
                return {
                    "allowed": False,
                    "checked": False,
                    "provider": "tianyi",
                    "message": f"\u63d0\u4ea4\u524d\u68c0\u6d4b\u5931\u8d25\uff1a{message}",
                    "inspection": inspection,
                }
            try:
                ok, data = tianyi_importer.check_share(
                    getattr(link, "url", ""),
                    title,
                    str(getattr(link, "password", "") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                ok, data = False, {"success": False, "message": str(exc)}
            data_payload = data if isinstance(data, dict) else {"data": data}
            inspection = (
                summary(ok, data_payload, fallback_size=fallback_size)
                if callable(summary)
                else {
                    "provider": "tianyi",
                    "status": "ok" if ok else "failed",
                    "success": bool(ok),
                    "message": str(data_payload.get("message") or ""),
                    "summary": {"title": title, "total_size_text": self.format_size(fallback_size)},
                    "items": [],
                }
            )
            message = inspection.get("message") or (
                "\u5929\u7ffc\u4e91\u8d44\u6e90\u6709\u6548"
                if ok
                else "\u5929\u7ffc\u4e91\u8d44\u6e90\u65e0\u6548\u6216\u5df2\u5931\u6548"
            )
            return self._checked("tianyi", ok, message, inspection)
        if source == "sixpan" and not bool(
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
            tianyi_importer=tianyi_importer,
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

    @staticmethod
    def _configured(importer: Any | None) -> bool:
        if importer is None:
            return False
        configured = getattr(importer, "configured", None)
        if configured is not None:
            try:
                return bool(configured)
            except Exception:  # noqa: BLE001
                return False
        checker = getattr(importer, "check_configuration", None)
        if not callable(checker):
            return True
        try:
            result = checker()
        except Exception:  # noqa: BLE001
            return False
        if isinstance(result, dict):
            return bool(result.get("ok"))
        return bool(getattr(result, "ok", result))
