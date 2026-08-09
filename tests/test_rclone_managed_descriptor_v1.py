from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path

from fnos_media_import.domain.models import SelectionSnapshot
from fnos_media_import.openlist.locator_resolver import RcloneLocatorResolver
from fnos_media_import.planning.job_plan import attach_plan_integrity, compute_operation_key
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.transfer import ErrorCode, TransferError
from fnos_media_import.transfer.execution_repository import TransferExecutionRepository
from fnos_media_import.transfer.rclone_executor import build_managed_copy_run


JOB_ID = 7


def _path_descriptor(
    *,
    mount_id: str,
    connection_id: str,
    storage_identity_id: str,
    path: str,
    identity_root: str = "/",
) -> dict:
    return {
        "kind": "openlist",
        "namespace_id": f"openlist:{connection_id}:{mount_id}",
        "path": path,
        "mount_id": mount_id,
        "mount_revision": 1,
        "connection_id": connection_id,
        "connection_revision": 1,
        "storage_identity_id": storage_identity_id,
        "identity_root": identity_root,
    }


def _plan(*, local_target: bool = False, identity_root: str = "/") -> dict:
    selection = SelectionSnapshot(provider_kind="quark", selected_items=("share-item",))
    source = _path_descriptor(
        mount_id="src",
        connection_id="ol",
        storage_identity_id="storage:source",
        path=f"/_fnos_ingress/job-{JOB_ID}",
        identity_root=identity_root,
    )
    if local_target:
        staging = {
            "kind": "local",
            "namespace_id": "local:library",
            "path": f"/storage-targets/library/job-{JOB_ID}",
        }
        final = {
            "kind": "local",
            "namespace_id": "local:library",
            "path": "/storage-targets/library",
        }
    else:
        staging = _path_descriptor(
            mount_id="dst",
            connection_id="ol",
            storage_identity_id="storage:destination",
            path=f"/_fnos_staging/movie/job-{JOB_ID}",
            identity_root=identity_root,
        )
        final = {
            **staging,
            "path": "/Media/Movies",
        }
    plan = {
        "version": 1,
        "plan_schema_version": 1,
        "job_id": JOB_ID,
        "category": "movie",
        "source": {
            "provider_kind": "quark",
            "source_form": "share",
            "search_source_id": "quark-search",
            "canonical_source_id": "quark:share:managed-descriptor",
            "record_id": "source-record-7",
            "record_revision": 1,
            "account_id": "account-quark",
            "account_revision": 1,
            "account_selection_revision": 1,
            "selection": selection.to_dict(),
            "selection_hash": selection.selection_hash,
        },
        "destination": {
            "id": "library-main",
            "provider_kind": "tianyi",
            "target_revision": 1,
            "binding_id": "binding-library-main",
            "binding_revision": 1,
            "account_id": "account-tianyi",
            "account_revision": 1,
        },
        "routing": {
            "assignment_id": "assignment-quark-movie",
            "assignment_revision": 1,
            "search_source_id": "quark-search",
            "category_key": "movie",
            "destination_id": "library-main",
            "enabled": True,
        },
        "target_category": {"category_key": "movie", "public_label": "电影"},
        "bindings": {
            "source_mount_id": "src",
            "source_mount_revision": 1,
            "source_mount_namespace_id": "openlist:ol:src",
            "source_connection_id": "ol",
            "source_connection_revision": 1,
            "source_mount_storage_identity_id": "storage:source",
            "source_mount_identity_root": identity_root,
            "destination_mount_id": "dst" if not local_target else None,
            "destination_mount_revision": 1 if not local_target else None,
            "destination_mount_namespace_id": (
                "openlist:ol:dst" if not local_target else None
            ),
            "destination_connection_id": "ol" if not local_target else None,
            "destination_connection_revision": 1 if not local_target else None,
            "destination_mount_storage_identity_id": (
                "storage:destination" if not local_target else None
            ),
            "destination_mount_identity_root": identity_root if not local_target else None,
        },
        "transfer": {
            "strategy": "rclone_copy",
            "executor": "rclone",
            "idempotency_key": "",
            "delete_source_after_ack": True,
        },
        "paths": {
            "source_landing": source,
            "destination_staging": staging,
            "destination_final": final,
        },
        "verification": {
            "expected_file_count": 1,
            "expected_total_size": 100,
            "minimum": ["file_count", "total_size"],
            "selection_hash": selection.selection_hash,
        },
        "postprocess": {
            "organizer_enabled": False,
            "media_refresh_enabled": False,
        },
    }
    plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
    return attach_plan_integrity(plan)


