from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.services.runtime_reload_service import (
    RuntimeReloadService,
    finalize_organizer_runtime_transition,
)


class RcloneStartupRecoveryActivationTests(unittest.TestCase):
    def test_handler_registration_does_not_start_recovery_and_activation_is_idempotent(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.lock = threading.Lock()
        service._run_ready_handler = None
        service._direct_ready_handler = None
        service._pending_run_ready_results = []
        service._startup_recovery_activated = False
        calls: list[str] = []
        service.recover_waiting_organizer_dispatches = lambda: calls.append("organizer") or {"success": True}
        service.recover_unstarted_staging_jobs = lambda: calls.append("transfer") or {"success": True}

        service.set_run_ready_handler(lambda _result, _context: None)

        self.assertEqual(calls, [])
        first = service.activate_startup_recovery()
        second = service.activate_startup_recovery()
        self.assertTrue(first["success"])
        self.assertTrue(second["skipped"])
        self.assertEqual(calls, ["organizer", "transfer"])

    def test_failed_activation_can_be_retried_explicitly(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.lock = threading.Lock()
        service._startup_recovery_activated = False
        attempts = 0

        def recover_organizer():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("database unavailable")
            return {"success": True}

        service.recover_waiting_organizer_dispatches = recover_organizer
        service.recover_unstarted_staging_jobs = lambda: {"success": True}

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            service.activate_startup_recovery()
        result = service.activate_startup_recovery()

        self.assertTrue(result["success"])
        self.assertEqual(attempts, 2)


class RuntimeOrganizerTransitionTests(unittest.TestCase):
    def test_finalize_transition_switches_before_suspend_and_activation(self) -> None:
        events: list[str] = []

        class Organizer:
            def __init__(self, name: str) -> None:
                self.name = name

            def suspend_background(self) -> None:
                events.append(f"suspend:{self.name}")

            def activate_background_recovery(self, *, include_scanning: bool) -> None:
                events.append(f"activate:{self.name}:{include_scanning}")

        old_build = SimpleNamespace(organizer_service=Organizer("old"))
        new_build = SimpleNamespace(organizer_service=Organizer("new"))
        dispatcher = SimpleNamespace(
            set_organizer=lambda organizer: events.append(f"dispatch:{organizer.name}")
        )
        retirement = SimpleNamespace(retire=lambda build: events.append("retire:old" if build is old_build else "retire:other"))

        finalize_organizer_runtime_transition(
            dispatcher=dispatcher,
            previous_build=old_build,
            candidate_build=new_build,
            retirement=retirement,
        )

        self.assertEqual(
            events,
            ["dispatch:new", "suspend:old", "activate:new:False", "retire:old"],
        )

    def test_finalize_transition_keeps_web_only_organizer_background_suspended(self) -> None:
        events: list[str] = []

        class Organizer:
            def __init__(self, name: str) -> None:
                self.name = name

            def suspend_background(self) -> None:
                events.append(f"suspend:{self.name}")

            def activate_background_recovery(self, *, include_scanning: bool) -> None:
                events.append(f"activate:{self.name}:{include_scanning}")

        old_build = SimpleNamespace(organizer_service=Organizer("old"))
        new_build = SimpleNamespace(organizer_service=Organizer("new"))
        dispatcher = SimpleNamespace(
            set_organizer=lambda organizer: events.append(f"dispatch:{organizer.name}")
        )
        retirement = SimpleNamespace(retire=lambda _build: events.append("retire:old"))

        finalize_organizer_runtime_transition(
            dispatcher=dispatcher,
            previous_build=old_build,
            candidate_build=new_build,
            retirement=retirement,
            activate_background=False,
        )

        self.assertEqual(events, ["dispatch:new", "suspend:old", "retire:old"])

    def test_reload_service_does_not_publish_or_retire_organizer_early(self) -> None:
        events: list[str] = []

        class Organizer:
            def status(self) -> dict:
                return {"enabled": True}

            def suspend_background(self) -> None:
                events.append("suspend")

            def activate_background_recovery(self, **_kwargs) -> None:
                events.append("activate")

        def build(name: str):
            return SimpleNamespace(
                pansou=f"pansou:{name}",
                btbtla=f"btbtla:{name}",
                quark_importer=f"quark:{name}",
                cloud139_importer=f"cloud139:{name}",
                generic_importers={},
                fnos=SimpleNamespace(describe=lambda: {"configured": True}),
                search_service=f"search:{name}",
                import_service=f"import:{name}",
                organizer_service=Organizer(),
                close=lambda: None,
            )

        old_build = build("old")
        candidate = build("new")
        old_config = SimpleNamespace(
            raw={
                "app": {"secret_key": "persisted-runtime-secret"},
                "security": {"ip_hash_salt": "persisted-ip-salt"},
                "update_scheduler": {},
                "hot_discovery": {},
                "rclone": {},
                "fnos": {},
            },
            categories={},
        )
        new_config = SimpleNamespace(
            raw={
                "app": {"secret_key": "change-me-in-production"},
                "update_scheduler": {},
                "hot_discovery": {},
                "rclone": {},
                "fnos": {},
                "security": {"ip_hash_salt": ""},
            },
            categories={},
        )

        class Builder:
            def build(self, config, *, recover_background: bool = True):
                events.append(f"build:{recover_background}")
                self.config = config
                return candidate

        dispatcher = SimpleNamespace(set_organizer=lambda _organizer: events.append("dispatch"))
        retirement = SimpleNamespace(retire=lambda _build: events.append("retire"))
        runtime_services = SimpleNamespace(swap=lambda _snapshot: events.append("swap") or 2)
        update_service = SimpleNamespace(set_runtime=lambda **_kwargs: None)
        update_scheduler = SimpleNamespace(apply_config=lambda **_kwargs: None)
        trending_scheduler = SimpleNamespace(apply_config=lambda **_kwargs: None)
        rclone_service = SimpleNamespace(
            apply_runtime_config=lambda *_args: None,
            status=lambda: {"enabled": True},
        )
        service = RuntimeReloadService(
            load_config=lambda: new_config,
            builder=Builder(),
            runtime_services=runtime_services,
            retirement=retirement,
            database=object(),
            job_service=object(),
            rclone_service=rclone_service,
            update_service=update_service,
            update_scheduler=update_scheduler,
            trending_scheduler=trending_scheduler,
            config_bool=lambda config, key, default: bool(config.get(key, default)),
            config_int=lambda config, key, default: int(config.get(key, default)),
            rollback_logger=lambda: None,
            advanced_config_key="advanced",
            organizer_dispatcher=dispatcher,
        )

        result = service.reload(old_config, old_build)

        self.assertIs(result.build, candidate)
        self.assertTrue(result.response["success"])
        self.assertIn("update_scheduler", result.response)
        self.assertIn("trending_discovery", result.response)
        self.assertIn("swap", events)
        self.assertNotIn("dispatch", events)
        self.assertNotIn("suspend", events)
        self.assertNotIn("activate", events)
        self.assertNotIn("retire", events)
        self.assertEqual(result.config.raw["app"]["secret_key"], "persisted-runtime-secret")
        self.assertEqual(result.config.raw["security"]["ip_hash_salt"], "persisted-ip-salt")


if __name__ == "__main__":
    unittest.main()
