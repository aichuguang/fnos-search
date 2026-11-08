from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fnos_media_import.agent.audit import AgentAuditLog
from fnos_media_import.agent.auth import DEFAULT_SCOPES, AgentTokenStore, hash_token
from fnos_media_import.database import Database


class AgentTokenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "agent.db")
        self.database.init_schema()
        self.store = AgentTokenStore(self.database)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_create_returns_plaintext_and_verifies(self) -> None:
        plaintext, record = self.store.create("hermes")
        self.assertTrue(plaintext.startswith("fnos_agent_"))
        self.assertEqual(sorted(record["scopes"]), sorted(DEFAULT_SCOPES))
        verified = self.store.verify(plaintext)
        self.assertIsNotNone(verified)
        assert verified is not None
        self.assertEqual(verified.name, "hermes")
        self.assertEqual(verified.scopes, frozenset(DEFAULT_SCOPES))

    def test_plaintext_is_not_persisted(self) -> None:
        plaintext, _ = self.store.create("hermes")
        with self.database.connect() as connection:
            row = connection.execute("SELECT token_hash FROM agent_tokens").fetchone()
        self.assertEqual(row["token_hash"], hash_token(plaintext))
        self.assertNotEqual(row["token_hash"], plaintext)

    def test_wrong_token_is_rejected(self) -> None:
        self.store.create("hermes")
        self.assertIsNone(self.store.verify("fnos_agent_wrong"))

    def test_token_without_prefix_is_rejected(self) -> None:
        self.store.create("hermes")
        self.assertIsNone(self.store.verify("plain-value"))

    def test_revoke_invalidates_token(self) -> None:
        plaintext, record = self.store.create("hermes")
        self.assertTrue(self.store.revoke(record["id"]))
        self.assertIsNone(self.store.verify(plaintext))
        self.assertFalse(self.store.revoke(record["id"]))

    def test_expired_token_is_rejected(self) -> None:
        plaintext, _ = self.store.create("hermes", expires_at="2000-01-01T00:00:00+00:00")
        self.assertIsNone(self.store.verify(plaintext))

    def test_list_tokens_never_exposes_hash(self) -> None:
        self.store.create("hermes")
        rows = self.store.list_tokens()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("token_hash", rows[0])
        self.assertIn("token_prefix", rows[0])

    def test_unknown_scopes_are_dropped(self) -> None:
        _, record = self.store.create("hermes", ["search", "bogus"])
        self.assertEqual(record["scopes"], ["search"])

    def test_audit_stores_digest_not_payload(self) -> None:
        _, record = self.store.create("hermes")
        audit = AgentAuditLog(self.database)
        audit.record(
            token_id=record["id"],
            token_name="hermes",
            tool="search_resources",
            scope="search",
            risk="read",
            arguments={"keyword": "秘密关键词"},
            outcome="ok",
            error_code="",
            duration_ms=12,
        )
        rows = audit.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "search_resources")
        self.assertNotIn("秘密关键词", str(rows[0]))

    def test_touch_updates_last_used_at(self) -> None:
        _, record = self.store.create("hermes")
        self.store.touch(record["id"])
        rows = self.store.list_tokens()
        self.assertTrue(rows[0]["last_used_at"])


if __name__ == "__main__":
    unittest.main()
