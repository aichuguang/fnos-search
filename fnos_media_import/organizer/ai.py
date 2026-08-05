from __future__ import annotations

import json
import re
from typing import Any

import requests


class AiApiError(RuntimeError):
    def __init__(self, status_code: int, body: str, path: str, url: str) -> None:
        self.status_code = int(status_code)
        self.body = str(body or "")[:500]
        self.path = path
        self.url = url
        super().__init__(f"AI HTTP {self.status_code}: {self.body}")


class AiCalibrator:
    def __init__(self, config: dict[str, Any]) -> None:
        self.enabled = bool(config.get("enabled", False))
        self.api_style = str(config.get("api_style") or "chat").strip().lower()
        self.base_url = str(config.get("base_url") or "https://api.openai.com/v1").strip().rstrip("/")
        self.api_key = str(config.get("api_key") or "").strip()
        self.model = str(config.get("model") or "gpt-4.1-mini").strip()
        self.timeout = int(config.get("timeout") or 45)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.base_url and self.api_key and self.model)

    def test(self) -> dict[str, Any]:
        if not self.configured:
            return {"success": False, "message": "AI 未启用或配置不完整"}
        try:
            parsed = self.calibrate({"title_candidates": ["测试"], "files": []})
            return {
                "success": True,
                "message": f"AI 连接正常（{parsed.get('_ai_api_style') or self.api_style}）",
                "parsed": parsed,
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": str(exc)}

    def calibrate(self, evidence: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            return {}
        prompt = {
            "role": "system",
            "content": (
                "你是影视资源命名校准器。只输出 JSON，不要 Markdown。"
                "字段：media_type（只能用 movie/tv/unknown）, title, year, season, "
                "episode_rule, tmdb_queries, confidence, requires_review, reasons。"
                "注意：title 必须是 TMDB/影视标准基础标题，不要带第几季、Sxx、4K、1080P、DDP、AAC、HIFI、字幕、更新集数、广告词。"
                "season 单独输出数字；TMDB 查询也只给基础标题。"
            ),
        }
        return self.ask_json(prompt["content"], evidence)

    def ask_json(self, system_prompt: str, evidence: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            return {}
        prompt = {"role": "system", "content": str(system_prompt or "只输出 JSON，不要 Markdown。")}
        user = {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)[:12000]}
        errors: list[AiApiError] = []
        for style in self._style_order():
            try:
                if style == "responses":
                    text = self._call_responses(prompt["content"], user["content"])
                else:
                    text = self._call_chat(prompt, user)
                parsed = _extract_json(text)
                parsed["_ai_api_style"] = style
                return parsed
            except AiApiError as exc:
                errors.append(exc)
                if not _should_try_alternate_endpoint(exc):
                    raise
        if errors:
            detail = "；".join(f"{item.path} -> HTTP {item.status_code}: {item.body}" for item in errors)
            raise RuntimeError(f"AI 接口测试失败，已尝试 {len(errors)} 个端点：{detail}")
        return {}

    def _call_chat(self, prompt: dict[str, Any], user: dict[str, Any]) -> str:
        payload = {"model": self.model, "messages": [prompt, user], "temperature": 0.1}
        response = self._post("/chat/completions", payload)
        return self._chat_text(response)

    def _call_responses(self, instructions: str, user_content: str) -> str:
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": user_content,
            "temperature": 0.1,
        }
        response = self._post("/responses", payload)
        return self._responses_text(response)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._url_for(path)
        response = self._request_json(url, payload)
        if response.status_code in {400, 422} and "temperature" in (response.text or "").lower():
            retry_payload = dict(payload)
            retry_payload.pop("temperature", None)
            response = self._request_json(url, retry_payload)
        if response.status_code >= 400:
            raise AiApiError(response.status_code, response.text or "", path, url)
        data = response.json()
        return data if isinstance(data, dict) else {}

    def _request_json(self, url: str, payload: dict[str, Any]) -> requests.Response:
        return requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            timeout=self.timeout,
        )

    def _url_for(self, path: str) -> str:
        base = self.base_url.rstrip("/")
        known_suffixes = ("/chat/completions", "/responses")
        for suffix in known_suffixes:
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return f"{base}{path}"

    def _style_order(self) -> list[str]:
        style = self.api_style if self.api_style in {"auto", "chat", "responses"} else "chat"
        if style == "responses":
            return ["responses", "chat"]
        if style == "auto":
            return ["responses", "chat"] if self.model.lower().startswith(("gpt-5", "o1", "o3", "o4")) else ["chat", "responses"]
        return ["chat", "responses"]

    @staticmethod
    def _chat_text(payload: dict[str, Any]) -> str:
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or choice.get("text") or ""
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    chunks.append(str(item.get("text") or item.get("content") or ""))
                else:
                    chunks.append(str(item))
            return "\n".join(part for part in chunks if part)
        return str(content or "")

    @staticmethod
    def _responses_text(payload: dict[str, Any]) -> str:
        if payload.get("output_text"):
            return str(payload.get("output_text") or "")
        chunks: list[str] = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("text"):
                    chunks.append(str(content.get("text")))
        return "\n".join(chunks)


def _extract_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _should_try_alternate_endpoint(exc: AiApiError) -> bool:
    body = exc.body.lower()
    if exc.status_code in {404, 405}:
        return True
    if exc.status_code in {400, 422} and any(
        marker in body
        for marker in (
            "model",
            "endpoint",
            "api",
            "unsupported",
            "not support",
            "not supported",
            "不支持",
            "所选模型",
        )
    ):
        return True
    return False
