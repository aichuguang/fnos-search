from __future__ import annotations

import unittest

from fnos_media_import.transfer.dispatch import TransferDispatchError, TransferDispatchService


class _Adapter:
    def relay_uploader_for(self, _account_id: str, _revision: int):
        return object()


class _Registry:
    def __init__(self) -> None:
        self.adapters = {"quark": _Adapter(), "mobile": _Adapter()}

    def get(self, kind: str):
        return self.adapters[kind]


def _plan(strategy: str) -> dict:
    return {
        "version": 1,
        "job_id": 1,
        "source": {"provider_kind": "quark", "source_form": "share"},
        "destination": {
            "id": "mobile_main",
            "provider_kind": "mobile",
            "account_id": "acc_mobile",
            "account_revision": 1,
        },
        "transfer": {"strategy": strategy, "idempotency_key": "v1:key"},
        "paths": {},
    }


class TransferDispatchTests(unittest.TestCase):
    def _service(self) -> TransferDispatchService:
        return TransferDispatchService(
            database=object(),
            accounts_service=object(),
            target_service=object(),
            mount_service=object(),
            registry=_Registry(),
            rclone_service=object(),
            locator_resolver=object(),
        )

    def test_builds_native_executor(self) -> None:
        self.assertIn("native_save", self._service().build_executors(_plan("native_save")))

    def test_builds_rclone_executor(self) -> None:
        plan = _plan("rclone_copy")
        plan["destination"]["provider_kind"] = "quark"
        self.assertIn("rclone_copy", self._service().build_executors(plan))

    def test_builds_mobile_relay_executor_from_frozen_account(self) -> None:
        self.assertIn(
            "relay_upload_mobile",
            self._service().build_executors(_plan("relay_upload_mobile")),
        )

    def test_unknown_strategy_fails_closed(self) -> None:
        with self.assertRaises(TransferDispatchError):
            self._service().build_executors(_plan("manual_review"))

    def test_hot_reload_replaces_organizer_adapter(self) -> None:
        service = self._service()
        organizer = object()

        service.set_organizer_service(organizer)

        self.assertIs(service.organizer_service, organizer)
        self.assertIs(service.organizer.service, organizer)


if __name__ == "__main__":
    unittest.main()
