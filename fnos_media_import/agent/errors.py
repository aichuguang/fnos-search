"""Tool failures with machine-readable codes an agent can react to.

The MCP layer reports these as ``isError`` tool results instead of protocol
errors, because a model can usually self-correct a bad argument but cannot
recover from a transport-level failure.
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """A recoverable or reportable tool failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        hint: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.hint = hint
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload


def invalid_params(message: str, hint: str = "") -> AgentError:
    return AgentError("INVALID_PARAMS", message, hint=hint)


def missing_argument(name: str, hint: str = "") -> AgentError:
    return AgentError(
        "INVALID_PARAMS",
        f"缺少必填参数 {name}",
        hint=hint or f"请在参数里补上 {name}",
    )


def scope_denied(scope: str) -> AgentError:
    return AgentError(
        "SCOPE_DENIED",
        f"当前令牌没有 {scope} 权限",
        hint="请在管理后台为这个 Agent 令牌追加对应作用域",
    )


def not_found(what: str) -> AgentError:
    return AgentError("NOT_FOUND", f"{what}不存在", hint=f"确认 {what}编号是否正确")


def disabled() -> AgentError:
    return AgentError(
        "AGENT_DISABLED",
        "Agent 接口未启用",
        hint="在部署环境设置 FNOS_AGENT_ENABLED=true 后重启 Web 容器",
    )


def server_busy(limit: int) -> AgentError:
    return AgentError(
        "SERVER_BUSY",
        "Agent 并发已满，请稍后重试",
        retryable=True,
        hint=f"当前并发上限 {limit}，可稍后重试或分批调用",
    )


def rate_limited(retry_after_seconds: int) -> AgentError:
    return AgentError(
        "RATE_LIMITED",
        "调用过于频繁",
        retryable=True,
        hint=f"约 {max(1, retry_after_seconds)} 秒后重试",
        details={"retry_after_seconds": max(1, retry_after_seconds)},
    )


def organizer_error(body: dict[str, Any], status_code: int, fallback: str) -> AgentError:
    """Preserve actionable domain refusals without suggesting blind retries."""
    code = str(body.get("code") or ("STATE_CONFLICT" if status_code == 409 else "REJECTED"))
    if status_code == 404:
        code = "NOT_FOUND"
    hints = {
        "PLAN_CHANGED": "重新读取计划并按调用方规则评估；可用 execute=true 执行当前版本，不沿用旧指纹",
        "EXECUTION_CONFIRMATION_REQUIRED": "保留现有恢复计划；有执行权限的 AI 可按用户规则使用 apply(execute=true)、retry(execute=true) 或 rollback(confirm=true)",
        "REVIEW_REQUIRED": "先读取审核原因并修正身份或季集，不要直接重复执行",
        "IDENTITY_REQUIRED": "先重新识别生成身份计划，不允许原名直通入库",
        "STATE_CONFLICT": "重新读取任务状态；不要自动循环重试",
    }
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    safe_details = {key: details[key] for key in ("current_plan_hash", "current_revision", "retry_after_seconds", "next_action") if key in details}
    for key in ("current_revision", "retry_after_seconds"):
        if key in body:
            safe_details[key] = body[key]
    retryable = code in {"SERVER_BUSY", "RATE_LIMITED", "TEMPORARILY_UNAVAILABLE"} and body.get("retryable") is True
    return AgentError(
        code, str(body.get("message") or fallback)[:1000], retryable=retryable,
        hint=str(body.get("hint") or hints.get(code) or "读取当前任务详情，解决原因后再操作")[:1000],
        details=safe_details,
    )


def translate(exc: BaseException) -> AgentError:
    """Map a domain exception to a stable tool error without importing it."""

    if isinstance(exc, AgentError):
        return exc
    name = type(exc).__name__
    text = str(exc) or name
    if name in {"IdentityReviewRequired", "IdentityError", "DuplicateIdentity", "LibraryScanError"}:
        return AgentError("REVIEW_REQUIRED", text[:1000], retryable=False,
                          hint="读取身份、目标目录和审核原因；修正问题或恢复目录后重新识别", details={"type": name})
    if name == "OpenListTransientError":
        return AgentError("TEMPORARILY_UNAVAILABLE", text[:500], retryable=True,
                          hint="等待后读取状态再重试；不要并发重复提交", details={"retry_after_seconds": 30})
    if name == "TmdbLookupError":
        return AgentError("METADATA_UNAVAILABLE", text[:500], retryable=False,
                          hint="检查 TMDB 可用性后重新识别，不要用 local 身份绕过失败")
    if name in {"OrganizerRunLeaseLost", "OrganizerScanLeaseLost", "WorkerTaskConflictError"}:
        return AgentError("STATE_CONFLICT", text[:500], retryable=False, hint="读取最新任务状态，不自动接管其他执行器")
    if name in {"SearchProvidersUnavailableError", "SearchServiceUnavailableError"}:
        return AgentError(
            "SEARCH_UNAVAILABLE",
            "搜索源暂不可用",
            retryable=True,
            hint="稍后重试，或检查后台的搜索源配置",
            details={"type": name},
        )
    if name in {"ActiveProviderUnavailable", "ProviderUnavailable"}:
        return AgentError(
            "PROVIDER_UNAVAILABLE",
            text,
            retryable=False,
            hint="在管理后台检查并激活对应网盘账号",
            details={"type": name},
        )
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return AgentError("INVALID_PARAMS", text, hint="检查参数名与取值")
    if isinstance(exc, PermissionError):
        return AgentError("FORBIDDEN", text)
    return AgentError(
        "INTERNAL_ERROR",
        "工具执行失败",
        retryable=False,
        hint="查看 Web 日志中的 agent 错误详情",
        details={"type": name, "message": text[:300]},
    )
