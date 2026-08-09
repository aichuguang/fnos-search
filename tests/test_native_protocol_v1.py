"""V1 formal Provider Adapter and native verification contract tests."""

from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import unittest

from fnos_media_import.domain.models import (
    DestinationDescriptor,
    NativeLocator,
    ProviderCapabilities,
    SourceDescriptor,
)
from fnos_media_import.importers.base import ImportResult
from fnos_media_import.providers.adapters import (
    MobileProviderAdapter,
    QuarkProviderAdapter,
    SixPanProviderAdapter,
    TianyiProviderAdapter,
)
from fnos_media_import.providers.base import validate_adapter_contract
from fnos_media_import.transfer import (
    ErrorCode,
    NativeSaveRequest,
    OperationReceipt,
    OperationStatus,
    PHASE_CLEANUP_PENDING,
    PHASE_FAILED,
    TransferOrchestrator,
    TransferPlan,
    TransferVerifyResult,
)
from fnos_media_import.transfer.native_executor import NativeTransferExecutor
from fnos_media_import.transfer.orchestrator import RetryPolicy
from fnos_media_import.transfer.verifier import VerifyRequest

from tests.test_transfer_v1 import _FakeAck, _FakeCleanup, make_plan


class _ProtocolAdapter(RepositoryFakeMixin):
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
        del request
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


class _SourceRecords(RepositoryFakeMixin):
    def __init__(self, payload=None) -> None:
        self.payload = payload or {
            "title": "受保护分享",
            "url": "https://pan.quark.cn/s/share-id",
            "password": "A1B2",
            "selection": {},
        }

    def resolve(self, source_record_id, revision):
        self.last = (source_record_id, revision)
        return dict(self.payload)


class _ImporterWithoutNativeLifecycle:
    pass


class _SixPanTaskState(RepositoryFakeMixin):
    def __init__(self, status: str, message: str = "") -> None:
        self.status = status
        self.message = message
        self.completed = status == "completed"
        self.failed = status == "failed"


class _SixPanLifecycleImporter(RepositoryFakeMixin):
    def __init__(self, status: str = "completed") -> None:
        self.status = status
        self.deleted: list[tuple[list[str], bool]] = []

    def find_task(self, task_id: str):
        return {"id": task_id, "status": self.status}

    def task_state(self, _task):
        return _SixPanTaskState(self.status, f"sixpan:{self.status}")

    def delete_offline_tasks(self, identities, *, delete_files=False):
        self.deleted.append((list(identities), bool(delete_files)))
        return {"success": True}


class _MobileLifecycleImporter(RepositoryFakeMixin):
    def __init__(self, status: str = "completed") -> None:
        self.status = status

    def _cmcc(self):
        return self

    def get_task(self, task_id: str):
        return {"taskId": task_id, "status": self.status}


def _receipt(operation_id: str, provider_kind: str) -> OperationReceipt:
    return OperationReceipt(
        operation_id=operation_id,
        status="submitted",
        provider_kind=provider_kind,
        metadata={"account_id": f"acc-{provider_kind}", "account_revision": 1},
    )


