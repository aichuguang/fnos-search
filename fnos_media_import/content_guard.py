from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .organizer.ai import AiCalibrator


BT_SOURCE_TYPES = {"magnet", "torrent", "bt"}
DEFAULT_KEYWORD_FILE = "data/sensitive_words.txt"
AI_UNCONFIGURED_ALLOW_MESSAGE = "AI 未配置，词库未命中后允许 BT/磁链自动入库"
AI_FAILURE_ALLOW_MESSAGE_TEMPLATE = "{label}，AI 审核失败，词库未命中后允许 BT/磁链自动入库"


def evaluate_submission_content_risk(
    *,
    config: dict[str, Any] | None = None,
    title: str = "",
    source_type: str = "",
    source_url: str = "",
    category: str = "",
    note: str = "",
    files: list[dict[str, Any]] | None = None,
    ignore_files: list[str] | None = None,
    parse_error: str = "",
) -> dict[str, Any]:
    """Submit-time content review.

    搜索、详情预览阶段不调用这里；只有真正提交入库时才审。
    策略：
    - 所有来源先过词库；网盘类只过词库，未命中直接放行。
    - BT/磁链额外要求文件可解析，并调用现有 AI 配置做安全分。
    - AI 安全分高自动通过，低分或无法判断进入后台审核。
    """

    guard = ContentGuard(config or {})
    return guard.evaluate(
        title=title,
        source_type=source_type,
        source_url=source_url,
        category=category,
        note=note,
        files=files,
        ignore_files=ignore_files,
        parse_error=parse_error,
    )


def evaluate_bt_content_risk(
    *,
    title: str = "",
    source_type: str = "",
    source_url: str = "",
    files: list[dict[str, Any]] | None = None,
    ignore_files: list[str] | None = None,
    parse_error: str = "",
) -> dict[str, Any]:
    """Backward-compatible wrapper used by older tests/imports."""

    return evaluate_submission_content_risk(
        config={},
        title=title,
        source_type=source_type,
        source_url=source_url,
        files=files,
        ignore_files=ignore_files,
        parse_error=parse_error,
    )


