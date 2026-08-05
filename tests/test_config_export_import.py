from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from fnos_media_import.config_persistence import (
    ADVANCED_CONFIG_EXPORT_FORMAT,
    ADVANCED_CONFIG_EXPORT_VERSION,
    normalize_advanced_config_payload,
)
from fnos_media_import.database import Database
from fnos_media_import.services.settings_service import SettingsDependencies, SettingsService


class _FakeDb:
    def __init__(self, settings: dict[str, Any]) -> None:
        self._settings = settings

    def get_app_settings(self) -> dict[str, Any]:
        return self._settings

    def set_app_settings(self, updates: dict[str, Any]) -> None:
        self._settings.update(updates)


def _service(stored: dict[str, Any]) -> SettingsService:
    deps = SettingsDependencies(
        db=_FakeDb({"advanced_config": stored}),
        raw_config=lambda: {
            "quark": {"auto_save_url": "http://effective-quark.local", "token": "effective-token"},
            "tmdb": {"language": "zh-CN"},
        },
        redact_config=lambda config: config,
        advanced_response=lambda *_args, **_kwargs: {},
        normalize_advanced=lambda *_args, **_kwargs: {},
        advanced_key="advanced_config",
        reload_runtime=lambda: {},
        effective_settings=lambda: {},
        payload_bool=lambda payload, key, default: default,
    )
    return SettingsService(deps)


def _database_service(
    database: Database,
    *,
    reload_runtime=lambda: {},
    normalize=normalize_advanced_config_payload,
) -> SettingsService:
    return SettingsService(
        SettingsDependencies(
            db=database,
            raw_config=lambda: {},
            redact_config=lambda config: config,
            advanced_response=lambda *_args, **_kwargs: {},
            normalize_advanced=normalize,
            advanced_key="advanced_config",
            reload_runtime=reload_runtime,
            effective_settings=lambda: {},
            payload_bool=lambda payload, key, default: default,
        )
    )


def _unified_database_service(database: Database, *, reload_runtime=lambda: {}) -> SettingsService:
    def payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
        return bool(payload.get(key, default))

    return SettingsService(
        SettingsDependencies(
            db=database,
            raw_config=lambda: database.get_app_settings().get("advanced_config") or {},
            redact_config=lambda config: config,
            advanced_response=lambda _raw, settings: {
                "config": settings.get("advanced_config") or {},
                "stored": settings.get("advanced_config") or {},
                "meta": {},
            },
            normalize_advanced=normalize_advanced_config_payload,
            advanced_key="advanced_config",
            reload_runtime=reload_runtime,
            effective_settings=lambda: {
                "public": {
                    "allow_anonymous_search": database.get_app_settings().get("public.allow_anonymous_search"),
                },
                "submission": {"mode": database.get_app_settings().get("submission.mode")},
            },
            payload_bool=payload_bool,
            search_providers=lambda: [{"key": "pansou", "name": "PanSou"}],
        )
    )


