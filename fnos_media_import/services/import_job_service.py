"""公共流程 v4 接入：访客不选择保存路径。

`ImportJobCreationService.create` 在服务端解析来源（SourceDescriptor）与
管理员通过 source_destination_rules 分配的目标，解析激活账号，调用 v4
Planner 冻结执行计划并持久化。访客提交的任何目标/账号/路径字段都在进入
业务层前被忽略；没有任何可用目标时任务失败关闭，绝不回退默认盘。

`ImportJobRetryService.retry` 只接受 v4 计划；旧版本一律返回
UNSUPPORTED_PLAN_VERSION，且不从当前配置重建计划。

v4 规划依赖（SourceResolver / DestinationResolver / plan_import /
validate_v4_plan）由并行 Agent 提供的 planning 包实现。这里以构造函数注入
为主，未注入时通过受控的惰性导入加载；planning 尚未就绪时错误会清晰暴露，
不会被静默吞掉。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable

from ..constants import (
    EVENT_ERROR,
    EVENT_INFO,
    EVENT_WARN,
    JOB_CANCELLED,
    JOB_CHECKING,
    JOB_CONFIRMING,
    JOB_CREATED,
    JOB_DONE,
    JOB_FAILED,
    JOB_ORGANIZING,
    JOB_PROVIDER_SUBMITTING,
    JOB_REFRESHING,
    JOB_REVIEW,
    JOB_SUBMITTED,
    JOB_TRANSFERRING,
    JOB_UNSUPPORTED,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
    JOB_WAITING_TRANSFER,
)
from ..domain.models import EXECUTION_PLAN_VERSION, DestinationDescriptor, SourceDescriptor


class V4PlanningError(ValueError):
    """v4 规划失败且必须失败关闭（不生成任务、不回退默认盘）。"""

    def __init__(self, message: str, code: str = "PLANNING_FAILED") -> None:
        super().__init__(message)
        self.code = code


# 访客/调用方提交时不允许携带的“基础设施”字段。进入业务层前被忽略，
# 绝不参与来源识别、目标解析或执行计划。
GUEST_FORBIDDEN_FIELDS = frozenset(
    {
        "destination_id",
        "account_id",
        "provider_kind",
        "driver",
        "backend",
        "remote",
        "mount_id",
        "root",
        "absolute_path",
        "target_path",
        "target_path_override",
        "cloud139_target_path_override",
        "options_revision",
        "context",
        "update_context",
        "organizer_context",
        "target_root_is_resource",
    }
)

PROVIDER_SUBMISSION_FENCE_KEY = "provider_submission_fence"
PROVIDER_SUBMISSION_FENCE_VERSION = 1
PROVIDER_RETRYABLE_JOB_STATUSES = frozenset({JOB_CREATED, JOB_FAILED, JOB_UNSUPPORTED})
PROVIDER_POST_SUBMISSION_JOB_STATUSES = frozenset(
    {
        JOB_CHECKING,
        JOB_SUBMITTED,
        JOB_WAITING_TRANSFER,
        JOB_TRANSFERRING,
        JOB_WAITING_OPENLIST,
        JOB_WAITING_ORGANIZER,
        JOB_ORGANIZING,
        JOB_CONFIRMING,
        JOB_REVIEW,
        JOB_REFRESHING,
        JOB_DONE,
    }
)


def _load_planning_dependency(name: str) -> Any:
    """Lazily import a v4 planning dependency.

    The planning package is owned by the parallel v4 build.  If a module is
    not yet present, the ImportError is surfaced as a clear
    ``V4PlanningError`` instead of being swallowed or silently falling back.
    """
    try:
        if name == "source_resolver":
            from ..planning.source_resolver import SourceResolver

            return SourceResolver
        if name == "destination_resolver":
            from ..planning.destination_resolver import DestinationResolver

            return DestinationResolver
        if name == "plan_import":
            from ..planning.planner import plan_import

            return plan_import
        if name == "validate_v4_plan":
            from ..planning.plan_validator import validate_v4_plan

            return validate_v4_plan
    except ImportError as exc:
        raise V4PlanningError(
            f"v4 规划依赖 planning.{name} 尚未就绪：{exc}",
            code="PLANNING_UNAVAILABLE",
        ) from exc
    raise V4PlanningError(f"未知的 v4 规划依赖：{name}", code="PLANNING_UNAVAILABLE")


def _account_id(account: Any) -> str:
    if isinstance(account, (tuple, list)) and len(account) >= 1:
        return str(account[0] or "").strip()
    return str((account or {}).get("account_id") or "").strip()


def _account_revision(account: Any) -> int:
    if isinstance(account, (tuple, list)) and len(account) >= 2:
        return _to_nonneg_int(account[1])
    return _to_nonneg_int((account or {}).get("account_revision"))


def _selection_revision(account: Any) -> int:
    if isinstance(account, (tuple, list)) and len(account) >= 3:
        return _to_nonneg_int(account[2])
    return _to_nonneg_int((account or {}).get("selection_revision"))


def _to_nonneg_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _account_dict(account: Any) -> dict[str, Any]:
    if not account:
        return {}
    return {
        "account_id": _account_id(account),
        "account_revision": _account_revision(account),
        "selection_revision": _selection_revision(account),
    }


def _plan_target_path(plan: dict[str, Any]) -> str:
    paths = plan.get("paths") if isinstance(plan.get("paths"), dict) else {}
    for key in ("destination_final", "final"):
        ref = paths.get(key) if isinstance(paths.get(key), dict) else {}
        path = str(ref.get("path") or "").strip()
        if path:
            return path
    return ""


class ImportJobCreationService:
    """Creates the durable v4 import record from a server-resolved plan.

    The guest never selects a save path.  The source is resolved from the
    trusted search-cache record (or re-detected from the validated URL), the
    destination is resolved from admin ``source_destination_rules``, the
    active accounts are frozen, and the v4 Planner produces an immutable
    execution plan that is validated and persisted with the job.
    """

    def __init__(
        self,
        *,
        database: Any,
        config: Any,
        detect_link: Callable[..., Any] | None = None,
        job_source_url: Callable[[str, dict[str, Any]], str] | None = None,
        target_path: Callable[..., str] | None = None,
        staging_plan: Callable[..., dict[str, Any]] | None = None,
        submit_quark: Callable[..., dict[str, Any]] | None = None,
        submit_cloud139: Callable[..., dict[str, Any]] | None = None,
        submit_generic: Callable[..., dict[str, Any]] | None = None,
        source_resolver: Any | None = None,
        destination_resolver: Any | None = None,
        plan_import: Callable[..., dict[str, Any]] | None = None,
        validate_v4_plan: Callable[..., Any] | None = None,
        plan_validation_deps: Any | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.detect_link = detect_link
        self.job_source_url = job_source_url or (lambda url, _payload: url)
        self.target_path = target_path
        self.staging_plan = staging_plan
        self.submit_quark = submit_quark
        self.submit_cloud139 = submit_cloud139
        self.submit_generic = submit_generic
        # v4 planning dependencies.  Injected by tests / the integration layer;
        # defaults are lazily imported from the planning package.
        self.source_resolver = source_resolver
        self.destination_resolver = destination_resolver
        self.plan_import = plan_import
        self.validate_v4_plan = validate_v4_plan
        self.plan_validation_deps = plan_validation_deps

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        # 访客提交不体现、不选择保存位置：任何目标/账号/路径/凭证形状字段
        # 都在进入业务层前被丢弃，绝不影响来源识别或目标解析。
        for field in GUEST_FORBIDDEN_FIELDS:
            payload.pop(field, None)

        title = str(payload.get("title") or "未命名资源").strip()
        url = str(payload.get("url") or payload.get("source_url") or "").strip()
        if not url:
            raise ValueError("资源链接不能为空")
        password = str(payload.get("password") or "")
        category_key = str(payload.get("category") or "movie")
        category = self.config.category(category_key)
        category_label = category.get("label", category_key)

        # (1) 服务端识别来源，不信任客户端 provider 字段。
        source = self._resolve_source(record=self._build_source_record(payload))
        # (2) 管理员通过 source_destination_rules 分配目标；0 个可用目标
        #     必须失败关闭，绝不为空目标回退默认盘。
        destination = self._resolve_destination(source, category_key)
        if destination is None:
            raise V4PlanningError(
                "该资源当前没有可用的保存位置，请联系管理员为该来源配置入库目标",
                code="SOURCE_DESTINATION_NOT_CONFIGURED",
            )
        # (3) 解析并冻结来源与目标激活账号。
        source_account = self._resolve_source_account(source)
        destination_account = self._resolve_destination_account(destination)
        selected_items_hash = str(source.selected_items_hash or "")

        idempotency_key = self._v4_idempotency_key(
            payload,
            source=source,
            destination=destination,
            source_account=source_account,
            destination_account=destination_account,
            selected_items_hash=selected_items_hash,
        )
        source_url = self.job_source_url(url, payload)
        raw_data = {
            "request": payload,
            "source": self._source_dict(source),
            "destination": self._destination_dict(destination),
            "source_account": _account_dict(source_account),
            "destination_account": _account_dict(destination_account),
            "selected_items_hash": selected_items_hash,
            "idempotency_identity": idempotency_key,
            PROVIDER_SUBMISSION_FENCE_KEY: {
                "version": PROVIDER_SUBMISSION_FENCE_VERSION,
                "state": "not_started",
                "attempt": 0,
            },
        }
        job_id, created = self.database.create_job(
            {
                "title": title,
                "category": category_key,
                "category_label": category_label,
                "source_type": source.provider_kind,
                "source_url": source_url,
                "password": password,
                "target_route": _source_route(source),
                "target_path": "",
                "status": JOB_CREATED,
                "raw_data": raw_data,
                "idempotency_key": idempotency_key,
                "config_revision": payload.get("config_revision") or 1,
                "executor_id": payload.get("executor_id") or "web",
                "destination_id": destination.destination_id,
                "plan_version": EXECUTION_PLAN_VERSION,
            }
        )
        if not created:
            job = self.database.get_job(job_id) or {}
            return {
                "job": job,
                "created": False,
                "message": "该资源和分类已存在入库任务",
                "idempotency_key": str(job.get("idempotency_key") or ""),
            }
        try:
            # (4) v4 Planner 冻结唯一执行策略。
            plan = self._plan_import(
                source=source,
                destination=destination,
                category_key=category_key,
                category=category,
                job_id=job_id,
                request_payload=payload,
            )
            # (5) 校验器与生成器执行相同约束。
            self._validate_v4_plan(plan, job_record=self.database.get_job(job_id) or {})
            # (6) 固化 v4 计划与 destination_id / plan_version。
            target_path = _plan_target_path(plan)
            if not _update_created_job(
                self.database,
                job_id,
                target_path=target_path,
                raw_data={**raw_data, "execution_plan": plan},
            ):
                raise V4PlanningError(
                    "任务状态已变化，未固化 v4 执行计划", code="PLANNING_CONFLICT"
                )
        except Exception as exc:  # noqa: BLE001
            message = str(exc) if isinstance(exc, V4PlanningError) else f"v4 执行计划失败：{exc}"
            _mark_creation_failed(self.database, job_id, message)
            self.database.add_event(job_id, EVENT_ERROR, message)
            return {
                "job": self.database.get_job(job_id),
                "created": True,
                "message": message,
                "success": False,
                "plan_error": True,
                "idempotency_key": idempotency_key,
            }

        self.database.add_event(job_id, EVENT_INFO, f"创建入库任务：{title}")
        self.database.add_event(
            job_id,
            EVENT_INFO,
            f"已解析保存目标：{destination.destination_id}（{destination.provider_kind}）",
            self._destination_dict(destination),
        )
        return {
            "job": self.database.get_job(job_id),
            "created": True,
            "message": "已创建入库任务",
            "success": True,
            "idempotency_key": idempotency_key,
        }

    def _raw_config(self) -> dict[str, Any]:
        return getattr(self.config, "raw", {}) if self.config is not None else {}

    def _organizer_enabled(self) -> bool:
        raw = self._raw_config()
        organizer = raw.get("organizer") if isinstance(raw.get("organizer"), dict) else {}
        return bool(organizer.get("enabled", False))

    def _build_source_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Build the trusted source record for the v4 SourceResolver.

        Prefers the search-cache record (server-trusted).  For manual URL
        submissions without a cache entry, a minimal record is synthesized
        from the validated payload; the resolver never trusts client provider
        fields.
        """
        cached = self._cached_record(payload)
        if cached:
            return cached
        return {
            "url": str(payload.get("url") or payload.get("source_url") or "").strip(),
            "password": str(payload.get("password") or ""),
            "raw_data": payload,
        }

    def _cached_record(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        public_id = str(payload.get("public_id") or payload.get("resource_id") or "").strip()
        if not public_id:
            return None
        getter = getattr(self.database, "get_search_cache", None)
        if not callable(getter):
            return None
        try:
            record = getter(public_id)
        except Exception:  # noqa: BLE001
            return None
        return record if isinstance(record, dict) else None

    def _source_resolver(self) -> Any:
        if self.source_resolver is not None:
            return self.source_resolver
        resolver_cls = _load_planning_dependency("source_resolver")
        detect_link = self.detect_link
        if detect_link is None:
            from ..classifiers.link_classifier import detect_link
        return resolver_cls(
            detect_link=detect_link,
            config=self._raw_config(),
        )

    def _destination_resolver(self) -> Any:
        if self.destination_resolver is not None:
            return self.destination_resolver
        resolver_cls = _load_planning_dependency("destination_resolver")
        # DestinationResolver 需要 rules/target/accounts 服务，必须由集成层注入；
        # 这里只加载类以清晰暴露依赖缺口。
        raise V4PlanningError(
            "v4 目标解析器 DestinationResolver 必须由集成层注入"
            "（rules_service / target_service / accounts_service）",
            code="PLANNING_UNAVAILABLE",
        )

    def _resolve_source(self, *, record: dict[str, Any]) -> SourceDescriptor:
        resolver = self._source_resolver()
        try:
            source = resolver.resolve(record)
        except V4PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V4PlanningError(
                f"无法解析入库来源：{exc}", code="SOURCE_UNRESOLVABLE"
            ) from exc
        if not isinstance(source, SourceDescriptor):
            raise V4PlanningError(
                "来源解析器返回了无效的来源描述", code="SOURCE_UNRESOLVABLE"
            )
        return source

    def _resolve_destination(
        self, source: SourceDescriptor, category_key: str
    ) -> DestinationDescriptor | None:
        resolver = self._destination_resolver()
        try:
            destination = resolver.resolve(source, category_key, audience="guest")
        except V4PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V4PlanningError(
                f"解析保存目标失败：{exc}", code="SOURCE_DESTINATION_NOT_CONFIGURED"
            ) from exc
        if destination is None:
            return None
        if not isinstance(destination, DestinationDescriptor):
            raise V4PlanningError(
                "目标解析器返回了无效的目标描述", code="PLANNING_FAILED"
            )
        return destination

    def _resolve_source_account(self, source: SourceDescriptor) -> Any:
        resolver = self._destination_resolver()
        try:
            return resolver.resolve_account(
                source.provider_kind, source.account_scope or "default", role="source"
            )
        except V4PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V4PlanningError(
                f"当前来源账号不可用：{exc}", code="SOURCE_ACCOUNT_UNAVAILABLE"
            ) from exc

    def _resolve_destination_account(self, destination: DestinationDescriptor) -> Any:
        if not destination.account_scope:
            # 本地/无账号目标不需要账号。
            return None
        resolver = self._destination_resolver()
        try:
            return resolver.resolve_account(
                destination.provider_kind, destination.account_scope, role="destination"
            )
        except V4PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V4PlanningError(
                f"保存位置暂不可用：{exc}", code="DESTINATION_ACCOUNT_UNAVAILABLE"
            ) from exc

    def _v4_idempotency_key(
        self,
        payload: dict[str, Any],
        *,
        source: SourceDescriptor,
        destination: DestinationDescriptor,
        source_account: dict[str, Any] | None,
        destination_account: dict[str, Any] | None,
        selected_items_hash: str,
    ) -> str:
        base = str(payload.get("idempotency_key") or "").strip()
        if not base:
            raw = f"v4|{source.canonical_source_id}|{str(payload.get('category') or '').strip()}"
            base = "v4:" + hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()
        parts = [
            f"src={source.canonical_source_id}",
            f"cat={str(payload.get('category') or '').strip()}",
            f"sel={selected_items_hash}",
            f"dest={destination.destination_id}",
            f"drev={int(destination.target_revision or 1)}",
        ]
        if source_account:
            parts.append(f"sa={_account_id(source_account)}@{_account_revision(source_account)}")
        if destination_account:
            parts.append(f"da={_account_id(destination_account)}@{_account_revision(destination_account)}")
        return f"{base}:{':'.join(parts)}"

    def _plan_import(
        self,
        *,
        source: SourceDescriptor,
        destination: DestinationDescriptor,
        category_key: str,
        category: dict[str, Any],
        job_id: int,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        planner = self.plan_import or _load_planning_dependency("plan_import")
        try:
            plan = planner(
                source=source,
                category_key=category_key,
                job_id=int(job_id),
                destination_id=destination.destination_id,
                audience="guest",
                organizer_enabled=self._organizer_enabled(),
                media_refresh_enabled=False,
            )
        except V4PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V4PlanningError(f"v4 计划生成失败：{exc}", code="PLANNING_FAILED") from exc
        if not isinstance(plan, dict) or not plan:
            raise V4PlanningError("v4 Planner 未返回执行计划", code="PLANNING_FAILED")
        return plan

    def _validate_v4_plan(
        self, plan: dict[str, Any], job_record: dict[str, Any]
    ) -> None:
        validator = self.validate_v4_plan or _load_planning_dependency("validate_v4_plan")
        try:
            if self.plan_validation_deps is not None:
                validator(plan, job_record, deps=self.plan_validation_deps)
            else:
                validator(plan, job_record)
        except V4PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V4PlanningError(
                f"v4 执行计划校验失败：{exc}", code="PLANNING_VALIDATION_FAILED"
            ) from exc

    @staticmethod
    def _source_dict(source: SourceDescriptor) -> dict[str, Any]:
        return {
            "provider_kind": source.provider_kind,
            "source_form": source.source_form,
            "canonical_source_id": source.canonical_source_id,
            "account_scope": source.account_scope,
            "selected_items_hash": source.selected_items_hash,
        }

    @staticmethod
    def _destination_dict(destination: DestinationDescriptor) -> dict[str, Any]:
        return {
            "destination_id": destination.destination_id,
            "provider_kind": destination.provider_kind,
            "account_scope": destination.account_scope,
            "target_revision": destination.target_revision,
        }


class ImportJobRetryService:
    """Only retries frozen v4 plans.

    Older plan versions are rejected with ``UNSUPPORTED_PLAN_VERSION``; the
    plan is never rebuilt from current configuration.  A v4 plan is validated
    before the durable submission fence is re-established for the executor.
    """

    def __init__(
        self,
        *,
        database: Any,
        config: Any,
        submit_quark: Callable[..., dict[str, Any]] | None = None,
        submit_cloud139: Callable[..., dict[str, Any]] | None = None,
        submit_generic: Callable[..., dict[str, Any]] | None = None,
        validate_v4_plan: Callable[..., Any] | None = None,
        plan_validation_deps: Any | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.submit_quark = submit_quark
        self.submit_cloud139 = submit_cloud139
        self.submit_generic = submit_generic
        self.validate_v4_plan = validate_v4_plan
        self.plan_validation_deps = plan_validation_deps

    def retry(self, job_id: int) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if not job:
            raise ValueError("任务不存在")
        try:
            persisted_plan_version = int(job.get("plan_version") or 0)
        except (TypeError, ValueError) as exc:
            message = f"任务 plan_version 无效，已停止重试：{exc}"
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
            }
        if persisted_plan_version != EXECUTION_PLAN_VERSION:
            message = (
                f"任务计划版本为 {persisted_plan_version or '未知'}，"
                f"仅支持执行 v{EXECUTION_PLAN_VERSION} 计划；"
                f"已拒绝重试，请按当前配置重新提交为新任务"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
                "unsupported_plan_version": True,
                "error_code": "UNSUPPORTED_PLAN_VERSION",
            }

        plan = self._persisted_plan(job)
        if not plan:
            message = "任务标记为 v4，但没有固化 execution_plan，已拒绝重试"
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
            }
        try:
            self._validate_v4_plan(plan, job_record=job)
        except V4PlanningError as exc:
            message = f"持久化 v4 执行计划无效，已拒绝重试：{exc}"
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
                "error_code": str(getattr(exc, "code", "PLANNING_VALIDATION_FAILED")),
            }

        current_status = str(job.get("status") or "").strip().lower()
        if current_status == JOB_CANCELLED:
            message = "任务已取消，如需重新入库请重新提交资源"
            self.database.add_event(job_id, EVENT_WARN, message)
            return {"job": job, "created": False, "message": message, "success": False}
        if current_status == JOB_PROVIDER_SUBMITTING or _provider_submission_is_ambiguous(job):
            message = (
                "任务已进入网盘提交栅栏，但尚未确认外部接口是否成功；"
                "为避免重复转存或重复离线下载，禁止自动重提，请人工核对"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
                "provider_submission_ambiguous": True,
            }
        if current_status == JOB_CREATED and not _has_not_started_provider_fence(job):
            message = (
                "该历史任务停留在已创建状态，但没有可证明尚未调用网盘接口的提交栅栏；"
                "为避免重复提交，已转为人工核对"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
                "provider_submission_ambiguous": True,
            }
        if current_status not in PROVIDER_RETRYABLE_JOB_STATUSES:
            stage_label = "已完成" if current_status == JOB_DONE else "已进入网盘提交后的处理阶段"
            message = (
                f"任务{stage_label}（当前状态：{current_status or '未知'}），"
                "通用重试不会再次调用网盘接口，请使用对应阶段的重试入口"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": current_status in PROVIDER_POST_SUBMISSION_JOB_STATUSES,
                "provider_retry_blocked": True,
            }

        self.database.add_event(job_id, EVENT_INFO, "手动重试 v4 任务")
        claimed, claimed_job, claim_message = _claim_provider_submission(
            self.database,
            job_id=job_id,
            expected_status=current_status,
            provider=str(job.get("source_type") or job.get("target_route") or "unknown"),
            require_not_started_fence=current_status == JOB_CREATED,
        )
        if not claimed:
            return {
                "job": claimed_job,
                "created": False,
                "message": claim_message,
                "success": False,
                "provider_submission_claim_failed": True,
                "retryable": True,
            }
        # v4 计划已冻结，执行器按计划恢复，不再从配置重建。
        self.database.add_event(
            job_id,
            EVENT_INFO,
            "v4 计划校验通过，已建立提交栅栏，等待 v4 执行器接管",
            {"execution_plan_version": EXECUTION_PLAN_VERSION},
        )
        return {
            "job": claimed_job,
            "created": False,
            "message": "v4 计划已校验并重新建立提交栅栏，等待执行器接管",
            "success": True,
            "v4_plan_ready": True,
            "idempotency_key": str(claimed_job.get("idempotency_key") or ""),
        }

    @staticmethod
    def _persisted_plan(job: dict[str, Any]) -> dict[str, Any]:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        plan = raw_data.get("execution_plan") if isinstance(raw_data.get("execution_plan"), dict) else {}
        return plan

    def _validate_v4_plan(
        self, plan: dict[str, Any], job_record: dict[str, Any]
    ) -> None:
        validator = self.validate_v4_plan or _load_planning_dependency("validate_v4_plan")
        try:
            if self.plan_validation_deps is not None:
                validator(plan, job_record, deps=self.plan_validation_deps)
            else:
                validator(plan, job_record)
        except V4PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V4PlanningError(
                f"v4 执行计划校验失败：{exc}", code="PLANNING_VALIDATION_FAILED"
            ) from exc


