from __future__ import annotations

import os


VALID_PROCESS_ROLES = {"all", "web", "scheduler", "worker"}


def process_role_is_explicit(value: str | None = None) -> bool:
    return bool(str(value if value is not None else os.getenv("FNOS_PROCESS_ROLE") or "").strip())


def legacy_deployment_layout(value: str | None = None) -> bool:
    return (
        str(os.getenv("APP_ENV") or "").strip().lower() == "production"
        and not process_role_is_explicit(value)
    )


def resolve_process_role(value: str | None = None) -> str:
    configured = str(value if value is not None else os.getenv("FNOS_PROCESS_ROLE") or "").strip()
    role = configured.lower() if configured else ("web" if legacy_deployment_layout(value) else "all")
    if role not in VALID_PROCESS_ROLES:
        raise ValueError(f"invalid FNOS_PROCESS_ROLE: {role}")
    return role


def role_runs(role: str, component: str) -> bool:
    if role == "all":
        return True
    return role == component