class ContentGuard:
    def __init__(self, raw_config: dict[str, Any]) -> None:
        self.raw_config = raw_config if isinstance(raw_config, dict) else {}
        self.config = self._section()
        self.enabled = _as_bool(self.config.get("enabled"), True)
        self.keyword_enabled = _as_bool(self.config.get("keyword_enabled"), True)
        self.bt_ai_enabled = _as_bool(self.config.get("bt_ai_enabled"), True)
        self.bt_ai_pass_score = _bounded_int(self.config.get("bt_ai_pass_score"), 75, 0, 100)
        self.bt_parse_failure_review = _as_bool(self.config.get("bt_parse_failure_review"), True)
        self.bt_ai_failure_review = _as_bool(self.config.get("bt_ai_failure_review"), True)
        self.bt_ai_retries = _bounded_int(self.config.get("bt_ai_retries"), 2, 0, 5)
        self.max_files_for_ai = _bounded_int(self.config.get("max_files_for_ai"), 80, 1, 300)
        self.keywords = _load_keyword_rules(self.config)
        self.ai = AiCalibrator(self.raw_config.get("ai", {}))

    def _section(self) -> dict[str, Any]:
        section = self.raw_config.get("content_review")
        if isinstance(section, dict):
            return section
        section = self.raw_config.get("risk_control")
        if isinstance(section, dict):
            return section
        return {}

    def evaluate(
        self,
        *,
        title: str = "",
        source_type: str = "",
        source_url: str = "",
        category: str = "",
        note: str = "",
        files: list[dict[str, Any]] | None = None,
        ignore_files: list[str] | None = None,
        parse_error: str = "",
    ) -> dict[str, Any]:
        source = str(source_type or "").strip().lower()
        if not self.enabled:
            return _allow_result(source, "内容审核未启用")

        file_rows, selected_rows, ignored = _selected_file_rows(files or [], ignore_files or [])
        texts = _texts_for_review(title=title, source_url=source_url, note=note, files=selected_rows or file_rows)
        keyword_hits = self._keyword_hits(texts)
        if keyword_hits:
            return _review_result(
                source=source,
                label=_primary_keyword_label(keyword_hits),
                reason="您的提交包含敏感资源，请等待人工审核",
                score=max(_bounded_int(item.get("score"), 100, 0, 100) for item in keyword_hits),
                safety_score=0,
                evidence=keyword_hits[:12],
                stage="keyword",
                assessed=_assessed(source, file_rows, selected_rows, ignored, parse_error),
            )

        if source not in BT_SOURCE_TYPES:
            return _allow_result(source, "词库未命中，网盘类资源允许自动入库", assessed=_assessed(source, file_rows, selected_rows, ignored, parse_error))

        if parse_error and self.bt_parse_failure_review:
            return _review_result(
                source=source,
                label="内容无法确认",
                reason="BT/磁链内容解析失败，等待人工审核",
                score=100,
                safety_score=0,
                evidence=[{"label": "内容无法确认", "where": "解析", "match": _clip(parse_error, 160)}],
                stage="parse",
                assessed=_assessed(source, file_rows, selected_rows, ignored, parse_error),
            )

        if not self.bt_ai_enabled:
            return _allow_result(source, "BT AI 审核未启用，词库未命中后允许自动入库", assessed=_assessed(source, file_rows, selected_rows, ignored, parse_error))

        if not self.ai.configured:
            return _allow_result(
                source,
                AI_UNCONFIGURED_ALLOW_MESSAGE,
                assessed=_assessed(source, file_rows, selected_rows, ignored, parse_error),
                ai_result={"skipped": True, "reason": "ai_unconfigured"},
            )

        evidence_payload = {
            "title": title,
            "category": category,
            "source_type": source,
            "note": note,
            "files": [_ai_file_item(item) for item in (selected_rows or file_rows)[: self.max_files_for_ai]],
            "instruction": "safety_score 越高越安全；只判断是否适合影视资源自助入库。",
        }
        try:
            ai_result = _ai_review(self.ai, evidence_payload, retries=self.bt_ai_retries)
        except Exception as exc:  # noqa: BLE001
            label, reason = _friendly_ai_failure(exc)
            failure_result = {
                "error": str(exc),
                "label": label,
                "fallback": "review" if self.bt_ai_failure_review else "allow",
            }
            if self.bt_ai_failure_review:
                return _review_result(
                    source=source,
                    label=label,
                    reason=reason,
                    score=100,
                    safety_score=0,
                    evidence=[
                        {
                            "label": label,
                            "where": "AI",
                            "match": "调用失败",
                            "text": _clip(exc, 160),
                        }
                    ],
                    stage="ai_failure",
                    assessed=_assessed(source, file_rows, selected_rows, ignored, parse_error),
                    ai_result=failure_result,
                )
            return _allow_result(
                source,
                AI_FAILURE_ALLOW_MESSAGE_TEMPLATE.format(label=label),
                assessed=_assessed(source, file_rows, selected_rows, ignored, parse_error),
                ai_result=failure_result,
            )

        safety_score = _bounded_int(ai_result.get("safety_score"), 0, 0, 100)
        verdict = str(ai_result.get("verdict") or "").strip().lower()
        if safety_score < self.bt_ai_pass_score or verdict in {"review", "block", "reject"}:
            return _review_result(
                source=source,
                label=str(ai_result.get("category") or "AI 低分").strip() or "AI 低分",
                reason=str(ai_result.get("reason") or "BT/磁链 AI 安全评分偏低，等待人工审核"),
                score=100 - safety_score,
                safety_score=safety_score,
                evidence=[{"label": "AI 安全评分", "where": "AI", "match": str(safety_score), "text": _clip(ai_result.get("reason"), 160)}],
                stage="ai",
                assessed=_assessed(source, file_rows, selected_rows, ignored, parse_error),
                ai_result=ai_result,
            )

        return _allow_result(
            source,
            str(ai_result.get("reason") or f"BT/磁链 AI 安全评分 {safety_score}，允许自动入库"),
            score=100 - safety_score,
            safety_score=safety_score,
            assessed=_assessed(source, file_rows, selected_rows, ignored, parse_error),
            ai_result=ai_result,
        )

    def _keyword_hits(self, texts: list[tuple[str, str]]) -> list[dict[str, Any]]:
        if not self.keyword_enabled:
            return []
        hits: list[dict[str, Any]] = []
        for where, text in texts:
            normalized = _normalize_text(text)
            if not normalized:
                continue
            for rule in self.keywords:
                if rule.matches(normalized, text):
                    hits.append({"label": rule.label, "where": where, "match": rule.pattern, "text": _clip(text, 160), "score": rule.score})
                    break
            for code in _adult_code_hits(text):
                hits.append({"label": "疑似成人番号", "where": where, "match": code, "text": _clip(text, 160), "score": 90})
                break
        return _dedupe_evidence(hits)


