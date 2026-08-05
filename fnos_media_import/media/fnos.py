from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from requests import RequestException

from ..fnos_paths import path_tails


API_LOGIN = "/v/api/v1/login"
API_MDB_LIST = "/v/api/v1/mdb/list"
API_MDB_SCAN = "/v/api/v1/mdb/scan/{guid}"
API_TASK_STOP = "/v/api/v1/task/stop"
API_MEDIADB_SUM = "/v/api/v1/mediadb/sum"
API_MEDIADB_LIST = "/v/api/v1/mediadb/list"
API_TASK_RUNNING = "/v/api/v1/task/running"
API_TASK_RUNNING_FALLBACKS = (API_TASK_RUNNING, "/api/v1/task/running")

SIGN_MODE_AUTO = "auto"
SIGN_MODE_PHP = "php"
SIGN_MODE_FNV = "fnv"

MSG_REFRESH_NOT_CONFIGURED = "\u98de\u725b\u5237\u65b0\u914d\u7f6e\u4e0d\u5b8c\u6574\uff0c\u8bf7\u914d\u7f6e FNOS_SERVER_URL / FNOS_API_KEY / FNOS_SECRET"
MSG_LOGIN_NOT_CONFIGURED = "\u98de\u725b\u8d26\u53f7\u5bc6\u7801\u672a\u914d\u7f6e\uff0c\u65e0\u6cd5\u81ea\u52a8\u91cd\u65b0\u767b\u5f55"
MSG_LOGIN_FAILED = "\u98de\u725b\u767b\u5f55\u5931\u8d25"
MSG_LIBRARY_REQUIRED = "\u98de\u725b\u5a92\u4f53\u5e93\u540d\u79f0\u4e0d\u80fd\u4e3a\u7a7a"
MSG_LIBRARY_LIST_FAILED = "\u98de\u725b\u5a92\u4f53\u5e93\u5217\u8868\u83b7\u53d6\u5931\u8d25"
MSG_LIBRARY_NOT_FOUND = "\u672a\u627e\u5230\u5bf9\u5e94\u98de\u725b\u5a92\u4f53\u5e93"
MSG_REFRESH_TRIGGERED = "\u5df2\u89e6\u53d1\u98de\u725b\u5a92\u4f53\u5e93\u5237\u65b0"
MSG_DIR_REFRESH_TRIGGERED = "\u5df2\u89e6\u53d1\u98de\u725b\u5a92\u4f53\u5e93\u76ee\u5f55\u5237\u65b0"
MSG_REFRESH_FAILED = "\u98de\u725b\u5a92\u4f53\u5e93\u5237\u65b0\u5931\u8d25"
MSG_DUPLICATE_RESTART_FAILED = "\u98de\u725b\u91cd\u590d\u5237\u65b0\u4efb\u52a1\u505c\u6b62\u540e\u91cd\u8bd5\u5931\u8d25"
MSG_REQUEST_FAILED = "\u98de\u725b API \u8bf7\u6c42\u5931\u8d25"
MSG_AUTH_RETRY_EXHAUSTED = "\u98de\u725b token \u5931\u6548\uff0c\u91cd\u65b0\u767b\u5f55\u91cd\u8bd5 3 \u6b21\u540e\u4ecd\u5931\u8d25"
MSG_GUID_REQUIRED = "\u98de\u725b\u5a92\u4f53\u5e93 GUID \u4e0d\u80fd\u4e3a\u7a7a"
MSG_SUMMARY_FAILED = "\u98de\u725b\u5a92\u4f53\u5e93\u7edf\u8ba1\u83b7\u53d6\u5931\u8d25"
MSG_RUNNING_FAILED = "\u98de\u725b\u5237\u65b0\u4efb\u52a1\u72b6\u6001\u83b7\u53d6\u5931\u8d25"
MSG_SUMMARY_LOADED = "\u5df2\u83b7\u53d6\u98de\u725b\u5a92\u4f53\u5e93\u7edf\u8ba1"
MSG_LIBRARY_LIST_LOADED = "\u5df2\u83b7\u53d6\u98de\u725b\u5a92\u4f53\u5e93\u5217\u8868"
MSG_DIR_NOT_MATCHED = "\u672a\u5339\u914d\u5230\u98de\u725b\u5a92\u4f53\u5e93\u771f\u5b9e\u5237\u65b0\u76ee\u5f55\uff0c\u8bf7\u786e\u8ba4\u5165\u5e93\u76ee\u6807\u4e0e\u98de\u725b\u5a92\u4f53\u5e93 dir_list \u4e00\u81f4"
MSG_DIR_LIST_REQUIRED = "\u98de\u725b\u5a92\u4f53\u5e93\u5237\u65b0\u5fc5\u987b\u643a\u5e26 dir_list\uff0c\u5df2\u62d2\u7edd\u6574\u5e93\u626b\u63cf"


