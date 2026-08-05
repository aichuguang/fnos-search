from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from fnos_media_import.config import _default_config
from fnos_media_import.importers.sixpan import SixPanApiError, SixPanClient, SixPanOfflineImporter
from fnos_media_import.services.import_job_service import ImportJobCreationService, ImportJobRetryService
from fnos_media_import.services.import_service import ImportService
from fnos_media_import.services.public_submission_preparation_service import PublicSubmissionPreparationService
from fnos_media_import.services.sixpan_offline_sync_service import SixPanOfflineSyncService


class _Link:
    source_type = "magnet"
    url = "magnet:?xt=urn:btih:abc"
    password = ""
    route = "sixpan_offline"
    supported = True
    reason = ""

    def to_dict(self):
        return {"source_type": self.source_type, "url": self.url, "route": self.route}


class _Config:
    raw = {"routes": {}}

    @staticmethod
    def category(key: str) -> dict:
        return {"label": key}


class PublicSubmissionPreparationHardeningTests(unittest.TestCase):
    def test_title_bound_and_sixpan_selection_are_scoped(self) -> None:
        seen_limits: list[int] = []

        def limited(value, _label, max_length, **_kwargs):
            seen_limits.append(max_length)
            return str(value or "")[:max_length] if max_length else str(value or "")

        service = PublicSubmissionPreparationService(
            search_cache=lambda _key: None,
            categories=lambda: {"movie": {"label": "Movie"}},
            category=lambda key: {"label": key},
            routes=lambda: {},
            limited_text=limited,
            validate_url=lambda value, _security: str(value),
            safe_string_list=lambda value, **_kwargs: list(value or []),
            safe_quark_selection=lambda value: value if isinstance(value, dict) else {},
            safe_cloud139_selection=lambda value: value if isinstance(value, dict) else {},
            detect_link=lambda *_args, **_kwargs: _Link(),
            security_config=lambda: {"max_title_length": 42, "max_token_length": 80, "max_note_length": 500, "max_password_length": 32},
            config_int=lambda config, key, default: int(config.get(key, default)),
        )

        prepared = service.prepare(
            {
                "url": "magnet:?xt=urn:btih:abc",
                "title": "x" * 100,
                "category": "movie",
                "ignore_files": ["sample.mkv"],
                "sixpan_selection": {"parse_status": "files_ready"},
            }
        )
        self.assertTrue(prepared.scoped_selection)
        self.assertEqual(len(prepared.payload["title"]), 42)
        self.assertIn(42, seen_limits)

    def test_nested_ignore_files_and_cached_title_are_normalized(self) -> None:
        def limited(value, _label, max_length, **_kwargs):
            text = str(value or "")
            if max_length and len(text) > max_length:
                raise ValueError("too long")
            return text

        cached_title = "Cached title"
        service = PublicSubmissionPreparationService(
            search_cache=lambda _key: {
                "title": cached_title,
                "source_url": "magnet:?xt=urn:btih:abc",
                "password": "",
                "source_type": "magnet",
            },
            categories=lambda: {"movie": {"label": "Movie"}},
            category=lambda key: {"label": key},
            routes=lambda: {},
            limited_text=limited,
            validate_url=lambda value, _security: str(value),
            safe_string_list=lambda value, **_kwargs: list(value or []),
            safe_quark_selection=lambda value: value if isinstance(value, dict) else {},
            safe_cloud139_selection=lambda value: value if isinstance(value, dict) else {},
            detect_link=lambda *_args, **_kwargs: _Link(),
            security_config=lambda: {"max_title_length": len(cached_title), "max_token_length": 80, "max_note_length": 500, "max_password_length": 32},
            config_int=lambda config, key, default: int(config.get(key, default)),
        )

        prepared = service.prepare(
            {
                "public_id": "cached",
                "category": "movie",
                "sixpan_selection": {"ignore_files": ["sample.mkv"]},
            }
        )
        self.assertEqual(prepared.payload["title"], cached_title)
        self.assertEqual(prepared.payload["ignore_files"], ["sample.mkv"])
        self.assertEqual(prepared.payload["sixpan_selection"]["ignore_files"], ["sample.mkv"])


