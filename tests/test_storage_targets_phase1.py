"""阶段 1：v3 存储骨架的契约测试（零行为变化的兼容层）。"""

from __future__ import annotations

import unittest
from typing import Any

from fnos_media_import.config import (
    CATEGORY_KEYS,
    _ensure_storage_config,
    load_config,
)
from fnos_media_import.config_persistence import (
    normalize_advanced_config_payload,
    redact_advanced_config,
    sanitize_advanced_config,
)
from fnos_media_import.storage.registry import (
    UnknownStorageDriver,
    get_driver,
    registered_driver_kinds,
)
from fnos_media_import.storage.types import PathRef, StorageTarget, TargetCategoryConfig
from fnos_media_import.storage_paths import (
    cmcc_upload_root,
    openlist_root_for_upload,
)


def _target(destination_id: str, driver: str, **config: Any) -> StorageTarget:
    return StorageTarget(
        destination_id=destination_id,
        name=destination_id,
        driver=driver,
        enabled=True,
        guest_selectable=True,
        config=config,
        categories={
            "movie": TargetCategoryConfig(
                final_root=str(config.get("_final_root") or "电影"),
                organizer_namespace_id=str(config.get("_organizer_ns") or "openlist:main"),
                organizer_root=str(config.get("_organizer_root") or "/移动云盘A/电影"),
            )
        },
    )


class StorageRegistryTests(unittest.TestCase):
    def test_known_drivers_are_registered(self) -> None:
        self.assertEqual(sorted(registered_driver_kinds()), ["cmcc_api", "rclone"])

    def test_get_driver_returns_registered_driver(self) -> None:
        self.assertEqual(get_driver("cmcc_api").kind, "cmcc_api")
        self.assertEqual(get_driver("RCLONE").kind, "rclone")

    def test_unknown_driver_raises_and_never_falls_back(self) -> None:
        with self.assertRaises(UnknownStorageDriver):
            get_driver("nope")


