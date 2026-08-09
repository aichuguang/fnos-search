from __future__ import annotations

import os


VALID_PROCESS_ROLES = {"all", "web", "scheduler", "worker"}


def resolve_process_role(value: str | None = None) -> str:
    configured = str(value if value is not None else os.getenv("FNOS_PROCESS_ROLE") or "").strip()
    if not configured:
        raise ValueError("FNOS_PROCESS_ROLE is required; choose one of all, web, scheduler, worker")
    role = configured.lower()
    if role not in VALID_PROCESS_ROLES:
        raise ValueError(f"invalid FNOS_PROCESS_ROLE: {role}")
    return role


def role_runs(role: str, component: str) -> bool:
    if role == "all":
        return True
    return role == component
