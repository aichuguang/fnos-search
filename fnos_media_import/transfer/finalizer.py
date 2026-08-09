from __future__ import annotations

import os
import posixpath
import shutil
from typing import Any

from . import TransferPlan


class TransferFinalizer:
    def __init__(
        self,
        mount_service: Any,
        *,
        local_root: str,
        execution_repository: Any = None,
    ) -> None:
        self.mount_service = mount_service
        self.local_root = os.path.realpath(local_root)
        self.execution_repository = execution_repository

    def finalize(self, plan: TransferPlan) -> dict[str, Any]:
        repository_error = self._validate_repository_plan(plan)
        if repository_error:
            return self._failed(repository_error, code="PLAN_SNAPSHOT_MISMATCH")
        staging = plan.destination_staging
        final = plan.destination_final
        if staging is None or final is None:
            return self._failed("计划缺少 staging/final", code="INVALID_PLAN")
        if staging["namespace_id"] != final["namespace_id"]:
            return self._failed("staging/final 不在同一存储身份", code="PATH_CONFLICT")
        namespace = staging["namespace_id"]
        if namespace.startswith("openlist:"):
            identity, error = self._frozen_openlist_identity(plan, staging, final)
            if error:
                return self._failed(error, code="INVALID_PLAN")
            mount_id = str(identity["mount_id"])
            revision = int(identity["mount_revision"])
            source_state = self.mount_service.list_path(
                mount_id,
                staging["path"],
                revision=revision,
            )
            destination_state = self.mount_service.list_path(
                mount_id,
                final["path"],
                revision=revision,
            )
            source_exists = bool(source_state.get("exists"))
            destination_exists = bool(destination_state.get("exists"))
            if not source_exists and destination_exists:
                return {
                    "status": "done",
                    "reconciled": True,
                    "source": staging["path"],
                    "destination": final["path"],
                    "mount_id": mount_id,
                    "mount_revision": revision,
                }
            if not source_exists:
                return self._failed("staging source does not exist", code="VERIFY_MISMATCH")
            if destination_exists:
                return self._failed(
                    f"final destination already exists: {final['path']}",
                    code="PATH_CONFLICT",
                    conflict=True,
                )
            result = self.mount_service.move_path(
                mount_id,
                staging["path"],
                final["path"],
                revision=revision,
            )
            if isinstance(result, dict):
                return {
                    **result,
                    "mount_id": mount_id,
                    "mount_revision": revision,
                }
            return {
                "status": "done" if result is True else str(result or "failed"),
                "mount_id": mount_id,
                "mount_revision": revision,
            }
        if namespace.startswith("local:"):
            source = self._contained(staging["path"])
            destination = self._contained(final["path"])
            source_exists = os.path.lexists(source)
            destination_exists = os.path.lexists(destination)
            if not source_exists and destination_exists:
                return {
                    "status": "done",
                    "reconciled": True,
                    "source": source,
                    "destination": destination,
                }
            if not source_exists:
                return {"status": "failed", "message": "staging source does not exist"}
            if destination_exists:
                return {
                    "status": "failed",
                    "message": f"final destination already exists: {destination}",
                    "conflict": True,
                }
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.move(source, destination)
            return {"status": "done", "source": source, "destination": destination}
        return self._failed(
            f"不支持的 finalize namespace：{namespace}",
            code="INVALID_PLAN",
        )

    def _validate_repository_plan(self, plan: TransferPlan) -> str:
        """Fence finalize against the immutable plan repository in production."""

        if self.execution_repository is None:
            return ""
        try:
            persisted = self.execution_repository.plan(int(plan.job_id))
        except Exception as exc:  # noqa: BLE001 - converted into a fail-closed result
            return f"无法读取冻结执行计划：{exc}"
        if not isinstance(persisted, dict):
            return "缺少冻结执行计划，禁止归档"
        if persisted != plan.raw:
            return "归档计划与冻结执行计划不一致"
        try:
            execution = self.execution_repository.get(int(plan.job_id))
        except Exception as exc:  # noqa: BLE001
            return f"无法读取传输执行状态：{exc}"
        if execution is None:
            return "缺少 transfer_execution，禁止旧 finalizer 兜底归档"
        strategy = str(getattr(execution, "strategy", "") or "").strip().lower()
        if strategy and strategy != str(plan.strategy or "").strip().lower():
            return "transfer_execution 策略与冻结执行计划不一致"
        return ""

    def _frozen_openlist_identity(
        self,
        plan: TransferPlan,
        staging: dict[str, Any],
        final: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        mount_id = str(plan.destination_mount_id or "").strip()
        revision = int(plan.destination_mount_revision or 0)
        if not mount_id or revision <= 0:
            return {}, "finalize 缺少冻结目标 mount/revision"
        expected = {
            "mount_id": mount_id,
            "mount_revision": revision,
            "namespace_id": str(
                plan.bindings.get("destination_mount_namespace_id")
                or staging.get("namespace_id")
                or ""
            ),
            "connection_id": str(plan.bindings.get("destination_connection_id") or ""),
            "connection_revision": self._positive_int(
                plan.bindings.get("destination_connection_revision")
            ),
            "storage_identity_id": str(
                plan.bindings.get("destination_mount_storage_identity_id") or ""
            ),
            "identity_root": self._identity_root(
                plan.bindings.get("destination_mount_identity_root")
            ),
        }
        for label, descriptor in (("staging", staging), ("final", final)):
            checks = {
                "mount_id": str(descriptor.get("mount_id") or ""),
                "mount_revision": self._positive_int(descriptor.get("mount_revision")),
                "namespace_id": str(descriptor.get("namespace_id") or ""),
                "connection_id": str(descriptor.get("connection_id") or ""),
                "connection_revision": self._positive_int(
                    descriptor.get("connection_revision")
                ),
                "storage_identity_id": str(descriptor.get("storage_identity_id") or ""),
                "identity_root": self._identity_root(descriptor.get("identity_root")),
            }
            for key, expected_value in expected.items():
                # V1 OpenList descriptors are required to repeat the frozen
                # identity.  Missing values are not allowed to fall back to a
                # namespace string or the current admin configuration.
                if checks[key] != expected_value:
                    return {}, f"{label} 的 {key} 与冻结目标 binding 不一致"
        getter = getattr(self.mount_service, "get_revision", None)
        if not callable(getter):
            return {}, "OpenList mount service 不支持冻结 revision"
        try:
            frozen = getter(mount_id, revision)
        except Exception as exc:  # noqa: BLE001
            return {}, f"读取冻结 mount 失败：{exc}"
        if not isinstance(frozen, dict):
            return {}, f"冻结 mount 不存在：{mount_id}@{revision}"
        frozen_checks = {
            "namespace_id": str(frozen.get("namespace_id") or ""),
            "connection_id": str(frozen.get("connection_id") or ""),
            "connection_revision": self._positive_int(frozen.get("connection_revision")),
            "storage_identity_id": str(frozen.get("storage_identity_id") or ""),
            "identity_root": self._identity_root(frozen.get("identity_root")),
        }
        for key, expected_value in expected.items():
            if key in {"mount_id", "mount_revision"}:
                continue
            if frozen_checks[key] != expected_value:
                return {}, f"冻结 mount 的 {key} 与执行计划不一致"
        if not bool(frozen.get("enabled", True)):
            return {}, "冻结 mount 在计划生成时未启用"
        if not bool(frozen.get("writable")):
            return {}, "冻结 mount 在计划生成时不可写"
        if str(frozen.get("identity_status") or "") not in {"verified", "declared"}:
            return {}, "冻结 mount 的存储身份未确认"
        return expected, ""

    @staticmethod
    def _positive_int(value: Any) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0

    @staticmethod
    def _identity_root(value: Any) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if not text:
            return ""
        return posixpath.normpath("/" + text.strip("/"))

    @staticmethod
    def _failed(
        message: str,
        *,
        code: str,
        conflict: bool = False,
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "message": str(message),
            "code": str(code),
            "retryable": False,
            **({"conflict": True} if conflict else {}),
        }

    def _contained(self, path: str) -> str:
        value = os.path.realpath(path)
        if os.path.commonpath([value, self.local_root]) != self.local_root or value == self.local_root:
            raise ValueError(f"本地 finalize 路径越界：{value}")
        return value