class NativeProtocolTests(unittest.TestCase):
    def test_builtin_adapters_satisfy_their_declared_v1_contracts(self) -> None:
        importer = _ImporterWithoutNativeLifecycle()
        adapters = (
            QuarkProviderAdapter(
                importer_factory=lambda _account_id, _revision: importer,
                account_importer_factory=lambda _values: importer,
                source_record_service=_SourceRecords(),
            ),
            TianyiProviderAdapter(
                importer_factory=lambda _account_id, _revision: importer,
                account_importer_factory=lambda _values: importer,
                source_record_service=_SourceRecords(),
            ),
            MobileProviderAdapter(
                importer_factory=lambda _account_id, _revision: importer,
                account_importer_factory=lambda _values: importer,
                source_record_service=_SourceRecords(),
            ),
            SixPanProviderAdapter(
                importer_factory=lambda _account_id, _revision: importer,
                account_importer_factory=lambda _values: importer,
                source_record_service=_SourceRecords(),
            ),
        )

        self.assertEqual(
            {adapter.kind: validate_adapter_contract(adapter) for adapter in adapters},
            {"quark": [], "tianyi": [], "mobile": [], "sixpan": []},
        )

    def test_quark_native_save_uses_frozen_account_and_source(self) -> None:
        class _QuarkImporter:
            def __init__(self) -> None:
                self.kwargs = None

            def import_resource(self, **kwargs):
                self.kwargs = kwargs
                return ImportResult(
                    True,
                    "completed",
                    "ok",
                    external_task_id="quark-task-1",
                )

        importer = _QuarkImporter()
        calls = []
        adapter = QuarkProviderAdapter(
            importer_factory=lambda account_id, revision: calls.append(
                (account_id, revision)
            )
            or importer,
            account_importer_factory=lambda _values: importer,
            source_record_service=_SourceRecords(),
        )
        request = NativeSaveRequest(
            idempotency_key="quark-password-1",
            source=SourceDescriptor(
                provider_kind="quark",
                source_form="share",
                canonical_source_id="quark:share:share-id",
                source_record_id="source-quark",
                source_record_revision=1,
            ),
            destination=DestinationDescriptor("quark-main", "quark"),
            locator=NativeLocator("provider_path", "/入库"),
            account_id="acc-quark",
            account_revision=7,
            category_key="movie",
        )

        receipt = adapter.native_save(request)

        self.assertEqual(calls, [("acc-quark", 7)])
        self.assertEqual(importer.kwargs["password"], "A1B2")
        self.assertEqual(receipt.metadata["account_revision"], 7)

    def test_orchestrator_passes_only_verify_request(self) -> None:
        adapter = _ProtocolAdapter()
        plan = make_plan(
            source_kind="quark", dest_kind="quark", strategy="native_save"
        )
        executor = NativeTransferExecutor(
            {"quark": adapter}, strategy="native_save"
        )
        orchestrator = TransferOrchestrator(
            plan,
            {"native_save": executor},
            cleanup=_FakeCleanup(),
            ack_service=_FakeAck(),
        )

        for _ in range(20):
            result = orchestrator.step()
            if result.finished:
                break

        self.assertEqual(orchestrator.state.phase, "completed")
        self.assertIsInstance(adapter.verify_request, VerifyRequest)
        self.assertIsInstance(adapter.verify_request.operation, OperationReceipt)
        self.assertEqual(adapter.verify_request.expected_file_count, 2)

    def test_tianyi_native_save_and_lifecycle_use_frozen_account(self) -> None:
        class _TianyiImporter:
            def __init__(self) -> None:
                self.import_kwargs = None
                self.folder_calls = []
                self.poll_receipt = None
                self.verify_request = None
                self.cancel_receipt = None

            def _ensure_folder_path(self, root_folder_id, relative_path):
                self.folder_calls.append((root_folder_id, relative_path))
                return "resolved-folder-id"

            def import_resource(self, **kwargs):
                self.import_kwargs = kwargs
                return ImportResult(
                    True,
                    "submitted",
                    "ok",
                    external_task_id="tianyi-task-1",
                )

            def poll_operation(self, receipt):
                self.poll_receipt = receipt
                return OperationStatus("completed", "done")

            def verify_native_result(self, request):
                self.verify_request = request
                return TransferVerifyResult("ok", "verified")

            def cancel_operation(self, receipt):
                self.cancel_receipt = receipt
                return {"status": "cancelled", "message": "cancelled"}

        importer = _TianyiImporter()
        calls = []
        adapter = TianyiProviderAdapter(
            importer_factory=lambda account_id, revision: calls.append(
                (account_id, revision)
            )
            or importer,
            account_importer_factory=lambda _values: importer,
            source_record_service=_SourceRecords(
                {
                    "title": "天翼分享",
                    "url": "https://cloud.189.cn/t/share-id",
                    "password": "A1B2",
                    "selection": {},
                }
            ),
        )
        request = NativeSaveRequest(
            idempotency_key="tianyi-save-1",
            source=SourceDescriptor(
                provider_kind="tianyi",
                source_form="share",
                canonical_source_id="tianyi:share:share-id",
                source_record_id="source-tianyi",
                source_record_revision=1,
            ),
            destination=DestinationDescriptor("tianyi-main", "tianyi"),
            locator=NativeLocator("folder_path", "root-folder-id::电影"),
            account_id="acc-tianyi",
            account_revision=9,
            category_key="movie",
        )

        receipt = adapter.native_save(request)
        verify_request = VerifyRequest(operation=receipt)

        self.assertEqual(calls, [("acc-tianyi", 9)])
        self.assertEqual(importer.folder_calls, [("root-folder-id", "电影")])
        self.assertEqual(importer.import_kwargs["save_path"], "resolved-folder-id")
        self.assertEqual(receipt.metadata["account_revision"], 9)
        self.assertEqual(adapter.poll_operation(receipt).status, "completed")
        self.assertEqual(adapter.verify_native_result(verify_request).status, "ok")
        self.assertEqual(adapter.cancel_operation(receipt).status, "cancelled")
        self.assertIs(importer.poll_receipt, receipt)
        self.assertIs(importer.verify_request, verify_request)
        self.assertIs(importer.cancel_receipt, receipt)

    def test_native_executor_rejects_old_verify_call_shape(self) -> None:
        adapter = _ProtocolAdapter()
        plan = make_plan(
            source_kind="quark", dest_kind="quark", strategy="native_save"
        )
        executor = NativeTransferExecutor(
            {"quark": adapter}, strategy="native_save"
        ).bind(TransferPlan(plan))
        with self.assertRaisesRegex(TypeError, "VerifyRequest"):
            executor.verify(plan)

    def test_sixpan_missing_lifecycle_fails_closed(self) -> None:
        importer = _ImporterWithoutNativeLifecycle()
        adapter = SixPanProviderAdapter(
            importer_factory=lambda _account_id, _revision: importer,
            account_importer_factory=lambda _values: importer,
            source_record_service=_SourceRecords(),
        )
        receipt = _receipt("sixpan-task-1", "sixpan")

        status = adapter.poll_operation(receipt)
        verified = adapter.verify_native_result(VerifyRequest(operation=receipt))
        cancelled = adapter.cancel_operation(receipt)

        self.assertEqual(status.status, "unknown")
        self.assertEqual(status.error_code, ErrorCode.RESULT_UNKNOWN)
        self.assertEqual(verified.status, "unknown")
        self.assertEqual(cancelled.status, "not_cancellable")

    def test_sixpan_lower_level_lifecycle_is_formal_adapter_contract(self) -> None:
        importer = _SixPanLifecycleImporter("completed")
        adapter = SixPanProviderAdapter(
            importer_factory=lambda _account_id, _revision: importer,
            account_importer_factory=lambda _values: importer,
            source_record_service=_SourceRecords(),
        )
        receipt = _receipt("sixpan-task-3", "sixpan")

        self.assertTrue(adapter.capabilities().supports_native_offline)
        self.assertEqual(adapter.poll_operation(receipt).status, "completed")
        self.assertEqual(
            adapter.verify_native_result(VerifyRequest(operation=receipt)).status,
            "ok",
        )
        self.assertEqual(adapter.cancel_operation(receipt).status, "cancel_pending")
        self.assertEqual(importer.deleted, [(["sixpan-task-3"], False)])

    def test_mobile_task_poll_and_verify_use_frozen_receipt(self) -> None:
        importer = _MobileLifecycleImporter("success")
        adapter = MobileProviderAdapter(
            importer_factory=lambda _account_id, _revision: importer,
            account_importer_factory=lambda _values: importer,
            source_record_service=_SourceRecords(),
        )
        receipt = _receipt("mobile-task-1", "mobile")

        self.assertTrue(adapter.capabilities().supports_relay_upload)
        self.assertEqual(adapter.poll_operation(receipt).status, "completed")
        self.assertEqual(
            adapter.verify_native_result(VerifyRequest(operation=receipt)).status,
            "ok",
        )

    def test_cleanup_failure_never_claims_completed(self) -> None:
        class _FailingCleanup:
            @staticmethod
            def cleanup(*_args, **_kwargs):
                return {
                    "ok": False,
                    "retryable": True,
                    "reason": "remote cleanup unavailable",
                }

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
        second = orchestrator.step()

        self.assertEqual(first.phase, PHASE_CLEANUP_PENDING)
        self.assertTrue(first.needs_wait)
        self.assertEqual(second.phase, PHASE_FAILED)

    def test_non_retryable_cleanup_failure_fails_closed(self) -> None:
        class _RefusedCleanup:
            @staticmethod
            def cleanup(*_args, **_kwargs):
                return {"ok": False, "retryable": False, "reason": "scope refused"}

        orchestrator = TransferOrchestrator(
            make_plan(), {}, cleanup=_RefusedCleanup()
        )
        orchestrator.state.phase = PHASE_CLEANUP_PENDING
        orchestrator.state.target_verified = True
        orchestrator.state.app_acked = True
        result = orchestrator.step()
        self.assertEqual(result.phase, PHASE_FAILED)


if __name__ == "__main__":
    unittest.main()
