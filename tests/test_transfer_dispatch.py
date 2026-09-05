from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import unittest

from contextlib import contextmanager

from fnos_media_import.transfer.dispatch import (
    TransferDispatchError,
    TransferDispatchService,
    _OrganizerAdapter,
    _plan_scan_filters,
)
from fnos_media_import.transfer import TransferPlan
from fnos_media_import.transfer.provider_relay_executor import ProviderRelayExecutor


class _Adapter(RepositoryFakeMixin):
    def __init__(self, *, supports_relay: bool = False) -> None:
        self.calls: list[tuple[str, int]] = []
        self.supports_relay = supports_relay

    def capabilities(self):
        return {"supports_relay_upload": self.supports_relay}

    def relay_uploader_for(self, _account_id: str, _revision: int):
        self.calls.append((_account_id, _revision))
        return object()


class _Registry(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.adapters = {
            "quark": _Adapter(),
            "mobile": _Adapter(supports_relay=True),
            "tianyi": _Adapter(supports_relay=True),
        }

    def get(self, kind: str):
        return self.adapters[kind]


class _OrganizerDatabase:
    class _Jobs:
        @staticmethod
        def get(job_id: int):
            return {"id": job_id, "status": "organizing"}

    job_queries = _Jobs()

    @contextmanager
    def connect(self):
        class _Connection:
            @staticmethod
            def execute(*_args):
                class _Result:
                    @staticmethod
                    def fetchone():
                        return {
                            "id": 7,
                            "status": "waiting_review",
                            "error_message": "需要人工确认",
                        }

                return _Result()

        yield _Connection()


class _ProgressDatabase:
    class _JobCommands:
        def __init__(self) -> None:
            self.events: list[tuple] = []

        def add_event(self, job_id, level, message, raw_data=None):
            self.events.append((job_id, level, message, raw_data))

    def __init__(self) -> None:
        self.job_commands = self._JobCommands()


class _BrokenProgressDatabase:
    class _JobCommands:
        @staticmethod
        def add_event(*_args, **_kwargs):
            raise RuntimeError("事件写入失败")

    job_commands = _JobCommands()


def _plan(strategy: str) -> dict:
    return {
        "version": 1,
        "job_id": 1,
        "source": {"provider_kind": "quark", "source_form": "share"},
        "destination": {
            "id": "mobile_main",
            "provider_kind": "mobile",
            "account_id": "acc_mobile",
            "account_revision": 1,
        },
        "transfer": {"strategy": strategy, "idempotency_key": "v1:key"},
        "paths": {},
    }


def _native_plan_with_selection(organizer_root: str, selection: dict) -> dict:
    plan = _plan("native_save")
    plan["source"]["provider_kind"] = "mobile"
    plan["source"]["selection"] = selection
    plan["paths"] = {
        "organizer": {"namespace_id": "openlist:conn1:mnt1", "path": organizer_root, "kind": "openlist"},
        "destination_final": {
            "kind": "native",
            "namespace_id": "native:mobile:mobile_lib",
            "path": "/电影",
        },
    }
    return plan


class TransferDispatchTests(unittest.TestCase):
    def _service(self) -> TransferDispatchService:
        return TransferDispatchService(
            database=object(),
            accounts_service=object(),
            target_service=object(),
            mount_service=object(),
            registry=_Registry(),
            rclone_service=object(),
            locator_resolver=object(),
        )

    def test_builds_native_executor(self) -> None:
        self.assertIn("native_save", self._service().build_executors(_plan("native_save")))

    def test_builds_rclone_executor(self) -> None:
        plan = _plan("rclone_copy")
        plan["destination"]["provider_kind"] = "quark"
        self.assertIn("rclone_copy", self._service().build_executors(plan))

    def test_builds_provider_relay_executor_from_frozen_account(self) -> None:
        service = self._service()
        executors = service.build_executors(_plan("relay_upload_provider"))
        self.assertIsInstance(executors["relay_upload_provider"], ProviderRelayExecutor)
        self.assertEqual(service.registry.adapters["mobile"].calls, [("acc_mobile", 1)])

    def test_builds_tianyi_provider_relay_from_frozen_account(self) -> None:
        service = self._service()
        plan = _plan("relay_upload_provider")
        plan["destination"].update(
            {
                "id": "tianyi_main",
                "provider_kind": "tianyi",
                "account_id": "acc_tianyi",
                "account_revision": 7,
            }
        )

        executors = service.build_executors(plan)

        self.assertIsInstance(executors["relay_upload_provider"], ProviderRelayExecutor)
        self.assertEqual(service.registry.adapters["tianyi"].calls, [("acc_tianyi", 7)])

    def test_relay_executor_progress_routes_to_upload_log_bridge(self) -> None:
        bridged: list[tuple[int, str]] = []
        service = TransferDispatchService(
            database=object(),
            accounts_service=object(),
            target_service=object(),
            mount_service=object(),
            registry=_Registry(),
            rclone_service=object(),
            locator_resolver=object(),
            upload_log_bridge=lambda job_id, message: bridged.append((job_id, message)),
        )
        executors = service.build_executors(_plan("relay_upload_provider"))
        sink = executors["relay_upload_provider"].progress_sink
        self.assertIsNotNone(sink)

        sink("上传中 电影.mkv：3.04 GiB / 6.74 GiB，45%")

        self.assertEqual(bridged, [(1, "上传中 电影.mkv：3.04 GiB / 6.74 GiB，45%")])

    def test_relay_executor_progress_survives_broken_log_bridge(self) -> None:
        def broken_bridge(_job_id: int, _message: str) -> None:
            raise RuntimeError("Worker 不可达")

        service = TransferDispatchService(
            database=object(),
            accounts_service=object(),
            target_service=object(),
            mount_service=object(),
            registry=_Registry(),
            rclone_service=object(),
            locator_resolver=object(),
            upload_log_bridge=broken_bridge,
        )
        executors = service.build_executors(_plan("relay_upload_provider"))
        sink = executors["relay_upload_provider"].progress_sink
        self.assertIsNotNone(sink)
        sink("上传中：50%")  # 桥接故障不得影响上传主流程

    def test_relay_executor_progress_sink_absent_without_bridge(self) -> None:
        service = self._service()
        executors = service.build_executors(_plan("relay_upload_provider"))
        self.assertIsNone(executors["relay_upload_provider"].progress_sink)

    def test_relay_executor_event_sink_writes_job_events(self) -> None:
        database = _ProgressDatabase()
        service = TransferDispatchService(
            database=database,
            accounts_service=object(),
            target_service=object(),
            mount_service=object(),
            registry=_Registry(),
            rclone_service=object(),
            locator_resolver=object(),
        )
        executors = service.build_executors(_plan("relay_upload_provider"))
        sink = executors["relay_upload_provider"].event_sink
        self.assertIsNotNone(sink)

        sink("info", "开始上传到目标网盘")
        sink("info", "上传完成，准备整理")

        self.assertEqual(
            database.job_commands.events,
            [
                (1, "info", "开始上传到目标网盘", None),
                (1, "info", "上传完成，准备整理", None),
            ],
        )

    def test_relay_executor_event_sink_swallows_write_failures(self) -> None:
        service = TransferDispatchService(
            database=_BrokenProgressDatabase(),
            accounts_service=object(),
            target_service=object(),
            mount_service=object(),
            registry=_Registry(),
            rclone_service=object(),
            locator_resolver=object(),
        )
        executors = service.build_executors(_plan("relay_upload_provider"))
        sink = executors["relay_upload_provider"].event_sink
        self.assertIsNotNone(sink)
        sink("error", "上传失败：认证已过期")  # 事件写入异常不得影响上传主流程

    def test_unknown_strategy_fails_closed(self) -> None:
        with self.assertRaises(TransferDispatchError):
            self._service().build_executors(_plan("manual_review"))

    def test_hot_reload_replaces_organizer_adapter(self) -> None:
        service = self._service()
        organizer = object()

        service.set_organizer_service(organizer)

        self.assertIs(service.organizer_service, organizer)
        self.assertIs(service.organizer.service, organizer)

    def test_organizer_waiting_review_is_not_treated_as_pending(self) -> None:
        adapter = _OrganizerAdapter(_OrganizerDatabase(), object())
        plan = TransferPlan.from_dict(
            {
                "version": 1,
                "job_id": 1,
                "source": {"provider_kind": "quark", "source_form": "share"},
                "destination": {"provider_kind": "quark"},
                "transfer": {"strategy": "native_save", "idempotency_key": "test"},
                "paths": {
                    "organizer": {"path": "/organizer/job-1"},
                    "destination_final": {
                        "kind": "native",
                        "namespace_id": "native:quark:quark_lib",
                        "path": "/电影",
                    },
                },
            }
        )

        result = adapter.organize(plan, organizer_task_id=7)

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.task_id, 7)


