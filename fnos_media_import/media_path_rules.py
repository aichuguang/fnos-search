from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any


EPISODIC_CATEGORY_KEYS = {"tv", "anime", "variety"}
FALLBACK_ORGANIZE_DIR = "_待整理"
MEDIA_TECH_TOKENS = {
    "2160p",
    "1080p",
    "720p",
    "480p",
    "web-dl",
    "web",
    "dl",
    "webrip",
    "bluray",
    "bdrip",
    "hdr",
    "hdr10",
    "dv",
    "h265",
    "h.265",
    "hevc",
    "x265",
    "h264",
    "h.264",
    "x264",
    "ddp",
    "ddp5",
    "atmos",
    "aac",
    "国语",
    "中字",
    "国语中字",
}
MEDIA_FILE_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".ts",
    ".m2ts",
    ".mpg",
    ".mpeg",
    ".srt",
    ".ass",
    ".ssa",
    ".sup",
}


def sanitize_resource_dir_name(value: Any, fallback: str = "未命名资源", max_length: int = 120) -> str:
    """把资源标题转换成安全的单级目录名。"""

    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    if not text:
        text = fallback
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .-_")
    return text or fallback


def normalize_resource_name(value: Any) -> str:
    text = sanitize_resource_dir_name(value, fallback="")
    return re.sub(r"[\s._\-【】\[\]()（）]+", "", text).lower()


def append_resource_dir(base_path: Any, resource_name: Any) -> str:
    """在远端基础目录下追加资源目录；如果已经追加过则不重复追加。"""

    base = str(base_path or "").strip().replace("\\", "/")
    child = sanitize_resource_dir_name(resource_name)
    if not base:
        return child
    base = "/" + base.strip("/") if base.startswith("/") else base.strip("/")
    last = PurePosixPath(base.strip("/")).name
    if normalize_resource_name(last) == normalize_resource_name(child):
        return base
    return f"{base.rstrip('/')}/{child}"


def should_force_resource_dir(category_key: Any, file_count: int = 0) -> bool:
    key = str(category_key or "").strip().lower()
    if key in EPISODIC_CATEGORY_KEYS:
        return True
    if key == "movie" and file_count > 1:
        return True
    return False


def build_standard_naming_plan(
    *,
    category_key: Any,
    filename: Any,
    target_file: Any = "",
    resource_title: Any = "",
    source_file: Any = "",
) -> dict[str, Any]:
    """生成上传前标准命名计划。

    这是保守规则：不联网、不强猜；能识别标题/年份/集数才改名，否则只保证目录不污染根目录。
    """

    key = str(category_key or "").strip().lower()
    original_name = PurePosixPath(str(filename or "")).name or PurePosixPath(str(target_file or source_file or "")).name
    suffix = PurePosixPath(original_name).suffix
    current_target = str(target_file or original_name).strip().replace("\\", "/").strip("/")
    current_parts = [part for part in current_target.split("/") if part]
    current_dir = "/".join(current_parts[:-1])
    title_source = str(resource_title or "").strip() or _resource_title_from_target(current_target) or PurePosixPath(original_name).stem
    parsed_title, year = split_title_year(title_source)
    if not year:
        _, year = split_title_year(original_name)
    display_title = format_title_year(parsed_title, year)

    plan = {
        "success": True,
        "category": key,
        "original_name": original_name,
        "original_target": current_target,
        "target_relative_dir": current_dir,
        "target_name": original_name,
        "target_file": current_target or original_name,
        "applied": False,
        "confidence": 0.35,
        "mode": "keep",
        "reason": "置信度不足，保留原文件名",
    }

    if not display_title:
        fallback_dir = f"{FALLBACK_ORGANIZE_DIR}/{sanitize_resource_dir_name(PurePosixPath(original_name).stem)}"
        return _finish_plan(plan, fallback_dir, original_name, 0.2, "fallback", "缺少可用标题，进入待整理目录")

    if key == "movie":
        target_dir = display_title
        target_name = f"{display_title}{suffix}" if suffix else display_title
        confidence = 0.78 if year else 0.62
        reason = "电影按 片名 (年份)/片名 (年份) 命名" if year else "电影缺少年份，仅按片名目录命名"
        return _finish_plan(plan, target_dir, target_name, confidence, "standard_movie", reason)

    if key in EPISODIC_CATEGORY_KEYS:
        date_text = parse_date_text(original_name)
        if key == "variety" and date_text:
            date_year = date_text[:4]
            show_title = format_title_year(parsed_title, year or date_year)
            target_dir = f"{show_title}/Season {date_year}"
            target_name = f"{show_title} - {date_text}{suffix}" if suffix else f"{show_title} - {date_text}"
            return _finish_plan(plan, target_dir, target_name, 0.82, "standard_variety_date", "综艺识别到日期，按日期型节目命名")

        season, episode = parse_episode_number(original_name)
        if episode:
            season = season or 1
            target_dir = f"{display_title}/Season {season:02d}"
            target_name = f"{display_title} - S{season:02d}E{episode:02d}{suffix}" if suffix else f"{display_title} - S{season:02d}E{episode:02d}"
            return _finish_plan(plan, target_dir, target_name, 0.8, "standard_episode", "识别到剧集集数，按 Season/SxxEyy 命名")

        target_dir = display_title
        return _finish_plan(plan, target_dir, original_name, 0.55, "resource_dir_only", "未可靠识别集数，只整理到资源目录")

    return plan


