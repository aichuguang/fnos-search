from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict
from typing import Any


class PublicTmdbService:
    """TMDB lookup and short-lived, signed candidate snapshots for public flow."""

    TOKEN_VERSION = 1
    TOKEN_TTL_SECONDS = 24 * 60 * 60
    SEARCH_CACHE_TTL_SECONDS = 30 * 60
    SEARCH_CACHE_MAX_ENTRIES = 128

    def __init__(
        self,
        *,
        client: Any,
        secret_key: str,
        now: Any = time.time,
        cache_ttl_seconds: int = SEARCH_CACHE_TTL_SECONDS,
        cache_max_entries: int = SEARCH_CACHE_MAX_ENTRIES,
    ) -> None:
        self.client = client
        self.secret_key = str(secret_key or "").encode("utf-8")
        self.now = now
        self._cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self._cache_max_entries = max(0, int(cache_max_entries))
        self._search_cache: "OrderedDict[str, tuple[float, list[Any]]]" = OrderedDict()
        self._search_cache_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(getattr(self._client(), "configured", False))

    def search(self, query: str) -> dict[str, Any]:
        normalized = str(query or "").strip()
        if not normalized:
            return {"status": "empty", "items": [], "fallback_query": ""}
        if not self.configured:
            return {"status": "not_configured", "items": [], "fallback_query": normalized}
        try:
            rows = self._cached_search(normalized)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "items": [],
                "fallback_query": normalized,
                "message": str(exc)[:300] or "TMDB 查询失败",
            }
        items: list[dict[str, Any]] = []
        for row in rows or []:
            candidate = self._candidate(row)
            if not candidate:
                continue
            candidate["selection_token"] = self.issue_token(candidate, query=normalized)
            items.append(candidate)
        return {"status": "ready" if items else "empty", "items": items, "fallback_query": normalized}

    def _cached_search(self, query: str) -> list[Any]:
        """TMDB 关键词识别结果缓存：同一关键词短时间内直接复用原始行。

        只缓存非空结果，避免把瞬时网络故障缓存成“无匹配”；
        selection_token 每次重新签发，不受缓存影响。
        """
        if self._cache_ttl_seconds <= 0 or self._cache_max_entries <= 0:
            return self._client().search(query, "auto")
        now = float(self.now())
        with self._search_cache_lock:
            hit = self._search_cache.get(query)
            if hit is not None:
                if hit[0] + self._cache_ttl_seconds > now:
                    self._search_cache.move_to_end(query)
                    return hit[1]
                del self._search_cache[query]
        rows = self._client().search(query, "auto")
        if rows:
            with self._search_cache_lock:
                self._search_cache[query] = (now, rows)
                while len(self._search_cache) > self._cache_max_entries:
                    self._search_cache.popitem(last=False)
        return rows

    def issue_token(self, candidate: dict[str, Any], *, query: str = "") -> str:
        issued_at = int(self.now())
        payload = {
            "v": self.TOKEN_VERSION,
            "iat": issued_at,
            "exp": issued_at + self.TOKEN_TTL_SECONDS,
            "query": str(query or "").strip()[:80],
            "candidate": self._candidate(candidate),
        }
        body = self._encode(payload)
        return f"{body}.{self._sign(body)}"

    def resolve_token(self, token: Any) -> dict[str, Any] | None:
        value = str(token or "").strip()
        if not value or len(value) > 8192 or "." not in value:
            return None
        body, signature = value.rsplit(".", 1)
        try:
            expected_signature = self._sign(body)
        except UnicodeEncodeError:
            return None
        if not hmac.compare_digest(expected_signature, signature):
            return None
        try:
            payload = json.loads(self._decode(body))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeError, binascii.Error):
            return None
        if not isinstance(payload, dict) or int(payload.get("v") or 0) != self.TOKEN_VERSION:
            return None
        try:
            if int(payload.get("exp") or 0) < int(self.now()):
                return None
        except (TypeError, ValueError):
            return None
        candidate = self._candidate(payload.get("candidate"))
        if not candidate:
            return None
        return {
            **candidate,
            "original_query": str(payload.get("query") or "").strip()[:80],
        }

    @staticmethod
    def _candidate(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        try:
            tmdb_id = int(value.get("tmdb_id") or value.get("id") or 0)
        except (TypeError, ValueError):
            tmdb_id = 0
        media_type = str(value.get("media_type") or "").strip().lower()
        title = str(value.get("title") or "").strip()[:300]
        if tmdb_id <= 0 or media_type not in {"movie", "tv"} or not title:
            return {}
        return {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": title,
            "original_title": str(value.get("original_title") or "").strip()[:300],
            "year": str(value.get("year") or "").strip()[:4],
            "overview": str(value.get("overview") or "").strip()[:1000],
            "poster_url": str(value.get("poster_url") or value.get("poster_path") or "").strip()[:1000],
            "vote_average": value.get("vote_average"),
        }

    def _client(self) -> Any:
        return self.client() if callable(self.client) else self.client

    def _sign(self, body: str) -> str:
        return hmac.new(self.secret_key, body.encode("ascii"), hashlib.sha256).hexdigest()

    @staticmethod
    def _encode(value: dict[str, Any]) -> str:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(value: str) -> str:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
