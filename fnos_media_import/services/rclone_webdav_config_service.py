from __future__ import annotations

import json
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit


class RcloneWebdavConfigError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class RcloneWebdavConfigService:
    """Manage one OpenList WebDAV remote with the worker-local rclone binary."""

    CONFIG_PATH = "/config/rclone/rclone.conf"
    BACKUP_PATH = "/config/rclone/rclone.conf.webui.bak"
    _CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
    _BACKUP_SCRIPT = """set -eu
config=/config/rclone/rclone.conf
backup=/config/rclone/rclone.conf.webui.bak
temporary=/config/rclone/rclone.conf.webui.bak.tmp
[ -f "$config" ] || : > "$config"
rm -f "$temporary"
cp "$config" "$temporary"
chmod 600 "$temporary"
mv -f "$temporary" "$backup"
"""
    _RESTORE_SCRIPT = """set -eu
config=/config/rclone/rclone.conf
backup=/config/rclone/rclone.conf.webui.bak
temporary=/config/rclone/rclone.conf.webui.restore.tmp
[ -f "$backup" ]
rm -f "$temporary"
cp "$backup" "$temporary"
chmod 660 "$temporary"
mv -f "$temporary" "$config"
"""
    _SAVE_SCRIPT = """set -eu
IFS= read -r operation
IFS= read -r remote_name
IFS= read -r webdav_url
IFS= read -r webdav_user
IFS= read -r webdav_password
if [ "$operation" = "create" ]; then
  rclone config create "$remote_name" webdav url "$webdav_url" vendor other user "$webdav_user" pass "$webdav_password" --obscure --config /config/rclone/rclone.conf
elif [ -n "$webdav_password" ]; then
  rclone config update "$remote_name" url "$webdav_url" vendor other user "$webdav_user" pass "$webdav_password" --obscure --config /config/rclone/rclone.conf
else
  rclone config update "$remote_name" url "$webdav_url" vendor other user "$webdav_user" --config /config/rclone/rclone.conf
fi
"""

    def __init__(
        self,
        current_config: Callable[[], Mapping[str, Any]],
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 30,
    ) -> None:
        self._current_config = current_config
        self._runner = runner
        self._timeout_seconds = max(5, min(int(timeout_seconds or 30), 120))
        self._lock = threading.Lock()

    def status(self, remote_name: Any = None) -> dict[str, Any]:
        remote = self._validate_remote_name(remote_name or self._default_remote_name())
        configs = self._config_dump()
        item = configs.get(remote)
        if not isinstance(item, Mapping):
            return {
                "success": True,
                "configured": False,
                "remote_name": remote,
                "type": "",
                "url": "",
                "user": "",
                "password_set": False,
                "connection_status": "unconfigured",
                "message": "WebDAV 尚未配置",
            }
        remote_type = str(item.get("type") or "").strip().lower()
        return {
            "success": True,
            "configured": True,
            "remote_name": remote,
            "type": remote_type,
            "url": str(item.get("url") or ""),
            "user": str(item.get("user") or ""),
            "password_set": bool(item.get("pass")),
            "connection_status": "unknown" if remote_type == "webdav" else "unsupported",
            "message": "WebDAV 已配置" if remote_type == "webdav" else f"同名 Remote 已存在，类型为 {remote_type or '未知'}",
        }

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise RcloneWebdavConfigError("请求格式不正确")
        remote = self._validate_remote_name(payload.get("remote_name") or self._default_remote_name())
        url = self._validate_url(payload.get("url"))
        username = self._validate_text(payload.get("username"), "用户名", max_length=255, required=True)
        password = self._validate_text(payload.get("password"), "密码", max_length=1024, required=False, trim=False)

        with self._lock:
            configs = self._config_dump()
            current = configs.get(remote)
            if isinstance(current, Mapping):
                remote_type = str(current.get("type") or "").strip().lower()
                if remote_type != "webdav":
                    raise RcloneWebdavConfigError(
                        f"Remote“{remote}”已存在且类型不是 WebDAV，未覆盖原配置",
                        409,
                    )
                operation = "update"
                if not password and not current.get("pass"):
                    raise RcloneWebdavConfigError("当前 Remote 没有已保存密码，请填写密码")
            else:
                operation = "create"
                if not password:
                    raise RcloneWebdavConfigError("首次配置 WebDAV 时必须填写密码")

            self._backup()
            secret_values = [password] if password else []
            try:
                result = self._run(
                    ["sh", "-c", self._SAVE_SCRIPT],
                    input_text="\n".join((operation, remote, url, username, password)) + "\n",
                )
                if result.returncode != 0:
                    detail = self._safe_process_message(result, secret_values)
                    message = "rclone 配置写入失败，请确认配置目录权限正常"
                    raise RcloneWebdavConfigError(f"{message}：{detail}" if detail else message, 502)
                self._test_remote(remote, secret_values=secret_values)
                state = self.status(remote)
            except RcloneWebdavConfigError as exc:
                restored = self._restore()
                suffix = "，已恢复保存前的配置" if restored else "，且自动恢复失败，请检查 rclone 配置文件"
                raise RcloneWebdavConfigError(f"{exc}{suffix}", exc.status_code) from exc

            state.update(
                {
                    "connection_status": "success",
                    "message": "WebDAV 配置已保存，连接检测成功",
                }
            )
            return state

    def test(self, remote_name: Any = None) -> dict[str, Any]:
        remote = self._validate_remote_name(remote_name or self._default_remote_name())
        with self._lock:
            state = self.status(remote)
            if not state["configured"]:
                raise RcloneWebdavConfigError("请先保存 WebDAV 配置")
            if state["type"] != "webdav":
                raise RcloneWebdavConfigError(f"Remote“{remote}”不是 WebDAV 类型", 409)
            self._test_remote(remote)
        state.update({"connection_status": "success", "message": "WebDAV 连接成功"})
        return state

    def _config_dump(self) -> dict[str, Any]:
        result = self._run(["rclone", "config", "dump", "--config", self.CONFIG_PATH])
        if result.returncode != 0:
            detail = self._safe_process_message(result, [])
            message = "无法读取 rclone 配置，请确认 rclone 可用且配置目录权限正常"
            raise RcloneWebdavConfigError(f"{message}：{detail}" if detail else message, 503)
        try:
            data = json.loads(result.stdout or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise RcloneWebdavConfigError("rclone 配置格式无法识别，请检查配置文件", 502) from exc
        if not isinstance(data, dict):
            raise RcloneWebdavConfigError("rclone 配置格式无法识别，请检查配置文件", 502)
        return data

    def _test_remote(self, remote: str, *, secret_values: list[str] | None = None) -> None:
        result = self._run(
            ["rclone", "lsd", f"{remote}:", "--config", self.CONFIG_PATH]
        )
        if result.returncode == 0:
            return
        detail = self._safe_process_message(result, secret_values or [])
        message = "WebDAV 连接失败"
        if detail:
            message = f"{message}：{detail}"
        raise RcloneWebdavConfigError(message, 502)

    def _backup(self) -> None:
        result = self._run(["sh", "-c", self._BACKUP_SCRIPT])
        if result.returncode != 0:
            detail = self._safe_process_message(result, [])
            message = "无法备份 rclone 配置，已停止保存"
            raise RcloneWebdavConfigError(f"{message}：{detail}" if detail else message, 503)

    def _restore(self) -> bool:
        try:
            result = self._run(["sh", "-c", self._RESTORE_SCRIPT])
        except RcloneWebdavConfigError:
            return False
        return result.returncode == 0

    def _run(self, command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise RcloneWebdavConfigError("rclone 管理命令不可用", 503) from exc
        except subprocess.TimeoutExpired as exc:
            raise RcloneWebdavConfigError("rclone 操作超时，请检查 Worker 和 OpenList 连接", 504) from exc
        except OSError as exc:
            raise RcloneWebdavConfigError("无法调用 rclone 管理配置", 503) from exc

    def _default_remote_name(self) -> str:
        return str(self._current_config().get("remote_name") or "MP")

    def _validate_remote_name(self, value: Any) -> str:
        remote = self._validate_text(value, "Remote 名称", max_length=64, required=True)
        if any(char in remote for char in (":", "/", "\\")):
            raise RcloneWebdavConfigError("Remote 名称不能包含冒号或路径分隔符")
        return remote

    def _validate_url(self, value: Any) -> str:
        url = self._validate_text(value, "WebDAV URL", max_length=2048, required=True)
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise RcloneWebdavConfigError("WebDAV URL 必须是完整的 http 或 https 地址")
        if parsed.username is not None or parsed.password is not None:
            raise RcloneWebdavConfigError("WebDAV URL 中不要包含用户名或密码")
        return url.rstrip("/")

    def _validate_text(
        self,
        value: Any,
        label: str,
        *,
        max_length: int,
        required: bool,
        trim: bool = True,
    ) -> str:
        text = "" if value is None else str(value)
        if trim:
            text = text.strip()
        if required and not text:
            raise RcloneWebdavConfigError(f"请填写{label}")
        if len(text) > max_length:
            raise RcloneWebdavConfigError(f"{label}长度不能超过 {max_length} 个字符")
        if self._CONTROL_PATTERN.search(text):
            raise RcloneWebdavConfigError(f"{label}不能包含控制字符")
        return text

    @classmethod
    def _safe_process_message(cls, result: subprocess.CompletedProcess[str], secrets: list[str]) -> str:
        raw = str(result.stderr or result.stdout or "").strip().splitlines()
        message = raw[-1].strip()[:500] if raw else ""
        for secret in secrets:
            if secret:
                message = message.replace(secret, "***")
        return cls._CONTROL_PATTERN.sub("", message)
