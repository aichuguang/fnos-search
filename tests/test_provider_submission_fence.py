from __future__ import annotations

import copy
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.constants import (
    JOB_DONE,
    JOB_FAILED,
    JOB_ORGANIZING,
    JOB_CREATED,
    JOB_PROVIDER_SUBMITTING,
    JOB_SUBMITTED,
    JOB_WAITING_TRANSFER,
    ROUTE_QUARK_TO_MOBILE,
)
from fnos_media_import.database import Database
from fnos_media_import.importers.base import ImportResult
from fnos_media_import.services.import_job_service import (
    PROVIDER_SUBMISSION_FENCE_KEY,
    ImportJobCreationService,
    ImportJobRetryService,
)
from fnos_media_import.services.import_service import ImportService
from fnos_media_import.services.job_cancellation_service import (
    JobCancellationDependencies,
    JobCancellationService,
)
from fnos_media_import.services.public_import_job_coordinator import PublicImportJobCoordinator


class _Config:
    raw = {
        "routes": {},
        "organizer": {"enabled": False, "staging_enabled": False},
        "openlist": {},
    }

    @staticmethod
    def category(_key: str) -> dict:
        return {"label": "电视剧", "quark_save_path": "/离线下载/电视剧"}


class _QuarkLink:
    route = ROUTE_QUARK_TO_MOBILE
    source_type = "quark"
    url = "https://pan.quark.cn/s/provider-fence"
    password = ""
    supported = True

    @staticmethod
    def to_dict() -> dict:
        return {"route": ROUTE_QUARK_TO_MOBILE, "source_type": "quark"}


def _not_started_fence() -> dict:
    return {
        PROVIDER_SUBMISSION_FENCE_KEY: {
            "version": 1,
            "state": "not_started",
            "attempt": 0,
        }
    }


def _submitting_fence() -> dict:
    return {
        PROVIDER_SUBMISSION_FENCE_KEY: {
            "version": 1,
            "state": "submitting",
            "attempt": 1,
        }
    }


class ProviderSubmissionDatabaseFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"provider-submission-fence-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()

    def _creation_service(self, submit_quark) -> ImportJobCreationService:
        return ImportJobCreationService(
            database=self.database,
            config=_Config(),
            detect_link=lambda *_args, **_kwargs: _QuarkLink(),
            job_source_url=lambda url, _payload: url,
            target_path=lambda *_args, **_kwargs: "/离线下载/电视剧",
            staging_plan=None,
            submit_quark=submit_quark,
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )

    def test_new_job_is_atomically_claimed_before_provider_callback(self) -> None:
        observed: dict = {}

        def submit(job_id: int, *_args, **_kwargs) -> dict:
            observed.update(self.database.get_job(job_id) or {})
            return {"success": True, "job": observed}

        result = self._creation_service(submit).create(
            {
                "title": "提交栅栏测试",
                "url": _QuarkLink.url,
                "category": "tv",
                "idempotency_key": f"provider-fence:{uuid.uuid4().hex}",
            }
        )

        self.assertTrue(result["success"])
        self.assertEqual(observed["status"], JOB_PROVIDER_SUBMITTING)
        fence = observed["raw_data"][PROVIDER_SUBMISSION_FENCE_KEY]
        self.assertEqual(fence["state"], "submitting")
        self.assertEqual(fence["attempt"], 1)

    def test_crash_after_claim_leaves_ambiguous_state_and_idempotent_create_does_not_resubmit(self) -> None:
        calls: list[int] = []

        def crashing_submit(job_id: int, *_args, **_kwargs) -> dict:
            calls.append(job_id)
            raise RuntimeError("simulated process crash window")

        service = self._creation_service(crashing_submit)
        payload = {
            "title": "崩溃恢复测试",
            "url": _QuarkLink.url,
            "category": "tv",
            "idempotency_key": f"provider-fence:{uuid.uuid4().hex}",
        }

        with self.assertRaisesRegex(RuntimeError, "simulated"):
            service.create(payload)
        replay = service.create(payload)

        self.assertEqual(len(calls), 1)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["job"]["status"], JOB_PROVIDER_SUBMITTING)

    def test_historical_created_without_fence_is_never_retried(self) -> None:
        job_id, created = self.database.create_job(
            {
                "title": "历史任务",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": _QuarkLink.url,
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/离线下载/电视剧",
                "status": JOB_CREATED,
                "raw_data": {"request": {"url": _QuarkLink.url}},
                "idempotency_key": f"legacy-created:{uuid.uuid4().hex}",
            }
        )
        self.assertTrue(created)
        provider_calls: list[int] = []
        retry = ImportJobRetryService(
            database=self.database,
            config=_Config(),
            submit_quark=lambda job_id, *_args, **_kwargs: provider_calls.append(job_id) or {},
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )

        result = retry.retry(job_id)

        self.assertEqual(provider_calls, [])
        self.assertTrue(result["manual_review"])
        self.assertTrue(result["provider_submission_ambiguous"])
        self.assertEqual(self.database.get_job(job_id)["status"], JOB_CREATED)

    def test_post_submission_statuses_never_call_provider_again(self) -> None:
        provider_calls: list[int] = []
        retry = ImportJobRetryService(
            database=self.database,
            config=_Config(),
            submit_quark=lambda job_id, *_args, **_kwargs: provider_calls.append(job_id) or {},
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )
        for status in (JOB_SUBMITTED, JOB_WAITING_TRANSFER, JOB_ORGANIZING, JOB_DONE):
            job_id, created = self.database.create_job(
                {
                    "title": f"post submission {status}",
                    "category": "tv",
                    "category_label": "电视剧",
                    "source_type": "quark",
                    "source_url": f"{_QuarkLink.url}/{status}",
                    "target_route": ROUTE_QUARK_TO_MOBILE,
                    "target_path": "/离线下载/电视剧",
                    "status": status,
                    "raw_data": {"request": {"url": _QuarkLink.url}},
                    "idempotency_key": f"post-submission:{status}:{uuid.uuid4().hex}",
                }
            )
            self.assertTrue(created)

            result = retry.retry(job_id)

            self.assertFalse(result["success"])
            self.assertTrue(result["provider_retry_blocked"])
        self.assertEqual(provider_calls, [])

    def test_successful_provider_fence_blocks_retry_after_later_failure(self) -> None:
        job_id, created = self.database.create_job(
            {
                "title": "provider succeeded then organizer failed",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": _QuarkLink.url,
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/离线下载/电视剧",
                "status": JOB_FAILED,
                "raw_data": {
                    "request": {"url": _QuarkLink.url},
                    PROVIDER_SUBMISSION_FENCE_KEY: {
                        "version": 1,
                        "state": "finished",
                        "attempt": 1,
                        "success": True,
                    },
                },
                "idempotency_key": f"provider-succeeded-later-failed:{uuid.uuid4().hex}",
            }
        )
        self.assertTrue(created)
        provider_calls: list[int] = []
        retry = ImportJobRetryService(
            database=self.database,
            config=_Config(),
            submit_quark=lambda job_id, *_args, **_kwargs: provider_calls.append(job_id) or {},
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )

        result = retry.retry(job_id)

        self.assertFalse(result["success"])
        self.assertTrue(result["provider_completed"])
        self.assertEqual(provider_calls, [])

    def test_failed_status_with_unfinished_submission_fence_is_still_ambiguous(self) -> None:
        job_id, created = self.database.create_job(
            {
                "title": "provider result unknown but status was changed",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": _QuarkLink.url,
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/离线下载/电视剧",
                "status": JOB_FAILED,
                "raw_data": {
                    "request": {"url": _QuarkLink.url},
                    **_submitting_fence(),
                },
                "idempotency_key": f"provider-ambiguous-failed:{uuid.uuid4().hex}",
            }
        )
        self.assertTrue(created)
        provider_calls: list[int] = []
        retry = ImportJobRetryService(
            database=self.database,
            config=_Config(),
            submit_quark=lambda current_job_id, *_args, **_kwargs: provider_calls.append(current_job_id) or {},
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )

        result = retry.retry(job_id)

        self.assertFalse(result["success"])
        self.assertTrue(result["provider_submission_ambiguous"])
        self.assertEqual(provider_calls, [])
        self.assertEqual(self.database.get_job(job_id)["status"], JOB_FAILED)

    def test_confirmed_provider_failure_remains_retryable(self) -> None:
        job_id, created = self.database.create_job(
            {
                "title": "provider failure retry",
                "category": "tv",
                "category_label": "电视剧",
                "source_type": "quark",
                "source_url": _QuarkLink.url,
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/离线下载/电视剧",
                "status": JOB_FAILED,
                "raw_data": {
                    "request": {"url": _QuarkLink.url},
                    PROVIDER_SUBMISSION_FENCE_KEY: {
                        "version": 1,
                        "state": "finished",
                        "attempt": 1,
                        "success": False,
                    },
                },
                "idempotency_key": f"provider-failed-retry:{uuid.uuid4().hex}",
            }
        )
        self.assertTrue(created)
        provider_calls: list[int] = []
        retry = ImportJobRetryService(
            database=self.database,
            config=_Config(),
            submit_quark=lambda current_job_id, *_args, **_kwargs: provider_calls.append(current_job_id)
            or {"success": True},
            submit_cloud139=lambda *_args, **_kwargs: {},
            submit_generic=lambda *_args, **_kwargs: {},
        )

        result = retry.retry(job_id)

        self.assertTrue(result["success"])
        self.assertEqual(provider_calls, [job_id])
        self.assertEqual(self.database.get_job(job_id)["status"], JOB_PROVIDER_SUBMITTING)