class CmccApiDriverTests(unittest.TestCase):
    def test_validate_requires_host_and_token_for_web_basic(self) -> None:
        driver = get_driver("cmcc_api")
        self.assertTrue(driver.validate(_target("m", "cmcc_api")))
        ok = _target("m", "cmcc_api", host="https://example.com", access_token="t")
        self.assertEqual(driver.validate(ok), [])

    def test_capabilities_are_relay_upload(self) -> None:
        caps = get_driver("cmcc_api").capabilities(_target("m", "cmcc_api"))
        self.assertTrue(caps.supports_relay_upload)
        self.assertFalse(caps.supports_rclone_dest)
        self.assertFalse(caps.supports_native_save)

        native_caps = get_driver("cmcc_api").capabilities(
            _target("m", "cmcc_api", native_ingress="cloud139_direct")
        )
        self.assertTrue(native_caps.supports_native_save)

    def test_resolve_paths_builds_staging_under_final_parent(self) -> None:
        plan = get_driver("cmcc_api").resolve_paths(
            target=_target("mobile_main", "cmcc_api", namespace_id="cmcc:primary"),
            category_key="movie",
            job_id=42,
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        self.assertEqual(plan.final.path, "电影")
        self.assertEqual(plan.final.namespace_id, "cmcc:primary")
        self.assertEqual(plan.staging.path, "_入库暂存/电影/job-42")
        self.assertIsNotNone(plan.organizer)
        self.assertEqual(plan.organizer.path, "/移动云盘A/_入库暂存/电影/job-42")  # type: ignore[union-attr]

    def test_worker_spec_builds_relay_contract(self) -> None:
        driver = get_driver("cmcc_api")
        target = _target("mobile_main", "cmcc_api", namespace_id="cmcc:primary")
        paths = driver.resolve_paths(
            target=target,
            category_key="movie",
            job_id=42,
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        spec = driver.worker_spec(
            target=target,
            paths=paths,
            strategy="relay_upload",
            source=PathRef("rclone:MP", "离线下载/电影/job-42"),
            delete_source_after_ack=False,
        )
        self.assertEqual(spec.mode, "relay_upload")
        self.assertEqual(spec.uploader, "cmcc_api")
        self.assertFalse(spec.delete_source_after_ack)


class RcloneDriverTests(unittest.TestCase):
    def test_validate_requires_remote_or_local_root(self) -> None:
        driver = get_driver("rclone")
        self.assertTrue(driver.validate(_target("q", "rclone")))
        self.assertEqual(driver.validate(_target("q", "rclone", remote="quark_dst")), [])
        self.assertEqual(driver.validate(_target("q", "rclone", root="/storage-targets/local")), [])
        self.assertTrue(driver.validate(_target("q", "rclone", root="relative/path")))

    def test_capabilities_are_rclone_dest(self) -> None:
        caps = get_driver("rclone").capabilities(_target("q", "rclone", remote="quark_dst"))
        self.assertTrue(caps.supports_rclone_dest)
        self.assertFalse(caps.supports_relay_upload)

    def test_resolve_paths_remote_namespace(self) -> None:
        plan = get_driver("rclone").resolve_paths(
            target=_target("quark_archive", "rclone", remote="quark_dst"),
            category_key="movie",
            job_id=7,
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        self.assertEqual(plan.namespace_id, "rclone:quark_dst")
        self.assertEqual(plan.final.path, "电影")
        self.assertEqual(plan.staging.path, "_入库暂存/电影/job-7")

    def test_resolve_paths_local_root_prefixes_absolute_path(self) -> None:
        plan = get_driver("rclone").resolve_paths(
            target=_target("local_library", "rclone", root="/storage-targets/local-library"),
            category_key="movie",
            job_id=7,
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        self.assertEqual(plan.final.path, "/storage-targets/local-library/电影")
        self.assertEqual(plan.staging.path, "/storage-targets/local-library/_入库暂存/电影/job-7")

    def test_worker_spec_builds_copy_contract(self) -> None:
        driver = get_driver("rclone")
        target = _target("quark_archive", "rclone", remote="quark_dst")
        paths = driver.resolve_paths(
            target=target,
            category_key="movie",
            job_id=7,
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        spec = driver.worker_spec(
            target=target,
            paths=paths,
            strategy="rclone_copy",
            source=PathRef("rclone:MP", "离线下载/电影/job-7"),
        )
        self.assertEqual(spec.mode, "rclone_copy")
        self.assertEqual(spec.destination.namespace_id, "rclone:quark_dst")
        self.assertIsNone(spec.uploader)


class MobileMainMaterializationTests(unittest.TestCase):
    def test_materializes_mobile_main_from_legacy_config(self) -> None:
        raw = {
            "cmcc_upload": {
                "host": "https://example.com/hcy",
                "auth_mode": "web_basic",
                "access_token": "tok",
                "phone": "138",
                "mode": "rapid_first",
                "rename_mode": "auto_rename",
            },
            "cloud139": {
                "target_root_path": "根目录",
                "fnos_mount_name": "移动云盘A",
            },
            "categories": {
                key: {"label": f"{key}", "cloud139_target_path": key, "mobile_target_path": f"移动云盘A/{key}"}
                for key in CATEGORY_KEYS
            },
        }
        _ensure_storage_config(raw)
        storage = raw["storage"]
        self.assertEqual(storage["default_target"], "mobile_main")
        mm = storage["targets"]["mobile_main"]
        self.assertEqual(mm["driver"], "cmcc_api")
        self.assertEqual(mm["native_ingress"], "cloud139_direct")
        self.assertEqual(mm["host"], "https://example.com/hcy")
        self.assertEqual(mm["access_token"], "tok")
        self.assertEqual(mm["upload_mode"], "rapid_first")
        for key in CATEGORY_KEYS:
            category = raw["categories"][key]
            self.assertEqual(mm["categories"][key]["final_root"], cmcc_upload_root(category, raw["cloud139"]))
            self.assertEqual(
                mm["categories"][key]["organizer_root"],
                openlist_root_for_upload(category, backend="cmcc_api", cloud139_config=raw["cloud139"]),
            )

    def test_fallback_chain_matches_legacy_cmcc_upload_root(self) -> None:
        # cmcc_parent_path 优先；其次 cloud139_target_path；最后 mobile_target_path/label。
        parent = {"label": "电影", "cmcc_parent_path": "/自定义", "mobile_target_path": "移动云盘A/电影"}
        self.assertEqual(cmcc_upload_root(parent), "自定义")
        plain = {"label": "电影", "mobile_target_path": "移动云盘A/电影"}
        self.assertEqual(cmcc_upload_root(plain), "移动云盘A/电影")
        with_root = {"label": "电影", "cloud139_target_path": "根目录/电影", "mobile_target_path": "移动云盘A/电影"}
        self.assertEqual(cmcc_upload_root(with_root, {"target_root_path": "根目录"}), "根目录/电影")

    def test_legacy_target_not_overwritten_when_user_provided(self) -> None:
        raw = {
            "storage": {
                "targets": {
                    "mobile_main": {
                        "name": "我的盘",
                        "driver": "cmcc_api",
                        "enabled": True,
                        "guest_selectable": True,
                        "host": "https://user.example.com",
                    }
                }
            }
        }
        _ensure_storage_config(raw)
        self.assertEqual(raw["storage"]["targets"]["mobile_main"]["host"], "https://user.example.com")

    def test_load_config_always_materializes_mobile_main(self) -> None:
        cfg = load_config()
        mm = cfg.raw["storage"]["targets"]["mobile_main"]
        self.assertEqual(mm["driver"], "cmcc_api")
        self.assertEqual(cfg.raw["storage"]["default_target"], "mobile_main")


class StorageConfigPersistenceTests(unittest.TestCase):
    def test_unknown_driver_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_advanced_config_payload(
                {"mode": "merge", "config": {"storage": {"targets": {"x": {"driver": "unknown_driver"}}}}}
            )

    def test_target_without_driver_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_advanced_config_payload(
                {"mode": "merge", "config": {"storage": {"targets": {"x": {"name": "Incomplete"}}}}}
            )

    def test_rclone_target_without_remote_or_local_root_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_advanced_config_payload(
                {"mode": "merge", "config": {"storage": {"targets": {"x": {"driver": "rclone"}}}}}
            )

    def test_unknown_target_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_advanced_config_payload(
                {"mode": "merge", "config": {"storage": {"targets": {"x": {"driver": "cmcc_api", "bogus": 1}}}}}
            )

    def test_native_ingress_is_persisted_only_for_supported_cmcc_binding(self) -> None:
        out = normalize_advanced_config_payload(
            {
                "mode": "merge",
                "config": {
                    "storage": {
                        "targets": {
                            "archive": {
                                "driver": "cmcc_api",
                                "native_ingress": "cloud139_direct",
                            }
                        }
                    }
                },
            }
        )
        self.assertEqual(
            out["storage"]["targets"]["archive"]["native_ingress"],
            "cloud139_direct",
        )

        for target in (
            {"driver": "cmcc_api", "native_ingress": "unknown_ingress"},
            {"driver": "rclone", "remote": "archive", "native_ingress": "cloud139_direct"},
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                normalize_advanced_config_payload(
                    {
                        "mode": "merge",
                        "config": {"storage": {"targets": {"archive": target}}},
                    }
                )

    def test_bad_destination_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_advanced_config_payload(
                {"mode": "merge", "config": {"storage": {"targets": {"Bad ID!": {"driver": "cmcc_api"}}}}}
            )

    def test_storage_round_trip_via_sanitize(self) -> None:
        payload = {
            "storage": {
                "enabled": True,
                "plan_version": 3,
                "default_target": "quark_archive",
                "targets": {
                    "quark_archive": {
                        "name": "夸克归档",
                        "driver": "rclone",
                        "enabled": True,
                        "guest_selectable": False,
                        "remote": "quark_dst",
                        "categories": {
                            "movie": {
                                "final_root": "影视/电影",
                                "organizer_namespace_id": "openlist:main",
                                "organizer_root": "/夸克/影视/电影",
                            }
                        },
                    }
                },
            }
        }
        out = sanitize_advanced_config(payload, current={}, preserve_secret_placeholders=False)
        target = out["storage"]["targets"]["quark_archive"]
        self.assertEqual(target["driver"], "rclone")
        self.assertFalse(target["guest_selectable"])
        self.assertEqual(target["categories"]["movie"]["final_root"], "影视/电影")

    def test_secret_fields_are_masked(self) -> None:
        redacted = redact_advanced_config(
            {"storage": {"targets": {"m": {"access_token": "abc", "phone": "138"}}}}
        )
        target = redacted["storage"]["targets"]["m"]
        self.assertEqual(target["access_token"], "***")
        self.assertEqual(target["phone"], "***")

    def test_merge_keeps_masked_secret_placeholder_out_of_payload(self) -> None:
        out = sanitize_advanced_config(
            {"storage": {"targets": {"m": {"driver": "cmcc_api", "access_token": "***"}}}},
            current={},
            preserve_secret_placeholders=True,
        )
        target = out["storage"]["targets"]["m"]
        self.assertNotIn("access_token", target)
        self.assertEqual(target["driver"], "cmcc_api")

    def test_clear_secrets_handles_nested_storage_path(self) -> None:
        out = normalize_advanced_config_payload(
            {
                "mode": "merge",
                "clear_secrets": ["storage.targets.mobile_main.access_token"],
                "config": {"storage": {"targets": {"mobile_main": {"driver": "cmcc_api", "access_token": "s"}}}},
            }
        )
        target = out["storage"]["targets"]["mobile_main"]
        self.assertNotIn("access_token", target)

    def test_replace_with_masked_storage_secret_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_advanced_config_payload(
                {"mode": "replace", "config": {"storage": {"targets": {"m": {"driver": "cmcc_api", "access_token": "***"}}}}}
            )


if __name__ == "__main__":
    unittest.main()
