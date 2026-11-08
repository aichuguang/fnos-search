"""Durable single-job gate for the V1 transfer phase.

The worker queue is intentionally per-job so a restart can recover every
execution independently.  It is not, however, a scheduling policy: without a
gate the durable worker picks one job task at a time, advances one file, and
then round-robins to the next job.  For a NAS sharing one download link, one
upload API and one disk, that interleaving creates unnecessary contention and
makes progress unpredictable.

This gate gives the queue a job-level admission rule: only one execution may
own the transfer phase at a time.  The owner releases the gate as soon as the
execution reaches ``organizing`` (or any terminal/waiting phase), so the
organizer of the finished job can overlap the next job's transfer while two
transfer jobs never do.

The lease lives in the existing ``scheduler_leases`` table.  The owner value
embeds the job id, which keeps the repository's atomic acquire/release
semantics and lets recovery diagnostics identify the active job without a new
schema migration.
"""

from __future__ import annotations

from typing import Any

GATE_NAME = "v1_transfer_job_gate"
OWNER_PREFIX = "job:"


def _owner(job_id: int) -> str:
    return f"{OWNER_PREFIX}{int(job_id)}"


def _job_id_from_owner(value: Any) -> int:
    text = str(value or "").strip()
    if not text.startswith(OWNER_PREFIX):
        return 0
    try:
        return max(0, int(text[len(OWNER_PREFIX):]))
    except (TypeError, ValueError):
        return 0


class TransferJobGate:
    """A tiny job-id admission lease over ``scheduler_leases``.

    ``leases`` is a :class:`SchedulerLeaseRepository` in production.  A
    missing repository is treated as an unconfigured/legacy deployment and
    fails open; the transfer execution repository remains the authoritative
    safety fence in that case.
    """

    def __init__(
        self,
        leases: Any | None,
        *,
        name: str = GATE_NAME,
        ttl_seconds: int = 900,
    ) -> None:
        self.leases = leases
        self.name = str(name or GATE_NAME)
        self.ttl_seconds = max(5, int(ttl_seconds or 900))

    def try_acquire(self, job_id: int) -> tuple[bool, int]:
        """Try to make ``job_id`` the one active transfer job.

        Returns ``(acquired, active_job_id)``.  When another job owns the
        gate, ``active_job_id`` identifies it for a useful queue message.
        """
        normalized = max(0, int(job_id or 0))
        if normalized <= 0:
            return False, 0
        acquire = getattr(self.leases, "acquire", None)
        if not callable(acquire):
            return True, normalized
        if acquire(self.name, _owner(normalized), ttl_seconds=self.ttl_seconds):
            return True, normalized
        return False, self.active_job_id()

    def renew(self, job_id: int) -> bool:
        acquired, _active = self.try_acquire(job_id)
        return bool(acquired)

    def release(self, job_id: int) -> bool:
        release = getattr(self.leases, "release", None)
        if not callable(release):
            return False
        return bool(release(self.name, _owner(job_id)))

    def active_job_id(self) -> int:
        get = getattr(self.leases, "get", None)
        if not callable(get):
            return 0
        try:
            row = get(self.name)
        except Exception:  # noqa: BLE001 - diagnostics must not break transfer
            return 0
        if not isinstance(row, dict):
            return 0
        return _job_id_from_owner(row.get("owner_id"))


__all__ = ["GATE_NAME", "OWNER_PREFIX", "TransferJobGate"]
