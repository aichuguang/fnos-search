from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImportResult:
    success: bool
    status: str
    message: str
    external_task_id: str = ""
    target_path: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterCheckResult:
    ok: bool
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
