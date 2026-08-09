from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import copy
import uuid
import unittest
from pathlib import Path
from typing import Any

from fnos_media_import.accounts.service import ProviderAccountService
from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
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
from fnos_media_import.planning.job_plan import attach_plan_integrity
from fnos_media_import.planning.plan_validator import PlanValidationDeps, validate_plan
from fnos_media_import.planning.planner import ImportPlanner
from fnos_media_import.providers.adapters import QuarkProviderAdapter
from fnos_media_import.providers.registry import ProviderRegistry
from fnos_media_import.services.source_record_service import SourceRecordService
from fnos_media_import.storage.assignment_service import (
    AssignmentConflictError,
    SourceAssignmentService,
    assignment_id,
)
from fnos_media_import.storage.target_service import StorageTargetService


KEY = bytes.fromhex("42" * 32)


def _database(testcase: unittest.TestCase) -> RepositorySet:
    path = Path(__file__).with_name(f"_planning_revision_{uuid.uuid4().hex}.db")

    def cleanup() -> None:
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass

    testcase.addCleanup(cleanup)
    database = Database(path)
    database.init_schema()
    return RepositorySet.from_database(database)


class _WebDAV(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = set()

    def auth_probe(self) -> None:
        return None

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        return []

    def mkcol(self, path: str) -> None:
        self.directories.add(path)

    def put_bytes(self, path: str, content: bytes) -> None:
        self.files[path] = content

    def read_bytes(self, path: str) -> bytes:
        return self.files[path]

    def file_size(self, path: str) -> int:
        return len(self.files[path])

    def move(self, source: str, destination: str) -> None:
        self.files[destination] = self.files.pop(source)

    def delete(self, path: str) -> None:
        self.files.pop(path, None)
        self.directories.discard(path)

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.directories


class PlanningRevisionGuardTests(unittest.TestCase):
    def _local_target(self, targets: StorageTargetService, destination_id: str) -> None:
        targets.create_target(
            destination_id=destination_id,
            name=destination_id,
            provider_kind="local",
            target_kind="local",
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="电影",
                    rclone_locator=LocalLocator(
                        f"/storage-targets/{destination_id}", "电影"
                    ),
                )
            },
        )

    def test_assignment_history_cas_and_recreate_keep_monotonic_revision(self) -> None:
        database = _database(self)
        targets = StorageTargetService(database)
        self._local_target(targets, "local_a")
        self._local_target(targets, "local_b")
        assignments = SourceAssignmentService(database, targets)

        first = assignments.upsert("quark", "movie", "local_a")
        self.assertEqual(first.revision, 1)
        stable_id = assignment_id("quark", "movie")
        self.assertEqual(
            assignments.get_revision(stable_id, 1)["destination_id"], "local_a"
        )
        with self.assertRaises(AssignmentConflictError):
            assignments.upsert(
                "quark", "movie", "local_b", expected_revision=99
            )

        second = assignments.upsert(
            "quark", "movie", "local_b", expected_revision=1
        )
        self.assertEqual(second.revision, 2)
        assignments.delete("quark", "movie", expected_revision=2)
        recreated = assignments.upsert("quark", "movie", "local_a")
        self.assertEqual(recreated.revision, 3)
        self.assertEqual(
            assignments.get_revision(stable_id, 2)["destination_id"], "local_b"
        )

    def test_mount_probe_and_identity_confirmation_create_complete_revisions(self) -> None:
        database = _database(self)
        connections = OpenListConnectionService(database, lambda: KEY)
        connections.create_connection(
            {
                "connection_id": "ol_main",
                "base_url": "http://openlist:5244/dav",
                "username": "admin",
                "password": "secret",
            }
        )
        webdav = _WebDAV()
        mounts = OpenListMountService(
            database,
            connection_service=connections,
            webdav_factory=lambda _credentials: webdav,
        )
        created = mounts.create_mount(
            {
                "mount_id": "generic_main",
                "connection_id": "ol_main",
                "webdav_root": "/媒体",
                "storage_identity_id": "storage:generic-main",
            }
        )
        self.assertEqual(created["revision"], 1)
        revision_one = mounts.get_revision("generic_main", 1)
        self.assertFalse(revision_one["readable"])
        self.assertEqual(revision_one["connection_revision"], 1)
        self.assertEqual(
            revision_one["namespace_id"], "openlist:ol_main:generic_main"
        )

        probed = mounts.probe_mount("generic_main")
        self.assertTrue(probed["success"], probed)
        self.assertEqual(mounts.get_mount("generic_main")["revision"], 2)
        revision_two = mounts.get_revision("generic_main", 2)
        self.assertTrue(revision_two["readable"])
        self.assertTrue(revision_two["writable"])
        self.assertEqual(revision_two["identity_status"], "unverified")

        confirmed = mounts.confirm_identity("generic_main")
        self.assertEqual(confirmed["revision"], 3)
        revision_three = mounts.get_revision("generic_main", 3)
        self.assertEqual(revision_three["identity_status"], "declared")
        self.assertEqual(revision_three["storage_identity_id"], "storage:generic-main")
        self.assertFalse(mounts.get_revision("generic_main", 1)["readable"])

    def test_old_assignment_revision_remains_valid_and_integrity_is_exact(self) -> None:
        database = _database(self)
        accounts = ProviderAccountService(database, lambda: KEY)
        account = accounts.create_account(
            provider_kind="quark",
            name="夸克主账号",
            config={},
            credentials={"cookie": "sid=secret"},
        )
        accounts.test_account(
            account.account_id,
            tester=lambda *_args: AccountTestResult(True, "user", "ok"),
        )
        accounts.activate_account("default", account.account_id, None)

        targets = StorageTargetService(database)
        for destination_id, path in (("quark_a", "/电影A"), ("quark_b", "/电影B")):
            targets.create_target(
                destination_id=destination_id,
                name=destination_id,
                provider_kind="quark",
                categories={
                    "movie": TargetCategory(
                        category_key="movie",
                        public_label="电影",
                        native_locator=NativeLocator("path", path),
                    )
                },
                account_id=account.account_id,
            )
        assignments = SourceAssignmentService(database, targets)
        assignments.upsert("quark", "movie", "quark_a")
        sources = SourceRecordService(database, lambda: KEY)
        source = sources.capture(
            SourceDescriptor(
                "quark",
                "share",
                "quark:share:abc",
                search_source_id="quark",
            ),
            {"url": "https://pan.quark.cn/s/abc", "password": ""},
        )
        mounts = OpenListMountService(database)
        importer = object()
        registry = ProviderRegistry(
            [
                QuarkProviderAdapter(
                    importer_factory=lambda _account_id, _revision: importer,
                    account_importer_factory=lambda _values: importer,
                    source_record_service=sources,
                )
            ]
        )
        resolver = DestinationResolver(assignments, targets, accounts)
        planner = ImportPlanner(
            accounts_service=accounts,
            target_service=targets,
            rules_service=assignments,
            mount_service=mounts,
            registry=registry,
            destination_resolver=resolver,
        )
        deps = PlanValidationDeps(
            accounts_service=accounts,
            target_service=targets,
            mount_service=mounts,
            registry=registry,
            source_record_service=sources,
            assignment_service=assignments,
        )

        plan = planner.plan_import(source=source, category_key="movie", job_id=7)
        job = {
            "id": 7,
            "category": "movie",
            "destination_id": "quark_a",
            "plan_version": 1,
        }
        validate_plan(plan, job, deps=deps)
        assignments.upsert("quark", "movie", "quark_b", expected_revision=1)
        validate_plan(plan, job, deps=deps)

        tampered = copy.deepcopy(plan)
        tampered["destination"]["target_revision"] += 1
        with self.assertRaisesRegex(ValueError, "plan_hash"):
            validate_plan(tampered, job, deps=deps)

        forged_key = copy.deepcopy(plan)
        forged_key["transfer"]["idempotency_key"] = "import:v1:forged"
        attach_plan_integrity(forged_key)
        with self.assertRaisesRegex(ValueError, "idempotency_key"):
            validate_plan(forged_key, job, deps=deps)


if __name__ == "__main__":
    unittest.main()
