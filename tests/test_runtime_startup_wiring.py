from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.services.runtime_reload_service import (
    RuntimeReloadService,
    finalize_organizer_runtime_transition,
)


class RcloneStartupRecoveryActivationTests(unittest.TestCase):
    def test_scoped_v1_recovery_activation_is_idempotent(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.db = None
        service.lock = threading.Lock()
        service._startup_recovery_activated = False
        calls: list[str] = []
        service.recover_scoped_runs = lambda: calls.append("scoped") or {"success": True}

        first = service.activate_startup_recovery()
        second = service.activate_startup_recovery()
        self.assertTrue(first["success"])
        self.assertTrue(second["skipped"])
        self.assertEqual(calls, ["scoped"])
        self.assertNotIn("waiting_organizer", first)
        self.assertNotIn("waiting_transfer", first)

    def test_failed_activation_can_be_retried_explicitly(self) -> None:
        service = RcloneService.__new__(RcloneService)
        service.db = None
        service.lock = threading.Lock()
        service._startup_recovery_activated = False
        attempts = 0

        def recover_scoped():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("database unavailable")
            return {"success": True}

        service.recover_scoped_runs = recover_scoped

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            service.activate_startup_recovery()
        result = service.activate_startup_recovery()

        self.assertTrue(result["success"])
        self.assertEqual(attempts, 2)


class RuntimeCompositionSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[1] / "fnos_media_import" / "app.py"
        ).read_text(encoding="utf-8")

    def test_v1_rclone_locator_is_shared_and_hot_reloaded_in_place(self) -> None:
        start = self.source.index("locator_config = rclone_runtime_config(app_config)")
        end = self.source.index("transfer_recovery_scanner =", start)
        startup = self.source[start:end]

        self.assertIn(
            "v1_rclone_locator = RcloneLocatorResolver(locator_config)", startup
        )
        self.assertIn("v1_rclone_locator.bind_revision_services(", startup)
        self.assertIn("rclone_service.set_v1_locator_resolver(", startup)
        self.assertIn("locator_resolver=v1_rclone_locator", startup)
        self.assertIn("runtime_installer = RuntimeInstaller(", startup)
        self.assertNotIn("_wire_v1_runtime_build", self.source)

    def test_runtime_installer_never_reassigns_transfer_dispatch_fields(self) -> None:
        installer_source = (
            Path(__file__).resolve().parents[1]
            / "fnos_media_import"
            / "runtime_builder.py"
        ).read_text(encoding="utf-8")
        start = installer_source.index("class RuntimeInstaller:")
        installer_source = installer_source[start:]

        self.assertIn("self.locator_resolver.update_config(locator_config)", installer_source)
        self.assertIn(
            "self.transfer_dispatch.set_organizer_service(build.organizer_service)",
            installer_source,
        )
        self.assertNotIn("self.transfer_dispatch.locator_resolver =", installer_source)
        self.assertNotIn("self.transfer_dispatch.rclone_config =", installer_source)

    def test_v1_master_key_fails_closed_without_empty_key_fallback(self) -> None:
        startup_start = self.source.index("root_key = master_key_from_env()")
        startup_end = self.source.index("active_process_role =", startup_start)
        startup_block = self.source[startup_start:startup_end]
        provider_start = self.source.index("def _v1_master_key() -> bytes:")
        provider_end = self.source.index("v1_accounts_service =", provider_start)
        provider_block = self.source[provider_start:provider_end]

        self.assertIn("raise RuntimeError(", startup_block)
        self.assertIn("return root_key", provider_block)
        self.assertNotIn('return b""', startup_block + provider_block)
        self.assertNotIn("return b''", startup_block + provider_block)
        self.assertNotIn("\n    _v1_master_key()\n", provider_block)


class RuntimeOrganizerTransitionTests(unittest.TestCase):
    def test_finalize_transition_runs_lifecycle_after_installer_publication(self) -> None:
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
        retirement = SimpleNamespace(retire=lambda build: events.append("retire:old" if build is old_build else "retire:other"))

        finalize_organizer_runtime_transition(
            previous_build=old_build,
            candidate_build=new_build,
            retirement=retirement,
        )

        self.assertEqual(
            events,
            ["suspend:old", "activate:new:False", "retire:old"],
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
        retirement = SimpleNamespace(retire=lambda _build: events.append("retire:old"))

        finalize_organizer_runtime_transition(
            previous_build=old_build,
            candidate_build=new_build,
            retirement=retirement,
            activate_background=False,
        )

        self.assertEqual(events, ["suspend:old", "retire:old"])

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
                "app": {},
                "update_scheduler": {},
                "hot_discovery": {},
                "rclone": {},
                "fnos": {},
                "security": {"ip_hash_salt": ""},
                "storage": {"targets": {"mobile_backup": {"driver": "cmcc_api"}}},
            },
            categories={},
        )

        class Builder:
            def build(self, config, *, recover_background: bool = True):
                events.append(f"build:{recover_background}")
                self.config = config
                return candidate

        retirement = SimpleNamespace(retire=lambda _build: events.append("retire"))
        runtime_services = SimpleNamespace(swap=lambda _snapshot: events.append("swap") or 2)
        update_service = SimpleNamespace(set_runtime=lambda **_kwargs: None)
        update_scheduler = SimpleNamespace(apply_config=lambda **_kwargs: None)
        trending_scheduler = SimpleNamespace(apply_config=lambda **_kwargs: None)
        rclone_apply_args: list[tuple] = []
        rclone_service = SimpleNamespace(
            apply_runtime_config=lambda *args: rclone_apply_args.append(args),
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
        self.assertNotIn("secret_key", result.config.raw["app"])
        self.assertEqual(result.config.raw["security"]["ip_hash_salt"], "persisted-ip-salt")
        # V1 runtime reload must not re-introduce the removed global storage
        # registry, even when a stale in-memory config object still contains it.
        self.assertEqual(rclone_apply_args[-1][-1], {})


if __name__ == "__main__":
    unittest.main()
