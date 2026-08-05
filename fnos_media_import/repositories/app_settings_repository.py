from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from ..time_utils import utc_now_iso


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


class AppSettingsRepository:
    """Persists application settings without exposing SQL to route handlers."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def get_all(self) -> dict[str, Any]:
        with self._connection_factory() as connection:
            rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            value = row["value"]
            try:
                result[str(row["key"])] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                result[str(row["key"])] = value
        return result

    def set_many(self, updates: dict[str, Any]) -> None:
        if not updates:
            return
        now = utc_now_iso()
        with self._connection_factory() as connection:
            for key, value in updates.items():
                connection.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (str(key), json.dumps(value, ensure_ascii=False), now),
                )

    def update_atomic(
        self,
        key: str,
        updater: Callable[[Any, bool], Any],
    ) -> tuple[bool, Any, Any]:
        """Read, transform and persist one setting under a SQLite write lock.

        Returning the previous value lets callers perform a conditional rollback
        if a later runtime reload fails.  ``BEGIN IMMEDIATE`` is intentional:
        without it, two processes can both read the same JSON document and a
        merge from the second request can silently discard fields saved by the
        first request.
        """

        setting_key = str(key)
        now = utc_now_iso()
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (setting_key,),
            ).fetchone()
            existed = row is not None
            previous = self._decode_value(row["value"]) if row is not None else None
            updated = updater(previous, existed)
            connection.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (setting_key, json.dumps(updated, ensure_ascii=False), now),
            )
        return existed, previous, updated

    def mutate_all_atomic(
        self,
        updater: Callable[[dict[str, Any]], tuple[dict[str, Any], set[str]]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically validate and mutate multiple settings.

        The callback runs while holding a SQLite write lock. It returns the
        values to upsert and the keys to delete. Raising from the callback
        aborts the transaction, so cross-section validation cannot leave a
        partially saved configuration behind.
        """

        now = utc_now_iso()
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
            previous = {
                str(row["key"]): self._decode_value(row["value"])
                for row in rows
            }
            updates, delete_keys = updater(copy.deepcopy(previous))
            if not isinstance(updates, dict) or not isinstance(delete_keys, set):
                raise TypeError("app settings updater must return (dict, set)")
            for key in delete_keys:
                connection.execute("DELETE FROM app_settings WHERE key = ?", (str(key),))
            for key, value in updates.items():
                connection.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (str(key), json.dumps(value, ensure_ascii=False), now),
                )
            current = copy.deepcopy(previous)
            for key in delete_keys:
                current.pop(str(key), None)
            current.update(copy.deepcopy(updates))
        return previous, current

    def compare_and_set(
        self,
        key: str,
        expected: Any,
        replacement: Any,
        *,
        expected_exists: bool = True,
        replacement_exists: bool = True,
    ) -> bool:
        """Conditionally replace one setting without overwriting a newer write."""

        setting_key = str(key)
        with self._connection_factory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (setting_key,),
            ).fetchone()
            exists = row is not None
            if exists != bool(expected_exists):
                return False
            current = self._decode_value(row["value"]) if row is not None else None
            if current != expected:
                return False
            if replacement_exists:
                connection.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (
                        setting_key,
                        json.dumps(replacement, ensure_ascii=False),
                        utc_now_iso(),
                    ),
                )
            else:
                connection.execute(
                    "DELETE FROM app_settings WHERE key = ?",
                    (setting_key,),
                )
        return True

    @staticmethod
    def _decode_value(value: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value
