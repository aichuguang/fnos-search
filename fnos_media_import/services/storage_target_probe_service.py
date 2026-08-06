from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from ..cmcc.auth import CmccAuthProvider
from ..cmcc.client import CmccClient
from ..storage.types import StorageTarget


CommandRunner = Callable[[list[str]], dict[str, Any]]
CmccClientFactory = Callable[[dict[str, Any]], Any]


class StorageTargetProbeService:
    """Run an explicit, bounded health probe for an administrator-selected target."""

    def __init__(
        self,
        *,
        rclone_config: Callable[[], dict[str, Any]],
        command_runner: CommandRunner | None = None,
        cmcc_client_factory: CmccClientFactory | None = None,
    ) -> None:
        self.rclone_config = rclone_config
        self.command_runner = command_runner or self._run_command
        self.cmcc_client_factory = cmcc_client_factory or self._cmcc_client

    def probe(self, target: StorageTarget) -> dict[str, Any]:
        if target.driver == "rclone":
            if str(target.config.get("remote") or "").strip():
                return self._probe_rclone_remote(target)
            return self._probe_local(target)
        if target.driver == "cmcc_api":
            return self._probe_cmcc(target)
        return self._result(
            target,
            [{"name": "driver", "ok": False, "required": True, "message": f"不支持探测驱动：{target.driver}"}],
        )

    def _probe_rclone_remote(self, target: StorageTarget) -> dict[str, Any]:
        remote = str(target.config.get("remote") or "").strip()
        config = self.rclone_config() if callable(self.rclone_config) else {}
        base = [
            "--config",
            str(config.get("config_path") or "/config/rclone/rclone.conf"),
            "--cache-dir",
            str(config.get("cache_dir") or "/cache"),
        ]
        checks: list[dict[str, Any]] = []
        read = self.command_runner(["rclone", "lsd", f"{remote}:", "--max-depth", "1", *base])
        checks.append(self._check("目录读取", read, required=True))

        probe_path = f"{remote}:.fnos-storage-probe-{uuid.uuid4().hex}"
        created = self.command_runner(["rclone", "mkdir", probe_path, *base]) if read.get("ok") else {
            "ok": False,
            "message": "目录读取失败，未执行写入探测",
        }
        removed = {"ok": False, "message": "临时目录未创建"}
        if created.get("ok"):
            removed = self.command_runner(["rclone", "rmdir", probe_path, *base])
        write_ok = bool(created.get("ok") and removed.get("ok"))
        checks.append(
            {
                "name": "临时写入与删除",
                "ok": write_ok,
                "required": True,
                "message": (
                    "唯一临时目录创建并删除成功"
                    if write_ok
                    else str(removed.get("message") or created.get("message") or "写入或清理失败")
                ),
            }
        )
        quota = self.command_runner(["rclone", "about", f"{remote}:", "--json", *base])
        checks.append(self._check("容量读取", quota, required=False))
        return self._result(target, checks)

    def _probe_local(self, target: StorageTarget) -> dict[str, Any]:
        root = Path(str(target.config.get("root") or ""))
        mount = Path("/storage-targets").resolve(strict=False)
        resolved = root.resolve(strict=False)
        try:
            resolved.relative_to(mount)
        except ValueError:
            return self._result(
                target,
                [{"name": "受控挂载", "ok": False, "required": True, "message": "本地目标越出 /storage-targets"}],
            )
        checks: list[dict[str, Any]] = [
            {
                "name": "目录读取",
                "ok": resolved.is_dir(),
                "required": True,
                "message": str(resolved) if resolved.is_dir() else f"目录不存在或未挂载：{resolved}",
            }
        ]
        probe_dir = resolved / f".fnos-storage-probe-{uuid.uuid4().hex}"
        probe_file = probe_dir / "probe.txt"
        write_ok = False
        message = "目录读取失败，未执行写入探测"
        if resolved.is_dir():
            try:
                probe_dir.mkdir()
                probe_file.write_bytes(b"fnos-storage-probe")
                write_ok = probe_file.read_bytes() == b"fnos-storage-probe"
                message = "唯一临时文件写入、读取并删除成功" if write_ok else "临时文件内容复核失败"
            except OSError as exc:
                message = str(exc)
            finally:
                try:
                    if probe_file.exists():
                        probe_file.unlink()
                    if probe_dir.exists():
                        probe_dir.rmdir()
                except OSError as exc:
                    write_ok = False
                    message = f"临时内容清理失败：{exc}"
        checks.append({"name": "临时写入与删除", "ok": write_ok, "required": True, "message": message})
        checks.append({"name": "容量读取", "ok": False, "required": False, "message": "本地容量展示暂未接入"})
        return self._result(target, checks)

    def _probe_cmcc(self, target: StorageTarget) -> dict[str, Any]:
        client = self.cmcc_client_factory(target.config)
        try:
            rows = client.list_dir("/", page_size=1)
            checks = [
                {"name": "认证与目录读取", "ok": isinstance(rows, list), "required": True, "message": "移动云根目录可访问"},
                {"name": "临时写入与删除", "ok": False, "required": False, "message": "当前 CMCC SDK 未提供无残留删除探测"},
                {"name": "容量读取", "ok": False, "required": False, "message": "当前 CMCC SDK 未接入容量接口"},
            ]
        except Exception as exc:  # noqa: BLE001
            checks = [
                {"name": "认证与目录读取", "ok": False, "required": True, "message": str(exc)[:300]},
            ]
        finally:
            session = getattr(client, "session", None)
            close = getattr(session, "close", None)
            if callable(close):
                close()
        return self._result(target, checks)

    @staticmethod
    def _result(target: StorageTarget, checks: list[dict[str, Any]]) -> dict[str, Any]:
        errors = [str(item.get("message") or item.get("name") or "探测失败") for item in checks if item.get("required") and not item.get("ok")]
        ok = not errors
        return {
            "success": ok,
            "ok": ok,
            "destination_id": target.destination_id,
            "checks": checks,
            "errors": errors,
            "message": "目标连接检测通过" if ok else "目标连接检测未通过",
        }

    @staticmethod
    def _check(name: str, result: dict[str, Any], *, required: bool) -> dict[str, Any]:
        return {
            "name": name,
            "ok": bool(result.get("ok")),
            "required": required,
            "message": str(result.get("message") or ("通过" if result.get("ok") else "失败"))[:300],
        }

    @staticmethod
    def _run_command(command: list[str]) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except FileNotFoundError as exc:
            return {"ok": False, "message": f"rclone 命令不存在：{exc}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": "连接检测超时"}
        message = (completed.stdout or completed.stderr or f"exit={completed.returncode}").strip()
        return {"ok": completed.returncode == 0, "message": message[:300]}

    @staticmethod
    def _cmcc_client(config: dict[str, Any]) -> CmccClient:
        return CmccClient(CmccAuthProvider(config), timeout=15)


__all__ = ["StorageTargetProbeService"]
