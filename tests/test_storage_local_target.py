"""阶段 4：本地目录作为受控 rclone 目标（路径安全 + 部署挂载契约）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml

from fnos_media_import.storage._pathutil import resolve_local_target
from fnos_media_import.storage.registry import get_driver
from fnos_media_import.storage.types import StorageTarget, TargetCategoryConfig


ROOT = Path(__file__).resolve().parents[1]

LOCAL_ROOT = "/storage-targets/local-library"


def _target(destination_id: str, **config: Any) -> StorageTarget:
    return StorageTarget(
        destination_id=destination_id,
        name=destination_id,
        driver="rclone",
        enabled=True,
        guest_selectable=True,
        config=config,
        categories={
            "movie": TargetCategoryConfig(
                final_root=str(config.get("_final_root") or "电影"),
                organizer_namespace_id=str(config.get("_organizer_ns") or ""),
                organizer_root=str(config.get("_organizer_root") or ""),
            )
        },
    )


class LocalRcloneDriverValidateTests(unittest.TestCase):
    def test_relative_root_is_rejected(self) -> None:
        errors = get_driver("rclone").validate(_target("local", root="relative/path"))
        self.assertTrue(errors)
        self.assertTrue(any("root" in error for error in errors))

    def test_missing_root_and_remote_is_rejected(self) -> None:
        errors = get_driver("rclone").validate(_target("local"))
        self.assertTrue(errors)

    def test_absolute_root_is_accepted(self) -> None:
        errors = get_driver("rclone").validate(_target("local", root=LOCAL_ROOT))
        self.assertEqual(errors, [])

    def test_root_with_dotdot_is_rejected(self) -> None:
        errors = get_driver("rclone").validate(_target("local", root="/storage-targets/../data"))
        self.assertTrue(errors)


class ResolveLocalTargetTests(unittest.TestCase):
    def test_rejects_dotdot_escape(self) -> None:
        with self.assertRaises(ValueError):
            resolve_local_target(LOCAL_ROOT, "movie/../../etc")

    def test_rejects_absolute_relative(self) -> None:
        with self.assertRaises(ValueError):
            resolve_local_target(LOCAL_ROOT, "/etc/passwd")

    def test_rejects_dotdot_in_root(self) -> None:
        with self.assertRaises(ValueError):
            resolve_local_target("/storage-targets/../data", "movie")

    def test_rejects_empty_relative(self) -> None:
        with self.assertRaises(ValueError):
            resolve_local_target(LOCAL_ROOT, "")

    def test_accepts_normal_subpath(self) -> None:
        resolved = resolve_local_target(LOCAL_ROOT, "电影/剧集")
        self.assertEqual(resolved, f"{LOCAL_ROOT}/电影/剧集")

    def test_normalizes_separators(self) -> None:
        resolved = resolve_local_target("\\storage-targets\\local", "movie\\sub")
        self.assertEqual(resolved, "/storage-targets/local/movie/sub")

    def test_result_stays_under_root(self) -> None:
        resolved = resolve_local_target(LOCAL_ROOT, "影视/电影")
        self.assertTrue(resolved.startswith(f"{LOCAL_ROOT}/"))


class LocalResolvePathsTests(unittest.TestCase):
    def test_final_and_staging_stay_under_configured_root(self) -> None:
        plan = get_driver("rclone").resolve_paths(
            target=_target("local_library", root=LOCAL_ROOT),
            category_key="movie",
            job_id=7,
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        self.assertEqual(plan.namespace_id, "local:local_library")
        self.assertTrue(plan.final.path.startswith(f"{LOCAL_ROOT}/"))
        self.assertTrue(plan.staging.path.startswith(f"{LOCAL_ROOT}/"))
        self.assertEqual(plan.final.path, f"{LOCAL_ROOT}/电影")
        self.assertEqual(plan.staging.path, f"{LOCAL_ROOT}/_入库暂存/电影/job-7")

    def test_without_staging_staging_equals_final(self) -> None:
        plan = get_driver("rclone").resolve_paths(
            target=_target("local_library", root=LOCAL_ROOT),
            category_key="movie",
            job_id=7,
            staging_enabled=False,
            staging_dir_name="_入库暂存",
        )
        self.assertEqual(plan.staging.path, plan.final.path)
        self.assertTrue(plan.final.path.startswith(f"{LOCAL_ROOT}/"))


class ComposeLocalMountTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_compose_files_declare_optional_gated_local_storage_mount(self) -> None:
        for name in ("docker-compose.yml", "compose.dev.yaml"):
            text = self._read(name)
            self.assertIn("/storage-targets", text)
            mount_lines = [
                line.strip()
                for line in text.splitlines()
                if "/storage-targets" in line and "RCLONE_LOCAL_STORAGE_HOST_PATH" in line
            ]
            self.assertTrue(mount_lines, name)
            # 默认注释掉，且由环境变量门控，不影响现有部署。
            self.assertTrue(all(line.startswith("#") for line in mount_lines), name)

    def test_compose_parses_and_keeps_existing_worker_volumes(self) -> None:
        for name in ("docker-compose.yml", "compose.dev.yaml"):
            compose = yaml.safe_load(self._read(name))
            worker = compose["services"]["fnos-rclone-worker"]
            volumes = {str(value) for value in worker["volumes"]}
            self.assertIn("${RCLONE_CONFIG_HOST_PATH:-./rclone/config}:/config/rclone", volumes)
            self.assertIn("${RCLONE_CACHE_HOST_PATH:-./rclone/cache}:/cache", volumes)
            self.assertIn("${RCLONE_TEMP_HOST_PATH:-./rclone/temp}:/temp", volumes)

    def test_env_example_documents_local_storage_host_path(self) -> None:
        env_text = self._read(".env.example")
        self.assertIn("RCLONE_LOCAL_STORAGE_HOST_PATH=./rclone/local-storage", env_text)
        self.assertIn("/storage-targets", env_text)
        # 注释必须说明它不是应用数据目录。
        self.assertIn("APP_DATA_HOST_PATH", env_text)


if __name__ == "__main__":
    unittest.main()
