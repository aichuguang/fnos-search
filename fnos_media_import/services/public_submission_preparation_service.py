from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class PublicSubmissionPreparationError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PreparedPublicSubmission:
    public_id: str
    payload: dict[str, Any]
    cached: dict[str, Any] | None
    link: Any
    category_key: str
    category: dict[str, Any]
    scoped_selection: bool


class PublicSubmissionPreparationService:
    """Validates and normalizes untrusted public submission input."""

    def __init__(
        self,
        *,
        search_cache: Callable[[str], dict[str, Any] | None],
        categories: Callable[[], dict[str, Any]],
        category: Callable[[str], dict[str, Any]],
        routes: Callable[[], dict[str, Any]],
        limited_text: Callable[..., str],
        validate_url: Callable[[Any, dict[str, Any]], str],
        safe_string_list: Callable[..., list[str]],
        safe_quark_selection: Callable[[Any], Any],
        safe_cloud139_selection: Callable[[Any], Any],
        detect_link: Callable[..., Any],
        security_config: Callable[[], dict[str, Any]],
        config_int: Callable[[dict[str, Any], str, int], int],
    ) -> None:
        self.search_cache = search_cache
        self.categories = categories
        self.category = category
        self.routes = routes
        self.limited_text = limited_text
        self.validate_url = validate_url
        self.safe_string_list = safe_string_list
        self.safe_quark_selection = safe_quark_selection
        self.safe_cloud139_selection = safe_cloud139_selection
        self.detect_link = detect_link
        self.security_config = security_config
        self.config_int = config_int

    def prepare(self, payload: dict[str, Any]) -> PreparedPublicSubmission:
        security = self.security_config()
        public_id = self.limited_text(
            payload.get("public_id") or payload.get("resource_id"),
            "资源编号",
            self.config_int(security, "max_token_length", 80),
        )
        note = self.limited_text(
            payload.get("note"), "备注", self.config_int(security, "max_note_length", 500)
        )
        # Apply the same title bound to cached and manual submissions.  Passing
        # ``0`` used to mean "unlimited" to ``_limited_text`` and allowed a
        # public caller to persist arbitrarily long provider/path titles.
        max_title_length = self.config_int(security, "max_title_length", 300)
        manual_title = self.limited_text(
            payload.get("preferred_title") or payload.get("title"),
            "资源标题",
            max_title_length,
        )
        manual_password = self.limited_text(
            payload.get("password"), "提取码", self.config_int(security, "max_password_length", 32)
        )
        cached = self.search_cache(public_id) if public_id else None
        prepared = dict(payload)
        if cached:
            self._apply_cached_resource(prepared, cached, manual_title, manual_password)
        elif not prepared.get("url"):
            raise PublicSubmissionPreparationError("资源不存在或提交已过期，请重新搜索", 404)
        if not cached and not str(manual_title or prepared.get("title") or "").strip():
            raise PublicSubmissionPreparationError("资源标题不能为空")

        prepared["title"] = manual_title or self.limited_text(
            prepared.get("title") or "手动提交资源",
            "资源标题",
            max_title_length,
        )
        prepared["url"] = self.validate_url(prepared.get("url"), security)
        prepared["password"] = str(prepared.get("password") or "")
        prepared["note"] = note
        sixpan_selection = (
            dict(prepared["sixpan_selection"])
            if isinstance(prepared.get("sixpan_selection"), dict)
            else None
        )
        ignore_source = prepared.get("ignore_files")
        if ignore_source is None and sixpan_selection is not None:
            # Some internal/admin callers persist the effective ignore list
            # only inside sixpan_selection.  Do not replace it with an empty
            # top-level list while normalizing a later submission.
            ignore_source = sixpan_selection.get("ignore_files")
        prepared["ignore_files"] = self.safe_string_list(
            ignore_source, max_items=2000, max_length=512
        )
        prepared["quark_selection"] = self.safe_quark_selection(prepared.get("quark_selection"))
        prepared["cloud139_selection"] = self.safe_cloud139_selection(prepared.get("cloud139_selection"))
        if sixpan_selection is not None or prepared["ignore_files"]:
            prepared["sixpan_selection"] = {
                **(sixpan_selection or {}),
                "ignore_files": prepared["ignore_files"],
            }
        else:
            prepared["sixpan_selection"] = {}

        link = self.detect_link(
            prepared["url"], self.routes(), password=prepared["password"]
        )
        category_key = str(prepared.get("category") or "").strip()
        if not category_key:
            raise PublicSubmissionPreparationError("资源分类不能为空")
        if category_key not in self.categories():
            raise PublicSubmissionPreparationError("资源分类不存在")
        return PreparedPublicSubmission(
            public_id=public_id,
            payload=prepared,
            cached=cached,
            link=link,
            category_key=category_key,
            category=self.category(category_key),
            # A SixPan file selection is just as important as a Quark/139
            # selection.  URL-only duplicate detection cannot distinguish two
            # submissions that ignore different files, so let the durable job
            # identity (which includes the canonical ignore list) decide.
            scoped_selection=bool(
                prepared.get("quark_selection")
                or prepared.get("cloud139_selection")
                or prepared.get("sixpan_selection")
                or prepared.get("ignore_files")
            ),
        )

    @staticmethod
    def _apply_cached_resource(
        payload: dict[str, Any],
        cached: dict[str, Any],
        manual_title: str,
        manual_password: str,
    ) -> None:
        payload["title"] = manual_title or cached.get("title")
        payload["url"] = cached.get("source_url")
        payload["password"] = manual_password or cached.get("password") or ""
        raw = cached.get("raw_data") if isinstance(cached.get("raw_data"), dict) else {}
        source_type = str(cached.get("source_type") or raw.get("source_type") or "").strip().lower()
        if source_type == "bt_detail":
            raise PublicSubmissionPreparationError(
                "请先在资源详情中选择下载资源并解析磁链，再提交入库"
            )
