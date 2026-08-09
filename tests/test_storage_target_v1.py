from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fnos_media_import.accounts.service import ProviderAccountService
from fnos_media_import.database import Database
from fnos_media_import.domain.models import AccountTestResult, NativeLocator, TargetCategory
from fnos_media_import.storage.assignment_service import AssignmentError, SourceAssignmentService
from fnos_media_import.storage.target_service import StorageTargetService


KEY = bytes.fromhex("11" * 32)


class StorageTargetV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "targets.db")
        self.db.init_schema()
        self.accounts = ProviderAccountService(self.db, lambda: KEY)
        self.targets = StorageTargetService(self.db)
        self.assignments = SourceAssignmentService(self.db, self.targets)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _account(self, provider: str) -> str:
        if provider == "sixpan":
            config = {"client_id": "client"}
            credentials = {"client_secret": "secret", "refresh_token": "refresh"}
        elif provider == "quark":
            config = {}
            credentials = {"cookie": "sid=secret-cookie"}
        else:
            raise AssertionError(provider)
        account = self.accounts.create_account(
            provider_kind=provider,
            name=f"{provider}-account",
            config=config,
            credentials=credentials,
        )
        self.accounts.test_account(
            account.account_id,
            tester=lambda *_args: AccountTestResult(True, f"{provider}-user", "ok"),
        )
        return account.account_id

    def _native_target(self, destination_id: str, provider: str, account_id: str) -> None:
        self.targets.create_target(
            destination_id=destination_id,
            name=destination_id,
            provider_kind=provider,
            categories={
                "movie": TargetCategory(
                    category_key="movie",
                    public_label="电影",
                    native_locator=NativeLocator("path", "/电影"),
                )
            },
            account_id=account_id,
        )

    def test_magnet_assignment_only_accepts_sixpan_binding(self) -> None:
        sixpan = self._account("sixpan")
        quark = self._account("quark")
        self._native_target("sixpan_main", "sixpan", sixpan)
        self._native_target("quark_main", "quark", quark)

        saved = self.assignments.upsert("sixpan_magnet", "movie", "sixpan_main")
        self.assertEqual(saved.destination_id, "sixpan_main")
        with self.assertRaises(AssignmentError):
            self.assignments.upsert("sixpan_magnet", "movie", "quark_main")

    def test_target_revision_creates_new_frozen_binding(self) -> None:
        quark = self._account("quark")
        self._native_target("quark_main", "quark", quark)
        first = self.targets.resolve_binding("quark_main", 1)

        updated = self.targets.revise_target(
            "quark_main",
            name="夸克主盘（二次修订）",
            account_id=quark,
        )
        second = self.targets.resolve_binding("quark_main", updated.current_revision)
        self.assertNotEqual(first.binding_id, second.binding_id)
        self.assertEqual(first.account_id, second.account_id)
        self.assertEqual(
            self.targets.get_binding(first.binding_id, first.current_revision),
            first,
        )

    def test_assignment_replace_is_atomic(self) -> None:
        quark = self._account("quark")
        self._native_target("quark_main", "quark", quark)
        self.assignments.upsert("quark", "movie", "quark_main")
        with self.assertRaises(AssignmentError):
            self.assignments.replace(
                [
                    {
                        "search_source_id": "quark",
                        "category_key": "movie",
                        "destination_id": "quark_main",
                    },
                    {
                        "search_source_id": "unknown",
                        "category_key": "movie",
                        "destination_id": "quark_main",
                    },
                ]
            )
        self.assertEqual(
            self.assignments.get_assignment("quark", "movie").destination_id,
            "quark_main",
        )


if __name__ == "__main__":
    unittest.main()