class SixpanIdentityTests(unittest.TestCase):
    def test_different_ignore_sets_have_different_identity(self) -> None:
        url = "magnet:?xt=urn:btih:abc"
        first = ImportService._job_source_url(url, {"ignore_files": ["sample", "trailer"]})
        reordered = ImportService._job_source_url(url, {"ignore_files": ["trailer", "sample"]})
        second = ImportService._job_source_url(url, {"ignore_files": ["sample"]})
        nested = ImportService._job_source_url(
            url,
            {"sixpan_selection": {"ignore_files": ["sample"]}},
        )
        parse_only = ImportService._job_source_url(
            url,
            {"ignore_files": ["sample"], "sixpan_selection": {"parse_status": "empty_files", "selected_count": 0}},
        )
        self.assertNotEqual(first, second)
        self.assertEqual(first, reordered)
        self.assertEqual(second, nested)
        self.assertEqual(second, parse_only)
        self.assertEqual(
            ImportService._job_source_url(
                url,
                {"sixpan_selection": {"parse_status": "files_ready", "selected_count": 3}},
            ),
            url,
        )
        self.assertEqual(ImportService._job_source_url(url, {}), url)


class SixpanImporterTests(unittest.TestCase):
    def test_default_config_has_no_embedded_client_credentials(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SIXPAN_CLIENT_ID": "", "SIXPAN_CLIENT_SECRET": ""},
            clear=False,
        ):
            sixpan = _default_config()["sixpan"]

        self.assertEqual(sixpan["client_id"], "")
        self.assertEqual(sixpan["client_secret"], "")

    def test_client_credentials_must_be_configured_explicitly(self) -> None:
        unconfigured = SixPanClient({})
        configured = SixPanClient({"client_id": "test-client", "client_secret": "test-secret"})

        self.assertFalse(unconfigured.auth_configured)
        self.assertEqual(unconfigured.client_id, "")
        self.assertEqual(unconfigured.client_secret, "")
        self.assertTrue(configured.auth_configured)

    def _importer(self, client) -> SixPanOfflineImporter:
        importer = SixPanOfflineImporter(
            {
                "host": "example.test",
                "client_id": "id",
                "client_secret": "secret",
                "access_token": "token",
                "parse_cache_ttl_seconds": 300,
                "parse_cache_max_entries": 8,
                "task_max_pages": 10,
            }
        )
        importer.client = client
        return importer

    def test_parse_result_is_bounded_and_defensively_copied(self) -> None:
        class Client:
            def __init__(self):
                self.calls = 0

            def parse_offline_task(self, source_url, *, source_type, title):
                self.calls += 1
                return {"task_files": [{"name": title, "path": source_url}]}

        client = Client()
        importer = self._importer(client)
        first = importer.parse_resource("Title", "magnet:?xt=urn:btih:abc")
        first["task_files"][0]["name"] = "mutated"
        second = importer.parse_resource("Title", "magnet:?xt=urn:btih:abc")
        self.assertEqual(client.calls, 1)
        self.assertEqual(second["task_files"][0]["name"], "Title")

    def test_find_task_follows_continuation_pages(self) -> None:
        class Client:
            def __init__(self):
                self.tokens: list[str] = []

            def list_offline_tasks(self, *, limit, token):
                self.tokens.append(token)
                if not token:
                    return {"data": {"tasks": [{"identity": "new"}], "list_info": {"token": "page-2"}}}
                return {"data": {"tasks": [{"identity": "old"}], "list_info": {}}}

        client = Client()
        importer = self._importer(client)
        self.assertEqual(importer.find_task("old")["identity"], "old")
        self.assertEqual(client.tokens, ["", "page-2"])

    def test_find_task_reads_outer_continuation_metadata(self) -> None:
        class Client:
            def __init__(self):
                self.tokens: list[str] = []

            def list_offline_tasks(self, *, limit, token):
                self.tokens.append(token)
                if not token:
                    return {
                        "data": {"tasks": [{"identity": "new"}]},
                        "list_info": {"next_page_token": "page-2"},
                    }
                return {"data": {"tasks": [{"identity": "old"}]}}

        client = Client()
        importer = self._importer(client)
        self.assertEqual(importer.find_task("old")["identity"], "old")
        self.assertEqual(client.tokens, ["", "page-2"])

    def test_repeated_continuation_token_is_not_treated_as_missing(self) -> None:
        class Client:
            def list_offline_tasks(self, *, limit, token):
                return {"data": {"tasks": [], "list_info": {"token": "same"}}}

        importer = self._importer(Client())
        with self.assertRaises(SixPanApiError):
            importer.find_task("not-found")

    def test_unknown_status_is_reported_as_unknown(self) -> None:
        importer = self._importer(SimpleNamespace())
        state = importer.task_state({"identity": "x", "status": "vendor_future_state"})
        self.assertEqual(state.state, "unknown")

    def test_numeric_zero_status_is_running(self) -> None:
        importer = self._importer(SimpleNamespace())
        state = importer.task_state({"identity": "x", "status": 0})
        self.assertEqual(state.state, "running")


