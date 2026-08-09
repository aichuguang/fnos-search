"""V1 多链路任务创建：目标只由管理员 assignment 决定。"""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
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


class V1PlanningError(ValueError):
    """V1 规划失败且必须失败关闭。"""

    def __init__(self, message: str, code: str = "PLANNING_FAILED") -> None:
        super().__init__(message)
        self.code = code


# 所有调用方都不允许覆盖管理员配置的基础设施字段。
GUEST_FORBIDDEN_FIELDS = frozenset(
    {
        "destination_id",
        "idempotency_key",
        "executor_id",
        "config_revision",
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

_PLAN_SECRET_FIELDS = frozenset(
    {
        "url", "source_url", "share_url", "shareurl", "magnet", "magnet_uri",
        "password", "pwd", "token", "access_token", "refresh_token", "cookie",
        "authorization", "credentials", "credentials_ciphertext", "secret",
    }
)


def _load_planning_dependency(name: str) -> Any:
    """Lazily import a V1 planning dependency.

    If a planning module is unavailable, the error is surfaced clearly
    not yet present, the ImportError is surfaced as a clear
    ``V1PlanningError`` instead of being swallowed or silently falling back.
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
        if name == "validate_plan":
            from ..planning.plan_validator import validate_plan

            return validate_plan
    except ImportError as exc:
        raise V1PlanningError(
            f"V1 规划依赖 planning.{name} 尚未就绪：{exc}",
            code="PLANNING_UNAVAILABLE",
        ) from exc
    raise V1PlanningError(f"未知的 V1 规划依赖：{name}", code="PLANNING_UNAVAILABLE")


def _account_id(account: Any) -> str:
    if isinstance(account, (tuple, list)) and len(account) >= 1:
        return str(account[0] or "").strip()
    return str((account or {}).get("account_id") or "").strip()


def _account_revision(account: Any) -> int:
    if isinstance(account, (tuple, list)) and len(account) >= 2:
        return _to_nonneg_int(account[1])
    return _to_nonneg_int((account or {}).get("account_revision"))


def _selection_revision(account: Any) -> int:
    if isinstance(account, (tuple, list)):
        return _to_nonneg_int(account[2]) if len(account) >= 3 else 0
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
    """Creates the durable V1 import record from a server-resolved plan.

    The guest never selects a save path.  The source is resolved from the
    trusted search-cache record (or re-detected from the validated URL), the
    destination is resolved from admin ``source_destination_assignments``, the
    active accounts are frozen, and the V1 Planner produces an immutable
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
        validate_plan: Callable[..., Any] | None = None,
        plan_validation_deps: Any | None = None,
        execution_repository: Any | None = None,
        enqueue_transfer: Callable[[int], Any] | None = None,
        enqueue_transfer_with_connection: Callable[[Any, int], Any] | None = None,
        source_record_service: Any | None = None,
        config_revision_provider: Callable[[], int] | None = None,
        executor_id: str = "web",
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
        # V1 planning dependencies. Injected by tests / the integration layer;
        # defaults are lazily imported from the planning package.
        self.source_resolver = source_resolver
        self.destination_resolver = destination_resolver
        self.plan_import = plan_import
        self.validate_plan = validate_plan
        self.plan_validation_deps = plan_validation_deps
        self.execution_repository = execution_repository
        self.enqueue_transfer = enqueue_transfer
        self.enqueue_transfer_with_connection = enqueue_transfer_with_connection
        self.source_record_service = source_record_service
        self.config_revision_provider = config_revision_provider
        self.executor_id = str(executor_id or "web").strip() or "web"

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a job from an untrusted HTTP/public payload."""

        payload = dict(payload or {})
        forbidden = sorted(field for field in GUEST_FORBIDDEN_FIELDS if field in payload)
        if forbidden:
            raise V1PlanningError(
                f"保存位置由管理员配置，提交不得包含：{', '.join(forbidden)}",
                code="CLIENT_DESTINATION_FORBIDDEN",
            )

        return self._create(payload)

    def create_internal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a job for a trusted workflow without trusting its routing fields."""

        sanitized = dict(payload or {})
        for field in GUEST_FORBIDDEN_FIELDS:
            sanitized.pop(field, None)
        return self._create(sanitized)

    def _create(self, payload: dict[str, Any]) -> dict[str, Any]:

        title = str(payload.get("title") or "未命名资源").strip()
        url = str(payload.get("url") or payload.get("source_url") or "").strip()
        if not url:
            raise ValueError("资源链接不能为空")
        category_key = str(payload.get("category") or "movie")
        category = self.config.category(category_key)
        category_label = category.get("label", category_key)

        # (1) 服务端识别来源，不信任客户端 provider 字段。
        source_record = self._build_source_record(payload)
        source = self._resolve_source(record=source_record)
        if self.source_record_service is None:
            raise V1PlanningError("来源快照服务未装配", code="SOURCE_SNAPSHOT_UNAVAILABLE")
        # (2) 管理员通过 source_destination_assignments 分配目标；0 个可用目标
        #     必须失败关闭，绝不为空目标回退默认盘。
        destination = self._resolve_destination(source, category_key)
        if destination is None:
            raise V1PlanningError(
                "该资源当前没有可用的保存位置，请联系管理员为该来源配置入库目标",
                code="SOURCE_DESTINATION_NOT_CONFIGURED",
            )
        # (3) 解析并冻结来源与目标激活账号。
        source_account = self._resolve_source_account(source)
        destination_account = self._resolve_destination_account(destination)
        job_id = 0
        idempotency_key = ""
        duplicate_job_id = 0
        duplicate_idempotency_key = ""
        try:
            if self.execution_repository is None or self.enqueue_transfer_with_connection is None:
                raise V1PlanningError("V1 持久执行器未装配", code="EXECUTION_UNAVAILABLE")
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                # Source snapshot participates in the same transaction as the
                # job, immutable plan, execution row and durable queue task.
                # Any planning/validation/enqueue failure therefore leaves no
                # orphan encrypted source revision behind.
                source = self.source_record_service.capture(
                    source,
                    source_record,
                    connection=connection,
                )
                selected_items_hash = str(source.selected_items_hash or "")
                idempotency_key = self._v1_idempotency_key(
                    category_key=category_key,
                    source=source,
                    destination=destination,
                    source_account=source_account,
                    destination_account=destination_account,
                    selected_items_hash=selected_items_hash,
                )
                raw_data = {
                    # Full share URLs, magnet URIs and extraction passwords are
                    # held only in the encrypted source revision.  The job row
                    # contains a non-sensitive request summary and references.
                    "request": self._safe_request_summary(payload, category_key=category_key),
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
                job_payload = {
                    "title": title,
                    "category": category_key,
                    "category_label": category_label,
                    "source_type": source.provider_kind,
                    "source_url": self._job_source_reference(source),
                    "password": "",
                    "target_route": _source_route(source),
                    "target_path": "",
                    "status": JOB_CREATED,
                    "raw_data": raw_data,
                    "idempotency_key": idempotency_key,
                    # These are trusted server execution attributes. Client
                    # payload values with the same names are intentionally
                    # ignored and cannot select a config revision/executor.
                    "config_revision": self._trusted_config_revision(),
                    "executor_id": self.executor_id,
                    "destination_id": destination.destination_id,
                    "plan_version": EXECUTION_PLAN_VERSION,
                }
                job_id, created = self.database.job_commands.create_with_connection(
                    connection, job_payload
                )
                if not created:
                    existing = connection.execute(
                        "SELECT idempotency_key FROM import_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    duplicate_job_id = int(job_id)
                    duplicate_idempotency_key = (
                        str(existing["idempotency_key"] or "") if existing else ""
                    )
                else:
                    plan = self._plan_import(
                        source=source,
                        destination=destination,
                        category_key=category_key,
                        category=category,
                        job_id=job_id,
                        request_payload=payload,
                    )
                    secret_path = _first_secret_path(plan)
                    if secret_path:
                        raise V1PlanningError(
                            f"执行计划包含明文凭证字段：{secret_path}",
                            code="PLAN_CONTAINS_PLAINTEXT_SECRET",
                        )
                    job_record = {
                        "id": job_id,
                        "category": category_key,
                        "destination_id": destination.destination_id,
                        "plan_version": EXECUTION_PLAN_VERSION,
                    }
                    # PlanValidator must see the uncommitted source revision
                    # through this transaction, not through a second SQLite
                    # connection that would report it as missing. Composition
                    # roots normally share one service, but bind both objects
                    # when a test/container supplied separate instances.
                    source_services = [self.source_record_service]
                    dependency_service = getattr(
                        self.plan_validation_deps, "source_record_service", None
                    )
                    if dependency_service is not None and dependency_service is not self.source_record_service:
                        source_services.append(dependency_service)
                    with ExitStack() as stack:
                        for service in source_services:
                            binder = getattr(service, "bind_connection", None)
                            if callable(binder):
                                stack.enter_context(binder(connection))
                        self._validate_plan(plan, job_record=job_record)
                    # The immutable plan has exactly one source of truth:
                    # import_execution_plans.  Job raw_data contains only a
                    # non-sensitive reference/summary so status and provider
                    # callbacks cannot accidentally become a second plan store.
                    transfer = plan.get("transfer") if isinstance(plan.get("transfer"), dict) else {}
                    raw_data_with_plan = {
                        **raw_data,
                        "execution_plan_ref": {
                            "schema_version": int(EXECUTION_PLAN_VERSION),
                            "plan_hash": _canonical_plan_hash(plan),
                            "strategy": str(transfer.get("strategy") or ""),
                        },
                    }
                    connection.execute(
                        "UPDATE import_jobs SET target_path=?, raw_data=?, updated_at=? "
                        "WHERE id=? AND status=?",
                        (
                            _plan_target_path(plan),
                            json.dumps(raw_data_with_plan, ensure_ascii=False),
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            job_id,
                            JOB_CREATED,
                        ),
                    )
                    self.execution_repository.create_with_connection(connection, job_id, plan)
                    self.enqueue_transfer_with_connection(connection, job_id)
        except Exception as exc:  # noqa: BLE001
            message = str(exc) if isinstance(exc, V1PlanningError) else f"V1 执行计划失败：{exc}"
            return {
                "job": None,
                "created": False,
                "message": message,
                "success": False,
                "plan_error": True,
                "idempotency_key": idempotency_key,
            }

        if duplicate_job_id:
            return {
                "job": self.database.get_job(duplicate_job_id) or {},
                "created": False,
                "message": "相同来源快照、选集、分类、目标和账号修订的任务已存在",
                "idempotency_key": duplicate_idempotency_key,
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
        """Build the trusted source record for the V1 SourceResolver.

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
        if not isinstance(record, dict):
            return None
        # The cached URL/password remain the trusted source identity, while a
        # guest's already-sanitized file selection belongs to this submission.
        # Returning the cache row verbatim discarded Quark/139/Tianyi choices
        # before SourceResolver froze the encrypted source revision.
        merged = dict(record)
        raw = dict(record.get("raw_data") or {}) if isinstance(record.get("raw_data"), dict) else {}
        selection_fields = (
            "quark_selection",
            "cloud139_selection",
            "tianyi_selection",
            "sixpan_selection",
            "selected_items",
            "selected_files",
            "selected_folders",
            "selected_dirs",
            "ignore_files",
        )
        for field in selection_fields:
            if field not in payload:
                continue
            value = payload.get(field)
            merged[field] = value
            raw[field] = value
        merged["raw_data"] = raw
        return merged

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
        raise V1PlanningError(
            "V1 目标解析器 DestinationResolver 必须由集成层注入"
            "（rules_service / target_service / accounts_service）",
            code="PLANNING_UNAVAILABLE",
        )

    def _resolve_source(self, *, record: dict[str, Any]) -> SourceDescriptor:
        resolver = self._source_resolver()
        try:
            source = resolver.resolve(record)
        except V1PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V1PlanningError(
                f"无法解析入库来源：{exc}", code="SOURCE_UNRESOLVABLE"
            ) from exc
        if not isinstance(source, SourceDescriptor):
            raise V1PlanningError(
                "来源解析器返回了无效的来源描述", code="SOURCE_UNRESOLVABLE"
            )
        return source

    def _resolve_destination(
        self, source: SourceDescriptor, category_key: str
    ) -> DestinationDescriptor | None:
        resolver = self._destination_resolver()
        try:
            destination = resolver.resolve(source, category_key, audience="guest")
        except V1PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V1PlanningError(
                f"解析保存目标失败：{exc}", code="SOURCE_DESTINATION_NOT_CONFIGURED"
            ) from exc
        if destination is None:
            return None
        if not isinstance(destination, DestinationDescriptor):
            raise V1PlanningError(
                "目标解析器返回了无效的目标描述", code="PLANNING_FAILED"
            )
        return destination

    def _resolve_source_account(self, source: SourceDescriptor) -> Any:
        resolver = self._destination_resolver()
        try:
            return resolver.resolve_account(
                source.provider_kind, source.account_scope or "default", role="source"
            )
        except V1PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V1PlanningError(
                f"当前来源账号不可用：{exc}", code="SOURCE_ACCOUNT_UNAVAILABLE"
            ) from exc

    def _resolve_destination_account(self, destination: DestinationDescriptor) -> Any:
        if not destination.account_id:
            return None
        return destination.account_id, destination.account_revision or 0

    def _v1_idempotency_key(
        self,
        *,
        category_key: str,
        source: SourceDescriptor,
        destination: DestinationDescriptor,
        source_account: dict[str, Any] | None,
        destination_account: dict[str, Any] | None,
        selected_items_hash: str,
    ) -> str:
        raw = (
            f"v1|{source.source_record_id}|{source.source_record_revision}|"
            f"{source.canonical_source_id}|{str(category_key or '').strip()}"
        )
        base = "v1:" + hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()
        parts = [
            f"srev={source.source_record_id}@{source.source_record_revision}",
            f"src={source.canonical_source_id}",
            f"cat={str(category_key or '').strip()}",
            f"sel={selected_items_hash}",
            f"dest={destination.destination_id}",
            f"drev={int(destination.target_revision or 1)}",
            f"binding={destination.binding_id}@{destination.binding_revision}",
        ]
        if source_account:
            parts.append(f"sa={_account_id(source_account)}@{_account_revision(source_account)}")
        if destination_account:
            parts.append(f"da={_account_id(destination_account)}@{_account_revision(destination_account)}")
        return f"{base}:{':'.join(parts)}"

    @staticmethod
    def _job_source_reference(source: SourceDescriptor) -> str:
        return f"source-record:{source.source_record_id}@{int(source.source_record_revision)}"

    @staticmethod
    def _safe_request_summary(payload: dict[str, Any], *, category_key: str) -> dict[str, Any]:
        """Return the small non-secret subset kept on ``import_jobs``.

        Selection and Provider locator details are already frozen in the
        encrypted source revision and immutable plan.  An allow-list avoids a
        future client field (cookie/token/share URL) being copied to raw_data.
        """
        summary: dict[str, Any] = {"category": str(category_key or "")}
        for key in ("public_id", "resource_id"):
            value = payload.get(key)
            if value not in (None, ""):
                summary[key] = value
        return summary

    def _trusted_config_revision(self) -> int:
        provider = self.config_revision_provider
        if provider is None:
            return 1
        try:
            return max(1, int(provider() or 1))
        except (TypeError, ValueError):
            raise V1PlanningError(
                "服务端配置修订无效", code="CONFIG_REVISION_INVALID"
            ) from None

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
                audience="guest",
                organizer_enabled=self._organizer_enabled(),
                media_refresh_enabled=False,
            )
        except V1PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V1PlanningError(f"V1 计划生成失败：{exc}", code="PLANNING_FAILED") from exc
        if not isinstance(plan, dict) or not plan:
            raise V1PlanningError("V1 Planner 未返回执行计划", code="PLANNING_FAILED")
        return plan

    def _validate_plan(
        self, plan: dict[str, Any], job_record: dict[str, Any]
    ) -> None:
        validator = self.validate_plan or _load_planning_dependency("validate_plan")
        try:
            if self.plan_validation_deps is not None:
                validator(plan, job_record, deps=self.plan_validation_deps)
            else:
                validator(plan, job_record)
        except V1PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V1PlanningError(
                f"V1 执行计划校验失败：{exc}", code="PLANNING_VALIDATION_FAILED"
            ) from exc

    @staticmethod
    def _source_dict(source: SourceDescriptor) -> dict[str, Any]:
        return {
            "provider_kind": source.provider_kind,
            "source_form": source.source_form,
            "canonical_source_id": source.canonical_source_id,
            "account_scope": source.account_scope,
            "selected_items_hash": source.selected_items_hash,
            "search_source_id": source.search_source_id,
            "source_record_id": source.source_record_id,
            "source_record_revision": source.source_record_revision,
        }

    @staticmethod
    def _destination_dict(destination: DestinationDescriptor) -> dict[str, Any]:
        return {
            "destination_id": destination.destination_id,
            "provider_kind": destination.provider_kind,
            "target_revision": destination.target_revision,
            "binding_id": destination.binding_id,
            "binding_revision": destination.binding_revision,
            "account_id": destination.account_id,
            "account_revision": destination.account_revision,
        }


class ImportJobRetryService:
    """Only retries frozen V1 plans.

    Older plan versions are rejected with ``UNSUPPORTED_PLAN_VERSION``; the
    plan is never rebuilt from current configuration. A V1 plan is validated
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
        validate_plan: Callable[..., Any] | None = None,
        plan_validation_deps: Any | None = None,
        execution_repository: Any | None = None,
        enqueue_transfer: Callable[[int], Any] | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.submit_quark = submit_quark
        self.submit_cloud139 = submit_cloud139
        self.submit_generic = submit_generic
        self.validate_plan = validate_plan
        self.plan_validation_deps = plan_validation_deps
        self.execution_repository = execution_repository
        self.enqueue_transfer = enqueue_transfer

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
                f"仅支持执行 schema v{EXECUTION_PLAN_VERSION} 计划；"
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

        plan = self.execution_repository.plan(job_id) if self.execution_repository is not None else None
        if not plan:
            message = "任务没有 V1 不可变执行计划，已拒绝重试"
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
            }
        try:
            self._validate_plan(plan, job_record=job)
        except V1PlanningError as exc:
            message = f"持久化 V1 执行计划无效，已拒绝重试：{exc}"
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
                "error_code": str(getattr(exc, "code", "PLANNING_VALIDATION_FAILED")),
            }

        if self.execution_repository is None or self.enqueue_transfer is None:
            return {"job": job, "created": False, "message": "V1 Worker 未装配", "success": False}
        execution = self.execution_repository.get(job_id)
        if execution is None:
            return {
                "job": job,
                "created": False,
                "message": "任务缺少持久执行状态，已停止重试",
                "success": False,
                "manual_review": True,
                "error_code": "EXECUTION_MISSING",
            }
        if execution.phase in {"cancelled", "manual_review"}:
            message = (
                "已取消或人工复核任务不能复用原外部操作栅栏；"
                "请按当前来源与管理员路由创建新一代入库任务"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "requires_new_job": True,
                "error_code": "REIMPORT_NEW_GENERATION_REQUIRED",
            }
        try:
            self.execution_repository.reactivate(job_id)
            queued = self.enqueue_transfer(job_id)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.database.add_event(job_id, EVENT_WARN, message)
            return {"job": job, "created": False, "message": message, "success": False}
        self.database.add_event(job_id, EVENT_INFO, "V1 持久执行任务已重新入队")
        return {
            "job": self.database.get_job(job_id),
            "created": False,
            "message": "V1 执行状态已恢复并重新入队",
            "success": True,
            "queued": queued,
            "idempotency_key": str(job.get("idempotency_key") or ""),
        }

    def _validate_plan(
        self, plan: dict[str, Any], job_record: dict[str, Any]
    ) -> None:
        validator = self.validate_plan or _load_planning_dependency("validate_plan")
        try:
            if self.plan_validation_deps is not None:
                validator(plan, job_record, deps=self.plan_validation_deps)
            else:
                validator(plan, job_record)
        except V1PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise V1PlanningError(
                f"V1 执行计划校验失败：{exc}", code="PLANNING_VALIDATION_FAILED"
            ) from exc


def _source_route(source: SourceDescriptor) -> str:
    return f"{source.provider_kind}:{source.source_form}"


def _first_secret_path(value: Any, prefix: str = "plan") -> str:
    if isinstance(value, dict):
        for key, candidate in value.items():
            field = str(key).strip()
            path = f"{prefix}.{field}"
            if field.lower() in _PLAN_SECRET_FIELDS and candidate not in (None, "", [], {}):
                return path
            nested = _first_secret_path(candidate, path)
            if nested:
                return nested
    elif isinstance(value, (list, tuple)):
        for index, candidate in enumerate(value):
            nested = _first_secret_path(candidate, f"{prefix}[{index}]")
            if nested:
                return nested
    return ""


def _canonical_plan_hash(plan: dict[str, Any]) -> str:
    """Hash the exact JSON representation persisted in import_execution_plans."""

    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
