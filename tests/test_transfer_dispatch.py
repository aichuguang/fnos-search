"""TransferDispatchService 装配测试：策略 → 执行器映射。"""

from __future__ import annotations

import unittest

from fnos_media_import.transfer.dispatch import TransferDispatchService
from fnos_media_import.planning import DefaultAdapterRegistry


class _FakeRegistry:
    def __init__(self, adapters: dict[str, object] | None = None) -> None:
        self._adapters = adapters or {}

    def get(self, provider_kind: str) -> object:
        return self._adapters[provider_kind]


def _plan(strategy: str) -> dict:
    return {
        "version": 4,
        "job_id": 1,
        "source": {"provider_kind": "quark", "source_form": "share", "account_id": "a", "account_revision": 1, "account_selection_revision": 1},
        "destination": {"id": "d", "provider_kind": "mobile", "target_revision": 1, "account_id": "a", "account_revision": 1, "account_selection_revision": 1},
        "transfer": {"strategy": strategy, "executor": "rclone", "idempotency_key": "k", "delete_source_after_ack": True},
        "paths": {
            "source_landing": {"namespace_id": "openlist:c:s", "path": "/_fnos_ingress/job-1"},
            "destination_staging": {"namespace_id": "openlist:c:d", "path": "/_fnos_staging/movie/job-1"},
            "destination_final": {"namespace_id": "openlist:c:d", "path": "/电影"},
            "organizer": None,
            "local_temp": None,
        },
    }


class TransferDispatchTests(unittest.TestCase):
    def _service(self) -> TransferDispatchService:
        return TransferDispatchService(
            accounts_service=object(),
            target_service=object(),
            mount_service=object(),
            registry=_FakeRegistry({"quark": object(), "mobile": object()}),
            rclone_service=object(),
            locator_resolver=object(),
        )

    def test_native_strategy_builds_native_executor(self) -> None:
        executors = self._service().build_executors(_plan("native_save"))
        self.assertIn("native_save", executors)

    def test_offline_strategy_builds_native_executor(self) -> None:
        executors = self._service().build_executors(_plan("native_offline"))
        self.assertIn("native_offline", executors)

    def test_rclone_strategy_builds_rclone_executor(self) -> None:
        executors = self._service().build_executors(_plan("rclone_copy"))
        self.assertIn("rclone_copy", executors)

    def test_relay_strategy_builds_mobile_relay_executor(self) -> None:
        executors = self._service().build_executors(_plan("relay_upload_mobile"))
        self.assertIn("relay_upload_mobile", executors)

    def test_unknown_strategy_yields_no_executor(self) -> None:
        executors = self._service().build_executors(_plan("manual_review"))
        self.assertEqual(executors, {})

    def test_dispatch_rejects_non_v4_plan(self) -> None:
        service = self._service()
        job = {"raw_data": {"execution_plan": {"version": 3}}}
        with self.assertRaises(Exception):
            service.dispatch(job)


if __name__ == "__main__":
    unittest.main()