class _SyncDatabase:
    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = {int(item["id"]): copy.deepcopy(item) for item in jobs}
        self.events: list[tuple] = []
        self.updates: list[tuple[int, dict]] = []

    def list_jobs(self, *, limit, offset=0, status=None, source_type=None, **_kwargs):
        rows = [
            item
            for item in sorted(self.jobs.values(), key=lambda value: int(value["id"]), reverse=True)
            if (not status or item.get("status") == status) and (not source_type or item.get("source_type") == source_type)
        ]
        return copy.deepcopy(rows[offset : offset + limit])

    def get_job(self, job_id):
        return copy.deepcopy(self.jobs.get(int(job_id)))

    def update_job(self, job_id, **values):
        self.updates.append((int(job_id), copy.deepcopy(values)))
        self.jobs[int(job_id)].update(copy.deepcopy(values))

    def add_event(self, *values):
        self.events.append(values)


class SixpanWatchdogTests(unittest.TestCase):
    def _service(self, jobs, importer, now=None):
        database = _SyncDatabase(jobs)
        guest_updates: list[tuple] = []
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: importer,
            poll_limit=lambda: 2,
            category=lambda _key: {},
            enqueue_organizer=lambda *_args, **_kwargs: {"success": True, "queued": True},
            record_completed=lambda *_args, **_kwargs: {"success": True},
            sync_guest_requests=lambda *args, **kwargs: guest_updates.append((args, kwargs)),
            now=now,
        )
        return service, database, guest_updates

    def test_old_submitted_jobs_are_paginated_and_missing_is_bounded(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        jobs = [
            {"id": 1, "status": "submitted", "source_type": "magnet", "external_task_id": "missing-1", "created_at": "2025-12-31T23:00:00Z", "raw_data": {}},
            {"id": 2, "status": "submitted", "source_type": "magnet", "external_task_id": "missing-2", "created_at": "2025-12-31T23:00:00Z", "raw_data": {}},
            {"id": 3, "status": "submitted", "source_type": "magnet", "external_task_id": "missing-3", "created_at": "2025-12-31T23:00:00Z", "raw_data": {}},
        ]
        importer = SimpleNamespace(
            configured=True,
            poll_enabled=True,
            task_missing_poll_limit=2,
            task_unknown_poll_limit=2,
            submitted_timeout_seconds=0,
            find_task=lambda _task_id, **_kwargs: None,
        )
        service, database, guest_updates = self._service(jobs, importer, now=lambda: now)
        first = service.sync()
        self.assertEqual(first["checked"], 3)
        self.assertEqual(first["missing"], 3)
        self.assertTrue(all(database.jobs[index]["status"] == "submitted" for index in (1, 2, 3)))
        second = service.sync()
        self.assertEqual(second["reviewed"], 3)
        self.assertTrue(all(database.jobs[index]["status"] == "review" for index in (1, 2, 3)))
        self.assertEqual(len(guest_updates), 3)

    def test_unknown_state_watchdog_transitions_to_review(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = {"id": 9, "status": "submitted", "source_type": "torrent", "external_task_id": "task-9", "created_at": "2025-12-31T23:00:00Z", "raw_data": {}}
        importer = SimpleNamespace(
            configured=True,
            poll_enabled=True,
            task_missing_poll_limit=5,
            task_unknown_poll_limit=2,
            submitted_timeout_seconds=0,
            find_task=lambda _task_id, **_kwargs: {"identity": "task-9", "status": "future"},
            task_state=lambda _task: SimpleNamespace(state="unknown", message="future", progress=0, bytes_total=0, bytes_processed=0, completed=False, failed=False),
        )
        service, database, _guest_updates = self._service([job], importer, now=lambda: now)
        service.sync()
        self.assertEqual(database.jobs[9]["status"], "submitted")
        service.sync()
        self.assertEqual(database.jobs[9]["status"], "review")
        self.assertEqual(database.jobs[9]["raw_data"]["sixpan_watchdog"]["review_reason"], "unknown_state")

    def test_submitted_timeout_transitions_even_when_task_is_running(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = {"id": 10, "status": "submitted", "source_type": "magnet", "external_task_id": "task-10", "created_at": (now - timedelta(hours=2)).isoformat(), "raw_data": {}}
        importer = SimpleNamespace(
            configured=True,
            poll_enabled=True,
            task_missing_poll_limit=5,
            task_unknown_poll_limit=5,
            submitted_timeout_seconds=60,
            find_task=lambda _task_id, **_kwargs: {"identity": "task-10", "status": "running"},
            task_state=lambda _task: SimpleNamespace(state="running", message="running", progress=1, bytes_total=100, bytes_processed=1, completed=False, failed=False),
        )
        service, database, _guest_updates = self._service([job], importer, now=lambda: now)
        result = service.sync()
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(database.jobs[10]["status"], "review")
        self.assertEqual(database.jobs[10]["raw_data"]["sixpan_watchdog"]["review_reason"], "submitted_timeout")

    def test_late_completed_poll_cannot_restore_cancelled_job(self) -> None:
        job = {
            "id": 11,
            "status": "submitted",
            "source_type": "magnet",
            "external_task_id": "task-11",
            "target_path": "/_入库暂存/电视剧/job-11",
            "raw_data": {},
        }
        database = _SyncDatabase([job])
        organizer_calls: list[tuple] = []

        def find_task(_task_id, **_kwargs):
            database.update_job(11, status="cancelled")
            return {"identity": "task-11", "status": "completed"}

        importer = SimpleNamespace(
            configured=True,
            poll_enabled=True,
            find_task=find_task,
            task_state=lambda _task: SimpleNamespace(
                state="completed",
                message="completed",
                progress=100,
                bytes_total=100,
                bytes_processed=100,
                completed=True,
                failed=False,
            ),
        )
        service = SixPanOfflineSyncService(
            database=database,
            importer=lambda: importer,
            poll_limit=lambda: 20,
            category=lambda _key: {},
            enqueue_organizer=lambda *args, **kwargs: organizer_calls.append((args, kwargs)),
            record_completed=lambda *_args, **_kwargs: {"success": True},
            sync_guest_requests=lambda *_args, **_kwargs: None,
        )

        result = service.sync()

        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(database.jobs[11]["status"], "cancelled")
        self.assertEqual(organizer_calls, [])


class UnsupportedResultContractTests(unittest.TestCase):
    def test_unconfigured_generic_importer_reports_failure(self) -> None:
        class Database:
            def __init__(self):
                self.job = {
                    "id": 8,
                    "status": "provider_submitting",
                    "raw_data": {
                        "provider_submission_fence": {
                            "version": 1,
                            "state": "submitting",
                            "attempt": 1,
                        }
                    },
                }

            def update_job(self, _job_id, **values):
                self.job.update(values)

            def update_job_if_status(self, _job_id, expected_statuses, **values):
                if self.job.get("status") not in set(expected_statuses):
                    return False
                self.job.update(values)
                return True

            def add_event(self, *_args, **_kwargs):
                return None

            def get_job(self, _job_id):
                return dict(self.job)

        service = object.__new__(ImportService)
        service.db = Database()
        service.generic_importers = {}
        result = service._submit_generic_job(
            8,
            "title",
            "https://unsupported.example/item",
            "/target",
            {"label": "Movie"},
            "cloud189",
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["job"]["status"], "unsupported")

    def test_unsupported_creation_and_retry_report_failure(self) -> None:
        class Database:
            def __init__(self):
                self.job = {"id": 1, "status": "created", "raw_data": {}}

            def create_job(self, _data):
                return 1, True

            def get_job(self, _job_id):
                return dict(self.job)

            def update_job(self, _job_id, **values):
                self.job.update(values)

            def update_job_if_status(self, _job_id, expected_statuses, **values):
                if self.job.get("status") not in set(expected_statuses):
                    return False
                self.job.update(values)
                return True

            def add_event(self, *_args, **_kwargs):
                return None

        link = SimpleNamespace(
            url="https://unsupported.example/item",
            source_type="unsupported",
            password="",
            route="unsupported",
            supported=False,
            reason="unsupported",
            to_dict=lambda: {"supported": False},
        )
        database = Database()
        creation = ImportJobCreationService(
            database=database,
            config=_Config(),
            detect_link=lambda *_args, **_kwargs: link,
            job_source_url=lambda url, _payload: url,
            target_path=lambda *_args, **_kwargs: "/target",
            staging_plan=None,
            submit_quark=lambda *_args, **_kwargs: {},
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )
        result = creation.create({"url": link.url, "title": "x", "category": "movie"})
        self.assertFalse(result["success"])

        database.job.update({"target_route": "future", "category": "movie", "source_url": link.url, "source_type": "future", "title": "x"})
        retry = ImportJobRetryService(
            database=database,
            config=_Config(),
            submit_quark=lambda *_args, **_kwargs: {},
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )
        retry_result = retry.retry(1)
        self.assertFalse(retry_result["success"])


if __name__ == "__main__":
    unittest.main()
