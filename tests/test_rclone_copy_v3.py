"""阶段 3：rclone_copy 运行参数、env 生成、重叠检查与环境校验。"""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from typing import Any

from fnos_media_import.database import Database
from fnos_media_import.services.rclone_environment import RcloneEnvironmentChecker
from fnos_media_import.services.rclone_service import RcloneService


def _copy_plan(*, job_id: int, source_root: str = "离线下载/电影", target_root: str = "影视/_入库暂存/电影") -> dict:
    return {
        "version": 3,
        "job_id": job_id,
        "source": {"kind": "quark", "ingress": "quark_to_mobile", "namespace_id": "rclone:MP"},
        "destination": {"id": "quark_archive", "driver": "rclone", "namespace_id": "rclone:quark_dst", "config_revision": 3},
        "transfer": {"strategy": "rclone_copy", "executor": "rclone", "delete_source_after_ack": True},
        "paths": {
            "landing": {"namespace_id": "rclone:MP", "path": f"{source_root}/job-{job_id}"},
            "staging": {"namespace_id": "rclone:quark_dst", "path": f"{target_root}/job-{job_id}"},
            "final": {"namespace_id": "rclone:quark_dst", "path": "影视/电影"},
        },
    }


def _copy_job(*, job_id: int, plan: dict) -> dict:
    return {
        "id": job_id,
        "plan_version": 3,
        "destination_id": str((plan.get("destination") or {}).get("id") or ""),
        "category": "movie",
        "target_route": "quark_to_mobile",
        "raw_data": {"execution_plan": plan},
    }


def _service(copy_run: dict | None = None) -> RcloneService:
    service = RcloneService.__new__(RcloneService)
    service.config = {
        "upload_backend": "cmcc_api",
        "staging_enabled": True,
        "staging_dir_name": "_入库暂存",
        "config_path": "/config/rclone/rclone.conf",
        "cache_dir": "/cache",
        "remote_name": "MP",
        "app_callback_url": "http://127.0.0.1:5251/api/callback/rclone",
        "local_temp": "/temp/fnos-media-import",
        "lock_file": "/tmp/test.lock",
    }
    service.categories = {
        "movie": {"label": "电影", "quark_save_path": "/离线下载/电影", "mobile_target_path": "移动云盘A/电影"},
    }
    service.cmcc_upload_config = {"enabled": True, "backend": "cmcc_api"}
    service.cloud139_config = {}
    service.storage_config = {}
    return service


class CopyRunReaderTests(unittest.TestCase):
    def test_reads_rclone_copy_plan(self) -> None:
        run = _service()._copy_run_from_job(_copy_job(job_id=42, plan=_copy_plan(job_id=42)))
        self.assertTrue(run["copy_mode"])
        self.assertEqual(run["job_id"], 42)
        self.assertEqual(run["category"], "movie")
        self.assertEqual(run["category_suffix"], "MOVIE")
        self.assertEqual(run["job_dir_name"], "job-42")
        self.assertEqual(run["source_remote"], "MP")
        self.assertEqual(run["source_root"], "离线下载/电影")
        self.assertEqual(run["target_remote"], "quark_dst")
        self.assertEqual(run["target_root"], "影视/_入库暂存/电影")
        self.assertEqual(run["local_target"], "")

    def test_ignores_non_copy_plan(self) -> None:
        plan = _copy_plan(job_id=42)
        plan["transfer"]["strategy"] = "relay_upload"
        self.assertEqual(_service()._copy_run_from_job(_copy_job(job_id=42, plan=plan)), {})

    def test_ignores_missing_plan(self) -> None:
        self.assertEqual(_service()._copy_run_from_job({"id": 42, "raw_data": {}}), {})


