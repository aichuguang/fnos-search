from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.process_role import resolve_process_role
from fnos_media_import.services.rclone_worker_runtime import RcloneWorkerRuntime
from fnos_media_import.services.readiness_service import (
    ReadinessService,
    routes_require_openlist,
)
from fnos_media_import.services.security_status_service import SecurityStatusService
from fnos_media_import.services.trending_discovery_scheduler import TrendingDiscoveryScheduler
from fnos_media_import.services.update_scheduler import UpdateScheduler
from fnos_media_import.services.worker_watchdog import WorkerWatchdog


class ProcessRoleConfigurationTests(unittest.TestCase):
    def test_role_is_required_in_every_environment(self) -> None:
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            with self.assertRaisesRegex(ValueError, "FNOS_PROCESS_ROLE is required"):
                resolve_process_role()
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            with self.assertRaisesRegex(ValueError, "FNOS_PROCESS_ROLE is required"):
                resolve_process_role()

    def test_explicit_roles_are_resolved_without_environment_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {"APP_ENV": "production", "FNOS_PROCESS_ROLE": "all"},
            clear=True,
        ):
            self.assertEqual(resolve_process_role(), "all")


class ReadinessServiceTests(unittest.TestCase):
    @staticmethod
    def _service(**overrides):
        arguments = {
            "process_role": "web",
            "database_probe": lambda: {"ok": True},
            "docker_socket_mounted": lambda: False,
            "root_key_probe": lambda: b"root-key",
            "active_provider_accounts_probe": lambda: 1,
            "openlist_required": lambda: False,
            "valid_openlist_mounts_probe": lambda: 0,
            "enabled_storage_targets_probe": lambda: 1,
            "remote_worker_status": lambda: {
                "success": True,
                "healthy": True,
                "status": "ready",
            },
            "local_worker_status": None,
        }
        arguments.update(overrides)
        return ReadinessService(**arguments)

    def test_web_is_ready_only_when_database_and_worker_are_ready(self) -> None:
        status = self._service().status()

        self.assertTrue(status["ok"])
        self.assertEqual(status["checks"]["worker"]["status"], "ready")

    def test_worker_unavailable_makes_web_unready(self) -> None:
        status = self._service(
            remote_worker_status=lambda: {
                "success": False,
                "healthy": False,
                "status": "worker_unavailable",
                "message": "无法连接 Worker",
            }
        ).status()

        self.assertFalse(status["ok"])
        self.assertEqual(status["checks"]["worker"]["status"], "worker_unavailable")

    def test_web_always_checks_the_explicitly_configured_worker(self) -> None:
        calls: list[str] = []
        status = self._service(
            remote_worker_status=lambda: calls.append("called") or {
                "success": True,
                "healthy": True,
                "status": "ready",
            },
        ).status()

        self.assertTrue(status["ok"])
        self.assertNotIn("deployment_layout", status["checks"])
        self.assertEqual(calls, ["called"])

    def test_docker_socket_and_database_failure_are_reported_separately(self) -> None:
        def fail_database():
            raise RuntimeError("secret database detail")

        status = self._service(
            database_probe=fail_database,
            docker_socket_mounted=lambda: True,
        ).status()

        self.assertFalse(status["ok"])
        self.assertEqual(status["checks"]["database"]["status"], "error")
        self.assertEqual(status["checks"]["docker_socket"]["status"], "mounted")
        self.assertNotIn("secret database detail", str(status))

    def test_worker_role_uses_local_core_runtime_status(self) -> None:
        status = self._service(
            process_role="all",
            remote_worker_status=None,
            local_worker_status=lambda: {
                "success": True,
                "healthy": False,
                "status": "unhealthy",
                "failed_checks": ["durable_worker"],
            },
        ).status()

        self.assertFalse(status["ok"])
        self.assertEqual(status["checks"]["worker_runtime"]["status"], "unhealthy")

    def test_root_key_failure_is_reported_without_echoing_secret_or_exception(self) -> None:
        status = self._service(
            root_key_probe=lambda: (_ for _ in ()).throw(RuntimeError("root-secret-value")),
        ).status()

        self.assertFalse(status["ok"])
        self.assertEqual(status["checks"]["root_key"]["status"], "invalid")
        self.assertNotIn("root-secret-value", str(status))

    def test_missing_active_provider_account_is_business_unconfigured(self) -> None:
        status = self._service(active_provider_accounts_probe=lambda: 0).status()

        self.assertTrue(status["ok"])
        self.assertEqual(status["checks"]["provider_accounts"]["active_count"], 0)
        self.assertEqual(status["checks"]["provider_accounts"]["status"], "unconfigured")
        self.assertFalse(status["checks"]["provider_accounts"]["required"])
        self.assertFalse(status["checks"]["provider_accounts"]["configured"])

    def test_openlist_is_not_checked_when_organizer_and_cross_disk_are_disabled(self) -> None:
        calls: list[str] = []
        status = self._service(
            openlist_required=lambda: False,
            valid_openlist_mounts_probe=lambda: calls.append("called") or 0,
        ).status()

        self.assertTrue(status["ok"])
        self.assertEqual(status["checks"]["openlist"]["status"], "not_required")
        self.assertEqual(calls, [])

    def test_required_openlist_without_valid_mount_blocks_readiness(self) -> None:
        status = self._service(
            openlist_required=lambda: True,
            valid_openlist_mounts_probe=lambda: 0,
        ).status()

        self.assertFalse(status["ok"])
        self.assertEqual(status["checks"]["openlist"]["status"], "unconfigured")

    def test_storage_target_absence_is_business_unconfigured_not_process_failure(self) -> None:
        status = self._service(enabled_storage_targets_probe=lambda: 0).status()

        self.assertTrue(status["ok"])
        self.assertFalse(status["checks"]["storage_targets"]["configured"])
        self.assertFalse(status["checks"]["storage_targets"]["required"])
        self.assertEqual(status["checks"]["storage_targets"]["status"], "unconfigured")

    def test_cross_provider_share_routes_require_openlist(self) -> None:
        sources = {
            "mobile": {"provider_kind": "mobile", "source_form": "share"},
            "sixpan_magnet": {
                "provider_kind": "sixpan",
                "source_form": "magnet",
            },
        }
        targets = {
            "mobile": SimpleNamespace(provider_kind="mobile", enabled=True),
            "quark": SimpleNamespace(provider_kind="quark", enabled=True),
            "sixpan": SimpleNamespace(provider_kind="sixpan", enabled=True),
        }

        self.assertFalse(
            routes_require_openlist(
                [{"search_source_id": "mobile", "destination_id": "mobile", "enabled": True}],
                sources,
                targets.get,
            )
        )
        self.assertTrue(
            routes_require_openlist(
                [{"search_source_id": "mobile", "destination_id": "quark", "enabled": True}],
                sources,
                targets.get,
            )
        )
        self.assertFalse(
            routes_require_openlist(
                [
                    {
                        "search_source_id": "sixpan_magnet",
                        "destination_id": "sixpan",
                        "enabled": True,
                    }
                ],
                sources,
                targets.get,
            )
        )


