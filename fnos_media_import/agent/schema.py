"""Small builders for the JSON Schema fragments MCP expects.

Keeping these explicit avoids a dependency on pydantic while still producing
schemas that clients can validate and that models can read.
"""

from __future__ import annotations

from typing import Any


def string(description: str = "", *, default: str | None = None, enum: list[str] | None = None, max_length: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if description:
        schema["description"] = description
    if enum:
        schema["enum"] = list(enum)
    if max_length:
        schema["maxLength"] = int(max_length)
    if default is not None:
        schema["default"] = default
    return schema


def integer(description: str = "", *, default: int | None = None, minimum: int | None = None, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer"}
    if description:
        schema["description"] = description
    if minimum is not None:
        schema["minimum"] = int(minimum)
    if maximum is not None:
        schema["maximum"] = int(maximum)
    if default is not None:
        schema["default"] = int(default)
    return schema


def boolean(description: str = "", *, default: bool | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "boolean"}
    if description:
        schema["description"] = description
    if default is not None:
        schema["default"] = bool(default)
    return schema


def array(item: dict[str, Any], *, description: str = "", max_items: int | None = None, min_items: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": item}
    if description:
        schema["description"] = description
    if max_items:
        schema["maxItems"] = int(max_items)
    if min_items:
        schema["minItems"] = int(min_items)
    return schema


def obj(properties: dict[str, Any], required: list[str] | None = None, *, description: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(required or []),
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


def open_obj(properties: dict[str, Any], required: list[str] | None = None, *, description: str = "") -> dict[str, Any]:
    """Object schema that tolerates provider-specific extra keys.

    Used only for the parts of the submission payload whose exact shape belongs
    to a provider adapter; the documented keys are still advertised.
    """

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(required or []),
        "additionalProperties": True,
    }
    if description:
        schema["description"] = description
    return schema


def selection_entry(*, with_token: bool = False) -> dict[str, Any]:
    """One selectable file or directory inside a share."""

    properties: dict[str, Any] = {
        "fid": string("文件/目录 ID", max_length=256),
        "name": string("显示名称", max_length=240),
        "path": string("分享内相对路径", max_length=1024),
        "type": string("dir 或 file", max_length=8),
        "is_dir": boolean("是否目录"),
    }
    if with_token:
        properties["share_fid_token"] = string("移动云目录令牌", max_length=2048)
    return open_obj(properties, required=["fid"])


def quark_selection_schema() -> dict[str, Any]:
    return open_obj(
        {
            "mode": string("勾选模式", enum=["root_dirs", "subdir_items"]),
            "selected_dirs": array(selection_entry(), description="root_dirs 模式选择的一级目录", max_items=100),
            "base_dir": selection_entry(),
            "selected_items": array(selection_entry(), description="subdir_items 模式下该目录内的条目", max_items=200),
        },
        required=["mode"],
        description="夸克勾选结构，与网页勾选后提交的格式一致",
    )


def cloud139_selection_schema() -> dict[str, Any]:
    return open_obj(
        {
            "mode": string("勾选模式，固定 items", enum=["items"]),
            "selected_files": array(selection_entry(with_token=True), max_items=300),
            "selected_folders": array(selection_entry(with_token=True), max_items=100),
        },
        required=["mode"],
        description="移动云勾选结构；selected_files / selected_folders 至少一个非空",
    )


def tianyi_selection_schema() -> dict[str, Any]:
    return open_obj(
        {
            "mode": string("勾选模式，固定 items", enum=["items"]),
            "selected_files": array(selection_entry(), max_items=300),
            "selected_folders": array(selection_entry(), max_items=100),
            "selected_items": array(selection_entry(), description="兼容合并列表写法", max_items=300),
        },
        required=["mode"],
        description="天翼勾选结构；也接受合并的 selected_items",
    )


def sixpan_selection_schema() -> dict[str, Any]:
    return open_obj(
        {
            "ignore_files": array(string(), description="BT/磁链中要忽略的文件名", max_items=2000),
            "parse_status": string("解析状态", max_length=32),
            "parse_error": string("解析失败原因", max_length=300),
        },
        description="六盘勾选结构；不传表示整份分享入库",
    )


def source_schema() -> dict[str, Any]:
    """One update-subscription source."""

    return open_obj(
        {
            "type": string("来源类型，例如 search / cloud / rss", max_length=24),
            "name": string("显示名称", max_length=120),
            "provider": string("网盘或搜索提供方", max_length=24),
            "url": string("来源链接", max_length=2048),
            "password": string("提取码", max_length=32),
            "priority": integer("优先级，数字越小越优先", minimum=0, maximum=1000),
            "enabled": boolean("是否启用"),
            "options": open_obj({}, description="提供方专属选项"),
        },
        required=["type"],
    )
