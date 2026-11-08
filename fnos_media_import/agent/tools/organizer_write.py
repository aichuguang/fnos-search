"""Organizer write tools.

Physical apply and rollback require ``organize.apply`` (absent from a default
token). An agent may act autonomously under the user's rules with execute=true;
the current plan fingerprint is still checked atomically by the backend. Retry
defaults to planning only and requires the same extra scope to resume execution.
"""

from __future__ import annotations

from typing import Any

from ..args import optional_text
from ..auth import SCOPE_ORGANIZE_APPLY, SCOPE_ORGANIZE_WRITE
from ..context import AgentCallContext
from ..errors import AgentError, invalid_params, not_found, organizer_error
from ..plans import organizer_plan_summary
from ..registry import RISK_DESTRUCTIVE, RISK_WRITE, ToolRegistry, ToolSpec
from ..schema import boolean, integer, obj, string
from ..services import AgentServices
from .common import compact_write_result, require_confirm, writes_of

_BATCH_FIELDS = ("title", "season", "tmdb_id")
_SINGLE_FIELDS = ("title", "year", "season", "episode", "media_type", "target_name")
MAPPING_STATUSES = ("ready", "need_edit", "conflict", "skipped")
_SAFE_OPERATION_TYPES = {"create_dir", "write_identity_manifest", "move_file", "cleanup_empty_dir"}


