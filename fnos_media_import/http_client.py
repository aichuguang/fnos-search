from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests


RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


class HttpJsonDecodeError(RuntimeError):
    """Raised when an endpoint declared as JSON returns an invalid body."""

    def __init__(self, method: str, url: str, status_code: int, response_text: str):
        self.method = str(method or "GET").upper()
        self.url = str(url or "")
        self.status_code = int(status_code or 0)
        self.response_text = str(response_text or "")
        super().__init__(
            f"{self.method} {self.url} returned invalid JSON (HTTP {self.status_code})"
        )


class HttpClient:
    def __init__(
        self,
        timeout: float = 20,
        verify_tls: bool = False,
        trust_env: bool = True,
        *,
        connect_timeout: float = 5,
        read_timeout: float | None = None,
        request_retries: int = 2,
        retry_delay_seconds: float = 0.2,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.read_timeout = max(
            0.1,
            float(read_timeout if read_timeout is not None else timeout),
        )
        self.connect_timeout = max(0.1, min(float(connect_timeout), self.read_timeout))
        self.timeout = self.read_timeout
        self.verify_tls = bool(verify_tls)
        self.request_retries = max(0, int(request_retries or 0))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds or 0))
        self.session = session or requests.Session()
        self.session.trust_env = bool(trust_env)
        self._sleep = sleep

    def get_json(self, url: str, **kwargs: Any) -> tuple[int, Any]:
        response = self._request("GET", url, retryable=True, **kwargs)
        return response.status_code, self._decode_json(response, "GET", url)

    def get_text(self, url: str, **kwargs: Any) -> tuple[int, str]:
        response = self._request("GET", url, retryable=True, **kwargs)
        return response.status_code, str(response.text or "")

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        read_only: bool = False,
        **kwargs: Any,
    ) -> tuple[int, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        response = self._request(
            "POST",
            url,
            retryable=bool(read_only),
            json=payload,
            headers=headers,
            **kwargs,
        )
        return response.status_code, self._decode_json(response, "POST", url)

    def close(self) -> None:
        self.session.close()

    def _request(
        self,
        method: str,
        url: str,
        *,
        retryable: bool,
        **kwargs: Any,
    ) -> requests.Response:
        timeout = self._normalize_timeout(kwargs.pop("timeout", None))
        verify = kwargs.pop("verify", self.verify_tls)
        configured_retries = kwargs.pop("retries", None)
        retries = (
            self.request_retries
            if configured_retries is None
            else max(0, int(configured_retries or 0))
        )
        if not retryable:
            retries = 0

        for attempt in range(retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=timeout,
                    verify=verify,
                    **kwargs,
                )
            except RETRYABLE_EXCEPTIONS:
                if attempt >= retries:
                    raise
                self._wait_before_retry(attempt)
                continue

            if response.status_code not in RETRYABLE_STATUS_CODES or attempt >= retries:
                return response
            response.close()
            self._wait_before_retry(attempt)

        raise RuntimeError("HTTP retry loop exited unexpectedly")

    def _normalize_timeout(self, override: Any) -> tuple[float, float]:
        if isinstance(override, (tuple, list)) and len(override) == 2:
            return max(0.1, float(override[0])), max(0.1, float(override[1]))
        if override is not None:
            read_timeout = max(0.1, float(override))
            return min(self.connect_timeout, read_timeout), read_timeout
        return self.connect_timeout, self.read_timeout

    def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_delay_seconds * (2**attempt)
        if delay > 0:
            self._sleep(delay)

    @staticmethod
    def _decode_json(response: requests.Response, method: str, url: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise HttpJsonDecodeError(
                method,
                url,
                int(response.status_code or 0),
                str(response.text or ""),
            ) from exc


__all__ = ["HttpClient", "HttpJsonDecodeError"]
