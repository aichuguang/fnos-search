from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from ..constants import JOB_DONE, JOB_FAILED, JOB_SUBMITTED
from .base import AdapterCheckResult, ImportResult


DEFAULT_SIXPAN_HOST = "openapi.2dland.cn"

REQUEST_SUFFIX = "hl6_request"
SIGN_PREFIX = "HL6"
SIGN_ALGORITHM = "HL6-HMAC-SHA256"


class SixPanApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, data: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data


@dataclass
class SixPanTaskState:
    state: str
    message: str
    progress: int = 0
    bytes_total: int = 0
    bytes_processed: int = 0
    task: dict[str, Any] | None = None

    @property
    def completed(self) -> bool:
        return self.state == "completed"

    @property
    def failed(self) -> bool:
        return self.state == "failed"


class SixPanClient:
    def __init__(self, config: dict[str, Any], token_update_callback: Any = None):
        self.host = _normalize_host(config.get("host") or config.get("api_host") or DEFAULT_SIXPAN_HOST)
        self.client_id = str(config.get("client_id") or "").strip()
        self.client_secret = str(config.get("client_secret") or "").strip()
        self.oauth_client_id = str(config.get("oauth_client_id") or config.get("auth_client_id") or self.client_id).strip()
        self.access_token = str(config.get("access_token") or config.get("token") or "").strip()
        self.refresh_token = str(config.get("refresh_token") or "").strip()
        self.timeout = int(config.get("timeout", 20) or 20)
        self.verify_tls = bool(config.get("verify_tls", True))
        self.session = requests.Session()
        self.token_update_callback = token_update_callback if callable(token_update_callback) else None

    @property
    def auth_configured(self) -> bool:
        return bool(self.host and self.client_id and self.client_secret)

    @property
    def configured(self) -> bool:
        return bool(self.auth_configured and (self.access_token or self.refresh_token))

    def request_device_code(self, *, device: str = "fnos-media-import/1.0", scope: str = "") -> dict[str, Any]:
        if not self.auth_configured:
            raise SixPanApiError("六盘 OpenAPI ClientID/ClientSecret 未配置，无法发起授权")
        payload = {
            "client_id": self.oauth_client_id or self.client_id,
            "device": device or "fnos-media-import/1.0",
        }
        if scope:
            payload["scope"] = scope
        data = self.post("/v6/oauth/device_code", _compact_payload(payload), allow_refresh=False)
        return data if isinstance(data, dict) else {"response": data}

    def get_device_code_state(self, *, device_code: str, user_code: str = "") -> dict[str, Any]:
        if not self.auth_configured:
            raise SixPanApiError("六盘 OpenAPI ClientID/ClientSecret 未配置，无法检查授权状态")
        payload = {
            "device_code": str(device_code or "").strip(),
        }
        if user_code:
            payload["user_code"] = str(user_code or "").strip()
        if not payload["device_code"]:
            raise SixPanApiError("六盘 device_code 为空，无法检查授权状态")
        data = self.post("/v6/oauth/get_device_code_state", payload, allow_refresh=False)
        response = data if isinstance(data, dict) else {"response": data}
        self.apply_token_response(response)
        return response

    def apply_token_response(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        access_token = _first_text(data, "access_token", "accessToken")
        refresh_token = _first_text(data, "refresh_token", "refreshToken")
        expires_in = _int(_first_value(data, "expires_in", "expiresIn"), 0)
        expires_in_ts = _int(_first_value(data, "expires_in_ts", "expiresInTs"), 0)
        if access_token:
            self.access_token = access_token
        if refresh_token:
            self.refresh_token = refresh_token
        result: dict[str, Any] = {}
        if access_token:
            result["access_token"] = access_token
        if refresh_token:
            result["refresh_token"] = refresh_token
        if expires_in:
            result["expires_in"] = expires_in
        if expires_in_ts:
            result["expires_in_ts"] = expires_in_ts
        if result and self.token_update_callback:
            try:
                self.token_update_callback(result)
            except Exception:
                pass
        return result

    def refresh_access_token(self) -> dict[str, Any]:
        if not self.refresh_token:
            raise SixPanApiError("六盘 refresh_token 未配置，无法刷新 access_token")
        payload = {
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
            "client_id": self.client_id,
        }
        data = self.post("/v6/oauth/refresh_token", payload, allow_refresh=False)
        token_data = self.apply_token_response(data if isinstance(data, dict) else {})
        if not token_data.get("access_token"):
            raise SixPanApiError("六盘刷新 token 成功但响应缺少 access_token", data=data)
        return data if isinstance(data, dict) else {"response": data}

    def post(self, path: str, payload: dict[str, Any] | None = None, *, allow_refresh: bool = True) -> Any:
        if allow_refresh and not self.access_token and self.refresh_token:
            self.refresh_access_token()
        try:
            return self._post_once(path, payload or {})
        except SixPanApiError as exc:
            if exc.status_code == 401 and allow_refresh and self.refresh_token:
                self.refresh_access_token()
                return self._post_once(path, payload or {})
            raise

    def _post_once(self, path: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._signed_headers("POST", path, body)
        url = f"https://{self.host}{path}"
        try:
            response = self.session.post(
                url,
                data=body,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
        except requests.RequestException as exc:
            raise SixPanApiError(f"六盘 API 请求异常：{exc}") from exc
        try:
            data: Any = response.json()
        except ValueError:
            data = {"text": response.text}
        if response.status_code < 200 or response.status_code >= 300:
            message = _response_message(data) or f"HTTP {response.status_code}"
            raise SixPanApiError(f"六盘 API 请求失败：{message}", status_code=response.status_code, data=data)
        return data

    def _signed_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        date_string = timestamp[:10]
        nonce = _base36(int(time.time_ns())) + secrets.token_hex(4)

        headers = {
            "host": self.host,
            "content-type": "application/json; charset=utf-8",
            "x-hl-nonce": nonce,
            "x-hl-timestamp": timestamp,
            # Go SDK 的签名器默认带这个头；服务端允许额外签名头存在。
            "other-header": "other-value",
        }
        signed_header_names = sorted(headers.keys())
        signed_headers = ";".join(signed_header_names)
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in signed_header_names)
        canonical_request = "\n".join(
            [
                method.upper(),
                path,
                "",
                canonical_headers,
                signed_headers,
                hashlib.sha256(body).hexdigest(),
            ]
        )
        credential_scope = f"{date_string}/{self.access_token}/{REQUEST_SUFFIX}"
        string_to_sign = "\n".join(
            [
                SIGN_ALGORITHM,
                timestamp,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature_key = (SIGN_PREFIX + self.client_secret).encode("utf-8")
        date_key = hmac.new(signature_key, date_string.encode("utf-8"), hashlib.sha256).digest()
        token_key = hmac.new(date_key, self.access_token.encode("utf-8"), hashlib.sha256).digest()
        signing_key = hmac.new(token_key, REQUEST_SUFFIX.encode("utf-8"), hashlib.sha256).digest()
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            f"{SIGN_ALGORITHM} "
            f"Credential={self.client_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        request_headers = {
            "Content-Type": headers["content-type"],
            "X-Hl-Nonce": headers["x-hl-nonce"],
            "X-Hl-Timestamp": headers["x-hl-timestamp"],
            "Other-Header": headers["other-header"],
            "Authorization": authorization,
            "User-Agent": "fnos-media-import/1.0",
        }
        # C++ 官方客户端只参与签名不显式发送 Host；requests 会自动带正确 Host。
        return request_headers

    def parse_offline_task(self, source_url: str, *, source_type: str = "", title: str = "") -> dict[str, Any]:
        payload = {
            "url": source_url,
            "from": "fnos-media-import",
        }
        if title:
            payload["file"] = title
        if source_type:
            payload["addon"] = json.dumps({"source_type": source_type}, ensure_ascii=False, separators=(",", ":"))
        data = self.post("/v6/offline_task/parse", _compact_payload(payload))
        return data if isinstance(data, dict) else {"response": data}

    def add_offline_task(
        self,
        *,
        source_url: str,
        title: str,
        save_path: str,
        ignore_files: list[str] | None = None,
        callbacks: list[str] | None = None,
        addon: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": source_url,
            "name": title,
            "save_path": save_path,
        }
        if ignore_files:
            payload["ignore_files"] = ignore_files
        encoded_callbacks = [_encode_callback_url(url) for url in callbacks or [] if str(url or "").strip()]
        if encoded_callbacks:
            payload["callbacks"] = encoded_callbacks
        if addon:
            payload["addon"] = json.dumps(addon, ensure_ascii=False, separators=(",", ":"))
        data = self.post("/v6/offline_task/add", _compact_payload(payload))
        return data if isinstance(data, dict) else {"response": data}

    def list_offline_tasks(self, *, limit: int = 100, token: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "list_info": {
                "limit": max(1, min(int(limit or 100), 500)),
            }
        }
        if token:
            payload["list_info"]["token"] = token
        data = self.post("/v6/offline_task/list", payload)
        return data if isinstance(data, dict) else {"response": data}

    def delete_offline_tasks(self, identities: list[str], *, delete_files: bool = False) -> dict[str, Any]:
        payload = {"identity": identities, "delete_files": bool(delete_files)}
        data = self.post("/v6/offline_task/delete", payload)
        return data if isinstance(data, dict) else {"response": data}

    def get_user(self) -> dict[str, Any]:
        data = self.post("/v6/user/get", {})
        return data if isinstance(data, dict) else {"response": data}


class SixPanOfflineImporter:
    def __init__(self, config: dict[str, Any], token_update_callback: Any = None):
        self.config = config or {}
        self.client = SixPanClient(self.config, token_update_callback=token_update_callback)
        self.timeout = self.client.timeout
        self.parse_before_add = bool(self.config.get("parse_before_add", True))
        self.parse_required = bool(self.config.get("parse_required", False))
        # Preview, content-guard and final submission can all ask SixPan to
        # parse the same magnet.  A short, bounded in-process cache avoids
        # multiplying API calls (and the resulting busy/reset responses) while
        # keeping a new importer instance after config reload fully isolated.
        self.parse_cache_ttl_seconds = _bounded_config_int(
            self.config.get("parse_cache_ttl_seconds", 300), default=300, minimum=0, maximum=86400
        )
        self.parse_cache_max_entries = _bounded_config_int(
            self.config.get("parse_cache_max_entries", 128), default=128, minimum=0, maximum=2048
        )
        self._parse_cache: OrderedDict[tuple[str, str, str], tuple[float, dict[str, Any]]] = OrderedDict()
        self._parse_cache_lock = threading.RLock()
        self.refresh_after_submit = bool(self.config.get("refresh_after_submit", False))
        self.poll_enabled = bool(self.config.get("poll_enabled", True))
        self.task_missing_poll_limit = _bounded_config_int(
            self.config.get("task_missing_poll_limit", 5), default=5, minimum=1, maximum=100
        )
        self.task_unknown_poll_limit = _bounded_config_int(
            self.config.get("task_unknown_poll_limit", 5), default=5, minimum=1, maximum=100
        )
        self.submitted_timeout_seconds = _bounded_config_int(
            self.config.get("submitted_timeout_seconds", 7 * 24 * 3600),
            default=7 * 24 * 3600,
            minimum=0,
            maximum=90 * 24 * 3600,
        )
        self.task_max_pages = _bounded_config_int(
            self.config.get("task_max_pages", 200), default=200, minimum=1, maximum=2000
        )
        self.fnos_mount_name = str(
            self.config.get("fnos_mount_name")
            or self.config.get("mount_name")
            or self.config.get("mount_path")
            or ""
        ).strip()
        self.mount_name = self.fnos_mount_name
        self.mount_path = self.fnos_mount_name
        self.success_statuses = _status_set(self.config.get("success_statuses"), {"completed", "done", "success", "finished", "3", "4", "100"})
        self.failed_statuses = _status_set(self.config.get("failed_statuses"), {"failed", "error", "fail", "-1", "5", "6"})
        self.running_statuses = _status_set(
            self.config.get("running_statuses"),
            {
                "created",
                "new",
                "pending",
                "waiting",
                "queued",
                "ready",
                "running",
                "downloading",
                "processing",
                "retrying",
                "0",
                "1",
                "2",
            },
        )
        self.callback_urls = _string_list(self.config.get("callbacks") or self.config.get("callback_urls"))

    @property
    def auth_configured(self) -> bool:
        return self.client.auth_configured

    @property
    def configured(self) -> bool:
        return self.client.configured

    def describe(self) -> dict[str, Any]:
        return {
            "name": "6盘离线",
            "adapter_type": "sixpan_offline",
            "configured": self.configured,
            "capabilities": {
                "submit": self.configured,
                "parse": self.configured,
                "task_poll": self.configured and self.poll_enabled,
                "delete": self.configured,
                "oauth_device_code": self.auth_configured,
                "refresh_after_submit": self.refresh_after_submit,
            },
            "config": {
                "host": self.client.host,
                "client_id_configured": bool(self.client.client_id),
                "client_secret_configured": bool(self.client.client_secret),
                "oauth_client_id_configured": bool(self.client.oauth_client_id),
                "access_token_configured": bool(self.client.access_token),
                "refresh_token_configured": bool(self.client.refresh_token),
                "authorized": bool(self.client.access_token or self.client.refresh_token),
                "timeout": self.timeout,
                "parse_before_add": self.parse_before_add,
                "parse_cache_ttl_seconds": self.parse_cache_ttl_seconds,
                "parse_cache_max_entries": self.parse_cache_max_entries,
                "poll_enabled": self.poll_enabled,
                "task_max_pages": self.task_max_pages,
                "task_missing_poll_limit": self.task_missing_poll_limit,
                "task_unknown_poll_limit": self.task_unknown_poll_limit,
                "submitted_timeout_seconds": self.submitted_timeout_seconds,
                "fnos_mount_name": self.fnos_mount_name,
            },
        }

    def check_configuration(self) -> AdapterCheckResult:
        if not self.client.host:
            return AdapterCheckResult(False, "unconfigured", "六盘 OpenAPI host 未配置")
        if not self.client.client_id or not self.client.client_secret:
            return AdapterCheckResult(False, "unconfigured", "六盘 OpenAPI ClientID/ClientSecret 未配置")
        if not self.client.access_token and not self.client.refresh_token:
            return AdapterCheckResult(False, "unconfigured", "六盘账号未授权：只需配置 ClientID/ClientSecret 后，在后台点击“开始授权”获取 token")
        return AdapterCheckResult(True, "ready", "六盘离线适配器已配置", self.describe())

    def start_device_authorization(self, *, device: str = "fnos-media-import/1.0", scope: str = "") -> dict[str, Any]:
        return self.client.request_device_code(device=device, scope=scope)

    def check_device_authorization(self, *, device_code: str, user_code: str = "") -> dict[str, Any]:
        return self.client.get_device_code_state(device_code=device_code, user_code=user_code)

    def extract_tokens(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.client.apply_token_response(data)

    def probe(self) -> AdapterCheckResult:
        config = self.check_configuration()
        if not config.ok:
            return config
        try:
            user = self.client.get_user()
        except Exception as exc:  # noqa: BLE001
            return AdapterCheckResult(False, "failed", f"六盘账号探测失败：{exc}", self.describe())
        return AdapterCheckResult(True, "ready", "六盘账号探测通过", {"user": _redact(user), **self.describe()})

    def parse_resource(self, title: str, source_url: str, source_type: str = "") -> dict[str, Any]:
        source_type = source_type or ("torrent" if _looks_like_torrent(source_url) else "magnet")
        key = (str(source_type).strip().lower(), str(source_url or "").strip(), str(title or "").strip())
        now = time.monotonic()
        if self.parse_cache_ttl_seconds > 0 and self.parse_cache_max_entries > 0:
            with self._parse_cache_lock:
                cached = self._parse_cache.get(key)
                if cached and now - cached[0] <= self.parse_cache_ttl_seconds:
                    self._parse_cache.move_to_end(key)
                    return copy.deepcopy(cached[1])
                if cached:
                    self._parse_cache.pop(key, None)
        data = self.client.parse_offline_task(source_url, source_type=source_type, title=title)
        if not isinstance(data, dict):
            data = {"response": data}
        if self.parse_cache_ttl_seconds > 0 and self.parse_cache_max_entries > 0:
            with self._parse_cache_lock:
                self._parse_cache[key] = (now, copy.deepcopy(data))
                self._parse_cache.move_to_end(key)
                while len(self._parse_cache) > self.parse_cache_max_entries:
                    self._parse_cache.popitem(last=False)
        return copy.deepcopy(data)

    def import_resource(
        self,
        title: str,
        source_url: str,
        category: dict[str, Any],
        password: str = "",
        ignore_files: list[str] | None = None,
        save_path: str = "",
    ) -> ImportResult:
        config = self.check_configuration()
        if not config.ok:
            return ImportResult(False, JOB_FAILED, config.message)

        save_path = str(save_path or "").strip() or self.target_path_for_category(category)
        if not save_path:
            return ImportResult(False, JOB_FAILED, "六盘离线保存目录为空，请配置 sixpan_save_path 或 quark_save_path")

        parse_data: dict[str, Any] = {}
        parse_error = ""
        expected_file_count = 0
        source_type = "torrent" if _looks_like_torrent(source_url) else "magnet"
        ignore_files = _string_list(ignore_files)
        if self.parse_before_add:
            try:
                parse_data = self.parse_resource(title, source_url, source_type=source_type)
                expected_file_count = _task_file_count(parse_data)
            except Exception as exc:  # noqa: BLE001
                parse_error = str(exc)
                if self.parse_required:
                    return ImportResult(
                        False,
                        JOB_FAILED,
                        f"六盘离线解析失败：{parse_error}",
                        target_path=save_path,
                        raw_data={"parse_error": parse_error, "request": {"title": title, "source_url": _redact_url(source_url), "save_path": save_path}},
                    )

        addon = {
            "title": title,
            "source": "fnos-media-import",
            "password": password or "",
        }
        try:
            response = self.client.add_offline_task(
                source_url=source_url,
                title=title,
                save_path=save_path,
                ignore_files=ignore_files,
                callbacks=self.callback_urls,
                addon=addon,
            )
        except Exception as exc:  # noqa: BLE001
            return ImportResult(
                False,
                JOB_FAILED,
                f"六盘离线任务提交失败：{exc}",
                target_path=save_path,
                raw_data={
                    "parse": _redact(parse_data),
                    "parse_error": parse_error,
                    "request": {"title": title, "source_url": _redact_url(source_url), "save_path": save_path},
                },
            )

        task_id = _task_identity(response)
        state = self.task_state(response)
        status = JOB_DONE if state.completed else JOB_SUBMITTED
        if state.failed:
            status = JOB_FAILED
        message = "六盘离线任务已完成，等待 Organizer 整理" if state.completed else "六盘离线任务已提交，等待离线完成后进入 Organizer"
        if state.failed:
            message = f"六盘离线任务提交后返回失败状态：{state.message}"
        if parse_error:
            message += f"；解析接口未通过但已继续提交：{parse_error}"

        raw_data = {
            "request": {"title": title, "source_url": _redact_url(source_url), "save_path": save_path, "ignore_files": ignore_files},
            "parse": _redact(parse_data),
            "parse_error": parse_error,
            "add": _redact(response),
            "task_state": state.__dict__ | {"task": _redact(state.task or {})},
            "expected_file_count": expected_file_count,
            "file_count": expected_file_count,
        }
        return ImportResult(
            not state.failed,
            status,
            message,
            external_task_id=task_id,
            target_path=save_path,
            raw_data=raw_data,
        )

    def target_path_for_category(self, category: dict[str, Any]) -> str:
        path = str(
            category.get("sixpan_save_path")
            or self.config.get("default_save_path")
            or category.get("quark_save_path")
            or category.get("mobile_target_path")
            or ""
        ).strip()
        # 早期配置沿用了夸克中转目录“/离线下载/分类”。六盘现在是直连保存，
        # 用户只需要六盘根目录下的五个分类目录：/电影、/电视剧、/动漫、/综艺、/其他。
        legacy_prefix = "/离线下载/"
        if path.startswith(legacy_prefix):
            path = "/" + path[len(legacy_prefix) :].strip("/")
        if not path:
            label = str(category.get("label") or "").strip()
            path = f"/{label}" if label else ""
        return path

    def list_tasks(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return up to ``limit`` tasks while following continuation tokens.

        The API's ``limit`` is a page size, not a guaranteed result count.  We
        therefore keep fetching pages until the requested logical limit is
        reached or the server reports that there is no continuation token.
        ``find_task`` deliberately uses the same iterator without the logical
        limit so old submitted jobs are not hidden on page two or later.
        """

        wanted = max(1, min(int(limit or 100), 5000))
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for task in self._iter_tasks(page_size=min(wanted, 500), max_pages=self.task_max_pages):
            identity = _task_identity(task)
            key = identity or json.dumps(task, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            result.append(task)
            if len(result) >= wanted:
                break
        return result

    def find_task(self, task_id: str, *, limit: int = 200) -> dict[str, Any] | None:
        wanted = str(task_id or "").strip()
        if not wanted:
            return None
        for task in self._iter_tasks(page_size=max(1, min(int(limit or 200), 500)), max_pages=self.task_max_pages):
            if wanted == _task_identity(task) or wanted in {
                str(task.get("identity") or "").strip(),
                str(task.get("task_identity") or task.get("taskIdentity") or "").strip(),
                str(task.get("id") or "").strip(),
                str(task.get("task_id") or task.get("taskId") or "").strip(),
            }:
                return task
        return None

    def _iter_tasks(self, *, page_size: int, max_pages: int):
        """Yield task rows from every SixPan continuation page.

        SixPan deployments have returned both ``list_info.token`` and
        ``pagination.next_token`` over time, so token extraction is deliberately
        tolerant.  A repeated token or a missing token while ``has_more`` is
        true is surfaced as an API error rather than being mistaken for a
        definitive "task missing" result by the poller.
        """

        token = ""
        seen_tokens: set[str] = set()
        for _page_number in range(max(1, max_pages)):
            response = self.client.list_offline_tasks(limit=page_size, token=token)
            body = _dict_body(response)
            rows = _task_rows(body)
            for task in rows:
                yield task
            # Pagination metadata is returned inside ``data`` by some
            # deployments and next to ``data`` by others.  Inspect the full
            # response rather than losing an outer list_info while unwrapping
            # the task rows.
            next_token, has_more = _next_task_page(response)
            if not next_token:
                if has_more:
                    raise SixPanApiError("六盘任务列表声明仍有下一页，但未返回 continuation token")
                return
            if next_token in seen_tokens or next_token == token:
                raise SixPanApiError("六盘任务列表 continuation token 重复，已停止分页")
            seen_tokens.add(next_token)
            token = next_token
        raise SixPanApiError("六盘任务列表分页超过安全上限，已停止继续请求")

    def task_state(self, task: dict[str, Any]) -> SixPanTaskState:
        if not isinstance(task, dict):
            return SixPanTaskState("unknown", "任务响应为空")
        task = _dict_body(task)
        if not task:
            return SixPanTaskState("unknown", "任务响应为空")
        status_value = task.get("status")
        status_text = str(status_value if status_value is not None else "").strip().lower()
        code = _int(task.get("code"), 0)
        progress = _int(task.get("progress"), 0)
        bytes_total = _int(task.get("bytes_total") or task.get("bytesTotal"), 0)
        bytes_processed = _int(task.get("bytes_processed") or task.get("bytesProcessed"), 0)
        message = str(task.get("message") or "").strip()

        if status_text in self.failed_statuses or (code < 0):
            return SixPanTaskState("failed", message or f"status={status_text or code}", progress, bytes_total, bytes_processed, task)
        if status_text in self.success_statuses:
            return SixPanTaskState("completed", message or f"status={status_text}", progress, bytes_total, bytes_processed, task)
        if progress >= 100:
            return SixPanTaskState("completed", message or "progress=100", progress, bytes_total, bytes_processed, task)
        if bytes_total > 0 and bytes_processed >= bytes_total:
            return SixPanTaskState("completed", message or "已处理字节数达到总大小", progress, bytes_total, bytes_processed, task)
        if status_text in self.running_statuses:
            return SixPanTaskState("running", message or f"status={status_text}", progress, bytes_total, bytes_processed, task)
        if progress > 0 or bytes_processed > 0:
            return SixPanTaskState("running", message or f"status={status_text or 'progressing'}", progress, bytes_total, bytes_processed, task)
        if status_text:
            return SixPanTaskState("unknown", message or f"未知任务状态：{status_text}", progress, bytes_total, bytes_processed, task)
        return SixPanTaskState("unknown", message or "任务状态为空", progress, bytes_total, bytes_processed, task)


def _normalize_host(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("http://"):
        text = text[len("http://") :]
    elif text.startswith("https://"):
        text = text[len("https://") :]
    return text.strip("/")


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        result[key] = value
    return result


def _looks_like_torrent(source_url: str) -> bool:
    text = str(source_url or "").lower()
    return text.endswith(".torrent") or ".torrent?" in text or "torrent" in text[:40]


def _response_message(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message", "msg", "error_description", "error", "code"):
            value = data.get(key)
            if value:
                return str(value)
    return ""


def _task_identity(data: dict[str, Any]) -> str:
    data = _dict_body(data)
    return str(
        data.get("identity")
        or data.get("task_identity")
        or data.get("taskIdentity")
        or data.get("id")
        or data.get("task_id")
        or data.get("taskId")
        or ""
    ).strip()


def _task_file_count(data: dict[str, Any]) -> int:
    data = _dict_body(data)
    task_files = data.get("task_files") or data.get("taskFiles") if isinstance(data, dict) else []
    if not isinstance(task_files, list):
        return 0
    count = 0
    for item in task_files:
        if isinstance(item, dict) and item.get("directory"):
            continue
        count += 1
    return count


def _encode_callback_url(url: str) -> str:
    payload = json.dumps({"url": str(url).strip()}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


def _dict_body(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    for key in ("data", "response", "result"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return data


def _task_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    for key in ("tasks", "offline_tasks", "offlineTasks", "items", "list"):
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _next_task_page(*bodies: dict[str, Any]) -> tuple[str, bool]:
    """Extract a continuation token from the API's known response shapes."""

    containers: list[tuple[dict[str, Any], bool]] = []
    pending: list[tuple[dict[str, Any], bool]] = [
        (body, False) for body in bodies if isinstance(body, dict)
    ]
    seen: set[int] = set()
    pagination_keys = ("list_info", "listInfo", "pagination", "page_info", "pageInfo", "page")
    wrapper_keys = ("data", "response", "result")
    while pending:
        container, pagination_container = pending.pop(0)
        identity = id(container)
        if identity in seen:
            continue
        seen.add(identity)
        containers.append((container, pagination_container))
        for key in (*wrapper_keys, *pagination_keys):
            value = container.get(key)
            if isinstance(value, dict):
                pending.append((value, pagination_container or key in pagination_keys))

    if not containers:
        return "", False
    token = ""
    has_more = False
    has_more_seen = False
    for container, _pagination_container in containers:
        if not token:
            for key in (
                "next_token",
                "nextToken",
                "next_page_token",
                "nextPageToken",
                "continuation_token",
                "continuationToken",
                "next_cursor",
                "nextCursor",
            ):
                candidate = str(container.get(key) or "").strip()
                if candidate:
                    token = candidate
                    break
        for key in ("has_more", "hasMore", "more"):
            if key in container:
                has_more_seen = True
                value = container.get(key)
                if isinstance(value, str):
                    container_has_more = value.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    container_has_more = bool(value)
                has_more = has_more or container_has_more
                break
    # Some SixPan responses use list_info.token as the *next* token.  Only
    # interpret that field as a continuation when the response also indicates
    # that more rows exist; otherwise it may simply echo the request cursor.
    if not token:
        for container, pagination_container in containers:
            if not pagination_container:
                continue
            candidate = str(container.get("token") or "").strip()
            if candidate and (has_more or not has_more_seen):
                token = candidate
                break
    return token, has_more


def _status_set(value: Any, default: set[str]) -> set[str]:
    items = _string_list(value)
    return {item.strip().lower() for item in items if item.strip()} or set(default)


def _bounded_config_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw = value.replace("\n", ",").replace("|", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return None


def _first_text(data: dict[str, Any], *keys: str) -> str:
    value = _first_value(data, *keys)
    return str(value or "").strip()


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value <= 0:
        return "0"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result


def _redact_url(value: str) -> str:
    text = str(value or "")
    if text.lower().startswith("magnet:"):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return f"magnet:?xt=sha256:{digest}"
    return text


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in {
                "access_token",
                "accesstoken",
                "refresh_token",
                "refreshtoken",
                "client_secret",
                "clientsecret",
                "secret_key",
                "authorization",
                "token",
                "device_code",
                "devicecode",
            }:
                result[key] = "***" if item else ""
            elif str(key).lower() in {"url", "source_url"}:
                result[key] = _redact_url(str(item or ""))
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
