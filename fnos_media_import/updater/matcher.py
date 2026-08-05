from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass
from typing import Any

from ..organizer.parser import parse_file_name, parse_season


@dataclass
class CandidateMatch:
    score: int
    decision: str
    reason: str
    season: int | None
    episode: int | None
    fingerprint: str


class UpdateMatcher:
    def match(
        self,
        subscription: dict[str, Any],
        candidate: dict[str, Any],
        *,
        existing_episodes: set[tuple[int | None, int]],
        target_episodes: set[tuple[int | None, int]],
    ) -> CandidateMatch:
        title = str(candidate.get("title") or "")
        parsed = parse_file_name(title, current_dir=str(candidate.get("parent_name") or ""), parent_dir=str(subscription.get("title") or ""))
        season = _first_season(
            _safe_season(candidate.get("season")),
            parsed.season,
            _safe_season(subscription.get("season")),
        )
        episode = _safe_int(candidate.get("episode")) or parsed.episode
        normalized_title = _normalize(subscription.get("title"))
        aliases = [_normalize(item) for item in (subscription.get("aliases") or []) if _normalize(item)]
        candidate_context = f"{title} {candidate.get('parent_name') or ''}"
        normalized_candidate = _normalize(candidate_context)
        include = [_normalize(item) for item in (subscription.get("include_keywords") or []) if _normalize(item)]
        exclude = [_normalize(item) for item in (subscription.get("exclude_keywords") or []) if _normalize(item)]
        score = 0
        reasons: list[str] = []
        if normalized_title and normalized_title in normalized_candidate:
            score += 45
            reasons.append("标题命中")
        elif any(alias and alias in normalized_candidate for alias in aliases):
            score += 38
            reasons.append("别名命中")
        else:
            token_hits = _token_hit_score(normalized_title, normalized_candidate)
            score += token_hits
            if token_hits:
                reasons.append("标题分词命中")
        expected_season = _safe_season(subscription.get("season"))
        if expected_season is not None and season == expected_season:
            score += 20
            reasons.append("季号命中")
        elif expected_season is None or season is None:
            score += 5
        else:
            score -= 30
            reasons.append("季号不匹配")
        episode_key = (season, episode) if episode else None
        target_hit = bool(episode and _episode_in_set(season, episode, target_episodes))
        existing_hit = bool(episode and _episode_in_set(season, episode, existing_episodes))
        if episode_key and target_episodes and target_hit:
            score += 35
            reasons.append("目标缺集命中")
        elif episode:
            score += 18
            reasons.append("集数明确")
        else:
            score -= 25
            reasons.append("未识别集数")
        if include and all(item in normalized_candidate for item in include):
            score += 10
            reasons.append("包含偏好命中")
        if any(item and item in normalized_candidate for item in exclude):
            score -= 50
            reasons.append("命中排除词")
        quality = _quality_score(title)
        score += quality
        if quality:
            reasons.append("清晰度加权")
        if candidate.get("source_type") == "cloud139":
            score += 12
        if candidate.get("source_type") in {"magnet", "torrent", "bt"}:
            score += 5
        score = max(0, min(100, score))
        min_score = int(subscription.get("min_score") or 75)
        source_type = str(candidate.get("source_type") or "").strip().lower()
        single_file_evidence = bool(candidate.get("file_level") or source_type in {"magnet", "torrent", "bt"})
        default_update = bool(candidate.get("default_update") or candidate.get("decision_hint") == "default_update")
        decision_hint = str(candidate.get("decision_hint") or "")
        if decision_hint == "error":
            decision = "review"
            reasons.append("候选来源异常，等待人工确认")
        elif default_update:
            decision = "review"
            reasons.append("未识别到具体新增文件，不自动追更")
        elif decision_hint in {"review", "requires_preview"}:
            decision = "review"
            reasons.append("候选需要人工确认")
        elif not target_episodes:
            decision = "review"
            reasons.append("未确定本轮目标集数，等待人工确认")
        elif episode_key and existing_hit:
            decision = "review"
            reasons.append("同集已入库，作为升级候选待确认")
        elif target_episodes and not single_file_evidence:
            decision = "review"
            reasons.append("未确认是单文件候选，不自动追更")
        elif score >= min_score and episode and (not target_episodes or target_hit):
            decision = "auto_import"
        else:
            decision = "review"
        fingerprint = candidate.get("fingerprint") or _fingerprint(candidate, season, episode)
        return CandidateMatch(score=score, decision=decision, reason="；".join(reasons) or "等待人工确认", season=season, episode=episode, fingerprint=fingerprint)


def _safe_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_season(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _first_season(*values: Any) -> int | None:
    for value in values:
        season = _safe_season(value)
        if season is not None:
            return season
    return None


def _episode_in_set(season: int | None, episode: int, episodes: set[tuple[int | None, int]]) -> bool:
    """目标季为空或候选季为空时，只按集号匹配；两边都有季号时必须一致。"""

    for target_season, target_episode in episodes:
        if target_episode != episode:
            continue
        if target_season is not None and season is not None and target_season != season:
            continue
        return True
    return False


def _normalize(value: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _token_hit_score(keyword: str, title: str) -> int:
    if not keyword or not title:
        return 0
    tokens = [item for item in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", keyword) if len(item) >= 2]
    if not tokens:
        return 0
    hits = sum(1 for item in tokens if item in title)
    return int(30 * hits / len(tokens))


def _quality_score(title: Any) -> int:
    text = str(title or "").lower()
    score = 0
    if "4k" in text or "2160" in text:
        score += 8
    if "1080" in text:
        score += 6
    if "hdr" in text or "杜比" in text:
        score += 3
    if "合集" in text or "全集" in text:
        score -= 10
    return score


def _fingerprint(candidate: dict[str, Any], season: int | None, episode: int | None) -> str:
    raw = "|".join(
        str(candidate.get(key) or "")
        for key in ("source_type", "url", "source_url", "file_id", "title", "size", "size_text")
    )
    season_key = "unknown" if season is None else f"{season:02d}"
    raw = f"{raw}|S{season_key}E{episode or 0}"
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def episode_from_text(value: Any, season_hint: int | None = None) -> tuple[int | None, int | None]:
    normalized_hint = _safe_season(season_hint)
    parent_dir = f"Season {normalized_hint:02d}" if normalized_hint is not None else ""
    parsed = parse_file_name(posixpath.basename(str(value or "")), parent_dir=parent_dir)
    season = _first_season(parsed.season, normalized_hint, parse_season(value))
    return season, parsed.episode
