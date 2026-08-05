from __future__ import annotations

from typing import Any

import requests


class HttpClient:
    def __init__(self, timeout: int = 20, verify_tls: bool = False, trust_env: bool = True):
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.session = requests.Session()
        self.session.trust_env = bool(trust_env)

    def get_json(self, url: str, **kwargs: Any) -> tuple[int, Any]:
        response = self.session.get(url, timeout=kwargs.pop("timeout", self.timeout), verify=self.verify_tls, **kwargs)
        return response.status_code, response.json()

    def post_json(self, url: str, payload: dict[str, Any], **kwargs: Any) -> tuple[int, Any]:
        headers = kwargs.pop("headers", {})
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        response = self.session.post(
            url,
            json=payload,
            headers=headers,
            timeout=kwargs.pop("timeout", self.timeout),
            verify=self.verify_tls,
            **kwargs,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text}
        return response.status_code, data
