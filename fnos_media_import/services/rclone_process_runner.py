from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..constants import RCLONE_EXIT_LOCK_BUSY


LEGACY_LOCK_BUSY_MESSAGE = "已有 rclone 搬运任务运行中，本次跳过"


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    error: str = ""

    @property
    def not_started(self) -> bool:
        """脚本因互斥锁被占用而根本没开始搬运，既不是成功也不是搬运失败。"""
        return self.exit_code == RCLONE_EXIT_LOCK_BUSY


class RcloneProcessRunner:
    """Executes the worker command and translates process failures to stable results."""

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        on_line: Callable[[str], None],
        on_started: Callable[[subprocess.Popen[str]], None],
    ) -> ProcessResult:
        legacy_lock_busy = False
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                start_new_session=os.name != "nt",
            )
            on_started(process)
            assert process.stdout is not None
            for line in process.stdout:
                message = line.rstrip("\r\n")
                if message == LEGACY_LOCK_BUSY_MESSAGE or message.endswith(f"] {LEGACY_LOCK_BUSY_MESSAGE}"):
                    legacy_lock_busy = True
                on_line(message)
            exit_code = process.wait()
        except FileNotFoundError as exc:
            return ProcessResult(127, f"无法启动脚本，请确认 sh 和 rclone 等命令已安装：{exc}")
        except Exception as exc:  # noqa: BLE001
            return ProcessResult(1, f"rclone 搬运任务执行异常：{exc}")

        # Mixed deployments may still invoke the old mkdir-lock script which
        # reported a skipped run as exit 0. Never trust that as a completed copy.
        if exit_code == 0 and legacy_lock_busy:
            exit_code = RCLONE_EXIT_LOCK_BUSY
        if exit_code == 0:
            return ProcessResult(0)
        if exit_code in (130, 143):
            return ProcessResult(exit_code, f"rclone 搬运脚本已停止：{exit_code}")
        if exit_code == RCLONE_EXIT_LOCK_BUSY:
            return ProcessResult(exit_code, "rclone 互斥锁被占用，本次搬运未启动，稍后由队列重新调度")
        return ProcessResult(exit_code, f"rclone 搬运脚本异常退出：{exit_code}")
