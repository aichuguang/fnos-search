"""V1 原生执行协议的定向回归测试。

这些测试不连接真实网盘，只验证编排器与 LegacyNativeAdapter 的边界：
原生校验必须收到 VerifyRequest，缺少轮询/校验实现必须 fail-closed。
"""

from __future__ import annotations

import unittest

from fnos_media_import.domain.models import (
    DestinationDescriptor,
    NativeLocator,
    ProviderCapabilities,
    SourceDescriptor,
)
from fnos_media_import.importers.base import ImportResult
from fnos_media_import.transfer import (
    ErrorCode,
    OperationReceipt,
    OperationStatus,
    NativeSaveRequest,
    PHASE_CLEANUP_PENDING,
    PHASE_FAILED,
    TransferPlan,
    TransferOrchestrator,
    TransferVerifyResult,
)
from fnos_media_import.transfer.orchestrator import RetryPolicy
from fnos_media_import.transfer.native_executor import LegacyNativeAdapter, NativeTransferExecutor
from fnos_media_import.transfer.verifier import VerifyRequest

from tests.test_transfer_v1 import _FakeAck, _FakeCleanup, make_plan


class _ProtocolAdapter:
    def __init__(self) -> None:
        self.verify_request = None

    def capabilities(self, account=None):
        del account
        return ProviderCapabilities(
            supports_native_save=True,
            supports_operation_polling=True,
            supports_native_verify=True,
        )

    def native_save(self, request):
        return OperationReceipt(
            operation_id="native-protocol-1",
            status="submitted",
            provider_kind="quark",
        )

    def poll_operation(self, receipt):
        del receipt
        return OperationStatus("completed", "done")

    def verify_native_result(self, request):
        self.verify_request = request
        return TransferVerifyResult("ok", "verified")


class _ImporterWithoutNativeLifecycle:
    """模拟六盘旧 importer：只有提交接口，没有 poll/verify/cancel。"""


class _SixPanTaskState:
    def __init__(self, status: str, message: str = "") -> None:
        self.status = status
        self.message = message
        self.completed = status == "completed"
        self.failed = status == "failed"


class _SixPanLifecycleImporter:
    def __init__(self, status: str = "completed") -> None:
        self.status = status
        self.deleted: list[tuple[list[str], bool]] = []
        self.client = self

    def find_task(self, task_id: str):
        return {"id": task_id, "status": self.status}

    def task_state(self, _task):
        return _SixPanTaskState(self.status, f"sixpan:{self.status}")

    def delete_offline_tasks(self, identities, *, delete_files=False):
        self.deleted.append((list(identities), bool(delete_files)))
        return {"success": True}


class _MobileLifecycleImporter:
    def __init__(self, status: str = "completed") -> None:
        self.status = status

    def _cmcc(self):
        return self

    def get_task(self, task_id: str):
        return {"taskId": task_id, "status": self.status}


