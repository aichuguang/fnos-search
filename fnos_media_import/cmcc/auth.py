from __future__ import annotations

import base64
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import requests

from ..config import CMCC_DEFAULT_HOST

logger = logging.getLogger(__name__)

CMCC_TOKEN_REFRESH_URL = "https://aas.caiyun.feixin.10086.cn:443/tellin/authTokenRefresh.do"
CMCC_TOKEN_REFRESH_MARGIN_SECONDS = 15 * 24 * 60 * 60


class CmccAuthError(RuntimeError):
    """Base class for CMCC auth errors."""


class CmccAuthExpired(CmccAuthError):
    """Raised when CMCC reports an expired/invalid authorization."""


class CmccAuthConfigError(CmccAuthError):
    """Raised when local WebAPI auth configuration is incomplete."""


@dataclass
class CmccAuthCredentials:
    """139 WebAPI Basic authorization.

    这里只保留 Web 端抓包得到的 `Authorization: Basic ...`。
    上传链路只接收浏览器 WebAPI 的 Basic 认证值。
    """

    authorization: str
    phone: str = ""

    def headers(self) -> dict[str, str]:
        return {"Authorization": self.authorization}


class CmccAuthProvider:
    """Reads CMCC WebAPI Basic authorization from the active account only."""

    AUTH_ERROR_CODES = {"04000005", "05050006", "HTTP_401", "HTTP_403", "401", "403"}

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        authorization_update_callback: Callable[[dict[str, Any]], bool] | None = None,
        refresh_margin_seconds: int = CMCC_TOKEN_REFRESH_MARGIN_SECONDS,
        refresh_timeout: int = 20,
    ) -> None:
        self.config = config if isinstance(config, dict) else {}
        configured_host = str(self.config.get("host") or CMCC_DEFAULT_HOST).rstrip("/")
        if "miniapp.yun.139.com" in configured_host:
            configured_host = CMCC_DEFAULT_HOST
        if configured_host.rstrip("/") == "https://personal-kd-njs.yun.139.com":
            configured_host = "https://personal-kd-njs.yun.139.com/hcy"
        self.host = configured_host
        self.phone = str(self.config.get("phone") or "").strip()
        self._authorization_update_callback = (
            authorization_update_callback if callable(authorization_update_callback) else None
        )
        self._refresh_margin_seconds = max(0, int(refresh_margin_seconds))
        self._refresh_timeout = max(1, int(refresh_timeout))
        self._credential_key = (
            "access_token"
            if str(self.config.get("access_token") or "").strip()
            else "authorization"
        )
        self._refresh_lock = threading.Lock()
        self._credentials: CmccAuthCredentials | None = None

    def invalidate(self) -> None:
        self._credentials = None

    def reload(self) -> CmccAuthCredentials:
        """Force a refresh after Provider rejects the current token."""
        self.invalidate()
        return self.get_credentials(force_refresh=True)

    def get_headers(self) -> dict[str, str]:
        return self.get_credentials().headers()

    def get_credentials(self, *, force_refresh: bool = False) -> CmccAuthCredentials:
        if self._credentials is not None and not force_refresh:
            if not self._should_refresh(self._credentials.authorization):
                return self._credentials

        with self._refresh_lock:
            if self._credentials is not None and not force_refresh:
                if not self._should_refresh(self._credentials.authorization):
                    return self._credentials

            raw_authorization = str(
                self.config.get("access_token") or self.config.get("authorization") or ""
            ).strip()
            if not raw_authorization:
                raise CmccAuthConfigError("CMCC WebAPI 只接受 Authorization: Basic ...；请配置 CMCC_ACCESS_TOKEN=Basic ...")

            authorization = self._normalize_basic_authorization(raw_authorization)
            if force_refresh or self._should_refresh(authorization):
                authorization = self._refresh_authorization(authorization)
            phone = self.phone or self._extract_phone_from_basic(authorization)
            self._credentials = CmccAuthCredentials(authorization=authorization, phone=phone)
            return self._credentials

    @staticmethod
    def _normalize_basic_authorization(value: str) -> str:
        text = str(value or "").strip()
        text = re.sub(r"^Authorization\s*:\s*", "", text, flags=re.IGNORECASE).strip()
        if not text:
            raise CmccAuthConfigError("CMCC WebAPI Authorization 为空；请粘贴 Basic ...")

        if re.match(r"^Basic\s+", text, flags=re.IGNORECASE):
            token = re.sub(r"^Basic\s+", "", text, flags=re.IGNORECASE).strip()
            if not token:
                raise CmccAuthConfigError("CMCC WebAPI Basic token 为空")
            return f"Basic {token}"

        if re.match(r"^(Bearer|Bre|accessToken)\b", text, flags=re.IGNORECASE):
            raise CmccAuthConfigError("CMCC WebAPI 上传只接受 Authorization: Basic ...，不要填 Bearer/accessToken/skill-token")

        # 兼容只粘贴 Basic 后面的 base64 值。
        if re.fullmatch(r"[A-Za-z0-9+/=_-]+", text):
            return f"Basic {text}"

        raise CmccAuthConfigError("CMCC WebAPI Authorization 格式不对；需要 Basic ...")

    @staticmethod
    def _decode_basic(authorization: str) -> str:
        token = re.sub(r"^Basic\s+", "", authorization or "", flags=re.IGNORECASE).strip()
        if not token:
            return ""
        try:
            padded = token + ("=" * (-len(token) % 4))
            return base64.b64decode(padded).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    @classmethod
    def _authorization_expiration_ms(cls, authorization: str) -> int | None:
        """Return the embedded 139 token expiry, or None for legacy formats."""
        decoded = cls._decode_basic(authorization)
        if not decoded:
            return None
        parts = decoded.split(":", 2)
        if len(parts) < 3:
            return None
        token_parts = parts[2].split("|")
        if len(token_parts) < 4:
            return None
        try:
            return int(token_parts[3])
        except (TypeError, ValueError):
            return None

    def _should_refresh(self, authorization: str) -> bool:
        expiration_ms = self._authorization_expiration_ms(authorization)
        if expiration_ms is None:
            return False
        remaining_ms = expiration_ms - int(time.time() * 1000)
        return remaining_ms <= self._refresh_margin_seconds * 1000

    def _refresh_authorization(self, authorization: str) -> str:
        """Rotate the 139 token with OpenList's authTokenRefresh grant.

        The Basic payload is ``pc:<phone>:<token>|<...>|<expiry-ms>``.  Only the
        token segment is sent to the refresh endpoint; the account segment is
        the phone number used by the WebAPI.
        """
        decoded = self._decode_basic(authorization)
        if not decoded:
            raise CmccAuthExpired("139 授权自动续期失败：Authorization 不是可解析的 Basic 凭证")
        parts = decoded.split(":", 2)
        if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
            raise CmccAuthExpired("139 授权自动续期失败：Basic 凭证缺少账号或 token 字段")

        body = (
            "<root><token>{token}</token><account>{account}</account>"
            "<clienttype>656</clienttype></root>"
        ).format(token=escape(parts[2]), account=escape(parts[1]))
        try:
            response = requests.post(
                CMCC_TOKEN_REFRESH_URL,
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/xml; charset=UTF-8"},
                timeout=self._refresh_timeout,
            )
        except requests.RequestException as exc:
            raise CmccAuthExpired(f"139 授权自动续期请求失败：{exc}") from exc
        if response.status_code != 200:
            raise CmccAuthExpired(f"139 授权自动续期失败：HTTP {response.status_code}")
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise CmccAuthExpired("139 授权自动续期失败：响应不是有效 XML") from exc
        return_code = str(root.findtext("return") or "").strip()
        new_token = str(root.findtext("token") or "").strip()
        description = str(root.findtext("desc") or "").strip()
        if return_code not in {"0", "0000"} or not new_token:
            detail = description or return_code or "未知错误"
            raise CmccAuthExpired(f"139 授权自动续期失败：{detail}")

        new_authorization = "Basic " + base64.b64encode(
            f"{parts[0]}:{parts[1]}:{new_token}".encode("utf-8")
        ).decode("ascii")
        self.config[self._credential_key] = new_authorization
        if self.config.get("access_token"):
            self.config["access_token"] = new_authorization
        if self.config.get("authorization"):
            self.config["authorization"] = new_authorization
        self._persist_authorization(new_authorization)
        return new_authorization

    def _persist_authorization(self, authorization: str) -> None:
        if self._authorization_update_callback is None:
            return
        patch = {"authorization": authorization}
        if self._credential_key == "access_token" or self.config.get("access_token"):
            # Keep legacy aliases in sync so both the manifest validator and
            # CmccAuthProvider read the rotated token.
            patch["access_token"] = authorization
        try:
            persisted = self._authorization_update_callback(patch)
            if not persisted:
                logger.warning("139 授权已自动续期，但持久化回调返回失败")
        except Exception as exc:  # noqa: BLE001 - 当前任务仍可使用内存中的新 token
            logger.warning("139 授权已自动续期，但持久化失败：%s", exc)

    @staticmethod
    def _extract_phone_from_basic(authorization: str) -> str:
        decoded = CmccAuthProvider._decode_basic(authorization)
        if not decoded:
            return ""
        for part in decoded.split(":"):
            text = part.strip()
            if re.fullmatch(r"1\d{10}", text):
                return text
        return ""

    @classmethod
    def is_auth_error(cls, payload: Any = None, *, status_code: int | None = None, message: str = "") -> bool:
        if status_code in {401, 403}:
            return True
        code = ""
        if isinstance(payload, dict):
            code = str(payload.get("code") or payload.get("errorCode") or "").strip().upper()
            message = message or str(payload.get("message") or payload.get("msg") or payload.get("desc") or "")
        if code in cls.AUTH_ERROR_CODES:
            return True
        lower = str(message or "").lower()
        return any(token in lower for token in ("authorization", "basic", "token", "access", "401", "403", "认证", "登录"))
