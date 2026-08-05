from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fnos_media_import.database import Database
from fnos_media_import.services.job_service import JobService
from fnos_media_import.web_input import _recent_business_events, _task_log_summaries


class _TimelineDatabase:
    def get_job(self, job_id: int) -> dict[str, object]:
        return {
            "id": job_id,
            "title": "测试影片",
            "status": "done",
            "created_at": "2026-08-04T10:00:00Z",
            "updated_at": "2026-08-04T10:06:00Z",
        }

    def list_events(self, job_id: int) -> list[dict[str, object]]:
        return [
            {"id": 1, "level": "info", "message": "网盘保存完成", "created_at": "2026-08-04T10:01:00Z"},
            {"id": 2, "level": "info", "message": "网盘保存完成", "created_at": "2026-08-04T10:01:01Z"},
            {"id": 3, "level": "info", "message": "飞牛媒体库刷新完成", "created_at": "2026-08-04T10:05:00Z"},
        ]

    def list_guest_requests_by_job(self, job_id: int) -> list[dict[str, object]]:
        return []

    def list_guest_request_events_for_requests(self, request_ids: list[int]) -> dict[int, list[dict[str, object]]]:
        return {}

    def list_all_rclone_file_events(self, *, job_id: int) -> list[dict[str, object]]:
        return [
            {"id": 10, "run_id": 7, "job_id": job_id, "filename": "a.mkv", "status": "transferring", "level": "info", "message": "开始搬运", "created_at": "2026-08-04T10:02:00Z"},
            {"id": 11, "run_id": 7, "job_id": job_id, "filename": "a.mkv", "status": "done", "level": "info", "message": "搬运完成", "created_at": "2026-08-04T10:03:00Z"},
            {"id": 12, "run_id": 7, "job_id": job_id, "filename": "b.srt", "status": "failed", "level": "error", "message": "搬运失败", "created_at": "2026-08-04T10:03:30Z"},
        ]

    def list_organizer_tasks_by_job(self, job_id: int, limit: int) -> list[dict[str, object]]:
        return [{"id": 20, "job_id": job_id, "status": "done", "created_at": "2026-08-04T10:04:00Z"}]

    def get_organizer_task(self, task_id: int) -> dict[str, object]:
        return {
            "id": task_id,
            "status": "done",
            "created_at": "2026-08-04T10:04:00Z",
            "mappings": [],
            "operations": [
                {"id": 30, "task_id": task_id, "run_id": 21, "type": "move", "description": "移动文件", "status": "done", "created_at": "2026-08-04T10:04:20Z"}
            ],
        }

    def list_organizer_runs_by_task_ids(self, task_ids: list[int]) -> list[dict[str, object]]:
        return [
            {"id": 21, "task_id": 20, "status": "success", "started_at": "2026-08-04T10:04:05Z", "finished_at": "2026-08-04T10:04:40Z"}
        ]


class JobTimelineTests(unittest.TestCase):
    def test_business_timeline_is_compact_and_technical_events_are_complete(self) -> None:
        job = JobService(_TimelineDatabase()).get_job_with_events(1)  # type: ignore[arg-type]

        self.assertIsNotNone(job)
        assert job is not None
        summaries = [item for item in job["timeline"] if item["type"] == "rclone_summary"]
        self.assertEqual(len(summaries), 1)
        self.assertIn("完成 1 个文件", summaries[0]["message"])
        self.assertIn("失败 1 个", summaries[0]["message"])
        duplicate = next(item for item in job["timeline"] if item["message"] == "网盘保存完成")
        self.assertEqual(duplicate["occurrence_count"], 2)
        self.assertTrue(any(item["type"] == "organizer_run" for item in job["timeline"]))
        self.assertEqual(len([item for item in job["technical_events"] if item["type"] == "rclone_file_event"]), 3)
        self.assertTrue(any(item["type"] == "organizer_operation" for item in job["technical_events"]))


class BusinessEventQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "events.db")
        self.database.init_schema()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO import_jobs (
                    title, category, category_label, source_type, source_url,
                    target_route, status, created_at, updated_at
                ) VALUES ('没有事件的任务', 'movie', '电影', 'quark', 'https://example.invalid/1',
                          'quark_to_mobile', 'created', '2026-08-04T10:00:00Z', '2026-08-04T10:00:00Z')
                """
            )
            self.job_id = int(cursor.lastrowid)
        rclone_event_id = self.database.add_rclone_file_event(
            job_id=self.job_id,
            status="done",
            level="info",
            filename="movie.mkv",
            message="文件搬运完成",
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE rclone_file_events SET created_at = '2026-08-04T10:01:00Z' WHERE id = ?",
                (rclone_event_id,),
            )
            connection.execute(
                """
                INSERT INTO worker_tasks (
                    task_type, payload, status, idempotency_key, available_at,
                    error_message, created_at, updated_at, completed_at
                ) VALUES ('import_execute', ?, 'failed', 'task-log-test',
                          '2026-08-04T10:00:00Z', 'worker failed',
                          '2026-08-04T10:00:00Z', '2026-08-04T10:02:00Z', '2026-08-04T10:02:00Z')
                """,
                (f'{{"job_id": {self.job_id}}}',),
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_job_creation_makes_every_job_visible(self) -> None:
        result = _recent_business_events(self.database, 20, job_id=self.job_id)

        event_types = {item["event_type"] for item in result["items"]}
        self.assertIn("job_created", event_types)
        self.assertIn("rclone_file_event", event_types)
        self.assertEqual(result["total"], 2)

    def test_source_filter_and_pagination_return_total(self) -> None:
        result = _recent_business_events(self.database, 1, source="rclone", offset=0)

        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["source"], "rclone")

    def test_task_log_summary_keeps_one_row_per_job_and_counts_related_records(self) -> None:
        result = _task_log_summaries(self.database, 20, keyword=str(self.job_id))

        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["id"], self.job_id)
        self.assertEqual(item["log_count"], 3)
        self.assertEqual(item["error_count"], 1)
        self.assertEqual(item["latest_log_at"], "2026-08-04T10:02:00Z")

    def test_task_detail_includes_related_worker_record(self) -> None:
        job = JobService(self.database).get_job_with_events(self.job_id)

        self.assertIsNotNone(job)
        assert job is not None
        worker_events = [item for item in job["technical_events"] if item["source"] == "worker_task"]
        self.assertEqual(len(worker_events), 1)
        self.assertEqual(worker_events[0]["status"], "failed")
        self.assertEqual(worker_events[0]["raw_data"]["attempts"], 0)


class TimelineUiContractTests(unittest.TestCase):
    def test_task_detail_and_log_center_expose_task_index_and_raw_logs(self) -> None:
        jobs_script = Path("static/admin-jobs.js").read_text(encoding="utf-8")
        rclone_script = Path("static/admin-rclone.js").read_text(encoding="utf-8")
        template = Path("templates/admin.html").read_text(encoding="utf-8")

        self.assertIn("job.technical_events", jobs_script)
        self.assertIn('className: "job-detail-modal"', jobs_script)
        self.assertIn("mountOrganizerPagedList", jobs_script)
        self.assertIn("renderOriginalLogData(item)", jobs_script)
        self.assertIn("options.openTechnical", jobs_script)
        self.assertIn("/api/admin/system/task-logs", rclone_script)
        self.assertIn('id="taskLogKeyword"', template)
        self.assertIn('id="taskLogsPager"', template)
        self.assertIn('id="taskLogDateFrom" type="hidden"', template)
        self.assertIn('id="taskLogDateTo" type="hidden"', template)
        self.assertIn('data-date-picker', template)
        self.assertIn('class="date-range-control"', template)
        self.assertIn('data-date-role="start"', template)
        self.assertIn('data-date-role="end"', template)
        self.assertNotIn('type="date"', template)
        self.assertIn("function initTaskLogDatePickers()", rclone_script)
        self.assertIn("function renderTaskLogDatePicker(picker)", rclone_script)
        self.assertIn('taskLogDatePickers.get("start")', rclone_script)
        self.assertIn('taskLogDatePickers.get("end")', rclone_script)
        self.assertIn("开始日期不能晚于结束日期", rclone_script)
        self.assertIn("高级排障 · rclone 运行批次", template)
        self.assertNotIn('id="adminBusinessEvents"', template)
        self.assertNotIn('id="adminRcloneFiles"', template)


if __name__ == "__main__":
    unittest.main()
