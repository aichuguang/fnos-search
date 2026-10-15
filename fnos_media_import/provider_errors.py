"""Conservative source-resource error mapping shared by cloud providers."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit


SHARE_INVALID_MESSAGE = "分享资源已失效或被删除，请更换有效链接后重试。"

_SHARE_READ_PATHS = {
    "quark": {
        "/1/clouddrive/share/sharepage/token",
        "/1/clouddrive/share/sharepage/detail",
    },
    "tianyi": {
        "/api/open/share/getshareinfobycode.action",
        "/api/open/share/getshareinfobycodev2.action",
        "/api/open/share/listsharedir.action",
    },
    "mobile": {
        "/yun-share/richlifeapp/devapp/ioutlink/getoutlinkinfov6",
    },
}
_SOURCE_OPERATION_PATHS = {
    "quark": {"/1/clouddrive/share/sharepage/save"},
    "tianyi": {"/api/open/share/checkaccesscode.action"},
    "mobile": {
        "/yun-share/richlifeapp/devapp/ibatchoprtask/createouterlinkbatchoprtask",
    },
    "sixpan": {"/v6/offline_task/parse", "/v6/offline_task/add"},
}
_SHARE_CODES = {
    "shareinvalid", "invalidshare", "invalidsharecode", "sharenotfound",
    "shareinfonotfound", "sharenotexist", "sharenotexists", "shareexpired",
    "shareoverdue", "sharedeleted", "sharecanceled", "sharecancelled",
    "sharelinknotfound", "sharelinkexpired", "sharelinkinvalid",
    "outlinknotfound", "outlinkexpired", "outlinkdeleted", "outlinkinvalid",
}
_SOURCE_CODES = {
    "resourcenotfound", "resourcenotexist", "resourcenotexists", "resourceexpired",
    "resourcedeleted", "filenotfound", "filenotexist", "filenotexists", "filedeleted",
    "sourcenotfound", "sourceexpired", "torrentnotfound",
}
_AUTH_CODES = {
    "invalidsessionkey", "invalidaccesstoken", "userinvalidopentoken",
    "authexpired", "authrequired", "tokenexpired", "accesstokenexpired",
    "accesscoderequired", "accesscodeinvalid", "invalidaccesscode",
    "passworderror", "invalidpassword", "permissiondenied", "accessdenied",
    "ratelimited", "toomanyrequests", "checkiperror", "sessionipmismatch",
}
_NON_RESOURCE_MESSAGE = re.compile(
    r"提取码|访问码|验证码|口令|密码|登录|登陆|认证|鉴权|无权限|没有权限|没有访问权限|权限不足|请求过于频繁|"
    r"\b(?:unauthorized|forbidden|permission|access denied|rate limit|too many requests)\b|"
    r"\b(?:cookie|session|access[ _-]?token|authorization)\b",
    re.IGNORECASE,
)
_MISSING_RESOURCE_MESSAGE = re.compile(
    r"(?:分享(?:链接|资源|文件)?|共享(?:链接|文件)?|外链|源文件|资源|文件|链接)"
    r"(?:已经被|已经|已被|已|被)?(?:失效|删除|取消|过期|不存在)|"
    r"\b(?:share(?:d)?(?:\s+(?:link|resource|file))?|resource|file|torrent|source)\s+"
    r"(?:(?:has|have)\s+(?:been\s+)?|(?:is|was)\s+|already\s+)?"
    r"(?:expired|deleted|removed|cancelled|canceled|not\s+found|does\s+not\s+exist|"
    r"no\s+longer\s+(?:available|exists))\b",
    re.IGNORECASE,
)


def is_share_resource_unavailable(
    provider: str,
    *,
    path: str = "",
    http_status: int | None = None,
    code: Any = "",
    message: str = "",
    source_operation: bool = False,
) -> bool:
    """Do not confuse a missing API route, expired login or outage with a share.

    Bare 404/410 is meaningful only on known share-reading endpoints.  Save,
    offline, account and destination-file endpoints need explicit source/share
    business evidence; in particular, SixPan's generic API 404 is not enough.
    """
    normalized_path = urlsplit(str(path or "")).path.rstrip("/").lower()
    normalized_code = re.sub(r"[^a-z0-9]", "", str(code or "").lower())
    status = int(http_status or 0)
    if status in {401, 408, 425, 429} or status >= 500:
        return False
    if normalized_code in _AUTH_CODES or _NON_RESOURCE_MESSAGE.search(str(message or "")):
        return False
    if provider == "quark" and normalized_code == "40001":
        return False
    if provider == "mobile" and normalized_code in {"04000005", "05050006"}:
        return False

    share_read = normalized_path in _SHARE_READ_PATHS.get(provider, set())
    source_operation = bool(
        source_operation
        or share_read
        or normalized_path in _SOURCE_OPERATION_PATHS.get(provider, set())
    )
    if normalized_code in _SHARE_CODES:
        return True
    if provider == "quark" and (
        normalized_code in {"41001", "41002", "41003"}
        or (normalized_code == "10000" and source_operation)
    ):
        return True
    if share_read and status in {404, 410}:
        return True
    return source_operation and bool(
        normalized_code in _SOURCE_CODES
        or _MISSING_RESOURCE_MESSAGE.search(str(message or ""))
    )


def share_error_details(
    provider: str,
    *,
    path: str = "",
    http_status: int | None = None,
    code: Any = "",
    message: str = "",
) -> dict[str, Any]:
    """Keep bounded, credential-redacted diagnostics out of the friendly text."""
    details = {
        "provider_code": str(code if code is not None else ""),
        "http_status": int(http_status or 0),
        "provider_path": urlsplit(str(path or "")).path,
        "technical_message": _diagnostic_message(message),
    }
    logging.getLogger("fnos_media_import.providers").warning(
        "event=share_resource_unavailable provider=%s path=%s http_status=%s code=%s detail=%s",
        provider,
        details["provider_path"],
        details["http_status"],
        details["provider_code"],
        details["technical_message"],
    )
    return details


def provider_error_details(exc: BaseException) -> dict[str, Any]:
    details = getattr(exc, "details", None)
    result = dict(details) if isinstance(details, dict) else {}
    result["error_code"] = str(getattr(exc, "code", "") or result.get("error_code") or "")
    status = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    if status is not None:
        result.setdefault("http_status", int(status))
    result["technical_message"] = _diagnostic_message(str(result.get("technical_message") or exc))
    return result


def _diagnostic_message(message: str) -> str:
    text = re.sub(
        r'''(?i)(["']?\b(?:authorization|cookie|set-cookie)\b["']?\s*[:=]\s*)'''
        r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|(?:Basic|Bearer)\s+[^\s,;]+|[^\r\n}]+)''',
        lambda match: f"{match.group(1)}[REDACTED]",
        str(message or ""),
    )
    return re.sub(
        r'''(?i)(["']?\b(?:sessionkey|sessionsecret|session_key|session_secret|access_token|'''
        r'''refresh_token|accesstoken|refreshtoken|client_secret|clientsecret|stoken|share_token|sharetoken|'''
        r'''password|passwd|passcode|accesscode|'''
        r'''__puus|__pus)\b["']?\s*[:=]\s*)'''
        r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&}]+)''',
        lambda match: f"{match.group(1)}[REDACTED]",
        text,
    )[:500]
