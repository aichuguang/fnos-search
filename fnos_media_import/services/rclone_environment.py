from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

CommandRunner = Callable[[str, list[str], bool], dict[str, Any]]


class RcloneEnvironmentChecker:
    """Validates the Docker/rclone runtime and configured remote directories."""

    DIRECTORY_LABELS = (
        ("电影源目录", "RCLONE_SRC_MOVIE_DIR"),
        ("电视剧源目录", "RCLONE_SRC_TV_DIR"),
        ("动漫源目录", "RCLONE_SRC_ANIME_DIR"),
        ("综艺源目录", "RCLONE_SRC_VARIETY_DIR"),
        ("其他源目录", "RCLONE_SRC_OTHER_DIR"),
        ("电影目标目录", "RCLONE_DST_MOVIE_DIR"),
        ("电视剧目标目录", "RCLONE_DST_TV_DIR"),
        ("动漫目标目录", "RCLONE_DST_ANIME_DIR"),
        ("综艺目标目录", "RCLONE_DST_VARIETY_DIR"),
        ("其他目标目录", "RCLONE_DST_OTHER_DIR"),
    )

    def __init__(self, config: dict[str, Any], *, command_runner: CommandRunner | None = None) -> None:
        self.config = config
        self.command_runner = command_runner or self.command_check

    def apply_config(self, config: dict[str, Any]) -> None:
        self.config = config

    def check(self, script_path: Path, category_dirs: dict[str, str]) -> dict[str, Any]:
        container_name = str(self.config.get("container_name", "rclone-server"))
        checks = [
            {"name": "rclone 搬运脚本", "ok": script_path.exists(), "message": str(script_path)},
            self.command_runner("docker", ["docker", "version", "--format", "{{.Server.Version}}"], True),
            self.command_runner(
                "rclone 容器",
                ["docker", "ps", "-q", "-f", f"name=^/{container_name}$"],
                True,
            ),
            self.remote_access_check(container_name),
        ]
        for label, env_name in self.DIRECTORY_LABELS:
            checks.append(self.directory_check(f"rclone {label}", container_name, category_dirs.get(env_name, "")))
        failed = [item for item in checks if not item["ok"]]
        message = "环境检查通过" if not failed else "环境检查失败：" + "、".join(str(item["name"]) for item in failed)
        return {"success": not failed, "message": message, "items": checks}

    @staticmethod
    def command_check(name: str, command: list[str], allow_empty: bool = True) -> dict[str, Any]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=12,
                encoding="utf-8",
                errors="replace",
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            ok = result.returncode == 0 and (allow_empty or bool(stdout))
            return {
                "name": name,
                "ok": ok,
                "message": stdout or stderr or f"exit={result.returncode}",
                "exit_code": result.returncode,
            }
        except FileNotFoundError as exc:
            return {"name": name, "ok": False, "message": f"命令不存在：{exc}", "exit_code": 127}
        except subprocess.TimeoutExpired:
            return {"name": name, "ok": False, "message": "检查超时", "exit_code": 124}

    def directory_check(self, name: str, container_name: str, directory: str) -> dict[str, Any]:
        remote_path = f"{self.config.get('remote_name', 'MP')}:{directory}"
        stat_command = ["docker", "exec", container_name, "rclone", "lsjson", remote_path, "--stat"]
        item = self.command_runner(name, stat_command, True)
        if item["ok"]:
            item["message"] = f"{remote_path} 可访问"
            return item
        message = str(item.get("message") or "")
        if "directory not found" not in message.lower() and "not found" not in message.lower():
            return item
        mkdir_item = self.command_runner(
            f"{name} 自动创建",
            ["docker", "exec", container_name, "rclone", "mkdir", remote_path],
            True,
        )
        if not mkdir_item["ok"]:
            item["message"] = f"{remote_path} 不存在，自动创建失败：{mkdir_item.get('message') or message}"
            return item
        verify_item = self.command_runner(name, stat_command, True)
        if verify_item["ok"]:
            verify_item["message"] = f"{remote_path} 不存在，已自动创建空目录"
        else:
            verify_item["message"] = f"{remote_path} 已尝试自动创建，但复查失败：{verify_item.get('message') or message}"
        return verify_item

    def remote_access_check(self, container_name: str) -> dict[str, Any]:
        remote = f"{self.config.get('remote_name', 'MP')}:"
        item = self.command_runner(
            "rclone remote",
            ["docker", "exec", container_name, "rclone", "lsd", remote],
            True,
        )
        if item["ok"]:
            item["message"] = f"{remote} 可访问"
        return item