def _relay_plan() -> dict:
    plan = _plan(local_target=True)
    plan["destination"].update(
        {
            "provider_kind": "mobile",
            "account_id": "account-mobile",
            "account_revision": 1,
        }
    )
    plan["transfer"].update(
        {
            "strategy": "relay_upload_mobile",
            "executor": "mobile_relay",
        }
    )
    native_target = {
        "kind": "native",
        "namespace_id": "native:mobile:library-main",
        "path": "/mobile-library",
    }
    plan["paths"].update(
        {
            "destination_staging": dict(native_target),
            "destination_final": dict(native_target),
            "local_temp": {
                "kind": "relay",
                "namespace_id": "local:relay",
                "path": f"/data/relay/job-{JOB_ID}",
            },
        }
    )
    plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
    return attach_plan_integrity(plan)


def _mount_snapshot(
    mount_id: str,
    *,
    root: str,
    storage_identity_id: str,
    revision: int = 1,
    connection_revision: int = 1,
    identity_root: str = "/",
) -> dict:
    return {
        "mount_id": mount_id,
        "revision": revision,
        "connection_id": "ol",
        "connection_revision": connection_revision,
        "namespace_id": f"openlist:ol:{mount_id}",
        "enabled": True,
        "identity_status": "declared",
        "storage_identity_id": storage_identity_id,
        "identity_root": identity_root,
        "webdav_root": root,
        "readable": True,
        "writable": True,
    }


def _resolver(
    *,
    source: dict | None = None,
    destination: dict | None = None,
    connection: dict | None = None,
) -> RcloneLocatorResolver:
    mounts = {
        "src": source
        or _mount_snapshot(
            "src", root="/source-mount", storage_identity_id="storage:source"
        ),
        "dst": destination
        or _mount_snapshot(
            "dst", root="/destination-mount", storage_identity_id="storage:destination"
        ),
    }
    connection_snapshot = connection or {
        "connection_id": "ol",
        "revision": 1,
        "enabled": True,
        "credential_configured": True,
        "last_test_status": "ok",
    }
    resolver = RcloneLocatorResolver(
        {
            "remote_name": "OL",
            "config_path": "/config/rclone/rclone.conf",
            "local_target_root": "/storage-targets",
        }
    )
    resolver.update_config(
        {
            "mount_revision_resolver": lambda mount_id, _revision: mounts.get(mount_id),
            "connection_revision_resolver": (
                lambda _connection_id, _revision: connection_snapshot
            ),
        }
    )
    return resolver