class ConfigExportImportTests(unittest.TestCase):
    @staticmethod
    def _all_settings_payload() -> dict[str, Any]:
        return {
            "settings": {
                "public": {
                    "allow_anonymous_search": False,
                    "request_query_enabled": True,
                    "hide_full_links": True,
                },
                "submission": {"mode": "review"},
            },
            "search": {"providers": [{"key": "pansou", "enabled": True, "priority": 20}]},
            "advanced": {"config": {"quark": {"auto_save_url": "http://quark.local"}}},
        }

    def test_unified_save_commits_all_sections_in_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "settings.db")
            database.init_schema()
            service = _unified_database_service(database)

            result, status = service.update_all(self._all_settings_payload())

            self.assertEqual(status, 200, result)
            stored = database.get_app_settings()
            self.assertFalse(stored["public.allow_anonymous_search"])
            self.assertEqual(stored["submission.mode"], "review")
            self.assertEqual(stored["search.providers"]["pansou"]["priority"], 20)
            self.assertEqual(stored["advanced_config"]["quark"]["auto_save_url"], "http://quark.local")

    def test_unified_save_validation_failure_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "settings.db")
            database.init_schema()
            database.set_app_settings({"submission.mode": "auto"})
            service = _unified_database_service(database)
            payload = self._all_settings_payload()
            payload["settings"]["submission"]["mode"] = "invalid"

            result, status = service.update_all(payload)

            self.assertEqual(status, 400, result)
            self.assertEqual(database.get_app_settings(), {"submission.mode": "auto"})

    def test_unified_save_reload_failure_rolls_back_every_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "settings.db")
            database.init_schema()
            before = {
                "public.allow_anonymous_search": True,
                "submission.mode": "auto",
                "search.providers": {"pansou": {"enabled": False, "priority": 90}},
                "advanced_config": {"quark": {"auto_save_url": "http://old.local"}},
            }
            database.set_app_settings(before)
            reload_calls = 0

            def reload_runtime() -> dict[str, Any]:
                nonlocal reload_calls
                reload_calls += 1
                if reload_calls == 1:
                    raise RuntimeError("reload failed")
                return {"restored": True}

            service = _unified_database_service(database, reload_runtime=reload_runtime)
            with self.assertLogs("fnos_media_import.services.settings_service", level="ERROR"):
                result, status = service.update_all(self._all_settings_payload())

            self.assertEqual(status, 500)
            self.assertTrue(result["rolled_back"])
            self.assertTrue(result["runtime_restored"])
            self.assertEqual(database.get_app_settings(), before)

    def test_sixpan_paths_are_normalized_to_one_persisted_source(self) -> None:
        normalized = normalize_advanced_config_payload(
            {
                "config": {
                    "sixpan": {"openlist_mount_name": "/清云/"},
                    "categories": {
                        "tv": {
                            "label": "电视剧",
                            "sixpan_save_path": "/电视剧",
                            "sixpan_fnos_target_path": "/旧挂载/电视剧",
                        }
                    },
                }
            },
            current_stored={},
        )

        self.assertEqual(normalized["sixpan"]["fnos_mount_name"], "清云")
        self.assertNotIn("openlist_mount_name", normalized["sixpan"])
        self.assertNotIn("sixpan_fnos_target_path", normalized["categories"]["tv"])

    def test_export_returns_versioned_stored_and_effective_config(self) -> None:
        stored = {
            "quark": {"auto_save_url": "http://quark.local", "token": "super-secret-token"},
            "pansou": {"base_url": "http://pansou.local"},
        }
        service = _service(stored)
        result, status = service.export_advanced()
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        document = result["document"]
        self.assertEqual(document["format"], ADVANCED_CONFIG_EXPORT_FORMAT)
        self.assertEqual(document["version"], ADVANCED_CONFIG_EXPORT_VERSION)
        self.assertTrue(document["exported_at"])
        self.assertEqual(document["stored"]["quark"]["token"], "super-secret-token")
        self.assertEqual(document["effective"]["quark"]["token"], "effective-token")
        self.assertTrue(document["source"]["sensitive"])

    def test_export_returns_empty_config_when_nothing_stored(self) -> None:
        service = _service({})
        result, status = service.export_advanced()
        self.assertEqual(status, 200)
        self.assertEqual(result["document"]["stored"], {})

    def test_export_then_import_round_trip_keeps_secrets(self) -> None:
        stored = {
            "quark": {"auto_save_url": "http://quark.local", "token": "real-secret-token"},
            "pansou": {"base_url": "http://pansou.local", "username": "admin"},
        }
        service = _service(stored)
        exported, status = service.export_advanced()
        self.assertEqual(status, 200)
        normalized = normalize_advanced_config_payload(
            {"config": exported["document"], "mode": "replace", "scope": "stored"},
            current_stored={},
        )
        self.assertEqual(normalized.get("quark", {}).get("token"), "real-secret-token")
        self.assertEqual(normalized.get("quark", {}).get("auto_save_url"), "http://quark.local")
        self.assertEqual(normalized.get("pansou", {}).get("base_url"), "http://pansou.local")

    def test_merge_preserves_omitted_fields_and_blank_secrets(self) -> None:
        current = {
            "quark": {"auto_save_url": "http://old.local", "token": "old-secret"},
            "tmdb": {"language": "zh-CN"},
        }
        normalized = normalize_advanced_config_payload(
            {"config": {"quark": {"auto_save_url": "http://new.local", "token": ""}}, "mode": "merge"},
            current_stored=current,
        )
        self.assertEqual(normalized["quark"]["auto_save_url"], "http://new.local")
        self.assertEqual(normalized["quark"]["token"], "old-secret")
        self.assertEqual(normalized["tmdb"]["language"], "zh-CN")

    def test_merge_clear_secret_removes_only_requested_stored_secret(self) -> None:
        current = {
            "quark": {"auto_save_url": "http://quark.local", "token": "old-secret"},
            "tmdb": {"token": "tmdb-secret", "language": "zh-CN"},
        }
        normalized = normalize_advanced_config_payload(
            {"config": {}, "clear_secrets": ["quark.token"]},
            current_stored=current,
        )

        self.assertEqual(normalized["quark"], {"auto_save_url": "http://quark.local"})
        self.assertEqual(normalized["tmdb"], current["tmdb"])

    def test_clear_secret_rejects_non_secret_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能清除非密钥字段"):
            normalize_advanced_config_payload(
                {"config": {}, "clear_secrets": ["quark.auto_save_url"]},
                current_stored={"quark": {"auto_save_url": "http://quark.local"}},
            )

    def test_merge_patch_preserves_unrelated_nested_fields(self) -> None:
        current = {
            "pansou": {
                "base_url": "http://pansou.local",
                "timeout": 20,
                "conc": 10,
            },
            "organizer": {"enabled": True, "stable_window_seconds": 120},
        }
        normalized = normalize_advanced_config_payload(
            {"config": {"pansou": {"timeout": 30}}},
            current_stored=current,
        )

        self.assertEqual(
            normalized,
            {
                "pansou": {
                    "base_url": "http://pansou.local",
                    "timeout": 30,
                    "conc": 10,
                },
                "organizer": {"enabled": True, "stable_window_seconds": 120},
            },
        )

    def test_openlist_bulk_operation_settings_can_be_persisted(self) -> None:
        normalized = normalize_advanced_config_payload(
            {
                "config": {
                    "openlist": {"batch_timeout": 600},
                    "organizer": {
                        "bulk_operations_enabled": True,
                        "regex_rename_min_items": 10,
                        "bulk_reconcile_timeout_seconds": 120,
                    },
                },
                "mode": "replace",
            },
            current_stored={},
        )

        self.assertEqual(normalized["openlist"]["batch_timeout"], 600)
        self.assertEqual(
            normalized["organizer"],
            {
                "bulk_operations_enabled": True,
                "regex_rename_min_items": 10,
                "bulk_reconcile_timeout_seconds": 120,
            },
        )

    def test_replace_clears_omitted_fields_and_blank_secrets(self) -> None:
        current = {
            "quark": {"auto_save_url": "http://old.local", "token": "old-secret"},
            "tmdb": {"token": "old-tmdb-token", "language": "zh-CN"},
        }
        normalized = normalize_advanced_config_payload(
            {"config": {"quark": {"auto_save_url": "http://new.local", "token": ""}}, "mode": "replace"},
            current_stored=current,
        )
        self.assertEqual(normalized, {"quark": {"auto_save_url": "http://new.local"}})

    def test_replace_rejects_masked_secret(self) -> None:
        with self.assertRaisesRegex(ValueError, "脱敏占位符"):
            normalize_advanced_config_payload(
                {"config": {"quark": {"token": "***"}}, "mode": "replace"},
                current_stored={"quark": {"token": "old-secret"}},
            )

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "pansou.unknown_option"):
            normalize_advanced_config_payload(
                {"config": {"pansou": {"unknown_option": True}}, "mode": "replace"},
                current_stored={},
            )

    def test_unknown_export_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "版本 999 不受支持"):
            normalize_advanced_config_payload(
                {
                    "config": {
                        "format": ADVANCED_CONFIG_EXPORT_FORMAT,
                        "version": 999,
                        "stored": {},
                        "effective": {},
                    },
                    "mode": "replace",
                },
                current_stored={},
            )

    def test_reload_failure_rolls_database_back_and_restores_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "settings.db")
            database.init_schema()
            previous = {"quark": {"auto_save_url": "http://old.local", "token": "old-secret"}}
            database.set_app_settings({"advanced_config": previous})
            reload_calls = 0

            def reload_runtime() -> dict[str, Any]:
                nonlocal reload_calls
                reload_calls += 1
                if reload_calls == 1:
                    raise RuntimeError("candidate runtime failed")
                return {"restored": True}

            service = _database_service(database, reload_runtime=reload_runtime)
            with self.assertLogs("fnos_media_import.services.settings_service", level="ERROR") as captured:
                result, status = service.update_advanced(
                    {
                        "config": {"quark": {"auto_save_url": "http://bad.local"}},
                        "mode": "replace",
                    }
                )

            self.assertEqual(status, 500)
            self.assertTrue(result["rolled_back"])
            self.assertTrue(result["runtime_restored"])
            self.assertEqual(reload_calls, 2)
            self.assertEqual(database.get_app_settings()["advanced_config"], previous)
            self.assertTrue(any("runtime reload failed" in item for item in captured.output))

    def test_concurrent_merges_do_not_drop_each_others_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "settings.db")
            database.init_schema()
            database.set_app_settings(
                {"advanced_config": {"pansou": {"base_url": "http://pansou.local"}}}
            )
            start = threading.Barrier(3)
            results: list[tuple[dict[str, Any], int]] = []

            def slow_normalize(payload: dict[str, Any], *, current_stored: dict[str, Any]) -> dict[str, Any]:
                time.sleep(0.05)
                return normalize_advanced_config_payload(payload, current_stored=current_stored)

            services = [
                _database_service(database, normalize=slow_normalize),
                _database_service(database, normalize=slow_normalize),
            ]
            payloads = [
                {"config": {"quark": {"auto_save_url": "http://quark.local"}}, "mode": "merge"},
                {"config": {"tmdb": {"language": "zh-CN"}}, "mode": "merge"},
            ]

            def save(service: SettingsService, payload: dict[str, Any]) -> None:
                start.wait()
                results.append(service.update_advanced(payload))

            threads = [
                threading.Thread(target=save, args=(service, payload))
                for service, payload in zip(services, payloads)
            ]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sorted(status for _result, status in results), [200, 200])
            stored = database.get_app_settings()["advanced_config"]
            self.assertEqual(stored["pansou"]["base_url"], "http://pansou.local")
            self.assertEqual(stored["quark"]["auto_save_url"], "http://quark.local")
            self.assertEqual(stored["tmdb"]["language"], "zh-CN")

    def test_failed_reload_does_not_rollback_a_newer_successful_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "settings.db")
            database.init_schema()
            database.set_app_settings(
                {"advanced_config": {"pansou": {"base_url": "http://old.local"}}}
            )
            first_reload_started = threading.Event()
            allow_first_reload_to_fail = threading.Event()
            first_reload_calls = 0
            first_result: list[tuple[dict[str, Any], int]] = []

            def first_reload() -> dict[str, Any]:
                nonlocal first_reload_calls
                first_reload_calls += 1
                if first_reload_calls == 1:
                    first_reload_started.set()
                    self.assertTrue(allow_first_reload_to_fail.wait(timeout=5))
                    raise RuntimeError("first runtime failed")
                return {"restored": True}

            failing_service = _database_service(database, reload_runtime=first_reload)
            succeeding_service = _database_service(database, reload_runtime=lambda: {"ok": True})

            thread = threading.Thread(
                target=lambda: first_result.append(
                    failing_service.update_advanced(
                        {
                            "config": {"quark": {"auto_save_url": "http://bad.local"}},
                            "mode": "replace",
                        }
                    )
                )
            )
            with self.assertLogs("fnos_media_import.services.settings_service", level="ERROR") as captured:
                thread.start()
                self.assertTrue(first_reload_started.wait(timeout=5))
                second_result, second_status = succeeding_service.update_advanced(
                    {
                        "config": {"tmdb": {"language": "en-US"}},
                        "mode": "replace",
                    }
                )
                self.assertEqual(second_status, 200, second_result)
                allow_first_reload_to_fail.set()
                thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(first_result), 1)
            failed_body, failed_status = first_result[0]
            self.assertEqual(failed_status, 500)
            self.assertTrue(failed_body["superseded"])
            self.assertFalse(failed_body["rolled_back"])
            self.assertTrue(failed_body["runtime_restored"])
            self.assertEqual(
                database.get_app_settings()["advanced_config"],
                {"tmdb": {"language": "en-US"}},
            )
            self.assertTrue(any("runtime reload failed" in item for item in captured.output))

    def test_reload_failure_rolls_back_config_but_preserves_refreshed_sixpan_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "settings.db")
            database.init_schema()
            previous = {
                "pansou": {"base_url": "http://old.local"},
                "sixpan": {
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                },
            }
            database.set_app_settings({"advanced_config": previous})
            reload_calls = 0

            def reload_runtime() -> dict[str, Any]:
                nonlocal reload_calls
                reload_calls += 1
                if reload_calls == 1:

                    def refresh_token(current: Any, _existed: bool) -> dict[str, Any]:
                        stored = copy.deepcopy(current) if isinstance(current, dict) else {}
                        sixpan = stored.setdefault("sixpan", {})
                        sixpan["access_token"] = "refreshed-access"
                        return stored

                    database.update_app_setting_atomic("advanced_config", refresh_token)
                    raise RuntimeError("reload failed after token refresh")
                return {"restored": True}

            service = _database_service(database, reload_runtime=reload_runtime)
            with self.assertLogs("fnos_media_import.services.settings_service", level="ERROR"):
                result, status = service.update_advanced(
                    {
                        "config": {
                            "pansou": {"base_url": "http://bad.local"},
                            # This value belongs to the failed payload and must
                            # not be mistaken for a concurrent token refresh.
                            "sixpan": {"refresh_token": "bad-refresh"},
                        },
                        "mode": "replace",
                    }
                )

            self.assertEqual(status, 500)
            self.assertTrue(result["rolled_back"])
            self.assertFalse(result["superseded"])
            self.assertTrue(result["runtime_restored"])
            self.assertEqual(reload_calls, 2)
            self.assertEqual(
                database.get_app_settings()["advanced_config"],
                {
                    "pansou": {"base_url": "http://old.local"},
                    "sixpan": {
                        "access_token": "refreshed-access",
                        "refresh_token": "old-refresh",
                    },
                },
            )


if __name__ == "__main__":
    unittest.main()
