from __future__ import annotations

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
    migrate_persisted_advanced_config_setting,
    migrate_stored_advanced_config,
    normalize_advanced_config_payload,
    persisted_config_from_settings,
)
from fnos_media_import.database import Database
from fnos_media_import.services.settings_service import SettingsDependencies, SettingsService


WORK_TEST_ROOT = Path.cwd() / "codex_tmp" / "config-export-tests"


class _FakeDb:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = copy.deepcopy(settings or {})

    def get_app_settings(self) -> dict[str, Any]:
        return copy.deepcopy(self._settings)

    def set_app_settings(self, updates: dict[str, Any]) -> None:
        self._settings.update(copy.deepcopy(updates))


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
    def __enter__(self) -> Database:
        WORK_TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = WORK_TEST_ROOT / f"settings-{uuid.uuid4().hex}.db"
        self.database = Database(self.path)
        self.database.init_schema()
        return self.database

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

    def test_export_filters_legacy_database_fields(self) -> None:
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
        self.assertEqual(status, 200, result)
        self.assertEqual(result["document"]["stored"], {"pansou": {"timeout": 20}})

    def test_database_legacy_fields_are_migrated_but_new_imports_stay_strict(self) -> None:
        raw = {
            "quark": {"token": "legacy"},
            "openlist": {"base_url": "http://legacy", "timeout": 30},
            "pansou": {"base_url": "http://pansou", "unknown_option": True},
            "routes": {"quark": {"enabled": True}},
        }
        cleaned, removed = migrate_stored_advanced_config(raw)
        self.assertEqual(
            cleaned,
            {
                "openlist": {"timeout": 30},
                "pansou": {"base_url": "http://pansou"},
            },
        )
        self.assertEqual(persisted_config_from_settings({"advanced_config": raw}), cleaned)
        self.assertIn("quark", removed)
        self.assertIn("openlist.base_url", removed)
        self.assertIn("pansou.unknown_option", removed)
        self.assertIn("routes", removed)
        with self.assertRaisesRegex(ValueError, "pansou.unknown_option"):
            normalize_advanced_config_payload(
                _payload({"pansou": {"unknown_option": True}}, mode="replace")
            )

    def test_replace_can_overwrite_a_legacy_current_database_value(self) -> None:
        replaced = normalize_advanced_config_payload(
            _payload({"pansou": {"timeout": 45}}, mode="replace"),
            current_stored={
                "quark": {"token": "legacy"},
                "routes": {"quark": {"enabled": True}},
            },
        )
        self.assertEqual(replaced, {"pansou": {"timeout": 45}})

    def test_startup_migration_cas_persists_cleaned_database_value(self) -> None:
        with _WorkspaceDatabase() as database:
            database.set_app_settings(
                {
                    "advanced_config": {
                        "quark": {"token": "legacy"},
                        "openlist": {"base_url": "http://legacy", "timeout": 30},
                        "pansou": {"base_url": "http://pansou", "unknown_option": True},
                    }
                }
            )
            settings, removed = migrate_persisted_advanced_config_setting(database)

            expected = {
                "openlist": {"timeout": 30},
                "pansou": {"base_url": "http://pansou"},
            }
            self.assertEqual(settings["advanced_config"], expected)
            self.assertEqual(database.get_app_settings()["advanced_config"], expected)
            self.assertIn("quark", removed)
            self.assertIn("openlist.base_url", removed)

    def test_update_service_requires_envelope_and_persists_merge(self) -> None:
        db = _FakeDb({"advanced_config": {"tmdb": {"language": "zh-CN"}}})
        service = _service(db, raw_config={"tmdb": {"language": "zh-CN"}})
        result, status = service.update_advanced(_payload({"pansou": {"timeout": 20}}))
        self.assertEqual(status, 200, result)
        self.assertEqual(
            db.get_app_settings()["advanced_config"],
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
            settings = database.get_app_settings()
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
            self.assertNotIn("advanced_config", database.get_app_settings())

    def test_reload_failure_rolls_back_v1_config(self) -> None:
        with _WorkspaceDatabase() as database:
            database.set_app_settings({"advanced_config": {"tmdb": {"language": "zh-CN"}}})
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
                database.get_app_settings()["advanced_config"],
                {"tmdb": {"language": "zh-CN"}},
            )


if __name__ == "__main__":
    unittest.main()
