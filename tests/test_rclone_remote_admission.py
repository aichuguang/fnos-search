from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fnos_media_import.services.rclone_process_runner import ProcessResult
from fnos_media_import.services.rclone_webdav_config_service import RcloneWebdavConfigError
from tests.test_rclone_completion_recovery import _RcloneRecoveryFixture
from tests.test_rclone_lock_busy_and_relay_reconcile import _ImmediateTimer


class RcloneRemoteAdmissionTests(_RcloneRecoveryFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        del self.service._ensure_managed_remote_config
        self.service._v1_locator_resolver = SimpleNamespace(remote_name="MP")
        self.remote = SimpleNamespace(ensure_confirmation=Mock(return_value={
            "success": True, "sync_status": "confirmed", "auto_confirmed": False,
        }))
        self.service.set_webdav_config_service(self.remote)

    def test_real_confirmation_error_propagates_from_admission_helper(self) -> None:
        error = RcloneWebdavConfigError("Remote identity changed", 409, code="RCLONE_SYNC_CONFLICT")
        self.remote.ensure_confirmation.side_effect = error
        with self.assertRaises(RcloneWebdavConfigError) as raised:
            self.service._ensure_managed_remote_config()
        self.assertIs(raised.exception, error)

    def test_late_remote_drift_finishes_claimed_run_without_spawning(self) -> None:
        self.remote.ensure_confirmation.side_effect = RcloneWebdavConfigError(
            "Remote identity changed", 409, code="RCLONE_SYNC_CONFLICT",
        )
        self.service._run_script("admission-test", staging_run=self.descriptor)

        self.remote.ensure_confirmation.assert_called_once()
        self.service.process_runner.run.assert_not_called()
        self.assertEqual(self._status(), "failed")
        row = self.repo.get_rclone_run(8)
        self.assertEqual(row["exit_code"], 2)
        self.assertIn("RCLONE_SYNC_CONFLICT", row["error_message"])
        self.assertIn("进程未启动", row["error_message"])
        self.assertEqual(self.service.run_state.status, "failed")
        self.assertIsNone(self.service.run_state.current_run_id)
        self.assertIsNone(self.service.process)
        self.service._start_next_queued_run.assert_called_once()
        self.assertEqual(self.wakeups, [9])

    def test_unexpected_confirmation_error_is_redacted_and_does_not_spawn(self) -> None:
        self.remote.ensure_confirmation.side_effect = RuntimeError("Authorization: private-token-value")
        self.service._run_script("admission-test", staging_run=self.descriptor)

        self.service.process_runner.run.assert_not_called()
        self.assertEqual(self._status(), "failed")
        self.assertIn("RCLONE_SYNC_FAILED", self.repo.get_rclone_run(8)["error_message"])
        self.assertNotIn("private-token-value", "\n".join(self.logs))
        self.assertNotIn("private-token-value", self.repo.get_rclone_run(8)["error_message"])

    def test_unsuccessful_or_missing_confirmation_result_is_not_accepted(self) -> None:
        for result in ({"success": False}, {}, None, True):
            with self.subTest(result=result):
                self.remote.ensure_confirmation.return_value = result
                with self.assertRaises(RcloneWebdavConfigError) as raised:
                    self.service._ensure_managed_remote_config()
                self.assertEqual(raised.exception.code, "RCLONE_SYNC_RESULT_UNKNOWN")
        self.remote.ensure_confirmation.return_value = {"success": False}
        self.service._run_script("admission-test", staging_run=self.descriptor)
        self.service.process_runner.run.assert_not_called()
        self.assertEqual(self._status(), "failed")

    def test_failed_result_persistence_retries_completion_not_process(self) -> None:
        self.remote.ensure_confirmation.side_effect = RcloneWebdavConfigError(
            "Remote verification unavailable", 503, code="RCLONE_SYNC_RESULT_UNKNOWN",
        )
        self._fail_once("update_rclone_run")
        self.service._run_script("admission-test", staging_run=self.descriptor)

        self.assertEqual(self._status(), "running")
        self.assertEqual(self.service.run_state.status, "failed")
        self.assertIsNone(self.service.run_state.current_run_id)
        self.assertIsNone(self.service.process)
        self.assertEqual(len(self.service._completion_retry_timers), 1)
        _ImmediateTimer.instances[-1].fire()
        self.assertEqual(self._status(), "failed")
        self.assertEqual(self.wakeups, [9])
        self.service.process_runner.run.assert_not_called()
        self.assertEqual(self.service.start_calls, [])

    def test_successful_confirmation_occurs_before_process_start(self) -> None:
        calls = []

        def confirm():
            calls.append("confirm")
            self.assertEqual(self._status(), "running")
            return {"success": True, "sync_status": "confirmed", "auto_confirmed": True}

        def run(*_args, **_kwargs):
            calls.append("spawn")
            return ProcessResult(0)

        self.remote.ensure_confirmation.side_effect = confirm
        self.service.process_runner.run.side_effect = run
        self.service._run_script("admission-test", staging_run=self.descriptor)

        self.assertEqual(calls, ["confirm", "spawn"])
        self.assertEqual(self._status(), "success")
        self.assertEqual(self.wakeups, [9])
        self.service._start_next_queued_run.assert_called_once()

    def test_legacy_service_without_ensure_retains_compatibility_path(self) -> None:
        legacy = SimpleNamespace(
            status=Mock(side_effect=RuntimeError("legacy status unavailable")), save=Mock(),
        )
        self.service.set_webdav_config_service(legacy)
        self.service._run_script("legacy-test", staging_run=self.descriptor)

        legacy.status.assert_called_once_with("MP")
        legacy.save.assert_not_called()
        self.service.process_runner.run.assert_called_once()
        self.assertEqual(self._status(), "success")


if __name__ == "__main__":
    unittest.main()