class NativeProtocolTests(unittest.TestCase):
    def test_quark_native_save_forwards_share_password(self) -> None:
        class _QuarkImporter:
            def __init__(self) -> None:
                self.kwargs = None

            def import_resource(self, **kwargs):
                self.kwargs = kwargs
                return ImportResult(True, "completed", "ok", external_task_id="quark-task-1")

        importer = _QuarkImporter()
        adapter = LegacyNativeAdapter(
            importer,
            "quark",
            supports_native_save=True,
            source_loader=lambda _request: {
                "title": "受保护分享",
                "share_url": "https://pan.quark.cn/s/share-id",
                "password": "A1B2",
            },
        )
        request = NativeSaveRequest(
            idempotency_key="quark-password-1",
            source=SourceDescriptor(
                provider_kind="quark",
                source_form="share",
                canonical_source_id="quark:share:share-id",
            ),
            destination=DestinationDescriptor("quark-main", "quark"),
            locator=NativeLocator("provider_path", "/入库"),
            account_id="acc-quark",
            account_revision=1,
            category_key="movie",
        )

        adapter.native_save(request)

        self.assertIsNotNone(importer.kwargs)
        self.assertEqual(importer.kwargs["password"], "A1B2")

    def test_orchestrator_passes_verify_request_to_native_executor(self) -> None:
        adapter = _ProtocolAdapter()
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save")
        orchestrator = TransferOrchestrator(
            plan,
            {"native_save": executor},
            cleanup=_FakeCleanup(),
            ack_service=_FakeAck(),
        )

        # 运行到完成，确保 target/final 两次原生校验都不会触发旧的
        # ``plan + descriptor=...`` TypeError。
        for _ in range(20):
            result = orchestrator.step()
            if result.finished:
                break

        self.assertEqual(orchestrator.state.phase, "completed")
        self.assertIsInstance(adapter.verify_request, VerifyRequest)
        self.assertEqual(adapter.verify_request.expected_file_count, 2)
        self.assertEqual(adapter.verify_request.expected_total_size, 100)

    def test_native_executor_accepts_legacy_keyword_shape_without_type_error(self) -> None:
        adapter = _ProtocolAdapter()
        plan = make_plan(source_kind="quark", dest_kind="quark", strategy="native_save")
        executor = NativeTransferExecutor({"quark": adapter}, strategy="native_save").bind(TransferPlan(plan))
        receipt = OperationReceipt(
            operation_id="native-protocol-2",
            status="completed",
            provider_kind="quark",
        )
        result = executor.verify(
            plan,
            descriptor=plan["paths"]["destination_final"],
            expected_file_count=2,
            expected_total_size=100,
            checksum="when_supported",
            receipt=receipt,
        )
        self.assertEqual(result.status, "ok")
        self.assertIsInstance(adapter.verify_request, VerifyRequest)

    def test_legacy_adapter_without_poll_does_not_return_running_forever(self) -> None:
        adapter = LegacyNativeAdapter(
            _ImporterWithoutNativeLifecycle(),
            "sixpan",
            supports_native_offline=True,
            supports_operation_polling=True,
            supports_native_verify=True,
        )
        receipt = OperationReceipt(
            operation_id="sixpan-task-1",
            status="submitted",
            provider_kind="sixpan",
        )
        status = adapter.poll_operation(receipt)
        self.assertEqual(status.status, "unknown")
        self.assertEqual(status.error_code, ErrorCode.RESULT_UNKNOWN)

        verified = adapter.verify_native_result(VerifyRequest(), receipt=receipt)
        self.assertEqual(verified.status, "unknown")
        self.assertEqual(verified.code, ErrorCode.RESULT_UNKNOWN)

    def test_legacy_adapter_missing_cancel_is_not_claimed_cancelled(self) -> None:
        adapter = LegacyNativeAdapter(_ImporterWithoutNativeLifecycle(), "sixpan")
        result = adapter.cancel_operation(
            OperationReceipt(operation_id="sixpan-task-2", status="running", provider_kind="sixpan")
        )
        self.assertEqual(result.status, "not_cancellable")

    def test_legacy_capabilities_honor_enabled_flags(self) -> None:
        adapter = LegacyNativeAdapter(_ImporterWithoutNativeLifecycle(), "sixpan")
        caps = adapter.capabilities()
        self.assertFalse(caps.supports_native_offline)
        self.assertFalse(caps.supports_native_verify)

    def test_sixpan_lower_level_task_api_is_exposed_as_v1_lifecycle(self) -> None:
        importer = _SixPanLifecycleImporter("completed")
        adapter = LegacyNativeAdapter(
            importer,
            "sixpan",
            supports_native_offline=True,
        )
        caps = adapter.capabilities()
        self.assertTrue(caps.supports_operation_polling)
        self.assertTrue(caps.supports_native_verify)

        receipt = OperationReceipt(
            operation_id="sixpan-task-3",
            status="submitted",
            provider_kind="sixpan",
        )
        self.assertEqual(adapter.poll_operation(receipt).status, "completed")
        self.assertEqual(
            adapter.verify_native_result(VerifyRequest(), receipt=receipt).status,
            "ok",
        )
        cancelled = adapter.cancel_operation(receipt)
        self.assertEqual(cancelled.status, "cancel_pending")
        self.assertEqual(importer.deleted, [(["sixpan-task-3"], False)])

    def test_mobile_lower_level_task_api_is_exposed_as_v1_lifecycle(self) -> None:
        adapter = LegacyNativeAdapter(
            _MobileLifecycleImporter("success"),
            "mobile",
            supports_native_save=True,
        )
        caps = adapter.capabilities()
        self.assertTrue(caps.supports_operation_polling)
        self.assertTrue(caps.supports_native_verify)
        self.assertTrue(caps.supports_source_materialization)
        receipt = OperationReceipt(
            operation_id="mobile-task-1",
            status="submitted",
            provider_kind="mobile",
        )
        self.assertEqual(adapter.poll_operation(receipt).status, "completed")
        self.assertEqual(
            adapter.verify_native_result(VerifyRequest(), receipt=receipt).status,
            "ok",
        )

    def test_cleanup_failure_never_claims_completed(self) -> None:
        class _FailingCleanup:
            @staticmethod
            def cleanup(*_args, **_kwargs):
                return {"ok": False, "retryable": True, "reason": "remote cleanup unavailable"}

        orchestrator = TransferOrchestrator(
            make_plan(),
            {},
            cleanup=_FailingCleanup(),
            retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
        )
        orchestrator.state.phase = PHASE_CLEANUP_PENDING
        orchestrator.state.target_verified = True
        orchestrator.state.app_acked = True

        first = orchestrator.step()
        self.assertEqual(first.phase, PHASE_CLEANUP_PENDING)
        self.assertTrue(first.needs_wait)
        self.assertNotEqual(first.phase, "completed")

        second = orchestrator.step()
        self.assertEqual(second.phase, PHASE_FAILED)
        self.assertNotEqual(second.phase, "completed")

    def test_non_retryable_cleanup_failure_fails_closed(self) -> None:
        class _RefusedCleanup:
            @staticmethod
            def cleanup(*_args, **_kwargs):
                return {"ok": False, "retryable": False, "reason": "scope refused"}

        orchestrator = TransferOrchestrator(make_plan(), {}, cleanup=_RefusedCleanup())
        orchestrator.state.phase = PHASE_CLEANUP_PENDING
        orchestrator.state.target_verified = True
        orchestrator.state.app_acked = True
        result = orchestrator.step()
        self.assertEqual(result.phase, PHASE_FAILED)


if __name__ == "__main__":
    unittest.main()
