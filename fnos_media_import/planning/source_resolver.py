"""SourceResolver：从服务端可信资源记录解析来源描述。

不信任客户端 provider 字段。``provider_kind`` 与 ``source_form`` 只由服务端对
记录中的 URL 调用 ``detect_link`` 识别后派生；``canonical_source_id`` 由稳定的
分享/磁链标识计算；``selected_items_hash`` 只由服务端已确认的选中条目计算。
客户端提交的 ``provider_kind``/``account_id``/``mount_id``/``target_path`` 等
基础设施字段一律不进入来源描述（§15.2）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..domain.models import SelectionSnapshot, SourceDescriptor
from ..providers.manifests import BUILTIN_MANIFESTS
from . import VALID_SOURCE_FORMS, SourceResolutionError

_LOCAL_SOURCE = {
    "search_source_id": "local",
    "provider_kind": "local",
    "source_form": "local",
    "source_types": ["local"],
}


def _builtin_source_catalog() -> list[dict[str, Any]]:
    """Project built-in manifests into the source classification catalog."""

    result: list[dict[str, Any]] = []
    for provider_kind, provider_manifest in sorted(BUILTIN_MANIFESTS.items()):
        for raw in provider_manifest.search_sources:
            item = dict(raw)
            item["provider_kind"] = provider_kind
            item.setdefault("label", provider_manifest.display_name)
            item.setdefault("icon_text", provider_manifest.icon_text)
            result.append(item)
    return result


@dataclass(frozen=True)
class LinkClassification:
    provider_kind: str
    source_form: str
    search_source_id: str
    reason: str = ""


class SourceResolver:
    """把一个可信资源记录解析为 ``SourceDescriptor``。

    ``detect_link(url)`` 是服务端注入的 URL 分类函数，返回带
    ``provider_kind``/``source_form`` 的对象或 dict；兼容现有
    ``LinkInfo``（只有 ``source_type``）时通过翻译表映射。
    """

    def __init__(
        self,
        detect_link: Callable[..., Any],
        config: dict[str, Any] | None = None,
        source_catalog_provider: Callable[[], list[dict[str, Any]]] | None = None,
    ):
        self.detect_link = detect_link
        self.config = config if isinstance(config, dict) else {}
        self.source_catalog_provider = source_catalog_provider

    def resolve(self, record: dict[str, Any]) -> SourceDescriptor:
        record = record if isinstance(record, dict) else {}
        url = str(record.get("url") or record.get("source_url") or "").strip()
        if not url:
            raise SourceResolutionError("资源记录缺少 URL，无法识别来源")

        classification = self._classify(url)
        provider_kind = classification.provider_kind
        source_form = classification.source_form

        known = {item["provider_kind"] for item in self._source_catalog()}
        if provider_kind not in known:
            raise SourceResolutionError(f"来源 Provider 未注册：{provider_kind}")
        if source_form not in VALID_SOURCE_FORMS:
            raise SourceResolutionError(f"来源形态不受支持：{source_form}")

        canonical_id = _canonical_source_id(provider_kind, source_form, url)
        selection = SelectionSnapshot.from_payload(provider_kind, record)
        return SourceDescriptor(
            provider_kind=provider_kind,
            source_form=source_form,
            canonical_source_id=canonical_id,
            account_scope="default",
            selected_items_hash=selection.selection_hash,
            search_source_id=classification.search_source_id,
            expected_file_count=_expected_int(record, "expected_file_count", "file_count"),
            expected_total_bytes=_expected_int(record, "expected_total_bytes", "total_size", "total_bytes"),
            selection=selection,
        )

    def _classify(self, url: str) -> LinkClassification:
        result = self.detect_link(url)
        return _normalize_classification(result, url, self._source_catalog())

    def _source_catalog(self) -> list[dict[str, Any]]:
        if self.source_catalog_provider is None:
            # Isolated tests use the same manifests as production, not a second
            # hard-coded provider list.
            return [*_builtin_source_catalog(), dict(_LOCAL_SOURCE)]
        return [*self.source_catalog_provider(), dict(_LOCAL_SOURCE)]

    def set_source_catalog_provider(
        self,
        provider: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self.source_catalog_provider = provider


def _normalize_classification(
    result: Any,
    url: str,
    source_catalog: list[dict[str, Any]] | None = None,
) -> LinkClassification:
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

    catalog = list(source_catalog if source_catalog is not None else [*_builtin_source_catalog(), _LOCAL_SOURCE])
    matched = next(
        (
            item
            for item in catalog
            if provider == str(item.get("provider_kind") or "")
            or provider in {str(value) for value in item.get("source_types") or []}
        ),
        None,
    )
    if matched is not None:
        provider = str(matched.get("provider_kind") or provider)
        source_form = str(matched.get("source_form") or source_form)
    if matched is None:
        raise SourceResolutionError(f"无法识别来源 Provider：{provider or 'unknown'}")
    if source_form not in VALID_SOURCE_FORMS:
        raise SourceResolutionError(f"无法识别来源形态：{source_form or 'unknown'}")
    search_source_id = str(matched.get("search_source_id") or provider)
    return LinkClassification(
        provider_kind=provider,
        source_form=source_form,
        search_source_id=search_source_id,
        reason=reason,
    )


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
            return xt
        normalized = _normalize_magnet_for_identity(text)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    parsed = urlsplit(text if "://" in text else f"https://{text}")
    if provider_kind == "quark":
        pwd_id = _quark_pwd_id(parsed.path)
        if pwd_id:
            return pwd_id
    # A path basename is not a safe identity: many providers use generic
    # ``share.html``/``index`` endpoints and different links can therefore
    # collapse into one source record.  Keep the identity stable across query
    # parameter ordering and tracking parameters, then hash the complete
    # provider URL (including its share token/query and fragment).
    normalized = _normalize_url_for_identity(parsed)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


_TRACKING_QUERY_KEYS = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm"}
)


def _normalize_url_for_identity(parsed: Any) -> str:
    """Canonicalize a share URL without dropping provider access tokens."""
    scheme = str(getattr(parsed, "scheme", "") or "https").lower()
    hostname = str(getattr(parsed, "hostname", "") or "").lower()
    try:
        port = getattr(parsed, "port", None)
    except ValueError:
        port = None
    netloc = hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{netloc}:{port}"
    path = str(getattr(parsed, "path", "") or "/")
    if not path.startswith("/"):
        path = "/" + path
    path = "/".join(part for part in path.split("/") if part) or "/"
    path = "/" + path if path != "/" else path
    pairs = []
    for key, value in parse_qsl(str(getattr(parsed, "query", "") or ""), keep_blank_values=True):
        if str(key).lower() in _TRACKING_QUERY_KEYS:
            continue
        pairs.append((str(key), str(value)))
    pairs.sort()
    query = urlencode(pairs, doseq=True)
    fragment = str(getattr(parsed, "fragment", "") or "")
    return urlunsplit((scheme, netloc, path, query, fragment))


def _quark_pwd_id(path: str) -> str:
    if "/s/" in f"/{path}":
        return f"/{path}".split("/s/", 1)[1].split("/", 1)[0].split("#", 1)[0]
    return ""


def _magnet_xt(text: str) -> str:
    parsed = urlsplit(str(text or "").strip())
    if parsed.scheme.lower() != "magnet":
        return ""
    for key, raw_value in parse_qsl(parsed.query, keep_blank_values=True):
        if str(key).strip().lower() != "xt":
            continue
        value = str(raw_value or "").strip()
        prefix = "urn:btih:"
        if not value.lower().startswith(prefix):
            continue
        infohash = value[len(prefix) :].strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", infohash):
            return infohash.lower()
        if re.fullmatch(r"[A-Za-z2-7]{32}", infohash):
            try:
                decoded = base64.b32decode(infohash.upper(), casefold=True)
            except (binascii.Error, ValueError):
                continue
            if len(decoded) == 20:
                return decoded.hex()
    return ""


def _normalize_magnet_for_identity(text: str) -> str:
    """Build a deterministic fallback identity for malformed magnets."""

    parsed = urlsplit(str(text or "").strip())
    if parsed.scheme.lower() != "magnet":
        return str(text or "").strip()
    pairs = sorted(
        (str(key).strip().lower(), str(value).strip())
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    )
    return "magnet:?" + urlencode(pairs, doseq=True)


def _last_path_segment(path: str) -> str:
    segments = [seg for seg in (path or "").split("/") if seg]
    return segments[-1] if segments else ""


def _selected_items_hash(record: dict[str, Any]) -> str:
    """Compatibility helper returning the unified selection hash."""

    provider = str(record.get("provider_kind") or record.get("source_type") or "").strip()
    return SelectionSnapshot.from_payload(provider, record).selection_hash


def _hash_items(items: Any) -> str:
    if items in (None, [], {}):
        return ""
    try:
        canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = str(items)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expected_int(record: dict[str, Any], *keys: str) -> int:
    raw = record.get("raw_data") if isinstance(record.get("raw_data"), dict) else {}
    for key in keys:
        value = record.get(key, raw.get(key))
        try:
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0