class FnosMediaRefresher:
    """FNOS media-library refresh client with token cache and dir_list scan support."""

    def __init__(self, config: dict[str, Any]):
        self.config = config or {}
        self.server_url = _clean_base_url(
            self.config.get("server_url")
            or self.config.get("base_url")
            or self.config.get("url")
            or ""
        )
        self.username = str(self.config.get("username") or "").strip()
        self.password = str(self.config.get("password") or "").strip()
        self.app_name = str(self.config.get("app_name") or "trimemedia-web").strip()
        self.api_key = str(self.config.get("api_key") or "").strip()
        self.secret = str(self.config.get("secret") or self.config.get("secret_string") or "").strip()
        self.timeout = int(self.config.get("timeout", 20) or 20)
        self.verify_ssl = _as_bool(self.config.get("verify_ssl"), False)
        self.max_auth_retries = max(1, int(self.config.get("max_auth_retries", 3) or 3))
        self.retry_delay = float(self.config.get("retry_delay", 1) or 1)
        self.scan_dedupe_seconds = max(0, int(self.config.get("scan_dedupe_seconds", 45) or 45))
        self.log_enabled = _as_bool(self.config.get("log_enabled"), True)
        self.log_path = _resolve_cache_path(self.config.get("log_path") or "data/fnos_refresh.log")
        self.sign_mode = _normalize_sign_mode(self.config.get("sign_mode") or SIGN_MODE_AUTO)
        self.token_cache_path = _resolve_cache_path(self.config.get("token_cache_path") or "data/fnos_token.json")
        self.session = requests.Session()
        self.lock = threading.RLock()
        self.token = str(self.config.get("token") or "").strip() or self._load_cached_token()
        self._recent_scans: dict[tuple[str, tuple[str, ...]], float] = {}

    def describe(self) -> dict[str, Any]:
        return {
            "configured": self._configured(),
            "server_url": self.server_url,
            "sign_mode": self.sign_mode,
            "has_token": bool(self.token),
            "has_login": bool(self.username and self.password),
            "log_enabled": self.log_enabled,
            "log_path": str(self.log_path),
        }

    def refresh(self, library: str, dir_list: Any = None) -> dict[str, Any]:
        libraries = _split_libraries(library)
        if len(libraries) > 1:
            items = [self.refresh(name, dir_list=dir_list) for name in libraries]
            success = all(item.get("success") for item in items)
            return {
                "success": success,
                "message": MSG_REFRESH_TRIGGERED if success else MSG_REFRESH_FAILED,
                "library": "|".join(libraries),
                "items": items,
            }

        library_name = libraries[0] if libraries else ""
        requested_dirs = _normalize_dir_list(dir_list)
        if not self._configured():
            return {"success": False, "message": MSG_REFRESH_NOT_CONFIGURED, "library": library_name, "dir_list": requested_dirs}
        if not library_name:
            return {"success": False, "message": MSG_LIBRARY_REQUIRED, "dir_list": requested_dirs}

        with self.lock:
            if not self.token and not self.login(force=True):
                return {"success": False, "message": MSG_LOGIN_FAILED, "library": library_name, "dir_list": requested_dirs}

            list_result = self._request("GET", API_MDB_LIST, require_auth=True)
            if not _is_success_response(list_result):
                return {
                    "success": False,
                    "message": _response_message(list_result, MSG_LIBRARY_LIST_FAILED),
                    "library": library_name,
                    "dir_list": requested_dirs,
                    "response": _compact_response(list_result),
                }

            libraries_data = list_result.get("data") if isinstance(list_result, dict) else []
            library_item = _find_library_item(libraries_data, library_name)
            if not library_item:
                return {
                    "success": False,
                    "message": f"{MSG_LIBRARY_NOT_FOUND}\uff1a{library_name}",
                    "library": library_name,
                    "dir_list": requested_dirs,
                    "available_libraries": _library_names(libraries_data),
                }

            guid = str(library_item.get("guid") or "")
            dirs = _resolve_scan_dir_list(library_item, requested_dirs)
            if requested_dirs and not dirs:
                return _dir_not_matched_response(library=library_name, guid=guid, requested_dirs=requested_dirs, library_item=library_item)

            scan_result = self._scan_library(str(guid), dirs)
            success = _is_success_response(scan_result)
            return {
                "success": success,
                "message": (MSG_DIR_REFRESH_TRIGGERED if dirs else MSG_REFRESH_TRIGGERED) if success else _response_message(scan_result, MSG_REFRESH_FAILED),
                "library": library_name,
                "guid": str(guid),
                "dir_list": dirs,
                "requested_dir_list": requested_dirs,
                "scan": _compact_response(scan_result),
            }

    def refresh_guid(self, guid: str, library: str = "", dir_list: Any = None) -> dict[str, Any]:
        media_guid = str(guid or "").strip()
        library_name = str(library or "").strip()
        requested_dirs = _normalize_dir_list(dir_list)
        if not self._configured():
            return {"success": False, "message": MSG_REFRESH_NOT_CONFIGURED, "library": library_name, "guid": media_guid, "dir_list": requested_dirs}
        if not media_guid:
            return {"success": False, "message": MSG_GUID_REQUIRED, "library": library_name, "dir_list": requested_dirs}

        with self.lock:
            if not self.token and not self.login(force=True):
                return {"success": False, "message": MSG_LOGIN_FAILED, "library": library_name, "guid": media_guid, "dir_list": requested_dirs}

            library_item: dict[str, Any] | None = None
            list_result = self._request("GET", API_MDB_LIST, require_auth=True)
            if _is_success_response(list_result):
                libraries_data = list_result.get("data") if isinstance(list_result, dict) else []
                library_item = _find_library_item_by_guid(libraries_data, media_guid)
                library_name = library_name or _library_item_name(library_item)
            elif requested_dirs and not _absolute_scan_dirs(requested_dirs):
                return {
                    "success": False,
                    "message": _response_message(list_result, MSG_LIBRARY_LIST_FAILED),
                    "library": library_name,
                    "guid": media_guid,
                    "dir_list": requested_dirs,
                    "response": _compact_response(list_result),
                }

            dirs = _resolve_scan_dir_list(library_item, requested_dirs)
            if requested_dirs and not dirs:
                return _dir_not_matched_response(library=library_name, guid=media_guid, requested_dirs=requested_dirs, library_item=library_item)

            scan_result = self._scan_library(media_guid, dirs)
            success = _is_success_response(scan_result)
            return {
                "success": success,
                "message": (MSG_DIR_REFRESH_TRIGGERED if dirs else MSG_REFRESH_TRIGGERED) if success else _response_message(scan_result, MSG_REFRESH_FAILED),
                "library": library_name,
                "guid": media_guid,
                "dir_list": dirs,
                "requested_dir_list": requested_dirs,
                "scan": _compact_response(scan_result),
            }

    def media_summary(self) -> dict[str, Any]:
        if not self._configured():
            return {"success": False, "message": MSG_REFRESH_NOT_CONFIGURED, "summary": {}, "data": {}, "response": {}}
        with self.lock:
            if not self.token and not self.login(force=True):
                return {"success": False, "message": MSG_LOGIN_FAILED, "summary": {}, "data": {}, "response": {}}
            result = self._request("GET", API_MEDIADB_SUM, require_auth=True)
            success = _is_success_response(result)
            data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else {}
            return {
                "success": success,
                "message": MSG_SUMMARY_LOADED if success else _response_message(result, MSG_SUMMARY_FAILED),
                "summary": data,
                "data": data,
                "response": _compact_response(result),
            }

    def media_libraries(self, *, include_summary: bool = True) -> dict[str, Any]:
        if not self._configured():
            return {"success": False, "message": MSG_REFRESH_NOT_CONFIGURED, "items": [], "summary": {}, "response": {}}
        with self.lock:
            if not self.token and not self.login(force=True):
                return {"success": False, "message": MSG_LOGIN_FAILED, "items": [], "summary": {}, "response": {}}
            result = self._request("GET", API_MEDIADB_LIST, require_auth=True)
            success = _is_success_response(result)
            items = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), list) else []
            summary: dict[str, Any] = {}
            summary_response: dict[str, Any] | None = None
            if include_summary:
                summary_result = self.media_summary()
                summary = summary_result.get("summary") if isinstance(summary_result.get("summary"), dict) else {}
                summary_response = {
                    "success": bool(summary_result.get("success")),
                    "message": summary_result.get("message") or "",
                    "response": summary_result.get("response") or {},
                }
            return {
                "success": success,
                "message": MSG_LIBRARY_LIST_LOADED if success else _response_message(result, MSG_LIBRARY_LIST_FAILED),
                "items": items,
                "summary": summary,
                "response": _compact_response(result),
                "summary_response": summary_response,
            }

    def refresh_libraries(self) -> dict[str, Any]:
        """Return FNOS media-library refresh config list.

        `/v/api/v1/mediadb/list` is good for poster/count display, but it does not
        include the actual filesystem `dir_list`.  `/v/api/v1/mdb/list` is the
        scan/refresh list and contains mounted paths such as `/vol02/...`.
        """

        if not self._configured():
            return {"success": False, "message": MSG_REFRESH_NOT_CONFIGURED, "items": [], "response": {}}
        with self.lock:
            if not self.token and not self.login(force=True):
                return {"success": False, "message": MSG_LOGIN_FAILED, "items": [], "response": {}}
            result = self._request("GET", API_MDB_LIST, require_auth=True)
            success = _is_success_response(result)
            items = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), list) else []
            return {
                "success": success,
                "message": MSG_LIBRARY_LIST_LOADED if success else _response_message(result, MSG_LIBRARY_LIST_FAILED),
                "items": items,
                "response": _compact_response(result),
            }

    def running_tasks(self) -> dict[str, Any]:
        if not self._configured():
            return {"success": False, "message": MSG_REFRESH_NOT_CONFIGURED, "items": [], "response": {}}
        with self.lock:
            if not self.token and not self.login(force=True):
                return {"success": False, "message": MSG_LOGIN_FAILED, "items": [], "response": {}}
            result: dict[str, Any] = {}
            attempted: list[dict[str, Any]] = []
            for path in API_TASK_RUNNING_FALLBACKS:
                result = self._request("GET", path, require_auth=True)
                attempted.append({"path": path, "response": _compact_response(result)})
                if _is_success_response(result):
                    break
                # 旧代码误用了不带 /v 的路径；不同飞牛版本也可能没有运行中任务接口。
                # 404 只影响后台“是否有运行中刷新任务”的展示，不应污染媒体库连接状态。
                if _http_status(result) == 404:
                    continue
                break
            success = _is_success_response(result)
            if not success and attempted and all(_http_status(item.get("response")) == 404 for item in attempted):
                return {
                    "success": True,
                    "message": "",
                    "items": [],
                    "response": {"optional_unavailable": True, "attempted": attempted},
                }
            data = result.get("data") if isinstance(result, dict) else []
            items = _extract_task_items(data)
            return {
                "success": success,
                "message": "" if success else _response_message(result, MSG_RUNNING_FAILED),
                "items": items,
                "response": _compact_response(result),
                "attempted": attempted,
            }

    def login(self, *, force: bool = False) -> bool:
        if self.token and not force:
            return True
        if not self.username or not self.password:
            return False

        payload = {"app_name": self.app_name, "username": self.username, "password": self.password}
        result = self._request_once("POST", API_LOGIN, data=payload, token="")
        token = ""
        if isinstance(result, dict):
            token = str((result.get("data") or {}).get("token") or "").strip()
        if token:
            self.token = token
            self._save_cached_token(token)
            return True
        return False

    def _scan_library(self, guid: str, dir_list: list[str]) -> dict[str, Any]:
        path = API_MDB_SCAN.format(guid=guid)
        if not dir_list:
            result = {"success": False, "message": MSG_DIR_LIST_REQUIRED, "guid": guid, "dir_list": []}
            self._write_refresh_log(
                "blocked_full_scan",
                {"method": "POST", "path": path, "guid": guid, "body": {}, "dir_list": [], "response": result},
            )
            return result
        body = {"dir_list": dir_list}
        request_payload = {"method": "POST", "path": path, "guid": guid, "body": body, "dir_list": dir_list}
        duplicate = self._recent_scan_result(guid, dir_list)
        if duplicate:
            self._write_refresh_log("deduped", {**request_payload, "response": _compact_response(duplicate)})
            return duplicate
        self._write_refresh_log("request", request_payload)
        result = self._request("POST", path, data=body, require_auth=True)
        if _response_code(result) != -14:
            self._write_refresh_log("response", {**request_payload, "response": _compact_response(result), "success": _is_success_response(result)})
            if _is_success_response(result):
                self._remember_scan(guid, dir_list)
            return result

        self._write_refresh_log("duplicate_task", {**request_payload, "response": _compact_response(result)})
        stop_result = self._request(
            "POST",
            API_TASK_STOP,
            data={"guid": guid, "type": "TaskItemScrap"},
            require_auth=True,
        )
        self._write_refresh_log(
            "duplicate_stop",
            {
                "method": "POST",
                "path": API_TASK_STOP,
                "body": {"guid": guid, "type": "TaskItemScrap"},
                "response": _compact_response(stop_result),
                "success": _is_success_response(stop_result),
            },
        )
        if not _is_success_response(stop_result):
            if isinstance(result, dict):
                result["_duplicate_stop"] = _compact_response(stop_result)
            return result

        if self.retry_delay > 0:
            time.sleep(self.retry_delay)
        retry_result = self._request("POST", path, data=body, require_auth=True)
        if isinstance(retry_result, dict):
            retry_result["_duplicate_stop"] = _compact_response(stop_result)
            retry_result["_retried_after_stop"] = True
            if not _is_success_response(retry_result):
                retry_result.setdefault("msg", MSG_DUPLICATE_RESTART_FAILED)
        self._write_refresh_log("retry_response", {**request_payload, "response": _compact_response(retry_result), "success": _is_success_response(retry_result)})
        if _is_success_response(retry_result):
            self._remember_scan(guid, dir_list)
        return retry_result

    def _write_refresh_log(self, event: str, payload: dict[str, Any]) -> None:
        if not self.log_enabled:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event,
                **payload,
            }
            with self.log_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            return

    def _recent_scan_result(self, guid: str, dir_list: list[str]) -> dict[str, Any] | None:
        if not self.scan_dedupe_seconds or not dir_list:
            return None
        key = self._scan_key(guid, dir_list)
        now = time.time()
        last = self._recent_scans.get(key)
        if last and now - last < self.scan_dedupe_seconds:
            return {
                "code": 0,
                "success": True,
                "data": True,
                "msg": "短时间内已触发相同目录刷新，本次跳过重复请求",
                "_deduped": True,
                "guid": str(guid or ""),
                "dir_list": list(dir_list),
            }
        self._prune_recent_scans(now)
        return None

    def _remember_scan(self, guid: str, dir_list: list[str]) -> None:
        if not self.scan_dedupe_seconds or not dir_list:
            return
        now = time.time()
        self._prune_recent_scans(now)
        self._recent_scans[self._scan_key(guid, dir_list)] = now

    def _prune_recent_scans(self, now: float) -> None:
        if not self.scan_dedupe_seconds:
            self._recent_scans.clear()
            return
        expired = [key for key, value in self._recent_scans.items() if now - value >= self.scan_dedupe_seconds]
        for key in expired:
            self._recent_scans.pop(key, None)

    @staticmethod
    def _scan_key(guid: str, dir_list: list[str]) -> tuple[str, tuple[str, ...]]:
        return str(guid or "").strip(), tuple(sorted(str(item or "").strip() for item in dir_list if str(item or "").strip()))

    def _request(self, method: str, path: str, *, data: Any = None, params: dict[str, Any] | None = None, require_auth: bool = True) -> dict[str, Any]:
        last_result: dict[str, Any] | None = None
        for _attempt in range(self.max_auth_retries):
            if require_auth and not self.token:
                if not self.login(force=True):
                    return _error_response(MSG_LOGIN_NOT_CONFIGURED, path=path)

            result = self._request_once(method, path, data=data, params=params, token=self.token if require_auth else "")
            if not isinstance(result, dict):
                return _error_response(MSG_REQUEST_FAILED, path=path)

            if _response_code(result) != -2:
                return result

            last_result = result
            if not require_auth or path == API_LOGIN:
                return result

            self._clear_token()
            if not self.login(force=True):
                return result

        if last_result is not None:
            last_result.setdefault("msg", MSG_AUTH_RETRY_EXHAUSTED)
            return last_result
        return _error_response(MSG_AUTH_RETRY_EXHAUSTED, path=path)

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        data: Any = None,
        params: dict[str, Any] | None = None,
        token: str = "",
    ) -> dict[str, Any]:
        if not self.server_url:
            return _error_response(MSG_REFRESH_NOT_CONFIGURED, path=path)

        method = method.upper()
        url = f"{self.server_url}{path}"
        params = params or None
        last_result: dict[str, Any] | None = None

        for sign_mode in self._sign_modes():
            body_text = self._request_body(method, path, data, sign_mode)
            authx = self._authx(method, path, params=params, body_text=body_text, sign_mode=sign_mode)
            headers = {
                "Content-Type": "application/json",
                "Authx": authx,
                "Cookie": "mode=relay",
            }
            if token:
                headers["Authorization"] = token

            try:
                request_kwargs: dict[str, Any] = {
                    "headers": headers,
                    "params": params,
                    "timeout": self.timeout,
                    "verify": self.verify_ssl,
                }
                if method in {"POST", "PUT", "PATCH"}:
                    request_kwargs["data"] = body_text.encode("utf-8")
                response = self.session.request(method, url, **request_kwargs)
                text = response.text
            except RequestException as exc:
                return _error_response(str(exc), path=path)

            try:
                decoded = response.json()
            except ValueError:
                decoded = {
                    "code": None,
                    "msg": f"HTTP {response.status_code}",
                    "raw": text[:1000],
                }
            if not isinstance(decoded, dict):
                decoded = {"code": None, "msg": f"HTTP {response.status_code}", "data": decoded}
            decoded["_http_status"] = response.status_code
            decoded["_sign_mode"] = sign_mode
            last_result = decoded

            # In auto sign mode, try the alternate sign rule when login fails.
            if self.sign_mode == SIGN_MODE_AUTO and path == API_LOGIN and not _is_success_response(decoded):
                continue
            return decoded

        return last_result or _error_response(MSG_REQUEST_FAILED, path=path)

    def _request_body(self, method: str, path: str, data: Any, sign_mode: str) -> str:
        if method.upper() == "GET":
            return ""
        if not isinstance(data, dict):
            payload: dict[str, Any] = {}
        else:
            payload = dict(data)
        if sign_mode == SIGN_MODE_PHP and not _is_mdb_scan_path(path):
            payload["nonce"] = _random_digits()
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def _authx(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        body_text: str,
        sign_mode: str,
    ) -> str:
        auth_nonce = _random_digits()
        timestamp = str(int(time.time() * 1000))
        if method.upper() == "GET" and sign_mode == SIGN_MODE_FNV and params:
            hash_source = urlencode(sorted(params.items()))
        elif method.upper() == "GET":
            hash_source = ""
        else:
            hash_source = body_text
        body_hash = _md5(hash_source)
        if sign_mode == SIGN_MODE_FNV:
            sign_parts = [self.secret, path, auth_nonce, timestamp, body_hash, self.api_key]
        else:
            sign_parts = [self.api_key, path, auth_nonce, timestamp, body_hash, self.secret]
        sign = _md5("_".join(sign_parts))
        return f"nonce={auth_nonce}&timestamp={timestamp}&sign={sign}"

    def _sign_modes(self) -> list[str]:
        if self.sign_mode == SIGN_MODE_AUTO:
            return [SIGN_MODE_PHP, SIGN_MODE_FNV]
        return [self.sign_mode]

    def _configured(self) -> bool:
        return bool(self.server_url and self.api_key and self.secret)

    def _load_cached_token(self) -> str:
        try:
            if not self.token_cache_path.exists():
                return ""
            payload = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return ""
        if str(payload.get("server_url") or "").rstrip("/") != self.server_url.rstrip("/"):
            return ""
        return str(payload.get("token") or "").strip()

    def _save_cached_token(self, token: str) -> None:
        try:
            self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"server_url": self.server_url, "token": token, "updated_at": int(time.time())}
            self.token_cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return

    def _clear_token(self) -> None:
        self.token = ""
        try:
            self.token_cache_path.unlink(missing_ok=True)
        except OSError:
            return