class KeywordRule:
    def __init__(self, pattern: str, label: str = "敏感词", score: int = 100, regex: bool = False) -> None:
        self.pattern = str(pattern or "").strip()
        self.label = str(label or "敏感词").strip()
        self.score = _bounded_int(score, 100, 0, 100)
        self.regex = bool(regex)
        self._compiled = re.compile(self.pattern, re.I) if self.regex and self.pattern else None
        self._normalized = _normalize_text(self.pattern)

    def matches(self, normalized_text: str, raw_text: str) -> bool:
        if not self.pattern:
            return False
        if self._compiled:
            return bool(self._compiled.search(raw_text))
        return bool(self._normalized and self._normalized in normalized_text)


def _ai_review(ai: AiCalibrator, evidence: dict[str, Any], *, retries: int = 2) -> dict[str, Any]:
    system = (
        "你是影视资源自助入库安全审核器。只输出 JSON，不要 Markdown。"
        "字段必须包含：safety_score(0-100，越高越安全), verdict(allow/review/block), "
        "category(normal/porn/gambling/political/violence/unknown), reason。"
        "只根据资源标题、备注、解析文件名判断内容风险。"
        "正常电影、电视剧、动漫、综艺，即使包含 4K、字幕、WEB-DL、BDRip、BT、磁链等词，也应给 80-100。"
        "成人色情、成人视频、成人视频番号、赌博、明显政治敏感、暴恐等给 0-30。"
        "看不出是否正常影视或信息不足给 40-69 并 verdict=review。"
        "不要臆测；不确定就 review。"
    )
    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            parsed = ai.ask_json(system, evidence)
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= retries or not _retryable_ai_error(exc):
                raise
            time.sleep(min(3.0, 0.8 * (2 ** attempt)))
    if last_error:
        raise last_error
    return {}


def _retryable_ai_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in (" 429", "rate limit", "too many requests", "timeout", "temporarily", "503", "502", "504"))


def _friendly_ai_failure(exc: Exception) -> tuple[str, str]:
    text = str(exc or "")
    lower = text.lower()
    if "429" in lower or "rate limit" in lower or "too many requests" in lower:
        return "AI 审核限流", "BT/磁链 AI 审核服务暂时限流，已转入人工审核"
    if "timeout" in lower or "timed out" in lower:
        return "AI 审核超时", "BT/磁链 AI 审核超时，已转入人工审核"
    return "AI 审核失败", "BT/磁链 AI 审核失败，等待人工审核"


def _load_keyword_rules(config: dict[str, Any]) -> list[KeywordRule]:
    rules: list[KeywordRule] = []
    min_len = _bounded_int(config.get("min_keyword_length"), 2, 1, 20)
    for item in config.get("keywords") or []:
        rule = _keyword_rule_from_any(item)
        if rule and (rule.regex or len(rule.pattern) >= min_len):
            rules.append(rule)
    file_path = str(config.get("keyword_file") or DEFAULT_KEYWORD_FILE).strip()
    if file_path:
        path = Path(file_path)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                rule = _keyword_rule_from_line(line)
                if rule and (rule.regex or len(rule.pattern) >= min_len):
                    rules.append(rule)
    rules.extend(_default_keyword_rules())
    return _dedupe_rules(rules)


