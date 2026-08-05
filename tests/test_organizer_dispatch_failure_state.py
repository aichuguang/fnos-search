from __future__ import annotations

import unittest

from fnos_media_import.constants import JOB_DONE, JOB_REVIEW
from fnos_media_import.services.organizer_dispatch_service import OrganizerDispatchService


class OrganizerDispatchFailureStateTests(unittest.TestCase):
    @staticmethod
    def _service(stage_updates: list[tuple]) -> OrganizerDispatchService:
        job = {
            "id": 11,
            "title": "测试剧",
            "category": "tv",
            "category_label": "电视剧",
            "status": JOB_DONE,
            "raw_data": {},
        }

        class FakeDatabase:
            @staticmethod
            def get_job(_job_id: int) -> dict:
                return dict(job)

            @staticmethod
            def add_event(*_args, **_kwargs) -> None:
                return None

        class FailingOrganizer:
            enabled = True
            openlist = type("OpenListState", (), {"configured": True})()

            @staticmethod
            def enqueue_from_completed_directory(**_kwargs):
                raise RuntimeError("connection reset")

        def set_completion_stage(current_job, status, stage, message, *_args, **_kwargs):
            stage_updates.append((status, stage, message))
            return {**current_job, "status": status}

        return OrganizerDispatchService(
            database=FakeDatabase(),
            organizer=FailingOrganizer(),
            resolve_plan=lambda _job: {"root_path": "/移动云/_入库暂存/电视剧/job-11"},
            resolve_rclone_plan=lambda _item: {"root_path": "/移动云/_入库暂存/电视剧/job-11"},
            set_completion_stage=set_completion_stage,
            invalid_virtual_path=lambda _path: False,
        )

    def test_direct_import_enqueue_exception_moves_job_to_review(self) -> None:
        updates: list[tuple] = []
        service = self._service(updates)
        result = {"success": True, "job": service.database.get_job(11)}

        organizer = service.enqueue_completed_import(result, "sixpan_poll")

        self.assertFalse(organizer["success"])
        self.assertEqual(updates[-1][0:2], (JOB_REVIEW, "review"))
        self.assertEqual(result["job"]["status"], JOB_REVIEW)

    def test_rclone_enqueue_exception_moves_job_to_review(self) -> None:
        updates: list[tuple] = []
        service = self._service(updates)
        job = service.database.get_job(11)
        category_refresh = {
            "completed_items": [
                {
                    "job_id": 11,
                    "job": job,
                    "category": "tv",
                    "category_label": "电视剧",
                }
            ]
        }

        organizer = service.enqueue_rclone_completed_items(category_refresh, {"run_id": 3})

        self.assertFalse(organizer["success"])
        self.assertEqual(updates[-1][0:2], (JOB_REVIEW, "review"))


if __name__ == "__main__":
    unittest.main()
