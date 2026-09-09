"""OpenList 删除适配器与任务级清理回归测试。

2026-09-05 线上事故：relay 任务入库成功后任务显示失败。根因是组合根
（dispatch）构造 TransferCleanupService 时从未装配 OpenList 删除适配器，
source_landing（openlist 描述符）的清理永远返回「OpenList 删除适配器未
配置」的可重试失败，3 次重试耗尽后把已入库的任务标成 PHASE_FAILED。

本文件覆盖：
- OpenListFilesystemOps 单元契约（冻结挂载解析、幂等拼根、缺挂载拒绝、
  远端异常降级为可重试失败、目录不存在视为已清理）
- 真实 TransferCleanupService + 组合适配器对 relay 计划的清理结果
- 编排器端到端回归：relay 全链路使用真实清理服务必须到达 PHASE_COMPLETED
  （旧代码此处为 PHASE_FAILED），且远端 job-<id> 目录收到 DELETE
- dispatch 组合根装配契约
"""

from __future__ import annotations

import unittest
from pathlib import Path

from fnos_media_import.transfer import (
    PHASE_COMPLETED,
    PHASE_FAILED,
    VisibilityStatus,
)
from fnos_media_import.transfer.cleanup import (
    CompositeFilesystemOps,
    LocalFilesystemOps,
    OpenListFilesystemOps,
    TransferCleanupService,
)
from fnos_media_import.transfer.orchestrator import TransferOrchestrator
from fnos_media_import.transfer.provider_relay_executor import ProviderRelayExecutor
from fnos_media_import.transfer.verifier import TransferVerifier
from tests.test_transfer_v1 import (
    _EventLog,
    _FakeAck,
    _FakeDownloadAdapter,
    _FakeFinalizer,
    _FakeMaterializer,
    _FakeTargetLister,
    _FakeUploader,
    _drive,
    _visibility_poller,
    make_plan,
)


class _FakeWebDAVClient:
    def __init__(self, *, exists: bool = True, error: Exception | None = None) -> None:
        self.exists_flag = exists
        self.error = error
        self.exists_calls: list[str] = []
        self.deleted: list[str] = []

    def exists(self, path: str) -> bool:
        self.exists_calls.append(path)
        if self.error is not None:
            raise self.error
        return self.exists_flag

    def delete(self, path: str) -> None:
        if self.error is not None:
            raise self.error
        self.deleted.append(path)


class _FakeMountService:
    """冻结挂载解析的替身：只实现 client_for_mount / resolve_mount_absolute_path。"""

    def __init__(self, client: _FakeWebDAVClient, *, webdav_root: str = "/网盘") -> None:
        self.client = client
        self.webdav_root = webdav_root
        self.requested: list[tuple[str, int | None]] = []

    def client_for_mount(self, mount_id: str, *, revision: int | None = None):
        self.requested.append((mount_id, revision))
        return self.client, {"mount_id": mount_id, "webdav_root": self.webdav_root}

    def resolve_mount_absolute_path(self, mount, path) -> str:
        root = str((mount or {}).get("webdav_root") or "/")
        text = str(path or "").strip()
        if not text:
            return root
        if text == root or text.startswith(f"{root}/"):
            return text
        return f"{root}/{text.lstrip('/')}"


def _landing_descriptor(**overrides) -> dict:
    descriptor = {
        "kind": "openlist",
        "namespace_id": "openlist:ol:mount_quark",
        "path": "/_入库暂存/job-1",
        "mount_id": "mount_quark",
        "mount_revision": 3,
    }
    descriptor.update(overrides)
    return descriptor


class OpenListFilesystemOpsTests(unittest.TestCase):
    def test_deletes_resolved_absolute_path_with_frozen_revision(self) -> None:
        client = _FakeWebDAVClient()
        mount_service = _FakeMountService(client, webdav_root="/网盘")

        result = OpenListFilesystemOps(mount_service).delete_tree(_landing_descriptor())

        self.assertTrue(result["ok"])
        self.assertEqual(client.deleted, ["/网盘/_入库暂存/job-1"])
        self.assertEqual(mount_service.requested, [("mount_quark", 3)])

    def test_missing_remote_directory_is_treated_as_cleaned(self) -> None:
        client = _FakeWebDAVClient(exists=False)
        ops = OpenListFilesystemOps(_FakeMountService(client))

        result = ops.delete_tree(_landing_descriptor())

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(client.deleted, [])

    def test_missing_mount_info_is_refused(self) -> None:
        client = _FakeWebDAVClient()
        ops = OpenListFilesystemOps(_FakeMountService(client))

        for overrides in ({"mount_id": ""}, {"path": ""}):
            with self.subTest(overrides=overrides):
                result = ops.delete_tree(_landing_descriptor(**overrides))
                self.assertFalse(result["ok"])
                self.assertTrue(result["refused"])
        self.assertEqual(client.deleted, [])

    def test_remote_failure_is_retryable_not_refused(self) -> None:
        client = _FakeWebDAVClient(error=RuntimeError("OpenList 网关超时"))
        result = OpenListFilesystemOps(_FakeMountService(client)).delete_tree(_landing_descriptor())

        self.assertFalse(result["ok"])
        self.assertFalse(result.get("refused"))
        self.assertTrue(result["retryable"])
        self.assertIn("OpenList 远端目录删除失败", result["message"])

    def test_global_path_is_not_double_prefixed(self) -> None:
        client = _FakeWebDAVClient()
        mount_service = _FakeMountService(client, webdav_root="/网盘")

        result = OpenListFilesystemOps(mount_service).delete_tree(
            _landing_descriptor(path="/网盘/_入库暂存/job-1")
        )

        self.assertTrue(result["ok"])
        self.assertEqual(client.deleted, ["/网盘/_入库暂存/job-1"])


