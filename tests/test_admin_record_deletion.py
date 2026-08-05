from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fnos_media_import.database import Database
from fnos_media_import.openapi import get_openapi_spec
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.services.job_admin_command_service import (
    JobAdminCommandDependencies,
    JobAdminCommandService,
)


class AdminRecordDeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"admin-record-deletion-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def _create_job(self, status: str) -> int:
        job_id, created = self.database.create_job(
            {
                "title": f"删除测试-{status}",
                "category": "movie",
                "category_label": "电影",
                "source_type": "magnet",
                "source_url": f"magnet:?xt=urn:btih:{uuid.uuid4().hex}",
                "target_route": "sixpan_offline",
                "status": status,
            }
        )
        self.assertTrue(created)
        return job_id

    def test_completed_job_delete_removes_only_job_record_and_events(self) -> None:
        job_id = self._create_job("done")
        self.database.add_event(job_id, "info", "已完成")
        request_id = self.database.create_guest_request(
            {
                "request_token": uuid.uuid4().hex,
                "job_id": job_id,
                "title": "保留访客记录",
                "category": "movie",
                "source_type": "magnet",
                "source_url": "magnet:?xt=urn:btih:guest-record",
                "status": "done",
                "public_status": "已完成",
            }
        )
        organizer_id = self.database.create_organizer_task(
            job_id=job_id,
            category="movie",
            openlist_root_path="/媒体/电影/删除测试",
            status="done",
        )
        service = JobAdminCommandService(
            JobAdminCommandDependencies(
                imports=SimpleNamespace(retry_job=lambda _job_id: {}),
                jobs=self.database,
                auto_start_rclone=lambda _result, _reason: None,
            )
        )

        result, status_code = service.delete(job_id)

        self.assertEqual(status_code, 200)
        self.assertTrue(result["deleted"])
        self.assertEqual(result["message"], "记录已删除")
        self.assertIsNone(self.database.get_job(job_id))
        self.assertEqual(self.database.list_events(job_id), [])
        self.assertIsNone(self.database.get_guest_request(request_id)["job_id"])
        self.assertIsNone(self.database.get_organizer_task(organizer_id, include_children=False)["job_id"])

    def test_active_job_delete_is_rejected(self) -> None:
        job_id = self._create_job("waiting_organizer")
        service = JobAdminCommandService(
            JobAdminCommandDependencies(
                imports=SimpleNamespace(retry_job=lambda _job_id: {}),
                jobs=self.database,
                auto_start_rclone=lambda _result, _reason: None,
            )
        )

        result, status_code = service.delete(job_id)

        self.assertEqual(status_code, 409)
        self.assertFalse(result["success"])
        self.assertIsNotNone(self.database.get_job(job_id))

    def test_completed_organizer_delete_cascades_its_plan_records(self) -> None:
        task_id = self.database.create_organizer_task(
            category="movie",
            openlist_root_path="/媒体/电影/删除整理记录",
            status="done",
        )
        replaced = self.database.replace_organizer_plan(
            task_id,
            files=[
                {
                    "path": "/媒体/电影/删除整理记录/电影.mkv",
                    "name": "电影.mkv",
                    "parent_path": "/媒体/电影/删除整理记录",
                    "ext": ".mkv",
                }
            ],
            mappings=[
                {
                    "source_path": "/媒体/电影/删除整理记录/电影.mkv",
                    "source_name": "电影.mkv",
                    "target_path": "/媒体/电影/删除整理记录/电影.mkv",
                    "target_name": "电影.mkv",
                    "status": "ready",
                }
            ],
            operations=[
                {
                    "type": "move_file",
                    "source_path": "/媒体/电影/删除整理记录/电影.mkv",
                    "target_path": "/媒体/电影/删除整理记录/电影.mkv",
                    "status": "done",
                }
            ],
            expected_revision=1,
            expected_status="done",
        )
        self.assertTrue(replaced)
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database

        result = service.delete_task(task_id)

        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "记录已删除")
        self.assertIsNone(self.database.get_organizer_task(task_id))
        with self.database.connect() as connection:
            for table in ("organizer_files", "organizer_mappings", "organizer_operations"):
                count = connection.execute(
                    f"SELECT COUNT(*) AS total FROM {table} WHERE task_id = ?",
                    (task_id,),
                ).fetchone()["total"]
                self.assertEqual(count, 0, table)

    def test_active_organizer_delete_is_rejected(self) -> None:
        task_id = self.database.create_organizer_task(
            category="movie",
            openlist_root_path="/媒体/电影/正在执行",
            status="executing",
        )
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database

        result = service.delete_task(task_id)

        self.assertFalse(result["success"])
        self.assertTrue(result["conflict"])
        self.assertEqual(result["message"], "只能删除已完成或已取消的记录")
        self.assertIsNotNone(self.database.get_organizer_task(task_id))


class AdminRecordDeletionContractTests(unittest.TestCase):
    def test_openapi_documents_terminal_record_deletion(self) -> None:
        paths = get_openapi_spec()["paths"]
        self.assertIn("delete", paths["/api/admin/jobs/{job_id}"])
        self.assertIn("delete", paths["/api/admin/organizer/tasks/{task_id}"])

    def test_admin_ui_uses_simplified_actions_and_terminal_delete(self) -> None:
        organizer = Path("static/admin-organizer.js").read_text(encoding="utf-8")
        jobs = Path("static/admin-jobs.js").read_text(encoding="utf-8")

        self.assertNotIn('data-organizer-action="approve"', organizer)
        self.assertIn("确认并整理", organizer)
        self.assertIn("开始整理", organizer)
        self.assertIn("重新识别", organizer)
        self.assertIn("跳过整理", organizer)
        self.assertIn('data-organizer-action="delete"', organizer)
        self.assertNotIn("删除记录已提交，正在等待后台返回", organizer)
        self.assertNotIn("Organizer 记录已删除", organizer)
        self.assertNotIn("只会删除 Organizer 记录", organizer)
        self.assertNotIn("不删除网盘或已入库", organizer)
        self.assertIn('toast("记录已删除", "success")', organizer)
        self.assertIn('method: deleting ? "DELETE" : "POST"', organizer)
        self.assertIn("await loadOrganizer();", organizer)
        self.assertIn("data-job-delete", jobs)
        self.assertIn('["done", "success", "completed", "cancelled"]', jobs)
        self.assertNotIn("网盘和已入库媒体文件未受影响", jobs)
        self.assertNotIn("不删除网盘或已入库", jobs)
        self.assertIn('toast("记录已删除", "success")', jobs)


if __name__ == "__main__":
    unittest.main()