class CopyRunValidationTests(unittest.TestCase):
    def test_valid_copy_run_normalizes(self) -> None:
        run = _service()._validated_copy_run(
            {"copy_mode": True, "job_id": 42, "category": "movie", "job_dir_name": "job-42",
             "source_remote": "MP", "source_root": "/离线下载/电影",
             "target_remote": "quark_dst", "target_root": "影视/_入库暂存/电影"}
        )
        self.assertEqual(run["source_root"], "离线下载/电影")

    def test_rejects_missing_fields(self) -> None:
        svc = _service()
        base = {"copy_mode": True, "job_id": 42, "category": "movie", "job_dir_name": "job-42",
                "source_remote": "MP", "source_root": "离线下载/电影",
                "target_remote": "quark_dst", "target_root": "影视/_入库暂存/电影"}
        for key in ("source_remote", "source_root", "target_root"):
            broken = {**base, key: ""}
            self.assertEqual(svc._validated_copy_run(broken), {}, f"{key} empty should reject")
        self.assertEqual(svc._validated_copy_run({**base, "job_dir_name": "job-43"}), {})

    def test_rejects_same_remote_overlap(self) -> None:
        run = {"copy_mode": True, "job_id": 42, "category": "movie", "job_dir_name": "job-42",
               "source_remote": "MP", "source_root": "离线下载/电影",
               "target_remote": "MP", "target_root": "离线下载/电影"}
        self.assertEqual(_service()._validated_copy_run(run), {})

    def test_allows_different_remote_same_path(self) -> None:
        run = {"copy_mode": True, "job_id": 42, "category": "movie", "job_dir_name": "job-42",
               "source_remote": "MP", "source_root": "影视/电影",
               "target_remote": "quark_dst", "target_root": "影视/电影"}
        self.assertTrue(_service()._validated_copy_run(run))

    def test_rejects_non_absolute_local_target(self) -> None:
        run = {"copy_mode": True, "job_id": 42, "category": "movie", "job_dir_name": "job-42",
               "source_remote": "MP", "source_root": "离线下载/电影",
               "target_remote": "", "target_root": "relative/暂存"}
        self.assertEqual(_service()._validated_copy_run(run), {})


class CopyOverlapTests(unittest.TestCase):
    def test_same_remote_containment_overlaps(self) -> None:
        self.assertTrue(RcloneService._copy_paths_overlap("MP", "a/b", "MP", "a/b"))
        self.assertTrue(RcloneService._copy_paths_overlap("MP", "a/b/c", "MP", "a/b"))

    def test_different_remote_never_overlaps(self) -> None:
        self.assertFalse(RcloneService._copy_paths_overlap("MP", "a/b", "quark_dst", "a/b"))


class CopyEnvTests(unittest.TestCase):
    def test_env_emits_copy_mode_contract(self) -> None:
        run = {
            "copy_mode": True, "job_id": 42, "category": "movie", "job_dir_name": "job-42",
            "source_remote": "MP", "source_root": "离线下载/电影",
            "target_remote": "quark_dst", "target_root": "影视/_入库暂存/电影", "local_target": "",
        }
        env = _service()._env(trigger_reason="quark_completed", staging_run=run)
        self.assertEqual(env["RCLONE_COPY_MODE"], "true")
        self.assertEqual(env["RCLONE_UPLOAD_BACKEND"], "rclone_copy")
        self.assertEqual(env["RCLONE_COPY_SOURCE_REMOTE"], "MP")
        self.assertEqual(env["RCLONE_COPY_SOURCE_ROOT"], "离线下载/电影")
        self.assertEqual(env["RCLONE_COPY_TARGET_REMOTE"], "quark_dst")
        self.assertEqual(env["RCLONE_COPY_TARGET_ROOT"], "影视/_入库暂存/电影")
        self.assertEqual(env["RCLONE_ONLY_JOB_DIR"], "job-42")
        self.assertEqual(env["RCLONE_ONLY_CATEGORY"], "movie")

    def test_invalid_copy_run_cannot_fall_back(self) -> None:
        with self.assertRaisesRegex(ValueError, "禁止按当前后台配置降级"):
            _service()._env(staging_run={"copy_mode": True, "job_id": 42})

    def test_relay_env_uses_credentials_from_destination_instance(self) -> None:
        service = _service()
        service.storage_config = {
            "targets": {
                "mobile_backup": {
                    "driver": "cmcc_api",
                    "host": "https://backup.example.test/hcy",
                    "access_token": "backup-basic-token",
                    "phone": "13900000000",
                }
            }
        }
        run = {
            "job_id": 42,
            "category": "movie",
            "category_suffix": "MOVIE",
            "job_dir_name": "job-42",
            "source_category_root": "离线下载/电影",
            "storage_staging_category_root": "移动云备份/电影/_入库暂存",
            "storage_backend": "cmcc_api",
            "destination_id": "mobile_backup",
            "delete_source_after_ack": True,
        }
        env = service._env(staging_run=run)
        self.assertEqual(env["CMCC_HOST"], "https://backup.example.test/hcy")
        self.assertEqual(env["CMCC_ACCESS_TOKEN"], "backup-basic-token")
        self.assertEqual(env["CMCC_PHONE"], "13900000000")
        self.assertNotEqual(env["CMCC_ACCESS_TOKEN"], "secret-token")

    def test_relay_env_freezes_source_delete_policy(self) -> None:
        service = _service()
        service.storage_config = {
            "targets": {
                "mobile_main": {
                    "driver": "cmcc_api",
                    "host": "https://example.test",
                    "access_token": "basic-token",
                }
            }
        }
        run = {
            "job_id": 42,
            "category": "movie",
            "category_suffix": "MOVIE",
            "job_dir_name": "job-42",
            "source_category_root": "离线下载/电影",
            "storage_staging_category_root": "移动云/电影/_入库暂存",
            "storage_backend": "cmcc_api",
            "destination_id": "mobile_main",
            "delete_source_after_ack": False,
        }
        self.assertEqual(service._env(staging_run=run)["RCLONE_DELETE_SOURCE_AFTER_ACK"], "false")


class CopyRunDatabaseBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.path = root / f"rclone-copy-{uuid.uuid4().hex}.db"
        self.database = Database(self.path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_binds_copy_run_to_persisted_execution_plan(self) -> None:
        job_id, _ = self.database.job_commands.create(
            {
                "title": "T", "category": "movie", "category_label": "电影",
                "source_type": "quark", "source_url": "https://u", "target_route": "quark_to_mobile",
                "target_path": "/离线下载/电影", "status": "created",
                "destination_id": "quark_archive", "plan_version": 3,
                "raw_data": {},
            }
        )
        self.database.job_commands.update(job_id, {"raw_data": {"execution_plan": _copy_plan(job_id=job_id)}})
        service = _service()
        service.db = self.database
        requested = {
            "copy_mode": True, "job_id": job_id, "category": "movie", "job_dir_name": f"job-{job_id}",
            "source_remote": "MP", "source_root": "离线下载/电影",
            "target_remote": "quark_dst", "target_root": "影视/_入库暂存/电影", "local_target": "",
            "destination_id": "quark_archive", "delete_source_after_ack": True,
        }
        bound, error = service._database_bound_staging_run(requested)
        self.assertEqual(error, "")
        self.assertTrue(bound["copy_mode"])
        self.assertEqual(bound["job_id"], job_id)

    def test_rejects_copy_run_mismatching_persisted_plan(self) -> None:
        job_id, _ = self.database.job_commands.create(
            {
                "title": "T", "category": "movie", "category_label": "电影",
                "source_type": "quark", "source_url": "https://u2", "target_route": "quark_to_mobile",
                "target_path": "/离线下载/电影", "status": "created",
                "destination_id": "quark_archive", "plan_version": 3,
                "raw_data": {},
            }
        )
        self.database.job_commands.update(job_id, {"raw_data": {"execution_plan": _copy_plan(job_id=job_id)}})
        service = _service()
        service.db = self.database
        tampered = {
            "copy_mode": True, "job_id": job_id, "category": "movie", "job_dir_name": f"job-{job_id}",
            "source_remote": "MP", "source_root": "离线下载/电影",
            "target_remote": "quark_dst", "target_root": "其他盘/暂存", "local_target": "",
            "destination_id": "quark_archive", "delete_source_after_ack": True,
        }
        bound, error = service._database_bound_staging_run(tampered)
        self.assertEqual(bound, {})
        self.assertIn("不一致", error)


class CopyEnvironmentCheckerTests(unittest.TestCase):
    def test_check_copy_mode_validates_remotes(self) -> None:
        calls: list[list[str]] = []

        def runner(name: str, command: list[str], allow_empty: bool) -> dict[str, Any]:
            calls.append(command)
            return {"name": name, "ok": command[0] == "rclone", "message": "ok", "exit_code": 0}

        checker = RcloneEnvironmentChecker(
            {
                "copy_source_remote": "MP",
                "copy_target_remote": "quark_dst",
                "config_path": "/config/rclone/rclone.conf",
                "cache_dir": "/cache",
            },
            command_runner=runner,
        )
        result = checker.check_copy_mode()
        self.assertTrue(result["success"])
        self.assertTrue(any("MP:" in " ".join(call) for call in calls))
        self.assertTrue(any("quark_dst:" in " ".join(call) for call in calls))

    def test_check_copy_mode_rejects_unsafe_local_target(self) -> None:
        def runner(name: str, command: list[str], allow_empty: bool) -> dict[str, Any]:
            return {"name": name, "ok": True, "message": "ok", "exit_code": 0}

        checker = RcloneEnvironmentChecker(
            {"copy_source_remote": "MP", "copy_local_target": "../escape", "config_path": "/c", "cache_dir": "/c"},
            command_runner=runner,
        )
        result = checker.check_copy_mode()
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