class _RaceDatabase:
    def __init__(self) -> None:
        self.job = {
            "id": 42,
            "status": JOB_PROVIDER_SUBMITTING,
            "raw_data": _submitting_fence(),
        }
        self.events: list[tuple] = []

    def get_job(self, _job_id: int) -> dict:
        return copy.deepcopy(self.job)

    def update_job_if_status(self, _job_id: int, expected_statuses, **updates) -> bool:
        if self.job.get("status") not in set(expected_statuses):
            return False
        self.job.update(copy.deepcopy(updates))
        return True

    def add_event(self, *args) -> None:
        self.events.append(args)


class ProviderResultAndCancellationFenceTests(unittest.TestCase):
    def test_provider_result_finishes_fence_with_atomic_status_write(self) -> None:
        database = _RaceDatabase()

        class Quark:
            @staticmethod
            def import_resource(**_kwargs) -> ImportResult:
                return ImportResult(
                    True,
                    JOB_SUBMITTED,
                    "网盘已受理",
                    external_task_id="external-42",
                    target_path="/离线下载/电视剧/job-42",
                    raw_data={"provider": "quark"},
                )

        service = ImportService.__new__(ImportService)
        service.db = database
        service.quark = Quark()

        result = service._submit_quark_job(
            42,
            "测试剧",
            _QuarkLink.url,
            "/离线下载/电视剧/job-42",
            "tv",
            {"label": "电视剧"},
        )

        self.assertTrue(result["success"])
        self.assertEqual(database.job["status"], JOB_SUBMITTED)
        self.assertEqual(database.job["external_task_id"], "external-42")
        fence = database.job["raw_data"][PROVIDER_SUBMISSION_FENCE_KEY]
        self.assertEqual(fence["state"], "finished")
        self.assertTrue(fence["success"])

    def test_provider_result_cas_cannot_overwrite_concurrent_cancelled_state(self) -> None:
        database = _RaceDatabase()

        class Quark:
            @staticmethod
            def import_resource(**_kwargs) -> ImportResult:
                database.job["status"] = "cancelled"
                return ImportResult(
                    True,
                    JOB_SUBMITTED,
                    "网盘已受理",
                    external_task_id="external-42",
                    target_path="/离线下载/电视剧/job-42",
                    raw_data={"provider": "quark"},
                )

        service = ImportService.__new__(ImportService)
        service.db = database
        service.quark = Quark()

        result = service._submit_quark_job(
            42,
            "测试剧",
            _QuarkLink.url,
            "/离线下载/电视剧/job-42",
            "tv",
            {"label": "电视剧"},
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["provider_write_conflict"])
        self.assertEqual(result["external_task_id"], "external-42")
        self.assertEqual(database.job["status"], "cancelled")

    def test_provider_submitting_cancel_is_retryable_and_has_no_side_effects(self) -> None:
        database = _RaceDatabase()
        cas_calls: list[tuple] = []
        original_cas = database.update_job_if_status

        def recording_cas(*args, **kwargs):
            cas_calls.append((args, kwargs))
            return original_cas(*args, **kwargs)

        database.update_job_if_status = recording_cas

        class Cleaner:
            @staticmethod
            def cleanup_cancelled_task(**_kwargs):
                raise AssertionError("提交中拒绝取消时不得清理文件")

            @staticmethod
            def cancel_job(*_args, **_kwargs):
                raise AssertionError("提交中拒绝取消时不得撤销 rclone")

        service = JobCancellationService(
            JobCancellationDependencies(
                jobs=database,
                cleaner=Cleaner(),
                merge_raw_data=lambda current, patch: {**(current or {}), **patch},
                payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
                cancelled_status="cancelled",
            )
        )

        result = service.cancel(
            database.get_job(42),
            reason="用户取消",
            payload={"cleanup": True},
            request_item=None,
            cleanup_default=True,
            stop_running_default=True,
            admin_username="admin",
        )

        self.assertFalse(result["cancelled"])
        self.assertTrue(result["retryable"])
        self.assertEqual(cas_calls, [])
        self.assertEqual(database.job["status"], JOB_PROVIDER_SUBMITTING)

    def test_safe_not_started_created_job_can_still_be_cancelled(self) -> None:
        database = _RaceDatabase()
        database.job.update(status=JOB_CREATED, raw_data=_not_started_fence())

        class Cleaner:
            @staticmethod
            def cancel_job(*_args, **_kwargs):
                return {"success": True}

            @staticmethod
            def cleanup_cancelled_task(**_kwargs):
                return {
                    "success": True,
                    "message": "已清理",
                    "items": [],
                    "warnings": [],
                    "errors": [],
                }

        service = JobCancellationService(
            JobCancellationDependencies(
                jobs=database,
                cleaner=Cleaner(),
                merge_raw_data=lambda current, patch: {**(current or {}), **patch},
                payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
                cancelled_status="cancelled",
            )
        )

        result = service.cancel(
            database.get_job(42),
            reason="安全取消",
            payload={"cleanup": False},
            request_item=None,
            cleanup_default=False,
            stop_running_default=True,
            admin_username="admin",
        )

        self.assertTrue(result["cancelled"])
        self.assertEqual(database.job["status"], "cancelled")


