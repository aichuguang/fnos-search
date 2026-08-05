"""UTC 时间工具。

统一生成数据库 ISO-8601 UTC 时间戳，输出固定为 ``YYYY-MM-DDTHH:MM:SSZ``
（秒级精度、``Z`` 后缀）。历史代码直接使用 ``datetime.utcnow()`` 已弃用，
此处是唯一真实实现；各模块的局部 helper 只做委托。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now_iso() -> str:
    """当前 UTC 时间，秒级精度，格式 ``...Z``。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now_iso_offset(**delta_kwargs: int) -> str:
    """当前 UTC 时间加减 ``timedelta`` 参数后的 ``...Z`` 字符串。

    支持 ``minutes=``/``seconds=`` 等 ``timedelta`` 关键字，负数表示过去，
    例如 ``utc_now_iso_offset(minutes=-90)``。
    """
    value = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(**delta_kwargs)
    return value.isoformat().replace("+00:00", "Z")
