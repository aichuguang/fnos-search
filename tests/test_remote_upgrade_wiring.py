from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.services.rclone_webdav_config_service import RcloneWebdavConfigError


ROOT = Path(__file__).resolve().parents[1]


class RemoteUpgradeWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "fnos_media_import" / "app.py").read_text(encoding="utf-8")

    def test_startup_confirms_before_consuming_or_recovering_transfers(self):
        startup = self.source[self.source.index("        rclone_worker_runtime.start()") :]
        confirm = startup.index("remote_bootstrap = _ensure_worker_remote_confirmation()")
        self.assertLess(confirm, startup.index("transfer_recovery_scanner.scan_once()"))
        self.assertLess(confirm, startup.index("durable_worker_runtime.start()"))

    def test_preflight_uses_worker_local_confirmation_not_web_local_files(self):
        start = self.source.index("    v1_transfer_dispatch = TransferDispatchService(")
        wiring = self.source[start : self.source.index('app.extensions["v1_rclone_locator"]', start)]
        self.assertIn("ensure_remote_confirmation=(", wiring)
        self.assertIn("_ensure_worker_remote_confirmation", wiring)
        self.assertIn('if role_runs(active_process_role, "worker") else None', wiring)

    def test_actual_confirmation_callback_respects_disabled_rclone(self):
        tree = ast.parse(self.source)
        callback = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                        and node.name == "_ensure_worker_remote_confirmation")
        module = ast.fix_missing_locations(ast.Module(body=[callback], type_ignores=[]))
        config = SimpleNamespace(ensure_confirmation=Mock(return_value={"success": True, "sync_status": "confirmed"}))
        runtime = SimpleNamespace(enabled=False)
        namespace = {"Any": Any, "rclone_service": runtime, "rclone_webdav_config_service": config,
                     "RcloneWebdavConfigError": RcloneWebdavConfigError}
        exec(compile(module, "app-worker-confirmation", "exec"), namespace)
        with self.assertRaises(RcloneWebdavConfigError) as error:
            namespace["_ensure_worker_remote_confirmation"]()
        self.assertEqual(error.exception.code, "RCLONE_DISABLED")
        config.ensure_confirmation.assert_not_called()
        runtime.enabled = True
        self.assertEqual(namespace["_ensure_worker_remote_confirmation"]()["sync_status"], "confirmed")
        config.ensure_confirmation.assert_called_once_with()

    def test_run_fallback_does_not_skip_confirmation_for_an_existing_remote(self):
        for confirmed in (True, False):
            with self.subTest(auto_confirmed=confirmed):
                config = SimpleNamespace(
                    ensure_confirmation=Mock(return_value={"success": True, "auto_confirmed": confirmed}),
                    status=Mock(return_value={"configured": True}), save=Mock(),
                )
                runtime = object.__new__(RcloneService)
                runtime._v1_webdav_config_service = config
                runtime._append_log = Mock()
                runtime._ensure_managed_remote_config()
                config.ensure_confirmation.assert_called_once_with()
                config.status.assert_not_called()
                config.save.assert_not_called()
                self.assertEqual(runtime._append_log.call_count, int(confirmed))

    def test_real_confirmation_error_propagates_without_rewriting_or_logging_secrets(self):
        config = SimpleNamespace(
            ensure_confirmation=Mock(side_effect=RcloneWebdavConfigError("secret=password", code="RCLONE_SYNC_CONFLICT")),
            save=Mock(),
        )
        runtime = object.__new__(RcloneService)
        runtime._v1_webdav_config_service = config
        runtime._append_log = Mock()
        with self.assertRaises(RcloneWebdavConfigError) as error:
            runtime._ensure_managed_remote_config()
        self.assertEqual(error.exception.code, "RCLONE_SYNC_CONFLICT")
        runtime._append_log.assert_not_called()
        config.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