class PlanScanFiltersTests(unittest.TestCase):
    def test_category_root_plan_with_file_selection_builds_manifest(self) -> None:
        plan = TransferPlan.from_dict(
            _native_plan_with_selection(
                "/移动云/电影",
                {
                    "provider_kind": "mobile",
                    "selected_items": [
                        {"name": "movie1.mkv", "is_dir": False},
                        {"name": "poster.jpg", "is_dir": False},
                    ],
                },
            )
        )

        filters = _plan_scan_filters(plan)

        self.assertEqual(
            filters,
            {
                "expected_names": ["movie1.mkv", "poster.jpg"],
                "expected_paths": ["/移动云/电影/movie1.mkv", "/移动云/电影/poster.jpg"],
                "expected_count": 2,
            },
        )

    def test_manifest_from_provider_selection_when_items_empty(self) -> None:
        plan = TransferPlan.from_dict(
            _native_plan_with_selection(
                "/天翼/电视剧",
                {
                    "provider_kind": "tianyi",
                    "selected_items": [],
                    "provider_selection": {
                        "tianyi_selection": {
                            "mode": "items",
                            "selected_files": [
                                {"fid": "f1", "name": "ep01.mp4", "is_dir": False},
                                {"fid": "d1", "name": "Season 01", "is_dir": True},
                            ],
                        }
                    },
                },
            )
        )

        filters = _plan_scan_filters(plan)

        self.assertEqual(filters["expected_names"], ["ep01.mp4"])
        self.assertEqual(filters["expected_paths"], ["/天翼/电视剧/ep01.mp4"])
        self.assertEqual(filters["expected_count"], 1)

    def test_job_scoped_organizer_root_gets_no_manifest(self) -> None:
        plan = TransferPlan.from_dict(
            _native_plan_with_selection(
                "/rclone/landing/job-1",
                {
                    "provider_kind": "mobile",
                    "selected_items": [{"name": "movie1.mkv", "is_dir": False}],
                },
            )
        )

        self.assertEqual(_plan_scan_filters(plan), {})

    def test_folder_only_selection_or_no_selection_gets_no_manifest(self) -> None:
        folder_plan = TransferPlan.from_dict(
            _native_plan_with_selection(
                "/移动云/电影",
                {"provider_kind": "mobile", "selected_items": [{"name": "剧集", "is_dir": True}]},
            )
        )
        empty_plan = TransferPlan.from_dict(
            _native_plan_with_selection("/移动云/电影", {"provider_kind": "mobile"})
        )

        self.assertEqual(_plan_scan_filters(folder_plan), {})
        self.assertEqual(_plan_scan_filters(empty_plan), {})

    def test_folder_selection_falls_back_to_rclone_staging_manifest(self) -> None:
        folder_plan = TransferPlan.from_dict(
            _native_plan_with_selection(
                "/移动云/电影",
                {"provider_kind": "mobile", "selected_items": [{"name": "爷们娘们走个面。", "is_dir": True}]},
            )
        )
        job = {
            "id": 1,
            "raw_data": {
                "rclone_staging_manifest": {
                    "version": 1,
                    "job_root": "/managed-openlist/_入库暂存/job-1",
                    "source_paths": [
                        "/managed-openlist/_入库暂存/job-1/爷们娘们走个面。/2026.2160p.WEB-DL.H.265.HDR.DDP5.1.2Audios-PandaQT.mkv",
                    ],
                }
            },
        }

        filters = _plan_scan_filters(folder_plan, job)

        self.assertEqual(
            filters,
            {
                "expected_paths": [
                    "/移动云/电影/爷们娘们走个面。/2026.2160p.WEB-DL.H.265.HDR.DDP5.1.2Audios-PandaQT.mkv",
                ],
                "expected_count": 1,
            },
        )

    def test_manifest_fallback_without_job_root_keeps_relative_paths(self) -> None:
        folder_plan = TransferPlan.from_dict(
            _native_plan_with_selection(
                "/移动云/电影",
                {"provider_kind": "mobile", "selected_items": []},
            )
        )
        job = {
            "id": 2,
            "raw_data": {
                "rclone_staging_manifest": {
                    "source_paths": ["爷们娘们走个面。/movie.mkv"],
                }
            },
        }

        filters = _plan_scan_filters(folder_plan, job)

        self.assertEqual(
            filters,
            {"expected_paths": ["/移动云/电影/爷们娘们走个面。/movie.mkv"], "expected_count": 1},
        )

    def test_manifest_fallback_with_empty_manifest_returns_no_filters(self) -> None:
        folder_plan = TransferPlan.from_dict(
            _native_plan_with_selection(
                "/移动云/电影",
                {"provider_kind": "mobile", "selected_items": [{"name": "剧集", "is_dir": True}]},
            )
        )

        self.assertEqual(_plan_scan_filters(folder_plan, {"id": 3}), {})
        self.assertEqual(
            _plan_scan_filters(
                folder_plan, {"id": 4, "raw_data": {"rclone_staging_manifest": {"source_paths": []}}}
            ),
            {},
        )

    def test_organizer_adapter_passes_scan_filters_to_enqueue_payload(self) -> None:
        class _Database:
            class _Jobs:
                @staticmethod
                def get(job_id: int):
                    return {"id": job_id, "status": "organizing", "category_label": "电影"}

            job_queries = _Jobs()

            @contextmanager
            def connect(self):
                class _Connection:
                    @staticmethod
                    def execute(*_args):
                        class _Result:
                            @staticmethod
                            def fetchone():
                                return None

                        return _Result()

                yield _Connection()

        class _Service:
            def __init__(self) -> None:
                self.payload: dict | None = None

            def enqueue_from_completed_directory(self, *, job, category_label, root_path, payload, trigger_type):
                self.payload = payload
                return {"success": True, "queued": True, "task_id": 9}

        service = _Service()
        adapter = _OrganizerAdapter(_Database(), service)
        plan = TransferPlan.from_dict(
            _native_plan_with_selection(
                "/移动云/电影",
                {
                    "provider_kind": "mobile",
                    "selected_items": [{"name": "movie1.mkv", "is_dir": False}],
                },
            )
        )

        result = adapter.organize(plan)

        self.assertEqual(result.status, "pending")
        self.assertEqual(result.task_id, 9)
        self.assertEqual(service.payload.get("v1_transfer"), True)
        filters = service.payload.get("scan_filters")
        self.assertEqual(filters.get("expected_names"), ["movie1.mkv"])
        self.assertEqual(filters.get("expected_paths"), ["/移动云/电影/movie1.mkv"])



if __name__ == "__main__":
    unittest.main()
