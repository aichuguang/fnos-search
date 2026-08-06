"""公共流程 v4 接入契约测试：访客不选择保存路径。

覆盖：
- 访客提交 -> 服务端解析来源与管理员分配的目标 -> 固化 v4 计划。
- 没有可用目标 -> 失败关闭，不回退默认盘。
- 访客夹带 destination_id / remote / root / account 等字段被忽略或拒绝。
- 重试只接受 v4 计划；旧版本返回 UNSUPPORTED_PLAN_VERSION。
- 伪造/损坏的 v4 计划在校验阶段失败。

v4 规划依赖使用 FAKE SourceResolver / DestinationResolver / plan_import /
validate_v4_plan 通过构造函数注入，避免依赖并行 Agent 尚未完成的 planning 包。
"""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from fnos_media_import.constants import JOB_CREATED, JOB_FAILED
from fnos_media_import.database import Database
from fnos_media_import.domain.models import (
    EXECUTION_PLAN_VERSION,
    DestinationDescriptor,
    SourceDescriptor,
)
from fnos_media_import.services.import_job_service import (
    V4PlanningError,
    ImportJobCreationService,
    ImportJobRetryService,
)
from fnos_media_import.services.public_import_job_coordinator import _v4_guest_idempotency_base
from fnos_media_import.services.public_submission_preparation_service import (
    PublicSubmissionPreparationService,
)


class _Link:
    route = "quark:share"
    source_type = "quark"
    url = "https://pan.quark.cn/s/abc"
    password = ""
    supported = True


class _Config:
    def __init__(self, raw: dict | None = None) -> None:
        self.raw = raw or {}

    def category(self, key: str) -> dict:
        return {"label": "电影"}


class _FakeSourceResolver:
    def __init__(self, source: SourceDescriptor) -> None:
        self.source = source
        self.resolve_calls = 0
        self.last_record: dict | None = None

    def resolve(self, record: dict | None) -> SourceDescriptor:
        self.resolve_calls += 1
        self.last_record = record if isinstance(record, dict) else {}
        return self.source


class _FakeDestinationResolver:
    def __init__(
        self,
        destination: DestinationDescriptor | None,
        *,
        source_account: tuple[str, int, int] | None = ("acc_q", 3, 1),
        destination_account: tuple[str, int, int] | None = ("acc_d", 2, 1),
    ) -> None:
        self.destination = destination
        self.source_account = source_account
        self.destination_account = destination_account
        self.resolve_calls = 0
        self.resolve_account_calls = 0

    def resolve(self, source: SourceDescriptor, category_key: str, audience: str = "guest"):
        self.resolve_calls += 1
        return self.destination

    def resolve_account(
        self,
        provider_kind: str,
        scope: str = "default",
        *,
        role: str = "destination",
    ) -> tuple[str, int, int]:
        self.resolve_account_calls += 1
        if role == "source":
            if self.source_account is None:
                raise V4PlanningError("当前来源账号不可用", code="SOURCE_ACCOUNT_UNAVAILABLE")
            return self.source_account
        if self.destination_account is None:
            raise V4PlanningError("保存位置账号不可用", code="DESTINATION_ACCOUNT_UNAVAILABLE")
        return self.destination_account


def _fake_plan_import(
    *,
    source: SourceDescriptor,
    category_key: str,
    job_id: int,
    destination_id: str | None = None,
    audience: str = "guest",
    organizer_enabled: bool = False,
    media_refresh_enabled: bool = False,
    **kwargs: object,
) -> dict:
    job_id = int(job_id)
    return {
        "version": EXECUTION_PLAN_VERSION,
        "job_id": job_id,
        "created_at": "2026-08-07T00:00:00Z",
        "source": {
            "provider_kind": source.provider_kind,
            "source_form": source.source_form,
            "canonical_source_id": source.canonical_source_id,
        },
        "destination": {
            "id": destination_id or "unknown",
            "provider_kind": source.provider_kind,
            "target_revision": 1,
        },
        "transfer": {
            "strategy": "rclone_copy",
            "executor": "rclone",
            "idempotency_key": f"import:test:{job_id}",
            "delete_source_after_ack": True,
        },
        "paths": {
            "source_landing": {"namespace_id": "openlist:main", "path": f"/_fnos_ingress/job-{job_id}"},
            "destination_staging": {"namespace_id": "openlist:main", "path": f"/影视/_fnos_staging/job-{job_id}"},
            "destination_final": {"namespace_id": "openlist:main", "path": "/影视/电影"},
        },
        "verification": {"minimum": ["file_count", "total_size"], "checksum": "when_supported"},
        "postprocess": {"organizer_enabled": False, "media_refresh_enabled": False},
    }


