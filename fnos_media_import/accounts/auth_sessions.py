"""Unified Provider login sessions.

Technical credentials stay inside encrypted session/account revisions. The admin
API only exposes the public authorization URL/code and coarse-grained status.
"""

from __future__ import annotations

import json
import base64
import io
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from urllib.parse import urlencode, urlparse

import requests

from ..time_utils import utc_now_iso
from .crypto import PURPOSE_AUTH_SESSION, decrypt, encrypt
from .service import ProviderAccountService


def _safe_provider_message(exc: BaseException) -> str:
    """Return a useful provider error without echoing credentials or URLs."""
    import re

    value = str(exc or "").strip() or "Provider authentication failed"
    # requests exceptions may include a complete URL containing redirect
    # tickets, tokens, or query parameters.  Keep only the endpoint path.
    value = re.sub(r"https?://[^\s]+", "provider endpoint", value, flags=re.IGNORECASE)
    value = re.sub(
        r"(?i)(password|passwd|token|accessToken|refreshToken|sessionKey|sessionSecret|session_key|session_secret|cookie|authorization|sson)\s*[:=]\s*[^,;\s]+",
        r"\1=[redacted]",
        value,
    )
    value = re.sub(
        r"(?i)\b(curIp|cookiesIp)\s*=\s*[^,;\s]+",
        r"\1=[redacted]",
        value,
    )
    return re.sub(r"[\r\n\t]+", " ", value)[:500]


class AuthSessionError(ValueError):
    code = "AUTH_SESSION_INVALID"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        stage: str = "",
        http_status: int | None = None,
        provider_kind: str = "",
        auth_method: str = "",
    ) -> None:
        super().__init__(message)
        if code:
            self.code = str(code)
        self.stage = str(stage or "")
        self.http_status = int(http_status) if http_status is not None else None
        self.provider_kind = str(provider_kind or "")
        self.auth_method = str(auth_method or "")


class AuthMethodUnavailable(AuthSessionError):
    code = "AUTH_METHOD_UNAVAILABLE"


class AuthSessionNotFound(AuthSessionError):
    code = "AUTH_SESSION_NOT_FOUND"


class _AuthSessionClaimLost(RuntimeError):
    """Internal signal used to roll back a stale persistence transaction."""


@dataclass(frozen=True)
class AuthStep:
    status: str
    message: str
    public_data: dict[str, Any] = field(default_factory=dict)
    private_state: dict[str, Any] = field(default_factory=dict)
    account_data: dict[str, Any] | None = None
    expires_at: str | None = None


class AuthHandler(Protocol):
    def start(self, payload: dict[str, Any]) -> AuthStep: ...
    def poll(self, state: dict[str, Any], payload: dict[str, Any]) -> AuthStep: ...
    def cancel(self, state: dict[str, Any]) -> None: ...


class ManagedAccountAuthHandler:
    """Imports a credential already managed by this application runtime."""

    def __init__(
        self,
        provider_kind: str,
        account_context: Callable[[], dict[str, Any]],
    ) -> None:
        self.provider_kind = provider_kind
        self.account_context = account_context

    def start(self, payload: dict[str, Any]) -> AuthStep:
        context = dict(self.account_context() or {})
        credentials = dict(context.get("credentials") or {})
        config = dict(context.get("config") or {})
        if not credentials:
            # Some built-in providers (currently Mobile) authenticate by
            # importing a browser-issued credential rather than redirecting to
            # an OAuth page.  Keep that input inside the same encrypted auth
            # session/account flow instead of a second "provider runtime"
            # settings store.
            supplied_config = dict(payload.get("config") or {})
            supplied_credentials = dict(payload.get("credentials") or {})
            for name in ("phone", "host"):
                if payload.get(name) not in (None, ""):
                    supplied_config[name] = str(payload.get(name)).strip()
            authorization = payload.get("authorization", payload.get("access_token"))
            if authorization not in (None, ""):
                supplied_credentials["authorization"] = str(authorization).strip()
            config.update(supplied_config)
            credentials.update(supplied_credentials)
        if not credentials:
            raise AuthMethodUnavailable(
                f"{self.provider_kind} 当前没有可导入的账号凭证"
            )
        return AuthStep(
            status="completed",
            message="账号凭证已验证并加密保存",
            account_data={
                "account_id": str(payload.get("account_id") or "") or None,
                "name": str(payload.get("name") or context.get("name") or "主账号"),
                "config": config,
                "credentials": credentials,
                "identity_hint": str(context.get("identity_hint") or ""),
            },
        )

    def poll(self, state: dict[str, Any], payload: dict[str, Any]) -> AuthStep:
        del state
        return self.start(payload)

    def cancel(self, state: dict[str, Any]) -> None:
        del state


