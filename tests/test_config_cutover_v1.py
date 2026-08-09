from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path

from fnos_media_import.config import _apply_env_overrides, _default_config, _reject_removed_config_fields, load_config
from fnos_media_import.config_persistence import (
    ADVANCED_CONFIG_KEY,
    ADVANCED_CONFIG_EXPORT_FORMAT,
    ADVANCED_CONFIG_EXPORT_VERSION,
    normalize_advanced_config_payload,
    persisted_config_from_settings,
)
from fnos_media_import.storage_paths import LegacyStoragePathError, normalize_sixpan_path_sources


def _document(stored: dict) -> dict:
    return {
        "format": ADVANCED_CONFIG_EXPORT_FORMAT,
        "version": ADVANCED_CONFIG_EXPORT_VERSION,
        "stored": stored,
    }


class ConfigCutoverTests(unittest.TestCase):
    def test_default_config_has_no_removed_global_credentials_or_mobile_target(self) -> None:
        # load_config() intentionally imports .env into the process environment
        # during real startup.  This unit test only verifies the config shape;
        # loading the developer root key here would leak it into later tests.
        with patch("fnos_media_import.config._load_dotenv"):
            raw = load_config().raw
        for section in ("quark", "cloud189", "cloud139", "cmcc_upload"):
            self.assertNotIn(section, raw)
        self.assertNotIn("secret_key", raw.get("app", {}))
        self.assertNotIn("base_url", raw["openlist"])
        self.assertNotIn("token", raw["openlist"])
        self.assertNotIn("targets", raw["storage"])
        self.assertNotIn("default_target", raw["storage"])
        self.assertNotIn("client_id", raw["sixpan"])
        self.assertNotIn("access_token", raw["sixpan"])
        self.assertNotIn("routes", raw)

    def test_config_file_routes_are_rejected_instead_of_loaded(self) -> None:
        with self.assertRaisesRegex(ValueError, "旧版字段"):
            _reject_removed_config_fields({"routes": {"quark": {"enabled": True}}})

    def test_config_file_app_session_secret_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, r"app\.secret_key"):
            _reject_removed_config_fields({"app": {"secret_key": "legacy"}})

    def test_removed_credential_environment_aliases_fail_closed(self) -> None:
        with patch.dict("os.environ", {"QUARK_AUTO_SAVE_TOKEN": "legacy"}, clear=False):
            with self.assertRaisesRegex(ValueError, "旧版配置环境变量"):
                _apply_env_overrides(_default_config())

    def test_rejected_cmcc_upload_aliases_are_not_read_by_live_code(self) -> None:
        uploader = Path("fnos_media_import/cmcc/uploader.py").read_text(encoding="utf-8")
        cli = Path("fnos_media_import/cmcc/cli.py").read_text(encoding="utf-8")
        self.assertNotIn("CMCC_UPLOAD_PUT_TIMEOUT", uploader)
        self.assertNotIn("CMCC_UPLOAD_MODE", cli)
        self.assertNotIn("CMCC_UPLOAD_RENAME_MODE", cli)

    def test_v1_composition_has_no_dead_master_key_probe_call(self) -> None:
        source = Path("fnos_media_import/app.py").read_text(encoding="utf-8")
        self.assertNotIn("\n    _v1_master_key()\n", source)

        with patch.dict("os.environ", {"APP_SECRET_KEY": "legacy"}, clear=False):
            with self.assertRaisesRegex(ValueError, "旧版配置环境变量"):
                _apply_env_overrides(_default_config())

    def test_removed_root_and_notification_key_aliases_fail_closed(self) -> None:
        for name in ("FNOS_ACCOUNT_MASTER_KEY", "NOTIFICATION_ENCRYPTION_KEY"):
            with self.subTest(name=name), patch.dict("os.environ", {name: "legacy"}, clear=False):
                with self.assertRaisesRegex(ValueError, "旧版配置环境变量"):
                    _apply_env_overrides(_default_config())

    def test_bare_advanced_patch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "format.*version"):
            normalize_advanced_config_payload({"config": {"pansou": {"timeout": 10}}})

    def test_ambiguous_or_malformed_envelope_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "同时包含"):
            normalize_advanced_config_payload(
                {
                    "format": ADVANCED_CONFIG_EXPORT_FORMAT,
                    "version": ADVANCED_CONFIG_EXPORT_VERSION,
                    "config": _document({}),
                    "stored": {},
                }
            )
        with self.assertRaisesRegex(ValueError, "stored 必须是对象"):
            normalize_advanced_config_payload(
                {
                    "config": {
                        "format": ADVANCED_CONFIG_EXPORT_FORMAT,
                        "version": ADVANCED_CONFIG_EXPORT_VERSION,
                        "stored": [],
                    }
                }
            )

    def test_database_does_not_accept_export_envelope_as_stored_config(self) -> None:
        with self.assertRaisesRegex(ValueError, "数据库高级配置不是 V1"):
            persisted_config_from_settings({ADVANCED_CONFIG_KEY: _document({})})

    def test_settings_rollback_has_no_global_sixpan_token_exception(self) -> None:
        source = Path("fnos_media_import/services/settings_service.py").read_text(encoding="utf-8")
        self.assertNotIn("_RUNTIME_MANAGED_SIXPAN_TOKEN_FIELDS", source)
        self.assertNotIn("rollback_preserving_tokens", source)

    def test_versioned_document_supports_merge_and_replace(self) -> None:
        current = {"pansou": {"base_url": "old", "src": "all"}}
        payload = {"config": _document({"pansou": {"base_url": "new"}}), "mode": "merge"}
        merged = normalize_advanced_config_payload(payload, current_stored=current)
        self.assertEqual(merged["pansou"], {"base_url": "new", "src": "all"})

        payload["mode"] = "replace"
        replaced = normalize_advanced_config_payload(payload, current_stored=current)
        self.assertEqual(replaced, {"pansou": {"base_url": "new"}})

    def test_removed_fields_are_rejected(self) -> None:
        for stored in (
            {"quark": {"auto_save_url": "http://legacy"}},
            {"cloud189": {"endpoint": "http://legacy"}},
            {"cmcc_upload": {"access_token": "legacy"}},
            {"openlist": {"base_url": "http://legacy"}},
            {"sixpan": {"client_id": "legacy"}},
            {"categories": {"movie": {"mobile_target_path": "legacy"}}},
            {"categories": {"movie": {"quark_save_path": "/legacy"}}},
            {"categories": {"movie": {"sixpan_save_path": "/legacy"}}},
        ):
            with self.subTest(stored=stored), self.assertRaisesRegex(ValueError, "旧版字段"):
                normalize_advanced_config_payload({"config": _document(stored)})

    def test_storage_target_rejects_legacy_identity_and_connection_fields(self) -> None:
        removed_fields = {
            "host": "https://legacy.example",
            "auth_mode": "web_basic",
            "access_token": "legacy-token",
            "phone": "13800000000",
            "upload_mode": "rapid_first",
            "rename_mode": "auto_rename",
            "openlist_mount": "legacy-mount",
            "namespace_id": "legacy:main",
            "organizer_namespace_id": "openlist:main",
            "organizer_root": "/legacy",
        }
        for field, value in removed_fields.items():
            stored = {
                "storage": {
                    "plan_version": 1,
                    "targets": {
                        "archive": {
                            "name": "归档",
                            "driver": "rclone",
                            "remote": "archive",
                            field: value,
                        }
                    },
                }
            }
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError,
                r"storage\.targets",
            ):
                normalize_advanced_config_payload(
                    {"config": _document(stored), "mode": "replace"}
                )

        category_stored = {
            "storage": {
                "plan_version": 1,
                "targets": {
                    "archive": {
                        "name": "归档",
                        "driver": "rclone",
                        "remote": "archive",
                        "categories": {
                            "movie": {
                                "final_root": "电影",
                                "organizer_namespace_id": "openlist:main",
                            }
                        },
                    }
                },
            }
        }
        with self.assertRaisesRegex(ValueError, r"storage\.targets"):
            normalize_advanced_config_payload(
                {"config": _document(category_stored), "mode": "replace"}
            )

    def test_storage_targets_and_default_are_database_resources(self) -> None:
        invalid = _document(
            {
                "storage": {
                    "enabled": True,
                    "plan_version": 1,
                    "default_target": "archive",
                    "targets": {
                        "archive": {
                            "name": "归档",
                            "driver": "rclone",
                            "remote": "archive_remote",
                            "categories": {"movie": {"final_root": "影视/电影"}},
                        }
                    },
                }
            }
        )
        with self.assertRaisesRegex(ValueError, r"storage\.(targets|default_target)"):
            normalize_advanced_config_payload({"config": invalid, "mode": "replace"})

        normalized = normalize_advanced_config_payload(
            {
                "config": _document({"storage": {"enabled": True, "plan_version": 1}}),
                "mode": "replace",
            }
        )
        self.assertEqual(normalized, {"storage": {"enabled": True, "plan_version": 1}})

    def test_routes_and_category_labels_are_not_advanced_config(self) -> None:
        for section in (
            {"routes": {"quark": {"enabled": True}}},
            {"categories": {"movie": {"label": "电影"}}},
        ):
            with self.subTest(section=section), self.assertRaises(ValueError):
                normalize_advanced_config_payload(
                    {"config": _document(section), "mode": "replace"}
                )

    def test_sixpan_path_aliases_are_not_derived(self) -> None:
        with self.assertRaises(LegacyStoragePathError):
            normalize_sixpan_path_sources(
                {"sixpan": {"fnos_mount_name": "legacy"}},
                materialize_category_paths=True,
            )


if __name__ == "__main__":
    unittest.main()