def _fake_validate_v4_plan(plan: dict, job_record: dict, *, deps: object = None) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("执行计划不是对象")
    if int(plan.get("version") or 0) != EXECUTION_PLAN_VERSION:
        raise ValueError(f"执行计划版本不是 v{EXECUTION_PLAN_VERSION}")
    job_id = int((job_record or {}).get("id") or 0)
    if int(plan.get("job_id") or 0) != job_id:
        raise ValueError("执行计划 job_id 与任务不一致")
    destination = plan.get("destination") if isinstance(plan.get("destination"), dict) else {}
    expected_destination = str((job_record or {}).get("destination_id") or "").strip()
    if str(destination.get("id") or "") != expected_destination:
        raise ValueError("执行计划目标与任务不一致")
    return plan


def _new_database() -> tuple[Database, Path]:
    root = Path(__file__).resolve().parents[1]
    path = root / f"public-flow-v4-{uuid.uuid4().hex}.db"
    database = Database(path)
    database.init_schema()
    return database, path


def _cleanup(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


class PublicV4FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database, self.path = _new_database()

    def tearDown(self) -> None:
        _cleanup(self.path)

    # ---- helpers -------------------------------------------------------

    def _creation_service(
        self,
        source_resolver: _FakeSourceResolver,
        destination_resolver: _FakeDestinationResolver,
        *,
        plan_import=None,
        validate=None,
    ) -> ImportJobCreationService:
        return ImportJobCreationService(
            database=self.database,
            config=_Config(),
            source_resolver=source_resolver,
            destination_resolver=destination_resolver,
            plan_import=plan_import or _fake_plan_import,
            validate_v4_plan=validate or _fake_validate_v4_plan,
        )

    def _retry_service(self, *, validate=None) -> ImportJobRetryService:
        return ImportJobRetryService(
            database=self.database,
            config=_Config(),
            validate_v4_plan=validate or _fake_validate_v4_plan,
        )

    def _job_row(self, **overrides: object) -> int:
        data: dict = {
            "title": "T",
            "category": "movie",
            "category_label": "电影",
            "source_type": "quark",
            "source_url": f"https://pan.quark.cn/s/{uuid.uuid4().hex[:8]}",
            "target_route": "quark:share",
            "target_path": "",
            "status": JOB_CREATED,
            "raw_data": {
                "provider_submission_fence": {"version": 1, "state": "not_started", "attempt": 0}
            },
            "idempotency_key": f"v4:test-{uuid.uuid4().hex}",
            "destination_id": "tianyi_library",
            "plan_version": EXECUTION_PLAN_VERSION,
        }
        data.update(overrides)
        return self.database.create_job(data)[0]

    # ---- guest submit -> server-resolved destination + v4 plan ----------

    def test_guest_submit_persists_server_resolved_destination_and_v4_plan(self) -> None:
        source = SourceDescriptor(
            provider_kind="quark",
            source_form="share",
            canonical_source_id="quark:share:abc",
            selected_items_hash="hash-1",
        )
        destination = DestinationDescriptor(
            destination_id="tianyi_library",
            provider_kind="tianyi",
            account_scope="default",
            target_revision=5,
        )
        source_resolver = _FakeSourceResolver(source)
        destination_resolver = _FakeDestinationResolver(destination)
        service = self._creation_service(source_resolver, destination_resolver)

        result = service.create(
            {"url": "https://pan.quark.cn/s/abc", "category": "movie", "public_id": "RS-1"}
        )

        self.assertTrue(result["created"])
        self.assertTrue(result["success"])
        job = self.database.get_job(result["job"]["id"])
        self.assertEqual(job["destination_id"], "tianyi_library")
        self.assertEqual(job["plan_version"], EXECUTION_PLAN_VERSION)
        raw_data = job["raw_data"]
        plan = raw_data["execution_plan"]
        self.assertEqual(plan["version"], EXECUTION_PLAN_VERSION)
        self.assertEqual(plan["job_id"], job["id"])
        self.assertEqual(plan["destination"]["id"], "tianyi_library")
        self.assertEqual(raw_data["destination"]["destination_id"], "tianyi_library")
        # 提交栅栏保留，供幂等恢复与 v4 执行器原子接管。
        self.assertEqual(raw_data["provider_submission_fence"]["state"], "not_started")

        # 幂等身份绑定 canonical source + category + destination + account revisions。
        key = job["idempotency_key"]
        self.assertIn("src=quark:share:abc", key)
        self.assertIn("cat=movie", key)
        self.assertIn("sel=hash-1", key)
        self.assertIn("dest=tianyi_library", key)
        self.assertIn("drev=5", key)
        self.assertIn("sa=acc_q@3", key)
        self.assertIn("da=acc_d@2", key)

    def test_same_resource_same_destination_reuses_job(self) -> None:
        source = SourceDescriptor(
            provider_kind="quark", source_form="share", canonical_source_id="quark:share:abc"
        )
        destination = DestinationDescriptor(
            destination_id="tianyi_library", provider_kind="tianyi", target_revision=5
        )
        service = self._creation_service(
            _FakeSourceResolver(source), _FakeDestinationResolver(destination)
        )
        payload = {"url": "https://pan.quark.cn/s/abc", "category": "movie"}
        first = service.create(payload)
        second = service.create(payload)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["job"]["id"], first["job"]["id"])

    def test_same_resource_different_destination_creates_separate_job(self) -> None:
        source = SourceDescriptor(
            provider_kind="quark", source_form="share", canonical_source_id="quark:share:abc"
        )
        destination_a = DestinationDescriptor(
            destination_id="tianyi_library", provider_kind="tianyi", target_revision=5
        )
        destination_b = DestinationDescriptor(
            destination_id="quark_library", provider_kind="quark", target_revision=2
        )
        service_a = self._creation_service(
            _FakeSourceResolver(source), _FakeDestinationResolver(destination_a)
        )
        service_b = self._creation_service(
            _FakeSourceResolver(source), _FakeDestinationResolver(destination_b)
        )
        payload = {"url": "https://pan.quark.cn/s/abc", "category": "movie"}
        first = service_a.create(payload)
        second = service_b.create(payload)
        self.assertTrue(first["created"])
        self.assertTrue(second["created"])
        self.assertNotEqual(second["job"]["id"], first["job"]["id"])

    # ---- no rule -> fail closed, no fallback ---------------------------

    def test_no_destination_rule_blocks_submission_without_fallback(self) -> None:
        source = SourceDescriptor(
            provider_kind="quark", source_form="share", canonical_source_id="quark:share:abc"
        )
        destination_resolver = _FakeDestinationResolver(None)
        service = self._creation_service(_FakeSourceResolver(source), destination_resolver)

        with self.assertRaises(V4PlanningError) as ctx:
            service.create({"url": "https://pan.quark.cn/s/abc", "category": "movie"})
        self.assertEqual(ctx.exception.code, "SOURCE_DESTINATION_NOT_CONFIGURED")
        self.assertIn("保存位置", str(ctx.exception))
        # 失败关闭：没有任务被创建，没有回退默认盘。
        self.assertEqual(self.database.count_jobs(), 0)

    def test_source_account_unavailable_blocks_submission(self) -> None:
        source = SourceDescriptor(
            provider_kind="quark", source_form="share", canonical_source_id="quark:share:abc"
        )
        destination = DestinationDescriptor(
            destination_id="tianyi_library", provider_kind="tianyi", target_revision=5
        )
        service = self._creation_service(
            _FakeSourceResolver(source),
            _FakeDestinationResolver(destination, source_account=None),
        )
        with self.assertRaises(V4PlanningError) as ctx:
            service.create({"url": "https://pan.quark.cn/s/abc", "category": "movie"})
        self.assertEqual(ctx.exception.code, "SOURCE_ACCOUNT_UNAVAILABLE")
        self.assertEqual(self.database.count_jobs(), 0)

    # ---- guest smuggling destination / path fields ---------------------

    def test_guest_smuggled_destination_and_path_fields_are_ignored(self) -> None:
        source = SourceDescriptor(
            provider_kind="quark", source_form="share", canonical_source_id="quark:share:abc"
        )
        destination = DestinationDescriptor(
            destination_id="tianyi_library", provider_kind="tianyi", target_revision=5
        )
        source_resolver = _FakeSourceResolver(source)
        destination_resolver = _FakeDestinationResolver(destination)
        service = self._creation_service(source_resolver, destination_resolver)

        result = service.create(
            {
                "url": "https://pan.quark.cn/s/abc",
                "category": "movie",
                "destination_id": "evil_target",
                "account_id": "hacker_acc",
                "provider_kind": "evil",
                "remote": "smuggled_remote",
                "mount_id": "mount_evil",
                "root": "/etc",
                "target_path": "/smuggled/path",
            }
        )

        self.assertTrue(result["created"])
        job = self.database.get_job(result["job"]["id"])
        # 服务端解析结果才是唯一真源。
        self.assertEqual(job["destination_id"], "tianyi_library")
        raw_data = job["raw_data"]
        self.assertEqual(raw_data["execution_plan"]["destination"]["id"], "tianyi_library")
        self.assertNotIn("evil_target", str(raw_data["execution_plan"]))
        self.assertNotIn("hacker_acc", str(raw_data))
        self.assertNotIn("smuggled_remote", str(raw_data))
        # 进入业务层的 request 不含夹带字段。
        self.assertIsNone(raw_data["request"].get("destination_id"))
        self.assertIsNone(raw_data["request"].get("remote"))
        self.assertIsNone(raw_data["request"].get("root"))
        self.assertEqual(destination_resolver.resolve_calls, 1)

    def test_preparation_service_drops_guest_destination_fields(self) -> None:
        service = PublicSubmissionPreparationService(
            search_cache=lambda _pid: None,
            categories=lambda: {"movie": {"label": "电影"}},
            category=lambda key: {"label": "电影"},
            routes=lambda: {"quark": {"enabled": True}},
            limited_text=lambda value, *args, **kwargs: str(value or ""),
            validate_url=lambda url, _security: str(url),
            safe_string_list=lambda *args, **kwargs: [],
            safe_quark_selection=lambda value: {},
            safe_cloud139_selection=lambda value: {},
            detect_link=lambda *_args, **_kwargs: _Link(),
            security_config=lambda: {},
            config_int=lambda _cfg, key, default: default,
        )
        prepared = service.prepare(
            {
                "url": "https://pan.quark.cn/s/abc",
                "title": "测试资源",
                "category": "movie",
                "destination_id": "evil_target",
                "remote": "smuggled",
                "root": "/etc",
                "account_id": "hacker_acc",
            }
        )
        self.assertNotIn("destination_id", prepared.payload)
        self.assertNotIn("remote", prepared.payload)
        self.assertNotIn("root", prepared.payload)
        self.assertNotIn("account_id", prepared.payload)

    # ---- coordinator v4 idempotency base -------------------------------

    def test_v4_guest_idempotency_base_derives_from_source_and_category(self) -> None:
        a = _v4_guest_idempotency_base({"url": "https://pan.quark.cn/s/abc", "category": "movie"})
        b = _v4_guest_idempotency_base({"url": "https://pan.quark.cn/s/abc", "category": "movie"})
        c = _v4_guest_idempotency_base({"url": "https://pan.quark.cn/s/abc", "category": "tv"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("v4:"))
        self.assertNotIn("destination", a)

    def test_create_refines_coordinator_base_key_with_resolved_identity(self) -> None:
        source = SourceDescriptor(
            provider_kind="quark", source_form="share", canonical_source_id="quark:share:abc"
        )
        destination = DestinationDescriptor(
            destination_id="tianyi_library", provider_kind="tianyi", target_revision=5
        )
        service = self._creation_service(
            _FakeSourceResolver(source), _FakeDestinationResolver(destination)
        )
        base = _v4_guest_idempotency_base({"url": "https://pan.quark.cn/s/abc", "category": "movie"})
        result = service.create(
            {"url": "https://pan.quark.cn/s/abc", "category": "movie", "idempotency_key": base}
        )
        job = self.database.get_job(result["job"]["id"])
        self.assertTrue(job["idempotency_key"].startswith(base + ":"))
        self.assertIn("dest=tianyi_library", job["idempotency_key"])

    # ---- retry requires v4 ---------------------------------------------

    def test_retry_rejects_non_v4_plan_with_unsupported_plan_version(self) -> None:
        job_id = self._job_row(plan_version=3, destination_id=None)
        retry = self._retry_service()
        result = retry.retry(job_id)
        self.assertFalse(result["success"])
        self.assertTrue(result.get("unsupported_plan_version"))
        self.assertEqual(result.get("error_code"), "UNSUPPORTED_PLAN_VERSION")
        self.assertFalse(result.get("v4_plan_ready"))
        # 不重建计划，也不调用任何 Provider。
        job = self.database.get_job(job_id)
        self.assertEqual(job["status"], JOB_CREATED)

    def test_retry_rejects_missing_plan_on_v4_job(self) -> None:
        job_id = self._job_row()  # plan_version=4 but no execution_plan
        retry = self._retry_service()
        result = retry.retry(job_id)
        self.assertFalse(result["success"])
        self.assertTrue(result["manual_review"])
        self.assertIn("没有固化 execution_plan", result["message"])

    def test_retry_forged_v4_plan_fails_validation(self) -> None:
        job_id = self._job_row(destination_id="tianyi_library")
        forged_plan = _fake_plan_import(
            source=SourceDescriptor(
                provider_kind="quark", source_form="share", canonical_source_id="quark:share:abc"
            ),
            category_key="movie",
            job_id=job_id,
            destination_id="evil_target",
        )
        self.database.update_job(
            job_id,
            raw_data={
                "execution_plan": forged_plan,
                "provider_submission_fence": {"version": 1, "state": "not_started", "attempt": 0},
            },
        )
        retry = self._retry_service()
        result = retry.retry(job_id)
        self.assertFalse(result["success"])
        self.assertTrue(result["manual_review"])
        self.assertEqual(result.get("error_code"), "PLANNING_VALIDATION_FAILED")
        self.assertIn("已拒绝重试", result["message"])
        # 校验失败时不会建立提交栅栏。
        job = self.database.get_job(job_id)
        self.assertEqual(job["status"], JOB_CREATED)

    def test_retry_v4_valid_plan_establishes_submission_fence(self) -> None:
        source = SourceDescriptor(
            provider_kind="quark", source_form="share", canonical_source_id="quark:share:abc"
        )
        destination = DestinationDescriptor(
            destination_id="tianyi_library", provider_kind="tianyi", target_revision=5
        )
        service = self._creation_service(
            _FakeSourceResolver(source), _FakeDestinationResolver(destination)
        )
        result = service.create({"url": "https://pan.quark.cn/s/abc", "category": "movie"})
        job_id = result["job"]["id"]
        retry = self._retry_service()
        outcome = retry.retry(job_id)
        self.assertTrue(outcome.get("v4_plan_ready"))
        self.assertTrue(outcome["success"])
        job = self.database.get_job(job_id)
        # 提交栅栏被原子接管，等待 v4 执行器。
        self.assertEqual(job["status"], "provider_submitting")

    def test_plan_generation_failure_marks_job_failed(self) -> None:
        source = SourceDescriptor(
            provider_kind="quark", source_form="share", canonical_source_id="quark:share:abc"
        )
        destination = DestinationDescriptor(
            destination_id="tianyi_library", provider_kind="tianyi", target_revision=5
        )

        def exploding_plan_import(**kwargs):
            raise ValueError("planner exploded")

        service = self._creation_service(
            _FakeSourceResolver(source),
            _FakeDestinationResolver(destination),
            plan_import=exploding_plan_import,
        )
        result = service.create({"url": "https://pan.quark.cn/s/abc", "category": "movie"})
        self.assertTrue(result["created"])
        self.assertFalse(result["success"])
        self.assertTrue(result.get("plan_error"))
        job = self.database.get_job(result["job"]["id"])
        self.assertEqual(job["status"], JOB_FAILED)
        self.assertIn("planner exploded", job.get("error_message") or "")


if __name__ == "__main__":
    unittest.main()
