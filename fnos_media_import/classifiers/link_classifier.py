from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from ..constants import (
    ROUTE_CLOUD139_DIRECT,
    ROUTE_CLOUD189_DIRECT,
    ROUTE_QUARK_TO_MOBILE,
    ROUTE_SIXPAN_OFFLINE,
    ROUTE_UNSUPPORTED,
    SOURCE_ALIYUN,
    SOURCE_BAIDU,
    SOURCE_CLOUD139,
    SOURCE_CLOUD189,
    SOURCE_MAGNET,
    SOURCE_QUARK,
    SOURCE_TORRENT,
    SOURCE_UC,
    SOURCE_UNKNOWN,
)


@dataclass(frozen=True)
class LinkInfo:
    source_type: str
    url: str
    password: str = ""
    supported: bool = False
    route: str = ROUTE_UNSUPPORTED
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


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


def detect_link(url: str, routes: dict | None = None, password: str = "") -> LinkInfo:
    normalized = normalize_url(url)
    lower = normalized.lower()
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    host = parsed.netloc.lower()
    detected_password = password or extract_password(normalized)

    source_type = SOURCE_UNKNOWN
    route = ROUTE_UNSUPPORTED
    reason = "暂不支持该资源类型"

    if lower.startswith("magnet:?xt="):
        source_type = SOURCE_MAGNET
        route = ROUTE_SIXPAN_OFFLINE
        reason = "磁链资源，后续接入 6盘离线"
    elif lower.endswith(".torrent") or ".torrent?" in lower:
        source_type = SOURCE_TORRENT
        route = ROUTE_SIXPAN_OFFLINE
        reason = "种子资源，后续接入 6盘离线"
    elif "pan.quark.cn" in host:
        source_type = SOURCE_QUARK
        route = ROUTE_QUARK_TO_MOBILE
        reason = "夸克资源，走 Quark 转存到移动云线路"
    elif "drive.uc.cn" in host:
        source_type = SOURCE_UC
        route = ROUTE_QUARK_TO_MOBILE
        reason = "UC 资源，后续可复用夸克类线路"
    elif "yun.139.com" in host or "caiyun.139.com" in host:
        source_type = SOURCE_CLOUD139
        route = ROUTE_CLOUD139_DIRECT
        reason = "移动云资源，后续直接入库移动云"
    elif "cloud.189.cn" in host:
        source_type = SOURCE_CLOUD189
        route = ROUTE_CLOUD189_DIRECT
        reason = "天翼云资源，后续直接入库天翼云"
    elif "alipan.com" in host or "aliyundrive.com" in host:
        source_type = SOURCE_ALIYUN
        reason = "阿里云盘资源，当前版本暂不支持"
    elif "pan.baidu.com" in host:
        source_type = SOURCE_BAIDU
        reason = "百度网盘资源，当前版本暂不支持"

    route_config = (routes or {}).get(source_type, {})
    supported = _as_bool(route_config.get("enabled", False))
    if route_config.get("route"):
        route = route_config["route"]
    if not supported and source_type == SOURCE_QUARK and not route_config:
        # 默认第一版支持 Quark，允许单元测试和默认配置直接工作。
        supported = True

    if source_type == SOURCE_UNKNOWN:
        supported = False
    elif not supported:
        reason = f"已识别为 {source_type}，但当前配置未启用该线路"

    return LinkInfo(
        source_type=source_type,
        url=normalized,
        password=detected_password,
        supported=supported,
        route=route,
        reason=reason,
    )


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