def _keyword_rule_from_any(item: Any) -> KeywordRule | None:
    if isinstance(item, dict):
        pattern = str(item.get("word") or item.get("keyword") or item.get("pattern") or "").strip()
        if not pattern:
            return None
        return KeywordRule(pattern, label=str(item.get("label") or item.get("category") or "敏感词"), score=_bounded_int(item.get("score"), 100, 0, 100), regex=_as_bool(item.get("regex"), False))
    return _keyword_rule_from_line(str(item or ""))


def _keyword_rule_from_line(line: str) -> KeywordRule | None:
    text = str(line or "").strip()
    if not text or text.startswith("#") or text.startswith("//"):
        return None
    text = text.strip(",，、;；")
    if not text:
        return None
    label = "敏感词"
    score = 100
    regex = False
    if text.startswith("re:"):
        regex = True
        text = text[3:].strip()
    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        text = parts[0]
        if len(parts) >= 2 and parts[1]:
            label = parts[1]
        if len(parts) >= 3:
            score = _bounded_int(parts[2], 100, 0, 100)
    elif ":" in text:
        maybe_label, maybe_word = [part.strip() for part in text.split(":", 1)]
        if maybe_label and maybe_word:
            label, text = maybe_label, maybe_word
    return KeywordRule(text, label=label, score=score, regex=regex) if text else None


def _default_keyword_rules() -> list[KeywordRule]:
    adult_words = [
        "成人",
        "18禁",
        "成人视频",
        "成人片",
        "成人资源",
        "成人电影",
        "成人影片",
        "情色",
        "色情",
        "裸聊",
        "露出",
        "调教",
        "自慰",
        "口交",
        "性交",
        "无码",
        "無碼",
        "有码",
        "番号",
        "女优",
        "骑兵",
        "步兵",
        "一本道",
        "东京热",
        "TokyoHot",
        "HEYZO",
        "Caribbeancom",
        "Caribbean",
        "1pondo",
        "Pacopacomama",
        "S-Cute",
        "麻豆传媒",
        "天美传媒",
        "蜜桃传媒",
        "精东影业",
        "星空传媒",
        "皇家华人",
        "果冻传媒",
        "91制片厂",
        "经典三级",
        "韩国伦理",
        "日本伦理",
        "伦理片",
        "porn",
        "xvideos",
        "xhamster",
        "xnxx",
        "javbus",
        "javdb",
        "missav",
        "hanime",
        "18comic",
    ]
    return [KeywordRule(word, label="成人敏感词", score=100) for word in adult_words]


def _selected_file_rows(files: list[dict[str, Any]], ignore_files: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    ignored = {str(item or "").strip() for item in ignore_files if str(item or "").strip()}
    file_rows = [item for item in files if isinstance(item, dict) and not item.get("directory")]
    selected = [
        item
        for item in file_rows
        if str(item.get("id") or item.get("identity") or item.get("path") or item.get("name") or "").strip() not in ignored
    ]
    return file_rows, selected, ignored


def _texts_for_review(*, title: str, source_url: str, note: str, files: list[dict[str, Any]]) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for label, value in (("标题", title), ("备注", note), ("链接", source_url)):
        text = str(value or "").strip()
        if text:
            texts.append((label, text))
    for item in files[:200]:
        value = str(item.get("path") or item.get("name") or item.get("identity") or "").strip()
        if value:
            texts.append(("文件名", value))
    return texts


def _ai_file_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(item.get("name") or "").strip(),
        "path": str(item.get("path") or "").strip(),
        "size": item.get("size") or 0,
        "size_text": str(item.get("size_text") or "").strip(),
        "media_type": str(item.get("media_type") or "").strip(),
        "reason": str(item.get("reason") or "").strip(),
    }


