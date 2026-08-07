"""TransferDispatchService：把 v4 冻结计划绑定真实执行器并驱动编排。

一次性切换后，v4 任务创建时冻结计划，随后由本服务根据 strategy 装配
对应执行器（native_save / native_offline / rclone_copy / relay_upload_mobile），
用 TransferOrchestrator 跑状态机，并把阶段状态回写任务 raw_data。
"""

from __future__ import annotations

import json
from typing import Any

from ..planning.plan_validator import validate_v4_plan
from .orchestrator import TransferOrchestrator
from .verifier import TransferVerifier
from .cleanup import TransferCleanupService


class TransferDispatchError(RuntimeError):
    pass


class TransferDispatchService:
    def __init__(
        self,
        *,
        accounts_service: Any,
        target_service: Any,
        mount_service: Any,
        registry: Any,
        rclone_service: Any,
        locator_resolver: Any,
        rclone_config: dict[str, Any] | None = None,
        relay_root: str = "/data/relay",
    ) -> None:
        self.accounts_service = accounts_service
        self.target_service = target_service
        self.mount_service = mount_service
        self.registry = registry
        self.rclone_service = rclone_service
        self.locator_resolver = locator_resolver
        self.rclone_config = rclone_config or {}
        self.relay_root = relay_root

    def build_executors(self, plan: dict[str, Any]) -> dict[str, Any]:
        strategy = str((plan.get("transfer") or {}).get("strategy") or "").strip()
        adapters: dict[str, Any] = {}
        provider_kinds = {
            str((plan.get("source") or {}).get("provider_kind") or ""),
            str((plan.get("destination") or {}).get("provider_kind") or ""),
        }
        for kind in provider_kinds:
            if not kind:
                continue
            try:
                adapters[kind] = self.registry.get(kind)
            except Exception:  # noqa: BLE001
                continue

        from .native_executor import NativeTransferExecutor
        from .rclone_executor import RcloneTransferExecutor
        from .mobile_relay_executor import MobileRelayExecutor

        executors: dict[str, Any] = {}
        if strategy in {"native_save", "native_offline"}:
            executors[strategy] = NativeTransferExecutor(adapters, strategy=strategy)
        if strategy == "rclone_copy":
            executors["rclone_copy"] = RcloneTransferExecutor(
                self.rclone_service,
                self.locator_resolver,
            )
        if strategy == "relay_upload_mobile":
            from .mobile_relay_executor import CmccMobileUploader

            mobile_uploader = CmccMobileUploader()
            executors["relay_upload_mobile"] = MobileRelayExecutor(
                self.rclone_service,
                mobile_uploader,
                relay_root=self.relay_root,
            )
        return executors

    def dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        """读取任务冻结的 v4 计划，装配执行器并驱动编排。返回阶段结果。"""
        if not isinstance(job, dict):
            raise TransferDispatchError("任务不存在")
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        plan = raw_data.get("execution_plan") if isinstance(raw_data.get("execution_plan"), dict) else {}
        if int(plan.get("version") or 0) != 4:
            raise TransferDispatchError("仅接受 v4 执行计划")

        executors = self.build_executors(plan)
        if not executors:
            raise TransferDispatchError(f"计划策略没有可用执行器：{(plan.get('transfer') or {}).get('strategy')}")

        orchestrator = TransferOrchestrator(
            plan,
            executors,
            verifier=TransferVerifier(),
            cleanup=TransferCleanupService(),
        )
        result = orchestrator.run(max_steps=100)
        return {
            "success": result.ok if hasattr(result, "ok") else True,
            "phase": str(getattr(result, "phase", "") or getattr(result, "state", "") or ""),
            "snapshot": orchestrator.snapshot(),
            "message": str(getattr(result, "message", "") or ""),
        }
