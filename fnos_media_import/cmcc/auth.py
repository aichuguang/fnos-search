from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, CMCC_DEFAULT_HOST


class CmccAuthError(RuntimeError):
    """Base class for CMCC auth errors."""


class CmccAuthExpired(CmccAuthError):
    """Raised when CMCC reports an expired/invalid authorization."""


class CmccAuthConfigError(CmccAuthError):
    """Raised when local WebAPI auth configuration is incomplete."""


def _load_dotenv_file(path: Path, *, override: bool = False) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return str(value).strip()
    return default


def _load_project_env() -> None:
    _load_dotenv_file(BASE_DIR / ".env")
    extra_env = _env_first("CMCC_ENV_FILE", "CM_CLOUD_ENV_FILE")
    if extra_env:
        _load_dotenv_file(Path(extra_env), override=True)


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
    """Reads CMCC WebAPI Basic authorization from env/direct values."""

    AUTH_ERROR_CODES = {"04000005", "05050006", "HTTP_401", "HTTP_403", "401", "403"}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        _load_project_env()
        self.config = config if isinstance(config, dict) else {}
        configured_host = str(
            self.config.get("host")
            or _env_first(
                "CMCC_HOST",
                "CMCC_WEB_HOST",
                "CM_CLOUD_HOST",
                default=CMCC_DEFAULT_HOST,
            )
        ).rstrip("/")
        if "miniapp.yun.139.com" in configured_host:
            configured_host = CMCC_DEFAULT_HOST
        if configured_host.rstrip("/") == "https://personal-kd-njs.yun.139.com":
            configured_host = "https://personal-kd-njs.yun.139.com/hcy"
        self.host = configured_host
        self.phone = str(self.config.get("phone") or "").strip() or _env_first("CMCC_PHONE", "CM_CLOUD_PHONE")
        self._credentials: CmccAuthCredentials | None = None

    def invalidate(self) -> None:
        self._credentials = None

    def reload(self) -> CmccAuthCredentials:
        self.invalidate()
        return self.get_credentials()

    def get_headers(self) -> dict[str, str]:
        return self.get_credentials().headers()

    def get_credentials(self) -> CmccAuthCredentials:
        if self._credentials:
            return self._credentials

        raw_authorization = str(self.config.get("access_token") or self.config.get("authorization") or "").strip() or _env_first(
            "CMCC_ACCESS_TOKEN",
            "CMCC_AUTHORIZATION",
            "CMCC_BASIC_AUTH",
            "CM_CLOUD_ACCESS_TOKEN",
        )
        if not raw_authorization:
            raise CmccAuthConfigError("CMCC WebAPI 只接受 Authorization: Basic ...；请配置 CMCC_ACCESS_TOKEN=Basic ...")

        authorization = self._normalize_basic_authorization(raw_authorization)
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
    def _extract_phone_from_basic(authorization: str) -> str:
        token = re.sub(r"^Basic\s+", "", authorization or "", flags=re.IGNORECASE).strip()
        if not token:
            return ""
        try:
            padded = token + ("=" * (-len(token) % 4))
            decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        except Exception:
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
