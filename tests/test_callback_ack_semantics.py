from __future__ import annotations

import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.services.callback_service import CallbackDependencies, RcloneCallbackService


def _safe_int(value, default, minimum, maximum) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


class _CallbackDatabase:
    def __init__(self, job_status: str) -> None:
        self.job = {
            "id": 42,
            "status": job_status,
            "category": "tv",
            "raw_data": {},
        }
        self.updates: list[dict] = []
        self.job_events: list[tuple] = []
        self.file_events: list[dict] = []
        self.rclone_events: list[tuple] = []

    def get_job(self, _job_id: int) -> dict:
        return copy.deepcopy(self.job)

    def update_job(self, _job_id: int, **changes) -> None:
        self.updates.append(copy.deepcopy(changes))
        self.job.update(copy.deepcopy(changes))

    def add_event(self, *args) -> None:
        self.job_events.append(copy.deepcopy(args))

    def add_rclone_file_event(self, **values) -> int:
        self.file_events.append(copy.deepcopy(values))
        return len(self.file_events)

    def add_rclone_event(self, *args) -> None:
        self.rclone_events.append(copy.deepcopy(args))


def _callback_service(database: _CallbackDatabase) -> RcloneCallbackService:
    return RcloneCallbackService(
        CallbackDependencies(
            db=database,
            rclone=SimpleNamespace(),
            safe_int=_safe_int,
            callback_level=lambda _status: "info",
            enqueue_organizer=lambda *_args: None,
            cancelled_status="cancelled",
        )
    )