class ManagedDescriptorTests(unittest.TestCase):
    def test_openlist_to_openlist_uses_one_managed_remote(self) -> None:
        run = build_managed_copy_run(_plan(), _resolver())

        self.assertEqual(run["source_remote"], "OL")
        self.assertEqual(run["source_root"], "/source-mount/_fnos_ingress")
        self.assertEqual(run["target_remote"], "OL")
        self.assertEqual(
            run["target_root"], "/destination-mount/_fnos_staging/movie"
        )
        self.assertEqual(run["job_dir_name"], f"job-{JOB_ID}")
        self.assertEqual(run["local_target"], "")

    def test_openlist_to_controlled_local_target(self) -> None:
        run = build_managed_copy_run(_plan(local_target=True), _resolver())

        self.assertEqual(run["source_remote"], "OL")
        self.assertEqual(run["target_remote"], "")
        self.assertEqual(run["target_root"], "/storage-targets/library")
        self.assertEqual(run["local_target"], "/storage-targets/library")

    def test_relay_download_uses_the_frozen_controlled_relay_root(self) -> None:
        run = build_managed_copy_run(_relay_plan(), _resolver())

        self.assertTrue(run["relay_mode"])
        self.assertEqual(run["source_remote"], "OL")
        self.assertEqual(run["target_remote"], "")
        self.assertEqual(run["target_root"], "/data/relay")
        self.assertEqual(run["local_target"], "/data/relay")

    def test_relay_path_outside_the_bound_root_is_rejected(self) -> None:
        plan = _relay_plan()
        plan["paths"]["local_temp"]["path"] = f"/tmp/relay/job-{JOB_ID}"

        with self.assertRaisesRegex(TransferError, "relay"):
            build_managed_copy_run(plan, _resolver())

    def test_non_managed_locator_has_no_execution_fallback(self) -> None:
        class _LegacyResolver:
            @staticmethod
            def remote(**_kwargs):
                return "LEGACY:/forged/job-7"

        with self.assertRaisesRegex(TransferError, "managed frozen-revision"):
            build_managed_copy_run(_plan(), _LegacyResolver())

    def test_mount_revision_mismatch_is_rejected(self) -> None:
        plan = _plan()
        plan["bindings"]["source_mount_revision"] = 2
        plan["paths"]["source_landing"]["mount_revision"] = 2

        with self.assertRaisesRegex(TransferError, "mount revision"):
            build_managed_copy_run(plan, _resolver())

    def test_connection_revision_mismatch_is_rejected(self) -> None:
        plan = _plan()
        plan["bindings"]["source_connection_revision"] = 2
        plan["paths"]["source_landing"]["connection_revision"] = 2

        with self.assertRaisesRegex(TransferError, "connection[_ ]revision"):
            build_managed_copy_run(plan, _resolver())

    def test_storage_identity_and_identity_root_mismatch_are_rejected(self) -> None:
        plan = _plan()
        plan["bindings"]["source_mount_storage_identity_id"] = "storage:forged"
        plan["paths"]["source_landing"]["storage_identity_id"] = "storage:forged"
        with self.assertRaisesRegex(TransferError, "storage identity"):
            build_managed_copy_run(plan, _resolver())

        plan = _plan(identity_root="/media")
        with self.assertRaisesRegex(TransferError, "identity_root"):
            build_managed_copy_run(plan, _resolver())

    def test_identity_root_equivalent_spellings_compare_after_normalization(self) -> None:
        plan = _plan(identity_root="media/")
        resolver = _resolver(
            source=_mount_snapshot(
                "src",
                root="/source-mount",
                storage_identity_id="storage:source",
                identity_root="/media",
            ),
            destination=_mount_snapshot(
                "dst",
                root="/destination-mount",
                storage_identity_id="storage:destination",
                identity_root="/media/",
            ),
        )

        run = build_managed_copy_run(plan, resolver)

        self.assertEqual(run["source_remote"], "OL")
        self.assertEqual(run["target_remote"], "OL")

    def test_runtime_config_update_preserves_frozen_revision_readers(self) -> None:
        resolver = _resolver()
        resolver.update_config(
            {
                "remote_name": "OL_RELOADED",
                "config_path": "/config/rclone/reloaded.conf",
            }
        )

        run = build_managed_copy_run(_plan(), resolver)

        self.assertEqual(run["source_remote"], "OL_RELOADED")
        self.assertEqual(run["target_remote"], "OL_RELOADED")

    def test_forged_remote_root_and_job_leaf_are_rejected(self) -> None:
        class _ForgedResolver:
            remote_name = "OL"

            @staticmethod
            def resolve_frozen_remote(**_kwargs):
                return "EVIL:/forged/job-7"

            @staticmethod
            def split_remote_path(value):
                remote, path = str(value).split(":", 1)
                return remote, path

        with self.assertRaisesRegex(TransferError, "非托管"):
            build_managed_copy_run(_plan(), _ForgedResolver())

        plan = _plan()
        plan["paths"]["source_landing"]["path"] = "/_fnos_ingress/job-99"
        with self.assertRaisesRegex(TransferError, "当前任务目录"):
            build_managed_copy_run(plan, _resolver())

        plan = _plan(local_target=True)
        plan["paths"]["destination_staging"]["path"] = (
            f"/storage-targets/../outside/job-{JOB_ID}"
        )
        with self.assertRaises(TransferError) as caught:
            build_managed_copy_run(plan, _resolver())
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_PLAN)


