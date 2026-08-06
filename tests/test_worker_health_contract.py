from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.process_role import legacy_deployment_layout, resolve_process_role
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.services.readiness_service import ReadinessService
from fnos_media_import.services.security_status_service import SecurityStatusService
from fnos_media_import.services.trending_discovery_scheduler import TrendingDiscoveryScheduler
from fnos_media_import.services.update_scheduler import UpdateScheduler
from fnos_media_import.services.worker_watchdog import WorkerWatchdog


class ProcessRoleCompatibilityTests(unittest.TestCase):
    def test_production_without_explicit_role_degrades_to_web(self) -> None:
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            self.assertTrue(legacy_deployment_layout())
            self.assertEqual(resolve_process_role(), "web")

    def test_development_without_explicit_role_keeps_all_in_one_runtime(self) -> None:
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            self.assertFalse(legacy_deployment_layout())
            self.assertEqual(resolve_process_role(), "all")

    def test_explicit_production_role_disables_legacy_degradation(self) -> None:
        with patch.dict(
            os.environ,
            {"APP_ENV": "production", "FNOS_PROCESS_ROLE": "all"},
            clear=True,
        ):
            self.assertFalse(legacy_deployment_layout())
            self.assertEqual(resolve_process_role(), "all")


class ReadinessServiceTests(unittest.TestCase):
    @staticmethod
    def _service(**overrides):
        arguments = {
            "process_role": "web",
            "database_probe": lambda: {"ok": True},
            "deployment_degraded": lambda: False,
            "docker_socket_mounted": lambda: False,
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

    def test_legacy_compose_is_degraded_without_contacting_worker(self) -> None:
        calls: list[str] = []
        status = self._service(
            deployment_degraded=lambda: True,
            remote_worker_status=lambda: calls.append("called") or {},
        ).status()

        self.assertFalse(status["ok"])
        self.assertEqual(status["checks"]["deployment_layout"]["status"], "legacy_compose")
        self.assertEqual(status["checks"]["worker"]["status"], "upgrade_required")
        self.assertEqual(calls, [])

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


class SecurityStatusCompatibilityTests(unittest.TestCase):
    def test_socket_and_legacy_layout_are_both_critical(self) -> None:
        service = SecurityStatusService(
            raw_config=lambda: {
                "admin": {"username": "owner", "password": "strong-password"},
                "app": {"secret_key": "stable-secret"},
            },
            settings=lambda: {},
            strict_enabled=lambda _raw: True,
            default_secret=lambda _value: False,
            docker_socket_mounted=lambda: True,
            deployment_degraded=lambda: True,
            admin_profile_key="admin.profile",
        )

        result = service.build()

        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["critical_count"], 2)
        self.assertTrue(result["flags"]["docker_socket_mounted"])
        self.assertTrue(result["flags"]["deployment_degraded"])


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

    def test_rclone_scheduler_stays_stopped_after_web_runtime_reload(self) -> None:
        restart_calls: list[int] = []
        service = object.__new__(RcloneService)
        service.config = {"auto_interval_minutes": 0}
        service.fnos_config = {}
        service.categories = {}
        service.cmcc_upload_config = {}
        service.cloud139_config = {}
        service.enabled = True
        service.scheduler_allowed = False
        service.environment_checker = SimpleNamespace(apply_config=lambda _config: None)
        service.worker_command = SimpleNamespace(apply_config=lambda _config: None)
        service.log_sink = SimpleNamespace(resize=lambda _limit: None)
        service.scheduler = SimpleNamespace(restart=restart_calls.append)

        service.apply_runtime_config(
            {"auto_interval_minutes": 15, "enabled": True},
            {},
            {},
            {},
            {},
        )

        self.assertEqual(restart_calls, [])

    def test_rclone_cold_start_guard_blocks_web_scheduler(self) -> None:
        start_calls: list[int] = []
        service = object.__new__(RcloneService)
        service.scheduler_allowed = False
        service.lock = threading.Lock()
        service.config = {"auto_interval_minutes": 15}
        service.scheduler = SimpleNamespace(start=start_calls.append)

        service.start_scheduler()

        self.assertEqual(start_calls, [])


if __name__ == "__main__":
    unittest.main()