def _allow_result(source: str, reason: str = "", *, score: int = 0, safety_score: int = 100, assessed: dict[str, Any] | None = None, ai_result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "review_required": False,
        "risk": "safe",
        "score": score,
        "safety_score": safety_score,
        "label": "",
        "reason": reason,
        "public_message": "",
        "admin_message": "",
        "evidence": [],
        "source_type": source,
        "assessed": assessed or {},
        "ai_result": ai_result or {},
    }


def _review_result(
    *,
    source: str,
    label: str,
    reason: str,
    score: int,
    safety_score: int,
    evidence: list[dict[str, Any]],
    stage: str,
    assessed: dict[str, Any],
    ai_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    label = str(label or "内容风控").strip()
    reason = str(reason or f"疑似{label}，等待人工审核").strip()
    return {
        "review_required": True,
        "risk": "review",
        "score": score,
        "safety_score": safety_score,
        "label": label,
        "reason": reason,
        "public_message": reason,
        "admin_message": _admin_message(label, evidence, score, safety_score, stage),
        "evidence": _dedupe_evidence(evidence)[:12],
        "source_type": source,
        "stage": stage,
        "assessed": assessed,
        "ai_result": ai_result or {},
    }


def _assessed(source: str, file_rows: list[dict[str, Any]], selected_rows: list[dict[str, Any]], ignored: set[str], parse_error: str) -> dict[str, Any]:
    return {
        "source_type": source,
        "file_count": len(file_rows),
        "selected_file_count": len(selected_rows),
        "ignored_count": len(ignored),
        "parse_error": _clip(parse_error, 200),
    }


def _adult_code_hits(text: str) -> list[str]:
    value = str(text or "")
    patterns = [
        r"(?<![A-Za-z0-9])(?:FC2(?:PPV)?|HEYZO|CARIB|1PON|TOKYO[-_ ]?HOT|S-CUTE)[-_ ]?\d{3,8}(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9])(?:ABP|IPX|SSIS|SSNI|JUQ|MIDV|MIAA|MIDE|MIFD|DASS|DLDSS|FSDSS|JUL|JUX|SNIS|PPPD|PRED|ADN|WANZ|SDDE|HND|CJOD|GVG|NHDTA|NTRD|MEYD|MIMK|MUKD|STARS|VEC|EBOD|JAV|AVOP)[-_ ]?\d{2,5}(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9])\d{6}[-_ ]\d{2,4}(?![A-Za-z0-9])",
    ]
    hits: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            hit = match.group(0)
            if hit not in hits:
                hits.append(hit)
    return hits


def _primary_keyword_label(evidence: list[dict[str, Any]]) -> str:
    labels = [str(item.get("label") or "") for item in evidence]
    for label in ("成人敏感词", "疑似成人番号", "政治敏感词", "赌博敏感词", "敏感词"):
        if label in labels:
            return label
    return labels[0] if labels else "敏感词"


def _admin_message(label: str, evidence: list[dict[str, Any]], score: int, safety_score: int, stage: str) -> str:
    sample = "；".join(f"{item.get('where')}命中{item.get('label')}({item.get('match')})" for item in evidence[:4])
    suffix = f"。{sample}" if sample else ""
    return f"内容风控进入人工审核：{label}，阶段 {stage}，风险分 {score}，安全分 {safety_score}{suffix}"


def _dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (str(item.get("label") or ""), str(item.get("match") or "").lower(), str(item.get("where") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _dedupe_rules(rules: list[KeywordRule]) -> list[KeywordRule]:
    result: list[KeywordRule] = []
    seen: set[tuple[str, str, bool]] = set()
    for rule in rules:
        key = (rule.pattern.lower(), rule.label, rule.regex)
        if key in seen:
            continue
        seen.add(key)
        result.append(rule)
    return result


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").casefold())


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clip(value: Any, limit: int = 80) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