def register(registry: ToolRegistry, services: AgentServices | None = None) -> None:
    registry.register(
        ToolSpec(
            name="update_organizer_mappings",
            title="修改整理映射",
            description=(
                "修正整理任务的目标片名、季集或状态。传 mapping_id 只改单条（可改 title/year/season/"
                "episode/media_type/tmdb_id/target_name/target_path/status）；不传 mapping_id 则批量改全部映射，"
                "此时只支持 title、season 与 tmdb_id。单条 tmdb_id=null 表示显式解除绑定；批量禁止 null。"
                "tmdb_id 不传或留空会保留已有身份。必须修正身份、季集与冲突原因；只改 ready 不代表问题已解决。"
                "只给 target_name 时会同步算出 target_path，避免“改名字但按旧路径移动”。"
                "返回新的计划摘要与 plan_hash，供调用方按用户已授权规则审核和并发校验。"
            ),
            input_schema=obj(
                {
                    "task_id": integer("整理任务编号", minimum=1),
                    "mapping_id": integer("只修改这一条映射", minimum=1),
                    "title": string("目标片名", max_length=200),
                    "year": string("年份", max_length=10),
                    "season": integer("季号", minimum=0, maximum=99),
                    "episode": integer("集号", minimum=1, maximum=2000),
                    "media_type": string("电影或剧集", enum=["movie", "tv"], max_length=20),
                    "tmdb_id": {**integer("TMDB ID；不传保留，单条 null 解除绑定，批量禁止 null", minimum=1, maximum=2_147_483_647), "type": ["integer", "null"]},
                    "target_name": string("目标文件名；会同步推导 target_path", max_length=300),
                    "target_path": string("完整目标路径，优先于 target_name", max_length=1024),
                    "status": string("映射状态", enum=list(MAPPING_STATUSES), max_length=16),
                },
                required=["task_id"],
            ),
            scope=SCOPE_ORGANIZE_WRITE,
            risk=RISK_WRITE,
            handler=_update_mappings,
        )
    )
    registry.register(
        ToolSpec(
            name="apply_organizer_task",
            title="执行整理（移动/改名）",
            description=(
                "按当前身份计划执行真实改名、移动和身份附件写入，再请求 OpenList 异步刷新。"
                "AI 可按用户已授权规则自主审核，传 execute=true 即可一次调用执行，不要求用户逐次确认或预先获取 plan_hash。"
                "执行时仍用当前计划指纹和版本做原子校验；若提供 plan_hash 则必须严格匹配。"
                "显式 execute=false 始终只预览；不传 execute 时兼容携带 plan_hash 执行，两者都不传也只预览。需要 organize.apply 作用域（full 档位），未解决的审核问题仍会阻止执行。"
            ),
            input_schema=obj(
                {
                    "task_id": integer("整理任务编号", minimum=1),
                    "execute": boolean("调用方决定执行当前计划；AI 可依用户已授权规则设为 true", default=False),
                    "plan_hash": string("可选的已审核计划指纹；提供时严格匹配，不提供可用 execute=true 执行当前计划", max_length=64),
                },
                required=["task_id"],
            ),
            scope=SCOPE_ORGANIZE_APPLY,
            risk=RISK_DESTRUCTIVE,
            idempotent=False,
            handler=_apply_task,
        )
    )
    registry.register(
        ToolSpec(
            name="retry_organizer_task",
            title="重新识别或恢复整理任务",
            description=(
                "默认仅重新扫描识别并生成计划（auto_apply=false），不移动文件。"
                "AI 可按用户已授权规则自主传 execute=true 请求后台安全恢复整理，不要求用户逐次确认；"
                "此模式额外要求 organize.apply 权限，未解决的身份、季集或冲突问题不会自动变为 ready。"
            ),
            input_schema=obj({
                "task_id": integer("整理任务编号", minimum=1),
                "execute": boolean("调用方请求安全恢复执行；true 额外要求 organize.apply，false 仅重新识别", default=False),
            }, required=["task_id"]),
            scope=SCOPE_ORGANIZE_WRITE,
            risk=RISK_DESTRUCTIVE,
            idempotent=False,
            handler=_retry_task,
        )
    )
    registry.register(
        ToolSpec(
            name="rollback_organizer_run",
            title="回滚整理批次",
            description=(
                "按批次撤销已执行的改名与移动。confirm=true 表示调用方的执行意图，"
                "AI 可按用户已授权规则自主提供，不要求用户逐次点击确认；不可逆的覆盖冲突会直接失败。需要 organize.apply 权限。"
            ),
            input_schema=obj(
                {
                    "run_id": integer("整理批次编号", minimum=1),
                    "confirm": boolean("调用方的回滚执行意图；可由 AI 按用户已授权规则设为 true", default=False),
                },
                required=["run_id"],
            ),
            scope=SCOPE_ORGANIZE_APPLY,
            risk=RISK_DESTRUCTIVE,
            idempotent=False,
            handler=_rollback_run,
        )
    )


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------
def _task_of(context: AgentCallContext, task_id: int) -> dict[str, Any]:
    detail, status_code = context.services.organizer_task_detail(task_id)
    if status_code == 404 or not detail.get("success", False):
        raise not_found("整理任务")
    task = detail.get("task")
    if not isinstance(task, dict):
        raise not_found("整理任务")
    return task


