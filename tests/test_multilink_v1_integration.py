from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from fnos_media_import.accounts.service import ProviderAccountService
from fnos_media_import.config import AppConfig
from fnos_media_import.database import Database
from fnos_media_import.repositories import OrganizerRepositories, RepositorySet
from fnos_media_import.domain.models import (
    AccountTestResult,
    LocalLocator,
    NativeLocator,
    OpenListLocator,
    RcloneLocator,
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
from fnos_media_import.providers.adapters import (
    QuarkProviderAdapter,
    TianyiProviderAdapter,
)
from fnos_media_import.providers.registry import ProviderRegistry
from fnos_media_import.runtime_builder import RuntimeBuilder
from fnos_media_import.services.import_job_service import (
    ImportJobCreationService,
    V1PlanningError,
)
from fnos_media_import.services.source_record_service import SourceRecordService
from fnos_media_import.services.worker_task_dispatcher import WorkerTaskDispatcher
from fnos_media_import.storage.assignment_service import SourceAssignmentService
from fnos_media_import.storage.target_service import StorageTargetService
from fnos_media_import.transfer.execution_repository import TransferExecutionRepository


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
        self._db_infrastructure = Database(self.db_path)
        self._db_infrastructure.init_schema()
        self.db = RepositorySet.from_database(self._db_infrastructure)
        self.accounts = ProviderAccountService(self.db, lambda: KEY)
        self.connections = OpenListConnectionService(self.db, lambda: KEY)
        self.mounts = OpenListMountService(self.db, self.connections)
        self.targets = StorageTargetService(self.db)
        self.assignments = SourceAssignmentService(self.db, self.targets)
        self.sources = SourceRecordService(self.db, lambda: KEY)
        importer = _MaterializingImporter()
        self.registry = ProviderRegistry(
            [
                QuarkProviderAdapter(
                    importer_factory=lambda _account_id, _revision: importer,
                    account_importer_factory=lambda _values: importer,
                    source_record_service=self.sources,
                ),
                TianyiProviderAdapter(
                    importer_factory=lambda _account_id, _revision: importer,
                    account_importer_factory=lambda _values: importer,
                    source_record_service=self.sources,
                ),
            ]
        )

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

    def _tianyi_account(self) -> str:
        account = self.accounts.create_account(
            provider_kind="tianyi",
            name="\u5929\u7ffc\u4e3b\u8d26\u53f7",
            config={"identity": "user"},
            credentials={
                "session_key": "session-key",
                "session_secret": "0123456789abcdef",
            },
        )
        self.accounts.test_account(
            account.account_id,
            tester=lambda *_args: AccountTestResult(True, "tianyi-user", "ok"),
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
                "native_locator": {
                    "kind": "path",
                    "value": "/FNOS",
                    "display_name": "Provider root",
                    "upload_verified": True,
                    "organizer_mutation_verified": True,
                    "probe_fingerprint": "source-proof",
                },
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

    def test_tianyi_same_provider_plan_uses_native_save(self) -> None:
        account_id = self._tianyi_account()
        self.targets.create_target(
            destination_id="tianyi_main",
            name="\u5929\u7ffc\u4e3b\u76d8",
            provider_kind="tianyi",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="\u7535\u5f71",
                    native_locator=NativeLocator("folder_id", "root-folder-id"),
                )
            },
            account_id=account_id,
        )
        self.assignments.upsert("tianyi", "movie", "tianyi_main")
        source = self.sources.capture(
            SourceDescriptor(
                "tianyi",
                "share",
                "tianyi:share:abc",
                search_source_id="tianyi",
            ),
            {"url": "https://cloud.189.cn/t/example", "password": ""},
        )
        planner, _resolver, deps = self._planner()

        plan = planner.plan_import(
            source=source,
            category_key="movie",
            job_id=41,
        )

        self.assertEqual(plan["transfer"]["strategy"], "native_save")
        self.assertEqual(plan["destination"]["account_id"], account_id)
        self.assertEqual(
            plan["paths"]["destination_final"]["path"],
            "root-folder-id",
        )
        validate_plan(
            plan,
            {
                "id": 41,
                "category": "movie",
                "destination_id": "tianyi_main",
                "plan_version": 1,
            },
            deps=deps,
        )

    def test_runtime_builder_initial_build_can_create_execute_and_retry_without_rewire(self) -> None:
        account_id = self._quark_account()
        self.targets.create_target(
            destination_id="quark_runtime",
            name="Quark runtime target",
            provider_kind="quark",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="Movie",
                    native_locator=NativeLocator("path", "/Movies"),
                )
            },
            account_id=account_id,
        )
        self.assignments.upsert("quark", "movie", "quark_runtime")
        self.accounts.set_catalog_provider(self.registry.manifests)
        self.assignments.set_source_catalog_provider(self.registry.search_sources)
        executions = TransferExecutionRepository(self.db)
        worker_dispatcher = WorkerTaskDispatcher(
            repository=self.db.worker_tasks,
            enabled=lambda: True,
            config_revision=lambda: 1,
        )
        active_importer = _MaterializingImporter()
        config = AppConfig(
            raw={
                "pansou": {},
                "btbtla": {"base_url": "https://example.invalid"},
                "fnos": {
                    "log_enabled": False,
                    "token_cache_path": str(self.db_path.with_suffix(".fnos-token.json")),
                },
                "search": {"providers": {}},
                "openlist": {},
                "organizer": {"enabled": False, "staging_enabled": False},
                "categories": {"movie": {"label": "Movie", "fnos_lib": "Movie"}},
            },
            base_dir=self.db_path.parent,
        )
        builder = RuntimeBuilder(
            self.db,
            organizer_repositories=OrganizerRepositories.from_set(self.db),
            owner_id="runtime-builder-test",
            organizer_openlist_client_resolver=lambda _job, _plan: None,
            provider_importer_resolver=lambda _provider_kind: active_importer,
            accounts_service=self.accounts,
            target_service=self.targets,
            mount_service=self.mounts,
            rules_service=self.assignments,
            provider_registry=self.registry,
            source_record_service=self.sources,
            execution_repository=executions,
            enqueue_transfer=lambda job_id: worker_dispatcher.import_transfer_execute(
                job_id,
                reactivate=True,
            ),
            enqueue_transfer_with_connection=(
                worker_dispatcher.import_transfer_execute_with_connection
            ),
            config_revision_provider=lambda: 1,
        )

        runtime = builder.build(config, recover_background=False)
        try:
            created = runtime.import_service.create_import_job(
                {
                    "title": "Runtime builder V1",
                    "category": "movie",
                    "url": "https://pan.quark.cn/s/runtime-builder-v1",
                }
            )
            self.assertTrue(created["success"], created)
            self.assertTrue(created["created"])
            job_id = int(created["job"]["id"])
            self.assertEqual(executions.plan(job_id)["version"], 1)

            claimed = self.db.worker_tasks.claim(
                "runtime-worker",
                task_types=["import_transfer_execute"],
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["payload"]["job_id"], job_id)
            self.assertTrue(
                self.db.worker_tasks.fail(
                    int(claimed["id"]),
                    "runtime-worker",
                    "simulated worker restart",
                    terminal=True,
                )
            )

            replacement = builder.build(config, recover_background=False)
            try:
                retried = replacement.import_service.retry_job(job_id)
                self.assertTrue(retried["success"])
                reactivated = self.db.worker_tasks.get(int(claimed["id"]))
                self.assertEqual(reactivated["status"], "pending")
                self.assertEqual(reactivated["attempts"], 0)
            finally:
                replacement.close()
        finally:
            runtime.close()

    def test_quark_to_tianyi_relay_freezes_and_requires_native_proofs(self) -> None:
        quark_account = self._quark_account()
        tianyi_account = self._tianyi_account()
        self.connections.create_connection(
            {
                "connection_id": "ol_relay",
                "name": "OpenList relay",
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "secret",
            }
        )
        source_mount = self.mounts.create_mount(
            {
                "mount_id": "quark_relay_source",
                "connection_id": "ol_relay",
                "name": "\u5938\u514b\u6765\u6e90",
                "webdav_root": "/quark",
                "account_id": quark_account,
                "provider_kind": "quark",
                "identity_root": "/",
                "native_locator": {"kind": "path", "value": "/FNOS"},
            }
        )
        destination_mount = self.mounts.create_mount(
            {
                "mount_id": "tianyi_relay_target",
                "connection_id": "ol_relay",
                "name": "\u5929\u7ffc\u76ee\u6807",
                "webdav_root": "/tianyi",
                "account_id": tianyi_account,
                "provider_kind": "tianyi",
                "identity_root": "/",
                "native_locator": {
                    "kind": "folder_id",
                    "value": "root-folder-id",
                    "upload_verified": True,
                    "organizer_mutation_verified": True,
                },
            }
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE openlist_mounts SET readable=1, writable=1, "
                "last_tested_at='2026-08-09T00:00:00Z' WHERE mount_id=?",
                (source_mount["mount_id"],),
            )
            connection.execute(
                "UPDATE openlist_mounts SET readable=1, writable=0, "
                "last_tested_at='2026-08-09T00:00:00Z' WHERE mount_id=?",
                (destination_mount["mount_id"],),
            )
        self.mounts.confirm_identity(source_mount["mount_id"])
        self.mounts.confirm_identity(destination_mount["mount_id"])
        self.targets.create_target(
            destination_id="tianyi_relay",
            name="\u5929\u7ffc\u8de8\u76d8\u76ee\u6807",
            provider_kind="tianyi",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="\u7535\u5f71",
                    native_locator=NativeLocator("folder_id", "root-folder-id"),
                    rclone_locator=RcloneLocator(
                        "ol_relay",
                        destination_mount["mount_id"],
                        "/tianyi/电影",
                    ),
                    organizer_locator=OpenListLocator(
                        "ol_relay",
                        destination_mount["mount_id"],
                        "/tianyi",
                    ),
                )
            },
            account_id=tianyi_account,
        )
        self.assignments.upsert("quark", "movie", "tianyi_relay")
        source = self.sources.capture(
            SourceDescriptor(
                "quark",
                "share",
                "quark:share:relay",
                search_source_id="quark",
                expected_file_count=2,
                expected_total_bytes=12,
            ),
            {"url": "https://pan.quark.cn/s/relay", "password": ""},
        )
        planner, _resolver, deps = self._planner()
        plan = planner.plan_import(
            source=source,
            category_key="movie",
            job_id=42,
        )
        job = {
            "id": 42,
            "category": "movie",
            "destination_id": "tianyi_relay",
            "plan_version": 1,
        }

        self.assertEqual(
            plan["transfer"]["strategy"],
            "relay_upload_provider",
        )
        self.assertEqual(plan["destination"]["account_id"], tianyi_account)
        self.assertGreater(plan["destination"]["account_revision"], 0)
        self.assertEqual(
            plan["paths"]["destination_final"]["namespace_id"],
            "native:tianyi:tianyi_relay",
        )
        self.assertEqual(
            plan["paths"]["destination_final"]["path"],
            "root-folder-id",
        )
        self.assertEqual(
            plan["bindings"]["destination_mount_id"],
            destination_mount["mount_id"],
        )
        validate_plan(plan, job, deps=deps)

        destination_revision = int(
            plan["bindings"]["destination_mount_revision"]
        )
        original_revision = self.mounts.get_revision(
            destination_mount["mount_id"],
            destination_revision,
        )
        self.assertIsNotNone(original_revision)
        for index, proof_name in enumerate(
            ("upload_verified", "organizer_mutation_verified"),
            start=43,
        ):
            broken_revision = dict(original_revision)
            broken_revision["native_locator"] = dict(
                original_revision["native_locator"]
            )
            broken_revision["native_locator"][proof_name] = False
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE openlist_mount_revisions SET config_json=? "
                    "WHERE mount_id=? AND revision=?",
                    (
                        json.dumps(
                            broken_revision,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        destination_mount["mount_id"],
                        destination_revision,
                    ),
                )
            with self.subTest(proof_name=proof_name):
                with self.assertRaises(PlanningError):
                    planner.plan_import(
                        source=source,
                        category_key="movie",
                        job_id=index,
                    )
                with self.assertRaises(PlanningError):
                    validate_plan(plan, job, deps=deps)
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE openlist_mount_revisions SET config_json=? "
                    "WHERE mount_id=? AND revision=?",
                    (
                        json.dumps(
                            original_revision,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        destination_mount["mount_id"],
                        destination_revision,
                    ),
                )
        validate_plan(plan, job, deps=deps)

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
            lambda _url: {
                "provider_kind": "115",
                "source_form": "share",
                "search_source_id": "p115",
            },
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