class SixpanDeviceCodeAuthHandler:
    DEFAULT_EXPIRES_IN = 600
    CONFIG_FIELDS = ("host", "client_id", "client_secret", "oauth_client_id")
    PENDING_STATES = frozenset(
        {
            "AUTHORIZATION_PENDING",
            "AUTHORIZATION_PENDING_LOGIN",
            "AUTHORIZATION_PENDING_CONFIRMATION",
            "PENDING",
        }
    )
    FAILED_STATES = frozenset(
        {
            "AUTHORIZATION_DENIED",
            "AUTHORIZATION_EXPIRED",
            "AUTHORIZATION_FAILED",
            "ACCESS_DENIED",
            "CANCELLED",
            "DENIED",
            "EXPIRED",
            "FAILED",
            "REJECTED",
        }
    )

    def __init__(
        self,
        config_provider: Callable[[], dict[str, Any]],
        importer_factory: Callable[[dict[str, Any]], Any],
    ) -> None:
        self.config_provider = config_provider
        self.importer_factory = importer_factory

    def _config(self, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
        config = dict(self.config_provider() or {})
        for name in self.CONFIG_FIELDS:
            value = (supplied or {}).get(name)
            if value not in (None, ""):
                config[name] = str(value).strip()
        config.setdefault("host", "openapi.2dland.cn")
        if not str(config.get("client_id") or "").strip() or not str(
            config.get("client_secret") or ""
        ).strip():
            raise AuthMethodUnavailable(
                "请填写六盘 Client ID 与 Client Secret；Token 将由设备授权自动获取"
            )
        return config

    def start(self, payload: dict[str, Any]) -> AuthStep:
        config = self._config(payload)
        try:
            importer = self.importer_factory(config)
            result = importer.start_device_authorization(
                device=str(payload.get("device") or "fnos-media-import/admin"),
                scope=str(payload.get("scope") or ""),
            )
        except AuthSessionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AuthSessionError(
                _safe_provider_message(exc),
                code=str(getattr(exc, "code", "") or "SIXPAN_DEVICE_CODE_START_FAILED"),
                stage="device_code_start",
                http_status=self._http_status(exc),
            ) from exc
        if not isinstance(result, dict):
            raise AuthSessionError(
                "六盘授权接口返回格式无效",
                code="SIXPAN_DEVICE_CODE_RESPONSE_INVALID",
                stage="device_code_start",
            )
        device_code = str(result.get("device_code") or result.get("deviceCode") or "").strip()
        if not device_code:
            raise AuthSessionError(
                "六盘授权接口未返回 device_code",
                code="SIXPAN_DEVICE_CODE_RESPONSE_INVALID",
                stage="device_code_start",
            )
        verification_uri = str(
            result.get("verification_uri") or result.get("verificationUri") or ""
        ).strip()
        if verification_uri and urlparse(verification_uri).scheme not in {"http", "https"}:
            raise AuthSessionError(
                "六盘授权接口返回了不安全的授权地址",
                code="SIXPAN_DEVICE_CODE_RESPONSE_INVALID",
                stage="device_code_start",
            )
        try:
            expires_in = int(result.get("expires_in") or result.get("expiresIn") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in <= 0:
            expires_in = self.DEFAULT_EXPIRES_IN
        expires_at = (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(seconds=expires_in)
        ).isoformat().replace("+00:00", "Z")
        try:
            interval = max(2, int(result.get("interval") or 5))
        except (TypeError, ValueError):
            interval = 5
        public = {
            "user_code": str(result.get("user_code") or result.get("userCode") or ""),
            "verification_uri": verification_uri,
            "interval": interval,
            "expires_in": expires_in,
        }
        return AuthStep(
            status="waiting",
            message="请打开六盘授权页面并确认登录",
            public_data=public,
            private_state={
                "device_code": device_code,
                "user_code": public["user_code"],
                "account_id": str(payload.get("account_id") or ""),
                "name": str(payload.get("name") or "六盘主账号"),
                "expires_at": expires_at,
                # 应用凭据随授权会话加密保存，轮询不再依赖可变的全局配置。
                "auth_config": {
                    key: config[key]
                    for key in self.CONFIG_FIELDS
                    if str(config.get(key) or "").strip()
                },
            },
            expires_at=expires_at,
        )

    def poll(self, state: dict[str, Any], payload: dict[str, Any]) -> AuthStep:
        del payload
        expires_at = str(state.get("expires_at") or "")
        if self._expired(expires_at):
            return AuthStep(
                "failed",
                "六盘设备授权已过期，请重新发起授权",
                private_state=state,
                expires_at=expires_at or None,
            )
        try:
            config = self._config(dict(state.get("auth_config") or {}))
            importer = self.importer_factory(config)
            result = importer.check_device_authorization(
                device_code=str(state.get("device_code") or ""),
                user_code=str(state.get("user_code") or ""),
            )
            if not isinstance(result, dict):
                raise AuthSessionError(
                    "六盘授权状态返回格式无效",
                    code="SIXPAN_DEVICE_CODE_RESPONSE_INVALID",
                    stage="device_code_poll",
                )
            tokens = importer.extract_tokens(result)
            if not isinstance(tokens, dict):
                raise AuthSessionError(
                    "六盘授权 Token 返回格式无效",
                    code="SIXPAN_DEVICE_CODE_RESPONSE_INVALID",
                    stage="device_code_poll",
                )
        except AuthSessionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AuthSessionError(
                _safe_provider_message(exc),
                code=str(getattr(exc, "code", "") or "SIXPAN_DEVICE_CODE_POLL_FAILED"),
                stage="device_code_poll",
                http_status=self._http_status(exc),
            ) from exc
        if not (tokens.get("access_token") or tokens.get("refresh_token")):
            status = str(result.get("status") or result.get("state") or "").strip().upper()
            provider_message = str(
                result.get("message")
                or result.get("msg")
                or result.get("error_description")
                or result.get("error")
                or ""
            ).strip()
            provider_error = str(
                result.get("error_description") or result.get("error") or ""
            ).strip()
            login_value = result.get("login")
            login_text = str(login_value if login_value is not None else "").strip().lower()
            login_is_false = login_value is False or login_text in {
                "0",
                "false",
                "no",
            }
            login_is_true = login_value is True or login_text in {
                "1",
                "true",
                "yes",
            }
            if status in self.FAILED_STATES:
                return AuthStep(
                    status="failed",
                    message=provider_message or "六盘授权被拒绝或已失效，请重新发起授权",
                    private_state=state,
                    expires_at=expires_at or None,
                )
            if provider_error:
                return AuthStep(
                    status="failed",
                    message=provider_error,
                    private_state=state,
                    expires_at=expires_at or None,
                )
            if status in {"AUTHORIZATION_SUCCESS", "SUCCESS", "AUTHORIZED"} or login_is_true:
                return AuthStep(
                    status="failed",
                    message="六盘授权已完成，但接口未返回有效 Token，请重新发起授权",
                    private_state=state,
                    expires_at=expires_at or None,
                )
            if status not in self.PENDING_STATES and not login_is_false and provider_message:
                return AuthStep(
                    status="failed",
                    message=provider_message,
                    private_state=state,
                    expires_at=expires_at or None,
                )
            if status not in self.PENDING_STATES and not login_is_false and status:
                detail = provider_message or status or "未返回授权状态"
                return AuthStep(
                    status="failed",
                    message=f"六盘授权未返回有效 Token：{detail}",
                    private_state=state,
                    expires_at=expires_at or None,
                )
            return AuthStep(
                status="waiting",
                message={
                    "AUTHORIZATION_PENDING_LOGIN": "等待在六盘页面登录",
                    "AUTHORIZATION_PENDING_CONFIRMATION": "等待确认六盘授权",
                }.get(status, "六盘仍在等待授权"),
                private_state=state,
                expires_at=expires_at or None,
            )
        account_config = {
            key: config[key]
            for key in ("host", "client_id", "oauth_client_id")
            if str(config.get(key) or "").strip()
        }
        credentials = {
            "client_secret": str(config.get("client_secret") or ""),
            "access_token": str(tokens.get("access_token") or ""),
            "refresh_token": str(tokens.get("refresh_token") or ""),
        }
        return AuthStep(
            status="completed",
            message="六盘授权成功，Token 已自动加密保存",
            account_data={
                "account_id": str(state.get("account_id") or "") or None,
                "name": str(state.get("name") or "六盘主账号"),
                "config": account_config,
                "credentials": credentials,
            },
            expires_at=expires_at or None,
        )

    def cancel(self, state: dict[str, Any]) -> None:
        del state

    @staticmethod
    def _expired(value: str) -> bool:
        if not value:
            return False
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return True

    @staticmethod
    def _http_status(exc: BaseException) -> int | None:
        value = getattr(exc, "http_status", None)
        if value in (None, "", 0, "0"):
            value = getattr(exc, "status_code", None)
        try:
            status = int(value)
        except (TypeError, ValueError):
            return None
        return status if status > 0 else None


class TianyiPasswordAuthHandler:
    """Cloud189 username/password login with an encrypted captcha continuation."""

    SESSION_TTL_SECONDS = 5 * 60

    def __init__(self, importer_factory: Callable[[dict[str, Any]], Any]) -> None:
        self.importer_factory = importer_factory

    def start(self, payload: dict[str, Any]) -> AuthStep:
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not username or not password:
            raise AuthSessionError(
                "请输入天翼云账号和密码",
                code="LOGIN_INPUT_REQUIRED",
                stage="login_input",
            )
        config = {
            key: str(dict(payload.get("config") or {}).get(key) or "").strip()
            for key in ("base_url", "api_url", "auth_url")
            if str(dict(payload.get("config") or {}).get(key) or "").strip()
        }
        importer = self.importer_factory(
            {**config, "username": username, "password": password}
        )
        try:
            result = importer.begin_password_login(username, password)
        except Exception as exc:  # noqa: BLE001
            raise AuthSessionError(
                _safe_provider_message(exc),
                code=str(getattr(exc, "code", "") or "TIANYI_LOGIN_FAILED"),
                stage=str(getattr(exc, "stage", "") or "login_start"),
                http_status=getattr(exc, "http_status", None),
            ) from exc
        common = {
            "account_id": str(payload.get("account_id") or ""),
            "name": str(payload.get("name") or "天翼云主账号"),
            "username": username,
            "password": password,
            "config": config,
        }
        if str(result.get("status") or "") == "captcha_required":
            expires_at = self._expires_at()
            return self._captcha_waiting(
                {**common, "expires_at": expires_at},
                result,
                fallback_message="\u5929\u7ffc\u4e91\u8981\u6c42\u56fe\u7247\u9a8c\u8bc1\u7801\uff0c\u8bf7\u8bc6\u522b\u540e\u8f93\u5165",
            )
        return self._completed(common, result)

    def poll(self, state: dict[str, Any], payload: dict[str, Any]) -> AuthStep:
        if self._expired(str(state.get("expires_at") or "")):
            return AuthStep("failed", "天翼云验证码已过期，请重新登录", expires_at=state.get("expires_at"))
        importer = self.importer_factory(
            {
                **dict(state.get("config") or {}),
                "username": str(state.get("username") or ""),
                "password": str(state.get("password") or ""),
            }
        )
        if bool(payload.get("refresh_captcha")):
            try:
                result = importer.refresh_password_captcha(
                    dict(state.get("login_state") or {})
                )
            except Exception as exc:  # noqa: BLE001
                raise AuthSessionError(
                    _safe_provider_message(exc),
                    code=str(getattr(exc, "code", "") or "TIANYI_CAPTCHA_REFRESH_FAILED"),
                    stage=str(getattr(exc, "stage", "") or "captcha_image"),
                    http_status=getattr(exc, "http_status", None),
                ) from exc
            if str(result.get("status") or "") == "completed":
                return self._completed(state, result)
            return self._captcha_waiting(
                state,
                result,
                fallback_message="\u5df2\u66f4\u6362\u5929\u7ffc\u4e91\u56fe\u7247\u9a8c\u8bc1\u7801",
            )

        validate_code = str(payload.get("validate_code") or payload.get("captcha") or "").strip()
        if not validate_code:
            return self._captcha_waiting(
                state,
                {},
                fallback_message="\u8bf7\u8f93\u5165\u56fe\u7247\u4e2d\u7684\u9a8c\u8bc1\u7801",
            )
        try:
            result = importer.complete_password_login(
                dict(state.get("login_state") or {}),
                validate_code,
            )
        except Exception as exc:  # noqa: BLE001
            code = str(getattr(exc, "code", "") or "")
            if code in {"CAPTCHA_INVALID", "CAPTCHA_REQUIRED"}:
                try:
                    refreshed = importer.refresh_password_captcha(
                        dict(state.get("login_state") or {})
                    )
                except Exception:  # noqa: BLE001
                    refreshed = {}
                return self._captcha_waiting(
                    state,
                    refreshed,
                    fallback_message="\u9a8c\u8bc1\u7801\u9519\u8bef\uff0c\u8bf7\u8f93\u5165\u65b0\u56fe\u4e2d\u7684\u5b57\u7b26",
                    error=_safe_provider_message(exc),
                )
            raise AuthSessionError(
                _safe_provider_message(exc),
                code=str(getattr(exc, "code", "") or "TIANYI_LOGIN_FAILED"),
                stage=str(getattr(exc, "stage", "") or "login_submit"),
                http_status=getattr(exc, "http_status", None),
            ) from exc
        if str(result.get("status") or "") == "captcha_required":
            return self._captcha_waiting(
                state,
                result,
                fallback_message="\u9a8c\u8bc1\u7801\u9519\u8bef\uff0c\u8bf7\u8f93\u5165\u65b0\u56fe\u4e2d\u7684\u5b57\u7b26",
                error=str(result.get("error") or ""),
            )
        return self._completed(state, result)

    def cancel(self, state: dict[str, Any]) -> None:
        del state

    @staticmethod
    def _captcha_waiting(
        state: dict[str, Any],
        result: dict[str, Any],
        *,
        fallback_message: str,
        error: str = "",
    ) -> AuthStep:
        image = str(result.get("captcha_image") or state.get("captcha_image") or "")
        login_state = result.get("login_state")
        next_state = {
            **dict(state or {}),
            "login_state": (
                dict(login_state)
                if isinstance(login_state, dict)
                else dict(state.get("login_state") or {})
            ),
            "captcha_image": image,
        }
        public_data = {"challenge": "captcha", "captcha_image": image}
        public_error = str(error or result.get("error") or "").strip()
        if public_error:
            public_data["error"] = public_error
        return AuthStep(
            status="waiting",
            message=str(result.get("message") or fallback_message),
            public_data=public_data,
            private_state=next_state,
            expires_at=str(next_state.get("expires_at") or "") or None,
        )

    @staticmethod
    def _completed(state: dict[str, Any], result: dict[str, Any]) -> AuthStep:
        session = dict(result.get("session") or {})
        derived_credentials: dict[str, Any] = {}
        for source_key, target_key in (
            ("sessionKey", "session_key"),
            ("sessionSecret", "session_secret"),
            ("accessToken", "access_token"),
            ("refreshToken", "refresh_token"),
            ("expiresIn", "expires_in"),
        ):
            if session.get(source_key) not in (None, ""):
                derived_credentials[target_key] = session[source_key]
        credentials = {
            "password": str(state.get("password") or ""),
            **derived_credentials,
            **dict(result.get("credentials") or {}),
        }
        safe_config = {
            key: value
            for key, value in dict(state.get("config") or {}).items()
            if key not in {"password", "access_token", "refresh_token", "session_key", "session_secret"}
        }
        return AuthStep(
            status="completed",
            message="天翼云登录成功，Token 已加密保存并会自动刷新",
            account_data={
                "account_id": str(state.get("account_id") or "") or None,
                "name": str(state.get("name") or "天翼云主账号"),
                "config": {**safe_config, "username": str(state.get("username") or "")},
                "credentials": credentials,
                "identity_hint": str(result.get("identity_hint") or state.get("username") or ""),
            },
        )

    @classmethod
    def _expires_at(cls) -> str:
        return (
            datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=cls.SESSION_TTL_SECONDS)
        ).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _expired(value: str) -> bool:
        if not value:
            return False
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except ValueError:
            return True


class QuarkQrAuthHandler:
    """Perform Quark's first-party QR login and publish only verified cookies.

    The QR token and service ticket are kept in the encrypted auth-session state.
    The browser receives a short-lived QR image only; credentials are extracted
    from the server-side session after the ticket is exchanged at ``account/info``.
    """

    UOP_URL = "https://uop.quark.cn/cas/ajax"
    PAN_URL = "https://pan.quark.cn"
    CLIENT_ID = "532"
    API_VERSION = "1.2"
    QR_TTL_SECONDS = 300

    def __init__(
        self,
        account_tester: Callable[[str, dict[str, Any], dict[str, Any]], Any],
        *,
        timeout: int = 20,
        session_factory: Callable[[], requests.Session] | None = None,
    ) -> None:
        self.account_tester = account_tester
        self.timeout = max(5, min(int(timeout or 20), 120))
        self.session_factory = session_factory or requests.Session

    def start(self, payload: dict[str, Any]) -> AuthStep:
        session = self._session()
        request_id = str(uuid.uuid4())
        try:
            response = session.get(
                f"{self.UOP_URL}/getTokenForQrcodeLogin",
                params={"client_id": self.CLIENT_ID, "v": self.API_VERSION, "request_id": request_id},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AuthSessionError("\u5938\u514b\u626b\u7801\u670d\u52a1\u6682\u65f6\u4e0d\u53ef\u7528") from exc
        data = self._json(response)
        token = self._nested(data, "data", "members", "token")
        if not token or self._status(data) not in {2000000, 200, 0, "2000000", "200", "0"}:
            raise AuthSessionError("\u5938\u514b\u4e8c\u7ef4\u7801\u83b7\u53d6\u5931\u8d25")
        qr_url = self._qr_url(str(token))
        public = {"qr_url": qr_url, "qr_image": self._qr_image(qr_url), "interval": 2}
        expires_at = self._expires_at()
        return AuthStep(
            status="waiting",
            message="\u8bf7\u4f7f\u7528\u5938\u514b APP \u626b\u63cf\u4e8c\u7ef4\u7801",
            public_data=public,
            private_state={
                "qr_token": str(token),
                "account_id": str(payload.get("account_id") or ""),
                "name": str(payload.get("name") or "\u5938\u514b\u4e3b\u8d26\u53f7"),
                "config": dict(payload.get("config") or {}),
                "expires_at": expires_at,
            },
            expires_at=expires_at,
        )

    def poll(self, state: dict[str, Any], payload: dict[str, Any]) -> AuthStep:
        del payload
        if self._expired(str(state.get("expires_at") or "")):
            return AuthStep("failed", "\u4e8c\u7ef4\u7801\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55", expires_at=state.get("expires_at"))
        session = self._session()
        token = str(state.get("qr_token") or "").strip()
        try:
            response = session.get(
                f"{self.UOP_URL}/getServiceTicketByQrcodeToken",
                params={
                    "client_id": self.CLIENT_ID,
                    "v": self.API_VERSION,
                    "token": token,
                    "request_id": str(uuid.uuid4()),
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AuthSessionError("\u5938\u514b\u626b\u7801\u72b6\u6001\u67e5\u8be2\u6682\u65f6\u5931\u8d25") from exc
        data = self._json(response)
        ticket = self._nested(data, "data", "members", "service_ticket")
        if not ticket:
            status = self._status(data)
            if status in {50004002, "50004002"}:
                return AuthStep("failed", "\u4e8c\u7ef4\u7801\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55", private_state=state, expires_at=state.get("expires_at"))
            return AuthStep("waiting", "\u7b49\u5f85\u4f7f\u7528\u5938\u514b APP \u626b\u7801\u786e\u8ba4", private_state=state, expires_at=state.get("expires_at"))

        try:
            account_response = session.get(
                f"{self.PAN_URL}/account/info",
                params={"st": str(ticket), "lw": "scan"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AuthSessionError("\u5938\u514b\u767b\u5f55\u6001\u4ea4\u6362\u6682\u65f6\u5931\u8d25") from exc
        if account_response.status_code >= 400:
            return AuthStep("failed", "\u5938\u514b\u767b\u5f55\u4ea4\u6362 Cookie \u5931\u8d25", private_state=state)
        account_payload = self._json(account_response)
        account_info = (
            account_payload.get("data")
            if isinstance(account_payload.get("data"), dict)
            else account_payload
        )
        account_identity = str(
            account_info.get("nickname")
            or account_info.get("user_name")
            or account_info.get("email")
            or account_info.get("phone")
            or account_info.get("uid")
            or ""
        ).strip()
        self._enrich_cookie(session)
        cookie = self._cookie_header(session)
        if not cookie or "__pus=" not in cookie:
            return AuthStep("failed", "\u626b\u7801\u6210\u529f\uff0c\u4f46\u672a\u83b7\u53d6\u5230\u6709\u6548 Cookie", private_state=state)
        config = dict(state.get("config") or {})
        try:
            test = self.account_tester("quark", {"cookie": cookie}, config)
        except Exception as exc:  # noqa: BLE001
            return AuthStep("failed", f"\u5938\u514b Cookie \u68c0\u6d4b\u5f02\u5e38\uff1a{exc}", private_state=state)
        if not getattr(test, "ok", False):
            return AuthStep("failed", str(getattr(test, "message", "") or "\u5938\u514b Cookie \u9a8c\u8bc1\u5931\u8d25"), private_state=state)
        identity = str(getattr(test, "identity_hint", "") or account_identity)
        return AuthStep(
            status="completed",
            message="\u5938\u514b\u626b\u7801\u767b\u5f55\u6210\u529f\uff0cCookie \u5df2\u9a8c\u8bc1\u5e76\u52a0\u5bc6\u4fdd\u5b58",
            account_data={
                "account_id": str(state.get("account_id") or "") or None,
                "name": str(state.get("name") or "\u5938\u514b\u4e3b\u8d26\u53f7"),
                "config": config,
                "credentials": {"cookie": cookie},
                "identity_hint": identity,
            },
        )

    def cancel(self, state: dict[str, Any]) -> None:
        del state

    def _session(self) -> requests.Session:
        session = self.session_factory()
        session.trust_env = False
        session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        })
        return session

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise AuthSessionError("\u5938\u514b\u767b\u5f55\u63a5\u53e3\u8fd4\u56de\u4e86\u65e0\u6548\u6570\u636e") from exc
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _status(data: dict[str, Any]) -> Any:
        return data.get("status", data.get("code"))

    @staticmethod
    def _nested(data: dict[str, Any], *keys: str) -> Any:
        value: Any = data
        for key in keys:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    @classmethod
    def _qr_url(cls, token: str) -> str:
        params = {
            "token": token,
            "client_id": cls.CLIENT_ID,
            "ssb": "weblogin",
            "uc_param_str": "",
            "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0",
        }
        return "https://su.quark.cn/4_eMHBJ?" + urlencode(params)

    @staticmethod
    def _qr_image(value: str) -> str:
        try:
            import qrcode
            from qrcode.image.svg import SvgPathImage

            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
            qr.add_data(value)
            qr.make(fit=True)
            image = qr.make_image(image_factory=SvgPathImage)
            output = io.BytesIO()
            image.save(output)
            return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        except Exception:
            return ""

    @staticmethod
    def _cookie_header(session: requests.Session) -> str:
        values: dict[str, str] = {}
        for cookie in session.cookies:
            if not cookie.domain or "quark.cn" in cookie.domain:
                values[cookie.name] = cookie.value
        return "; ".join(f"{name}={value}" for name, value in values.items())

    def _enrich_cookie(self, session: requests.Session) -> None:
        """Best-effort refresh of ``__puus`` used by Quark file/download APIs."""
        if "__puus=" in self._cookie_header(session):
            return
        urls = (
            "https://pan.quark.cn/list",
            "https://drive-pc.quark.cn/1/clouddrive/file/sort?pr=ucpro&fr=pc&uc_param_str=&pdir_fid=0&_page=1&_size=50&_fetch_total=1&_sort=file_type:asc,updated_at:desc",
            "https://drive.quark.cn/1/clouddrive/member?pr=ucpro&fr=pc&uc_param_str=&fetch_subscribe=true",
        )
        for url in urls:
            if "__puus=" in self._cookie_header(session):
                break
            try:
                session.get(url, timeout=self.timeout)
            except requests.RequestException:
                continue

    @staticmethod
    def _expires_at() -> str:
        return (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=QuarkQrAuthHandler.QR_TTL_SECONDS)).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _expired(value: str) -> bool:
        if not value:
            return False
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except ValueError:
            return True


class ProviderAuthSessionService:
    TERMINAL = frozenset({"completed", "failed", "cancelled"})
    POLL_LEASE_SECONDS = 10 * 60
    PERSISTING = frozenset({"persisting", "persisting_active"})
    FROZEN_RESULT_FORMAT = "provider_auth_completion_v1"

    def __init__(
        self,
        database: Any,
        master_key_provider: Callable[[], bytes],
        accounts_service: ProviderAccountService,
        handlers: dict[tuple[str, str], AuthHandler],
    ) -> None:
        self.database = database
        self.master_key_provider = master_key_provider
        self.accounts = accounts_service
        self.handlers = dict(handlers)

    def start(
        self, provider_kind: str, auth_method: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        key = (str(provider_kind or "").strip(), str(auth_method or "").strip())
        handler = self.handlers.get(key)
        if handler is None:
            raise AuthMethodUnavailable(
                f"登录方式尚不可用：{key[0]}/{key[1]}",
                stage="dispatch",
                provider_kind=key[0],
                auth_method=key[1],
            )
        session_id = f"auth_{secrets.token_hex(12)}"
        try:
            step = handler.start(dict(payload or {}))
        except AuthSessionError as exc:
            exc.provider_kind = key[0]
            exc.auth_method = key[1]
            raise
        try:
            self._validate_step(step)
        except AuthSessionError as exc:
            exc.provider_kind = key[0]
            exc.auth_method = key[1]
            raise
        try:
            account_id = self._complete_account(key[0], step.account_data)
        except AuthSessionError as exc:
            exc.provider_kind = key[0]
            exc.auth_method = key[1]
            raise
        now = utc_now_iso()
        state_ciphertext, key_id = self._encrypt(step.private_state)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO provider_auth_sessions "
                "(session_id, provider_kind, auth_method, status, state_ciphertext, "
                "credential_key_id, public_json, message, account_id, expires_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    key[0],
                    key[1],
                    step.status,
                    state_ciphertext,
                    key_id,
                    json.dumps(step.public_data, ensure_ascii=False),
                    step.message,
                    account_id,
                    step.expires_at,
                    now,
                    now,
                ),
            )
        return self.get(session_id)

    def get(self, session_id: str) -> dict[str, Any]:
        row = self._expire_waiting(self._row(session_id))
        return self._public(row)

    def poll(self, session_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self._expire_waiting(self._row(session_id))
        status = str(row["status"])
        if status in self.TERMINAL:
            return self._public(row)
        if status in self.PERSISTING:
            return self._resume_persisting(row)
        claimed = self._claim_poll(row)
        if claimed is None:
            # Another request owns the polling lease.  ``polling`` is an
            # internal serialization state and remains ``waiting`` on the API.
            return self._public(self._row(session_id))
        row = claimed
        claim_version = int(row["row_version"] or 1)
        key = (str(row["provider_kind"]), str(row["auth_method"]))
        handler = self.handlers.get(key)
        if handler is None:
            self._release_poll_claim(session_id, claim_version)
            raise AuthMethodUnavailable(
                f"登录方式尚不可用：{key[0]}/{key[1]}",
                stage="dispatch",
                provider_kind=key[0],
                auth_method=key[1],
            )
        try:
            step = handler.poll(self._decrypt(row), dict(payload or {}))
        except AuthSessionError as exc:
            self._release_poll_claim(session_id, claim_version)
            exc.provider_kind = key[0]
            exc.auth_method = key[1]
            raise
        except Exception:
            self._release_poll_claim(session_id, claim_version)
            raise
        try:
            self._validate_step(step)
            persisted_status = step.status
            persisted_state = step.private_state
            if step.status == "completed":
                # Freeze the one-time Provider result before touching account
                # tables.  Recovery from this point only retries the account
                # probe/persistence and never calls the Provider again.
                persisted_status = "persisting"
                persisted_state = {
                    "format": self.FROZEN_RESULT_FORMAT,
                    "account_data": dict(step.account_data or {}),
                }
            state_ciphertext, key_id = self._encrypt(persisted_state)
            public_data = step.public_data or json.loads(row["public_json"] or "{}")
            public_json = json.dumps(public_data, ensure_ascii=False)
        except AuthSessionError as exc:
            self._release_poll_claim(session_id, claim_version)
            exc.provider_kind = key[0]
            exc.auth_method = key[1]
            raise
        except Exception:
            self._release_poll_claim(session_id, claim_version)
            raise
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE provider_auth_sessions SET status=?, state_ciphertext=?, "
                "credential_key_id=?, public_json=?, message=?, "
                "expires_at=?, updated_at=?, row_version=row_version+1 "
                "WHERE session_id=? AND status='polling' AND row_version=?",
                (
                    persisted_status,
                    state_ciphertext,
                    key_id,
                    public_json,
                    step.message,
                    step.expires_at,
                    utc_now_iso(),
                    session_id,
                    claim_version,
                ),
            )
        if updated.rowcount != 1:
            # A stale lease may have been recovered by another process.  Never
            # overwrite its newer result with this request's stale response.
            return self.get(session_id)
        if persisted_status == "persisting":
            return self._resume_persisting(self._row(session_id))
        return self.get(session_id)

    def _resume_persisting(self, row: Any) -> dict[str, Any]:
        """Persist a frozen Provider result without polling the Provider again."""

        claimed = self._claim_persist(row)
        if claimed is None:
            return self._public(self._row(str(row["session_id"])))

        session_id = str(claimed["session_id"])
        claim_version = int(claimed["row_version"] or 1)
        provider_kind = str(claimed["provider_kind"])
        auth_method = str(claimed["auth_method"])
        try:
            frozen = self._decrypt(claimed)
            if str(frozen.get("format") or "") != self.FROZEN_RESULT_FORMAT:
                raise AuthSessionError(
                    "登录结果恢复数据无效，请重新发起登录",
                    code="AUTH_RESULT_INVALID",
                    stage="account_persist",
                )
            account_data = frozen.get("account_data")
            if not isinstance(account_data, dict):
                raise AuthSessionError(
                    "登录结果未包含账号凭证，请重新发起登录",
                    code="AUTH_RESULT_INVALID",
                    stage="account_persist",
                )
            prepared, test = self.accounts.prepare_authenticated_account(
                provider_kind,
                account_data,
                cache_namespace=f"auth-session:{session_id}",
            )
            if not test.ok:
                raise AuthSessionError(
                    test.message or "账号登录成功，但账号检测未通过",
                    code="ACCOUNT_PROBE_FAILED",
                    stage="account_probe",
                )
            empty_state, empty_key_id = self._encrypt({})
        except AuthSessionError as exc:
            if not self._release_persist_claim(session_id, claim_version):
                return self.get(session_id)
            exc.provider_kind = provider_kind
            exc.auth_method = auth_method
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._release_persist_claim(session_id, claim_version):
                return self.get(session_id)
            raise AuthSessionError(
                _safe_provider_message(exc),
                code="ACCOUNT_PROBE_FAILED",
                stage="account_probe",
                provider_kind=provider_kind,
                auth_method=auth_method,
            ) from exc

        try:
            with self.database.connect() as connection:
                # Acquire the write guard before mutating account tables.  The
                # guard, account revision/selection, and terminal session state
                # commit together; losing the CAS rolls every account write
                # back instead of leaving an orphan or duplicate account.
                guarded = connection.execute(
                    "UPDATE provider_auth_sessions SET updated_at=?, "
                    "row_version=row_version+1 WHERE session_id=? "
                    "AND status='persisting_active' AND row_version=?",
                    (utc_now_iso(), session_id, claim_version),
                )
                if guarded.rowcount != 1:
                    raise _AuthSessionClaimLost(session_id)
                commit_version = claim_version + 1
                account = self.accounts.commit_authenticated_account(
                    prepared, conn=connection
                )
                completed = connection.execute(
                    "UPDATE provider_auth_sessions SET status='completed', "
                    "state_ciphertext=?, credential_key_id=?, account_id=?, "
                    "updated_at=?, row_version=row_version+1 WHERE session_id=? "
                    "AND status='persisting_active' AND row_version=?",
                    (
                        empty_state,
                        empty_key_id,
                        account.account_id,
                        utc_now_iso(),
                        session_id,
                        commit_version,
                    ),
                )
                if completed.rowcount != 1:
                    raise _AuthSessionClaimLost(session_id)
        except _AuthSessionClaimLost:
            return self.get(session_id)
        except AuthSessionError as exc:
            if not self._release_persist_claim(session_id, claim_version):
                return self.get(session_id)
            exc.provider_kind = provider_kind
            exc.auth_method = auth_method
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._release_persist_claim(session_id, claim_version):
                return self.get(session_id)
            raise AuthSessionError(
                _safe_provider_message(exc),
                code="ACCOUNT_PERSIST_FAILED",
                stage="account_persist",
                provider_kind=provider_kind,
                auth_method=auth_method,
            ) from exc
        return self.get(session_id)

    def cancel(self, session_id: str) -> dict[str, Any]:
        row = self._expire_waiting(self._row(session_id))
        status = str(row["status"])
        if status in self.TERMINAL:
            return self._public(row)
        if status in {"polling", "persisting_active"} and not self._lease_stale(row):
            # Do not race an active Provider exchange/account commit.  The
            # caller may retry cancellation after the bounded lease expires.
            return self._public(row)
        if status not in {"waiting", "polling", "persisting", "persisting_active"}:
            return self._public(row)

        state = self._decrypt(row) if status in {"waiting", "polling"} else {}
        empty_state, empty_key_id = self._encrypt({})
        row_version = int(row["row_version"] or 1)
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE provider_auth_sessions SET status='cancelled', message=?, "
                "state_ciphertext=?, credential_key_id=?, updated_at=?, "
                "row_version=row_version+1 WHERE session_id=? AND status=? AND row_version=?",
                (
                    "登录已取消",
                    empty_state,
                    empty_key_id,
                    utc_now_iso(),
                    session_id,
                    status,
                    row_version,
                ),
            )
        if updated.rowcount == 1 and status in {"waiting", "polling"}:
            handler = self.handlers.get((str(row["provider_kind"]), str(row["auth_method"])))
            if handler is not None:
                handler.cancel(state)
        return self.get(session_id)

    def _complete_account(
        self, provider_kind: str, account_data: dict[str, Any] | None
    ) -> str | None:
        if account_data is None:
            return None
        existing_id = str(account_data.get("account_id") or "").strip()
        if existing_id:
            existing = self.accounts.get_account(existing_id)
            if existing is None or existing.provider_kind != provider_kind:
                raise AuthSessionError("重新登录的账号不存在或 Provider 不匹配", stage="account_persist")
            try:
                account, test = self.accounts.revise_credentials_if_healthy(
                    existing_id,
                    config=dict(account_data.get("config") or {}),
                    credentials=dict(account_data.get("credentials") or {}),
                    identity_hint=str(account_data.get("identity_hint") or ""),
                    name=str(account_data.get("name") or ""),
                )
            except AuthSessionError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise AuthSessionError(
                    _safe_provider_message(exc),
                    code="ACCOUNT_PERSIST_FAILED",
                    stage="account_persist",
                ) from exc
            if not test.ok:
                raise AuthSessionError(
                    test.message or "账号登录成功，但账号检测未通过",
                    code="ACCOUNT_PROBE_FAILED",
                    stage="account_probe",
                )
        else:
            try:
                account = self.accounts.create_account(
                    provider_kind=provider_kind,
                    name=str(account_data.get("name") or "主账号"),
                    config=dict(account_data.get("config") or {}),
                    credentials=dict(account_data.get("credentials") or {}),
                    identity_hint=str(account_data.get("identity_hint") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                raise AuthSessionError(
                    _safe_provider_message(exc),
                    code="ACCOUNT_PERSIST_FAILED",
                    stage="account_persist",
                ) from exc
        if account is None:
            raise AuthSessionError("登录完成后无法读取账号", stage="account_persist")
        if not existing_id:
            try:
                test = self.accounts.test_account(account.account_id)
            except Exception as exc:  # noqa: BLE001
                self.accounts.delete_account(account.account_id)
                raise AuthSessionError(
                    _safe_provider_message(exc),
                    code="ACCOUNT_PROBE_FAILED",
                    stage="account_probe",
                ) from exc
            if not test.ok:
                self.accounts.delete_account(account.account_id)
                raise AuthSessionError(
                    test.message or "账号登录成功，但账号检测未通过",
                    code="ACCOUNT_PROBE_FAILED",
                    stage="account_probe",
                )
        current = self.accounts.get_selection(provider_kind)
        self.accounts.activate_account(
            "default",
            account.account_id,
            current.selection_revision if current else None,
        )
        return account.account_id

    @staticmethod
    def _validate_step(step: AuthStep) -> None:
        if step.status not in {"waiting", "completed", "failed", "cancelled"}:
            raise AuthSessionError(f"登录处理器返回了非法状态：{step.status}")
        if step.status == "completed" and step.account_data is None:
            raise AuthSessionError("登录处理器未返回账号凭证")

    def _row(self, session_id: str) -> Any:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_auth_sessions WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
        if row is None:
            raise AuthSessionNotFound(f"登录会话不存在：{session_id}")
        return row

    def _expire_waiting(self, row: Any) -> Any:
        if str(row["status"]) != "waiting":
            return row
        expires_at = str(row["expires_at"] or "").strip()
        if not expires_at:
            return row
        try:
            expired = datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            expired = True
        if not expired:
            return row
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE provider_auth_sessions SET status='failed', message=?, updated_at=?, "
                "row_version=row_version+1 WHERE session_id=? AND status='waiting' AND row_version=?",
                (
                    "登录会话已过期，请重新发起登录",
                    utc_now_iso(),
                    str(row["session_id"]),
                    int(row["row_version"] or 1),
                ),
            )
        return self._row(str(row["session_id"]))

    def _claim_poll(self, row: Any) -> Any | None:
        status = str(row["status"])
        if status == "polling":
            if self._lease_stale(row):
                self._terminalize_stale_poll(row)
            return None
        if status != "waiting":
            return None
        row_version = int(row["row_version"] or 1)
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE provider_auth_sessions SET status='polling', updated_at=?, "
                "row_version=row_version+1 WHERE session_id=? AND status=? AND row_version=?",
                (
                    utc_now_iso(),
                    str(row["session_id"]),
                    status,
                    row_version,
                ),
            )
        if updated.rowcount != 1:
            return None
        return self._row(str(row["session_id"]))

    def _claim_persist(self, row: Any) -> Any | None:
        status = str(row["status"])
        if status == "persisting_active" and not self._lease_stale(row):
            return None
        if status not in self.PERSISTING:
            return None
        row_version = int(row["row_version"] or 1)
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE provider_auth_sessions SET status='persisting_active', updated_at=?, "
                "row_version=row_version+1 WHERE session_id=? AND status=? AND row_version=?",
                (
                    utc_now_iso(),
                    str(row["session_id"]),
                    status,
                    row_version,
                ),
            )
        if updated.rowcount != 1:
            return None
        return self._row(str(row["session_id"]))

    def _release_poll_claim(self, session_id: str, row_version: int) -> bool:
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE provider_auth_sessions SET status='waiting', updated_at=?, "
                "row_version=row_version+1 WHERE session_id=? AND status='polling' AND row_version=?",
                (utc_now_iso(), str(session_id), int(row_version)),
            )
        return updated.rowcount == 1

    def _release_persist_claim(self, session_id: str, row_version: int) -> bool:
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE provider_auth_sessions SET status='persisting', updated_at=?, "
                "row_version=row_version+1 WHERE session_id=? "
                "AND status='persisting_active' AND row_version=?",
                (utc_now_iso(), str(session_id), int(row_version)),
            )
        return updated.rowcount == 1

    def _terminalize_stale_poll(self, row: Any) -> bool:
        """Fail closed instead of re-consuming a one-time Provider result."""

        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE provider_auth_sessions SET status='failed', message=?, updated_at=?, "
                "row_version=row_version+1 WHERE session_id=? AND status='polling' "
                "AND row_version=?",
                (
                    "授权状态恢复失败：上次验证结果不确定，为避免重复兑换凭证，请重新发起登录",
                    utc_now_iso(),
                    str(row["session_id"]),
                    int(row["row_version"] or 1),
                ),
            )
        return updated.rowcount == 1

    def _lease_stale(self, row: Any) -> bool:
        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"] or "").replace("Z", "+00:00"))
            return updated_at <= datetime.now(timezone.utc) - timedelta(
                seconds=self.POLL_LEASE_SECONDS
            )
        except (TypeError, ValueError):
            return True

    def _encrypt(self, state: dict[str, Any]) -> tuple[bytes, str]:
        return encrypt(
            self.master_key_provider(),
            json.dumps(state or {}, ensure_ascii=False, sort_keys=True),
            purpose=PURPOSE_AUTH_SESSION,
        )

    def _decrypt(self, row: Any) -> dict[str, Any]:
        raw = decrypt(
            self.master_key_provider(),
            str(row["credential_key_id"]),
            row["state_ciphertext"],
            purpose=PURPOSE_AUTH_SESSION,
        )
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        status = str(row["status"])
        return {
            "session_id": str(row["session_id"]),
            "provider_kind": str(row["provider_kind"]),
            "auth_method": str(row["auth_method"]),
            "status": (
                "waiting"
                if status in {"polling", "persisting", "persisting_active"}
                else status
            ),
            "message": str(row["message"] or ""),
            "public": json.loads(row["public_json"] or "{}"),
            "account_id": str(row["account_id"] or "") or None,
            "expires_at": row["expires_at"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }


__all__ = [
    "AuthMethodUnavailable",
    "AuthSessionError",
    "AuthSessionNotFound",
    "AuthStep",
    "ManagedAccountAuthHandler",
    "ProviderAuthSessionService",
    "QuarkQrAuthHandler",
    "SixpanDeviceCodeAuthHandler",
    "TianyiPasswordAuthHandler",
]