class SecurityStatusTests(unittest.TestCase):
    def test_socket_is_critical_without_legacy_layout_flag(self) -> None:
        service = SecurityStatusService(
            raw_config=lambda: {
                "admin": {"username": "owner", "password": "strong-password"},
            },
            settings=lambda: {},
            strict_enabled=lambda _raw: True,
            docker_socket_mounted=lambda: True,
            admin_profile_key="admin.profile",
        )

        result = service.build()

        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["critical_count"], 1)
        self.assertTrue(result["flags"]["docker_socket_mounted"])
        self.assertNotIn("deployment_degraded", result["flags"])


class WorkerWatchdogTests(unittest.TestCase):
    def test_grace_period_and_sustained_failure_trigger_termination(self) -> None:
        terminations: list[str] = []
        critical_logs: list[str] = []
        watchdog = WorkerWatchdog(
            status=lambda: {
                "success": True,
                "healthy": False,
                "status": "unhealthy",
                "failed_checks": ["durable_worker"],
            },
            terminate=lambda: terminations.append("exit"),
            log_critical=critical_logs.append,
            startup_grace_seconds=120,
            failure_timeout_seconds=90,
        )
        watchdog.started_at = 0

        self.assertFalse(watchdog.check_once(119))
        self.assertFalse(watchdog.check_once(120))
        self.assertFalse(watchdog.check_once(209.9))
        self.assertTrue(watchdog.check_once(210))
        self.assertEqual(terminations, ["exit"])
        self.assertIn("durable_worker", critical_logs[0])
        self.assertFalse(watchdog.check_once(400))
        self.assertEqual(terminations, ["exit"])

    def test_healthy_probe_resets_the_failure_window(self) -> None:
        states = iter([False, True, False, False])
        watchdog = WorkerWatchdog(
            status=lambda: {
                "success": True,
                "healthy": next(states),
                "status": "ready",
            },
            terminate=lambda: self.fail("watchdog terminated after recovery"),
            log_critical=lambda _message: None,
            startup_grace_seconds=0,
            failure_timeout_seconds=90,
        )
        watchdog.started_at = 0

        self.assertFalse(watchdog.check_once(0))
        self.assertFalse(watchdog.check_once(50))
        self.assertIsNone(watchdog.unhealthy_since)
        self.assertFalse(watchdog.check_once(100))
        self.assertFalse(watchdog.check_once(189))


class WebRoleSchedulerBoundaryTests(unittest.TestCase):
    def test_update_and_trending_schedulers_cannot_start_in_web_role(self) -> None:
        update_service = SimpleNamespace(db=SimpleNamespace(release_scheduler_lease=lambda *_args: True))
        update_scheduler = UpdateScheduler(
            update_service,
            enabled=False,
            process_role="web",
        )
        update_scheduler.apply_config(enabled=True)

        trending_scheduler = TrendingDiscoveryScheduler(
            service=SimpleNamespace(),
            database=SimpleNamespace(release_scheduler_lease=lambda *_args: True),
            owner_id="web",
            enabled=False,
            process_role="web",
        )
        trending_scheduler.apply_config(enabled=True)

        self.assertIsNone(update_scheduler.thread)
        self.assertIsNone(trending_scheduler.thread)

    def test_rclone_worker_runtime_has_no_periodic_scheduler(self) -> None:
        shutdown_calls: list[bool] = []
        runtime = RcloneWorkerRuntime(
            rclone_service=SimpleNamespace(
                shutdown=lambda: shutdown_calls.append(True)
            )
        )

        runtime.start()
        runtime.start()
        self.assertTrue(runtime.started)
        self.assertEqual([], shutdown_calls)

        runtime.shutdown()
        self.assertFalse(runtime.started)
        self.assertEqual([True], shutdown_calls)


if __name__ == "__main__":
    unittest.main()
