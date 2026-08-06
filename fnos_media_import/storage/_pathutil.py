"""存储路径的共享小工具（v3）。"""

from __future__ import annotations

from typing import Any


def normalize_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.strip("/")


def normalize_visible_path(value: Any) -> str:
    """OpenList 可见路径：清理内部斜杠但保留前导 /。"""

    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    text = text.strip()
    if not text:
        return ""
    return text if text.startswith("/") else f"/{text}"


def staging_path_for(
    *,
    final_root: str,
    staging_dir_name: str,
    category_label: str,
    job_id: int,
) -> str:
    """在 final_root 同级的暂存目录下派生任务级路径。

    例如 final_root=影视/电影 -> 影视/_入库暂存/电影/job-42。
    无法解析父级时退回 `_入库暂存/<label>/job-<id>`。
    """

    base = normalize_path(final_root)
    dir_name = normalize_path(staging_dir_name) or "_入库暂存"
    label = normalize_path(category_label) or (base.rsplit("/", 1)[-1] if base else "default")
    parent = base.rsplit("/", 1)[0] if "/" in base else ""
    prefix = f"{parent}/{dir_name}".strip("/") if parent else dir_name
    return f"{prefix}/{label}/job-{int(job_id)}"


def is_unsafe_path(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if "\x00" in text or any(ord(ch) < 32 for ch in text):
        return True
    normalized = text.replace("\\", "/").strip("/")
    if normalized in {"", ".", ".."}:
        return True
    return any(segment in {"", ".", ".."} for segment in normalized.split("/"))


def _normalize_absolute(value: Any) -> str:
    """规范化容器内绝对路径：统一分隔符、折叠连续斜杠，但保留前导斜杠。"""

    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text


def resolve_local_target(root: str, relative: str) -> str:
    """把受控容器根目录与相对路径安全拼接。

    返回规范化后的绝对容器路径。任何越界都抛出 ValueError：

    - ``root`` 不是绝对容器路径、是文件系统根、含 ``..``/``.``/空段或控制字符。
    - ``relative`` 为空、是绝对路径、含 ``..``/``.``/空段或控制字符。

    拼接结果始终严格位于 ``root`` 内，这是本地存储目标的唯一合法路径来源。
    """

    root_norm = _normalize_absolute(root)
    if not root_norm.startswith("/"):
        raise ValueError("root 必须是容器内绝对路径")
    root_norm = root_norm.rstrip("/")
    if root_norm == "":
        raise ValueError("root 不能是文件系统根目录")
    if "\x00" in root_norm or any(ord(ch) < 32 for ch in root_norm):
        raise ValueError("root 包含非法控制字符")
    if any(segment in {"", ".", ".."} for segment in root_norm.split("/")[1:]):
        raise ValueError("root 包含 .. 或非法路径段")
    # Local destinations are deployment mounts, never arbitrary application
    # paths.  Keeping this allowlist in the shared resolver means every caller
    # (planner, worker and admin validation) gets the same boundary.
    if root_norm != "/storage-targets" and not root_norm.startswith("/storage-targets/"):
        raise ValueError("root 必须位于 /storage-targets 挂载目录内")

    rel_text = str(relative or "").strip()
    if not rel_text:
        raise ValueError("relative 不能为空")
    if rel_text.startswith("/") or rel_text.startswith("\\"):
        raise ValueError("relative 必须是相对路径，不能是绝对路径")
    if "\x00" in rel_text or any(ord(ch) < 32 for ch in rel_text):
        raise ValueError("relative 包含非法控制字符")
    rel_norm = normalize_path(rel_text)
    if rel_norm in {"", ".", ".."}:
        raise ValueError("relative 不合法")
    if any(segment in {"", ".", ".."} for segment in rel_norm.split("/")):
        raise ValueError("relative 包含 .. 或非法路径段")

    return f"{root_norm}/{rel_norm}"