def _update_mappings(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    task_id = _bounded_integer(arguments, "task_id", minimum=1, maximum=2_147_483_647)
    mapping_id = _bounded_integer(arguments, "mapping_id", minimum=1, maximum=2_147_483_647) if arguments.get("mapping_id") not in (None, "") else 0
    payload: dict[str, Any] = {}
    for name in _SINGLE_FIELDS:
        value = arguments.get(name)
        if value is None or value == "":
            continue
        if name == "season":
            payload[name] = _bounded_integer(arguments, name, minimum=0, maximum=99)
        elif name == "episode":
            payload[name] = _bounded_integer(arguments, name, minimum=1, maximum=2000)
        else:
            if not isinstance(value, str):
                raise invalid_params(f"{name} 必须是字符串")
            limit = {"title": 200, "year": 10, "media_type": 20, "target_name": 300}[name]
            normalized = optional_text(arguments, name, max_length=limit)
            if normalized:
                payload[name] = normalized
    if "media_type" in payload:
        payload["media_type"] = payload["media_type"].lower()
        if payload["media_type"] not in {"movie", "tv"}:
            raise invalid_params("media_type 只能是 movie 或 tv")
    if "target_name" in payload:
        name = payload["target_name"]
        if "/" in name or "\\" in name or ".." in name or name == "." or any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise invalid_params("target_name 必须是文件名，不能包含路径分隔符、.. 或控制字符")
    if "tmdb_id" in arguments:
        tmdb_id = arguments["tmdb_id"]
        if tmdb_id is None:
            if not mapping_id:
                raise invalid_params("批量修改不能用 null 解除 TMDB 绑定；请逐条确认修改")
            payload["tmdb_id"] = None
        elif not (isinstance(tmdb_id, str) and not tmdb_id.strip()):
            payload["tmdb_id"] = _bounded_integer(arguments, "tmdb_id", minimum=1, maximum=2_147_483_647)
    status = optional_text(arguments, "status", max_length=16)
    if status:
        if status not in MAPPING_STATUSES:
            raise invalid_params(f"status 只能是 {', '.join(MAPPING_STATUSES)}")
        payload["status"] = status
    target_path = optional_text(arguments, "target_path", max_length=1024)

    if not payload and not target_path:
        raise invalid_params(
            "没有要修改的字段",
            hint="至少提供 title / season / year / episode / media_type / tmdb_id / target_name / target_path / status 之一",
        )
    if not mapping_id:
        extra = sorted(set(payload) - set(_BATCH_FIELDS))
        if extra or target_path:
            raise invalid_params(
                f"批量修改只支持 title、season 与 tmdb_id，无法处理：{', '.join(extra) or 'target_path'}",
                hint="只想改其中一条时请传 mapping_id",
            )
    else:
        task = _task_of(context, task_id)
        mapping = next(
            (
                item
                for item in (task.get("mappings") or [])
                if isinstance(item, dict) and int(item.get("id") or 0) == mapping_id
            ),
            None,
        )
        if mapping is None:
            raise not_found("映射记录")
        if target_path:
            payload["target_path"] = target_path
        elif payload.get("target_name"):
            # The move decision uses target_path, so a bare filename change must
            # also rewrite the path; otherwise the file moves under the old name.
            current = str(mapping.get("target_path") or "").replace("\\", "/")
            directory = current.rsplit("/", 1)[0] if "/" in current else ""
            name = str(payload["target_name"]).strip()
            payload["target_path"] = f"{directory}/{name}" if directory else name

    body, status_code = writes.update_organizer_mappings(task_id, mapping_id, payload)
    if status_code >= 400 or not body.get("success", True):
        raise organizer_error(body, status_code, "映射未修改")
    task = body.get("task") if isinstance(body.get("task"), dict) else {}
    result = compact_write_result(body)
    if task:
        result["plan"] = organizer_plan_summary(task)
    return result


def _bounded_integer(arguments: dict[str, Any], name: str, *, minimum: int, maximum: int) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid_params(f"{name} 必须是整数")
    if not minimum <= value <= maximum:
        raise invalid_params(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _execute_requested(arguments: dict[str, Any]) -> bool:
    value = arguments.get("execute", False)
    if not isinstance(value, bool):
        raise invalid_params("execute 必须是布尔值")
    return value


def _plan_blockers(task: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    reasons = []
    if plan.get("blocked_count"):
        reasons.append("仍有身份、季集或目标冲突待修正；仅修改 ready 状态不能替代校验")
    if plan.get("missing_identity_count"):
        reasons.append("缺少作品身份计划，必须重新识别")
    if any(not identity.get("manifest_valid") or identity.get("errors_total") for identity in plan.get("identities") or []):
        reasons.append("作品身份信息无效或仍有审核错误，必须先修正")
    if any(isinstance(mapping, dict) and mapping.get("status") in {"delete_ad", "skipped_existing"} for mapping in task.get("mappings") or []):
        reasons.append("包含已停用的删除或重复源清理映射，必须重新识别")
    if any(not isinstance(operation, dict) or operation.get("type") not in _SAFE_OPERATION_TYPES for operation in task.get("operations") or []):
        reasons.append("包含不受支持的旧文件操作，必须重新识别")
    return reasons


def _apply_task(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    task_id = _bounded_integer(arguments, "task_id", minimum=1, maximum=2_147_483_647)
    supplied_hash = optional_text(arguments, "plan_hash", max_length=64)
    execute = _execute_requested(arguments)
    task = _task_of(context, task_id)
    plan = organizer_plan_summary(task)
    blockers = _plan_blockers(task, plan)

    if not execute and ("execute" in arguments or not supplied_hash):
        result: dict[str, Any] = {
            "confirmation_required": True,
            "human_confirmation_required": False,
            "executed": False,
            "plan": plan,
            "can_apply": not blockers,
        }
        if blockers:
            result["blocked_warning"] = "；".join(blockers)
        result["instruction"] = (
            "可按用户已授权规则自主审核此计划；符合规则时传 execute=true 执行，无需逐次询问用户。也可携带 plan_hash 锁定已审核版本。"
        )
        return result

    if supplied_hash and supplied_hash != plan["plan_hash"]:
        raise AgentError(
            "PLAN_CHANGED",
            "计划已变化，不能按旧确认执行",
            retryable=False,
            hint="重新读取当前计划并按用户已授权规则评估，不要沿用过期指纹执行",
            details={"current_plan_hash": plan["plan_hash"], "plan": plan},
        )

    if blockers:
        raise AgentError(
            "STATE_CONFLICT",
            "整理计划仍被阻止：" + "；".join(blockers),
            hint="修正身份、季集与冲突原因或重新识别，重新读取计划并确认新的 plan_hash",
            details={"plan": plan},
        )

    body, status_code = writes.apply_organizer_task(task_id, plan.get("revision"), plan["plan_hash"])
    if status_code >= 400 or not body.get("success", True):
        raise organizer_error(body, status_code, "整理未执行")
    result = compact_write_result(body)
    result["plan_hash"] = plan["plan_hash"]
    result["next"] = "用 get_organizer_task 或 list_media_libraries 确认整理结果"
    return result


def _retry_task(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    task_id = _bounded_integer(arguments, "task_id", minimum=1, maximum=2_147_483_647)
    execute = _execute_requested(arguments)
    if execute:
        context.require(SCOPE_ORGANIZE_APPLY)
        body, status_code = writes.retry_organizer_task(task_id, True)
    else:
        body, status_code = writes.retry_organizer_task(task_id)
    result = _organizer_result(body, status_code, "恢复整理失败" if execute else "重新识别整理计划失败")
    result["mode"] = "autonomous_retry" if execute else "rescan_only"
    result["next"] = (
        "读取任务状态跟踪安全恢复结果；若仍待审核，按用户已授权规则修正原因，不能仅改为 ready"
        if execute else "读取新的整理计划；按用户已授权规则修正待审核项后，可用 apply_organizer_task execute=true 自主执行"
    )
    if isinstance(body.get("task"), dict):
        result["plan"] = organizer_plan_summary(body["task"])
    return result


def _rollback_run(context: AgentCallContext, arguments: dict[str, Any]) -> dict[str, Any]:
    writes = writes_of(context)
    require_confirm(arguments, action="回滚整理批次")
    body, status_code = writes.rollback_organizer_run(_bounded_integer(arguments, "run_id", minimum=1, maximum=2_147_483_647))
    return _organizer_result(body, status_code, "回滚失败")


def _organizer_result(body: dict[str, Any], status_code: int, fallback: str) -> dict[str, Any]:
    if status_code >= 400 or not body.get("success", True):
        raise organizer_error(body, status_code, fallback)
    return compact_write_result(body)


__all__ = ["register"]
