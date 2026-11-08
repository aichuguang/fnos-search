"""Agent token storage and verification.

Tokens are shown once at creation and only their SHA-256 digest is persisted,
mirroring how the application treats the rclone worker control token.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

TOKEN_PREFIX = "fnos_agent_"
TOKEN_BYTES = 32

SCOPE_SEARCH = "search"
SCOPE_IMPORT_READ = "import.read"
SCOPE_IMPORT_WRITE = "import.write"
SCOPE_ORGANIZE_READ = "organize.read"
SCOPE_ORGANIZE_WRITE = "organize.write"
SCOPE_ORGANIZE_APPLY = "organize.apply"
SCOPE_SCHEDULE_READ = "schedule.read"
SCOPE_SCHEDULE_WRITE = "schedule.write"
SCOPE_OPS_READ = "ops.read"

ALL_SCOPES: tuple[str, ...] = (
    SCOPE_SEARCH,
    SCOPE_IMPORT_READ,
    SCOPE_IMPORT_WRITE,
    SCOPE_ORGANIZE_READ,
    SCOPE_ORGANIZE_WRITE,
    SCOPE_ORGANIZE_APPLY,
    SCOPE_SCHEDULE_READ,
    SCOPE_SCHEDULE_WRITE,
    SCOPE_OPS_READ,
)

# Least privilege by default: a new token can only read. Write scopes must be
# requested explicitly, and ``organize.apply`` (physical moves) is never part of
# a preset other than ``full``.
DEFAULT_SCOPES: tuple[str, ...] = (
    SCOPE_SEARCH,
    SCOPE_IMPORT_READ,
    SCOPE_ORGANIZE_READ,
    SCOPE_SCHEDULE_READ,
    SCOPE_OPS_READ,
)

SCOPE_PROFILES: dict[str, tuple[str, ...]] = {
    "read": DEFAULT_SCOPES,
    "operator": (
        *DEFAULT_SCOPES,
        SCOPE_IMPORT_WRITE,
        SCOPE_ORGANIZE_WRITE,
        SCOPE_SCHEDULE_WRITE,
    ),
    "full": (
        *DEFAULT_SCOPES,
        SCOPE_IMPORT_WRITE,
        SCOPE_ORGANIZE_WRITE,
        SCOPE_SCHEDULE_WRITE,
        SCOPE_ORGANIZE_APPLY,
    ),
}


@dataclass(frozen=True)
class AgentTokenRecord:
    id: int
    name: str
    scopes: frozenset[str]
    rate_limit: int
    expires_at: str
    revoked_at: str
    last_used_at: str

    @property
    def active(self) -> bool:
        if self.revoked_at:
            return False
        if not self.expires_at:
            return True
        try:
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def normalize_scopes(raw: Any) -> frozenset[str]:
    if not isinstance(raw, (list, tuple, set)):
        return frozenset()
    allowed = set(ALL_SCOPES)
    return frozenset(str(item).strip() for item in raw if str(item).strip() in allowed)


class AgentTokenStore:
    """Persistence for agent tokens and the call audit trail."""

    def __init__(self, database: Any) -> None:
        self._database = database

    @property
    def database(self) -> Any:
        return self._database

    # -- tokens ---------------------------------------------------------
    def verify(self, plaintext: str) -> AgentTokenRecord | None:
        candidate = str(plaintext or "").strip()
        if not candidate.startswith(TOKEN_PREFIX):
            return None
        digest = hash_token(candidate)
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, token_hash, scopes, rate_limit, expires_at, revoked_at, last_used_at
                  FROM agent_tokens
                 WHERE token_hash = ?
                """,
                (digest,),
            ).fetchone()
        if not row:
            return None
        record = AgentTokenRecord(
            id=int(row["id"]),
            name=str(row["name"] or ""),
            scopes=normalize_scopes(_load_json(row["scopes"])),
            rate_limit=max(0, int(row["rate_limit"] or 0)),
            expires_at=str(row["expires_at"] or ""),
            revoked_at=str(row["revoked_at"] or ""),
            last_used_at=str(row["last_used_at"] or ""),
        )
        return record if record.active else None

    def create(
        self,
        name: str,
        scopes: list[str] | tuple[str, ...] | None = None,
        *,
        rate_limit: int = 0,
        expires_at: str = "",
    ) -> tuple[str, dict[str, Any]]:
        plaintext = generate_token()
        now = _now_iso()
        chosen = normalize_scopes(list(scopes) if scopes is not None else list(DEFAULT_SCOPES))
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_tokens
                    (name, token_hash, token_prefix, scopes, rate_limit, expires_at,
                     revoked_at, last_used_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, '', '', ?, ?)
                """,
                (
                    str(name or "").strip() or "agent",
                    hash_token(plaintext),
                    plaintext[: len(TOKEN_PREFIX) + 4],
                    json.dumps(sorted(chosen), ensure_ascii=False),
                    max(0, int(rate_limit or 0)),
                    str(expires_at or ""),
                    now,
                    now,
                ),
            )
            token_id = int(cursor.lastrowid or 0)
        return plaintext, {
            "id": token_id,
            "name": str(name or "").strip() or "agent",
            "scopes": sorted(chosen),
            "rate_limit": max(0, int(rate_limit or 0)),
            "expires_at": str(expires_at or ""),
            "created_at": now,
        }

    def list_tokens(self) -> list[dict[str, Any]]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, token_prefix, scopes, rate_limit, expires_at,
                       revoked_at, last_used_at, created_at
                  FROM agent_tokens
                 ORDER BY id DESC
                """
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"] or ""),
                "token_prefix": str(row["token_prefix"] or ""),
                "scopes": sorted(normalize_scopes(_load_json(row["scopes"]))),
                "rate_limit": int(row["rate_limit"] or 0),
                "expires_at": str(row["expires_at"] or ""),
                "revoked_at": str(row["revoked_at"] or ""),
                "last_used_at": str(row["last_used_at"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
            for row in rows
        ]

    def revoke(self, token_id: int) -> bool:
        now = _now_iso()
        with self._database.connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_tokens SET revoked_at = ?, updated_at = ? WHERE id = ? AND revoked_at = ''",
                (now, now, int(token_id)),
            )
            return cursor.rowcount > 0

    def touch(self, token_id: int) -> None:
        try:
            with self._database.connect() as connection:
                connection.execute(
                    "UPDATE agent_tokens SET last_used_at = ? WHERE id = ?",
                    (_now_iso(), int(token_id)),
                )
        except Exception:  # noqa: BLE001 - never fail a call for bookkeeping
            return


def _load_json(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    try:
        return json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
