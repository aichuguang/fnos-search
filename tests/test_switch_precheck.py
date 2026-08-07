"""一次性切换预检测试（设计文档 §21.1）。"""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from fnos_media_import.database import Database
from fnos_media_import.planning.switch_precheck import switch_precheck


class SwitchPrecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.path = root / f"switch-precheck-{uuid.uuid4().hex}.db"
        self.database = Database(self.path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def _create_job(self, *, plan_version: int | None, status: str, source_url: str) -> int:
        job_id, _ = self.database.job_commands.create(
            {
                "title": "T",
                "category": "movie",
                "category_label": "电影",
                "source_type": "quark",
                "source_url": source_url,
                "target_route": "quark_to_mobile",
                "target_path": "/x",
                "status": status,
                "plan_version": plan_version,
                "destination_id": "mobile_main" if plan_version == 4 else None,
            }
        )
        return job_id

    def test_passes_when_no_old_inflight(self) -> None:
        result = switch_precheck(self.database)
        self.assertTrue(result["ok"])

    def test_passes_when_old_tasks_are_terminal(self) -> None:
        self._create_job(plan_version=3, status="done", source_url="u1")
        self._create_job(plan_version=2, status="failed", source_url="u2")
        result = switch_precheck(self.database)
        self.assertTrue(result["ok"])

    def test_fails_when_non_v4_inflight_exists(self) -> None:
        self._create_job(plan_version=3, status="waiting_transfer", source_url="u3")
        result = switch_precheck(self.database)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["old_inflight"]), 1)

    def test_ignores_v4_inflight(self) -> None:
        self._create_job(plan_version=4, status="transferring", source_url="u4")
        result = switch_precheck(self.database)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
