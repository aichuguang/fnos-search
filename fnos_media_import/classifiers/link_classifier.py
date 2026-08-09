from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class LinkInfo:
    provider_kind: str
    source_form: str
    search_source_id: str
    url: str
    password: str = ""
    supported: bool = False
    reason: str = ""
    # Resource/search tables intentionally keep their classification field.
    # It is not part of the V1 detection API contract.
    resource_kind: str = "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_kind": self.provider_kind,
            "source_form": self.source_form,
            "search_source_id": self.search_source_id,
            "url": self.url,
            "password": self.password,
            "supported": self.supported,
            "reason": self.reason,
        }


PASSWORD_PATTERNS = [
    re.compile(r"(?:提取码|密码|访问码|口令)[:：\s]*([A-Za-z0-9]{2,8})"),
    re.compile(r"\?pwd=([A-Za-z0-9]{2,8})"),
]


def extract_password(text: str) -> str:
    for pattern in PASSWORD_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return ""


def normalize_url(url: str) -> str:
    return (url or "").strip()


def detect_link(url: str, *, password: str = "") -> LinkInfo:
    """Classify a source without choosing a destination or transfer strategy."""

    normalized = normalize_url(url)
    lower = normalized.lower()
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    host = parsed.netloc.lower()
    detected_password = password or extract_password(normalized)

    provider_kind = "unknown"
    source_form = ""
    search_source_id = ""
    resource_kind = "unknown"
    supported = False
    reason = "暂不支持该资源类型"

    if lower.startswith("magnet:?xt="):
        provider_kind = "sixpan"
        source_form = "magnet"
        search_source_id = "sixpan_magnet"
        resource_kind = "magnet"
        supported = True
        reason = "已识别六盘磁链来源"
    elif lower.endswith(".torrent") or ".torrent?" in lower:
        provider_kind = "sixpan"
        source_form = "magnet"
        search_source_id = "sixpan_magnet"
        resource_kind = "torrent"
        supported = True
        reason = "已识别六盘种子来源"
    elif "pan.quark.cn" in host:
        provider_kind = "quark"
        source_form = "share"
        search_source_id = "quark"
        resource_kind = "quark"
        supported = True
        reason = "已识别夸克分享来源"
    elif "drive.uc.cn" in host:
        resource_kind = "uc"
        reason = "UC 分享尚未接入 V1 Provider"
    elif "yun.139.com" in host or "caiyun.139.com" in host:
        provider_kind = "mobile"
        source_form = "share"
        search_source_id = "mobile"
        resource_kind = "cloud139"
        supported = True
        reason = "已识别移动云盘分享来源"
    elif "cloud.189.cn" in host:
        provider_kind = "tianyi"
        source_form = "share"
        search_source_id = "tianyi"
        resource_kind = "cloud189"
        supported = True
        reason = "已识别天翼云盘分享来源"
    elif "alipan.com" in host or "aliyundrive.com" in host:
        resource_kind = "aliyun"
        reason = "阿里云盘资源当前版本暂不支持"
    elif "pan.baidu.com" in host:
        resource_kind = "baidu"
        reason = "百度网盘资源当前版本暂不支持"

    return LinkInfo(
        provider_kind=provider_kind,
        source_form=source_form,
        search_source_id=search_source_id,
        url=normalized,
        password=detected_password,
        supported=supported,
        reason=reason,
        resource_kind=resource_kind,
    )


__all__ = ["LinkInfo", "detect_link", "extract_password", "normalize_url"]