def _source_route(source: SourceDescriptor) -> str:
    return f"{source.provider_kind}:{source.source_form}"


def _claim_provider_submission(
    database: Any,
    *,
    job_id: int,
    expected_status: str,
    provider: str,
    require_not_started_fence: bool,
) -> tuple[bool, dict[str, Any], str]:
    current = database.get_job(job_id) or {}
    current_status = str(current.get("status") or "").strip().lower()
    normalized_expected = str(expected_status or "").strip().lower()
    if not current or current_status != normalized_expected:
        return False, current, "任务状态已变化，未开始调用网盘接口，请刷新后重试"
    if current_status == JOB_PROVIDER_SUBMITTING or _provider_submission_is_ambiguous(current):
        return False, current, "网盘提交结果尚未确认，已禁止重复调用接口"
    if require_not_started_fence and not _has_not_started_provider_fence(current):
        return (
            False,
            current,
            "任务缺少可证明网盘接口尚未调用的提交栅栏，已停止自动提交并等待人工核对",
        )
    updater = getattr(database, "update_job_if_status", None)
    if not callable(updater):
        return False, current, "任务存储不支持原子提交栅栏，已停止调用网盘接口"

    raw_data = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
    previous_fence = (
        raw_data.get(PROVIDER_SUBMISSION_FENCE_KEY)
        if isinstance(raw_data.get(PROVIDER_SUBMISSION_FENCE_KEY), dict)
        else {}
    )
    try:
        previous_attempt = max(0, int(previous_fence.get("attempt") or 0))
    except (TypeError, ValueError):
        previous_attempt = 0
    fence = {
        "version": PROVIDER_SUBMISSION_FENCE_VERSION,
        "state": "submitting",
        "attempt": previous_attempt + 1,
        "provider": str(provider or "unknown"),
        "previous_status": current_status,
        "started_at": _utc_now_text(),
    }
    claimed = bool(
        updater(
            job_id,
            {current_status},
            status=JOB_PROVIDER_SUBMITTING,
            error_message="",
            raw_data={**raw_data, PROVIDER_SUBMISSION_FENCE_KEY: fence},
        )
    )
    latest = database.get_job(job_id) or current
    if not claimed:
        return False, latest, "任务状态并发变化，未调用网盘接口，请刷新后重试"
    database.add_event(
        job_id,
        EVENT_INFO,
        "已建立网盘提交原子栅栏，开始调用外部接口",
        {"provider_submission_fence": fence},
    )
    return True, latest, ""


