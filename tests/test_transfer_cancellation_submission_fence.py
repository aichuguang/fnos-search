from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fnos_media_import.transfer.dispatch import TransferDispatchService
from tests.test_job_cancellation_v1 import _Cleaner, _Execution, _Jobs, _service


class CancellationSubmissionFenceTests(unittest.TestCase):
    def test_admin_cancel_unknown_submission_preserves_job_and_artifacts(self):
        for phase in ("retry_wait", "manual_review", "failed"):
            for fence_state in ("prepared", "committed"):
                with self.subTest(phase=phase, fence=fence_state):
                    jobs, cleaner = _Jobs(status="review" if phase == "manual_review" else "transferring"), _Cleaner()
                    execution = _Execution(phase, snapshot={"submission_fence": {"state": fence_state}})
                    service = _service(jobs, cleaner, execution=execution)
                    result = service.cancel(
                        jobs.get(42), reason="admin cancel", payload={"cleanup": True}, request_item=None,
                        cleanup_default=True, stop_running_default=True, admin_username="admin",
                    )
                    self.assertFalse(result["cancelled"])
                    self.assertTrue(result["state_conflict"])
                    self.assertNotEqual(jobs.get(42)["status"], "cancelled")
                    self.assertEqual(cleaner.cancel_calls, [])
                    self.assertTrue(service._should_defer_transfer_cleanup(42))

    def test_already_cancelled_worker_never_cleans_prepared_submission_without_receipt(self):
        dispatch = TransferDispatchService.__new__(TransferDispatchService)
        dispatch._persist_manual_review = Mock()
        dispatch._cleanup_cancelled_artifacts = Mock()
        for fence_state in ("prepared", "committed"):
            execution = SimpleNamespace(
                phase="retry_wait", receipt=None,
                snapshot={"submission_fence": {"state": fence_state}},
            )
            result = dispatch._stop_if_job_cancelled({"id": 42, "status": "cancelled"}, execution, "worker")
            self.assertEqual(result["phase"], "manual_review")
            self.assertFalse(result["cancelled"])
        dispatch._cleanup_cancelled_artifacts.assert_not_called()
        self.assertEqual(dispatch._persist_manual_review.call_count, 2)


if __name__ == "__main__":
    unittest.main()