class TransferCleanupServiceOpenListTests(unittest.TestCase):
    def _service(self, client: _FakeWebDAVClient, *, relay_root: str) -> TransferCleanupService:
        return TransferCleanupService(
            CompositeFilesystemOps(
                openlist_ops=OpenListFilesystemOps(_FakeMountService(client)),
                local_ops=LocalFilesystemOps((relay_root,)),
            ),
            local_roots=(relay_root,),
            relay_root=relay_root,
        )

    def test_relay_plan_landing_is_deleted_remotely(self) -> None:
        client = _FakeWebDAVClient()
        plan = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_入库暂存/job-1",
            final="/移动云/电影",
            local_temp="/nonexistent-relay/job-1",
        )

        result = self._service(client, relay_root="/nonexistent-relay").cleanup(
            plan, target_verified=True, app_acked=True
        )

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(client.deleted, ["/网盘/_入库暂存/job-1"])

    def test_cleanup_skips_before_verify_and_ack(self) -> None:
        client = _FakeWebDAVClient()
        plan = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_入库暂存/job-1",
            final="/移动云/电影",
            local_temp="/nonexistent-relay/job-1",
        )

        result = self._service(client, relay_root="/nonexistent-relay").cleanup(
            plan, target_verified=False, app_acked=False
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.skipped)
        self.assertEqual(client.deleted, [])


class RelayCompletedWithRealCleanupTests(unittest.TestCase):
    """端到端回归：relay 链路必须带着真实清理服务走完并保持 completed。"""

    def test_relay_full_flow_completes_and_deletes_landing(self) -> None:
        events = _EventLog()
        client = _FakeWebDAVClient()
        mount_service = _FakeMountService(client, webdav_root="/网盘")
        relay_root = "/nonexistent-relay"
        cleanup = TransferCleanupService(
            CompositeFilesystemOps(
                openlist_ops=OpenListFilesystemOps(mount_service),
                local_ops=LocalFilesystemOps((relay_root,)),
            ),
            local_roots=(relay_root,),
            relay_root=relay_root,
        )
        plan = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_入库暂存/job-1",
            final="/移动云/电影",
            local_temp=f"{relay_root}/job-1",
        )
        executor = ProviderRelayExecutor(
            _FakeDownloadAdapter(events=events),
            _FakeUploader(events=events),
            source_materializer=_FakeMaterializer(events=events),
            visibility_poller=_visibility_poller(VisibilityStatus.visible, events),
            relay_root=relay_root,
        )
        orch = TransferOrchestrator(
            plan,
            {"relay_upload_provider": executor},
            verifier=TransferVerifier(target_lister=_FakeTargetLister(events=events)),
            cleanup=cleanup,
            ack_service=_FakeAck(events=events),
            finalizer_service=_FakeFinalizer(),
        )

        _drive(orch)

        self.assertEqual(orch.state.phase, PHASE_COMPLETED)
        self.assertEqual(client.deleted, ["/网盘/_入库暂存/job-1"])

    def test_missing_adapter_exhausts_retries_and_fails_the_job(self) -> None:
        """旧组合根（无 OpenList 适配器）的故障形态留档：入库成功但任务失败。"""

        events = _EventLog()
        relay_root = "/nonexistent-relay"
        cleanup = TransferCleanupService(
            CompositeFilesystemOps(local_ops=LocalFilesystemOps((relay_root,))),
            local_roots=(relay_root,),
            relay_root=relay_root,
        )
        plan = make_plan(
            source_kind="quark",
            dest_kind="mobile",
            strategy="relay_upload_provider",
            landing="/_入库暂存/job-1",
            final="/移动云/电影",
            local_temp=f"{relay_root}/job-1",
        )
        executor = ProviderRelayExecutor(
            _FakeDownloadAdapter(events=events),
            _FakeUploader(events=events),
            source_materializer=_FakeMaterializer(events=events),
            visibility_poller=_visibility_poller(VisibilityStatus.visible, events),
            relay_root=relay_root,
        )
        orch = TransferOrchestrator(
            plan,
            {"relay_upload_provider": executor},
            verifier=TransferVerifier(target_lister=_FakeTargetLister(events=events)),
            cleanup=cleanup,
            ack_service=_FakeAck(events=events),
            finalizer_service=_FakeFinalizer(),
        )

        _drive(orch, rounds=200)

        self.assertEqual(orch.state.phase, PHASE_FAILED)
        self.assertIn("清理失败且无法继续重试", orch.state.message)
        items = (orch.state.cleanup or {}).get("items") or []
        self.assertTrue(
            any("OpenList 删除适配器未配置" in str(item.get("message") or "") for item in items)
        )


class CleanupWiringContractTests(unittest.TestCase):
    def test_dispatch_composition_root_binds_openlist_ops(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "fnos_media_import"
            / "transfer"
            / "dispatch.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "openlist_ops=OpenListFilesystemOps(mount_service)",
            source,
        )


if __name__ == "__main__":
    unittest.main()