def _update_created_job(database: Any, job_id: int, **updates: Any) -> bool:
    updater = getattr(database, "update_job_if_status", None)
    if not callable(updater):
        return False
    return bool(updater(job_id, {JOB_CREATED}, **updates))


def _mark_creation_failed(
    database: Any,
    job_id: int,
    message: str,
    *,
    status: str = JOB_FAILED,
) -> bool:
    current = database.get_job(job_id) or {}
    raw_data = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
    return _update_created_job(
        database,
        job_id,
        status=status,
        error_message=message,
        raw_data={**raw_data, "execution_plan_error": message},
    )


def _has_not_started_provider_fence(job: dict[str, Any]) -> bool:
    raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
    fence = (
        raw_data.get(PROVIDER_SUBMISSION_FENCE_KEY)
        if isinstance(raw_data.get(PROVIDER_SUBMISSION_FENCE_KEY), dict)
        else {}
    )
    try:
        version = int(fence.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    return version == PROVIDER_SUBMISSION_FENCE_VERSION and str(fence.get("state") or "") == "not_started"


def _provider_submission_is_ambiguous(job: dict[str, Any]) -> bool:
    raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
    fence = (
        raw_data.get(PROVIDER_SUBMISSION_FENCE_KEY)
        if isinstance(raw_data.get(PROVIDER_SUBMISSION_FENCE_KEY), dict)
        else {}
    )
    try:
        version = int(fence.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    return bool(
        version == PROVIDER_SUBMISSION_FENCE_VERSION
        and str(fence.get("state") or "").strip().lower() == "submitting"
    )


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
