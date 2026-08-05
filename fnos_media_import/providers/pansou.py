from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import requests

from ..classifiers.link_classifier import detect_link


DEFAULT_CLOUD_TYPES = ["quark", "tianyi", "mobile", "magnet"]
DEFAULT_CONCURRENCY = 10
logger = logging.getLogger(__name__)


class PanSouAuthError(RuntimeError):
    pass


class PanSouTokenManager:
    def __init__(self, base_url: str, username: str = "", password: str = "", default_token: str = "", timeout: int = 15, verify_tls: bool = True):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = default_token
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.expires_at = self._extract_exp(default_token)

    @staticmethod
    def _extract_exp(token: str) -> int:
        if not token or token.count(".") < 2:
            return 0
        try:
            payload_part = token.split(".")[1]
            payload_part += "=" * (-len(payload_part) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_part).decode("utf-8"))
            return int(payload.get("exp", 0))
        except Exception:
            return 0

    def login(self) -> bool:
        if not self.username or not self.password:
            return False
        url = f"{self.base_url}/api/auth/login"
        try:
            response = _post_json_no_env_proxy(
                url,
                json={"username": self.username, "password": self.password},
                timeout=self.timeout,
                verify=self.verify_tls,
            )
        except requests.RequestException:
            return False
        if response.status_code != 200:
            return False
        try:
            data = response.json()
        except ValueError:
            return False
        token = data.get("token") or data.get("data", {}).get("token")
        if not token:
            return False
        self.token = token
        self.expires_at = int(data.get("expires_at") or self._extract_exp(token) or (time.time() + 86400))
        return True

    def get_token(self, force_refresh: bool = False) -> str:
        if force_refresh:
            return self.token if self.login() else ""
        if self.token and (not self.expires_at or time.time() < self.expires_at - 300):
            return self.token
        self.login()
        return self.token


