"""Frozen, deliberately small relay prefetch and disk limits."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RelayPipelineConfig:
    prefetch_files: int = 1
    cache_max_bytes: int = 20 * 1024 * 1024 * 1024
    disk_reserve_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            type(self.prefetch_files) is not int or self.prefetch_files not in {0, 1, 2}
            or type(self.cache_max_bytes) is not int or self.cache_max_bytes <= 0
            or type(self.disk_reserve_bytes) is not int or self.disk_reserve_bytes < 0
        ):
            raise ValueError("中继预取配置无效：文件数仅支持0/1/2，缓存上限须为正整数，磁盘余量不可为负")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RelayPipelineConfig":
        environ = os.environ if environ is None else environ
        defaults = cls()
        values = {}
        for field, key in (
            ("prefetch_files", "RELAY_PREFETCH_FILES"),
            ("cache_max_bytes", "RELAY_CACHE_MAX_BYTES"),
            ("disk_reserve_bytes", "RELAY_DISK_RESERVE_BYTES"),
        ):
            raw = environ.get(key, str(getattr(defaults, field))).strip()
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError(f"{key} 必须是非负整数")
            values[field] = int(raw)
        return cls(**values)

    @classmethod
    def from_dict(cls, value: Any) -> "RelayPipelineConfig":
        if not isinstance(value, dict) or set(value) != {
            "prefetch_files", "cache_max_bytes", "disk_reserve_bytes",
        }:
            raise ValueError("中继清单缺少已冻结的预取配置")
        return cls(**value)

    def to_dict(self) -> dict[str, int]:
        return asdict(self)