class CallbackTerminalAckTests(unittest.TestCase):
    def test_cancel_winning_between_read_and_status_write_is_not_revived(self) -> None:
        class RacingDatabase(_CallbackDatabase):
            def update_job_if_status(self, _job_id: int, _expected_statuses, **_changes) -> bool:
                self.job.update(
                    status="cancelled",
                    error_message="admin cancel won the race",
                    raw_data={"cancel": {"active": True, "generation": 1}},
                )
                return False

        database = RacingDatabase("transferring")

        response, status_code = _callback_service(database).handle(
            {
                "run_id": 9,
                "job_id": 42,
                "status": "done",
                "category": "tv",
                "filename": "E01.mkv",
                "source_path": "/source/E01.mkv",
                "target_path": "/target/E01.mkv",
                "message": "completion raced with cancellation",
            }
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(response["disposition"], "ignored_cancelled")
        self.assertIs(response["delete_source_allowed"], False)
        self.assertEqual(response["job"]["status"], "cancelled")
        self.assertEqual(database.job["raw_data"]["cancel"]["generation"], 1)
        self.assertEqual(database.updates, [])

    def test_cancelled_job_records_callback_but_never_allows_source_deletion(self) -> None:
        database = _CallbackDatabase("cancelled")

        response, status_code = _callback_service(database).handle(
            {
                "run_id": 9,
                "job_id": 42,
                "status": "done",
                "category": "tv",
                "filename": "E01.mkv",
                "source_path": "/source/E01.mkv",
                "target_path": "/target/E01.mkv",
                "message": "late completion after cancellation",
            }
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(response["success"])
        self.assertEqual(response["disposition"], "ignored_cancelled")
        self.assertIs(response["delete_source_allowed"], False)
        self.assertEqual(database.updates, [])
        self.assertEqual(database.file_events[0]["status"], "ignored_cancelled")

    def test_cancelled_category_summary_does_not_enter_finalizer(self) -> None:
        database = _CallbackDatabase("cancelled")
        finalizer_calls: list[tuple] = []
        organizer_calls: list[tuple] = []
        service = RcloneCallbackService(
            CallbackDependencies(
                db=database,
                rclone=SimpleNamespace(
                    finalize_category_imports=lambda *args: finalizer_calls.append(args)
                ),
                safe_int=_safe_int,
                callback_level=lambda _status: "info",
                enqueue_organizer=lambda *args: organizer_calls.append(args),
                cancelled_status="cancelled",
            )
        )

        response, status_code = service.handle(
            {
                "run_id": 9,
                "job_id": 42,
                "status": "category_done",
                "category": "tv",
                "message": "late category summary",
            }
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(response["disposition"], "ignored_cancelled")
        self.assertIs(response["delete_source_allowed"], False)
        self.assertEqual(finalizer_calls, [])
        self.assertEqual(organizer_calls, [])

    def test_late_file_callbacks_are_acked_without_rolling_back_later_job_status(self) -> None:
        later_job_statuses = (
            "waiting_openlist",
            "waiting_organizer",
            "organizing",
            "confirming",
            "review",
            "refreshing",
            "done",
        )
        late_callback_statuses = (
            "transferring",
            "processing",
            "done",
            "success",
            "skipped_existing",
            "failed",
            "upload_error",
        )

        for job_status in later_job_statuses:
            for callback_status in late_callback_statuses:
                with self.subTest(job_status=job_status, callback_status=callback_status):
                    database = _CallbackDatabase(job_status)
                    response, status_code = _callback_service(database).handle(
                        {
                            "run_id": 9,
                            "job_id": 42,
                            "status": callback_status,
                            "category": "tv",
                            "filename": "E01.mkv",
                            "source_path": "/source/E01.mkv",
                            "target_path": "/target/E01.mkv",
                            "message": "late callback",
                        }
                    )

                    self.assertEqual(status_code, 200)
                    self.assertTrue(response["success"])
                    self.assertEqual(response["disposition"], "ignored_terminal")
                    self.assertEqual(response["job"]["status"], job_status)
                    self.assertEqual(database.updates, [])
                    self.assertEqual(len(database.file_events), 1)
                    self.assertEqual(database.file_events[0]["status"], "ignored_terminal")
                    self.assertEqual(
                        database.file_events[0]["raw_data"]["ignored_callback_status"],
                        callback_status,
                    )
                    self.assertEqual(
                        database.file_events[0]["raw_data"]["preserved_job_status"],
                        job_status,
                    )
                    self.assertEqual(database.job_events[0][3]["callback_disposition"], "ignored_terminal")
                    self.assertIn("[ignored_terminal]", database.job_events[0][2])

    def test_normal_transfer_phase_callback_still_updates_job(self) -> None:
        database = _CallbackDatabase("transferring")

        response, status_code = _callback_service(database).handle(
            {
                "job_id": 42,
                "status": "done",
                "category": "tv",
                "filename": "E01.mkv",
                "source_path": "/source/E01.mkv",
                "target_path": "/target/E01.mkv",
            }
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(response["success"])
        self.assertEqual(database.updates, [{"status": "transferring", "error_message": ""}])
        self.assertEqual(database.file_events[0]["status"], "done")


class _FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class WorkerHttpCallbackAckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "fnos_rclone_worker.sh"
        ).read_text(encoding="utf-8")
        function_start = script.index("http_callback_post() {")
        python_start = script.index("<<'PY'\n", function_start) + len("<<'PY'\n")
        python_end = script.index("\nPY\n}", python_start)
        cls.callback_python = script[python_start:python_end]

    def _run_callback_python(self, response: object, *, job_dir: str = "") -> int:
        body = response if isinstance(response, bytes) else json.dumps(response).encode("utf-8")
        environment = {
            "CALLBACK_URL": "http://callback.invalid/rclone",
            "CALLBACK_RUN_ID": "7",
            "CALLBACK_STATUS": "done",
            "CALLBACK_CATEGORY": "tv",
            "CALLBACK_FILENAME": "E01.mkv",
            "CALLBACK_SOURCE_PATH": "/source/E01.mkv",
            "CALLBACK_TARGET_PATH": "/target/E01.mkv",
            "RCLONE_ONLY_JOB_DIR": job_dir,
        }
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("urllib.request.urlopen", return_value=_FakeHttpResponse(body)),
            redirect_stderr(stderr),
        ):
            try:
                exec(compile(self.callback_python, "<http_callback_post>", "exec"), {})
            except SystemExit as exc:
                return int(exc.code or 0)
        return 0

    def test_requires_json_object_with_literal_success_true(self) -> None:
        self.assertEqual(self._run_callback_python({"success": True}), 0)
        self.assertEqual(self._run_callback_python(b"<html>ok</html>"), 1)
        self.assertEqual(self._run_callback_python({"success": False}), 1)
        self.assertEqual(self._run_callback_python([{"success": True}]), 1)
        self.assertEqual(self._run_callback_python({"success": 1}), 1)

    def test_rejects_mismatched_response_job_id(self) -> None:
        self.assertEqual(
            self._run_callback_python(
                {
                    "success": True,
                    "disposition": "accepted",
                    "delete_source_allowed": True,
                    "job": {"id": 42},
                },
                job_dir="job-42",
            ),
            0,
        )
        self.assertEqual(
            self._run_callback_python({"success": True, "job": {"id": 43}}, job_dir="job-42"),
            1,
        )

    def test_rejects_mismatched_manifest_job_id(self) -> None:
        self.assertEqual(
            self._run_callback_python(
                {
                    "success": True,
                    "disposition": "accepted",
                    "delete_source_allowed": True,
                    "manifest": {"job_id": "42"},
                },
                job_dir="job-42",
            ),
            0,
        )
        self.assertEqual(
            self._run_callback_python(
                {"success": True, "manifest": {"job_id": 99}},
                job_dir="job-42",
            ),
            1,
        )

    def test_completion_ack_requires_explicit_delete_permission(self) -> None:
        self.assertEqual(
            self._run_callback_python(
                {
                    "success": True,
                    "disposition": "ignored_cancelled",
                    "delete_source_allowed": False,
                    "job": {"id": 42},
                },
                job_dir="job-42",
            ),
            1,
        )
        self.assertEqual(
            self._run_callback_python(
                {
                    "success": True,
                    "disposition": "accepted",
                    "delete_source_allowed": False,
                    "job": {"id": 42},
                },
                job_dir="job-42",
            ),
            1,
        )
        self.assertEqual(
            self._run_callback_python(
                {
                    "success": True,
                    "disposition": "ignored_terminal",
                    "delete_source_allowed": True,
                    "job": {"id": 42},
                },
                job_dir="job-42",
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
