from __future__ import annotations

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
    ROUTE_CLOUD139_DIRECT,
    ROUTE_CLOUD189_DIRECT,
    ROUTE_QUARK_TO_MOBILE,
    ROUTE_SIXPAN_OFFLINE,
)
from .import_planner import (
    build_execution_plan,
    destination_scoped_idempotency_key,
    destination_strategy,
    execution_plan_from_job,
    provider_target_from_plan,
    resolve_destination_id,
    StoragePlanError,
    validated_execution_plan_from_job,
)
from .import_staging_service import staging_plan_from_job, validated_staging_plan_from_job


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


class ImportJobCreationService:
    """Creates the durable import record and dispatches its provider route."""

    def __init__(
        self,
        *,
        database: Any,
        config: Any,
        detect_link: Callable[..., Any],
        job_source_url: Callable[[str, dict[str, Any]], str],
        target_path: Callable[..., str],
        staging_plan: Callable[..., dict[str, Any]] | None,
        submit_quark: Callable[..., dict[str, Any]],
        submit_cloud139: Callable[..., dict[str, Any]],
        submit_generic: Callable[..., dict[str, Any]],
    ) -> None:
        self.database = database
        self.config = config
        self.detect_link = detect_link
        self.job_source_url = job_source_url
        self.target_path = target_path
        self.staging_plan = staging_plan
        self.submit_quark = submit_quark
        self.submit_cloud139 = submit_cloud139
        self.submit_generic = submit_generic

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "未命名资源").strip()
        url = str(payload.get("url") or payload.get("source_url") or "").strip()
        if not url:
            raise ValueError("资源链接不能为空")
        password = payload.get("password") or ""
        category_key = str(payload.get("category") or "movie")
        category = self.config.category(category_key)
        category_label = category.get("label", category_key)
        link = self.detect_link(url, self.config.raw.get("routes", {}), password=password)
        source_url = self.job_source_url(link.url, payload)
        raw_config = self._raw_config()
        requested_destination = str(payload.get("destination_id") or "").strip()
        explicit_destination = bool(requested_destination)
        resolved_destination = resolve_destination_id(raw_config, requested_destination)
        if explicit_destination and not resolved_destination:
            raise ValueError(f"存储目标不存在、已禁用或当前不可用：{requested_destination}")

        # Legacy ingress remains version-2 until its source/target adapter is
        # implemented.  For Quark and Cloud139, however, an enabled default is
        # an actual user intent and an unsupported matrix entry must fail closed.
        legacy_ingress = link.route in {ROUTE_SIXPAN_OFFLINE, ROUTE_CLOUD189_DIRECT}
        destination_id: str | None = None
        plan_version: int | None = None
        selected_strategy = ""
        if resolved_destination and link.supported:
            try:
                _driver_kind, selected_strategy = destination_strategy(
                    raw_config=raw_config,
                    route=link.route,
                    source_kind=link.source_type,
                    category_key=category_key,
                    category=category,
                    destination_id=resolved_destination,
                )
            except StoragePlanError as exc:
                if explicit_destination or not legacy_ingress:
                    raise ValueError(str(exc)) from exc
                selected_strategy = ""
            if selected_strategy == "manual_review":
                if explicit_destination or not legacy_ingress:
                    raise ValueError("当前来源与所选存储目标没有已验证的传输策略")
            elif selected_strategy:
                destination_id = resolved_destination
                plan_version = 3

        target_path = self.target_path(link.route, category, request_payload=payload)
        job_id, created = self.database.create_job({
            "title": title,
            "category": category_key,
            "category_label": category_label,
            "source_type": link.source_type,
            "source_url": source_url,
            "password": link.password,
            "target_route": link.route,
            "target_path": target_path,
            "status": JOB_CREATED,
            "raw_data": {
                "request": payload,
                "link_info": link.to_dict(),
                PROVIDER_SUBMISSION_FENCE_KEY: {
                    "version": PROVIDER_SUBMISSION_FENCE_VERSION,
                    "state": "not_started",
                    "attempt": 0,
                },
            },
            "idempotency_key": destination_scoped_idempotency_key(
                payload.get("idempotency_key"), destination_id
            ) or None,
            "idempotency_payload": {
                "url": source_url,
                "category": category_key,
                "destination_id": destination_id,
                "quark_selection": payload.get("quark_selection"),
                "cloud139_selection": payload.get("cloud139_selection"),
                "sixpan_selection": payload.get("sixpan_selection"),
            },
            "config_revision": payload.get("config_revision") or 1,
            "executor_id": payload.get("executor_id") or "web",
            "destination_id": destination_id,
            "plan_version": plan_version,
        })
        if not created:
            return {
                "job": self.database.get_job(job_id),
                "created": False,
                "message": "该资源和分类已存在入库任务",
            }
        self.database.add_event(job_id, EVENT_INFO, f"创建入库任务：{title}")
        if not link.supported:
            changed = _update_created_job(
                self.database,
                job_id,
                status=JOB_UNSUPPORTED,
                error_message=link.reason,
            )
            self.database.add_event(job_id, EVENT_WARN, link.reason, link.to_dict())
            return {
                "job": self.database.get_job(job_id),
                "created": True,
                "message": link.reason if changed else "任务状态已变化，未写入暂不支持状态",
                "success": False,
            }
        execution_plan: dict[str, Any] | None = None
        if plan_version == 3:
            try:
                execution_plan = self._persist_execution_plan(
                    job_id,
                    route=link.route,
                    source_kind=link.source_type,
                    category_key=category_key,
                    category=category,
                    destination_id=destination_id,
                )
                planned_target = provider_target_from_plan(execution_plan, link.route)
                if not planned_target:
                    raise StoragePlanError("执行计划缺少来源/Provider 目标路径")
                target_path = planned_target
                current = self.database.get_job(job_id) or {}
                raw_data = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
                if not _update_created_job(
                    self.database,
                    job_id,
                    target_path=target_path,
                    raw_data={**raw_data, "execution_plan": execution_plan},
                ):
                    raise StoragePlanError("任务状态已变化，未固化 v3 执行计划")
            except Exception as exc:  # noqa: BLE001
                message = f"新任务 v3 执行计划失败：{exc}"
                _mark_creation_failed(self.database, job_id, message)
                self.database.add_event(job_id, EVENT_ERROR, message)
                return {
                    "job": self.database.get_job(job_id),
                    "created": True,
                    "message": message,
                    "success": False,
                    "plan_error": True,
                }
        elif self.staging_plan:
            try:
                plan = self.staging_plan(
                    job_id=job_id,
                    route=link.route,
                    category_key=category_key,
                    category=category,
                )
            except Exception as exc:  # noqa: BLE001
                message = f"新任务暂存路径规划失败：{exc}"
                current = self.database.get_job(job_id) or {}
                raw_data = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
                changed = _update_created_job(
                    self.database,
                    job_id,
                    status=JOB_FAILED,
                    error_message=message,
                    raw_data={
                        **raw_data,
                        "staging_plan_required": True,
                        "staging_plan_error": message,
                    },
                )
                self.database.add_event(job_id, EVENT_ERROR, message)
                return {
                    "job": self.database.get_job(job_id),
                    "created": True,
                    "message": message if changed else "任务状态已变化，暂存规划失败未覆盖当前状态",
                    "success": False,
                }
            if isinstance(plan, dict) and plan.get("enabled"):
                provider_target = str(plan.get("provider_target_path") or "").strip()
                if not provider_target:
                    message = "新任务暂存路径规划失败：缺少导入目标目录"
                    current = self.database.get_job(job_id) or {}
                    raw_data = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
                    changed = _update_created_job(
                        self.database,
                        job_id,
                        status=JOB_FAILED,
                        error_message=message,
                        raw_data={
                            **raw_data,
                            "staging_plan_required": True,
                            "staging_plan_error": message,
                        },
                    )
                    self.database.add_event(job_id, EVENT_ERROR, message, {"staging_plan": plan})
                    return {
                        "job": self.database.get_job(job_id),
                        "created": True,
                        "message": message if changed else "任务状态已变化，暂存规划失败未覆盖当前状态",
                        "success": False,
                    }
                current = self.database.get_job(job_id) or {}
                raw_data = current.get("raw_data") if isinstance(current.get("raw_data"), dict) else {}
                target_path = provider_target
                changed = _update_created_job(
                    self.database,
                    job_id,
                    target_path=target_path,
                    raw_data={
                        **raw_data,
                        "staging_plan_required": True,
                        "staging_plan": plan,
                    },
                )
                if not changed:
                    message = "任务状态已变化，未固化暂存计划，也未调用网盘接口"
                    self.database.add_event(job_id, EVENT_WARN, message, {"staging_plan": plan})
                    return {
                        "job": self.database.get_job(job_id),
                        "created": True,
                        "message": message,
                        "success": False,
                        "provider_submission_claim_failed": True,
                        "retryable": True,
                    }
                self.database.add_event(
                    job_id,
                    EVENT_INFO,
                    f"已分配任务级暂存目录：{provider_target}",
                    {"staging_plan": plan},
                )
        if selected_strategy == "manual_review":
            message = "当前来源与所选存储目标需要人工处理，未调用网盘接口"
            _mark_creation_failed(self.database, job_id, message, status=JOB_UNSUPPORTED)
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": self.database.get_job(job_id),
                "created": True,
                "message": message,
                "success": False,
                "manual_review": True,
            }
        if link.route in {
            ROUTE_QUARK_TO_MOBILE,
            ROUTE_CLOUD139_DIRECT,
            ROUTE_CLOUD189_DIRECT,
            ROUTE_SIXPAN_OFFLINE,
        }:
            claimed, claimed_job, claim_message = _claim_provider_submission(
                self.database,
                job_id=job_id,
                expected_status=JOB_CREATED,
                provider=str(link.source_type or link.route),
                require_not_started_fence=True,
            )
            if not claimed:
                return {
                    "job": claimed_job,
                    "created": True,
                    "message": claim_message,
                    "success": False,
                    "provider_submission_claim_failed": True,
                    "retryable": True,
                }
        if link.route == ROUTE_QUARK_TO_MOBILE:
            return self.submit_quark(job_id, title, link.url, target_path, category_key, category, request_payload=payload)
        if link.route == ROUTE_CLOUD139_DIRECT:
            return self.submit_cloud139(job_id, title, link.url, target_path, link.password, category, category_key, request_payload=payload)
        if link.route in {ROUTE_CLOUD189_DIRECT, ROUTE_SIXPAN_OFFLINE}:
            return self.submit_generic(
                job_id,
                title,
                link.url,
                target_path,
                category,
                link.source_type,
                link.password,
                request_payload=payload,
            )
        message = f"已识别线路 {link.route}，但第一版暂未实现提交适配器"
        changed = _update_created_job(
            self.database,
            job_id,
            status=JOB_UNSUPPORTED,
            error_message=message,
        )
        self.database.add_event(job_id, EVENT_WARN, message)
        return {
            "job": self.database.get_job(job_id),
            "created": True,
            "message": message if changed else "任务状态已变化，未写入暂不支持状态",
            "success": False,
        }

    def _raw_config(self) -> dict[str, Any]:
        return getattr(self.config, "raw", {}) if self.config is not None else {}

    def _staging_enabled(self) -> bool:
        raw = self._raw_config()
        organizer = raw.get("organizer") if isinstance(raw.get("organizer"), dict) else {}
        return bool(organizer.get("enabled", False) and organizer.get("staging_enabled", True))

    def _staging_dir_name(self) -> str:
        raw = self._raw_config()
        organizer = raw.get("organizer") if isinstance(raw.get("organizer"), dict) else {}
        return str(organizer.get("staging_dir_name") or "_入库暂存")

    def _persist_execution_plan(
        self,
        job_id: int,
        *,
        route: str,
        source_kind: str,
        category_key: str,
        category: dict[str, Any],
        destination_id: str | None,
    ) -> dict[str, Any]:
        plan = build_execution_plan(
            raw_config=self._raw_config(),
            route=route,
            source_kind=source_kind,
            category_key=category_key,
            category=category,
            job_id=job_id,
            destination_id=destination_id,
            staging_enabled=self._staging_enabled(),
            staging_dir_name=self._staging_dir_name(),
        )
        if not plan:
            raise StoragePlanError("未生成执行计划")
        return plan


