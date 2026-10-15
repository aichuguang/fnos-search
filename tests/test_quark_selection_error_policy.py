from __future__ import annotations

import unittest
from unittest.mock import Mock

from fnos_media_import.importers.base import ImportResult
from fnos_media_import.transfer import ErrorCode, TransferError, receipt_from_import_result
from fnos_media_import.transfer.orchestrator import TransferOrchestrator
from tests.test_transfer_v1 import make_plan


class QuarkSelectionErrorPolicyTests(unittest.TestCase):
    def test_invalid_selection_preserves_non_retryable_error_and_does_not_spin(self):
        result = ImportResult(
            False, "failed", "所选文件 ID 未匹配；本次未提交转存",
            raw_data={"provider": "quark", "error_code": ErrorCode.SHARE_INVALID},
        )
        receipt = receipt_from_import_result(result, provider_kind="quark")
        self.assertEqual(receipt.error_code, ErrorCode.SHARE_INVALID)
        self.assertFalse(TransferError(receipt.error_code, receipt.message).retryable)
        executor = Mock(needs_source_landing=True)
        executor.materialize_source.return_value = receipt
        plan = make_plan(
            source_kind="quark", dest_kind="mobile", strategy="relay_upload_provider",
            landing="/_入库暂存/job-1", final="/电影", local_temp="/data/relay/job-1",
        )
        orchestrator = TransferOrchestrator(plan, {"relay_upload_provider": executor})
        outcome = orchestrator.run()
        self.assertEqual(outcome.phase, "failed")
        self.assertEqual(orchestrator.state.error_code, ErrorCode.SHARE_INVALID)
        self.assertNotIn("重试次数耗尽", outcome.message)
        executor.materialize_source.assert_called_once_with()
        executor.submit.assert_not_called()

    def test_transient_and_legacy_failures_keep_existing_retry_contract(self):
        transient = receipt_from_import_result(
            ImportResult(False, "failed", "network unavailable", raw_data={"error_code": ErrorCode.NETWORK_TRANSIENT}),
            provider_kind="quark",
        )
        self.assertEqual(transient.error_code, ErrorCode.NETWORK_TRANSIENT)
        self.assertTrue(TransferError(transient.error_code, transient.message).retryable)
        legacy = receipt_from_import_result(ImportResult(False, "failed", "legacy"), provider_kind="quark")
        self.assertEqual(legacy.error_code, "")


if __name__ == "__main__":
    unittest.main()
