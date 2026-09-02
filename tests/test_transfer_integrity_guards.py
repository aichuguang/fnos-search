from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import os
import ntpath
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fnos_media_import.transfer import TransferPlan
from fnos_media_import.transfer.finalizer import TransferFinalizer
from fnos_media_import.transfer.verifier import ListedFiles, TransferVerifier


class TransferIntegrityGuardTests(unittest.TestCase):
    def test_empty_expectation_does_not_pass_without_explicit_contract(self) -> None:
        verifier = TransferVerifier(
            target_lister=lambda _descriptor: ListedFiles(exists=True, file_count=0, total_size=0)
        )
        result = verifier.verify(
            descriptor={"kind": "openlist", "namespace_id": "openlist:x", "path": "/job-1"},
            expected_file_count=0,
            expected_total_size=0,
        )
        self.assertEqual(result.status, "unknown")

    def test_explicit_empty_folder_contract_is_exact(self) -> None:
        verifier = TransferVerifier(
            target_lister=lambda _descriptor: ListedFiles(exists=True, file_count=0, total_size=0)
        )
        result = verifier.verify(
            descriptor={"kind": "openlist", "namespace_id": "openlist:x", "path": "/job-1"},
            expected_file_count=0,
            expected_total_size=0,
            allow_empty=True,
        )
        self.assertEqual(result.status, "ok")

    def test_unknown_counts_with_require_non_empty_passes_on_landed_files(self) -> None:
        # 访客整分享提交冻结数量为 0/0（数量未知）。Organizer 已把整理产物
        # 落到最终目录后，降级校验只需目录存在且至少一个文件。
        verifier = TransferVerifier(
            target_lister=lambda _descriptor: ListedFiles(
                exists=True, file_count=1, total_size=22425567232
            )
        )
        result = verifier.verify(
            descriptor={"kind": "openlist", "namespace_id": "openlist:x", "path": "/电影"},
            expected_file_count=0,
            expected_total_size=0,
            require_non_empty=True,
        )
        self.assertEqual(result.status, "ok")

    def test_unknown_counts_with_require_non_empty_fails_on_empty_target(self) -> None:
        verifier = TransferVerifier(
            target_lister=lambda _descriptor: ListedFiles(exists=True, file_count=0, total_size=0)
        )
        result = verifier.verify(
            descriptor={"kind": "openlist", "namespace_id": "openlist:x", "path": "/电影"},
            expected_file_count=0,
            expected_total_size=0,
            require_non_empty=True,
        )
        self.assertEqual(result.status, "missing")
        self.assertEqual(result.code, "VERIFY_MISMATCH")

    def test_local_finalize_reconciles_move_completed_before_snapshot(self) -> None:
        root = _writable_temp_dir(self)
        if root is None:
            return
        staging = Path(root) / "job-1" / "staging"
        final = Path(root) / "job-1" / "final"
        try:
            final.mkdir(parents=True)
            final_file = final / "file.bin"
            final_file.write_bytes(b"done")
        except PermissionError as exc:
            self.skipTest(f"filesystem ACL prevents nested test directory: {exc}")
        # The worker crashed after moving the directory.  The staging path
        # is absent while the final path is already present.
        result = TransferFinalizer(object(), local_root=root).finalize(
            TransferPlan.from_dict(_plan(staging=str(staging), final=str(final)))
        )
        self.assertEqual(result["status"], "done")
        self.assertTrue(result.get("reconciled"))
        self.assertTrue(final_file.exists())

    def test_local_finalize_rejects_paths_outside_root(self) -> None:
        root = _writable_temp_dir(self)
        outside = _writable_temp_dir(self)
        if root is None or outside is None:
            return
        with self.assertRaises(ValueError):
            TransferFinalizer(object(), local_root=root).finalize(
                TransferPlan.from_dict(
                    _plan(
                        staging=os.path.join(outside, "job-1"),
                        final=os.path.join(root, "job-1", "final"),
                    )
                )
            )

    def test_transfer_plan_preserves_windows_local_absolute_paths(self) -> None:
        staging = r"C:\storage-targets\job-1\staging"
        final = r"C:\storage-targets\job-1\final"

        plan = TransferPlan.from_dict(_plan(staging=staging, final=final))

        self.assertEqual(plan.destination_staging["path"], ntpath.normpath(staging))
        self.assertEqual(plan.destination_final["path"], ntpath.normpath(final))

    def test_transfer_plan_preserves_frozen_openlist_identity(self) -> None:
        plan = TransferPlan.from_dict(_openlist_plan())

        self.assertEqual(plan.destination_staging["mount_id"], "mount_target")
        self.assertEqual(plan.destination_staging["mount_revision"], 4)
        self.assertEqual(plan.destination_staging["connection_revision"], 3)
        self.assertEqual(plan.destination_final["storage_identity_id"], "storage:target")

    def test_openlist_finalize_uses_frozen_revision_after_current_config_changes(self) -> None:
        raw = _openlist_plan()
        plan = TransferPlan.from_dict(raw)
        mounts = _FrozenMountService(
            {
                "mount_id": "mount_target",
                "revision": 4,
                "namespace_id": "openlist:ol_main:mount_target",
                "connection_id": "ol_main",
                "connection_revision": 3,
                "storage_identity_id": "storage:target",
                "identity_root": "/media",
                "enabled": True,
                "writable": True,
                "identity_status": "verified",
            },
            source_exists=True,
            destination_exists=False,
        )
        finalizer = TransferFinalizer(
            mounts,
            local_root="/storage-targets",
            execution_repository=_ExecutionRepository(raw),
        )

        result = finalizer.finalize(plan)

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["mount_revision"], 4)
        self.assertEqual(
            mounts.calls,
            [
                ("get_revision", "mount_target", 4),
                ("list_path", "mount_target", "/_fnos_staging/movie/job-1", 4),
                ("list_path", "mount_target", "/media/movie/job-1", 4),
                (
                    "move_path",
                    "mount_target",
                    "/_fnos_staging/movie/job-1",
                    "/media/movie/job-1",
                    4,
                ),
            ],
        )

    def test_openlist_finalize_reconciles_completed_frozen_move(self) -> None:
        raw = _openlist_plan()
        mounts = _FrozenMountService(
            {
                "mount_id": "mount_target",
                "revision": 4,
                "namespace_id": "openlist:ol_main:mount_target",
                "connection_id": "ol_main",
                "connection_revision": 3,
                "storage_identity_id": "storage:target",
                "identity_root": "/media",
                "enabled": True,
                "writable": True,
                "identity_status": "verified",
            },
            source_exists=False,
            destination_exists=True,
        )

        result = TransferFinalizer(
            mounts,
            local_root="/storage-targets",
            execution_repository=_ExecutionRepository(raw),
        ).finalize(TransferPlan.from_dict(raw))

        self.assertEqual(result["status"], "done")
        self.assertTrue(result["reconciled"])
        self.assertFalse(any(call[0] == "move_path" for call in mounts.calls))

    def test_repository_mismatch_blocks_finalize_before_storage_access(self) -> None:
        raw = _openlist_plan()
        forged = _openlist_plan()
        forged["paths"]["destination_final"]["path"] = "/media/forged/job-1"
        mounts = _FrozenMountService({}, source_exists=True, destination_exists=False)

        result = TransferFinalizer(
            mounts,
            local_root="/storage-targets",
            execution_repository=_ExecutionRepository(raw),
        ).finalize(TransferPlan.from_dict(forged))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "PLAN_SNAPSHOT_MISMATCH")
        self.assertEqual(mounts.calls, [])


