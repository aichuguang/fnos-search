"""MCP method dispatch.

``handle`` takes one decoded JSON-RPC message plus the authenticated token and
returns the response object, or ``None`` for notifications.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..args import optional_text
from ..audit import AgentAuditLog
from ..auth import SCOPE_OPS_READ, SCOPE_SEARCH, AgentTokenRecord
from ..context import AgentCallContext
from ..errors import AgentError, disabled, translate
from ..registry import ToolRegistry
from ..services import AgentServices
from . import protocol

logger = logging.getLogger(__name__)

SERVER_NAME = "fnos-search-agent"
SERVER_VERSION = "0.3.0"

INSTRUCTIONS = (
    "这是飞牛影视统一入库维护系统的能力接口。典型流程：search_resources 搜索 → "
    "get_resource/list_resource_files 确认文件 → submit_import 提交入库 → "
    "get_job 轮询任务 → list_organizer_tasks 查看整理状态 → "
    "list_subscriptions 管理定时追更。"
    "写工具会真实产生业务变更：只有带 dry_run 参数的写工具在 dry_run=true 时才只预览；"
    "具备 organize.apply 的 Agent 可按用户规则自主审核，用 apply_organizer_task(execute=true) 单次执行；"
    "服务端仍绑定当次计划指纹并校验身份、路径和冲突。显式 execute=false 始终只预览；未给 execute 时兼容旧 plan_hash 执行流程，两者都未给时也只预览。"
    "retry_organizer_task(execute=true) 需要 organize.apply，可重新识别或续跑；默认 execute=false 仅重新识别，不移动文件。"
    "rollback_organizer_run 的 confirm=true 表示调用方明确的回滚意图，Agent 可按用户规则提供。是否询问人由用户的 Agent 规则决定，不强制逐次人工确认。"
    "后台 waiting_review 或“人工确认”是业务待审核状态，不代表只能由人类操作；有权限的 AI 可修正映射并执行，仅在用户规则要求或确实无法判断时询问。"
    "详情支持文件、映射和阻塞项分页，应读完相关分页；审核和身份错误先读证据再修正，不要无条件循环重试。没有跳过身份整理的入口。"
)


RESOURCE_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "uri": "fnos://config/categories",
        "name": "分类目录",
        "description": "当前系统启用的影视分类 key、标签与目录映射",
        "mimeType": "application/json",
    },
    {
        "uri": "fnos://status/system",
        "name": "系统状态",
        "description": "数据库、Worker、rclone、调度器的聚合健康状态",
        "mimeType": "application/json",
    },
    {
        "uri": "fnos://status/scheduler",
        "name": "调度器状态",
        "description": "追更调度器的启用状态、下次运行时间与最近一次结果",
        "mimeType": "application/json",
    },
)

# A read-only resource must not bypass the scope of the equivalent tool.
RESOURCE_SCOPES: dict[str, str] = {
    "fnos://config/categories": SCOPE_SEARCH,
    "fnos://status/system": SCOPE_OPS_READ,
    "fnos://status/scheduler": SCOPE_OPS_READ,
}


PROMPT_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "ingest_by_title",
        "title": "按片名入库",
        "description": "搜索资源、确认文件、提交入库并跟踪任务，直到整理完成",
        "arguments": [
            {"name": "title", "description": "要入库的影视名称", "required": True},
            {"name": "category", "description": "目标分类 key（可选）", "required": False},
        ],
    },
    {
        "name": "organize_pending",
        "title": "处理待整理任务",
        "description": "按用户规则自主审核等待整理的任务，核对身份、改名与分类后执行",
        "arguments": [
            {"name": "category", "description": "只看某个分类（可选）", "required": False},
        ],
    },
    {
        "name": "follow_series",
        "title": "建立追更订阅",
        "description": "根据片名和更新节奏创建定时追更订阅，并确认下次运行时间",
        "arguments": [
            {"name": "title", "description": "要追更的剧集名称", "required": True},
            {"name": "cadence", "description": "更新节奏，如 daily/weekly/每 6 小时", "required": False},
        ],
    },
)


class McpHandler:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        services: AgentServices,
        token_store: Any,
        audit: AgentAuditLog | None = None,
        enabled: bool = True,
    ) -> None:
        self._registry = registry
        self._services = services
        self._token_store = token_store
        self._audit = audit
        self._enabled = enabled

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def handle(self, payload: dict[str, Any], token: AgentTokenRecord) -> dict[str, Any] | None:
        request_id = payload.get("id")
        if not self._enabled:
            return protocol.failure(request_id, protocol.INTERNAL_ERROR, disabled().message, disabled().to_payload())
        envelope_error = protocol.validate_request(payload)
        if envelope_error:
            return protocol.failure(request_id, protocol.INVALID_REQUEST, envelope_error)

        method = str(payload.get("method") or "")
        params = payload.get("params") or {}
        if isinstance(params, list):
            params = {}

        if protocol.is_notification(payload):
            # Notifications never receive a response; only side effects matter.
            if method == "notifications/initialized":
                return None
            logger.debug("ignoring agent notification: %s", method)
            return None

        try:
            if method == "initialize":
                return protocol.success(request_id, self._initialize(params))
            if method == "ping":
                return protocol.success(request_id, {})
            if method == "tools/list":
                return protocol.success(
                    request_id,
                    {"tools": self._registry.describe(token.scopes)},
                )
            if method == "tools/call":
                return protocol.success(request_id, self._tools_call(params, token))
            if method == "resources/list":
                return protocol.success(
                    request_id,
                    {
                        "resources": [
                            dict(item)
                            for item in RESOURCE_DEFINITIONS
                            if RESOURCE_SCOPES.get(item["uri"]) in token.scopes
                        ]
                    },
                )
            if method == "resources/read":
                return protocol.success(request_id, self._resources_read(params, token))
            if method == "prompts/list":
                return protocol.success(
                    request_id,
                    {"prompts": [dict(item) for item in PROMPT_DEFINITIONS]},
                )
            if method == "prompts/get":
                return protocol.success(request_id, self._prompts_get(params))
            if method == "logging/setLevel":
                return protocol.success(request_id, {})
        except AgentError as exc:
            # Tool execution errors are reported inside tools/call; anything that
            # reaches here is a bad method argument, so map it to -32602.
            return protocol.failure(request_id, protocol.INVALID_PARAMS, exc.message, exc.to_payload())
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent mcp method failed: %s", method)
            mapped = translate(exc)
            return protocol.failure(request_id, protocol.INTERNAL_ERROR, mapped.message, mapped.to_payload())

        return protocol.failure(request_id, protocol.METHOD_NOT_FOUND, f"未知方法：{method}")

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------
    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or "")
        version = requested if requested in protocol.SUPPORTED_PROTOCOL_VERSIONS else protocol.DEFAULT_PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    def _tools_call(self, params: dict[str, Any], token: AgentTokenRecord) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        if not name:
            raise AgentError("INVALID_PARAMS", "缺少工具名称 tool name")
        spec = self._registry.get(name)
        if spec is None:
            raise AgentError(
                "UNKNOWN_TOOL",
                f"未知工具：{name}",
                hint="调用 tools/list 查看当前令牌可用的工具",
            )
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise AgentError("INVALID_PARAMS", "arguments 必须是对象")

        if spec.scope not in token.scopes:
            payload = AgentError(
                "SCOPE_DENIED",
                f"当前令牌没有 {spec.scope} 权限",
                hint="在管理后台为该令牌追加作用域后重试",
            ).to_payload()
            self._record(token, spec, arguments, "denied", "SCOPE_DENIED", 0)
            return self._tool_result(payload, is_error=True)

        context = AgentCallContext(
            token_id=token.id,
            token_name=token.name,
            scopes=token.scopes,
            services=self._services,
        )
        started = time.perf_counter()
        try:
            payload = spec.handler(context, arguments)
            elapsed = int((time.perf_counter() - started) * 1000)
            self._record(token, spec, arguments, "ok", "", elapsed)
            self._token_store.touch(token.id)
            return self._tool_result(payload, is_error=False)
        except AgentError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            self._record(token, spec, arguments, "error", exc.code, elapsed)
            return self._tool_result(exc.to_payload(), is_error=True)
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.perf_counter() - started) * 1000)
            mapped = translate(exc)
            logger.warning("agent tool %s failed: %s", spec.name, exc, exc_info=True)
            self._record(token, spec, arguments, "error", mapped.code, elapsed)
            return self._tool_result(mapped.to_payload(), is_error=True)

    def _resources_read(self, params: dict[str, Any], token: AgentTokenRecord) -> dict[str, Any]:
        uri = str(params.get("uri") or "").strip()
        required_scope = RESOURCE_SCOPES.get(uri)
        if required_scope is None:
            raise AgentError("NOT_FOUND", f"未知资源：{uri}")
        if required_scope not in token.scopes:
            raise AgentError(
                "SCOPE_DENIED",
                f"当前令牌没有读取该资源所需的 {required_scope} 权限",
                hint="在管理后台为该令牌追加作用域后重试",
            )
        try:
            if uri == "fnos://config/categories":
                body: Any = self._services.categories()
            elif uri == "fnos://status/system":
                body = self._services.system_status()
            else:
                body = self._services.scheduler_status()
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise translate(exc) from exc
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(body, ensure_ascii=False, indent=2, default=str),
                }
            ]
        }

    def _prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        title = optional_text(arguments, "title", max_length=200)
        category = optional_text(arguments, "category", max_length=40)

        if name == "ingest_by_title":
            if not title:
                raise AgentError("INVALID_PARAMS", "ingest_by_title 需要 title 参数")
            text = (
                f"请帮我把《{title}》入库" + (f"到分类 {category}" if category else "") + "。\n"
                "1. 调用 search_resources 搜索该片名，挑出清晰度与体量合理的候选。\n"
                "2. 对选定候选调用 get_resource，必要时用 list_resource_files 展开子目录。\n"
                "3. 调用 submit_import 提交，记录返回的 request_token 与 job_id。\n"
                "4. 用 get_job 查看状态；review/failed 时先读原因，不自动循环重试或把 queued 当作已完成。\n"
                "5. 用 list_organizer_tasks 查看整理任务；具备 full 权限时按我的 Agent 规则自主审核并执行，不把排队视为完成。"
            )
        elif name == "organize_pending":
            text = (
                "请检查当前等待整理的入库任务"
                + (f"（分类 {category}）" if category else "")
                + "：\n"
                "1. list_organizer_tasks 拉取待处理任务。\n"
                "2. 对每个任务调用 get_organizer_task，按 files_page/mappings_page/blocked_page 读完相关分页，核对身份、目录、季集和审核原因。\n"
                "waiting_review 或界面上的“人工确认”只是业务待审核状态，不限制为人类操作；有权限的 AI 可以自主处理。\n"
                "3. 用 update_organizer_mappings 修正 TMDB ID、标题或季集；只改 ready 不能替代身份校验。证据不足或我的 Agent 规则要求时才询问我。\n"
                "4. 具备 full 权限时，可按我的规则用 apply_organizer_task(execute=true) 单次执行；也可自行预览并用 plan_hash 执行，无需固定增加人工确认步骤。\n"
                "5. retry_organizer_task(execute=true) 可重新识别或续跑；默认 execute=false 只生成计划。不要用重建覆盖未完成的执行或回滚记录。\n"
                "6. 回滚用 rollback_organizer_run(confirm=true) 表达调用方意图，可按我的规则自主调用；核对实际完成状态后再汇报。"
            )
        elif name == "follow_series":
            text = (
                f"请为《{title}》建立定时追更订阅。\n"
                "1. 先用 resolve_tmdb 确认 TMDB 身份，避免同名误追。\n"
                "2. 用 list_subscriptions 检查是否已有同一作品的订阅。\n"
                "3. 可用 create_subscription(dry_run=true) 检查 next_run_at 和来源策略，按我的 Agent 规则判断是否需要询问。\n"
                "4. 在授权范围内正式创建，并记住 subscription_id 便于后续 run_subscription_now 或暂停。"
            )
        else:
            raise AgentError("NOT_FOUND", f"未知提示词：{name}")

        return {
            "description": f"prompt {name}",
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": text},
                }
            ],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _tool_result(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": bool(is_error),
        }

    def _record(self, token: AgentTokenRecord, spec: Any, arguments: dict[str, Any], outcome: str, error_code: str, elapsed_ms: int) -> None:
        if self._audit is None:
            return
        self._audit.record(
            token_id=token.id,
            token_name=token.name,
            tool=spec.name,
            scope=spec.scope,
            risk=spec.risk,
            arguments=arguments,
            outcome=outcome,
            error_code=error_code,
            duration_ms=elapsed_ms,
        )