def _clean_base_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _resolve_cache_path(value: Any) -> Path:
    path = Path(str(value or "data/fnos_token.json"))
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / path


def _random_digits() -> str:
    return str(random.randint(100000, 999999))


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _response_code(response: Any) -> int | None:
    if not isinstance(response, dict):
        return None
    code = response.get("code")
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _http_status(response: Any) -> int | None:
    if not isinstance(response, dict):
        return None
    status = response.get("_http_status")
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _is_success_response(response: Any) -> bool:
    return _response_code(response) == 0


def _response_message(response: Any, default: str) -> str:
    if isinstance(response, dict):
        return str(response.get("msg") or response.get("message") or default)
    return default


def _compact_response(response: Any) -> Any:
    if not isinstance(response, dict):
        return response
    compact = dict(response)
    if isinstance(compact.get("raw"), str) and len(compact["raw"]) > 1000:
        compact["raw"] = compact["raw"][:1000]
    return compact


def _extract_task_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "list", "tasks", "records", "rows", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _error_response(message: str, *, path: str = "") -> dict[str, Any]:
    return {"code": None, "success": False, "msg": message, "path": path}


def _find_library_guid(items: Any, name: str) -> str:
    item = _find_library_item(items, name)
    return str(item.get("guid") or "") if item else ""


