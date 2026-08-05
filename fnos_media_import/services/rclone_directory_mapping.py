from __future__ import annotations

from typing import Any


class RcloneDirectoryMappingValidator:
    """Normalizes and validates source/target directory mappings."""

    CATEGORIES = (
        ("movie", "电影", "离线电影", "MOVIE"),
        ("tv", "电视剧", "离线剧集", "TV"),
        ("anime", "动漫", "离线动漫", "ANIME"),
        ("variety", "综艺", "离线综艺", "VARIETY"),
        ("other", "其他", "离线其他", "OTHER"),
    )

    @classmethod
    def validate(cls, mapping: dict[str, str], *, category_filter: str = "") -> list[str]:
        filter_norm = cls.normalize_filter(category_filter)
        errors: list[str] = []
        for key, label, job_name, suffix in cls.CATEGORIES:
            aliases = {cls.normalize_filter(value) for value in (key, label, job_name, suffix)}
            if filter_norm and filter_norm not in aliases:
                continue
            source = mapping.get(f"RCLONE_SRC_{suffix}_DIR", "")
            target = mapping.get(f"RCLONE_DST_{suffix}_DIR", "")
            if cls.overlap(source, target):
                errors.append(f"{label}源目录与目标目录重叠：{source} -> {target}")
        return errors

    @classmethod
    def overlap(cls, left: Any, right: Any) -> bool:
        left_norm = cls.normalize(left)
        right_norm = cls.normalize(right)
        if not left_norm or not right_norm:
            return False
        return (
            left_norm == right_norm
            or left_norm.startswith(f"{right_norm}/")
            or right_norm.startswith(f"{left_norm}/")
        )

    @staticmethod
    def normalize(value: Any) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if ":" in text.split("/", 1)[0]:
            text = text.split(":", 1)[1]
        while "//" in text:
            text = text.replace("//", "/")
        return text.strip("/").casefold()

    @staticmethod
    def normalize_filter(value: Any) -> str:
        return "".join(str(value or "").strip().lower().split())

    @staticmethod
    def clean(value: Any) -> str:
        return str(value or "").strip().strip("/")
