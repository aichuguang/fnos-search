from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable


class RcloneWorkerCommandBuilder:
    """Resolves the worker script and selects an available shell."""

    def __init__(
        self,
        config: dict[str, Any],
        base_dir: Path,
        *,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.which = which or shutil.which

    def apply_config(self, config: dict[str, Any]) -> None:
        self.config = config

    def script_path(self) -> Path:
        configured = Path(str(self.config.get("script_path") or "scripts/fnos_rclone_worker.sh"))
        return configured if configured.is_absolute() else self.base_dir / configured

    def command(self, script_path: Path | None = None) -> list[str]:
        path = script_path or self.script_path()
        shell = str(self.config.get("shell") or "sh")
        if Path(shell).name == "bash" and not self.which(shell):
            shell = "sh"
        return [shell, str(path)]
