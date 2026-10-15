from __future__ import annotations

import unittest

from fnos_media_import.services.transfer_status_projection import project_transfer_status
from tests.test_transfer_status_consistency import observation, record


class RelayProgressProjectionTests(unittest.TestCase):
    def test_parallel_channels_and_buffer_are_projected_on_the_same_job_without_aliasing(self):
        live = observation(
            stage="transferring",
            relay_buffer={"enabled": True, "depth": 2, "ready": 1, "reserved": 1, "waiting": True},
            download={"state": "running", "active": True, "filename": "next.mkv", "run_id": 9,
                      "file_id": "dl:2", "percent": 20, "stale": False, "age_seconds": 0},
            upload={"state": "running", "active": True, "filename": "current.mkv", "run_id": 8,
                    "file_id": "up:1", "percent": 50, "stale": True, "age_seconds": 30},
        )
        items = project_transfer_status([live], [record()])
        self.assertEqual(len(items), 1)
        current = items[0]
        self.assertEqual(current["download"], live["download"])
        self.assertEqual(current["upload"], live["upload"])
        self.assertEqual(current["relay_buffer"], live["relay_buffer"])
        self.assertFalse(current["completed"])
        current["relay_buffer"]["ready"] = 2
        current["upload"]["filename"] = "other.mkv"
        self.assertEqual(live["relay_buffer"]["ready"], 1)
        self.assertEqual(live["upload"]["filename"], "current.mkv")

    def test_durable_post_transfer_attention_and_retry_remove_pipeline_metrics_and_buffer(self):
        live = observation(relay_buffer={"enabled": True, "depth": 2, "ready": 1, "reserved": 1, "waiting": True})
        for status, phase in (("transferring", "target_verifying"), ("organizing", "organizing"),
                              ("review", "manual_review"), ("failed", "failed"),
                              ("retry_requested", "retry_wait"), ("done", "completed")):
            with self.subTest(status=status, phase=phase):
                item = project_transfer_status([live], [record(job_status=status, phase=phase)])[0]
                self.assertNotIn("relay_buffer", item)
                for channel in ("download", "upload", "hash"):
                    self.assertIsNone(item[channel]["percent"])
                    self.assertIsNone(item[channel]["file_id"])
                    self.assertFalse(item[channel]["active"])
                self.assertEqual(item["completed"], status == "done")


if __name__ == "__main__":
    unittest.main()
