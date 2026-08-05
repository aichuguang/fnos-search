from __future__ import annotations

import os


VALID_PROCESS_ROLES = {"all", "web", "scheduler", "worker"}


def resolve_process_role(value: str | None = None) -> str:
    role = str(value or os.getenv("FNOS_PROCESS_ROLE") or "all").strip().lower()
    if role not in VALID_PROCESS_ROLES:
        raise ValueError(f"invalid FNOS_PROCESS_ROLE: {role}")
    return role


def role_runs(role: str, component: str) -> bool:
    if role == "all":
        return True
    return role == component
