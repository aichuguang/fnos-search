from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

PROXY_DEFAULT_PORTS = {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.ProxyError):
        return f"代理错误：{str(exc)[:200]}"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "连接超时：出网到 TMDB 被屏蔽或代理不可达"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "读取超时：能连上但响应被掐断（可能是网络干扰）"
    if isinstance(exc, requests.exceptions.SSLError):
        return f"TLS 握手失败：{str(exc)[:200]}"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return f"连接失败：{str(exc)[:200]}"
    return str(exc) or exc.__class__.__name__


def _mask_proxy(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.hostname or not parsed.username:
        return url
    host = parsed.hostname if ":" not in parsed.hostname else f"[{parsed.hostname}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://***:***@{host}{port}"


class TmdbClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.token = str(config.get("token") or "").strip()
        self.language = str(config.get("language") or "zh-CN").strip() or "zh-CN"
        self.base_url = "https://api.themoviedb.org/3"
        self.use_env_proxy = _as_bool(config.get("use_env_proxy"), True)
        self.proxy_enabled = _as_bool(config.get("proxy_enabled"), False)
        self.proxy_url = str(config.get("proxy_url") or "").strip()
        self.session = requests.Session()
        self._configure_proxy()

    def _configure_proxy(self) -> None:
        if not self.proxy_enabled:
            self.session.trust_env = self.use_env_proxy
            return
        if not self.proxy_url:
            raise ValueError("已启用 TMDB 独立代理，但未填写代理地址")
        parsed = urlparse(self.proxy_url)
        scheme = parsed.scheme.lower()
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise ValueError("TMDB 代理端口必须是 1-65535 之间的数字") from exc
        if scheme not in PROXY_DEFAULT_PORTS or not parsed.hostname:
            raise ValueError("TMDB 代理仅支持 http、https、socks5 或 socks5h")
        if parsed_port is not None and not 1 <= parsed_port <= 65535:
            raise ValueError("TMDB 代理端口必须是 1-65535 之间的数字")
        self.session.trust_env = False
        self.session.proxies.update({"http": self.proxy_url, "https": self.proxy_url})

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def test(self) -> dict[str, Any]:
        if not self.configured:
            return {"success": False, "message": "TMDB Token 未配置"}
        diagnostics: dict[str, Any] = {
            "proxy_enabled": self.proxy_enabled,
            "use_env_proxy": self.use_env_proxy,
            "proxy_url": _mask_proxy(self.proxy_url) if self.proxy_enabled else "",
        }
        url = f"{self.base_url}/authentication"
        query = {"language": self.language}
        try:
            response = self.session.get(
                url,
                params=query,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            message = _describe_error(exc)
            logger.warning("TMDB 连接失败：%s", message)
            return {"success": False, "message": f"TMDB 连接失败：{message}", "raw": {}, "diagnostics": diagnostics}
        if response.status_code >= 400:
            return {
                "success": False,
                "message": f"TMDB 请求失败：HTTP {response.status_code}（Token 可能已失效）",
                "raw": {"status_code": response.status_code, "body": response.text[:300]},
                "diagnostics": diagnostics,
            }
        data = response.json()
        return {
            "success": bool(data),
            "message": "TMDB 连接正常" if data else "TMDB 连接失败",
            "raw": data or {},
            "diagnostics": diagnostics,
        }

    def search(self, query: str, media_type: str = "auto") -> list[dict[str, Any]]:
        if not self.configured or not str(query or "").strip():
            return []
        path = "/search/multi" if media_type not in {"tv", "movie"} else f"/search/{media_type}"
        data = self._request(path, {"query": query, "include_adult": "false"})
        rows = data.get("results") if isinstance(data, dict) else []
        result: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            item_type = row.get("media_type") if media_type == "auto" else media_type
            if item_type not in {"tv", "movie"}:
                continue
            title = row.get("name") if item_type == "tv" else row.get("title")
            title = title or row.get("title") or row.get("name") or ""
            date = row.get("first_air_date") if item_type == "tv" else row.get("release_date")
            item = {
                "id": row.get("id"),
                "media_type": item_type,
                "title": title,
                "original_title": row.get("original_name") or row.get("original_title") or "",
                "year": str(date or "")[:4],
                "overview": row.get("overview") or "",
                "poster_path": f"https://image.tmdb.org/t/p/w500{row.get('poster_path')}" if row.get("poster_path") else "",
                "vote_average": row.get("vote_average"),
                "raw": row,
            }
            if item["title"]:
                result.append(item)
        return result[:12]

    def details(self, tmdb_id: int, media_type: str) -> dict[str, Any] | None:
        if not self.configured or media_type not in {"tv", "movie"}:
            return None
        data = self._request(f"/{media_type}/{int(tmdb_id)}")
        if not data:
            return None
        title = data.get("name") if media_type == "tv" else data.get("title")
        date = data.get("first_air_date") if media_type == "tv" else data.get("release_date")
        return {
            "id": data.get("id"),
            "media_type": media_type,
            "title": title or "",
            "year": str(date or "")[:4],
            "overview": data.get("overview") or "",
            "poster_path": f"https://image.tmdb.org/t/p/w500{data.get('poster_path')}" if data.get("poster_path") else "",
            "seasons": data.get("seasons") or [],
            "next_episode_to_air": data.get("next_episode_to_air") if isinstance(data.get("next_episode_to_air"), dict) else None,
            "last_episode_to_air": data.get("last_episode_to_air") if isinstance(data.get("last_episode_to_air"), dict) else None,
            "status": data.get("status") or "",
            "raw": data,
        }

    def season_episodes(self, tmdb_id: int, season: int) -> list[dict[str, Any]]:
        if not self.configured:
            return []
        data = self._request(f"/tv/{int(tmdb_id)}/season/{int(season)}")
        return [
            {
                "id": row.get("id"),
                "season": row.get("season_number"),
                "episode": row.get("episode_number"),
                "title": row.get("name") or "",
                "air_date": row.get("air_date") or "",
                "raw": row,
            }
            for row in (data.get("episodes") or [])
            if isinstance(row, dict)
        ] if isinstance(data, dict) else []

    def _request(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        query = {"language": self.language, **(params or {})}
        try:
            response = self.session.get(url, params=query, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"}, timeout=20)
            if response.status_code >= 400:
                return {}
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


def score_tmdb_result(expected_title: str, expected_year: str, category_key: str, result: dict[str, Any], has_episodes: bool) -> float:
    score = 40.0
    normalized_expected = _normalize(expected_title)
    normalized_title = _normalize(result.get("title") or "")
    normalized_original = _normalize(result.get("original_title") or "")
    if normalized_expected and (normalized_title == normalized_expected or normalized_original == normalized_expected):
        score += 40
    elif normalized_expected and (
        normalized_expected in normalized_title
        or normalized_title in normalized_expected
        or (normalized_original and (normalized_expected in normalized_original or normalized_original in normalized_expected))
    ):
        score += 18
    expected_year_text = str(expected_year or "").strip()
    result_year_text = str(result.get("year") or "").strip()
    if expected_year_text and result_year_text:
        if result_year_text == expected_year_text:
            score += 25
        elif category_key == "movie":
            # 电影文件名里带明确年份时，年份不符的同名英文片不能高分压过正确年份结果。
            score -= 35
    elif expected_year_text and category_key == "movie":
        score -= 8
    if category_key == "movie" and result.get("media_type") == "movie":
        score += 12
    if category_key in {"tv", "anime", "variety"} and result.get("media_type") == "tv":
        score += 12
    if has_episodes and result.get("media_type") == "tv":
        score += 8
    if expected_year_text and result_year_text and result_year_text != expected_year_text and category_key == "movie":
        score = min(score, 64)
    return max(0, min(100, score))


def _normalize(value: Any) -> str:
    import re

    return re.sub(r"[\s._\-()[\]【】（）]+", "", str(value or "").lower())