class ImportJobRetryService:
    """Rebuilds provider submission arguments from a persisted import job."""

    def __init__(
        self,
        *,
        database: Any,
        config: Any,
        submit_quark: Callable[..., dict[str, Any]],
        submit_cloud139: Callable[..., dict[str, Any]],
        submit_generic: Callable[..., dict[str, Any]],
    ) -> None:
        self.database = database
        self.config = config
        self.submit_quark = submit_quark
        self.submit_cloud139 = submit_cloud139
        self.submit_generic = submit_generic

    def retry(self, job_id: int) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if not job:
            raise ValueError("任务不存在")
        try:
            persisted_plan_version = int(job.get("plan_version") or 0)
        except (TypeError, ValueError) as exc:
            message = f"任务 plan_version 无效，已停止重试：{exc}"
            self.database.add_event(job_id, EVENT_WARN, message)
            return {"job": job, "created": False, "message": message, "success": False, "manual_review": True}
        v3_plan: dict[str, Any] = {}
        if persisted_plan_version >= 3:
            try:
                v3_plan = validated_execution_plan_from_job(job)
            except (ValueError, StoragePlanError) as exc:
                message = f"持久化 v3 执行计划无效，已拒绝重试：{exc}"
                self.database.add_event(job_id, EVENT_WARN, message)
                return {"job": job, "created": False, "message": message, "success": False, "manual_review": True}
            strategy = str((v3_plan.get("transfer") or {}).get("strategy") or "").strip().lower()
            if strategy == "manual_review":
                message = "该 v3 任务没有可自动执行的传输策略，已保留人工处理"
                self.database.add_event(job_id, EVENT_WARN, message)
                return {"job": job, "created": False, "message": message, "success": False, "manual_review": True}
        current_status = str(job.get("status") or "").strip().lower()
        if current_status == JOB_CANCELLED:
            message = "任务已取消，如需重新入库请重新提交资源"
            self.database.add_event(job_id, EVENT_WARN, message)
            return {"job": job, "created": False, "message": message, "success": False}
        if current_status == JOB_PROVIDER_SUBMITTING or _provider_submission_is_ambiguous(job):
            message = (
                "任务已进入网盘提交栅栏，但尚未确认外部接口是否成功；"
                "为避免重复转存或重复离线下载，禁止自动重提，请先到对应网盘核对任务后人工处理"
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
                "为避免升级后重复提交，已转为人工核对"
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
        if self._sixpan_completed_provider_requires_media_refresh_only(job):
            message = (
                "六盘离线任务已经完成，当前仅媒体库刷新失败；为避免重复下载，已拒绝重新提交六盘。"
                "请从媒体库刷新入口仅重试刷新，或完成后人工确认任务"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
                "provider_completed": True,
                "media_refresh_only": True,
            }
        if self._rclone_staging_retry_requires_manual_review(job):
            message = (
                "任务级 rclone 自动补跑已经耗尽；为避免重复转存或离线下载，已拒绝重提网盘任务。"
                "请先检查任务暂存目录和搬运日志，再从 rclone 搬运入口手动补跑或重新提交为新任务"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {"job": job, "created": False, "message": message, "success": False}
        if self._organizer_handoff_requires_manual_review(job):
            message = (
                "真实文件已进入任务暂存目录，但 Organizer 未完成接管；"
                "为避免重复转存或离线下载，已拒绝重提网盘任务，请在 Organizer 后台重试或人工创建整理任务"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {"job": job, "created": False, "message": message, "success": False}
        if _provider_submission_finished_successfully(job):
            message = (
                "网盘接口已经成功受理过该任务；当前失败或人工审核发生在后续搬运、整理或确认阶段。"
                "为避免重复转存或重复离线下载，已拒绝重新提交 Provider，请改用对应阶段的重试入口"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
                "manual_review": True,
                "provider_completed": True,
                "provider_retry_blocked": True,
            }
        if current_status not in PROVIDER_RETRYABLE_JOB_STATUSES:
            stage_label = "已完成" if current_status == JOB_DONE else "已进入网盘提交后的处理阶段"
            message = (
                f"任务{stage_label}（当前状态：{current_status or '未知'}），通用重试不会再次调用网盘接口。"
                "请等待当前阶段完成，或从 rclone、Organizer、媒体库刷新等对应入口重试"
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

        self.database.add_event(job_id, EVENT_INFO, "手动重试任务")
        route = job["target_route"]
        if not v3_plan and staging_plan_from_job(job):
            try:
                validated_staging_plan_from_job(job)
            except ValueError as exc:
                message = f"持久化 staging_plan 不完整，已拒绝重复提交网盘任务：{exc}"
                self.database.add_event(job_id, EVENT_WARN, message)
                return {
                    "job": job,
                    "created": False,
                    "message": message,
                    "success": False,
                }
        if not v3_plan and execution_plan_from_job(job):
            try:
                validated_execution_plan_from_job(job)
            except ValueError as exc:
                message = f"持久化执行计划不完整，已拒绝重试：{exc}"
                self.database.add_event(job_id, EVENT_WARN, message)
                return {
                    "job": job,
                    "created": False,
                    "message": message,
                    "success": False,
                }
        if not v3_plan and self._requires_persisted_staging_plan(job, route):
            message = (
                "当前已启用新任务级暂存，但该任务没有固化 staging_plan；"
                "为避免重新写入旧目录，已拒绝重试，请重新提交为新任务"
            )
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": job,
                "created": False,
                "message": message,
                "success": False,
            }
        category_key = str(job.get("category") or "")
        category = self.config.category(job["category"])

        if route not in {
            ROUTE_QUARK_TO_MOBILE,
            ROUTE_CLOUD139_DIRECT,
            ROUTE_CLOUD189_DIRECT,
            ROUTE_SIXPAN_OFFLINE,
        }:
            message = "该任务线路当前不支持重试"
            updater = getattr(self.database, "update_job_if_status", None)
            changed = bool(
                callable(updater)
                and updater(job_id, {current_status}, status=JOB_UNSUPPORTED, error_message=message)
            )
            latest = self.database.get_job(job_id) or job
            self.database.add_event(job_id, EVENT_WARN, message)
            return {
                "job": latest,
                "created": False,
                "message": message if changed else "任务状态已变化，未写入暂不支持状态",
                "success": False,
            }

        claimed, claimed_job, claim_message = _claim_provider_submission(
            self.database,
            job_id=job_id,
            expected_status=current_status,
            provider=str(job.get("source_type") or route),
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

        if route == ROUTE_QUARK_TO_MOBILE:
            request_payload = self._request_payload(job, use_raw_fallback=True)
            target_path = self._provider_target_path(job)
            return self.submit_quark(
                job_id,
                job["title"],
                self._retry_url(job, request_payload),
                target_path,
                category_key,
                category,
                request_payload=request_payload,
            )
        if route == ROUTE_CLOUD139_DIRECT:
            request_payload = self._request_payload(job)
            target_path = self._provider_target_path(job)
            return self.submit_cloud139(
                job_id,
                job["title"],
                self._retry_url(job, request_payload),
                target_path,
                job.get("password") or "",
                category,
                category_key,
                request_payload=request_payload,
            )
        if route in {ROUTE_CLOUD189_DIRECT, ROUTE_SIXPAN_OFFLINE}:
            request_payload = self._request_payload(job)
            return self.submit_generic(
                job_id,
                job["title"],
                # ``source_url`` may contain the internal selection fingerprint
                # used for durable deduplication.  Providers must always receive
                # the original persisted request URL instead.
                self._retry_url(job, request_payload),
                self._provider_target_path(job),
                category,
                job["source_type"],
                job.get("password") or "",
                request_payload=request_payload,
            )

        raise AssertionError("provider route dispatch fell through after validation")

    @staticmethod
    def _request_payload(job: dict[str, Any], *, use_raw_fallback: bool = False) -> dict[str, Any]:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        request = raw_data.get("request")
        if isinstance(request, dict):
            return request
        return raw_data if use_raw_fallback else {}

    @staticmethod
    def _retry_url(job: dict[str, Any], payload: dict[str, Any]) -> str:
        return str(payload.get("url") or payload.get("source_url") or job["source_url"])

    @staticmethod
    def _provider_target_path(job: dict[str, Any]) -> str:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        if plan.get("enabled"):
            planned = str(plan.get("provider_target_path") or "").strip()
            if planned:
                return planned
        execution = execution_plan_from_job(job)
        if execution:
            derived = provider_target_from_plan(execution, str(job.get("target_route") or ""))
            if derived:
                return derived
        return str(job.get("target_path") or "")

    def _requires_persisted_staging_plan(self, job: dict[str, Any], route: str) -> bool:
        if str(route or "").strip().lower() not in {
            ROUTE_QUARK_TO_MOBILE,
            ROUTE_CLOUD139_DIRECT,
            ROUTE_SIXPAN_OFFLINE,
        }:
            return False
        raw = getattr(self.config, "raw", {}) if self.config is not None else {}
        organizer = raw.get("organizer") if isinstance(raw, dict) and isinstance(raw.get("organizer"), dict) else {}
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        if plan.get("enabled"):
            return False
        if raw_data.get("staging_plan_required"):
            return True
        openlist = raw.get("openlist") if isinstance(raw, dict) and isinstance(raw.get("openlist"), dict) else {}
        return bool(
            organizer.get("enabled", False)
            and organizer.get("staging_enabled", True)
            and str(openlist.get("base_url") or "").strip()
        )

    @staticmethod
    def _rclone_staging_retry_requires_manual_review(job: dict[str, Any]) -> bool:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        return bool(completion.get("staging_retry_exhausted"))

    @staticmethod
    def _sixpan_completed_provider_requires_media_refresh_only(job: dict[str, Any]) -> bool:
        if str(job.get("target_route") or "").strip().lower() != ROUTE_SIXPAN_OFFLINE:
            return False
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        legacy_refresh = (
            raw_data.get("sixpan_legacy_refresh")
            if isinstance(raw_data.get("sixpan_legacy_refresh"), dict)
            else {}
        )
        if bool(completion.get("provider_completed")) and str(completion.get("retry_action") or "") == "media_refresh_only":
            return True
        if bool(legacy_refresh.get("provider_completed")) and str(legacy_refresh.get("retry_action") or "") == "media_refresh_only":
            return True

        # 兼容修复前已经落库的旧状态：当任务没有 staging plan、六盘轮询已记录
        # 完成且 completion 明确停在刷新 review 时，也禁止把 failed/review 当成
        # Provider 可重提状态。
        message = str(completion.get("message") or job.get("error_message") or "").strip()
        stage = str(completion.get("stage") or "").strip().lower()
        return bool(
            not staging_plan_from_job(job)
            and isinstance(raw_data.get("sixpan_poll"), dict)
            and stage == "review"
            and "刷新" in message
        )

    @staticmethod
    def _organizer_handoff_requires_manual_review(job: dict[str, Any]) -> bool:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        plan = raw_data.get("staging_plan") if isinstance(raw_data.get("staging_plan"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        if not plan.get("enabled") or str(completion.get("stage") or "").strip().lower() != "review":
            return False
        message = str(completion.get("message") or job.get("error_message") or "").strip().lower()
        return bool(
            raw_data.get("sixpan_organizer_enqueue")
            or completion.get("organizer_scan_path")
            or completion.get("openlist_visible_path")
            or "organizer" in message
            or "标准化" in message
        )


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


def _provider_submission_finished_successfully(job: dict[str, Any]) -> bool:
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
    success = fence.get("success")
    success_flag = success is True or str(success or "").strip().lower() in {"1", "true", "yes", "on"}
    return bool(
        version == PROVIDER_SUBMISSION_FENCE_VERSION
        and str(fence.get("state") or "").strip().lower() == "finished"
        and success_flag
    )


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