class ProviderSubmissionConcurrentReplayTests(unittest.TestCase):
    def test_concurrent_idempotent_replay_cannot_suppress_rclone_after_provider_success(self) -> None:
        first_claimed = threading.Event()
        release_first = threading.Event()
        service_lock = threading.Lock()
        request_lock = threading.Lock()
        rclone_calls: list[tuple[dict, str]] = []
        first_outcome: dict[str, object] = {}

        class Submission:
            request = {
                "id": 9,
                "status": "submitted",
                "public_status": "处理中",
                "job_id": None,
            }

            @classmethod
            def worker_execution_allowed(cls, _request_id: int):
                with request_lock:
                    current = copy.deepcopy(cls.request)
                if current["status"] != "submitted":
                    return False, "访客提交状态已变化", current
                if current.get("job_id"):
                    return False, "访客提交已绑定正式任务", current
                return True, "", current

            @classmethod
            def bind_import_job(cls, _request_id: int, **kwargs):
                job = kwargs["job"]
                with request_lock:
                    existing_job_id = cls.request.get("job_id")
                    if existing_job_id is not None:
                        outcome = "existing" if int(existing_job_id) == int(job["id"]) else "conflict"
                        return outcome, copy.deepcopy(cls.request)
                    cls.request.update(
                        job_id=int(job["id"]),
                        status=str(job.get("status") or "submitted"),
                        public_status=str(kwargs.get("public_status") or "处理中"),
                    )
                    return "bound", copy.deepcopy(cls.request)

            @staticmethod
            def mark_import_failed(_request_id: int, *, error: str):
                raise AssertionError(f"并发幂等重放不应标记请求失败：{error}")

        class Imports:
            call_count = 0
            job = {
                "id": 42,
                "status": JOB_PROVIDER_SUBMITTING,
                "idempotency_key": "guest-request:req-concurrent",
                "raw_data": _submitting_fence(),
            }

            @classmethod
            def create_import_job(cls, _payload: dict) -> dict:
                with service_lock:
                    cls.call_count += 1
                    call_number = cls.call_count
                    current = copy.deepcopy(cls.job)
                if call_number > 1:
                    return {"success": False, "created": False, "job": current}

                first_claimed.set()
                if not release_first.wait(timeout=5):
                    raise TimeoutError("等待并发重放超时")
                with service_lock:
                    cls.job = {
                        **cls.job,
                        "status": "waiting_transfer",
                        "raw_data": {
                            PROVIDER_SUBMISSION_FENCE_KEY: {
                                "version": 1,
                                "state": "finished",
                                "attempt": 1,
                                "success": True,
                            }
                        },
                    }
                    completed = copy.deepcopy(cls.job)
                return {"success": True, "created": True, "job": completed}

        coordinator = PublicImportJobCoordinator(
            import_service=lambda: Imports,
            submission_service=lambda: Submission,
            runtime_revision=lambda: 1,
            executor_id=lambda: "worker",
            start_rclone=lambda result, reason: rclone_calls.append((copy.deepcopy(result), reason))
            or {"started": True},
            public_status=lambda status: status,
            safe_result=lambda result: result,
            warn=lambda *_args: None,
            cancel_unbound_job=lambda *_args, **_kwargs: self.fail(
                "同一幂等任务的并发重放不得取消正在成功返回的正式任务"
            ),
        )

        def run_first() -> None:
            try:
                first_outcome["value"] = coordinator.execute_inline(
                    guest_request_id=9,
                    request_token="req-concurrent",
                    submit_payload={"title": "并发提交测试"},
                )
            except Exception as exc:  # noqa: BLE001
                first_outcome["error"] = exc

        first_thread = threading.Thread(target=run_first, daemon=True)
        first_thread.start()
        self.assertTrue(first_claimed.wait(timeout=5), "首个提交未进入 provider_submitting")

        replay = coordinator.execute_inline(
            guest_request_id=9,
            request_token="req-concurrent",
            submit_payload={"title": "并发提交测试"},
        )
        self.assertEqual(replay.job["status"], JOB_PROVIDER_SUBMITTING)

        release_first.set()
        first_thread.join(timeout=5)
        self.assertFalse(first_thread.is_alive(), "首个提交线程未正常结束")
        self.assertNotIn("error", first_outcome)

        completed = first_outcome["value"]
        self.assertEqual(completed.job["status"], "waiting_transfer")
        self.assertEqual(len(rclone_calls), 1, "并发重放不得吞掉 Provider 成功后的 rclone 分发")
        self.assertEqual(completed.rclone_start, {"started": True})
        self.assertEqual(Submission.request["job_id"], 42)


if __name__ == "__main__":
    unittest.main()