def _find_library_item(items: Any, name: str) -> dict[str, Any] | None:
    if not isinstance(items, list):
        return None
    normalized = _normalize_library_name(name)
    for item in items:
        if not isinstance(item, dict) or not item.get("guid"):
            continue
        names = [
            str(item.get("name") or ""),
            str(item.get("title") or ""),
            str(item.get("label") or ""),
        ]
        if any(_normalize_library_name(value) == normalized for value in names if value):
            return item
    return None


def _find_library_item_by_guid(items: Any, guid: str) -> dict[str, Any] | None:
    if not isinstance(items, list):
        return None
    wanted = str(guid or "").strip()
    if not wanted:
        return None
    for item in items:
        if isinstance(item, dict) and str(item.get("guid") or "").strip() == wanted:
            return item
    return None


def _library_item_name(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("name") or item.get("title") or item.get("label") or "").strip()


def _resolve_scan_dir_list(library_item: dict[str, Any] | None, requested_dirs: list[str]) -> list[str]:
    if not requested_dirs:
        return []
    actual_dirs = _absolute_scan_dirs((library_item or {}).get("dir_list"))
    if not actual_dirs:
        return _absolute_scan_dirs(requested_dirs)

    resolved: list[str] = []
    for hint in requested_dirs:
        text = str(hint or "").strip()
        if not text:
            continue
        matches = _match_actual_scan_dirs(actual_dirs, text)
        if matches and (_hint_segment_count(text) >= 2 or len(matches) == 1 or _is_absolute_scan_dir(text)):
            _extend_unique(resolved, matches)
            continue
        if _is_absolute_scan_dir(text):
            normalized = _normalize_scan_path(text)
            if normalized:
                _extend_unique(resolved, [normalized])
    return resolved


