from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.database import Database
from fnos_media_import.planning.job_plan import attach_plan_integrity, compute_operation_key
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.repositories.repository_set import RcloneRepositories
from fnos_media_import.services.callback_service import CallbackDependencies, RcloneCallbackService
from fnos_media_import.services.rclone_run_completion_service import RcloneRunCompletionService
from fnos_media_import.services.rclone_run_state import RcloneRunState
from fnos_media_import.services.rclone_service import RcloneService
from fnos_media_import.transfer import TransferOrchestrator, TransferPlan
from fnos_media_import.transfer.cleanup import TransferCleanupService
from fnos_media_import.transfer.dispatch import TransferDispatchService, _JobAckService
from fnos_media_import.transfer.execution_repository import TransferExecutionRepository
from fnos_media_import.transfer.finalizer import TransferFinalizer
from fnos_media_import.transfer.verifier import TransferVerifier
from tests.test_rclone_managed_descriptor_v1 import _plan, _resolver


class RcloneStagingManifestVerificationTests(unittest.TestCase):
    """Real SQLite repositories/callback/completion; only external I/O is fake."""

    def _prepare(self, *, expected_count=0, expected_size=0, organizer=True):
        temporary = tempfile.TemporaryDirectory(prefix="fnos-staging-manifest-")
        self.addCleanup(temporary.cleanup)
        database = Database(Path(temporary.name) / "isolated.sqlite")
        with patch("fnos_media_import.database.logger.warning"):
            database.init_schema()
        self.repos = RepositorySet.from_database(database)
        self.job_id, _ = self.repos.job_commands.create({
            "title": "Isolated movie", "category": "movie", "category_label": "Movie",
            "source_provider_kind": "quark", "source_form": "share", "source_url": "source-record:test",
            "status": "waiting_transfer", "destination_id": "library-main", "plan_version": 1, "raw_data": {},
        })
        raw = json.loads(json.dumps(_plan()).replace("job-7", f"job-{self.job_id}"))
        raw["job_id"] = self.job_id
        raw["verification"].update(expected_file_count=expected_count, expected_total_size=expected_size)
        raw["postprocess"]["organizer_enabled"] = organizer
        if organizer:
            raw["paths"]["organizer"] = dict(raw["paths"]["destination_staging"])
            raw["paths"]["organizer_final"] = dict(raw["paths"]["destination_final"])
            for key, value in list(raw["bindings"].items()):
                if key.startswith("destination_"):
                    raw["bindings"][key.replace("destination_", "organizer_", 1)] = value
        raw["transfer"]["idempotency_key"] = compute_operation_key(raw)
        attach_plan_integrity(raw)
        self.plan = TransferPlan.from_dict(raw)
        self.executions = TransferExecutionRepository(self.repos)
        self.executions.create(self.job_id, raw, phase="transferring")
        resolver = _resolver()
        service = object.__new__(RcloneService)
        service.db = RcloneRepositories.from_set(self.repos)
        service._v1_locator_resolver = resolver
        service.start = Mock(return_value={"success": True, "queued": True})
        self.dispatch = object.__new__(TransferDispatchService)
        self.dispatch.database = self.repos
        self.dispatch.executions = self.executions
        self.dispatch.rclone_service = service
        self.dispatch.locator_resolver = resolver
        self.dispatch.registry = SimpleNamespace(get=lambda kind: object())
        frozen = {
            **self.plan.destination_staging, "enabled": True, "writable": True,
            "identity_status": "verified", "webdav_root": "/destination-mount",
        }
        self.manifest_reads = []
        self.directory_reads = []
        self.on_list = lambda: None
        self.actual = [("Movie.mkv", 100), ("Movie.srt", 20)]
        self.location = "destination_staging"

        def list_dir(path):
            self.manifest_reads.append(path)
            self.on_list()
            return [{"path": path + "/" + name, "name": name, "size": size, "is_dir": False} for name, size in self.actual]

        def list_path(*args, **kwargs):
            self.directory_reads.append(args)
            exists = args[1] == self.plan.paths[self.location]["path"]
            return {"exists": exists, "file_count": len(self.actual), "total_size": sum(size for _, size in self.actual)}

        def move_path(mount_id, source, target, *, revision):
            self.assertEqual(source, self.plan.destination_staging["path"])
            self.assertEqual(target, self.plan.destination_final["path"])
            self.location = "destination_final"
            return {"status": "done"}

        webdav = SimpleNamespace(exists=lambda path: True, list_dir=list_dir)
        self.dispatch.mount_service = SimpleNamespace(
            get_revision=lambda *args: frozen,
            client_for_mount=lambda *args, **kwargs: (webdav, frozen),
            resolve_mount_absolute_path=lambda mount, path: "/destination-mount/" + path.lstrip("/"),
            list_path=list_path,
            move_path=move_path,
        )
        self.dispatch.verifier = TransferVerifier(self.dispatch._list_target)
        self.executor = self.dispatch.build_executors(raw)["rclone_copy"].bind(self.plan)
        self.receipt = self.executor.submit()
        self.run_id = int(self.receipt.metadata["run_id"])
        self.repos.rclone.mark_scoped_rclone_run_started(self.run_id)
        self.copy_run = self.executor.build_copy_run()
        source_root = self.copy_run["source_root"] + "/" + self.copy_run["job_dir_name"]
        callback = RcloneCallbackService(CallbackDependencies(
            db=self.repos, rclone=service, safe_int=lambda value, default, minimum, maximum: int(value or default),
            callback_level=lambda status: "info", cancelled_status="cancelled", execution_repository=self.executions,
        ))
        response, status = callback.handle({
            "run_id": self.run_id, "job_id": self.job_id, "status": "staging_manifest", "category": "movie",
            "source_path": self.copy_run["source_root"], "target_path": self.copy_run["target_root"],
            "manifest_paths": [source_root + "/" + name for name, _ in self.actual],
        })
        self.assertEqual(status, 200)
        self.assertTrue(response["success"])
        completion = RcloneRunCompletionService(
            database=service.db, state=RcloneRunState(), state_lock=threading.Lock(), log=lambda line: None, now=lambda: "now",
        )
        completion.persist_result(self.run_id, 0, "")
        self.base_manifest = self.repos.job_queries.get(self.job_id)["raw_data"]["rclone_staging_manifest"]

    def _orchestrator(self, *, phase="target_verifying", receipt=None):
        orchestrator = TransferOrchestrator(
            self.plan, {"rclone_copy": self.executor}, verifier=self.dispatch.verifier, organizer_service=SimpleNamespace(),
        )
        orchestrator.state.phase = phase
        orchestrator.state.executor_strategy = "rclone_copy"
        orchestrator.state.receipt = receipt or copy.deepcopy(self.receipt)
        return orchestrator

    def test_successful_copy_with_unknown_counts_restores_then_enters_organizer(self):
        self._prepare()
        original = self._orchestrator(phase="transferring")
        self.assertEqual(original.step().phase, "target_verifying")
        self.executions.save(self.executions.get(self.job_id), original.snapshot(), phase="target_verifying")
        restored = self._orchestrator().restore(self.executions.get(self.job_id).snapshot)
        self.assertEqual(restored.step().phase, "organizing")
        self.assertTrue(restored.state.staging_verified)
        self.assertFalse(restored.state.app_acked)
        self.assertEqual(len(self.manifest_reads), 1)
        self.assertEqual(self.directory_reads, [])

    def test_missing_manifest_file_cannot_be_replaced_by_an_unrelated_file(self):
        self._prepare()
        self.actual = [("Movie.mkv", 100), ("unrelated.srt", 20)]
        orchestrator = self._orchestrator()
        self.assertEqual(orchestrator.step().phase, "retry_wait")
        self.assertFalse(orchestrator.state.staging_verified)
        self.assertIn("Movie.srt", orchestrator.state.message)

    def test_missing_mismatched_or_untrusted_evidence_never_lists_the_target(self):
        self._prepare()
        original_descriptor = self.repos.rclone.get_rclone_run(self.run_id)["copy_descriptor"]
        cases = (
            "missing", "wrong_run", "wrong_root", "outside_path", "duplicate_path", "wrong_count",
            "failed_run", "cancelled_run", "wrong_operation", "wrong_receipt_job", "wrong_descriptor", "cancelled_job",
        )
        for case in cases:
            with self.subTest(case=case):
                manifest = copy.deepcopy(self.base_manifest)
                receipt = copy.deepcopy(self.receipt)
                descriptor = copy.deepcopy(original_descriptor)
                run_status = "success"
                job_status = "waiting_transfer"
                if case == "missing":
                    manifest = None
                elif case == "wrong_run":
                    manifest["last_run_id"] += 100
                elif case == "wrong_root":
                    manifest["job_root"] += "-other"
                elif case == "outside_path":
                    manifest["source_paths"][0] = "/other/job-999/Movie.mkv"
                elif case == "duplicate_path":
                    manifest["source_paths"][1] = manifest["source_paths"][0]
                elif case == "wrong_count":
                    manifest["expected_file_count"] += 1
                elif case in {"failed_run", "cancelled_run"}:
                    run_status = case.removesuffix("_run")
                elif case == "wrong_operation":
                    receipt.metadata["operation_key"] = "wrong-operation"
                elif case == "wrong_receipt_job":
                    receipt.metadata["job_id"] += 100
                elif case == "wrong_descriptor":
                    descriptor["target_root"] += "-other"
                elif case == "cancelled_job":
                    job_status = "cancelled"
                self.repos.job_commands.update(self.job_id, status=job_status, raw_data={"rclone_staging_manifest": manifest})
                with self.repos.connect() as connection:
                    connection.execute(
                        "UPDATE rclone_runs SET status=?, copy_descriptor_json=? WHERE id=?",
                        (run_status, json.dumps(descriptor), self.run_id),
                    )
                self.assertEqual(self._orchestrator(receipt=receipt).step().phase, "manual_review")
                self.assertEqual(self.manifest_reads, [])

    def test_run_success_is_rechecked_after_reading_the_target(self):
        self._prepare()
        self.on_list = lambda: self.repos.rclone.mark_scoped_rclone_run_status(self.run_id, "cancelled", terminal=True)
        orchestrator = self._orchestrator()
        self.assertEqual(orchestrator.step().phase, "manual_review")
        self.assertFalse(orchestrator.state.staging_verified)

    def test_frozen_count_and_size_contract_still_uses_exact_directory_totals(self):
        self._prepare(expected_count=2, expected_size=120)
        self.repos.job_commands.update(self.job_id, raw_data={})
        self.assertEqual(self._orchestrator().step().phase, "organizing")
        self.actual.append(("extra.txt", 0))
        self.assertEqual(self._orchestrator().step().phase, "retry_wait")
        self.actual = [("Movie.mkv", 101), ("Movie.srt", 20)]
        self.assertEqual(self._orchestrator().step().phase, "retry_wait")
        self.assertEqual(self.manifest_reads, [])
        self.assertEqual(len(self.directory_reads), 3)

    def test_non_organizer_copy_restores_through_final_verification_ack_and_cleanup(self):
        self._prepare(organizer=False)
        deleted = []
        filesystem = SimpleNamespace(
            resolve_real=lambda descriptor: descriptor["path"],
            delete_tree=lambda descriptor: deleted.append(descriptor["path"]) or {"ok": True},
        )
        cleanup = TransferCleanupService(filesystem)
        finalizer = TransferFinalizer(
            self.dispatch.mount_service, local_root="/storage-targets", execution_repository=self.executions,
        )
        snapshot = self._orchestrator(phase="transferring").snapshot()
        phases = []
        for _ in range(6):
            orchestrator = TransferOrchestrator(
                self.plan, {"rclone_copy": self.executor}, verifier=self.dispatch.verifier,
                finalizer_service=finalizer, ack_service=_JobAckService(self.repos, self.executions), cleanup=cleanup,
            ).restore(snapshot)
            result = orchestrator.step()
            snapshot = orchestrator.snapshot()
            self.executions.save(
                self.executions.get(self.job_id), snapshot, phase=result.phase,
                staging_verified=snapshot["staging_verified"], final_verified=snapshot["final_verified"], app_acked=snapshot["app_acked"],
            )
            phases.append(result.phase)
        self.assertEqual(phases, ["target_verifying", "finalizing", "final_verifying", "ack_pending", "cleanup_pending", "completed"])
        self.assertEqual(deleted, [self.plan.source_landing["path"]])
        self.assertEqual(self.manifest_reads, [
            "/destination-mount" + self.plan.destination_staging["path"],
            "/destination-mount" + self.plan.destination_final["path"],
        ])
        self.assertTrue(self.executions.get(self.job_id).app_acked)

    def test_non_organizer_copy_without_manifest_still_fails_closed(self):
        self._prepare(organizer=False)
        self.repos.job_commands.update(self.job_id, raw_data={})
        self.assertEqual(self._orchestrator().step().phase, "manual_review")
        self.assertEqual(self.manifest_reads, [])


if __name__ == "__main__":
    unittest.main()