class PanSouClient:
    def __init__(self, config: dict[str, Any], routes: dict[str, Any]):
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        self.timeout = _bounded_int(config.get("timeout"), 20, 1, 300)
        self.verify_tls = _as_bool(config.get("verify_tls"), True)
        self.routes = routes
        self.cloud_types = _normalize_cloud_types(config.get("cloud_types"), DEFAULT_CLOUD_TYPES)
        self.result_type = _normalize_choice(config.get("res"), {"all", "results", "merge"}, "merge")
        self.source_scope = _normalize_choice(config.get("src"), {"all", "tg", "plugin"}, "all")
        self.conc = _bounded_int(config.get("conc"), DEFAULT_CONCURRENCY, 0, 100)
        self.refresh = _as_bool(config.get("refresh"), False)
        self.channels = _normalize_string_list(config.get("channels"))
        self.plugins = _normalize_string_list(config.get("plugins"))
        self.ext = config.get("ext") if isinstance(config.get("ext"), dict) else {}
        self.filter_include = _normalize_string_list(config.get("filter_include"))
        self.filter_exclude = _normalize_string_list(config.get("filter_exclude"))
        self.async_poll_enabled = _as_bool(config.get("async_poll_enabled"), True)
        self.async_poll_interval_seconds = _bounded_float(config.get("async_poll_interval_seconds"), 0.8, 0.2, 10.0)
        self.async_poll_max_rounds = _bounded_int(config.get("async_poll_max_rounds"), 2, 0, 30)
        self.async_poll_stable_rounds = _bounded_int(config.get("async_poll_stable_rounds"), 1, 1, 10)
        self.token_manager = PanSouTokenManager(
            self.base_url,
            username=str(config.get("username", "")),
            password=str(config.get("password", "")),
            default_token=str(config.get("default_token", "")),
            timeout=self.timeout,
            verify_tls=self.verify_tls,
        )

    def search(self, keyword: str, sources: list[str] | None = None, token: str = "", options: dict[str, Any] | None = None) -> dict[str, Any]:
        if not keyword:
            raise ValueError("搜索关键词不能为空")
        if not self.base_url:
            raise ValueError("PanSou 地址未配置")

        runtime_options = options if isinstance(options, dict) else {}
        trace_id = str(runtime_options.get("trace_id") or "-")
        total_started = time.perf_counter()
        refresh = _as_bool(runtime_options.get("refresh"), self.refresh) if "refresh" in runtime_options else self.refresh
        async_poll_enabled = _as_bool(runtime_options.get("async_poll"), self.async_poll_enabled) if "async_poll" in runtime_options else self.async_poll_enabled
        async_poll_interval = _bounded_float(runtime_options.get("async_poll_interval_seconds"), self.async_poll_interval_seconds, 0.2, 10.0) if "async_poll_interval_seconds" in runtime_options else self.async_poll_interval_seconds
        async_poll_max_rounds = _bounded_int(runtime_options.get("async_poll_max_rounds"), self.async_poll_max_rounds, 0, 30) if "async_poll_max_rounds" in runtime_options else self.async_poll_max_rounds
        async_poll_stable_rounds = _bounded_int(runtime_options.get("async_poll_stable_rounds"), self.async_poll_stable_rounds, 1, 10) if "async_poll_stable_rounds" in runtime_options else self.async_poll_stable_rounds

        logger.info(
            "search_trace=%s stage=pansou_start keyword=%r res=%s src=%s conc=%s refresh=%s async_poll=%s cloud_types=%s",
            trace_id,
            _short_text(keyword),
            self.result_type,
            self.source_scope,
            self.conc,
            refresh,
            async_poll_enabled,
            ",".join(self.cloud_types),
        )
        first_started = time.perf_counter()
        data = self._request_search(keyword, sources, token, refresh=refresh)
        normalized_items = self.normalize_results(keyword, data)
        logger.info(
            "search_trace=%s stage=pansou_first elapsed_ms=%.1f raw_count=%d normalized_count=%d",
            trace_id,
            _elapsed_ms(first_started),
            _response_item_count(data),
            len(normalized_items),
        )
        snapshots = [{"round": 0, "count": len(normalized_items), "raw_count": _response_item_count(data)}]
        latest_data = data

        if async_poll_enabled and async_poll_max_rounds > 0:
            stable_rounds = 0
            for round_index in range(1, async_poll_max_rounds + 1):
                time.sleep(async_poll_interval)
                poll_started = time.perf_counter()
                poll_data = self._request_search(keyword, sources, token, refresh=False)
                poll_items = self.normalize_results(keyword, poll_data)
                merged_items = self._merge_items(normalized_items, poll_items)
                logger.info(
                    "search_trace=%s stage=pansou_poll round=%d elapsed_ms=%.1f raw_count=%d poll_count=%d merged_count=%d",
                    trace_id,
                    round_index,
                    _elapsed_ms(poll_started),
                    _response_item_count(poll_data),
                    len(poll_items),
                    len(merged_items),
                )
                snapshots.append({"round": round_index, "count": len(merged_items), "raw_count": _response_item_count(poll_data)})
                latest_data = poll_data
                if len(merged_items) <= len(normalized_items):
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                normalized_items = merged_items
                if stable_rounds >= async_poll_stable_rounds:
                    break

        logger.info(
            "search_trace=%s stage=pansou_done elapsed_ms=%.1f items=%d snapshots=%d",
            trace_id,
            _elapsed_ms(total_started),
            len(normalized_items),
            len(snapshots),
        )
        return {
            "raw": {
                "latest": latest_data,
                "snapshots": snapshots,
                "async_poll": {
                    "enabled": async_poll_enabled,
                    "interval_seconds": async_poll_interval,
                    "max_rounds": async_poll_max_rounds,
                    "stable_rounds": async_poll_stable_rounds,
                    "elapsed_hint_seconds": round(async_poll_interval * async_poll_max_rounds, 2),
                },
            },
            "items": normalized_items,
        }

    def _request_search(self, keyword: str, sources: list[str] | None, explicit_token: str = "", *, refresh: bool = False) -> Any:
        search_token = explicit_token or self.token_manager.get_token()
        response = self._perform_search(keyword, search_token, sources, refresh=refresh)
        if self._should_refresh_token(response):
            response = self._recover_auth_failure(keyword, sources, explicit_token, search_token, refresh=refresh)

        status_code, data = response
        if status_code >= 400:
            raise RuntimeError(f"PanSou 搜索失败：HTTP {status_code}")
        return data

    def _perform_search(self, keyword: str, token: str, sources: list[str] | None, *, refresh: bool = False) -> tuple[int, Any]:
        url = f"{self.base_url}/api/search"
        payload = self._search_payload(keyword, sources, refresh=refresh)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = _post_json_no_env_proxy(url, json=payload, headers=headers, timeout=self.timeout, verify=self.verify_tls)
        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text}
        return response.status_code, data

    def _recover_auth_failure(
        self,
        keyword: str,
        sources: list[str] | None,
        explicit_token: str,
        failed_token: str,
        *,
        refresh: bool = False,
    ) -> tuple[int, Any]:
        if explicit_token:
            raise PanSouAuthError("PanSou 临时 Token 无效或已过期，请刷新后重试")

        refreshed_token = self.token_manager.get_token(force_refresh=True)
        if refreshed_token and refreshed_token != failed_token:
            refreshed_response = self._perform_search(keyword, refreshed_token, sources, refresh=refresh)
            if not self._should_refresh_token(refreshed_response):
                return refreshed_response

        if failed_token:
            anonymous_response = self._perform_search(keyword, "", sources, refresh=refresh)
            if not self._should_refresh_token(anonymous_response):
                return anonymous_response

        if self.token_manager.username and self.token_manager.password:
            raise PanSouAuthError("PanSou 账号登录失败或授权已失效，请检查 PANSOU_USERNAME / PANSOU_PASSWORD")
        if failed_token:
            raise PanSouAuthError("PanSou Token 已过期或无效，请更新 PANSOU_DEFAULT_TOKEN，或配置 PANSOU_USERNAME / PANSOU_PASSWORD 自动刷新")
        raise PanSouAuthError("PanSou 需要授权，请配置 PANSOU_DEFAULT_TOKEN，或配置 PANSOU_USERNAME / PANSOU_PASSWORD")

    @staticmethod
    def _should_refresh_token(response: tuple[int, Any]) -> bool:
        status_code, data = response
        if status_code == 401:
            return True
        if isinstance(data, dict) and data.get("code") in ("AUTH_TOKEN_INVALID", "AUTH_TOKEN_EXPIRED", 401):
            return True
        return False

    def _search_payload(self, keyword: str, sources: list[str] | None, *, refresh: bool) -> dict[str, Any]:
        source_values = _normalize_string_list(sources)
        source_scope = self.source_scope
        for value in source_values:
            lowered = value.lower()
            if lowered in {"all", "tg", "plugin"}:
                source_scope = lowered
                break

        payload: dict[str, Any] = {
            "kw": keyword,
            "res": self.result_type,
            "src": source_scope,
            "refresh": bool(refresh),
            "cloud_types": self.cloud_types,
        }
        if self.conc > 0:
            payload["conc"] = self.conc
        if self.channels:
            payload["channels"] = self.channels
        # ``sources`` selects an outer search provider (for example ``pansou``
        # or ``btbtla``); it is not a PanSou plugin list.  When no plugins are
        # configured, omit the field so PanSou can use its complete default set.
        if self.plugins:
            payload["plugins"] = list(self.plugins)
        if self.ext:
            payload["ext"] = self.ext
        filter_payload: dict[str, Any] = {}
        if self.filter_include:
            filter_payload["include"] = self.filter_include
        if self.filter_exclude:
            filter_payload["exclude"] = self.filter_exclude
        if filter_payload:
            payload["filter"] = filter_payload
        return payload

    def normalize_results(self, keyword: str, data: Any) -> list[dict[str, Any]]:
        payload = data.get("data", data) if isinstance(data, dict) else data
        items: list[dict[str, Any]] = []

        def add_row(row: Any, source_hint: str = "") -> None:
            if isinstance(row, str):
                row = {"url": row}
            if not isinstance(row, dict):
                return
            links = row.get("links")
            if isinstance(links, list) and links:
                for link in links:
                    merged_row = dict(row)
                    if isinstance(link, dict):
                        merged_row.update(link)
                        link_hint = link.get("type") or link.get("cloud_type") or row.get("source") or source_hint
                    else:
                        merged_row["url"] = str(link or "").strip()
                        link_hint = row.get("source") or source_hint
                    item = self._row_to_item(keyword, merged_row, str(link_hint or "pansou"))
                    if item:
                        items.append(item)
                return
            item = self._row_to_item(keyword, row, str(row.get("source") or row.get("type") or row.get("cloud_type") or source_hint))
            if item:
                items.append(item)

        if isinstance(payload, dict):
            for key in ("merged_by_type", "merged", "merge"):
                merged = payload.get(key)
                if not isinstance(merged, dict):
                    continue
                for source_hint, rows in merged.items():
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        add_row(row, str(source_hint))

            for key in ("results", "items", "list"):
                rows = payload.get(key)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    add_row(row)
        elif isinstance(payload, list):
            for row in payload:
                add_row(row)

        # 去重，保留排序靠前的资源。
        return self._merge_items(items)

    def _row_to_item(self, keyword: str, row: dict[str, Any], source_hint: str = "") -> dict[str, Any] | None:
        url = row.get("url") or row.get("link") or row.get("share_url") or row.get("shareUrl")
        if not url:
            return None
        title = row.get("note") or row.get("content") or row.get("title") or row.get("work_title") or row.get("name") or keyword
        password = row.get("password") or row.get("pwd") or row.get("passcode") or row.get("code") or ""
        link_info = detect_link(url, self.routes, password=password)
        return {
            "title": title,
            "keyword": keyword,
            "url": link_info.url,
            "password": link_info.password,
            "source": "pansou",
            "source_hint": source_hint,
            "source_type": link_info.source_type,
            "supported": link_info.supported,
            "route": link_info.route,
            "reason": link_info.reason,
            "datetime": row.get("datetime") or row.get("created_at") or row.get("time") or "",
            "size": row.get("size"),
            "size_text": row.get("size_text") or row.get("sizeText") or "",
            "raw_data": row,
        }

    @staticmethod
    def _merge_items(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in groups:
            for item in group or []:
                key = str(item.get("url") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
        return deduped


def _normalize_cloud_types(value: Any, default: list[str]) -> list[str]:
    allowed = {"quark", "tianyi", "mobile", "magnet"}
    if isinstance(value, str):
        candidates = [item.strip().lower() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        candidates = [str(item).strip().lower() for item in value]
    else:
        candidates = []
    result = []
    for item in candidates:
        if item in allowed and item not in result:
            result.append(item)
    return result or list(default)


def _normalize_choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "是", "启用"}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _short_text(value: Any, limit: int = 40) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _post_json_no_env_proxy(url: str, **kwargs: Any) -> requests.Response:
    """调用内网/自建 PanSou 时不继承系统代理，避免被 127.0.0.1:7890 等代理劫持超时。"""

    with requests.Session() as session:
        session.trust_env = False
        return session.post(url, **kwargs)


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = str(value or "").replace("|", ",").replace("\n", ",").split(",")
    result: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _response_item_count(data: Any) -> int:
    payload = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return 0
    count = 0
    for key in ("results", "items", "list"):
        rows = payload.get(key)
        if isinstance(rows, list):
            count += len(rows)
    for key in ("merged_by_type", "merged", "merge"):
        merged = payload.get(key)
        if not isinstance(merged, dict):
            continue
        for rows in merged.values():
            if isinstance(rows, list):
                count += len(rows)
    return count