def _plan(*, staging: str, final: str) -> dict:
    return {
        "version": 1,
        "job_id": 1,
        "source": {"provider_kind": "quark"},
        "destination": {"id": "local", "provider_kind": "local"},
        "transfer": {"strategy": "rclone_copy"},
        "paths": {
            "source_landing": {
                "kind": "openlist",
                "namespace_id": "openlist:source:mount",
                "path": "/_fnos_ingress/job-1",
            },
            "destination_staging": {"kind": "local", "namespace_id": "local:root", "path": staging},
            "destination_final": {"kind": "local", "namespace_id": "local:root", "path": final},
        },
    }


def _openlist_plan() -> dict:
    namespace = "openlist:ol_main:mount_target"
    identity = {
        "kind": "openlist",
        "namespace_id": namespace,
        "mount_id": "mount_target",
        "mount_revision": 4,
        "connection_id": "ol_main",
        "connection_revision": 3,
        "storage_identity_id": "storage:target",
        "identity_root": "/media",
    }
    return {
        "version": 1,
        "job_id": 1,
        "source": {"provider_kind": "quark"},
        "destination": {"id": "target", "provider_kind": "quark"},
        "transfer": {"strategy": "rclone_copy"},
        "bindings": {
            "destination_mount_id": "mount_target",
            "destination_mount_revision": 4,
            "destination_mount_namespace_id": namespace,
            "destination_connection_id": "ol_main",
            "destination_connection_revision": 3,
            "destination_mount_storage_identity_id": "storage:target",
            "destination_mount_identity_root": "/media",
        },
        "paths": {
            "source_landing": {
                "kind": "openlist",
                "namespace_id": "openlist:ol_source:mount_source",
                "path": "/_fnos_ingress/job-1",
            },
            "destination_staging": {
                **identity,
                "path": "/_fnos_staging/movie/job-1",
            },
            "destination_final": {
                **identity,
                "path": "/media/movie/job-1",
            },
        },
    }


class _ExecutionRepository(RepositoryFakeMixin):
    def __init__(self, plan: dict) -> None:
        self._plan = plan

    def plan(self, _job_id: int) -> dict:
        return self._plan

    @staticmethod
    def get(_job_id: int):
        return SimpleNamespace(strategy="rclone_copy")


class _FrozenMountService(RepositoryFakeMixin):
    def __init__(
        self,
        revision: dict,
        *,
        source_exists: bool,
        destination_exists: bool,
    ) -> None:
        self.revision = revision
        self.source_exists = source_exists
        self.destination_exists = destination_exists
        self.calls: list[tuple] = []

    def get_revision(self, mount_id: str, revision: int) -> dict | None:
        self.calls.append(("get_revision", mount_id, revision))
        return dict(self.revision) if self.revision else None

    def get_mount(self, _mount_id: str):  # pragma: no cover - guard against regressions
        raise AssertionError("finalizer must not read the current mount revision")

    def list_path(self, mount_id: str, path: str, *, revision: int | None = None) -> dict:
        self.calls.append(("list_path", mount_id, path, revision))
        if "_fnos_staging" in path:
            return {"exists": self.source_exists}
        return {"exists": self.destination_exists}

    def move_path(
        self,
        mount_id: str,
        source: str,
        destination: str,
        *,
        revision: int | None = None,
    ) -> dict:
        self.calls.append(("move_path", mount_id, source, destination, revision))
        return {"status": "done", "source": source, "destination": destination}


def _writable_temp_dir(test_case: unittest.TestCase) -> str | None:
    try:
        root = tempfile.mkdtemp(dir=Path(__file__).parent)
        Path(root, "probe").mkdir()
    except PermissionError as exc:
        test_case.skipTest(f"filesystem ACL prevents test setup: {exc}")
        return None
    test_case.addCleanup(shutil.rmtree, root, ignore_errors=True)
    return root


if __name__ == "__main__":
    unittest.main()
