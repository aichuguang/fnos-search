from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fnos_media_import.public_web import _public_adapter_capabilities
from fnos_media_import.content_guard import evaluate_submission_content_risk
from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
class SearchCacheCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"search-cache-cleanup-{uuid.uuid4().hex}.db"
        self._database_infrastructure = Database(self.db_path)
        self._database_infrastructure.init_schema()
        self.database = RepositorySet.from_database(self._database_infrastructure)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def test_new_cache_write_prunes_expired_rows(self) -> None:
        expired_id = self.database.search_cache_commands.save(
            "expired",
            keyword="old",
            item={"title": "Old", "url": "https://example.invalid/old"},
            expires_minutes=-1,
        )

        self.database.search_cache_commands.save(
            "active",
            keyword="new",
            item={"title": "New", "url": "https://example.invalid/new"},
            expires_minutes=60,
        )

        with self.database.connect() as connection:
            expired = connection.execute(
                "SELECT 1 FROM search_cache WHERE id = ?", (expired_id,)
            ).fetchone()
            total = int(connection.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0])
        self.assertIsNone(expired)
        self.assertEqual(total, 1)


class ContentGuardFailurePolicyTests(unittest.TestCase):
    def test_ai_failure_defaults_to_manual_review(self) -> None:
        config = {
            "content_review": {
                "enabled": True,
                "keyword_enabled": False,
                "bt_ai_enabled": True,
            },
            "ai": {
                "enabled": True,
                "base_url": "https://example.invalid/v1",
                "api_key": "test-key",
                "model": "test-model",
            },
        }
        with patch(
            "fnos_media_import.content_guard._ai_review",
            side_effect=ConnectionResetError("connection reset"),
        ):
            result = evaluate_submission_content_risk(
                config=config,
                title="Normal Show",
                source_type="magnet",
                source_url="magnet:?xt=urn:btih:abc",
                files=[{"name": "Normal.Show.S01E01.mkv"}],
            )

        self.assertTrue(result["review_required"])
        self.assertEqual(result["stage"], "ai_failure")
        self.assertEqual(result["ai_result"]["fallback"], "review")


class PublicAdapterCapabilityTests(unittest.TestCase):
    @staticmethod
    def _cloud189(items: list[dict]) -> dict:
        return next(item for item in items if item.get("key") == "cloud189")

    def test_unconfigured_cloud189_webhook_is_not_advertised_as_enabled(self) -> None:
        item = self._cloud189(
            _public_adapter_capabilities(
                {
                    "cloud189": {},
                    "cloud139": {},
                    "cmcc_upload": {},
                    "sixpan": {},
                    "quark": {},
                }
            )
        )

        self.assertFalse(item["configured"])
        self.assertFalse(item["enabled"])
        self.assertFalse(item["capabilities"]["submit"])
        self.assertEqual("tianyi", item["provider_kind"])
        self.assertEqual("share", item["source_form"])
        self.assertEqual("tianyi", item["search_source_id"])

    def test_configured_cloud189_webhook_exposes_submit_capability(self) -> None:
        class ConfiguredCloud189:
            @staticmethod
            def describe() -> dict:
                return {
                    "adapter_type": "cloud189_provider",
                    "configured": True,
                    "capabilities": {"submit": True},
                    "message": "天翼云 Provider 账号已配置",
                }

        item = self._cloud189(
            _public_adapter_capabilities(
                {},
                generic_importers={"cloud189": ConfiguredCloud189()},
            )
        )

        self.assertTrue(item["configured"])
        self.assertTrue(item["enabled"])
        self.assertTrue(item["capabilities"]["submit"])


if __name__ == "__main__":
    unittest.main()
