from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import copy
import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any

from fnos_media_import.config_persistence import (
    ADVANCED_CONFIG_EXPORT_FORMAT,
    ADVANCED_CONFIG_EXPORT_VERSION,
    advanced_config_response,
    migrate_stored_advanced_config,
    normalize_advanced_config_payload,
    persisted_config_from_settings,
)
from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.services.settings_service import SettingsDependencies, SettingsService


WORK_TEST_ROOT = Path.cwd() / "codex_tmp" / "config-export-tests"


class _FakeAppSettings(RepositoryFakeMixin):
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = copy.deepcopy(settings or {})

    def get_all(self) -> dict[str, Any]:
        return copy.deepcopy(self._settings)

    def set_many(self, updates: dict[str, Any]) -> None:
        self._settings.update(copy.deepcopy(updates))

    def update_atomic(self, key: str, updater: Any) -> tuple[bool, Any, Any]:
        existed = key in self._settings
        previous = copy.deepcopy(self._settings.get(key))
        updated = updater(copy.deepcopy(previous), existed)
        self._settings[key] = copy.deepcopy(updated)
        return existed, previous, copy.deepcopy(updated)

    def mutate_all_atomic(self, updater: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        previous = copy.deepcopy(self._settings)
        updates, delete_keys = updater(copy.deepcopy(previous))
        for key in delete_keys:
            self._settings.pop(key, None)
        self._settings.update(copy.deepcopy(updates))
        return previous, copy.deepcopy(self._settings)

    def compare_and_set(
        self,
        key: str,
        expected: Any,
        replacement: Any,
        *,
        expected_exists: bool = True,
        replacement_exists: bool = True,
    ) -> bool:
        if (key in self._settings) != expected_exists:
            return False
        if self._settings.get(key) != expected:
            return False
        if replacement_exists:
            self._settings[key] = copy.deepcopy(replacement)
        else:
            self._settings.pop(key, None)
        return True


class _FakeDb(RepositoryFakeMixin):
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.app_settings = _FakeAppSettings(settings)


def _document(stored: dict[str, Any], *, effective: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "format": ADVANCED_CONFIG_EXPORT_FORMAT,
        "version": ADVANCED_CONFIG_EXPORT_VERSION,
        "stored": copy.deepcopy(stored),
        **({"effective": copy.deepcopy(effective)} if effective is not None else {}),
    }


def _payload(
    stored: dict[str, Any],
    *,
    mode: str = "merge",
    scope: str = "stored",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "config": _document(stored),
        "mode": mode,
        "scope": scope,
        **extra,
    }


def _service(
    db: Any,
    *,
    raw_config: dict[str, Any] | None = None,
    reload_runtime: Any = lambda: {},
) -> SettingsService:
    raw = copy.deepcopy(raw_config or {})

    def payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
        value = payload.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    return SettingsService(
        SettingsDependencies(
            db=db,
            raw_config=lambda: copy.deepcopy(raw),
            redact_config=lambda config: copy.deepcopy(config),
            advanced_response=advanced_config_response,
            normalize_advanced=normalize_advanced_config_payload,
            advanced_key="advanced_config",
            reload_runtime=reload_runtime,
            effective_settings=lambda: {},
            payload_bool=payload_bool,
            search_providers=lambda: [{"key": "pansou", "name": "PanSou"}],
        )
    )


class _WorkspaceDatabase:
    def __enter__(self) -> RepositorySet:
        WORK_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = WORK_TEST_ROOT / f"settings-{uuid.uuid4().hex}.db"
        self.database = Database(self.path)
        self.database.init_schema()
        return RepositorySet.from_database(self.database)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.database.close_all_connections()
        except AttributeError:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                (self.path.parent / f"{self.path.name}{suffix}").unlink()
            except FileNotFoundError:
                pass
        try:
            if self.path.parent.exists() and not any(self.path.parent.iterdir()):
                shutil.rmtree(self.path.parent)
        except OSError:
            pass


class ConfigExportImportV1Tests(unittest.TestCase):
    def test_bare_patch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "format.*version"):
            normalize_advanced_config_payload({"config": {"pansou": {"timeout": 10}}})

    def test_versioned_merge_and_replace(self) -> None:
        current = {"pansou": {"base_url": "old", "src": "all"}}
        merged = normalize_advanced_config_payload(
            _payload({"pansou": {"base_url": "new"}}),
            current_stored=current,
        )
        self.assertEqual(merged, {"pansou": {"base_url": "new", "src": "all"}})

        replaced = normalize_advanced_config_payload(
            _payload({"pansou": {"base_url": "new"}}, mode="replace"),
            current_stored=current,
        )
        self.assertEqual(replaced, {"pansou": {"base_url": "new"}})

    def test_merge_preserves_explicit_empty_string_and_list_overrides(self) -> None:
        current = {
            "pansou": {
                "base_url": "http://pansou.internal",
                "channels": ["channel-a"],
            }
        }
        merged = normalize_advanced_config_payload(
            _payload({"pansou": {"base_url": "", "channels": []}}),
            current_stored=current,
        )
        self.assertEqual(merged["pansou"]["base_url"], "")
        self.assertEqual(merged["pansou"]["channels"], [])

    def test_provider_priority_is_normalized_without_filtering_missing_types(self) -> None:
        normalized = normalize_advanced_config_payload(
            _payload(
                {
                    "pansou": {
                        "result_provider_priority": [
                            "mobile",
                            "invalid",
                            "mobile",
                            "quark",
                        ]
                    }
                },
                mode="replace",
            )
        )

        self.assertEqual(
            normalized["pansou"]["result_provider_priority"],
            ["mobile", "quark", "tianyi", "magnet"],
        )

    def test_provider_badges_are_normalized_and_validated(self) -> None:
        normalized = normalize_advanced_config_payload(
            _payload(
                {
                    "pansou": {
                        "cloud_types": [],
                        "result_provider_badges": {
                            "mobile": {"enabled": True, "text": "秒入库"},
                            "magnet": {"enabled": True, "text": "快速入库"},
                        },
                    }
                }
            )
        )

        self.assertEqual(normalized["pansou"]["cloud_types"], [])
        self.assertEqual(
            normalized["pansou"]["result_provider_badges"],
            {
                "quark": {"enabled": False, "text": ""},
                "mobile": {"enabled": True, "text": "秒入库"},
                "tianyi": {"enabled": False, "text": ""},
                "magnet": {"enabled": True, "text": "快速入库"},
            },
        )

        for badges in (
            {"mobile": {"enabled": True, "text": "秒"}},
            {"mobile": {"enabled": True, "text": "超过五个字符"}},
            {"mobile": {"enabled": True, "text": ""}},
            {"mobile": {"enabled": True, "text": "fast"}},
            {"mobile": {"enabled": True, "text": "秒save"}},
            {"unknown": {"enabled": True, "text": "测试角标"}},
        ):
            with self.subTest(badges=badges), self.assertRaises(ValueError):
                normalize_advanced_config_payload(
                    _payload({"pansou": {"result_provider_badges": badges}})
                )

    def test_legacy_non_chinese_provider_badge_does_not_block_startup(self) -> None:
        normalized = persisted_config_from_settings(
            {
                "advanced_config": {
                    "pansou": {
                        "result_provider_badges": {
                            "quark": {"enabled": True, "text": "FAST"},
                            "mobile": {"enabled": True, "text": "SAVE"},
                        }
                    }
                }
            }
        )

        badges = normalized["pansou"]["result_provider_badges"]
        self.assertEqual(badges["quark"], {"enabled": False, "text": ""})
        self.assertEqual(badges["mobile"], {"enabled": False, "text": "秒入库"})

        merged = normalize_advanced_config_payload(
            _payload({"pansou": {"timeout": 45}}),
            current_stored={
                "pansou": {
                    "result_provider_badges": {
                        "quark": {"enabled": True, "text": "FAST"},
                    }
                }
            },
        )
        self.assertEqual(
            merged["pansou"]["result_provider_badges"]["quark"],
            {"enabled": False, "text": ""},
        )

    def test_provider_badge_update_persists_and_triggers_runtime_reload(self) -> None:
        db = _FakeDb({})
        reload_calls = 0

        def reload_runtime() -> dict[str, Any]:
            nonlocal reload_calls
            reload_calls += 1
            return {"reloaded": True}

        service = _service(db, raw_config={}, reload_runtime=reload_runtime)
        result, status = service.update_advanced(
            _payload(
                {
                    "pansou": {
                        "result_provider_badges": {
                            "mobile": {"enabled": True, "text": "秒入库"},
                        }
                    }
                }
            )
        )

        self.assertEqual(status, 200, result)
        self.assertEqual(reload_calls, 1)
        self.assertEqual(
            db.app_settings.get_all()["advanced_config"]["pansou"]["result_provider_badges"]["mobile"],
            {"enabled": True, "text": "秒入库"},
        )

    def test_merge_preserves_secret_and_clear_secret_removes_only_requested_value(self) -> None:
        current = {
            "pansou": {"password": "old-secret", "base_url": "http://old"},
            "tmdb": {"token": "tmdb-secret", "language": "zh-CN"},
        }
        merged = normalize_advanced_config_payload(
            _payload({"pansou": {"base_url": "http://new", "password": ""}}, clear_secrets=[]),
            current_stored=current,
        )
        self.assertEqual(merged["pansou"]["password"], "old-secret")
        self.assertEqual(merged["pansou"]["base_url"], "http://new")

        cleared = normalize_advanced_config_payload(
            _payload({}, clear_secrets=["pansou.password"]),
            current_stored=current,
        )
        self.assertNotIn("password", cleared["pansou"])
        self.assertEqual(cleared["tmdb"], current["tmdb"])

    def test_replace_rejects_masked_secret(self) -> None:
        with self.assertRaisesRegex(ValueError, "脱敏占位符"):
            normalize_advanced_config_payload(
                _payload({"tmdb": {"token": "***"}}, mode="replace"),
                current_stored={"tmdb": {"token": "old-secret"}},
            )

    def test_unknown_and_removed_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "pansou.unknown_option"):
            normalize_advanced_config_payload(
                _payload({"pansou": {"unknown_option": True}}, mode="replace")
            )
        for section in (
            {"quark": {"token": "legacy"}},
            {"openlist": {"base_url": "http://legacy"}},
            {"routes": {"quark": {"enabled": True}}},
            {"storage": {"targets": {"legacy": {"driver": "rclone"}}}},
            {"categories": {"movie": {"quark_save_path": "/legacy"}}},
        ):
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, "旧版字段"):
                normalize_advanced_config_payload(_payload(section, mode="replace"))

    def test_proxy_validation_is_applied_after_envelope_parsing(self) -> None:
        with self.assertRaisesRegex(ValueError, "1-65535"):
            normalize_advanced_config_payload(
                _payload(
                    {
                        "btbtla": {
                            "proxy_enabled": True,
                            "proxy_url": "socks5h://proxy.local:70000",
                        }
                    }
                )
            )

    def test_export_contains_versioned_stored_and_effective_config(self) -> None:
        stored = {
            "pansou": {"base_url": "http://pansou.local", "password": "pansou-secret"},
            "tmdb": {"language": "zh-CN", "token": "tmdb-secret"},
        }
        service = _service(
            _FakeDb({"advanced_config": stored}),
            raw_config={"pansou": stored["pansou"], "tmdb": stored["tmdb"]},
        )
        result, status = service.export_advanced()
        self.assertEqual(status, 200, result)
        document = result["document"]
        self.assertEqual(document["format"], ADVANCED_CONFIG_EXPORT_FORMAT)
        self.assertEqual(document["version"], ADVANCED_CONFIG_EXPORT_VERSION)
        self.assertEqual(document["stored"]["tmdb"]["token"], "tmdb-secret")
        self.assertTrue(document["source"]["sensitive"])

        normalized = normalize_advanced_config_payload(
            {"config": document, "mode": "replace", "scope": "stored"}
        )
        self.assertEqual(normalized, stored)

    def test_export_rejects_legacy_database_fields_after_cutover(self) -> None:
        service = _service(
            _FakeDb(
                {
                    "advanced_config": {
                        "quark": {"token": "legacy"},
                        "pansou": {"timeout": 20, "unknown_option": True},
                    }
                }
            ),
            raw_config={},
        )
        result, status = service.export_advanced()
        self.assertEqual(status, 409, result)
        self.assertEqual(result["code"], "ADVANCED_CONFIG_MIGRATION_REQUIRED")

    def test_v29_cleanup_helper_filters_legacy_database_fields(self) -> None:
        raw = {
            "quark": {"token": "legacy"},
            "openlist": {"base_url": "http://legacy", "timeout": 30},
            "pansou": {"base_url": "http://pansou", "unknown_option": True},
            "routes": {"quark": {"enabled": True}},
            "rclone": {
                "auto_interval_minutes": 15,
                "staging_retry_delay_seconds": 30,
            },
        }
        cleaned, removed = migrate_stored_advanced_config(raw)
        self.assertEqual(
            cleaned,
            {
                "openlist": {"timeout": 30},
                "pansou": {"base_url": "http://pansou"},
                "rclone": {"staging_retry_delay_seconds": 30},
            },
        )
        with self.assertRaisesRegex(ValueError, "旧版字段"):
            persisted_config_from_settings({"advanced_config": raw})
        self.assertIn("quark", removed)
        self.assertIn("openlist.base_url", removed)
        self.assertIn("pansou.unknown_option", removed)
        self.assertIn("routes", removed)
        self.assertIn("rclone.auto_interval_minutes", removed)
        with self.assertRaisesRegex(ValueError, "pansou.unknown_option"):
            normalize_advanced_config_payload(
                _payload({"pansou": {"unknown_option": True}}, mode="replace")
            )

    def test_current_database_value_is_strict_even_for_replace(self) -> None:
        with self.assertRaisesRegex(ValueError, "旧版字段"):
            normalize_advanced_config_payload(
                _payload({"pansou": {"timeout": 45}}, mode="replace"),
                current_stored={
                    "quark": {"token": "legacy"},
                    "routes": {"quark": {"enabled": True}},
                },
            )

    def test_update_service_requires_envelope_and_persists_merge(self) -> None:
        db = _FakeDb({"advanced_config": {"tmdb": {"language": "zh-CN"}}})
        service = _service(db, raw_config={"tmdb": {"language": "zh-CN"}})
        result, status = service.update_advanced(_payload({"pansou": {"timeout": 20}}))
        self.assertEqual(status, 200, result)
        self.assertEqual(
            db.app_settings.get_all()["advanced_config"],
            {"tmdb": {"language": "zh-CN"}, "pansou": {"timeout": 20}},
        )
        result, status = service.update_advanced({"config": {"pansou": {"timeout": 30}}})
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])

    def test_unified_settings_transaction_uses_envelope(self) -> None:
        with _WorkspaceDatabase() as database:
            service = _service(database, raw_config={})
            payload = {
                "settings": {
                    "public": {"allow_anonymous_search": False},
                    "submission": {"mode": "review"},
                },
                "search": {"providers": [{"key": "pansou", "enabled": True, "priority": 20}]},
                "advanced": _payload({"pansou": {"timeout": 20}}),
            }
            result, status = service.update_all(payload)
            self.assertEqual(status, 200, result)
            settings = database.app_settings.get_all()
            self.assertEqual(settings["advanced_config"], {"pansou": {"timeout": 20}})
            self.assertFalse(settings["public.allow_anonymous_search"])
            self.assertEqual(settings["submission.mode"], "review")

    def test_unified_settings_without_advanced_section_uses_empty_v1_document(self) -> None:
        with _WorkspaceDatabase() as database:
            service = _service(database, raw_config={})
            result, status = service.update_all(
                {
                    "settings": {"submission": {"mode": "auto"}},
                    "search": {"providers": [{"key": "pansou", "enabled": True, "priority": 10}]},
                }
            )
            self.assertEqual(status, 200, result)
            self.assertNotIn("advanced_config", database.app_settings.get_all())

    def test_reload_failure_rolls_back_v1_config(self) -> None:
        with _WorkspaceDatabase() as database:
            database.app_settings.set_many({"advanced_config": {"tmdb": {"language": "zh-CN"}}})
            calls = 0

            def reload_runtime() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("reload failed")
                return {"restored": True}

            service = _service(
                database,
                raw_config={},
                reload_runtime=reload_runtime,
            )
            result, status = service.update_advanced(
                _payload({"pansou": {"timeout": 20}}, mode="replace")
            )
            self.assertEqual(status, 500)
            self.assertTrue(result["rolled_back"])
            self.assertTrue(result["runtime_restored"])
            self.assertEqual(
                database.app_settings.get_all()["advanced_config"],
                {"tmdb": {"language": "zh-CN"}},
            )


if __name__ == "__main__":
    unittest.main()
