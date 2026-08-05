from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from ..media_path_rules import clean_media_title, format_title_year, parse_date_text, sanitize_resource_dir_name, split_title_year
from .openlist_client import basename, dirname, join_path


EPISODIC_CATEGORIES = {"tv", "anime", "variety"}
TECH_VERSION_PATTERN = re.compile(
    r"(?i)(2160p|1080p|720p|4k|8k|hdr10?|dv|dolby\s*vision|sdr|60fps|120fps|高码率|臻彩|remux|bluray|web-?dl|h\.?265|hevc|x265|h\.?264|x264)"
)
NOISE_TOKEN_PATTERN = re.compile(
    r"(?ix)"
    r"\b(?:4k|8k|2160p|1080p|720p|480p|uhd|hdr10?|dv|sdr|hq|imax|web[- ]?dl|webrip|bluray|remux|hevc|avc|"
    r"h\.?265|x265|h\.?264|x264|ddp\s*\d(?:\.\d)?|aac\s*\d(?:\.\d)?|truehd|dts(?:-?hd)?|atmos|hifi|60fps|120fps)\b"
)
CN_NOISE_PATTERN = re.compile(
    r"(?:臻彩|高码率|内嵌|内封|外挂|简中|繁中|中字|中文字幕|字幕|国语|国配|粤语|英语|双语|合集|全集|完结|更新至|更\s*\d+\s*[集话話回]|[0-9.]+\s*(?:GB|G|MB|M)\s*[集话話回]?)",
    re.I,
)
CHINESE_NUMERAL_MAP = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
CHINESE_NUMBER_CHARS = "零〇一二两三四五六七八九十百千万"
EPISODE_MARKERS = "集话話回级"


@dataclass
class ParsedFile:
    title: str = ""
    year: str = ""
    season: int | None = None
    episode: int | None = None
    media_type: str = "unknown"
    confidence: float = 0
    reasons: list[str] = field(default_factory=list)
    version: str = ""


def parse_file_name(name: str, *, current_dir: str = "", parent_dir: str = "", siblings: list[str] | None = None) -> ParsedFile:
    stem = posixpath.splitext(str(name or "").replace("\\", "/"))[0]
    parent_season = parse_season(parent_dir)
    season = parent_season if parent_season is not None else parse_season(current_dir)
    parsed_season, episode, reason = parse_episode(stem, season)
    title = choose_title(stem, current_dir=current_dir, parent_dir=parent_dir, siblings=siblings or [], has_episode=episode is not None)
    title, year = split_title_year(title or "")
    if not year:
        for candidate in (current_dir, parent_dir, stem):
            _, year = split_title_year(candidate)
            if year:
                break
    reasons = []
    if reason:
        reasons.append(reason)
    if title:
        reasons.append(f"标题候选：{title}")
    if year:
        reasons.append(f"年份候选：{year}")
    confidence = 25
    if title:
        confidence += 25
    if episode is not None:
        confidence += 30
    if parsed_season is not None:
        confidence += 10
    if year:
        confidence += 8
    media_type = "tv" if episode is not None else ("movie" if year else "unknown")
    return ParsedFile(
        title=title,
        year=year,
        season=parsed_season,
        episode=episode,
        media_type=media_type,
        confidence=float(max(0, min(100, confidence))),
        reasons=reasons,
        version=extract_version_tags(name),
    )