def split_title_year(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    path = PurePosixPath(text.replace("\\", "/"))
    if path.suffix.lower() in MEDIA_FILE_SUFFIXES:
        text = path.stem
    year = ""
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
    if match:
        year = match.group(1)
    title = clean_media_title(text)
    if year:
        title = re.sub(rf"[\s._\-（(【\[]*{re.escape(year)}[\s._\-）)】\]]*", " ", title)
        title = clean_media_title(title)
    return title, year


def clean_media_title(value: Any) -> str:
    text = sanitize_resource_dir_name(value, fallback="")
    text = re.sub(r"【[^】]*】|\[[^\]]*\]", " ", text)
    # 先剔除 H.265 / H 265 / x265 这类编码标记，再把点号拆成空格。
    # 否则 H.265 会被拆成 “H 265”，后续按 token 清洗时会漏掉 265。
    text = re.sub(r"(?i)(?<![0-9a-z])(?:h|x)\s*[._-]?\s*(?:264|265)(?![0-9a-z])", " ", text)
    text = text.replace(".", " ").replace("_", " ")
    parts = []
    for raw in re.split(r"[\s\-]+", text):
        token = raw.strip()
        if not token:
            continue
        lowered = token.lower()
        if lowered in MEDIA_TECH_TOKENS:
            continue
        if re.fullmatch(r"(?:hd|uhd)?(?:2160p|1080p|720p|480p)", lowered):
            continue
        if re.fullmatch(r"(?:h\.?|x)(?:264|265)", lowered):
            continue
        if re.fullmatch(r"(?:ddp|aac|truehd|dts)\d(?:\.\d)?", lowered):
            continue
        if re.fullmatch(r"(?:豆瓣)?\d(?:\.\d)?", lowered):
            continue
        parts.append(token)
    return re.sub(r"\s+", " ", " ".join(parts)).strip(" .-_")


def format_title_year(title: Any, year: Any = "") -> str:
    clean_title = sanitize_resource_dir_name(title, fallback="")
    text_year = str(year or "").strip()
    if not clean_title:
        return ""
    if text_year and f"({text_year})" not in clean_title and f"（{text_year}）" not in clean_title:
        return f"{clean_title} ({text_year})"
    return clean_title


def parse_episode_number(value: Any) -> tuple[int | None, int | None]:
    text = str(value or "")
    patterns = [
        r"[Ss](\d{1,2})\s*[Ee](\d{1,3})",
        r"第\s*(\d{1,2})\s*季\s*第\s*(\d{1,3})\s*[集话話]",
        r"(?:第|EP|Ep|ep|E|e)\s*(\d{1,3})\s*(?:集|话|話)?",
    ]
    first = re.search(patterns[0], text)
    if first:
        return int(first.group(1)), int(first.group(2))
    second = re.search(patterns[1], text)
    if second:
        return int(second.group(1)), int(second.group(2))
    third = re.search(patterns[2], text)
    if third:
        return None, int(third.group(1))
    stem = PurePosixPath(text.replace("\\", "/")).stem
    if re.fullmatch(r"\d{1,3}", stem):
        return None, int(stem)
    tail = re.search(r"(?:^|[\s._\-])(\d{1,3})(?:v\d)?$", stem)
    if tail:
        return None, int(tail.group(1))
    return None, None


def parse_date_text(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"((?:19|20)\d{2})[.\-_年/]?(\d{1,2})[.\-_月/]?(\d{1,2})", text)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _resource_title_from_target(target_file: str) -> str:
    parts = [part for part in str(target_file or "").replace("\\", "/").strip("/").split("/") if part]
    if len(parts) >= 2 and parts[0] != FALLBACK_ORGANIZE_DIR:
        return parts[0]
    return ""


def _finish_plan(plan: dict[str, Any], relative_dir: str, target_name: str, confidence: float, mode: str, reason: str) -> dict[str, Any]:
    relative_dir = "/".join(part for part in str(relative_dir or "").replace("\\", "/").strip("/").split("/") if part)
    target_name = sanitize_resource_dir_name(target_name, fallback=plan.get("original_name") or "未命名资源", max_length=180)
    target_file = f"{relative_dir}/{target_name}" if relative_dir else target_name
    plan.update(
        {
            "target_relative_dir": relative_dir,
            "target_name": target_name,
            "target_file": target_file,
            "applied": target_file != (plan.get("original_target") or plan.get("original_name")),
            "confidence": round(float(confidence), 2),
            "mode": mode,
            "reason": reason,
        }
    )
    return plan
