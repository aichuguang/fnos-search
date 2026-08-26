from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from email.utils import formatdate
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote_plus, urlparse

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

from ..constants import JOB_DONE, JOB_FAILED, JOB_SUBMITTED
from ..http_client import HttpClient
from ..media_path_rules import should_force_resource_dir
from .base import AdapterCheckResult, ImportResult


logger = logging.getLogger("fnos_media_import.provider_auth")

TIANYI_SESSION_IP_MISMATCH_MESSAGE = (
    "天翼云拒绝了当前业务 Session。"
    "系统已按天翼云盘客户端协议刷新会话，但业务请求仍被拒绝。"
    "请在账号页重新登录。"
)


class _LimitedFileReader:
    """Expose exactly one file slice to ``requests`` without buffering it."""

    def __init__(self, handle: Any, length: int) -> None:
        self.handle = handle
        self.length = max(0, int(length))
        self.remaining = self.length

    def __len__(self) -> int:
        return self.length

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        wanted = self.remaining if size is None or int(size) < 0 else min(self.remaining, int(size))
        chunk = self.handle.read(wanted)
        self.remaining -= len(chunk)
        return chunk


class TianyiApiError(RuntimeError):
    """Raised when Cloud189 rejects an authenticated web API request."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        stage: str = "",
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "")
        self.stage = str(stage or "")
        self.http_status = int(http_status) if http_status is not None else None


class TianyiImporter:
    """Cloud189 share inspection and 189CloudPC transfer client.

    Account login and private-drive operations intentionally follow OpenList's
    ``drivers/189pc`` contract: TELEPC 6.2 session exchange plus
    ``sessionKey``/``sessionSecret`` HMAC-SHA1 request signing.  The legacy
    web-open contract is retained only for public-share metadata endpoints.
    """

    WEB_URL = "https://cloud.189.cn"
    API_URL = "https://api.cloud.189.cn"
    UPLOAD_URL = "https://upload.cloud.189.cn"
    AUTH_URL = "https://open.e.189.cn"
    APP_ID = "8025431004"
    ACCOUNT_TYPE = "02"
    CLIENT_TYPE = "10020"
    PC_CLIENT_TYPE = "TELEPC"
    PC_VERSION = "6.2"
    PC_CHANNEL_ID = "web_cloud.189.cn"
    RETURN_URL = "https://m.cloud.189.cn/zhuanti/2020/loginErrorPc/index.html"
    OPEN_APP_KEY = "600100422"
    # The SS-key -> access-token exchange is a separate Cloud189 contract
    # from the signed ``/api/open`` file APIs.  The first-party/reference
    # clients use the API host and this app key for that exchange.
    TOKEN_APP_KEY = "601102120"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
    )
    PAGE_SIZE = 1000
    _AUTH_CACHE_TTL = 6 * 60 * 60
    _AUTH_CACHE_MAX_ENTRIES = 256
    _ACCESS_TOKEN_TTL = 6 * 24 * 60 * 60
    _SESSION_KEEP_ALIVE_SECONDS = 5 * 60
    _auth_cache: dict[str, tuple[float, dict[str, Any]]] = {}
    _auth_cache_lock = threading.RLock()

    def __init__(
        self,
        config: dict[str, Any],
        *,
        token_update_callback: Callable[[dict[str, Any]], Any] | None = None,
        cache_namespace: str = "",
    ) -> None:
        values = dict(config or {})
        self.cache_namespace = str(
            cache_namespace or values.get("_cache_namespace") or ""
        ).strip()
        self.cookie = str(values.get("cookie") or values.get("cookies") or values.get("sson") or "").strip()
        self.username = str(values.get("username") or values.get("login_name") or "").strip()
        self.password = str(values.get("password") or "")
        self.access_token = str(values.get("access_token") or values.get("accessToken") or "").strip()
        self.refresh_token = str(values.get("refresh_token") or values.get("refreshToken") or "").strip()
        # A successful web login always yields a short-lived PC ``sessionKey``.
        # Keep it in the encrypted account revision so the post-login account
        # probe can use the already-authenticated session instead of submitting
        # the username/password a second time (which commonly triggers a new
        # captcha and made the login endpoint appear to fail with HTTP 400).
        self.session_key = str(values.get("session_key") or values.get("sessionKey") or "").strip()
        self.session_secret = str(values.get("session_secret") or values.get("sessionSecret") or "").strip()
        self.access_token_expires_at = self._as_timestamp(
            values.get("access_token_expires_at") or values.get("expires_at") or values.get("expiresIn")
        )
        configured_base = str(values.get("base_url") or self.WEB_URL).rstrip("/")
        self.web_url = configured_base.removesuffix("/api/open")
        self.api_url = str(values.get("api_url") or self.API_URL).rstrip("/")
        self.auth_url = str(values.get("auth_url") or self.AUTH_URL).rstrip("/")
        self.identity = str(values.get("identity") or "").strip()
        self.timeout = max(5, min(int(values.get("timeout") or 30), 120))
        self.http = HttpClient(timeout=self.timeout, verify_tls=True, trust_env=False)
        self.http.session.headers.update(
            {
                "Accept": "application/json;charset=UTF-8",
                "Referer": f"{self.web_url}/web/main/",
                "User-Agent": self.USER_AGENT,
            }
        )
        self._auth_data: dict[str, Any] = {
            key: value
            for key, value in {
                "sessionKey": self.session_key,
                "sessionSecret": self.session_secret,
            }.items()
            if value
        }
        self._force_refresh = False
        now = time.monotonic()
        # Auth-session persistence creates a fresh importer for the immediate
        # account probe.  A persisted PC session is already fresh and must be
        # used directly; treating a new importer as five minutes old caused a
        # second session exchange and unnecessarily escalated captcha risk.
        self._session_refreshed_at = now if self.session_key else 0.0
        self._last_keep_alive_at = now if self.session_key else 0.0
        self._token_update_callback = token_update_callback

    def check_configuration(self) -> AdapterCheckResult:
        if not (
            self.access_token
            or self.refresh_token
            or self.session_key
            or self._sson_cookie()
            or (self.username and self.password)
        ):
            return AdapterCheckResult(False, "unconfigured", "Cloud189 account credential is not configured")
        return AdapterCheckResult(True, "ready", "Cloud189 account credential is configured")

    def begin_password_login(self, username: str, password: str) -> dict[str, Any]:
        """Start Cloud189 password login and return a captcha challenge when required."""
        login_name = str(username or "").strip()
        secret = str(password or "")
        if not login_name or not secret:
            raise TianyiApiError("Cloud189 username and password are required", code="LOGIN_INPUT_REQUIRED")
        # Keep credentials on this short-lived importer so a login gateway that
        # changes its decision between needcaptcha and loginSubmit can refresh
        # the challenge without submitting a second blind password attempt.
        self.username = login_name
        self.password = secret
        state = self._password_login_state(login_name, secret)
        try:
            need_captcha = self.http.session.post(
                f"{self.auth_url}/api/logbox/oauth2/needcaptcha.do",
                headers={"REQID": state["req_id"]},
                data={
                    "appKey": state.get("app_key") or self.APP_ID,
                    "accountType": state.get("account_type") or self.ACCOUNT_TYPE,
                    "userName": state["rsa_username"],
                },
                timeout=self.timeout,
                verify=self.http.verify_tls,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._network_error("need_captcha", "Cloud189 captcha check request failed", exc) from exc
        self._raise_for_http(need_captcha, "Cloud189 captcha check", stage="need_captcha")
        captcha_required = self._captcha_required(need_captcha)
        logger.info(
            "event=tianyi_need_captcha stage=need_captcha required=%s raw=%s",
            captcha_required,
            str(getattr(need_captcha, "text", "") or "")[:300],
        )
        if captcha_required:
            return self._captcha_challenge(state)
        state["cookies"] = self._export_login_cookies()
        return self._submit_password_login(state, "")

    def complete_password_login(self, login_state: dict[str, Any], validate_code: str) -> dict[str, Any]:
        code = str(validate_code or "").strip()
        if not code:
            raise TianyiApiError("Cloud189 captcha is required", code="CAPTCHA_REQUIRED")
        state = dict(login_state or {})
        self._restore_login_cookies(state)
        return self._submit_password_login(state, code)

    def refresh_password_captcha(self, login_state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Refresh a password-login captcha without losing encrypted login state."""

        state = dict(login_state or {})
        if state:
            self._restore_login_cookies(state)
            try:
                return self._captcha_challenge(state)
            except TianyiApiError:
                # Expired gateway challenges require a complete bootstrap.  The
                # auth-session handler reconstructs this importer with the
                # encrypted username/password for exactly this continuation.
                pass
        if not self.username or not self.password:
            raise TianyiApiError(
                "Cloud189 captcha challenge expired; please restart login",
                code="CAPTCHA_EXPIRED",
                stage="captcha_image",
            )
        return self.begin_password_login(self.username, self.password)

    def _captcha_challenge(
        self,
        state: dict[str, Any],
        *,
        message: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        """Fetch and serialize a captcha while preserving the login CookieJar."""

        value = dict(state or {})
        captcha_token = str(value.get("captcha_token") or "").strip()
        req_id = str(value.get("req_id") or "").strip()
        login_ticket = str(value.get("lt") or "").strip()
        if not captcha_token or not req_id or not login_ticket:
            raise TianyiApiError(
                "Cloud189 captcha challenge is incomplete",
                code="LOGIN_STATE_INVALID",
                stage="captcha_image",
            )
        try:
            response = self.http.session.get(
                f"{self.auth_url}/api/logbox/oauth2/picCaptcha.do",
                params={"token": captcha_token},
                headers={
                    "REQID": req_id,
                    "lt": login_ticket,
                    "Referer": f"{self.auth_url}/api/logbox/oauth2/unifyAccountLogin.do",
                },
                timeout=self.timeout,
                verify=self.http.verify_tls,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._network_error(
                "captcha_image", "Cloud189 captcha image request failed", exc
            ) from exc
        self._raise_for_http(response, "Cloud189 captcha image", stage="captcha_image")
        content = bytes(getattr(response, "content", b"") or b"")
        if len(content) <= 20:
            raise TianyiApiError(
                "Cloud189 captcha image response is invalid",
                code="CAPTCHA_IMAGE_INVALID",
                stage="captcha_image",
                http_status=getattr(response, "status_code", None),
            )
        content_type = str(getattr(response, "headers", {}).get("Content-Type") or "image/png")
        mime_type = content_type.split(";", 1)[0].strip().lower()
        if not mime_type.startswith("image/"):
            mime_type = "image/png"
        value["cookies"] = self._export_login_cookies()
        return {
            "status": "captcha_required",
            "captcha_image": f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}",
            "login_state": value,
            "message": str(message or ""),
            "error": str(error or ""),
        }

    def _export_login_cookies(self) -> list[dict[str, Any]]:
        """Return a JSON-safe CookieJar snapshot for encrypted continuation state."""

        cookies: list[dict[str, Any]] = []
        for cookie in self.http.session.cookies:
            cookies.append(
                {
                    "name": str(cookie.name),
                    "value": str(cookie.value),
                    "domain": str(cookie.domain or ""),
                    "path": str(cookie.path or "/"),
                    "secure": bool(cookie.secure),
                    "expires": int(cookie.expires) if cookie.expires is not None else None,
                }
            )
        return cookies

    def _restore_login_cookies(self, state: dict[str, Any]) -> None:
        """Restore the provider login CookieJar after auth-session decryption."""

        raw_cookies = state.get("cookies") if isinstance(state, dict) else None
        if not isinstance(raw_cookies, list):
            return
        self.http.session.cookies.clear()
        for raw in raw_cookies:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            kwargs: dict[str, Any] = {
                "path": str(raw.get("path") or "/"),
                "secure": bool(raw.get("secure")),
            }
            domain = str(raw.get("domain") or "").strip()
            if domain:
                kwargs["domain"] = domain
            expires = raw.get("expires")
            if expires not in (None, ""):
                try:
                    kwargs["expires"] = int(expires)
                except (TypeError, ValueError):
                    pass
            self.http.session.cookies.set(name, str(raw.get("value") or ""), **kwargs)

    def probe(self) -> AdapterCheckResult:
        configured = self.check_configuration()
        if not configured.ok:
            return configured
        try:
            # Validate the same read-only API contract used by mounting and
            # transfers.  ``getUserSizeInfo.action`` is an optional portal
            # endpoint with an additional cookie/IP gate; OpenList does not
            # use it to decide whether a PC session is valid.  Treating that
            # endpoint as the login gate caused a valid login to be rolled
            # back with ACCOUNT_PROBE_FAILED even though file APIs worked.
            self._get(
                "/listFiles.action",
                folderId="-11",
                fileType=0,
                mediaAttr=0,
                orderBy="lastOpTime",
                descending=True,
                pageNum=1,
                pageSize=1,
                iconOption=5,
                recursive=0,
            )
            identity = str(self._auth_data.get("loginName") or self.identity or self.username or "")
            suffix = f": {identity}" if identity else ""
            return AdapterCheckResult(
                True,
                "ready",
                f"天翼云账号检测通过{suffix}",
                {"identity_hint": identity} if identity else {},
            )
        except Exception as exc:  # noqa: BLE001
            return AdapterCheckResult(
                False,
                "probe_failed",
                f"天翼云账号检测失败：{exc}",
            )

    def check_share(
        self,
        share_url: str,
        title: str = "",
        password: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        del title
        try:
            share_code = self._share_code(share_url)
            info = self._web_get("/api/open/share/getShareInfoByCodeV2.action", shareCode=share_code)
            share_mode = self._as_int(info.get("shareMode"), default=0)
            share_id = str(info.get("shareId") or "")
            if share_mode == 1:
                if not str(password or "").strip():
                    raise TianyiApiError("This Cloud189 share requires an access code", code="ACCESS_CODE_REQUIRED")
                checked = self._web_get(
                    "/api/open/share/checkAccessCode.action",
                    shareCode=share_code,
                    accessCode=str(password),
                    uuid=str(uuid.uuid4()),
                )
                share_id = str(checked.get("shareId") or share_id)
                if not share_id:
                    raise TianyiApiError("The Cloud189 access code is invalid", code="ACCESS_CODE_INVALID")
            if not share_id:
                raise TianyiApiError("Cloud189 share information did not include shareId", code="SHARE_INVALID")

            file_id = str(info.get("fileId") or "")
            if not file_id:
                raise TianyiApiError("Cloud189 share information did not include fileId", code="SHARE_INVALID")
            is_folder = self._as_bool(info.get("isFolder"), default=True)
            items = self._list_share_items(
                share_id=share_id,
                file_id=file_id,
                share_mode=share_mode,
                access_code=str(password or ""),
                is_folder=is_folder,
            )
            return True, {
                "success": True,
                "share_code": share_code,
                "share_id": share_id,
                "share_mode": share_mode,
                "root_file_id": file_id,
                "file_name": str(info.get("fileName") or info.get("name") or ""),
                "is_folder": is_folder,
                "items": items,
                "raw": info,
            }
        except Exception as exc:  # noqa: BLE001
            return False, {
                "success": False,
                "message": str(exc),
                "error_code": str(getattr(exc, "code", "") or ""),
            }

    def list_files(
        self,
        *,
        share_id: str,
        file_id: str,
        share_mode: int = 0,
        password: str = "",
    ) -> dict[str, Any]:
        try:
            return {
                "success": True,
                "items": self._list_share_items(
                    share_id=str(share_id),
                    file_id=str(file_id),
                    share_mode=int(share_mode),
                    access_code=str(password or ""),
                    is_folder=True,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": str(exc)}

    def import_resource(
        self,
        title: str,
        share_url: str,
        save_path: str,
        password: str = "",
        category_key: str = "",
        category: dict[str, Any] | None = None,
        native_selection: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> ImportResult:
        del category
        target_parent_id = str(save_path or "").strip()
        if not target_parent_id:
            return ImportResult(False, JOB_FAILED, "Cloud189 target folder_id cannot be empty")
        configured = self.check_configuration()
        if not configured.ok:
            return ImportResult(False, JOB_FAILED, configured.message, target_path=target_parent_id)
        try:
            ok, share = self.check_share(share_url, title, password)
            if not ok:
                raise TianyiApiError(
                    str(share.get("message") or "Cloud189 share check failed"),
                    code=str(share.get("error_code") or "SHARE_INVALID"),
                )
            selected = self._select_items(share.get("items"), native_selection)
            file_count = len(selected)
            create_resource_dir = bool(share.get("is_folder")) or should_force_resource_dir(category_key, file_count)
            target_folder_id = target_parent_id
            if create_resource_dir:
                folder_name = self._safe_folder_name(
                    str(title or share.get("file_name") or "Cloud189 resource")
                )
                target_folder_id = self._ensure_child_folder(target_parent_id, folder_name)

            if not selected:
                if bool(share.get("is_folder")):
                    return ImportResult(
                        True,
                        JOB_DONE,
                        "Cloud189 empty folder materialized",
                        external_task_id=f"folder:{target_folder_id}",
                        target_path=target_folder_id,
                        raw_data={
                            "provider": "tianyi",
                            "empty_share": True,
                            "target_parent_id": target_parent_id,
                            "target_folder_id": target_folder_id,
                        },
                    )
                raise TianyiApiError("Cloud189 share has no transferable items", code="SHARE_EMPTY")

            task_infos = [self._batch_task_info(item) for item in selected]
            form = {
                "type": "SHARE_SAVE",
                "taskInfos": json.dumps(task_infos, ensure_ascii=False, separators=(",", ":")),
                "targetFolderId": target_folder_id,
                "shareId": str(share["share_id"]),
            }
            result = self._post("/batch/createBatchTask.action", form=form)
            task_id = str(result.get("taskId") or "")
            if not task_id:
                raise TianyiApiError("Cloud189 batch save did not return taskId", code="TASK_ID_MISSING")
            return ImportResult(
                True,
                JOB_SUBMITTED,
                "Cloud189 transfer task submitted",
                external_task_id=task_id,
                target_path=target_folder_id,
                raw_data={
                    "provider": "tianyi",
                    "response": result,
                    "target_parent_id": target_parent_id,
                    "target_folder_id": target_folder_id,
                    "expected_names": [str(item.get("name") or "") for item in selected],
                    "expected_count": len(selected),
                    "share_id": str(share["share_id"]),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return ImportResult(
                False,
                JOB_FAILED,
                f"Cloud189 transfer failed: {exc}",
                target_path=target_parent_id,
                raw_data={"provider": "tianyi", "error_code": str(getattr(exc, "code", "") or "")},
            )

    def materialize_resource(
        self,
        *,
        title: str,
        share_url: str,
        root_locator: dict[str, Any],
        landing_path: str,
        password: str = "",
        selection: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> ImportResult:
        kind = str((root_locator or {}).get("kind") or "")
        root_folder_id = str((root_locator or {}).get("value") or "").strip()
        if kind != "folder_id" or not root_folder_id:
            return ImportResult(
                False,
                JOB_FAILED,
                "Cloud189 source mount requires a folder_id native root locator",
            )
        try:
            landing_folder_id = self._ensure_folder_path(root_folder_id, landing_path)
        except Exception as exc:  # noqa: BLE001
            return ImportResult(False, JOB_FAILED, f"Cloud189 landing folder creation failed: {exc}")
        result = self.import_resource(
            title=title,
            share_url=share_url,
            save_path=landing_folder_id,
            password=password,
            native_selection=selection,
        )
        if result.success:
            result.raw_data = {
                **dict(result.raw_data or {}),
                "landing_path": str(landing_path or ""),
                "landing_folder_id": landing_folder_id,
            }
        return result

    def poll_operation(self, receipt: Any) -> dict[str, Any]:
        task_id = self._receipt_task_id(receipt)
        if not task_id:
            return {"status": "unknown", "message": "Cloud189 task ID is missing"}
        try:
            value = self._check_batch_task(task_id)
            state = self._as_int(value.get("taskStatus"), default=-999)
            if state in {1, 3}:
                return {"status": "running", "message": "Cloud189 transfer is running", "metadata": value}
            if state == 2:
                self._resolve_conflicts(task_id)
                return {
                    "status": "running",
                    "message": "Cloud189 name conflicts were ignored; transfer resumed",
                    "metadata": value,
                }
            if state == 4:
                return {"status": "completed", "message": "Cloud189 transfer completed", "metadata": value}
            if state in {-1, 5, 6}:
                return {
                    "status": "failed",
                    "message": self._message(value, "Cloud189 transfer failed"),
                    "metadata": value,
                }
            return {
                "status": "unknown",
                "message": f"Cloud189 returned unknown task status: {value.get('taskStatus')}",
                "metadata": value,
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "unknown", "message": f"Cloud189 task check failed: {exc}"}

    def verify_native_result(self, request: Any) -> dict[str, Any]:
        from ..transfer.verifier import VerifyRequest

        if not isinstance(request, VerifyRequest):
            raise TypeError("verify_native_result requires VerifyRequest")
        receipt = request.operation
        raw = self._receipt_raw(receipt)
        if not raw.get("empty_share"):
            status = self.poll_operation(receipt)
            if status.get("status") == "failed":
                return {"status": "missing", "message": status.get("message") or "Cloud189 transfer failed"}
            if status.get("status") != "completed":
                return {"status": "unknown", "message": status.get("message") or "Cloud189 task is not complete"}

        target_folder_id = str(
            (getattr(receipt, "metadata", {}) or {}).get("target_path")
            or raw.get("target_folder_id")
            or ""
        )
        if not target_folder_id:
            return {"status": "unknown", "message": "Cloud189 target folder ID is missing"}
        try:
            target_items = self._list_drive_items(target_folder_id)
            expected_names = {str(name) for name in raw.get("expected_names") or [] if str(name)}
            actual_names = {str(item.get("name") or "") for item in target_items}
            missing = sorted(expected_names - actual_names)
            if missing:
                preview = ", ".join(missing[:3])
                return {"status": "missing", "message": f"Cloud189 target is missing saved items: {preview}"}
            return {"status": "ok", "message": "Cloud189 task and target folder verified"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "unknown", "message": f"Cloud189 target verification failed: {exc}"}

    def cancel_operation(self, receipt: Any) -> dict[str, Any]:
        del receipt
        return {
            "status": "not_cancellable",
            "message": "Cloud189 web API does not expose a reliable batch-task cancel operation",
        }

    def _check_batch_task(self, task_id: str) -> dict[str, Any]:
        return self._post(
            "/batch/checkBatchTask.action",
            form={"taskId": str(task_id), "type": "SHARE_SAVE"},
        )

    def _resolve_conflicts(self, task_id: str) -> None:
        conflict = self._post(
            "/batch/getConflictTaskInfo.action",
            form={"taskId": str(task_id), "type": "SHARE_SAVE"},
        )
        task_infos = conflict.get("taskInfos") if isinstance(conflict.get("taskInfos"), list) else []
        managed = [{**dict(item), "dealWay": 1} for item in task_infos if isinstance(item, dict)]
        self._post(
            "/batch/manageBatchTask.action",
            form={
                "taskId": str(task_id),
                "type": "SHARE_SAVE",
                "targetFolderId": str(conflict.get("targetFolderId") or ""),
                "taskInfos": json.dumps(managed, ensure_ascii=False, separators=(",", ":")),
            },
        )

    def _list_share_items(
        self,
        *,
        share_id: str,
        file_id: str,
        share_mode: int,
        access_code: str,
        is_folder: bool,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            value = self._web_get(
                "/api/open/share/listShareDir.action",
                shareId=str(share_id),
                isFolder=bool(is_folder),
                fileId=str(file_id),
                orderBy="lastOpTime",
                descending=True,
                shareMode=int(share_mode),
                pageNum=page,
                pageSize=self.PAGE_SIZE,
                accessCode=str(access_code or ""),
            )
            container = value.get("fileListAO") if isinstance(value.get("fileListAO"), dict) else {}
            rows = list(container.get("fileList") or []) + list(container.get("folderList") or [])
            normalized = [self._normalize_item(item) for item in rows if isinstance(item, dict)]
            items.extend(item for item in normalized if item.get("file_id"))
            if len(rows) < self.PAGE_SIZE:
                break
            page += 1
        return items

    def _list_drive_items(self, folder_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            value = self._get(
                "/listFiles.action",
                folderId=str(folder_id),
                fileType=0,
                mediaAttr=0,
                orderBy="lastOpTime",
                descending=True,
                pageNum=page,
                pageSize=self.PAGE_SIZE,
                iconOption=5,
                recursive=0,
            )
            container = value.get("fileListAO") if isinstance(value.get("fileListAO"), dict) else {}
            if not container:
                raise TianyiApiError(
                    "Cloud189 目录列表响应缺少 fileListAO",
                    code="INVALID_RESPONSE",
                    stage="provider_request",
                )
            rows = list(container.get("fileList") or []) + list(container.get("folderList") or [])
            items.extend(self._normalize_item(item) for item in rows if isinstance(item, dict))
            if len(rows) < self.PAGE_SIZE:
                break
            page += 1
        return items

    def _ensure_folder_path(self, root_folder_id: str, path: str) -> str:
        current = str(root_folder_id)
        for raw_name in str(path or "").replace("\\", "/").split("/"):
            name = self._safe_folder_name(raw_name)
            if name:
                current = self._ensure_child_folder(current, name)
        return current

    def _ensure_child_folder(self, parent_folder_id: str, folder_name: str) -> str:
        name = self._safe_folder_name(folder_name)
        if not name:
            raise TianyiApiError("Cloud189 folder name cannot be empty", code="FOLDER_NAME_INVALID")
        existing = self._find_folder(parent_folder_id, name)
        if existing:
            return existing
        try:
            created = self._post(
                "/createFolder.action",
                form={
                    "parentFolderId": str(parent_folder_id),
                    "folderName": name,
                    "relativePath": "",
                },
            )
            folder_id = str(created.get("id") or created.get("fileId") or created.get("folderId") or "")
            if folder_id:
                return folder_id
        except TianyiApiError as exc:
            if exc.code != "FileAlreadyExists":
                raise
        existing = self._find_folder(parent_folder_id, name)
        if not existing:
            raise TianyiApiError(f"Cloud189 folder creation returned no ID: {name}", code="FOLDER_CREATE_FAILED")
        return existing

    def _find_folder(self, parent_folder_id: str, name: str) -> str:
        for item in self._list_drive_items(parent_folder_id):
            if item.get("is_folder") and str(item.get("name") or "") == name:
                return str(item.get("file_id") or "")
        return ""

    def list_drive_folders(self, parent_folder_id: str = "-11") -> list[dict[str, Any]]:
        """List native Tianyi folders for the storage-location picker."""

        return [
            {
                "folder_id": str(item.get("file_id") or ""),
                "name": str(item.get("name") or ""),
                "parent_folder_id": str(parent_folder_id or "-11"),
            }
            for item in self._list_drive_items(str(parent_folder_id or "-11"))
            if bool(item.get("is_folder")) and str(item.get("file_id") or "")
        ]

    def resolve_folder_locator(
        self,
        locator_kind: str,
        locator_value: str,
        *,
        create: bool,
    ) -> str:
        """Resolve a frozen ``folder_id``/``folder_path`` locator."""

        kind = str(locator_kind or "folder_id").strip().lower()
        value = str(locator_value or "").strip()
        if kind == "folder_id":
            if not value:
                raise TianyiApiError(
                    "Cloud189 target folder_id cannot be empty",
                    code="TARGET_FOLDER_REQUIRED",
                )
            return value
        if kind != "folder_path":
            raise TianyiApiError(
                f"Unsupported Cloud189 target locator: {kind}",
                code="TARGET_LOCATOR_INVALID",
            )
        root_folder_id, separator, relative_path = value.partition("::")
        if not separator or not root_folder_id or not relative_path:
            raise TianyiApiError(
                "Cloud189 folder_path locator is invalid",
                code="TARGET_LOCATOR_INVALID",
            )
        if create:
            return self._ensure_folder_path(root_folder_id, relative_path)
        current = root_folder_id
        for raw_name in relative_path.replace("\\", "/").split("/"):
            name = self._safe_folder_name(raw_name)
            if not name:
                continue
            current = self._find_folder(current, name)
            if not current:
                return ""
        return current

    def upload_file(
        self,
        *,
        local_file: str,
        parent_folder_id: str,
        target_name: str = "",
        overwrite: bool = True,
        progress: Callable[[float], Any] | None = None,
    ) -> dict[str, Any]:
        """Upload one local file with the 189CloudPC multipart protocol."""

        path = os.path.realpath(str(local_file or ""))
        if not path or not os.path.isfile(path):
            raise TianyiApiError(
                f"Cloud189 upload file does not exist: {local_file}",
                code="LOCAL_FILE_MISSING",
            )
        folder_id = str(parent_folder_id or "").strip()
        if not folder_id:
            raise TianyiApiError(
                "Cloud189 upload parent folder_id is required",
                code="TARGET_FOLDER_REQUIRED",
            )
        file_name = str(target_name or os.path.basename(path)).strip()
        if not file_name or file_name in {".", ".."} or "/" in file_name or "\\" in file_name:
            raise TianyiApiError(
                "Cloud189 upload file name is invalid",
                code="FILE_NAME_INVALID",
            )
        file_size = int(os.path.getsize(path))
        if file_size <= 0:
            raise TianyiApiError(
                "Cloud189 189CloudPC upload does not support empty files",
                code="EMPTY_FILE_UNSUPPORTED",
            )
        slice_size = self._upload_part_size(file_size)
        file_md5, slice_md5, parts = self._upload_hashes(path, file_size, slice_size)
        params = {
            "parentFolderId": folder_id,
            "fileName": quote_plus(file_name, safe=""),
            "fileSize": str(file_size),
            "fileMd5": file_md5,
            "sliceSize": str(slice_size),
            "sliceMd5": slice_md5,
        }
        initialized = self._upload_request(
            "GET",
            "/person/initMultiUpload",
            params=params,
        )
        upload_data = self._upload_response_data(initialized)
        upload_file_id = str(
            upload_data.get("uploadFileId")
            or initialized.get("uploadFileId")
            or ""
        ).strip()
        if not upload_file_id:
            raise TianyiApiError(
                "Cloud189 initMultiUpload returned no uploadFileId",
                code="UPLOAD_SESSION_MISSING",
                stage="upload_init",
            )
        file_data_exists = self._as_int(
            upload_data.get("fileDataExists", initialized.get("fileDataExists")),
            default=0,
        )
        callback = progress if callable(progress) else None
        if file_data_exists != 1:
            for index, part in enumerate(parts, start=1):
                urls_response = self._upload_request(
                    "GET",
                    "/person/getMultiUploadUrls",
                    params={
                        "uploadFileId": upload_file_id,
                        "partInfo": part["part_info"],
                    },
                )
                upload_urls = urls_response.get("uploadUrls")
                if not isinstance(upload_urls, dict):
                    nested = self._upload_response_data(urls_response)
                    upload_urls = nested.get("uploadUrls") if isinstance(nested, dict) else None
                if not isinstance(upload_urls, dict) or not upload_urls:
                    raise TianyiApiError(
                        "Cloud189 getMultiUploadUrls returned no URL",
                        code="UPLOAD_URL_MISSING",
                        stage="upload_part_url",
                    )
                preferred = upload_urls.get(f"partNumber_{index}")
                raw_url = preferred if isinstance(preferred, dict) else next(
                    (item for item in upload_urls.values() if isinstance(item, dict)),
                    None,
                )
                if not isinstance(raw_url, dict):
                    raise TianyiApiError(
                        "Cloud189 upload URL payload is invalid",
                        code="UPLOAD_URL_INVALID",
                        stage="upload_part_url",
                    )
                request_url = str(raw_url.get("requestURL") or "").strip()
                if not request_url:
                    raise TianyiApiError(
                        "Cloud189 upload URL is empty",
                        code="UPLOAD_URL_INVALID",
                        stage="upload_part_url",
                    )
                self._put_upload_part(
                    request_url,
                    self._parse_upload_headers(raw_url.get("requestHeader")),
                    path,
                    int(part["offset"]),
                    int(part["size"]),
                )
                if callback is not None:
                    callback(index * 100.0 / (len(parts) + 1))

        committed = self._upload_request(
            "GET",
            "/person/commitMultiUploadFile",
            params={
                "uploadFileId": upload_file_id,
                "isLog": "0",
                "opertype": "3" if overwrite else "1",
            },
        )
        if callback is not None:
            callback(100.0)
        file_info = committed.get("file") if isinstance(committed.get("file"), dict) else {}
        return {
            "success": True,
            "file_id": str(file_info.get("userFileId") or file_info.get("fileId") or ""),
            "file_name": str(file_info.get("fileName") or file_name),
            "file_size": self._as_int(file_info.get("fileSize"), default=file_size),
            "file_md5": str(file_info.get("fileMd5") or file_md5),
            "upload_file_id": upload_file_id,
            "rapid_upload": file_data_exists == 1,
            "raw": committed,
        }

    def delete_drive_item(
        self,
        *,
        file_id: str,
        file_name: str,
        is_folder: bool = False,
    ) -> bool:
        """Delete one known item through the signed PC batch-task API."""

        item_id = str(file_id or "").strip()
        if not item_id:
            return False
        task_info = {
            "fileId": item_id,
            "fileName": str(file_name or ""),
            "isFolder": 1 if is_folder else 0,
        }
        try:
            created = self._post(
                "/batch/createBatchTask.action",
                form={
                    "type": "DELETE",
                    "taskInfos": json.dumps(
                        [task_info],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            )
        except TianyiApiError as exc:
            if exc.code in {"FileNotFound", "FileDoesNotExist", "NotFound"}:
                return True
            raise
        task_id = str(created.get("taskId") or "").strip()
        if not task_id:
            raise TianyiApiError(
                "Cloud189 delete task returned no taskId",
                code="TASK_ID_MISSING",
            )
        return self._wait_batch_task(task_id, "DELETE")

    def _wait_batch_task(
        self,
        task_id: str,
        task_type: str,
        *,
        attempts: int = 20,
        interval: float = 0.25,
    ) -> bool:
        for _attempt in range(max(1, int(attempts))):
            value = self._post(
                "/batch/checkBatchTask.action",
                form={"taskId": str(task_id), "type": str(task_type)},
            )
            state = self._as_int(value.get("taskStatus"), default=-999)
            if state == 4:
                return True
            if state in {-1, 5, 6}:
                raise TianyiApiError(
                    self._message(value, f"Cloud189 {task_type} task failed"),
                    code=self._response_code(value) or "BATCH_TASK_FAILED",
                )
            time.sleep(max(0.0, float(interval)))
        raise TianyiApiError(
            f"Cloud189 {task_type} task did not finish in time",
            code="BATCH_TASK_TIMEOUT",
        )

    @staticmethod
    def _upload_part_size(file_size: int) -> int:
        default = 10 * 1024 * 1024
        size = max(0, int(file_size))
        if size > default * 2 * 999:
            multiplier = max(math.ceil((size / 1999) / default), 5)
            return int(multiplier * default)
        if size > default * 999:
            return default * 2
        return default

    @staticmethod
    def _upload_hashes(
        local_file: str,
        file_size: int,
        slice_size: int,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        file_hash = hashlib.md5()  # noqa: S324 - Provider protocol requires MD5
        parts: list[dict[str, Any]] = []
        offset = 0
        with open(local_file, "rb") as handle:
            while offset < file_size:
                remaining = min(slice_size, file_size - offset)
                part_hash = hashlib.md5()  # noqa: S324 - Provider protocol requires MD5
                consumed = 0
                while consumed < remaining:
                    chunk = handle.read(min(1024 * 1024, remaining - consumed))
                    if not chunk:
                        raise TianyiApiError(
                            "Cloud189 upload source changed while hashing",
                            code="LOCAL_FILE_CHANGED",
                            stage="upload_hash",
                        )
                    file_hash.update(chunk)
                    part_hash.update(chunk)
                    consumed += len(chunk)
                digest = part_hash.digest()
                parts.append(
                    {
                        "offset": offset,
                        "size": remaining,
                        "md5": digest.hex().upper(),
                        "part_info": f"{len(parts) + 1}-{base64.b64encode(digest).decode('ascii')}",
                    }
                )
                offset += remaining
        if offset != file_size:
            raise TianyiApiError(
                "Cloud189 upload source size changed while hashing",
                code="LOCAL_FILE_CHANGED",
                stage="upload_hash",
            )
        file_md5 = file_hash.hexdigest().upper()
        if file_size <= slice_size:
            slice_md5 = file_md5
        else:
            joined = "\n".join(str(item["md5"]) for item in parts)
            slice_md5 = hashlib.md5(joined.encode("utf-8")).hexdigest().upper()  # noqa: S324
        return file_md5, slice_md5, parts

    @staticmethod
    def _aes_ecb_encrypt(value: str, key: str) -> str:
        key_bytes = str(key or "").encode("utf-8")[:16]
        if len(key_bytes) != 16:
            raise TianyiApiError(
                "Cloud189 sessionSecret is too short for upload parameter encryption",
                code="SESSION_CREDENTIALS_MISSING",
                stage="upload_request",
            )
        encrypted = AES.new(key_bytes, AES.MODE_ECB).encrypt(
            pad(str(value or "").encode("utf-8"), AES.block_size)
        )
        return encrypted.hex().upper()

    @staticmethod
    def _parse_upload_headers(value: Any) -> dict[str, str]:
        headers: dict[str, str] = {}
        for item in str(value or "").split("&"):
            key, separator, raw_value = item.partition("=")
            if separator and key:
                headers[str(key)] = str(raw_value)
        return headers

    @staticmethod
    def _upload_response_data(value: dict[str, Any]) -> dict[str, Any]:
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        return dict(data)

    def _upload_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any],
        retried: bool = False,
    ) -> dict[str, Any]:
        session = self._pc_session_data()
        session_key = str(session.get("sessionKey") or "").strip()
        session_secret = str(session.get("sessionSecret") or "").strip()
        if not session_key or not session_secret:
            raise TianyiApiError(
                "Cloud189 PC session is missing sessionKey/sessionSecret",
                code="SESSION_CREDENTIALS_MISSING",
                stage="upload_request",
            )
        normalized_path = "/" + str(path or "").lstrip("/")
        url = f"{self.UPLOAD_URL}{normalized_path}"
        encoded_params = "&".join(
            f"{key}={self._scalar(value)}"
            for key, value in sorted((params or {}).items())
            if value is not None
        )
        encrypted_params = self._aes_ecb_encrypt(encoded_params, session_secret)
        headers = self._pc_signature_headers(
            url,
            method,
            session_key=session_key,
            session_secret=session_secret,
            params_data=encrypted_params,
        )
        query = {**self._pc_client_suffix(), "params": encrypted_params}
        try:
            response = self.http.session.request(
                str(method or "GET").upper(),
                url,
                params=query,
                headers=headers,
                timeout=self.timeout,
                verify=self.http.verify_tls,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._network_error(
                "upload_request",
                "Cloud189 upload API request failed",
                exc,
            ) from exc
        value = self._response_payload(
            response,
            "Cloud189 upload API",
            stage="upload_request",
        )
        code = self._response_code(value)
        message = self._message(value, f"Cloud189 upload API error: {code}")
        invalid_session = code in {
            "InvalidSessionKey",
            "InvalidAccessToken",
            "UserInvalidOpenToken",
        } or "usersessionboisnull" in message.lower().replace(" ", "")
        ip_mismatch = self._is_session_ip_mismatch(message)
        if invalid_session or ip_mismatch:
            logger.warning(
                "event=tianyi_upload_session_rejected stage=upload_request path=%s code=%s "
                "has_access_token=%s has_refresh_token=%s has_session_key=%s retried=%s",
                normalized_path,
                code,
                bool(self.access_token),
                bool(self.refresh_token),
                bool(self._auth_data.get("sessionKey")),
                retried,
            )
            if not retried and self._rebind_session_for_ip_mismatch():
                return self._upload_request(
                    method,
                    path,
                    params=params,
                    retried=True,
                )
            if ip_mismatch:
                raise TianyiApiError(
                    TIANYI_SESSION_IP_MISMATCH_MESSAGE,
                    code="SESSION_IP_MISMATCH",
                    stage="upload_request",
                    http_status=response.status_code,
                )
            raise TianyiApiError(
                "Cloud189 PC session expired; please re-login",
                code=code or "AUTH_EXPIRED",
                stage="upload_request",
                http_status=response.status_code,
            )
        if response.status_code == 401 or (
            response.status_code == 403 and self._is_auth_rejection(code, message)
        ):
            raise TianyiApiError(
                "Cloud189 authentication expired",
                code=code or "AUTH_EXPIRED",
                stage="upload_request",
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise TianyiApiError(
                message,
                code=code or f"HTTP_{response.status_code}",
                stage="upload_request",
                http_status=response.status_code,
            )
        if code not in {"", "0", "success", "SUCCESS"}:
            raise TianyiApiError(
                message,
                code=code,
                stage="upload_request",
                http_status=response.status_code,
            )
        self._last_keep_alive_at = time.monotonic()
        return value

    def _put_upload_part(
        self,
        request_url: str,
        request_headers: dict[str, str],
        local_file: str,
        offset: int,
        size: int,
    ) -> None:
        last_error: BaseException | None = None
        for attempt in range(3):
            try:
                with open(local_file, "rb") as handle:
                    handle.seek(int(offset))
                    reader = _LimitedFileReader(handle, int(size))
                    headers = {str(key): str(value) for key, value in request_headers.items()}
                    headers.setdefault("Content-Length", str(int(size)))
                    response = self.http.session.put(
                        str(request_url),
                        params=self._pc_client_suffix(),
                        data=reader,
                        headers=headers,
                        timeout=max(self.timeout, 120),
                        verify=self.http.verify_tls,
                    )
                if int(response.status_code or 0) == 200:
                    return
                try:
                    value = self._response_payload(
                        response,
                        "Cloud189 upload part",
                        stage="upload_part",
                    )
                except TianyiApiError:
                    value = {}
                raise TianyiApiError(
                    self._message(
                        value,
                        f"Cloud189 upload part HTTP {response.status_code}",
                    ),
                    code=self._response_code(value) or f"HTTP_{response.status_code}",
                    stage="upload_part",
                    http_status=response.status_code,
                )
            except TianyiApiError as exc:
                last_error = exc
                if exc.http_status in {401, 403} or exc.code in {
                    "AUTH_EXPIRED",
                    "InvalidSessionKey",
                }:
                    raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
        if isinstance(last_error, TianyiApiError):
            raise last_error
        raise self._network_error(
            "upload_part",
            "Cloud189 upload part failed",
            last_error or RuntimeError("unknown upload error"),
        )

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(
        self,
        path: str,
        *,
        form: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", path, form=form, json_body=json_body)

    def _web_get(self, path: str, **params: Any) -> dict[str, Any]:
        return self._web_request("GET", path, params=params)

    def _pc_client_suffix(self) -> dict[str, str]:
        """Return the client query suffix used by OpenList 189CloudPC."""

        return {
            "clientType": self.PC_CLIENT_TYPE,
            "version": self.PC_VERSION,
            "channelId": self.PC_CHANNEL_ID,
            "rand": f"{secrets.randbelow(10_000)}_{secrets.randbelow(10_000_000)}",
        }

    @staticmethod
    def _pc_signature_headers(
        url: str,
        method: str,
        *,
        session_key: str,
        session_secret: str,
        params_data: str = "",
        date: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, str]:
        request_path = urlparse(str(url or "")).path or "/"
        operate = str(method or "GET").upper()
        http_date = str(date or formatdate(usegmt=True))
        source = (
            f"SessionKey={session_key}&Operate={operate}"
            f"&RequestURI={request_path}&Date={http_date}"
        )
        if params_data:
            source += f"&params={params_data}"
        signature = hmac.new(
            str(session_secret).encode("utf-8"),
            source.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest().upper()
        return {
            "Date": http_date,
            "SessionKey": str(session_key),
            "X-Request-ID": str(request_id or uuid.uuid4()),
            "Signature": signature,
        }

    def _pc_session_data(self) -> dict[str, Any]:
        value = self._normalize_session_data(self._auth_data)
        if not value.get("sessionKey") or not value.get("sessionSecret"):
            value = self._normalize_session_data(self._authenticate())
            self._auth_data = value
            self._publish_tokens(value)
        if not value.get("sessionKey") or not value.get("sessionSecret"):
            raise TianyiApiError(
                "Cloud189 PC session is missing sessionKey/sessionSecret",
                code="SESSION_CREDENTIALS_MISSING",
                stage="authenticate",
            )
        return value

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retried: bool = False,
        skip_keep_alive: bool = False,
    ) -> dict[str, Any]:
        """Call the same signed PC API contract used by OpenList 189CloudPC."""

        normalized_path = str(path or "")
        if (
            not skip_keep_alive
            and normalized_path.rstrip("/") != "/keepUserSession.action"
            and self._last_keep_alive_at
            and time.monotonic() - self._last_keep_alive_at >= self._SESSION_KEEP_ALIVE_SECONDS
        ):
            try:
                self._request(
                    "GET",
                    "/keepUserSession.action",
                    # The outer request owns the single refresh attempt.  This
                    # avoids exchanging the same access token twice when the
                    # keep-alive response itself reports InvalidSessionKey.
                    retried=True,
                    skip_keep_alive=True,
                )
            except Exception:  # noqa: BLE001
                if not self._rebind_session_for_ip_mismatch():
                    raise

        session = self._pc_session_data()
        session_key = str(session.get("sessionKey") or "").strip()
        session_secret = str(session.get("sessionSecret") or "").strip()
        if not session_key or not session_secret:
            raise TianyiApiError(
                "Cloud189 PC session is missing sessionKey/sessionSecret",
                code="SESSION_CREDENTIALS_MISSING",
                stage="provider_request",
            )
        url = normalized_path if normalized_path.startswith(("http://", "https://")) else f"{self.api_url}{normalized_path}"
        query = {**self._pc_client_suffix(), **self._query_values(params or {})}
        headers = self._pc_signature_headers(
            url,
            method,
            session_key=session_key,
            session_secret=session_secret,
        )
        try:
            response = self.http.session.request(
                method.upper(),
                url,
                params=query,
                data=form,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
                verify=self.http.verify_tls,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._network_error(
                "provider_request", "Cloud189 PC API request failed", exc
            ) from exc
        value = self._response_payload(response, "Cloud189 PC API", stage="provider_request")
        code = self._response_code(value)
        provider_message = self._message(value, f"Cloud189 API error: {code}")
        invalid_session = code in {"InvalidSessionKey", "InvalidAccessToken", "UserInvalidOpenToken"} or (
            "usersessionboisnull" in provider_message.lower().replace(" ", "")
        )
        ip_mismatch = self._is_session_ip_mismatch(provider_message)
        if invalid_session or ip_mismatch:
            logger.warning(
                "event=tianyi_pc_session_rejected stage=provider_request path=%s code=%s raw=%s "
                "has_access_token=%s has_refresh_token=%s has_session_key=%s retried=%s",
                path,
                code,
                provider_message[:300],
                bool(self.access_token),
                bool(self.refresh_token),
                bool(self._auth_data.get("sessionKey")),
                retried,
            )
            if not retried and self._rebind_session_for_ip_mismatch():
                return self._request(
                    method,
                    path,
                    params=params,
                    form=form,
                    json_body=json_body,
                    retried=True,
                    skip_keep_alive=True,
                )
            if ip_mismatch:
                raise TianyiApiError(
                    TIANYI_SESSION_IP_MISMATCH_MESSAGE,
                    code="SESSION_IP_MISMATCH",
                    stage="provider_request",
                    http_status=response.status_code,
                )
            raise TianyiApiError(
                "Cloud189 PC session expired; please re-login",
                code=code or "AUTH_EXPIRED",
                stage="provider_request",
                http_status=response.status_code,
            )
        if response.status_code == 401 or (
            response.status_code == 403 and self._is_auth_rejection(code, provider_message)
        ):
            raise TianyiApiError(
                "Cloud189 authentication expired",
                code=code or "AUTH_EXPIRED",
                stage="provider_request",
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise TianyiApiError(
                self._message(value, f"Cloud189 API HTTP {response.status_code}"),
                code=code or f"HTTP_{response.status_code}",
                stage="provider_request",
                http_status=response.status_code,
            )
        if not isinstance(value, dict):
            raise TianyiApiError(
                "Cloud189 API returned an invalid payload",
                code="INVALID_RESPONSE",
                stage="provider_request",
                http_status=response.status_code,
            )
        if code not in {"", "0", "success", "SUCCESS"}:
            raise TianyiApiError(
                provider_message,
                code=code,
                stage="provider_request",
                http_status=response.status_code,
            )
        self._last_keep_alive_at = time.monotonic()
        return value

    def _web_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retried: bool = False,
    ) -> dict[str, Any]:
        """Use the legacy web-open contract only for public-share inspection."""

        url = str(path) if str(path).startswith(("http://", "https://")) else f"{self.web_url}{path}"
        query = self._query_values(params or {})
        timestamp = str(int(time.time() * 1000))
        sign_values = self._query_values(json_body or {}) if method.upper() != "GET" else {}
        headers = {
            "Sign-Type": "1",
            "Signature": self._signature(
                {**sign_values, "Timestamp": timestamp, "AppKey": self.OPEN_APP_KEY}
            ),
            "Timestamp": timestamp,
            "AppKey": self.OPEN_APP_KEY,
        }
        query["sessionKey"] = self._session_key()
        response = self.http.session.request(
            method.upper(),
            url,
            params=query,
            data=form,
            json=json_body,
            headers=headers,
            timeout=self.timeout,
            verify=self.http.verify_tls,
        )
        value = self._response_payload(response, "Cloud189 web API", stage="provider_request")
        code = self._response_code(value)
        provider_message = self._message(value, f"Cloud189 web API error: {code}")
        if self._is_session_ip_mismatch(provider_message):
            logger.warning(
                "event=tianyi_session_ip_mismatch stage=provider_request path=%s code=%s raw=%s "
                "has_access_token=%s has_refresh_token=%s has_session_key=%s retried=%s",
                path,
                code,
                provider_message[:300],
                bool(self.access_token),
                bool(self.refresh_token),
                bool(self._auth_data.get("sessionKey")),
                retried,
            )
            if not retried and self._rebind_session_for_ip_mismatch():
                return self._web_request(
                    method,
                    path,
                    params=params,
                    form=form,
                    json_body=json_body,
                    retried=True,
                )
            raise TianyiApiError(
                TIANYI_SESSION_IP_MISMATCH_MESSAGE,
                code="SESSION_IP_MISMATCH",
                stage="provider_request",
                http_status=response.status_code,
            )
        if code in {"InvalidSessionKey", "InvalidAccessToken"} and not retried:
            if self._rebind_session_for_ip_mismatch():
                return self._web_request(
                    method,
                    path,
                    params=params,
                    form=form,
                    json_body=json_body,
                    retried=True,
                )
        if response.status_code == 401 or (
            response.status_code == 403 and self._is_auth_rejection(code, provider_message)
        ):
            raise TianyiApiError(
                "Cloud189 authentication expired",
                code=code or "AUTH_EXPIRED",
                stage="provider_request",
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise TianyiApiError(
                provider_message,
                code=code or f"HTTP_{response.status_code}",
                stage="provider_request",
                http_status=response.status_code,
            )
        if code not in {"", "0", "success", "SUCCESS"}:
            raise TianyiApiError(
                provider_message,
                code=code,
                stage="provider_request",
                http_status=response.status_code,
            )
        return value

    def _rebind_session_for_ip_mismatch(self) -> bool:
        """Exchange a stored token for one fresh PC session, at most once."""

        previous_cache_key = self._auth_cache_key()
        failures: list[BaseException] = []
        candidates: list[str] = []
        if self.access_token:
            candidates.append(self.access_token)
        for access_token in candidates:
            try:
                session = self._login_by_access_token(access_token)
                return self._install_rebound_session(previous_cache_key, session)
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)
        if self.refresh_token:
            try:
                refreshed = self._refresh_access_token(self.refresh_token)
                self._publish_tokens(refreshed)
                access_token = str(
                    refreshed.get("accessToken") or refreshed.get("access_token") or ""
                ).strip()
                if access_token:
                    session = self._login_by_access_token(access_token)
                    return self._install_rebound_session(previous_cache_key, session)
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)
        # Never submit the password silently from a business request.  A blind
        # re-login both loses captcha continuation state and is the main reason
        # two ordinary requests can suddenly escalate into a captcha challenge.
        if failures:
            last = failures[-1]
            logger.warning(
                "event=tianyi_session_ip_rebind_failed stage=provider_request code=%s error_type=%s",
                str(getattr(last, "code", "") or "SESSION_REBIND_FAILED"),
                type(last).__name__,
            )
        return False

    def _install_rebound_session(
        self,
        previous_cache_key: str,
        session: dict[str, Any],
    ) -> bool:
        value = self._normalize_session_data(session)
        session_key = str(value.get("sessionKey") or "").strip()
        session_secret = str(value.get("sessionSecret") or "").strip()
        if not session_key or not session_secret:
            return False
        with self._auth_cache_lock:
            self._auth_cache.pop(previous_cache_key, None)
        self._auth_data = value
        self._force_refresh = False
        self._publish_tokens(value)
        self._cache_auth(self._auth_cache_key(), value)
        now = time.monotonic()
        self._session_refreshed_at = now
        self._last_keep_alive_at = now
        return True

    @staticmethod
    def _is_session_ip_mismatch(message: Any) -> bool:
        normalized = str(message or "").strip().lower().replace(" ", "")
        return "checkiperror" in normalized or (
            "curip=" in normalized and "cookiesip=" in normalized
        )

    @staticmethod
    def _is_auth_rejection(code: Any, message: Any) -> bool:
        """Return True when a 403/400 body is a session/auth rejection.

        Cloud189 returns HTTP 403 for both authentication failures and
        business rejections (e.g. ``InsufficientStorageSpace`` when the
        account is out of quota).  Only the former must be surfaced as
        "authentication expired"; business codes keep their real message.
        """
        code_text = str(code or "").strip()
        normalized = str(message or "").strip().lower().replace(" ", "")
        auth_codes = {
            "invalid_session_key",
            "invalidaccesstoken",
            "userinvalidopentoken",
            "accountcheckfailed",
            "auth_expired",
            "session_expired",
        }
        return (
            code_text.lower() in auth_codes
            or "usersessionboisnull" in normalized
            or TianyiImporter._is_session_ip_mismatch(message)
        )

    def _session_key(self) -> str:
        if not self._auth_data.get("sessionKey"):
            self._auth_data = self._normalize_session_data(self._authenticate())
            self._publish_tokens(self._auth_data)
        session_key = str(self._auth_data.get("sessionKey") or "")
        if not session_key:
            raise TianyiApiError("Cloud189 login returned no sessionKey", code="SESSION_KEY_MISSING")
        return session_key

    def _authenticate(self) -> dict[str, Any]:
        configured = self.check_configuration()
        if not configured.ok:
            raise TianyiApiError(configured.message, code="UNCONFIGURED")
        cache_key = self._auth_cache_key()
        now = time.monotonic()
        with self._auth_cache_lock:
            cached = self._auth_cache.get(cache_key)
            if cached and cached[0] > now and not self._force_refresh:
                return dict(cached[1])
            if cached:
                self._auth_cache.pop(cache_key, None)

        failures: list[TianyiApiError] = []
        if self.access_token and not self._force_refresh and self._access_token_is_fresh():
            try:
                value = self._login_by_access_token(self.access_token)
                return self._cache_auth(cache_key, value)
            except Exception as exc:  # noqa: BLE001
                failures.append(self._as_api_error("access_token", "Cloud189 access-token login failed", exc))

        if self.refresh_token:
            try:
                refreshed = self._refresh_access_token(self.refresh_token)
                self._publish_tokens(refreshed)
                value = self._login_by_access_token(
                    str(refreshed.get("accessToken") or refreshed.get("access_token") or "")
                )
                self._force_refresh = False
                return self._cache_auth(self._auth_cache_key(), value)
            except Exception as exc:  # noqa: BLE001
                failures.append(self._as_api_error("token_refresh", "Cloud189 token refresh login failed", exc))

        sson = self._sson_cookie()
        if sson:
            try:
                value = self._login_by_sson(sson)
                self._publish_tokens(value)
                self._force_refresh = False
                return self._cache_auth(self._auth_cache_key(), value)
            except Exception as exc:  # noqa: BLE001
                failures.append(self._as_api_error("sson_login", "Cloud189 SSON login failed", exc))

        if self.username and self.password:
            try:
                result = self.begin_password_login(self.username, self.password)
                if result.get("status") == "captcha_required":
                    raise TianyiApiError(
                        "Cloud189 requires a captcha; please re-login from the admin account page",
                        code="CAPTCHA_REQUIRED",
                    )
                value = dict(result.get("session") or {})
                self._publish_tokens(value)
                self._force_refresh = False
                return self._cache_auth(self._auth_cache_key(), value)
            except TianyiApiError as exc:
                if exc.code == "CAPTCHA_REQUIRED":
                    raise
                failures.append(exc)
            except Exception as exc:  # noqa: BLE001
                failures.append(self._as_api_error("password_login", "Cloud189 password login failed", exc))

        last = failures[-1] if failures else None
        if last is None:
            raise TianyiApiError("Cloud189 authentication failed: no usable credential", code="LOGIN_FAILED", stage="authenticate")
        raise TianyiApiError(
            f"Cloud189 authentication failed: {last}",
            code=last.code or "LOGIN_FAILED",
            stage=last.stage or "authenticate",
            http_status=last.http_status,
        )

    def _login_by_sson(self, sson: str) -> dict[str, Any]:
        login_params = {
            "appId": self.APP_ID,
            "clientType": self.CLIENT_TYPE,
            "returnURL": self.RETURN_URL,
            "timeStamp": str(int(time.time() * 1000)),
        }
        landing = self._get_login_form_response(login_params)
        redirected = self.http.session.get(
            landing.url,
            headers={"Cookie": f"SSON={sson}"},
            timeout=self.timeout,
            verify=self.http.verify_tls,
            allow_redirects=True,
        )
        redirected.raise_for_status()
        return self._get_pc_session(redirect_url=redirected.url)

    def _login_by_access_token(self, access_token: str) -> dict[str, Any]:
        if not str(access_token or "").strip():
            raise TianyiApiError("Cloud189 access token is empty", code="ACCESS_TOKEN_MISSING")
        return self._get_pc_session(access_token=str(access_token).strip())

    def _get_pc_session(self, *, redirect_url: str = "", access_token: str = "") -> dict[str, Any]:
        """Exchange login redirect/access token using only TELEPC 6.2."""

        redirect = str(redirect_url or "").strip()
        bearer = str(access_token or "").strip()
        if not redirect and not bearer:
            raise TianyiApiError(
                "Cloud189 PC session exchange input is empty",
                code="SESSION_EXCHANGE_INPUT_REQUIRED",
                stage="session_exchange",
            )
        params = {
            "appId": self.APP_ID,
            **self._pc_client_suffix(),
            **({"accessToken": bearer} if bearer else {"redirectURL": redirect}),
        }
        headers = {"X-Request-ID": str(uuid.uuid4())}
        try:
            if bearer:
                response = self.http.session.get(
                    f"{self.api_url}/getSessionForPC.action",
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                    verify=self.http.verify_tls,
                )
            else:
                response = self.http.session.post(
                    f"{self.api_url}/getSessionForPC.action",
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                    verify=self.http.verify_tls,
                )
        except Exception as exc:  # noqa: BLE001
            raise self._network_error(
                "session_exchange", "Cloud189 PC session request failed", exc
            ) from exc
        value = self._normalize_session_data(
            self._response_payload(response, "Cloud189 PC session", stage="session_exchange")
        )
        code = self._response_code(value)
        if response.status_code >= 400 or code not in {"", "0", "success", "SUCCESS"}:
            raise TianyiApiError(
                self._message(value, "Cloud189 PC session exchange failed"),
                code=code or f"HTTP_{response.status_code}",
                stage="session_exchange",
                http_status=response.status_code,
            )
        if not value.get("sessionKey") or not value.get("sessionSecret"):
            raise TianyiApiError(
                self._message(value, "Cloud189 login returned an incomplete PC session"),
                code="SESSION_CREDENTIALS_MISSING",
                stage="session_exchange",
                http_status=response.status_code,
            )
        if bearer and not value.get("accessToken"):
            value["accessToken"] = bearer
        self._auth_data = dict(value)
        if not value.get("accessToken"):
            try:
                access = self._get_access_token_by_ss_key(str(value["sessionKey"]))
                if access:
                    value = self._normalize_session_data({**value, **access})
            except TianyiApiError:
                logger.info("event=tianyi_access_token_enrichment_failed stage=session_exchange")
        self._publish_tokens(value)
        now = time.monotonic()
        self._session_refreshed_at = now
        self._last_keep_alive_at = now
        return value

    def _get_access_token_by_ss_key(self, session_key: str) -> dict[str, Any]:
        """Enrich a PC session with the signed web access token when needed."""
        key = str(session_key or "").strip()
        if not key:
            return {}
        timestamp = str(int(time.time() * 1000))
        params = {"sessionKey": key}
        headers = {
            "Sign-Type": "1",
            "Signature": self._signature(
                {
                    "Timestamp": timestamp,
                    "AppKey": self.TOKEN_APP_KEY,
                    "sessionKey": key,
                }
            ),
            "Timestamp": timestamp,
            "AppKey": self.TOKEN_APP_KEY,
        }
        try:
            response = self.http.session.get(
                f"{self.api_url}/open/oauth2/getAccessTokenBySsKey.action",
                params=params,
                headers=headers,
                timeout=self.timeout,
                verify=self.http.verify_tls,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._network_error("session_exchange", "Cloud189 access-token exchange request failed", exc) from exc
        self._raise_for_http(response, "Cloud189 access-token exchange", stage="session_exchange")
        value = self._json_response(response, "Cloud189 access-token exchange", stage="session_exchange")
        code = self._response_code(value)
        if code not in {"", "0", "success", "SUCCESS"}:
            raise TianyiApiError(
                self._message(value, "Cloud189 access-token exchange failed"),
                code=code,
                stage="session_exchange",
                http_status=response.status_code,
            )
        access_token = str(value.get("accessToken") or value.get("access_token") or "").strip()
        if not access_token:
            raise TianyiApiError(
                "Cloud189 access-token exchange returned no token",
                code="ACCESS_TOKEN_MISSING",
                stage="session_exchange",
            )
        return {
            "accessToken": access_token,
            **(
                {"refreshToken": str(value.get("refreshToken") or "").strip()}
                if value.get("refreshToken")
                else {}
            ),
        }

    def _refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        try:
            response = self.http.session.post(
                f"{self.auth_url}/api/oauth2/refreshToken.do",
                data={
                    "clientId": self.APP_ID,
                    "refreshToken": str(refresh_token),
                    "grantType": "refresh_token",
                    "format": "json",
                },
                timeout=self.timeout,
                verify=self.http.verify_tls,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._network_error("token_refresh", "Cloud189 token refresh request failed", exc) from exc
        self._raise_for_http(response, "Cloud189 token refresh", stage="token_refresh")
        value = self._json_response(response, "Cloud189 token refresh", stage="token_refresh")
        if not value.get("accessToken"):
            raise TianyiApiError(
                self._message(value, "Cloud189 token refresh failed"),
                code=self._response_code(value) or "REFRESH_FAILED",
                stage="token_refresh",
                http_status=response.status_code,
            )
        return value

    def _get_login_form_response(self, params: dict[str, Any]) -> Any:
        """Load the exact PC login bootstrap used by OpenList 189CloudPC."""

        self.http.session.cookies.clear()
        try:
            response = self.http.session.get(
                f"{self.web_url}/api/portal/unifyLoginForPC.action",
                params=dict(params or {}),
                timeout=self.timeout,
                verify=self.http.verify_tls,
                allow_redirects=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._network_error(
                "login_form", "Cloud189 login page request failed", exc
            ) from exc
        self._raise_for_http(response, "Cloud189 login page", stage="login_form")
        html = str(response.text or "")
        if not all(marker in html for marker in ("captchaToken", "paramId", "reqId", "lt")):
            raise TianyiApiError(
                "Cloud189 login page returned an incomplete challenge",
                code="LOGIN_STATE_INVALID",
                stage="login_form",
                http_status=response.status_code,
            )
        return response

    def _password_login_state(self, username: str, password: str) -> dict[str, Any]:
        login_form = self._get_login_form_response(
            {
                "appId": self.APP_ID,
                "clientType": self.CLIENT_TYPE,
                "returnURL": self.RETURN_URL,
                "timeStamp": str(int(time.time() * 1000)),
            }
        )
        html = str(login_form.text or "")

        # The page contains a stable RSA fallback.  The modern page also
        # exposes a short-lived key through encryptConf.do; mirror the page
        # JavaScript and prefer that response when available.
        page_key = self._extract_optional_login_value(html, "j_rsaKey", r"j_rsaKey[\"']\s+value=[\"'](.+?)[\"']")
        public_key = page_key
        prefix = "{RSA}"
        try:
            encrypt_response = self.http.session.post(
                f"{self.auth_url}/api/logbox/config/encryptConf.do",
                data={"appId": self.APP_ID},
                timeout=self.timeout,
                verify=self.http.verify_tls,
            )
            self._raise_for_http(encrypt_response, "Cloud189 encryption config", stage="login_form")
            encrypt_payload = self._json_response(
                encrypt_response, "Cloud189 encryption config", stage="login_form"
            )
            encrypt_data = encrypt_payload.get("data") if isinstance(encrypt_payload.get("data"), dict) else {}
            configured_key = str(encrypt_data.get("pubKey") or "").strip()
            configured_prefix = str(encrypt_data.get("pre") or "").strip()
            if configured_key:
                public_key = configured_key
            if configured_prefix:
                prefix = configured_prefix
        except TianyiApiError:
            if not page_key:
                raise
            logger.info("event=tianyi_encrypt_config_fallback stage=login_form")
        except Exception as exc:  # noqa: BLE001
            if not page_key:
                raise self._network_error("login_form", "Cloud189 encryption config request failed", exc) from exc
            logger.info("event=tianyi_encrypt_config_fallback stage=login_form")

        if not public_key:
            raise TianyiApiError(
                "Cloud189 login page returned no RSA public key",
                code="ENCRYPT_CONFIG_FAILED",
                stage="login_form",
            )

        def optional(name: str, pattern: str, default: str = "") -> str:
            return self._extract_optional_login_value(html, name, pattern, default)

        account_type = optional("accountType", r"\baccountType\s*=\s*['\"](.*?)['\"]", self.ACCOUNT_TYPE)
        # OpenList 189CloudPC encrypts the operator's exact input.  Appending a
        # page-provided mail suffix changes mobile/alias accounts and can turn
        # a valid password into a provider-side credential failure.
        login_name = username
        state = {
            "app_key": self.APP_ID,
            "captcha_token": self._extract_login_value(
                html, "captchaToken", r"['\"]captchaToken['\"]\s+value=['\"](.+?)['\"]"
            ),
            "lt": self._extract_login_value(html, "lt", r"\blt\s*=\s*['\"](.+?)['\"]"),
            "param_id": self._extract_login_value(html, "paramId", r"\bparamId\s*=\s*['\"](.+?)['\"]"),
            "req_id": self._extract_login_value(html, "reqId", r"\breqId\s*=\s*['\"](.+?)['\"]"),
            "return_url": self._extract_login_value(
                html, "returnUrl", r"\breturnUrl\s*=\s*['\"](.+?)['\"]"
            ),
            "account_type": account_type,
            "client_type": optional("clientType", r"\bclientType\s*=\s*['\"](.*?)['\"]", self.CLIENT_TYPE),
            "is_oauth2": optional("isOauth2", r"\bisOauth2\s*=\s*['\"](.*?)['\"]", "false"),
            "state": optional("state", r"\bstate\s*=\s*['\"](.*?)['\"]", ""),
            "rsa_prefix": prefix or "{RSA}",
            "login_name": login_name,
        }
        try:
            state["rsa_username"] = f"{state['rsa_prefix']}{self._rsa_encrypt(public_key, login_name)}"
            state["rsa_password"] = f"{state['rsa_prefix']}{self._rsa_encrypt(public_key, password)}"
        except TianyiApiError as exc:
            if not exc.stage:
                exc.stage = "login_form"  # type: ignore[misc]
            raise
        return state

    def _submit_password_login(self, state: dict[str, Any], validate_code: str) -> dict[str, Any]:
        required = (
            "captcha_token", "lt", "param_id", "req_id", "return_url",
            "rsa_username", "rsa_password",
        )
        if any(not str(state.get(key) or "") for key in required):
            raise TianyiApiError(
                "Cloud189 login challenge is incomplete",
                code="LOGIN_STATE_INVALID",
                stage="login_submit",
            )
        form = {
            "appKey": str(state.get("app_key") or self.APP_ID),
            "accountType": str(state.get("account_type") or self.ACCOUNT_TYPE),
            "userName": str(state["rsa_username"]),
            "password": str(state["rsa_password"]),
            "validateCode": str(validate_code or ""),
            "captchaToken": str(state["captcha_token"]),
            "dynamicCheck": "FALSE",
            "clientType": str(state.get("client_type") or self.CLIENT_TYPE),
            "cb_SaveName": str(state.get("cb_save_name") or "1"),
            "isOauth2": str(state.get("is_oauth2") or "false"),
            "state": str(state.get("state") or ""),
            "returnUrl": str(state["return_url"]),
            "paramId": str(state["param_id"]),
        }
        try:
            response = self.http.session.post(
                f"{self.auth_url}/api/logbox/oauth2/loginSubmit.do",
                headers={
                    "Referer": f"{self.auth_url}/api/logbox/oauth2/unifyAccountLogin.do",
                    "X-Requested-With": "XMLHttpRequest",
                    "lt": str(state["lt"]),
                    "REQID": str(state["req_id"]),
                },
                data=form,
                timeout=self.timeout,
                verify=self.http.verify_tls,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._network_error("login_submit", "Cloud189 password login request failed", exc) from exc
        self._raise_for_http(response, "Cloud189 password login", stage="login_submit")
        value = self._json_response(response, "Cloud189 password login", stage="login_submit")
        redirect_url = str(
            value.get("toUrl")
            or value.get("redirectUrl")
            or value.get("redirect_url")
            or ""
        ).strip()
        if not redirect_url:
            message = str(value.get("msg") or value.get("message") or "Cloud189 password login failed")
            provider_code = self._response_code(value)
            if not provider_code and value.get("result") not in (None, 0, "0", "success", "SUCCESS"):
                provider_code = str(value.get("result") or "")
            captcha_error = self._is_captcha_error(provider_code, message, value)
            code = "CAPTCHA_INVALID" if captcha_error else provider_code or "LOGIN_FAILED"
            logger.warning(
                "event=tianyi_login_submit_failed stage=login_submit msg=%s result=%s code=%s raw=%s",
                message,
                value.get("result"),
                code,
                json.dumps(value, ensure_ascii=False)[:500],
            )
            if captcha_error:
                return self._captcha_challenge(
                    state,
                    message="天翼云验证码错误，请输入新图中的字符",
                    error=message,
                )
            raise TianyiApiError(message, code=code, stage="login_submit", http_status=response.status_code)
        session = self._normalize_session_data(
            {**value, **self._get_pc_session(redirect_url=redirect_url)}
        )
        self._publish_tokens(session)
        return {
            "status": "completed",
            "session": session,
            "credentials": self.token_credentials(session),
            "identity_hint": str(session.get("loginName") or ""),
        }

    def token_credentials(self, token_data: dict[str, Any]) -> dict[str, Any]:
        value = self._normalize_session_data(token_data)
        access_token = str(value.get("accessToken") or "").strip()
        refresh_token = str(value.get("refreshToken") or "").strip()
        expires_in = self._as_int(value.get("expiresIn"), default=self._ACCESS_TOKEN_TTL)
        expires_in = max(60, expires_in or self._ACCESS_TOKEN_TTL)
        result: dict[str, Any] = {}
        session_key = str(value.get("sessionKey") or "").strip()
        session_secret = str(value.get("sessionSecret") or "").strip()
        if session_key:
            result["session_key"] = session_key
        if session_secret:
            result["session_secret"] = session_secret
        if access_token:
            result["access_token"] = access_token
            explicit_expiry = self._as_timestamp(
                value.get("access_token_expires_at")
                or value.get("accessTokenExpiresAt")
                or value.get("expires_at")
            )
            result["access_token_expires_at"] = int(
                explicit_expiry or (time.time() + expires_in)
            )
        if refresh_token:
            result["refresh_token"] = refresh_token
        return result

    def _publish_tokens(self, token_data: dict[str, Any]) -> None:
        value = self._normalize_session_data(token_data)
        credentials = self.token_credentials(value)
        if not credentials:
            return
        self.access_token = str(credentials.get("access_token") or self.access_token)
        self.refresh_token = str(credentials.get("refresh_token") or self.refresh_token)
        self.session_key = str(credentials.get("session_key") or self.session_key)
        self.session_secret = str(credentials.get("session_secret") or self.session_secret)
        if self.session_key:
            self._auth_data["sessionKey"] = self.session_key
        if self.session_secret:
            self._auth_data["sessionSecret"] = self.session_secret
        self.access_token_expires_at = self._as_timestamp(
            credentials.get("access_token_expires_at") or self.access_token_expires_at
        )
        if self._token_update_callback is not None:
            self._token_update_callback(dict(credentials))

    def _cache_auth(self, cache_key: str, value: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_session_data(value)
        now = time.monotonic()
        with self._auth_cache_lock:
            for key, cached in list(self._auth_cache.items()):
                if cached[0] <= now:
                    self._auth_cache.pop(key, None)
            while cache_key not in self._auth_cache and len(self._auth_cache) >= self._AUTH_CACHE_MAX_ENTRIES:
                oldest = min(self._auth_cache, key=lambda key: self._auth_cache[key][0])
                self._auth_cache.pop(oldest, None)
            self._auth_cache[cache_key] = (now + self._AUTH_CACHE_TTL, dict(normalized))
        self._session_refreshed_at = now
        self._last_keep_alive_at = now
        return normalized

    def _invalidate_auth(self) -> None:
        cache_key = self._auth_cache_key()
        self._auth_data = {}
        self._force_refresh = True
        with self._auth_cache_lock:
            self._auth_cache.pop(cache_key, None)

    def _auth_cache_key(self) -> str:
        material = json.dumps(
            {
                "cache_namespace": self.cache_namespace,
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "session_key": self.session_key,
                "sson": self._sson_cookie(),
                "username": self.username,
                "password": self.password,
                "web_url": self.web_url,
                "api_url": self.api_url,
                "auth_url": self.auth_url,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _access_token_is_fresh(self) -> bool:
        return bool(self.access_token) and (
            not self.access_token_expires_at or self.access_token_expires_at > time.time() + 60
        )

    @staticmethod
    def _rsa_encrypt(public_key: str, value: str) -> str:
        key_text = str(public_key or "").strip()
        if "BEGIN PUBLIC KEY" not in key_text:
            key_text = f"-----BEGIN PUBLIC KEY-----\n{key_text}\n-----END PUBLIC KEY-----"
        try:
            key = RSA.import_key(key_text)
            encrypted = PKCS1_v1_5.new(key).encrypt(str(value).encode("utf-8"))
        except (ValueError, IndexError, TypeError) as exc:
            raise TianyiApiError("Cloud189 RSA encryption failed", code="ENCRYPT_FAILED") from exc
        # OpenList 189CloudPC serialises the encrypted block as upper-case hex.
        return encrypted.hex().upper()

    @staticmethod
    def _extract_login_value(html: str, name: str, pattern: str) -> str:
        match = re.search(pattern, str(html or ""), flags=re.IGNORECASE)
        if not match:
            raise TianyiApiError(
                f"Cloud189 login page is missing {name}",
                code="LOGIN_FORM_INVALID",
                stage="login_form",
            )
        return str(match.group(1))

    @staticmethod
    def _extract_optional_login_value(html: str, name: str, pattern: str, default: str = "") -> str:
        match = re.search(pattern, str(html or ""), flags=re.IGNORECASE)
        return str(match.group(1)) if match else str(default)

    @staticmethod
    def _json_response(
        response: Any,
        label: str,
        *,
        stage: str = "",
    ) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise TianyiApiError(
                f"{label} returned a non-JSON response",
                code="INVALID_RESPONSE",
                stage=stage,
                http_status=getattr(response, "status_code", None),
            ) from exc
        if not isinstance(value, dict):
            raise TianyiApiError(
                f"{label} returned an invalid payload",
                code="INVALID_RESPONSE",
                stage=stage,
                http_status=getattr(response, "status_code", None),
            )
        return value

    @classmethod
    def _response_payload(
        cls,
        response: Any,
        label: str,
        *,
        stage: str = "",
    ) -> dict[str, Any]:
        """Decode Cloud189 JSON success payloads and XML error payloads."""

        try:
            value = response.json()
        except (AttributeError, ValueError):
            value = None
        if isinstance(value, dict):
            return value
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            content = bytes(getattr(response, "content", b"") or b"")
            if content:
                text = content.decode("utf-8", errors="replace").strip()
        if text:
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                root = None
            if root is not None:
                parsed = cls._xml_element_value(root)
                if isinstance(parsed, dict):
                    return parsed
                return {cls._xml_tag(root.tag): parsed}
        raise TianyiApiError(
            f"{label} returned an invalid payload",
            code="INVALID_RESPONSE",
            stage=stage,
            http_status=getattr(response, "status_code", None),
        )

    @classmethod
    def _xml_element_value(cls, element: ET.Element) -> Any:
        children = list(element)
        if not children:
            return str(element.text or "").strip()
        result: dict[str, Any] = {}
        for child in children:
            key = cls._xml_tag(child.tag)
            value = cls._xml_element_value(child)
            if key in result:
                if not isinstance(result[key], list):
                    result[key] = [result[key]]
                result[key].append(value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _xml_tag(tag: str) -> str:
        return str(tag or "").rsplit("}", 1)[-1]

    @staticmethod
    def _normalize_session_data(value: dict[str, Any] | None) -> dict[str, Any]:
        raw = dict(value or {})
        nested = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        merged = {**nested, **raw}
        aliases = {
            "sessionKey": ("sessionKey", "session_key"),
            "sessionSecret": ("sessionSecret", "session_secret"),
            "accessToken": ("accessToken", "access_token"),
            "refreshToken": ("refreshToken", "refresh_token"),
            "expiresIn": ("expiresIn", "expires_in"),
            "accessTokenExpiresAt": (
                "accessTokenExpiresAt",
                "access_token_expires_at",
                "expires_at",
            ),
            "loginName": ("loginName", "login_name"),
        }
        result = dict(merged)
        result.pop("data", None)
        for canonical, names in aliases.items():
            for name in names:
                if merged.get(name) not in (None, ""):
                    result[canonical] = merged[name]
                    break
        return result

    @classmethod
    def _raise_for_http(cls, response: Any, label: str, *, stage: str) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 400:
            return
        try:
            value = response.json()
        except (AttributeError, ValueError):
            value = {}
        code = cls._response_code(value)
        message = cls._message(value, f"{label} HTTP {status}")
        raise TianyiApiError(message, code=code or f"HTTP_{status}", stage=stage, http_status=status)

    @staticmethod
    def _captcha_required(response: Any) -> bool:
        text = str(getattr(response, "text", "") or "").strip()
        if text:
            normalized = text.strip().lower()
            if normalized not in {"0", "false", "no", "null", "none", ""}:
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError):
                    return True
                if not isinstance(parsed, dict):
                    return str(parsed).strip().lower() not in {"0", "false", "no", "none", ""}
        try:
            value = response.json()
        except (AttributeError, ValueError):
            return False
        if isinstance(value, dict):
            raw = value.get("result") if "result" in value else value.get("needCaptcha")
            return str(raw if raw is not None else "").strip().lower() not in {
                "", "0", "false", "no", "none", "null",
            }
        return False

    @staticmethod
    def _is_captcha_error(code: Any, message: Any, payload: Any = None) -> bool:
        normalized_code = str(code or "").strip().lower().replace("_", "")
        if normalized_code in {
            "-2",
            "captchainvalid",
            "captchaerror",
            "validatecodeerror",
            "validatecodeinvalid",
            "needcaptcha",
        }:
            return True
        haystack = " ".join(
            (
                str(message or ""),
                json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else str(payload or ""),
            )
        ).lower()
        return any(
            marker in haystack
            for marker in (
                "captcha",
                "validatecode",
                "验证码",
                "校验码",
            )
        )

    @staticmethod
    def _network_error(stage: str, message: str, exc: BaseException) -> TianyiApiError:
        # Do not include ``str(exc)`` here: requests can include a full URL
        # with query parameters, which may contain a token or redirect ticket.
        return TianyiApiError(message, code="NETWORK_ERROR", stage=stage)

    @staticmethod
    def _as_api_error(stage: str, message: str, exc: BaseException) -> TianyiApiError:
        if isinstance(exc, TianyiApiError):
            return exc
        return TianyiApiError(message, code="LOGIN_FAILED", stage=stage)

    @staticmethod
    def _as_timestamp(value: Any) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        if number > 10_000_000_000:
            number /= 1000
        return number

    def _sson_cookie(self) -> str:
        value = str(self.cookie or "").strip()
        match = re.search(r"(?:^|;\s*)SSON=([^;]+)", value, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        if value and "=" not in value:
            return value
        return ""

    @classmethod
    def _share_code(cls, url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        query = parse_qs(parsed.query)
        code = str((query.get("code") or query.get("shareCode") or [""])[0])
        if not code and "content.21cn.com" in parsed.netloc and parsed.fragment:
            fragment_query = parsed.fragment.split("?", 1)[1] if "?" in parsed.fragment else ""
            code = str((parse_qs(fragment_query).get("shareCode") or [""])[0])
        if not code:
            match = re.search(r"(?:^|/)t/([A-Za-z0-9]+)(?:/|$)", parsed.path)
            if not match:
                match = re.search(r"(?:^|/)t/([A-Za-z0-9]+)(?:/|$)", parsed.fragment)
            code = match.group(1) if match else ""
        if not code and "share.html" in parsed.path:
            code = parsed.fragment.rstrip("/").split("/")[-1]
        if not re.fullmatch(r"[A-Za-z0-9]+", code or ""):
            raise TianyiApiError("Invalid Cloud189 share URL", code="SHARE_URL_INVALID")
        return code

    @staticmethod
    def _signature(values: dict[str, Any]) -> str:
        parameters = sorted(f"{key}={TianyiImporter._scalar(value)}" for key, value in values.items())
        return hashlib.md5("&".join(parameters).encode("utf-8")).hexdigest()  # noqa: S324

    @staticmethod
    def _query_values(values: dict[str, Any]) -> dict[str, str]:
        return {str(key): TianyiImporter._scalar(value) for key, value in values.items() if value is not None}

    @staticmethod
    def _scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        if isinstance(value, dict):
            return "[object Object]"
        if isinstance(value, (list, tuple)):
            return ",".join(TianyiImporter._scalar(item) for item in value)
        return str(value)

    @staticmethod
    def _response_code(value: Any) -> str:
        if not isinstance(value, dict):
            return "INVALID_RESPONSE"
        code = value.get("res_code")
        if code is None:
            code = value.get("errorCode")
        if code is None:
            code = value.get("code")
        if code is None and "result" in value:
            result = value.get("result")
            if result not in (None, 0, "0", "success", "SUCCESS"):
                code = result
        return str(code if code is not None else "")

    @staticmethod
    def _message(value: Any, fallback: str) -> str:
        if not isinstance(value, dict):
            return fallback
        return TianyiImporter._clean_cloud189_message(
            str(
                value.get("res_message")
                or value.get("res_msg")
                or value.get("errorMsg")
                or value.get("errorMessage")
                or value.get("msg")
                or value.get("error")
                or value.get("error_description")
                or value.get("message")
                or fallback
            )
        )

    @staticmethod
    def _clean_cloud189_message(text: str) -> str:
        """Strip Cloud189 error-noise and map known business rejections.

        The upload API returns ``msg`` values like
        ``sessionKey=<uuid>,user free space has been used up,requestId=<uuid>``.
        Drop the keyed noise and give the actionable cases a friendly message
        instead of dumping the raw ``sessionKey``/``requestId`` at the user.
        """
        cleaned = ", ".join(
            segment.strip()
            for segment in str(text or "").split(",")
            if segment.strip()
            and segment.strip().partition("=")[0].strip().lower()
            not in {"sessionkey", "requestid"}
        ).strip(" ,")
        lowered = cleaned.lower()
        if "user free space has been used up" in lowered:
            return "天翼云盘免费空间已用尽，请先清理空间或升级容量后再试"
        if "invalidpartsize" in lowered or "invalid part size" in lowered:
            return "天翼云上传分片参数无效"
        return cleaned

    @staticmethod
    def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
        file_id = TianyiImporter._first_id(
            item.get("id"), item.get("fileId"), item.get("folderId")
        )
        name = str(item.get("name") or item.get("fileName") or "")
        is_folder_value = item.get("isFolder")
        if is_folder_value is None:
            # Cloud189 folder rows carry ``id``/``parentId`` and no ``fileId``;
            # files carry ``fileId``.  Trust an explicit ``isFolder`` when
            # present, otherwise fall back to that structural difference.
            is_folder = bool(item.get("folderId")) or (
                bool(item.get("parentId")) and not bool(item.get("fileId"))
            )
        else:
            is_folder = TianyiImporter._as_bool(is_folder_value, default=False)
        return {
            "id": file_id,
            "file_id": file_id,
            "name": name,
            "file_name": name,
            "is_folder": is_folder,
            "is_dir": is_folder,
            "md5": str(item.get("md5") or ""),
            "size": TianyiImporter._as_int(item.get("size"), default=0),
            "raw": item,
        }

    @staticmethod
    def _batch_task_info(item: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "fileId": str(item.get("file_id") or item.get("id") or ""),
            "fileName": str(item.get("name") or item.get("file_name") or ""),
            "isFolder": 1 if bool(item.get("is_folder") or item.get("is_dir")) else 0,
        }
        if item.get("md5"):
            result["md5"] = str(item["md5"])
        return result

    @staticmethod
    def _select_items(items: Any, selection: dict[str, Any] | None) -> list[dict[str, Any]]:
        rows = [dict(item) for item in items or [] if isinstance(item, dict)]
        if not isinstance(selection, dict):
            return rows
        selected: list[Any] = []
        sources: list[dict[str, Any]] = []
        pending = [selection]
        visited: set[int] = set()
        while pending:
            source = pending.pop(0)
            marker = id(source)
            if marker in visited:
                continue
            visited.add(marker)
            sources.append(source)
            for key in ("tianyi_selection", "provider_selection", "generic_selection"):
                candidate = source.get(key)
                if isinstance(candidate, dict):
                    pending.append(candidate)
        for source in sources:
            for key in ("selected_items", "selected_files", "selected_folders", "selected_dirs"):
                value = source.get(key)
                if isinstance(value, list):
                    selected.extend(value)
        if not selected:
            return rows

        root_by_id = {
            str(item.get("file_id") or item.get("id") or ""): item
            for item in rows
            if str(item.get("file_id") or item.get("id") or "")
        }
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in selected:
            if isinstance(candidate, dict):
                file_id = str(
                    candidate.get("file_id")
                    or candidate.get("fileId")
                    or candidate.get("fid")
                    or candidate.get("id")
                    or ""
                ).strip()
            elif isinstance(candidate, (str, int)):
                file_id = str(candidate).strip()
            else:
                continue
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            if file_id in root_by_id:
                result.append(dict(root_by_id[file_id]))
                continue
            value = candidate if isinstance(candidate, dict) else {}
            name = str(
                value.get("name")
                or value.get("file_name")
                or value.get("fileName")
                or file_id
            )
            type_text = str(value.get("type") or value.get("kind") or "").strip().lower()
            is_folder = bool(
                value.get("is_folder")
                or value.get("is_dir")
                or value.get("directory")
                or type_text in {"dir", "folder", "directory"}
            )
            result.append(
                {
                    "id": file_id,
                    "file_id": file_id,
                    "name": name,
                    "file_name": name,
                    "is_folder": is_folder,
                    "is_dir": is_folder,
                    "md5": str(value.get("md5") or ""),
                    "size": TianyiImporter._as_int(value.get("size"), default=0),
                }
            )
        return result

    @staticmethod
    def _safe_folder_name(value: str) -> str:
        name = re.sub(r"[\\/:*?\"<>|]+", "_", str(value or "")).strip().strip(".")
        return name[:255]

    @staticmethod
    def _receipt_task_id(receipt: Any) -> str:
        return str(getattr(receipt, "external_ref", "") or getattr(receipt, "operation_id", "") or "").strip()

    @staticmethod
    def _receipt_raw(receipt: Any) -> dict[str, Any]:
        metadata = getattr(receipt, "metadata", {}) or {}
        raw = metadata.get("raw") if isinstance(metadata, dict) else {}
        return dict(raw) if isinstance(raw, dict) else {}

    @staticmethod
    def _as_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "folder"}

    @staticmethod
    def _first_id(*values: Any) -> str:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _as_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default


__all__ = ["TianyiApiError", "TianyiImporter"]