def _dir_not_matched_response(
    *,
    library: str,
    guid: str,
    requested_dirs: list[str],
    library_item: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "success": False,
        "message": MSG_DIR_NOT_MATCHED,
        "library": library,
        "guid": guid,
        "dir_list": [],
        "requested_dir_list": requested_dirs,
        "available_dir_list": _absolute_scan_dirs((library_item or {}).get("dir_list"))[:80],
    }


def _absolute_scan_dirs(value: Any) -> list[str]:
    result: list[str] = []
    for item in _normalize_dir_list(value):
        if not _is_absolute_scan_dir(item):
            continue
        normalized = _normalize_scan_path(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _match_actual_scan_dirs(actual_dirs: list[str], hint: str) -> list[str]:
    hint_norm = _normalize_scan_path(hint)
    if not hint_norm:
        return []
    hint_rel = hint_norm.strip("/")
    result: list[str] = []
    for actual in actual_dirs:
        actual_norm = _normalize_scan_path(actual)
        if not actual_norm:
            continue
        actual_rel = actual_norm.strip("/")
        if hint_norm.startswith("/"):
            if actual_norm == hint_norm:
                result.append(actual_norm)
            elif hint_norm.startswith(f"{actual_norm}/"):
                result.append(hint_norm)
            elif actual_norm.startswith(f"{hint_norm}/"):
                result.append(actual_norm)
            continue
        if actual_rel.endswith(f"/{hint_rel}") or actual_rel == hint_rel:
            result.append(actual_norm)
            continue
        for tail in _path_tails(actual_rel):
            if hint_rel == tail:
                result.append(actual_norm)
                break
            if hint_rel.startswith(f"{tail}/"):
                suffix = hint_rel[len(tail) + 1 :].strip("/")
                result.append(f"{actual_norm.rstrip('/')}/{suffix}" if suffix else actual_norm)
                break
    return list(dict.fromkeys(result))


def _path_tails(path: str) -> list[str]:
    return path_tails(path)


def _hint_segment_count(value: str) -> int:
    normalized = _normalize_scan_path(value)
    return len([part for part in normalized.strip("/").split("/") if part])


def _is_absolute_scan_dir(value: Any) -> bool:
    return str(value or "").strip().replace("\\", "/").startswith("/")


def _normalize_scan_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if not text:
        return ""
    if text.startswith("/"):
        return "/" + text.strip("/")
    return text.strip("/")


def _extend_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)


def _library_names(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name") or item.get("title")
            if name:
                names.append(str(name))
    return names[:50]


def _split_libraries(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in text.split("|") if item.strip()]


def _normalize_dir_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts = value.replace("\n", ",").replace("|", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]
    result: list[str] = []
    for item in parts:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _normalize_sign_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"legacy", "legacy_php", "old"}:
        return SIGN_MODE_PHP
    if mode in {SIGN_MODE_AUTO, SIGN_MODE_PHP, SIGN_MODE_FNV}:
        return mode
    return SIGN_MODE_PHP


def _is_mdb_scan_path(path: Any) -> bool:
    return str(path or "").strip().startswith("/v/api/v1/mdb/scan/")


def _normalize_library_name(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