def parse_season(value: Any) -> int | None:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{1,2}", text):
        return int(text)
    patterns = [
        r"(?i)(?:^|[\s._\-[(【])s(?:eason)?\s*0*(\d{1,2})(?=$|[\s._\-)\]】])",
        r"(?i)(?:^|[\s._\-[(【])season\s*0*(\d{1,2})(?=$|[\s._\-)\]】])",
        rf"第\s*(\d{{1,2}}|[{CHINESE_NUMBER_CHARS}]+)\s*季",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _season_number(match.group(1))
    return None


def parse_episode(value: Any, season_hint: int | None = None) -> tuple[int | None, int | None, str]:
    text = str(value or "")
    cn_number = rf"(?:\d{{1,4}}|[{CHINESE_NUMBER_CHARS}]{{1,12}})"
    patterns = [
        (r"(?i)(?:^|[^0-9A-Za-z])S\s*0*(\d{1,2})\s*E\s*0*(\d{1,4})(?:v\d+)?(?=$|[^0-9A-Za-z])", "命中 SxxExx 规则", True),
        (rf"第\s*({cn_number})\s*季.*?第\s*({cn_number})\s*[{EPISODE_MARKERS}]", "命中中文季集规则", True),
        (r"(?:^|[\s._-])0*(\d{1,2})\s*-\s*0*(\d{1,4})(?:\D|$)", "命中 季-集 规则", True),
    ]
    for pattern, reason, has_season in patterns:
        match = re.search(pattern, text)
        if match:
            if has_season:
                season = _season_number(match.group(1))
                episode = _episode_number(match.group(2))
                if episode and _valid_episode_number(episode):
                    return season, episode, reason
    roman = re.search(r"(?i)(?:^|[\s._\-])(?P<season>I|II|III|IV|V|VI|VII|VIII|IX|X)[\s._\-]+0*(?P<episode>\d{1,3})(?=$|[\s._\-\]）)]|\D)", text)
    if roman and _valid_episode_number(roman.group("episode")):
        return _roman_number(roman.group("season")), int(roman.group("episode")), "命中罗马数字季集规则"
    ep_patterns = [
        (r"(?i)(?:^|[^0-9A-Za-z])(?:EP?|Episode)\s*0*(\d{1,4})(?:v\d+)?(?=$|[^0-9A-Za-z])", "命中 EP 集数规则"),
        (rf"第\s*({cn_number})\s*[{EPISODE_MARKERS}]", "命中中文集数规则"),
        (rf"(?<![A-Za-z0-9])({cn_number})\s*[{EPISODE_MARKERS}]", "命中省略“第”的中文集数规则"),
    ]
    for pattern, reason in ep_patterns:
        for match in re.finditer(pattern, text):
            if _episode_marker_is_collection_context(text, match):
                continue
            episode = _episode_number(match.group(1))
            if episode and _valid_episode_number(episode):
                return season_hint, episode, reason
    cleaned_tail_text = _strip_trailing_metadata_for_episode(text)
    tail = re.search(r"(?:^|[\s._-])0*(\d{1,4})(?:v\d+)?$", cleaned_tail_text, re.I)
    if tail and not _tail_number_is_metadata(cleaned_tail_text, tail):
        episode = int(tail.group(1))
        if _valid_episode_number(episode):
            return season_hint, episode, "命中尾部数字集数规则"
    head = re.search(r"^0*(\d{1,4})(?=[\u4e00-\u9fff])", text)
    if head and not _is_year_number(head.group(1)):
        episode = int(head.group(1))
        if _valid_episode_number(episode):
            return season_hint, episode, "命中开头数字集数规则"
    if re.fullmatch(r"0*\d{1,4}(?:v\d+)?", text.strip(), re.I):
        number = re.sub(r"(?i)v\d+$", "", text.strip())
        if not _is_year_number(number) and _valid_episode_number(number):
            return season_hint, int(number), "命中裸数字文件名规则"
    standalone = _standalone_episode_candidate(text)
    if standalone:
        return season_hint, standalone, "命中独立数字集数规则"
    return season_hint, None, ""


def extract_season_from_title(value: Any) -> tuple[str, int | None]:
    """Return title with explicit season words removed, plus the detected season.

    TMDB 搜索通常搜“剧名”本体更稳定；但整理路径必须保留季号。因此这里
    只剥离 S02 / Season 2 / 第二季 这类明确季信息，不处理罗马数字，避免
    误伤片名里的普通 I/II。
    """

    text = _normalize_text(value)
    if not text:
        return "", None
    season = parse_season(text)
    cleaned = re.sub(r"(?i)(?:^|[\s._\-[(【])S(?:eason)?\s*0*\d{1,2}(?=$|[\s._\-)\]】])", " ", text)
    cleaned = re.sub(r"(?i)(?:^|[\s._\-[(【])Season\s*0*\d{1,2}(?=$|[\s._\-)\]】])", " ", cleaned)
    cleaned = re.sub(r"第\s*(?:\d{1,2}|[零〇一二两三四五六七八九十百]+)\s*季", " ", cleaned)
    # 常见资源会把第 N 季写成片名尾号，例如“一人之下6”。只在 CJK 标题尾部
    # 识别，避免误伤英文片名或年份。
    tail = re.search(r"(?<=[\u4e00-\u9fff])\s*([1-9]\d?)$", cleaned)
    if tail and season is None:
        season = int(tail.group(1))
        cleaned = cleaned[: tail.start()].strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_—–")
    return cleaned or text, season


def _season_number(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return _chinese_number(text)


def _episode_number(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return _chinese_number(text)


def _roman_number(value: Any) -> int | None:
    romans = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
    return romans.get(str(value or "").strip().upper())


def _chinese_number(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in CHINESE_NUMERAL_MAP:
        return CHINESE_NUMERAL_MAP[text]
    if not re.fullmatch(rf"[{CHINESE_NUMBER_CHARS}]+", text):
        return None
    if not any(unit in text for unit in "十百千万"):
        digits = [CHINESE_NUMERAL_MAP.get(char) for char in text]
        if any(item is None for item in digits):
            return None
        return int("".join(str(item) for item in digits))
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = 0
    section = 0
    number = 0
    for char in text:
        if char in CHINESE_NUMERAL_MAP:
            number = CHINESE_NUMERAL_MAP[char]
            continue
        unit = units.get(char)
        if not unit:
            return None
        if unit == 10000:
            section = (section + (number or 1)) * unit
            total += section
            section = 0
        else:
            section += (number or 1) * unit
        number = 0
    result = total + section + number
    return result if result > 0 else None


def _valid_episode_number(value: Any) -> bool:
    try:
        number = int(str(value).lstrip("0") or "0")
    except ValueError:
        return False
    return 0 < number <= 2000


def _episode_marker_is_collection_context(text: str, match: re.Match[str]) -> bool:
    prefix = text[max(0, match.start() - 8) : match.start()]
    suffix = text[match.end() : match.end() + 8]
    return bool(
        re.search(r"(?:更新\s*至|更\s*新?\s*至?|更到|更至|至)\s*$", prefix)
        or re.search(r"^\s*(?:全|完|完结|合集|全集)", suffix)
        or re.search(r"(?:合集|全集|完结)", text[max(0, match.start() - 12) : match.end() + 12])
    )


def _strip_trailing_metadata_for_episode(value: Any) -> str:
    text = str(value or "").strip(" ._-")
    if not text:
        return ""
    token_pattern = re.compile(
        r"(?ix)"
        r"(?:^|[\s._\-\[\]()【】])"
        r"(?:4k|8k|2160p|1080p|720p|480p|uhd|hdr10?|dv|sdr|web[- ]?dl|webrip|bluray|remux|hevc|avc|"
        r"h\.?265|x265|h\.?264|x264|aac(?:\s*\d(?:\.\d)?)?|ddp(?:\s*\d(?:\.\d)?)?|dts(?:-?hd)?|"
        r"60fps|120fps|10bit|8bit|中字|中文字幕|国语|国配|粤语|简中|繁中|内嵌|内封|高码率|臻彩)"
        r"[\s._\-\[\]()【】]*$"
    )
    changed = True
    while changed and text:
        before = text
        text = re.sub(r"[\s._\-\[\]()【】]+$", "", text).strip()
        text = token_pattern.sub("", text).strip(" ._-")
        changed = text != before
    return text


def _standalone_episode_candidate(value: Any) -> int | None:
    text = _strip_trailing_metadata_for_episode(value)
    if not text:
        return None
    matches = list(re.finditer(r"(?<![A-Za-z0-9])0*(\d{1,4})(?:v\d+)?(?![A-Za-z0-9])", text, re.I))
    for match in reversed(matches):
        number_text = match.group(1)
        if _is_year_number(number_text) or not _valid_episode_number(number_text):
            continue
        if _standalone_number_is_metadata_or_season(text, match):
            continue
        return int(number_text)
    return None


def _standalone_number_is_metadata_or_season(text: str, match: re.Match[str]) -> bool:
    number = match.group(1)
    prefix = text[max(0, match.start() - 16) : match.start()].lower()
    suffix = text[match.end() : match.end() + 16].lower()
    if re.search(r"(?:更新\s*至|更\s*新?\s*至?|更到|更至|至)\s*$", prefix):
        return True
    if re.match(rf"\s*[{EPISODE_MARKERS}]", suffix):
        return True
    if re.search(r"(?:season|第|s)\s*$", prefix) and re.match(r"\s*季", suffix):
        return True
    if re.search(r"(?:season|s)\s*$", prefix):
        return True
    if re.match(r"\s*(?:季|年|月|日|p|k|bit|fps)", suffix):
        return True
    if re.search(r"(?:h|x)\.?\s*$", prefix) or re.match(r"\s*(?:p|k)", suffix):
        return True
    if number in {"2160", "1080", "720", "480", "264", "265"}:
        nearby = f"{prefix}{number}{suffix}"
        if re.search(r"(?i)(?:p|k|h\.?26[45]|x26[45]|hevc|avc|清晰|画质|分辨率)", nearby):
            return True
    return False


def _tail_number_is_metadata(text: str, match: re.Match[str]) -> bool:
    number = match.group(1)
    if _is_year_number(number):
        return True
    prefix = text[: match.start(1)].rstrip(" ._-")
    tail_context = text[max(0, match.start(1) - 8) : match.end(1)].lower()
    if re.search(r"(?i)(?:^|[\s._-])(?:h|x)\.?\s*$", prefix):
        return True
    if re.search(r"(?i)(?:h|x)\.?\s*" + re.escape(number) + r"$", tail_context):
        return True
    if re.search(r"(?i)(?:^|[\s._-])(?:2160p|1080p|720p|480p|h\.?264|h\.?265|x264|x265|hevc|avc|aac|dts|ddp)\s*$", text[: match.end(1)]):
        return True
    return False


def _is_year_number(value: Any) -> bool:
    try:
        number = int(str(value).lstrip("0") or "0")
    except ValueError:
        return False
    return 1900 <= number <= 2099


def choose_title(stem: str, *, current_dir: str = "", parent_dir: str = "", siblings: list[str] | None = None, has_episode: bool = False) -> str:
    own = sanitize_candidate(stem)
    current = sanitize_candidate(current_dir)
    parent = sanitize_candidate(parent_dir)
    if has_episode and current and not is_season_name(current) and not invalid_title(current):
        return current
    if own and not invalid_title(own):
        return own
    if current and not is_season_name(current) and not invalid_title(current):
        return current
    if parent and not is_season_name(parent) and not invalid_title(parent):
        return parent
    common = common_prefix(siblings or [])
    return common if common and not invalid_title(common) else ""


def sanitize_candidate(value: Any) -> str:
    raw = _normalize_text(value)
    if not raw:
        return ""
    # 广告/谐音资源经常用逗号后给出更干净的别名：例如“...更25集,一人之下6”。
    # 先分段清洗，再选最像片名的一段。
    segments = [segment for segment in re.split(r"[,，;；]+", raw) if segment.strip()]
    candidates = []
    for segment in (segments or [raw]):
        candidates.extend(_bracket_title_candidates(segment))
        candidates.append(_clean_candidate_segment(segment))
    text = max(candidates, key=_candidate_score) if candidates else ""
    text, _ = extract_season_from_title(text)
    text = re.sub(r"\(\s*\)|（\s*）|\[\s*\]|【\s*】", " ", text)
    text = clean_media_title(text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_text(value: Any) -> str:
    text = sanitize_resource_dir_name(value, fallback="")
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", text)
    return text.replace("＋", "+").strip()


def _clean_candidate_segment(value: Any) -> str:
    text = _normalize_text(value)
    text = re.sub(r"【[^】]*】|\[[^\]]*\]", " ", text)
    text = re.sub(r"(?i)\b(BY|HD|BD|WEB|DL|REMUX|资源库|小谢|公众号)\b", " ", text)
    text = re.sub(r"(?i)\bS\s*0*\d{1,2}\s*E\s*0*\d{1,4}\b", " ", text)
    text = re.sub(r"(?i)\b(?:EP?|Episode)\s*0*\d{1,4}(?:v\d+)?\b", " ", text)
    text = re.sub(rf"第\s*(?:\d{{1,4}}|[{CHINESE_NUMBER_CHARS}]+)\s*[{EPISODE_MARKERS}]", " ", text)
    # “全41集”“共40集”“全41话”等全集数描述，连同“全/共”前缀整体剥掉，
    # 避免残留孤立的“全”字污染标题（例如“伪装者 全41集”→“伪装者”）。
    text = re.sub(rf"(?:全|共)\s*(?:\d{{1,4}}|[{CHINESE_NUMBER_CHARS}]+)\s*[{EPISODE_MARKERS}]", " ", text)
    text = re.sub(rf"(?<![A-Za-z0-9])(?:\d{{1,4}}|[{CHINESE_NUMBER_CHARS}]+)\s*[{EPISODE_MARKERS}]", " ", text)
    text = re.sub(r"(?i)(?:^|[\s._\-])(?:I|II|III|IV|V|VI|VII|VIII|IX|X)[\s._\-]+0*\d{1,3}(?=$|[\s._\-\]）)]|\D)", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = NOISE_TOKEN_PATTERN.sub(" ", text)
    text = CN_NOISE_PATTERN.sub(" ", text)
    text = re.sub(r"[+_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_—–")
    return text


def _bracket_title_candidates(value: Any) -> list[str]:
    """Extract plausible titles from bracket groups.

    BT/PT 资源常见格式是 `[发布组][片名][清晰度][编码]`。旧逻辑把所有方括号
    内容整体删除，导致 `[DBD-Raws][流浪地球][1080P]...` 变成空标题。这里
    只把“像片名”的括号段作为候选，技术参数和发布组仍会被评分压低或丢弃。
    """

    text = _normalize_text(value)
    if not text:
        return []
    result: list[str] = []
    for match in re.finditer(r"【([^】]{1,80})】|\[([^\]]{1,80})\]|\(([^)]{1,80})\)|（([^）]{1,80})）", text):
        raw = next((group for group in match.groups() if group), "")
        cleaned = _clean_candidate_segment(raw)
        if not cleaned or _looks_like_noise_bracket(raw, cleaned):
            continue
        result.append(cleaned)
    return result


def _looks_like_noise_bracket(raw: Any, cleaned: str) -> bool:
    raw_text = str(raw or "").strip()
    text = str(cleaned or "").strip()
    if not text:
        return True
    lower = raw_text.lower()
    if re.fullmatch(r"(?i)(?:4k|8k|2160p|1080p|720p|480p|bdrip|bluray|web-?dl|remux|hevc(?:-?10bit)?|h\.?265|x265|h\.?264|x264|flac|aac|dts|ddp|hdr10?|dv|sdr)", raw_text):
        return True
    if not re.search(r"[\u4e00-\u9fff]", text) and re.search(r"(?i)(raws?|subs?|字幕组|team|group|压制|rip|encoder)", lower):
        return True
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", text):
        return True
    return False


def _candidate_score(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return -1000
    score = 0.0
    if re.search(r"[\u4e00-\u9fff]", text):
        score += 60
    if re.search(r"[A-Za-z]", text):
        score += 20
    _, season = extract_season_from_title(text)
    if season:
        score += 15
    if re.search(r"(?:^|[\s._-])(?:4k|1080p|2160p|aac|ddp|hifi|中字)(?:$|[\s._-])", text, re.I):
        score -= 30
    return score - min(len(text), 80) * 0.25


def invalid_title(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or len(text) <= 1:
        return True
    if re.fullmatch(r"\d{1,4}", text):
        return True
    if is_season_name(text):
        return True
    return False


def is_season_name(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(re.fullmatch(rf"(?i)(season\s*)?\d{{1,2}}|s\d{{1,2}}|第?(?:\d{{1,2}}|[{CHINESE_NUMBER_CHARS}]+)季", text))


def common_prefix(names: list[str]) -> str:
    cleaned = [sanitize_candidate(posixpath.splitext(name)[0]) for name in names]
    cleaned = [item for item in cleaned if item]
    if len(cleaned) < 2:
        return ""
    prefix = cleaned[0]
    for item in cleaned[1:]:
        while prefix and not item.startswith(prefix):
            prefix = prefix[:-1]
    return prefix.strip(" -_")


def extract_version_tags(value: Any) -> str:
    tags = []
    for match in TECH_VERSION_PATTERN.finditer(str(value or "")):
        tag = re.sub(r"\s+", "", match.group(1)).upper()
        if tag not in tags:
            tags.append(tag)
    return " ".join(tags[:4])


def category_target_root(category: dict[str, Any]) -> str:
    # 这里只能使用 OpenList/FNOS 可见路径，不能回退到 139 官方云端路径。
    # 例如 139 官方保存路径可能是“博客/电影”，但 OpenList 里实际挂载为
    # “移动云2/电影”。误用官方路径会触发 OpenList storage not found。
    for key in ("openlist_root_path", "cloud139_fnos_target_path", "mobile_target_path", "sixpan_fnos_target_path"):
        value = str(category.get(key) or "").strip()
        if value and value.lower() not in {"none", "null", "undefined"}:
            return "/" + value.strip("/")
    label = str(category.get("label") or "媒体").strip()
    if label.lower() in {"none", "null", "undefined"}:
        label = "媒体"
    return "/" + label.strip("/")


def standard_target_path(
    *,
    category_key: str,
    category: dict[str, Any],
    title: str,
    year: str = "",
    season: int | None = None,
    episode: int | None = None,
    ext: str = "",
    version: str = "",
) -> str:
    resource_root = str(category.get("resource_root_path") or category.get("canonical_resource_root") or "").strip()
    root = join_path(resource_root) if resource_root else category_target_root(category)
    clean_title, detected_year = split_title_year(title)
    clean_title = clean_title or sanitize_candidate(title) or "未命名资源"
    year = str(year or detected_year or "").strip()
    display = format_title_year(clean_title, year) if year else sanitize_resource_dir_name(clean_title)
    if resource_root:
        if category_key == "movie":
            filename = f"{display}{ext}"
            return join_path(root, filename)
        season_value = 1 if season is None else season
        if episode is None:
            filename = f"{display}{ext}"
            return join_path(root, filename)
        suffix = f" - {version}" if version else ""
        filename = f"{display} - S{season_value:02d}E{episode:02d}{suffix}{ext}"
        return join_path(root, f"Season {season_value:02d}", filename)
    if category_key == "movie":
        filename = f"{display}{ext}"
        return join_path(root, display, filename)
    season_value = 1 if season is None else season
    if episode is None:
        filename = f"{display}{ext}"
        return join_path(root, display, filename)
    suffix = f" - {version}" if version else ""
    filename = f"{display} - S{season_value:02d}E{episode:02d}{suffix}{ext}"
    return join_path(root, display, f"Season {season_value:02d}", filename)


def evidence_title(*values: Any) -> str:
    for value in values:
        title, year = split_title_year(value)
        if title and not invalid_title(title):
            return format_title_year(title, year) if year else title
    return ""


def date_for_variety(name: str) -> str:
    return parse_date_text(name)
