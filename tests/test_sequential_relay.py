from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fnos_media_import.transfer import OperationReceipt, TransferPlan
from fnos_media_import.transfer.orchestrator import TransferOrchestrator
from fnos_media_import.transfer.mobile_relay_executor import CmccMobileUploader
from fnos_media_import.transfer.provider_relay_executor import ProviderRelayExecutor
from fnos_media_import.transfer.relay_file_state import RelayFileState
from fnos_media_import.services.rclone_service import RcloneService
from tests.test_relay_uploader_file_selection import _FakeMobileUploader
from tests.test_transfer_v1 import make_plan
from tests.test_transfer_v1 import _FakeAck, _FakeCleanup, _FakeMaterializer, _EventLog


class _Downloads:
    supports_sequential_relay = True

    def __init__(self, files, events):
        self.files, self.events, self.runs = files, events, {}

    def bind(self, plan):
        self.plan = plan
        self.root = Path(plan.local_temp["path"])
        self.store = RelayFileState(str(self.root), job_id=plan.job_id, plan_key=plan.idempotency_key)

    def download(self, *, source, local_dir, idempotency_key):
        if idempotency_key in self.runs:
            return self.runs[idempotency_key]
        index = int(idempotency_key.split(":", 2)[1]) if idempotency_key.startswith("relay-file:") else 0
        state = self.store.initialize(list(self.files))
        relative = state["files"][index]["path"]
        cached = [path for path in self.root.rglob("*.mkv")]
        if cached:
            raise AssertionError(f"Next download started before eviction: {cached}")
        data = self.files[relative]
        self.store.prepare_file(index, len(data))
        self.events.append(("download", relative))
        target = Path(self.store.file_path(relative))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.store.mark_downloaded(index, len(data))
        receipt = OperationReceipt(
            f"run-{index}", "completed", "rclone",
            metadata={"run_id": str(index + 1), "operation_key": idempotency_key},
        )
        self.runs[idempotency_key] = receipt
        return receipt

    def check(self, receipt):
        return {"status": "completed"}

    def find_download(self, **kwargs):
        return self.runs.get(kwargs["idempotency_key"])


class SequentialRelayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="relay-sequence-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve() / "job-1"
        self.events = []
        self.files = {"season/01.mkv": b"first", "season/02.mkv": b"second"}
        self.native = _FakeMobileUploader()
        self.native.client.check_exists = lambda **kwargs: {"exist": True, "file_id": "root"}
        native_upload = self.native.upload_file

        def upload(**kwargs):
            self.events.append(("upload", kwargs["target_name"]))
            return native_upload(**kwargs)

        self.native.upload_file = upload
        self.uploader = CmccMobileUploader(self.native)
        self.adapter = _Downloads(self.files, self.events)
        self.plan = TransferPlan.from_dict(make_plan(
            dest_kind="mobile", strategy="relay_upload_provider", landing="/source/job-1", final="/target",
            local_temp=self.root.as_posix(), expected_file_count=2, expected_total_size=11,
        ))
        self.executor = self._executor()
        self.receipt = self.executor.submit()

    def _executor(self):
        return ProviderRelayExecutor(self.adapter, self.uploader, relay_root=self.root.parent.as_posix()).bind(self.plan)

    def test_two_files_strictly_interleave_and_final_verify_without_local_media(self):
        first = self.executor.poll(self.receipt)
        self.assertEqual(first.status, "running", first.message)
        self.assertEqual(len(self.native.calls), 1)
        self.assertFalse(list(self.root.rglob("*.mkv")))
        second = self.executor.poll(self.receipt)
        self.assertEqual(second.status, "completed", second.message)
        self.assertEqual(self.events, [
            ("download", "season/01.mkv"), ("upload", "01.mkv"),
            ("download", "season/02.mkv"), ("upload", "02.mkv"),
        ])
        self.assertEqual(self.receipt.metadata["operation_key"], f"relay-dl:{self.plan.idempotency_key}")
        self.assertEqual(self.executor.verify().status, "ok")
        self.assertFalse(list(self.root.rglob("*.mkv")))
        self.assertTrue(all(item["state"] == "cleaned" for item in self.adapter.store.load()["files"]))

    def test_verify_failure_retains_cache_and_does_not_download_next(self):
        read_remote = self.native.client.list_tree
        self.native.client.list_tree = lambda _root: []
        result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "failed", result.message)
        self.assertTrue((self.root / "season/01.mkv").exists())
        self.assertEqual(len(self.adapter.runs), 1)
        self.native.client.list_tree = read_remote
        result = self._executor().poll(self.receipt)
        self.assertEqual(result.status, "running", result.message)
        self.assertEqual(len(self.native.calls), 1, "A completed file fence must not re-upload")

    def test_verified_checkpoint_failure_never_unlinks_and_reuses_upload_receipt(self):
        original = self.executor._sequential.store.save

        def fail_verified(state):
            if state["files"][0]["state"] == "verified":
                raise OSError("injected persistence failure")
            original(state)

        with patch.object(self.executor._sequential.store, "save", side_effect=fail_verified):
            result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "failed")
        self.assertTrue((self.root / "season/01.mkv").exists())
        self.assertEqual(self.adapter.store.load()["files"][0]["state"], "downloaded")
        result = self._executor().poll(self.receipt)
        self.assertEqual(result.status, "running", result.message)
        self.assertEqual(len(self.native.calls), 1)

    def test_crash_after_unlink_resumes_without_download_or_upload(self):
        original = self.executor._sequential.store.save

        def fail_cleaned(state):
            if state["files"][0]["state"] == "cleaned":
                raise OSError("injected post-unlink crash")
            original(state)

        with patch.object(self.executor._sequential.store, "save", side_effect=fail_cleaned):
            result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "failed")
        self.assertFalse((self.root / "season/01.mkv").exists())
        self.assertEqual(self.adapter.store.load()["files"][0]["state"], "verified")
        result = self._executor().poll(self.receipt)
        self.assertEqual(result.status, "running", result.message)
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(len(self.adapter.runs), 1)

    def test_snapshot_restart_keeps_outer_fence_and_skips_cleaned_file(self):
        self.executor.poll(self.receipt)
        restored = OperationReceipt(**dataclasses.asdict(self.receipt))
        result = self._executor().poll(restored)
        self.assertEqual(result.status, "completed", result.message)
        self.assertEqual(len(self.native.calls), 2)
        self.assertEqual(restored.operation_id, self.receipt.operation_id)
        self.assertEqual(restored.metadata["operation_key"], self.receipt.metadata["operation_key"])

    def test_restart_with_stale_receipt_index_uses_cached_next_file_without_redownload(self):
        self.executor.poll(self.receipt)
        self.assertEqual(self.receipt.metadata["relay_file_index"], 0)

        # Simulate a worker that downloaded file index 1 after the app
        # persisted the outer receipt for index 0, then crashed before the
        # coordinator observed it.
        state = self.adapter.store.load()
        data = self.files[state["files"][1]["path"]]
        self.adapter.store.prepare_file(1, len(data))
        Path(self.adapter.store.file_path(state["files"][1]["path"])).write_bytes(data)
        self.adapter.store.mark_downloaded(1, len(data))

        restored = OperationReceipt(**dataclasses.asdict(self.receipt))
        result = self._executor().poll(restored)

        self.assertEqual(result.status, "completed", result.message)
        self.assertEqual(restored.metadata["relay_file_index"], 1)
        self.assertEqual(restored.metadata["operation_key"], self.receipt.metadata["operation_key"])
        self.assertEqual(
            [event for event in self.events if event[0] == "download"],
            [("download", "season/01.mkv")],
            "the cached next file must be uploaded without issuing another download",
        )
        self.assertEqual(len(self.native.calls), 2)

    def test_small_disk_stops_before_downloading_any_bytes(self):
        with patch("fnos_media_import.transfer.relay_file_state.shutil.disk_usage", return_value=SimpleNamespace(free=3)):
            result = self.executor.poll(self.receipt)
        self.assertEqual(result.status, "failed", result.message)
        self.assertEqual(self.events, [])
        self.assertFalse(list(self.root.rglob("*.mkv")))

    def test_changed_source_manifest_is_rejected_without_touching_files(self):
        self.executor.poll(self.receipt)
        with self.assertRaises(ValueError):
            self.adapter.store.initialize(["unrelated.mkv"])
        self.assertEqual(len(self.adapter.store.load()["files"]), 2)

    def test_state_rejects_out_of_order_and_path_escape(self):
        self.adapter.store.initialize(list(self.files))
        with self.assertRaises(ValueError):
            self.adapter.store.prepare_file(1, 6)
        for relative in ["../outside", "/outside", "x/../outside", ".fnos-relay-upload-sequential.json"]:
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.adapter.store.file_path(relative)

    def test_file_operation_key_remains_bound_to_complete_original_plan_key(self):
        self.assertEqual(RcloneService._plan_scoped_operation_key("relay-file:12:import:job-1"), "import:job-1")
        self.assertEqual(RcloneService._plan_scoped_operation_key("relay-file:0:import:job-1"), "relay-file:0:import:job-1")

    def test_full_chain_survives_restart_at_each_phase_and_only_then_acks_and_cleans(self):
        events = _EventLog()
        snapshot = None
        phases = []
        for _ in range(20):
            executor = self._executor()
            executor.source_materializer = _FakeMaterializer(events=events)
            orchestrator = TransferOrchestrator(
                self.plan.raw, {"relay_upload_provider": executor},
                ack_service=_FakeAck(events=events), cleanup=_FakeCleanup(events=events),
            )
            if snapshot:
                orchestrator.restore(snapshot)
            result = orchestrator.step()
            snapshot = orchestrator.snapshot()
            phases.append(result.phase)
            if result.phase == "completed":
                break
        self.assertEqual(phases[-1], "completed", snapshot)
        self.assertTrue(snapshot["final_verified"])
        self.assertTrue(snapshot["app_acked"])
        self.assertEqual(len(self.native.calls), 2)
        self.assertFalse(list(self.root.rglob("*.mkv")))
        self.assertLess(phases.index("target_verifying"), phases.index("ack_pending"))
        self.assertLess(events.names().index("ack"), events.names().index("cleanup"))

    def test_unknown_transfer_keeps_diagnostic_reason_in_manual_review(self):
        orchestrator = TransferOrchestrator(self.plan.raw, {"relay_upload_provider": self.executor})
        orchestrator.state.phase = "transferring"
        orchestrator.state.executor_strategy = "relay_upload_provider"
        orchestrator.state.receipt = self.receipt
        with patch.object(self.executor, "poll", return_value={
            "status": "unknown", "message": "Frozen file manifest changed; no local files were deleted",
            "error_code": "RESULT_UNKNOWN",
        }):
            result = orchestrator.step()
        self.assertEqual(result.phase, "manual_review")
        self.assertIn("manifest changed", result.message)

    def test_other_tasks_cache_waits_instead_of_consuming_disk_or_retry_budget(self):
        other = RelayFileState(str(self.root.parent / "job-2"), job_id=2, plan_key="other-plan")
        other.initialize(["other.mkv"])
        other.prepare_file(0, 3)
        Path(other.file_path("other.mkv")).write_bytes(b"two")
        other.mark_downloaded(0, 3)
        self.adapter.store.initialize(list(self.files))
        with self.assertRaises(BlockingIOError):
            self.adapter.store.prepare_file(0, 5)
        state = other.load()
        state["files"][0].update(
            state="verified", upload_key="confirmed",
            verification={"status": "ok", "file_count": 1, "total_size": 3},
        )
        other.save(state)
        with self.assertRaises(BlockingIOError):
            self.adapter.store.prepare_file(0, 5)
        Path(other.file_path("other.mkv")).unlink()
        state["files"][0]["state"] = "cleaned"
        other.save(state)
        self.adapter.store.prepare_file(0, 5)
        self.assertEqual(self.adapter.store.load()["files"][0]["size"], 5)


if __name__ == "__main__":
    unittest.main()
