"""目标存储驱动注册表。未知 kind 必须抛出配置错误，绝不静默回落。"""

from __future__ import annotations

from .drivers.cmcc_api import CmccApiDriver
from .drivers.rclone import RcloneDriver
from .types import StorageDriver


class UnknownStorageDriver(ValueError):
    pass


_DRIVERS: dict[str, StorageDriver] = {}


def _register(driver: StorageDriver) -> None:
    _DRIVERS[driver.kind] = driver


_register(CmccApiDriver())
_register(RcloneDriver())


def get_driver(kind: str) -> StorageDriver:
    key = str(kind or "").strip().lower()
    driver = _DRIVERS.get(key)
    if driver is None:
        raise UnknownStorageDriver(f"未知存储驱动：{kind}")
    return driver


def registered_driver_kinds() -> frozenset[str]:
    return frozenset(_DRIVERS)
