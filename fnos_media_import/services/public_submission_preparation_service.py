from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable


class PublicSubmissionPreparationError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# 分享文案常见截断标记：介绍段、大小、标签、下载提示、链接。
_SHARE_TITLE_CUT_RE = re.compile(
    r"📜|📁|🏷|⬇️|⬇|🔍|【评论区|大小[:：]|标签[:：]|介绍[:：]|简介[:：]|剧情[:：]|https?://"
)
# 分享文案常见分类前缀，如「电影🗄」「剧集🗄」。
_SHARE_TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:电影|剧集|连续剧|电视剧|动漫|综艺|纪录片|资源分享?|分享)\s*(?:[🗄📌🏷🎬📺]|[:：])?\s*"
)


def normalize_share_title(value: Any, max_length: int) -> str:
    """从整段分享文案中提取干净标题，超长截断而不是拒绝。

    搜索结果缓存里的标题常带介绍、大小、标签、链接等整段文案，
    访客提交这类缓存资源时按纯标题校验会误伤，这里统一收敛为
    标记前的核心标题并截断到上限。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    cut = _SHARE_TITLE_CUT_RE.search(raw)
    text = raw[: cut.start()].strip() if cut else raw
    text = _SHARE_TITLE_PREFIX_RE.sub("", text, count=1)
    text = re.sub(r"\s+", " ", text).strip(" \t-_|·•，,")
    if not text:
        text = re.sub(r"\s+", " ", raw)
    if max_length > 0 and len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


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
        limited_text: Callable[..., str],
        validate_url: Callable[[Any, dict[str, Any]], str],
        safe_string_list: Callable[..., list[str]],
        safe_quark_selection: Callable[[Any], Any],
        safe_cloud139_selection: Callable[[Any], Any],
        detect_link: Callable[..., Any],
        security_config: Callable[[], dict[str, Any]],
        config_int: Callable[[dict[str, Any], str, int], int],
        safe_tianyi_selection: Callable[[Any], Any] | None = None,
    ) -> None:
        self.search_cache = search_cache
        self.categories = categories
        self.category = category
        self.limited_text = limited_text
        self.validate_url = validate_url
        self.safe_string_list = safe_string_list
        self.safe_quark_selection = safe_quark_selection
        self.safe_cloud139_selection = safe_cloud139_selection
        self.safe_tianyi_selection = safe_tianyi_selection or (lambda _value: {})
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
        # 访客不选择保存位置：payload 不得携带目标或任何基础设施字段。
        # 目标由服务端按来源 + 分类从 source_destination_assignments 解析。
        forbidden_fields = (
            "destination_id",
            "account_id",
            "provider_kind",
            "mount_id",
            "options_revision",
            "driver",
            "backend",
            "remote",
            "root",
            "absolute_path",
            "target_path",
            "target_path_override",
            "cloud139_target_path_override",
            "context",
            "update_context",
            "organizer_context",
            "target_root_is_resource",
        )
        forbidden = sorted(field for field in forbidden_fields if field in prepared)
        if forbidden:
            raise PublicSubmissionPreparationError(
                f"保存位置由管理员配置，提交不得包含：{', '.join(forbidden)}",
                400,
            )
        if cached:
            self._apply_cached_resource(prepared, cached, manual_title, manual_password)
        elif not prepared.get("url"):
            raise PublicSubmissionPreparationError("资源不存在或提交已过期，请重新搜索", 404)
        if not cached and not str(manual_title or prepared.get("title") or "").strip():
            raise PublicSubmissionPreparationError("资源标题不能为空")

        prepared["title"] = manual_title or normalize_share_title(
            prepared.get("title") or "手动提交资源",
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
        prepared["tianyi_selection"] = self.safe_tianyi_selection(prepared.get("tianyi_selection"))
        if sixpan_selection is not None or prepared["ignore_files"]:
            prepared["sixpan_selection"] = {
                **(sixpan_selection or {}),
                "ignore_files": prepared["ignore_files"],
            }
        else:
            prepared["sixpan_selection"] = {}

        link = self.detect_link(prepared["url"], password=prepared["password"])
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
                or prepared.get("tianyi_selection")
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
