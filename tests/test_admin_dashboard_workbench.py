from __future__ import annotations

import unittest
from pathlib import Path

from fnos_media_import.services.admin_dashboard_service import (
    AdminDashboardDependencies,
    AdminDashboardService,
)


class _JobQueries:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items

    def list(self, *, limit: int, offset: int = 0, **filters: object) -> list[dict[str, object]]:
        return self.items[offset : offset + limit]


class _RequestQueries:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items

    def list(self, limit: int = 100, status: str | None = None, offset: int = 0) -> list[dict[str, object]]:
        items = self.items if status is None else [item for item in self.items if item.get("status") == status]
        return items[offset : offset + limit]


class AdminDashboardWorkbenchTests(unittest.TestCase):
    def test_terminal_worker_failures_do_not_keep_active_queue_in_error(self) -> None:
        item = AdminDashboardService._queue_health(
            {
                "pending": 0,
                "running": 0,
                "failed": 2,
                "expired_leases": 0,
            }
        )

        self.assertEqual(item["state"], "ok")
        self.assertEqual(item["summary"], "队列已清空")

    def test_summary_contains_eight_sanitized_recent_jobs(self) -> None:
        jobs = [
            {
                "id": index,
                "title": f"任务 {index}",
                "category": "movie",
                "category_label": "电影",
                "source_type": "quark",
                "target_route": "quark_to_mobile",
                "status": "done" if index == 1 else "transferring",
                "updated_at": f"2026-08-{index:02d}T12:00:00+08:00",
                "raw_data": {"private": "dashboard must not expose this"},
            }
            for index in range(1, 11)
        ]
        service = AdminDashboardService(
            AdminDashboardDependencies(
                jobs=_JobQueries(jobs),
                requests=_RequestQueries([{"status": "pending_review"}]),
                reconcile_job=lambda item, _reason: item,
                decorate_job=lambda item: item,
                sync_requests=lambda items: items,
                system_status=lambda: {
                    "rclone": {"enabled": True, "running": True, "queue_count": 1},
                    "worker_queue": {
                        "enabled": True,
                        "dispatch_enabled": True,
                        "runtime_required": True,
                        "runtime_running": True,
                        "heartbeat_stale": False,
                        "queue_healthy": True,
                        "pending": 1,
                        "running": 1,
                        "failed": 0,
                        "expired_leases": 0,
                    },
                    "organizer": {"enabled": True, "openlist_configured": True, "counts": {}},
                    "update_scheduler": {"enabled": True},
                    "trending_discovery": {"enabled": False},
                    "data": {
                        "database": {"healthy": True},
                        "storage": {"free_bytes": 20 * 1024**3},
                    },
                },
            )
        )

        summary = service.summary(limit=200)

        self.assertEqual(summary["total_recent_jobs"], 10)
        self.assertEqual(summary["guest_request_status_counts"], {"pending_review": 1})
        self.assertEqual(len(summary["recent_jobs"]), 8)
        self.assertEqual(summary["recent_jobs"][0]["title"], "任务 1")
        self.assertNotIn("raw_data", summary["recent_jobs"][0])
        self.assertTrue(summary["rclone"]["running"])
        self.assertEqual(len(summary["health"]["items"]), 6)
        self.assertEqual(summary["health"]["items"][0]["id"], "worker")
        self.assertEqual(summary["health"]["items"][2]["state"], "active")

    def test_template_and_script_keep_dashboard_render_contract(self) -> None:
        template = Path("templates/admin.html").read_text(encoding="utf-8")
        script = Path("static/admin-jobs.js").read_text(encoding="utf-8")

        for element_id in (
            "adminOverviewGreeting",
            "adminOverview",
            "adminOverviewJobsList",
            "adminOverviewPipeline",
            "adminOverviewServices",
            "adminOverviewActivity",
        ):
            self.assertIn(f'id="{element_id}"', template)

        self.assertIn("summary.recent_jobs", script)
        self.assertIn("renderOverviewPipeline", script)
        self.assertIn("renderOverviewServices", script)
        self.assertIn("summary.health", script)
        self.assertNotIn('id="adminOverviewJobsBody"', template)


if __name__ == "__main__":
    unittest.main()
