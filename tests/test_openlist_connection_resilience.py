from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import requests

from fnos_media_import.app import _cloud139_scan_filters_from_job
from fnos_media_import.organizer.openlist_client import (
    OpenListClient,
    OpenListEndpointUnsupported,
    OpenListError,
    OpenListItem,
    OpenListTransientError,
)
from fnos_media_import.organizer.service import OrganizerService, _clean_title_hint, _title_from_update_payload


class _JsonResponse:
    def __init__(
        self,
        payload: dict | None,
        *,
        status_code: int = 200,
        text: str = "",
        content_type: str = "application/json",
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}
        self.closed = False

    def json(self) -> dict:
        if self.payload is None:
            raise ValueError("not json")
        return self.payload

    def close(self) -> None:
        self.closed = True


class OpenListClientRetryTests(unittest.TestCase):
    @staticmethod
    def _client(*, retries: int = 3) -> OpenListClient:
        return OpenListClient(
            {
                "base_url": "http://openlist.test",
                "request_retries": retries,
                "retry_backoff_seconds": 0,
            }
        )

    def test_list_dir_retries_connection_reset_and_recovers(self) -> None:
        client = self._client(retries=3)
        client.session.request = Mock(
            side_effect=[
                requests.exceptions.ConnectionError(
                    "Connection aborted.",
                    ConnectionResetError(104, "Connection reset by peer"),
                ),
                requests.exceptions.ConnectionError("remote disconnected"),
                _JsonResponse(
                    {
                        "code": 200,
                        "data": {
                            "content": [
                                {
                                    "name": "episode.mkv",
                                    "is_dir": False,
                                    "size": 123,
                                }
                            ]
                        },
                    }
                ),
            ]
        )

        rows = client.list_dir("/library", refresh=True)

        self.assertEqual([item.name for item in rows], ["episode.mkv"])
        self.assertEqual(client.session.request.call_count, 3)

    def test_list_dir_exhaustion_raises_transient_error_with_clean_message(self) -> None:
        client = self._client(retries=2)
        client.session.request = Mock(
            side_effect=requests.exceptions.ConnectionError(
                "Connection aborted.",
                ConnectionResetError(104, "Connection reset by peer"),
            )
        )

        with self.assertRaises(OpenListTransientError) as raised:
            client.list_dir("/library", refresh=True)

        self.assertEqual(client.session.request.call_count, 3)
        self.assertIn("已重试 2 次", str(raised.exception))
        self.assertIn("连接被对端重置", str(raised.exception))

    def test_list_dir_retries_busy_json_response_and_recovers(self) -> None:
        client = self._client(retries=2)
        client.session.request = Mock(
            side_effect=[
                _JsonResponse({"success": False, "message": "storage is busy, try again later"}),
                _JsonResponse({"code": 429, "message": "请求过多，请稍后重试"}),
                _JsonResponse({"code": 200, "data": {"content": []}}),
            ]
        )

        self.assertEqual(client.list_dir("/library", refresh=True), [])
        self.assertEqual(client.session.request.call_count, 3)

    def test_list_dir_retries_http_locked_response_and_recovers(self) -> None:
        client = self._client(retries=1)
        client.session.request = Mock(
            side_effect=[
                _JsonResponse({"code": 423, "message": "scan is locked"}, status_code=423),
                _JsonResponse({"code": 200, "data": {"content": []}}),
            ]
        )

        self.assertEqual(client.list_dir("/library", refresh=True), [])
        self.assertEqual(client.session.request.call_count, 2)

    def test_busy_mutating_request_is_classified_but_not_automatically_retried(self) -> None:
        client = self._client(retries=3)
        client.session.request = Mock(
            return_value=_JsonResponse({"code": 500, "message": "系统繁忙，请稍后重试"})
        )

        with self.assertRaises(OpenListTransientError) as raised:
            client.rename("/library/old.mkv", "new.mkv")

        self.assertEqual(client.session.request.call_count, 1)
        self.assertIn("系统繁忙", str(raised.exception))

    def test_mutating_request_is_not_automatically_retried(self) -> None:
        client = self._client(retries=3)
        client.session.request = Mock(side_effect=requests.exceptions.ConnectionError("Connection reset by peer"))

        with self.assertRaises(OpenListTransientError):
            client.rename("/library/old.mkv", "new.mkv")

        self.assertEqual(client.session.request.call_count, 1)

    def test_bulk_endpoints_use_batch_timeout_and_expected_payloads(self) -> None:
        client = OpenListClient(
            {
                "base_url": "http://openlist.test",
                "timeout": 15,
                "batch_timeout": 345,
                "request_retries": 3,
            }
        )
        client.session.request = Mock(return_value=_JsonResponse({"code": 200, "data": None}))

        self.assertTrue(client.batch_rename("/src", [("a.mkv", "b.mkv")]))
        self.assertTrue(client.regex_rename("/src", r"^E([0-9]{2})\.mkv$", "S01E$1.mkv"))
        self.assertTrue(client.move_many("/src", "/dst", ["S01E01.mkv", "S01E02.mkv"]))
        self.assertTrue(client.recursive_move("/src", "/dst"))

        calls = client.session.request.call_args_list
        self.assertEqual([call.args[1] for call in calls], [
            "http://openlist.test/api/fs/batch_rename",
            "http://openlist.test/api/fs/regex_rename",
            "http://openlist.test/api/fs/move",
            "http://openlist.test/api/fs/recursive_move",
        ])
        self.assertTrue(all(call.kwargs["timeout"] == 345 for call in calls))
        self.assertEqual(calls[0].kwargs["json"]["rename_objects"], [{"src_name": "a.mkv", "new_name": "b.mkv"}])
        self.assertEqual(calls[1].kwargs["json"]["src_name_regex"], r"^E([0-9]{2})\.mkv$")
        self.assertEqual(calls[2].kwargs["json"]["names"], ["S01E01.mkv", "S01E02.mkv"])
        self.assertEqual(calls[3].kwargs["json"]["conflict_policy"], "cancel")

    def test_non_bulk_404_remains_regular_openlist_error(self) -> None:
        client = self._client()
        client.session.request = Mock(
            return_value=_JsonResponse({"code": 404, "message": "file not found"}, status_code=404)
        )

        with self.assertRaises(OpenListError) as raised:
            client.rename("/src/a.mkv", "b.mkv")

        self.assertNotIsInstance(raised.exception, OpenListEndpointUnsupported)

    def test_unsupported_bulk_endpoint_is_cached(self) -> None:
        client = self._client()
        client.session.request = Mock(return_value=_JsonResponse({"code": 404, "message": "route not found"}, status_code=404))

        with self.assertRaises(OpenListEndpointUnsupported):
            client.batch_rename("/src", [("a.mkv", "b.mkv")])
        with self.assertRaises(OpenListEndpointUnsupported):
            client.batch_rename("/src", [("a.mkv", "b.mkv")])

        self.assertEqual(client.session.request.call_count, 1)

    def test_spa_html_response_marks_bulk_endpoint_unsupported(self) -> None:
        client = self._client()
        client.session.request = Mock(
            return_value=_JsonResponse(
                None,
                text="<!doctype html><html></html>",
                content_type="text/html; charset=utf-8",
            )
        )

        with self.assertRaises(OpenListEndpointUnsupported):
            client.regex_rename("/src", "^a$", "b")

    def test_get_item_does_not_hide_transient_failure_as_missing(self) -> None:
        client = self._client()
        client.list_dir = Mock(side_effect=OpenListTransientError("OpenList 服务繁忙"))  # type: ignore[method-assign]

        with self.assertRaises(OpenListTransientError):
            client.get_item("/library/episode.mkv")

    def test_recursive_scan_refreshes_parent_and_root_only(self) -> None:
        client = self._client()
        calls: list[tuple[str, bool | None]] = []

        def list_dir(path: str, *, refresh: bool | None = None) -> list[OpenListItem]:
            calls.append((path, refresh))
            if path == "/library":
                return []
            if path == "/library/show":
                return [OpenListItem(name="Season 01", path="/library/show/Season 01", is_dir=True)]
            if path == "/library/show/Season 01":
                return [OpenListItem(name="episode.mkv", path="/library/show/Season 01/episode.mkv", is_dir=False)]
            return []

        client.list_dir = list_dir  # type: ignore[method-assign]

        rows = client.scan_videos("/library/show", refresh=True)

        self.assertEqual([item.name for item in rows], ["episode.mkv"])
        self.assertEqual(
            calls,
            [
                ("/library", True),
                ("/library/show", True),
                ("/library/show/Season 01", False),
            ],
        )

    def test_recursive_scan_includes_tar_episode_archives(self) -> None:
        client = self._client()

        def list_dir(path: str, *, refresh: bool | None = None) -> list[OpenListItem]:
            if path == "/library":
                return []
            if path == "/library/show":
                return [OpenListItem(name="Season 01", path="/library/show/Season 01", is_dir=True)]
            if path == "/library/show/Season 01":
                return [OpenListItem(name="第1集 4K.tar", path="/library/show/Season 01/第1集 4K.tar", is_dir=False)]
            return []

        client.list_dir = list_dir  # type: ignore[method-assign]

        rows = client.scan_videos("/library/show", refresh=True)

        self.assertEqual([item.name for item in rows], ["第1集 4K.tar"])

    def test_expected_name_stops_category_scan_after_target_is_found(self) -> None:
        client = self._client()
        calls: list[str] = []

        def list_dir(path: str, *, refresh: bool | None = None) -> list[OpenListItem]:
            calls.append(path)
            if path == "/library/tv":
                return [
                    OpenListItem(name="A", path="/library/tv/A", is_dir=True),
                    OpenListItem(name="B", path="/library/tv/B", is_dir=True),
                ]
            if path == "/library/tv/A":
                return [OpenListItem(name="target.mkv", path="/library/tv/A/target.mkv", is_dir=False)]
            if path == "/library/tv/B":
                raise AssertionError("target found 后不应继续扫描其它资源目录")
            return []

        client.list_dir = list_dir  # type: ignore[method-assign]

        rows = client.scan_videos("/library/tv", expected_names=["target.mkv"])

        self.assertEqual([item.path for item in rows], ["/library/tv/A/target.mkv"])
        self.assertEqual(calls, ["/library/tv", "/library/tv/A"])

    def test_expected_path_is_read_directly_without_walking_category_root(self) -> None:
        client = self._client()
        calls: list[str] = []

        def list_dir(path: str, *, refresh: bool | None = None) -> list[OpenListItem]:
            calls.append(path)
            if path == "/library/tv/6音轨":
                return [
                    OpenListItem(
                        name="藏海传.S01E01.mkv",
                        path="/library/tv/6音轨/藏海传.S01E01.mkv",
                        is_dir=False,
                    )
                ]
            if path == "/library/tv":
                raise AssertionError("有精确文件路径时不应扫描整个分类根")
            return []

        client.list_dir = list_dir  # type: ignore[method-assign]

        rows = client.scan_videos(
            "/library/tv",
            expected_paths=["/library/tv/6音轨/藏海传.S01E01.mkv"],
        )

        self.assertEqual([item.path for item in rows], ["/library/tv/6音轨/藏海传.S01E01.mkv"])
        self.assertEqual(calls, ["/library/tv/6音轨"])

    def test_missing_expected_path_does_not_fall_back_to_recursive_scan(self) -> None:
        client = self._client()
        calls: list[str] = []

        def list_dir(path: str, *, refresh: bool | None = None) -> list[OpenListItem]:
            calls.append(path)
            return []

        client.list_dir = list_dir  # type: ignore[method-assign]

        rows = client.scan_videos(
            "/library/tv",
            expected_paths=["/library/tv/藏海传/藏海传.S01E01.mkv"],
        )

        self.assertEqual(rows, [])
        self.assertEqual(calls, ["/library/tv/藏海传"])


