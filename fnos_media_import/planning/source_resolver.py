"""SourceResolver：从服务端可信资源记录解析来源描述。

不信任客户端 provider 字段。``provider_kind`` 与 ``source_form`` 只由服务端对
记录中的 URL 调用 ``detect_link`` 识别后派生；``canonical_source_id`` 由稳定的
分享/磁链标识计算；``selected_items_hash`` 只由服务端已确认的选中条目计算。
客户端提交的 ``provider_kind``/``account_id``/``mount_id``/``target_path`` 等
基础设施字段一律不进入来源描述（§15.2）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from ..domain.models import SourceDescriptor
from . import KNOWN_PROVIDER_KINDS, VALID_SOURCE_FORMS, SourceResolutionError

# 旧 LinkInfo.source_type -> (v4 provider_kind, source_form) 翻译表。
# V1.0 迁移期允许接入现有 detect_link（classifiers.link_classifier）返回的
# source_type；新的 v4 detect_link 可以直接返回 provider_kind/source_form。
_SOURCE_TYPE_TO_KIND = {
    "quark": ("quark", "share"),
    "cloud139": ("mobile", "share"),
    "cloud189": ("tianyi", "share"),
    "sixpan": ("sixpan", "share"),
    "magnet": ("sixpan", "magnet"),
    "torrent": ("sixpan", "magnet"),
    "local": ("local", "local"),
}


@dataclass(frozen=True)
class LinkClassification:
    provider_kind: str
    source_form: str
    reason: str = ""


class SourceResolver:
    """把一个可信资源记录解析为 ``SourceDescriptor``。

    ``detect_link(url)`` 是服务端注入的 URL 分类函数，返回带
    ``provider_kind``/``source_form`` 的对象或 dict；兼容现有
    ``LinkInfo``（只有 ``source_type``）时通过翻译表映射。
    """

    def __init__(self, detect_link: Callable[..., Any], config: dict[str, Any] | None = None):
        self.detect_link = detect_link
        self.config = config if isinstance(config, dict) else {}

    def resolve(self, record: dict[str, Any]) -> SourceDescriptor:
        record = record if isinstance(record, dict) else {}
        url = str(record.get("url") or record.get("source_url") or "").strip()
        if not url:
            raise SourceResolutionError("资源记录缺少 URL，无法识别来源")

        classification = self._classify(url)
        provider_kind = classification.provider_kind
        source_form = classification.source_form

        if provider_kind not in KNOWN_PROVIDER_KINDS:
            raise SourceResolutionError(f"来源 Provider 不受支持：{provider_kind}")
        if source_form not in VALID_SOURCE_FORMS:
            raise SourceResolutionError(f"来源形态不受支持：{source_form}")

        canonical_id = _canonical_source_id(provider_kind, source_form, url)
        selection_hash = _selected_items_hash(record)
        return SourceDescriptor(
            provider_kind=provider_kind,
            source_form=source_form,
            canonical_source_id=canonical_id,
            account_scope="default",
            selected_items_hash=selection_hash,
        )

    def _classify(self, url: str) -> LinkClassification:
        result = self.detect_link(url)
        return _normalize_classification(result, url)


def _normalize_classification(result: Any, url: str) -> LinkClassification:
    if isinstance(result, dict):
        provider = str(result.get("provider_kind") or result.get("kind") or result.get("source_type") or "").strip()
        source_form = str(result.get("source_form") or result.get("form") or "").strip()
        reason = str(result.get("reason") or "").strip()
    else:
        provider = (
            str(getattr(result, "provider_kind", "") or getattr(result, "kind", "") or getattr(result, "source_type", "") or "")
            .strip()
        )
        source_form = str(getattr(result, "source_form", "") or getattr(result, "form", "") or "").strip()
        reason = str(getattr(result, "reason", "") or "").strip()

    if source_form not in VALID_SOURCE_FORMS:
        mapped = _SOURCE_TYPE_TO_KIND.get(provider)
        if mapped:
            provider, source_form = mapped
    if provider not in KNOWN_PROVIDER_KINDS:
        raise SourceResolutionError(f"无法识别来源 Provider：{provider or 'unknown'}")
    if source_form not in VALID_SOURCE_FORMS:
        raise SourceResolutionError(f"无法识别来源形态：{source_form or 'unknown'}")
    return LinkClassification(provider_kind=provider, source_form=source_form, reason=reason)


def _canonical_source_id(provider_kind: str, source_form: str, url: str) -> str:
    stable = _stable_id(provider_kind, source_form, url)
    return f"{provider_kind}:{source_form}:{stable}"


def _stable_id(provider_kind: str, source_form: str, url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return hashlib.sha256(b"").hexdigest()[:24]
    if source_form == "magnet":
        xt = _magnet_xt(text)
        if xt:
            return xt.rsplit(":", 1)[-1]
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if provider_kind == "quark":
        pwd_id = _quark_pwd_id(parsed.path)
        if pwd_id:
            return pwd_id
    segment = _last_path_segment(parsed.path)
    if segment:
        return segment
    if parsed.hostname:
        return f"{parsed.hostname}:{_last_path_segment(parsed.path) or hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _quark_pwd_id(path: str) -> str:
    if "/s/" in f"/{path}":
        return f"/{path}".split("/s/", 1)[1].split("/", 1)[0].split("#", 1)[0]
    return ""


def _magnet_xt(text: str) -> str:
    if not text.lower().startswith("magnet:?"):
        return ""
    for part in text.split("&"):
        if part.lower().startswith("xt="):
            return part[3:].strip()
    return ""


def _last_path_segment(path: str) -> str:
    segments = [seg for seg in (path or "").split("/") if seg]
    return segments[-1] if segments else ""


def _selected_items_hash(record: dict[str, Any]) -> str:
    items = record.get("selected_items")
    if items is None:
        raw_data = record.get("raw_data") if isinstance(record.get("raw_data"), dict) else {}
        items = raw_data.get("selected_items")
        if items is None:
            selection = raw_data.get("selection")
            if isinstance(selection, dict):
                items = selection.get("selected_items")
    return _hash_items(items)


def _hash_items(items: Any) -> str:
    if items in (None, [], {}):
        return ""
    try:
        canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = str(items)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
