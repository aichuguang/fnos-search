from __future__ import annotations

import os
import signal
from typing import Any, Callable


class RcloneProcessController:
    """Provides platform-aware process inspection and termination."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        get_process_group: Callable[[int], int] | None = None,
        kill_process_group: Callable[[int, int], None] | None = None,
    ) -> None:
        self.platform_name = platform_name or os.name
        self.get_process_group = get_process_group or getattr(os, "getpgid", lambda pid: pid)
        self.kill_process_group = kill_process_group or getattr(os, "killpg", self._unsupported_killpg)

    @staticmethod
    def is_active(process: Any) -> bool:
        return bool(process and process.poll() is None)

    def terminate(self, process: Any) -> bool:
        if not self.is_active(process):
            return False
        if self.platform_name == "nt":
            process.terminate()
            return True
        try:
            process_group = self.get_process_group(process.pid)
            self.kill_process_group(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:  # noqa: BLE001
            process.terminate()
        return True

    @staticmethod
    def _unsupported_killpg(_group: int, _signal: int) -> None:
        raise NotImplementedError("process-group termination is unavailable")
