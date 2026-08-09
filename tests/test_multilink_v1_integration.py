from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from fnos_media_import.accounts.service import ProviderAccountService
from fnos_media_import.database import Database
from fnos_media_import.domain.models import (
    AccountTestResult,
    LocalLocator,
    NativeLocator,
    SourceDescriptor,
    TargetCategory,
)
from fnos_media_import.openlist.connection_service import OpenListConnectionService
from fnos_media_import.openlist.mount_service import OpenListMountService
from fnos_media_import.planning.destination_resolver import DestinationResolver
from fnos_media_import.planning import PlanningError, SourceAccountUnavailable
from fnos_media_import.planning.plan_validator import PlanValidationDeps, validate_plan
from fnos_media_import.planning.planner import ImportPlanner
from fnos_media_import.planning.source_resolver import SourceResolver
from fnos_media_import.providers.registry import ProviderRegistry
from fnos_media_import.services.import_job_service import (
    ImportJobCreationService,
    V1PlanningError,
)
from fnos_media_import.services.source_record_service import SourceRecordService
from fnos_media_import.storage.assignment_service import SourceAssignmentService
from fnos_media_import.storage.target_service import StorageTargetService
from fnos_media_import.transfer.execution_repository import TransferExecutionRepository
from fnos_media_import.transfer.native_executor import LegacyNativeAdapter


KEY = bytes.fromhex("00" * 32)


class _Config:
    raw = {"organizer": {"enabled": False}}

    @staticmethod
    def category(key: str) -> dict[str, str]:
        return {"label": key}


class _MaterializingImporter:
    def materialize_resource(self, **_kwargs):
        raise AssertionError("planner capability checks must not execute materialization")


