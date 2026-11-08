"""Best-effort audit trail for agent tool calls."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest_arguments(arguments: Any) -> str:
    """Stable short digest so the audit trail never stores secrets verbatim."""

    try:
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = str(arguments)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class AgentAuditLog:
    def __init__(self, database: Any) -> None:
        self._database = database

    def record(
        self,
        *,
        token_id: int,
        token_name: str,
        tool: str,
        scope: str,
        risk: str,
        arguments: Any,
        outcome: str,
        error_code: str = "",
        duration_ms: int = 0,
    ) -> None:
        try:
            with self._database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO agent_tool_calls
                        (token_id, token_name, tool, scope, risk, args_digest,
                         outcome, error_code, duration_ms, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(token_id),
                        str(token_name or "")[:120],
                        str(tool or "")[:80],
                        str(scope or "")[:40],
                        str(risk or "read")[:20],
                        digest_arguments(arguments),
                        str(outcome or "")[:20],
                        str(error_code or "")[:60],
                        max(0, int(duration_ms or 0)),
                        _now_iso(),
                    ),
                )
        except Exception:  # noqa: BLE001 - auditing must never break a call
            logger.debug("failed to record agent tool call", exc_info=True)

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, token_id, token_name, tool, scope, risk, args_digest,
                       outcome, error_code, duration_ms, created_at
                  FROM agent_tool_calls
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (max(1, min(int(limit or 50), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]