class _MemoryDatabase:
    def __init__(self, plan: dict) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE import_execution_plans (
                job_id INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                plan_json TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE transfer_executions (
                job_id INTEGER PRIMARY KEY,
                strategy TEXT NOT NULL,
                phase TEXT NOT NULL,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                receipt_json TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                resume_at TEXT,
                organizer_task_id INTEGER,
                staging_verified INTEGER NOT NULL DEFAULT 0,
                final_verified INTEGER NOT NULL DEFAULT 0,
                app_acked INTEGER NOT NULL DEFAULT 0,
                cleanup_json TEXT NOT NULL DEFAULT '{}',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                row_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE import_jobs (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                error_message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self.job = {
            "id": JOB_ID,
            "status": "waiting_transfer",
            "plan_version": 1,
            "category": "movie",
            "destination_id": "library-main",
            "raw_data": {},
        }
        self.connection.execute(
            "INSERT INTO import_jobs(id, status) VALUES (?, ?)",
            (JOB_ID, self.job["status"]),
        )
        self.connection.commit()
        TransferExecutionRepository(self).create(JOB_ID, plan)

    @contextmanager
    def connect(self):
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def get_job(self, job_id: int):
        return dict(self.job) if int(job_id) == JOB_ID else None


class DatabaseBoundDescriptorTests(unittest.TestCase):
    def _service(self, plan: dict) -> RcloneService:
        service = object.__new__(RcloneService)
        service.db = _MemoryDatabase(plan)
        service._v1_locator_resolver = _resolver()
        return service

    def test_exact_descriptor_matches_database_frozen_plan(self) -> None:
        plan = _plan()
        service = self._service(plan)
        requested = build_managed_copy_run(plan, service._v1_locator_resolver)

        bound, error = service._database_bound_staging_run(
            requested,
            submission_operation_key=plan["transfer"]["idempotency_key"],
        )

        self.assertEqual(error, "")
        self.assertEqual(
            service._copy_run_identity(bound), service._copy_run_identity(requested)
        )

    def test_submission_operation_key_must_match_frozen_plan(self) -> None:
        plan = _plan()
        service = self._service(plan)
        requested = build_managed_copy_run(plan, service._v1_locator_resolver)

        bound, error = service._database_bound_staging_run(
            requested,
            submission_operation_key="forged-operation-key",
        )

        self.assertEqual(bound, {})
        self.assertIn("operation_key differs", error)

    def test_forged_descriptor_never_replaces_database_descriptor(self) -> None:
        plan = _plan()
        service = self._service(plan)
        requested = build_managed_copy_run(plan, service._v1_locator_resolver)
        requested["source_root"] = "/forged/source"

        bound, error = service._database_bound_staging_run(
            requested,
            submission_operation_key=plan["transfer"]["idempotency_key"],
        )

        self.assertEqual(bound, {})
        self.assertIn("differs from its frozen V1 plan", error)


class ManagedRuntimeEnvironmentTests(unittest.TestCase):
    def test_shell_environment_uses_the_bound_locator_not_stale_runtime_remote(self) -> None:
        service = object.__new__(RcloneService)
        service.config = {
            "remote_name": "STALE",
            "config_path": "/stale/rclone.conf",
            "cache_dir": "/cache",
        }
        service._v1_locator_resolver = _resolver()
        run = build_managed_copy_run(_plan(), service._v1_locator_resolver)

        env = service._env(staging_run=run)

        self.assertEqual(env["REMOTE_NAME"], "OL")
        self.assertEqual(env["RCLONE_CONFIG_PATH"], "/config/rclone/rclone.conf")
        self.assertEqual(env["RCLONE_COPY_SOURCE_REMOTE"], "OL")
        self.assertEqual(env["RCLONE_COPY_TARGET_REMOTE"], "OL")

    def test_shell_contract_allows_only_the_bound_relay_root_in_relay_mode(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "fnos_rclone_worker.sh"
        ).read_text(encoding="utf-8")

        self.assertIn('RCLONE_COPY_RELAY_MODE="${RCLONE_COPY_RELAY_MODE:-false}"', script)
        self.assertIn('copy_relay_root="$(normalize_local_path "$RCLONE_COPY_RELAY_ROOT")"', script)
        self.assertIn('"$copy_relay_root"|"$copy_relay_root"/*', script)


if __name__ == "__main__":
    unittest.main()