class MultilinkV1IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(__file__).with_name(
            f"_multilink_v1_{uuid.uuid4().hex}.db"
        )
        self.db = Database(self.db_path)
        self.db.init_schema()
        self.accounts = ProviderAccountService(self.db, lambda: KEY)
        self.connections = OpenListConnectionService(self.db, lambda: KEY)
        self.mounts = OpenListMountService(self.db, self.connections)
        self.targets = StorageTargetService(self.db)
        self.assignments = SourceAssignmentService(self.db, self.targets)
        self.registry = ProviderRegistry(
            [
                LegacyNativeAdapter(
                    _MaterializingImporter(),
                    "quark",
                    supports_native_save=True,
                )
            ]
        )
        self.sources = SourceRecordService(self.db, lambda: KEY)

    def tearDown(self) -> None:
        for candidate in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass

    def _quark_account(self) -> str:
        account = self.accounts.create_account(
            provider_kind="quark",
            name="主账号",
            config={},
            credentials={"cookie": "sid=secret-cookie"},
        )
        self.accounts.test_account(
            account.account_id,
            tester=lambda *_args: AccountTestResult(True, "quark-user", "ok"),
        )
        self.accounts.activate_account("default", account.account_id, None)
        return account.account_id

    def _planner(self) -> tuple[ImportPlanner, DestinationResolver, PlanValidationDeps]:
        resolver = DestinationResolver(self.assignments, self.targets, self.accounts)
        planner = ImportPlanner(
            accounts_service=self.accounts,
            target_service=self.targets,
            rules_service=self.assignments,
            mount_service=self.mounts,
            registry=self.registry,
            config={"FNOS_LOCAL_TARGET_ROOT": "/storage-targets"},
            destination_resolver=resolver,
        )
        deps = PlanValidationDeps(
            accounts_service=self.accounts,
            target_service=self.targets,
            mount_service=self.mounts,
            registry=self.registry,
            source_record_service=self.sources,
            assignment_service=self.assignments,
            config={"FNOS_LOCAL_TARGET_ROOT": "/storage-targets"},
        )
        return planner, resolver, deps

    def test_partial_account_revision_keeps_existing_secret(self) -> None:
        account_id = self._quark_account()
        updated = self.accounts.revise_credentials(
            account_id,
            config={},
            credentials={},
        )
        context = self.accounts.resolve_account_context(account_id, updated.current_revision)
        self.assertEqual(context["credentials"]["cookie"], "sid=secret-cookie")

    def test_refresh_advances_future_target_binding_but_keeps_old_revision(self) -> None:
        account_id = self._quark_account()
        self.targets.create_target(
            destination_id="quark_main",
            name="夸克主盘",
            provider_kind="quark",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="电影",
                    native_locator=NativeLocator("path", "/电影"),
                )
            },
            account_id=account_id,
        )
        before = self.targets.resolve_binding("quark_main", 1)

        updated = self.accounts.refresh_runtime_credentials(
            account_id,
            {"cookie": "sid=refreshed-cookie"},
        )

        current = self.targets.resolve_binding("quark_main", 1)
        frozen = self.targets.get_binding(before.binding_id, before.current_revision)
        self.assertEqual(current.current_revision, before.current_revision + 1)
        self.assertEqual(current.account_revision, updated.current_revision)
        self.assertEqual(frozen.account_revision, before.account_revision)

        self.accounts.test_account(
            account_id,
            tester=lambda *_args: AccountTestResult(True, "quark-user", "ok"),
        )
        unchanged = self.targets.resolve_binding("quark_main", 1)
        self.assertEqual(unchanged.current_revision, current.current_revision)

    def test_failed_active_account_is_rejected_by_planner_resolution(self) -> None:
        account_id = self._quark_account()
        self.accounts.test_account(
            account_id,
            tester=lambda *_args: AccountTestResult(False, message="凭证失效"),
        )
        resolver = DestinationResolver(self.assignments, self.targets, self.accounts)
        with self.assertRaises(SourceAccountUnavailable):
            resolver.resolve_account("quark", role="source")

    def test_cross_provider_plan_freezes_source_mount_native_locator(self) -> None:
        account_id = self._quark_account()
        self.connections.create_connection(
            {
                "connection_id": "ol_main",
                "name": "OpenList",
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "secret",
            }
        )
        self.mounts.create_mount(
            {
                "mount_id": "quark_main",
                "connection_id": "ol_main",
                "name": "夸克主盘",
                "webdav_root": "/夸克",
                "account_id": account_id,
                "provider_kind": "quark",
                "identity_root": "/",
                "native_locator": {"kind": "path", "value": "/FNOS"},
            }
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE openlist_mounts SET readable=1, writable=1, "
                "last_tested_at='2026-08-08T00:00:00Z' WHERE mount_id='quark_main'"
            )
        self.mounts.confirm_identity("quark_main")
        self.targets.create_target(
            destination_id="local_library",
            name="本地媒体库",
            provider_kind="local",
            target_kind="local",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="电影",
                    rclone_locator=LocalLocator("/storage-targets/local_library", "电影"),
                )
            },
        )
        self.assignments.upsert("quark", "movie", "local_library")
        source = self.sources.capture(
            SourceDescriptor("quark", "share", "quark:share:abc", search_source_id="quark"),
            {"url": "https://pan.quark.cn/s/abc", "password": ""},
        )
        planner, _resolver, deps = self._planner()
        plan = planner.plan_import(source=source, category_key="movie", job_id=42)
        self.assertEqual(plan["transfer"]["strategy"], "rclone_copy")
        self.assertEqual(
            plan["bindings"]["source_native_locator"],
            {"kind": "path", "value": "/FNOS"},
        )
        validate_plan(
            plan,
            {"id": 42, "category": "movie", "destination_id": "local_library", "plan_version": 1},
            deps=deps,
        )
        with self.assertRaises(PlanningError) as raised:
            planner.plan_import(
                source=source,
                category_key="movie",
                job_id=43,
                organizer_enabled=True,
            )
        self.assertEqual(
            raised.exception.code,
            "LOCAL_ORGANIZER_REQUIRES_OPENLIST_MOUNT",
        )

    def test_job_plan_execution_and_worker_task_are_atomic(self) -> None:
        account_id = self._quark_account()
        self.targets.create_target(
            destination_id="quark_main",
            name="夸克主盘",
            provider_kind="quark",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="电影",
                    native_locator=NativeLocator("path", "/电影"),
                )
            },
            account_id=account_id,
        )
        self.assignments.upsert("quark", "movie", "quark_main")
        planner, resolver, deps = self._planner()
        execution_repository = TransferExecutionRepository(self.db)
        service = ImportJobCreationService(
            database=self.db,
            config=_Config(),
            source_resolver=SourceResolver(
                lambda _url: {"provider_kind": "quark", "source_form": "share"}
            ),
            destination_resolver=resolver,
            plan_import=planner.plan_import,
            validate_plan=lambda plan, job: validate_plan(plan, job, deps=deps),
            execution_repository=execution_repository,
            enqueue_transfer_with_connection=lambda connection, job_id: self.db.worker_tasks.enqueue_with_connection(
                connection,
                "import_transfer_execute",
                {"job_id": job_id},
                f"import-transfer:{job_id}",
                max_attempts=20,
                config_revision=1,
            ),
            source_record_service=self.sources,
        )
        with self.assertRaises(V1PlanningError) as forbidden:
            service.create(
                {
                    "title": "测试电影",
                    "url": "https://pan.quark.cn/s/abc",
                    "category": "movie",
                    "destination_id": "attacker-target",
                }
            )
        self.assertEqual(forbidden.exception.code, "CLIENT_DESTINATION_FORBIDDEN")
        result = service.create_internal(
            {
                "title": "测试电影",
                "url": "https://pan.quark.cn/s/abc",
                "category": "movie",
                "destination_id": "attacker-target",
                "executor_id": "attacker",
                "config_revision": 999,
            }
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["job"]["destination_id"], "quark_main")
        self.assertNotEqual(result["job"]["executor_id"], "attacker")
        job_id = result["job"]["id"]
        with self.db.connect() as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM import_execution_plans WHERE job_id=?", (job_id,)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT 1 FROM transfer_executions WHERE job_id=?", (job_id,)).fetchone())
            job_row = connection.execute(
                "SELECT source_url, password, raw_data FROM import_jobs WHERE id=?", (job_id,)
            ).fetchone()
            task = connection.execute(
                "SELECT task_type FROM worker_tasks WHERE idempotency_key=?",
                (f"import-transfer:{job_id}",),
            ).fetchone()
        self.assertEqual(task["task_type"], "import_transfer_execute")
        self.assertTrue(str(job_row["source_url"]).startswith("source-record:"))
        self.assertEqual(job_row["password"], "")
        request_snapshot = json.loads(job_row["raw_data"])["request"]
        self.assertNotIn("url", request_snapshot)
        self.assertNotIn("password", request_snapshot)

        def fail_enqueue(_connection, _job_id):
            raise RuntimeError("queue unavailable")

        failing_service = ImportJobCreationService(
            database=self.db,
            config=_Config(),
            source_resolver=SourceResolver(
                lambda _url: {"provider_kind": "quark", "source_form": "share"}
            ),
            destination_resolver=resolver,
            plan_import=planner.plan_import,
            validate_plan=lambda plan, job: validate_plan(plan, job, deps=deps),
            execution_repository=execution_repository,
            enqueue_transfer_with_connection=fail_enqueue,
            source_record_service=self.sources,
        )
        failed = failing_service.create(
            {
                "title": "回滚测试",
                "url": "https://pan.quark.cn/s/rollback-case",
                "password": "secret",
                "category": "movie",
            }
        )
        self.assertFalse(failed["success"], failed)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM import_execution_plans").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM transfer_executions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM worker_tasks").fetchone()[0], 1)

    def test_new_provider_source_is_declared_without_core_mapping(self) -> None:
        resolver = SourceResolver(
            lambda _url: {"source_type": "115_share"},
            source_catalog_provider=lambda: [
                {
                    "search_source_id": "p115",
                    "provider_kind": "115",
                    "source_form": "share",
                    "source_types": ["115", "115_share"],
                }
            ],
        )
        source = resolver.resolve({"url": "https://115.com/s/example"})
        self.assertEqual(source.provider_kind, "115")
        self.assertEqual(source.search_source_id, "p115")


if __name__ == "__main__":
    unittest.main()
