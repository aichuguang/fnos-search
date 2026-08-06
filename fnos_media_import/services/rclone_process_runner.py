from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    error: str = ""


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
                on_line(line.rstrip("\n"))
            exit_code = process.wait()
        except FileNotFoundError as exc:
            return ProcessResult(127, f"无法启动脚本，请确认 sh 和 rclone 等命令已安装：{exc}")
        except Exception as exc:  # noqa: BLE001
            return ProcessResult(1, f"rclone 搬运任务执行异常：{exc}")

        if exit_code == 0:
            return ProcessResult(0)
        if exit_code in (130, 143):
            return ProcessResult(exit_code, f"rclone 搬运脚本已停止：{exit_code}")
        return ProcessResult(exit_code, f"rclone 搬运脚本异常退出：{exit_code}")