class Cloud139ScanFilterTests(unittest.TestCase):
    def test_selected_video_names_are_forwarded_to_organizer_scan(self) -> None:
        filters = _cloud139_scan_filters_from_job(
            {
                "raw_data": {
                    "selection": {
                        "selected_files": [
                            {"name": "show.S01E13.mkv"},
                            {"name": "show.S01E13.ass"},
                        ]
                    }
                }
            }
        )

        self.assertEqual(filters["expected_names"], ["show.S01E13.mkv"])
        self.assertEqual(filters["expected_paths"], [])


class OrganizerTransientScanTests(unittest.TestCase):
    def test_busy_and_locked_messages_are_retryable_visibility_errors(self) -> None:
        for message in (
            "OpenList 返回失败：storage is busy",
            "OpenList 返回失败：scan is locked",
            "OpenList 返回失败：too many requests",
            "OpenList 返回失败：系统繁忙，请稍后重试",
            "OpenList HTTP 423",
            "failed get dir: context deadline exceeded",
        ):
            with self.subTest(message=message):
                self.assertTrue(OrganizerService._retryable_openlist_visibility_error(OpenListError(message)))

    def test_release_title_slash_is_not_treated_as_a_directory_separator(self) -> None:
        raw_title = "藏海传 4K 国语/粤语 6音轨"

        self.assertEqual(_clean_title_hint(raw_title), "藏海传")
        self.assertEqual(
            _title_from_update_payload({}, {"title": raw_title}, "/移动云/电视剧"),
            "藏海传",
        )

    def test_update_progress_title_keeps_title_before_year(self) -> None:
        self.assertEqual(_clean_title_hint("百花杀（2026）更新至第31集 4K"), "百花杀")

    def test_retry_repairs_old_metadata_only_task_title_from_linked_job(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.updates: list[dict] = []

            def get_job(self, _job_id: int) -> dict:
                return {"id": 9, "title": "藏海传 4K 国语/粤语 6音轨"}

            def update_organizer_task(self, _task_id: int, **updates) -> None:
                self.updates.append(updates)

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        task = {
            "id": 17,
            "job_id": 9,
            "title": "6音轨",
            "source_keyword": "6音轨",
            "openlist_root_path": "/移动云/电视剧",
            "raw_data": {},
            "evidence": {},
        }

        repaired = service._repair_task_title_from_linked_job(task)

        self.assertEqual(repaired["title"], "藏海传")
        self.assertEqual(repaired["source_keyword"], "藏海传")
        self.assertEqual(service.db.updates[0]["title"], "藏海传")

    def test_title_repair_database_failure_does_not_block_retry_scan(self) -> None:
        task = {
            "id": 17,
            "job_id": 9,
            "title": "6音轨",
            "source_keyword": "6音轨",
            "openlist_root_path": "/移动云/电视剧",
            "raw_data": {},
        }

        for database in (
            SimpleNamespace(get_job=Mock(side_effect=RuntimeError("read failed"))),
            SimpleNamespace(
                get_job=Mock(return_value={"id": 9, "title": "藏海传 4K 国语/粤语 6音轨"}),
                update_organizer_task=Mock(side_effect=RuntimeError("write failed")),
            ),
        ):
            with self.subTest(database=database):
                service = OrganizerService.__new__(OrganizerService)
                service.db = database

                with self.assertLogs("fnos_media_import.organizer.service", level="WARNING"):
                    self.assertIs(service._repair_task_title_from_linked_job(task), task)

    def test_automatic_category_root_scan_without_file_evidence_is_rejected(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {"tv": {"label": "电视剧", "openlist_root_path": "/移动云/电视剧"}}
        service.organizer_config = {"max_scan_depth": 8, "max_files_per_task": 500}
        service.openlist = SimpleNamespace(scan_videos=Mock())
        task = {
            "category": "tv",
            "trigger_type": "rclone_category_done",
            "openlist_root_path": "/移动云/电视剧",
            "raw_data": {},
        }

        with self.assertRaises(OpenListError) as raised:
            service._scan_openlist_videos(task, "/移动云/电视剧")

        self.assertIn("拒绝递归扫描整个分类目录", str(raised.exception))
        service.openlist.scan_videos.assert_not_called()

    def test_automatic_category_root_scan_uses_exact_paths_and_zero_depth(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.categories = {"tv": {"label": "电视剧", "openlist_root_path": "/移动云/电视剧"}}
        service.organizer_config = {"max_scan_depth": 8, "max_files_per_task": 500}
        service.openlist = SimpleNamespace(scan_videos=Mock(return_value=[]))
        task = {
            "category": "tv",
            "trigger_type": "rclone_category_done",
            "openlist_root_path": "/移动云/电视剧",
            "raw_data": {
                "scan_filters": {
                    "expected_names": ["藏海传.S01E01.mkv"],
                    "expected_paths": ["/移动云/电视剧/6音轨/藏海传.S01E01.mkv"],
                }
            },
        }

        service._scan_openlist_videos(task, "/移动云/电视剧")

        service.openlist.scan_videos.assert_called_once_with(
            "/移动云/电视剧",
            max_depth=0,
            max_files=500,
            refresh=True,
            expected_names=["藏海传.S01E01.mkv"],
            expected_paths=["/移动云/电视剧/6音轨/藏海传.S01E01.mkv"],
        )

    def test_stale_timer_does_not_rerun_an_already_failed_task(self) -> None:
        service = OrganizerService.__new__(OrganizerService)
        service.db = SimpleNamespace(
            get_organizer_task=lambda _task_id, include_children=False: {"id": 17, "status": "failed"}
        )
        service.process_task = Mock()

        service._process_task_safely(17)

        service.process_task.assert_not_called()

    def test_background_task_waits_instead_of_failing_on_connection_reset(self) -> None:
        task = {
            "id": 17,
            "job_id": None,
            "trigger_type": "rclone_callback",
            "category": "tv",
            "openlist_root_path": "/mobile/tv",
            "status": "stabilizing",
            "raw_data": {
                "rclone": {},
                "scan_filters": {"expected_names": ["target.mkv"]},
            },
        }

        class FakeDatabase:
            def __init__(self) -> None:
                self.updates: list[tuple[int, dict]] = []

            def get_organizer_task(self, _task_id: int, *, include_children: bool = False) -> dict:
                return dict(task)

            def update_organizer_task(self, task_id: int, **updates) -> None:
                self.updates.append((task_id, updates))

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.categories = {"tv": {"label": "TV", "openlist_root_path": "/mobile/tv"}}
        service.organizer_config = {"openlist_visible_retry_delays_seconds": [1, 2, 3]}
        service.openlist = SimpleNamespace(
            scan_videos=Mock(side_effect=OpenListTransientError("OpenList 请求失败；连接被对端重置"))
        )
        service._sync_linked_job = Mock()
        service._schedule_task_after = Mock()

        result = service.process_task(17)

        self.assertTrue(result["success"])
        self.assertTrue(result["waiting_openlist"])
        self.assertEqual(result["status"], "waiting_openlist")
        self.assertTrue(
            any(updates.get("status") == "waiting_openlist" for _task_id, updates in service.db.updates)
        )
        self.assertFalse(any(updates.get("status") == "failed" for _task_id, updates in service.db.updates))
        service._schedule_task_after.assert_called_once_with(17, 1)

    def test_suspended_process_does_not_leave_false_waiting_openlist_state(self) -> None:
        task = {
            "id": 18,
            "trigger_type": "rclone_callback",
            "category": "tv",
            "openlist_root_path": "/mobile/tv",
            "raw_data": {"rclone": {}},
        }

        class FakeDatabase:
            def __init__(self) -> None:
                self.updates: list[tuple[int, dict]] = []

            def update_organizer_task(self, task_id: int, **updates) -> None:
                self.updates.append((task_id, updates))

        service = OrganizerService.__new__(OrganizerService)
        service.db = FakeDatabase()
        service.organizer_config = {"openlist_visible_retry_delays_seconds": [1]}
        service._background_suspended = True
        service._timers = {}
        service._sync_linked_job = Mock()

        result = service._schedule_openlist_visibility_retry(
            task,
            root_path="/mobile/tv",
            error=OpenListTransientError("OpenList 服务繁忙"),
        )

        self.assertFalse(result["success"])
        self.assertFalse(result["waiting_openlist"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(service.db.updates[-1][1]["status"], "failed")
        self.assertTrue(
            service.db.updates[-1][1]["raw_data"]["openlist_visibility_retry"]["schedule_failed"]
        )


if __name__ == "__main__":
    unittest.main()
