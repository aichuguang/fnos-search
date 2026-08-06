from __future__ import annotations

import os
import secrets
import stat
import sys
import time
from pathlib import Path


def _numeric_env(name: str, default: int) -> int:
    value = str(os.getenv(name, default)).strip()
    if not value.isdigit():
        raise RuntimeError(f"{name} must be a numeric uid/gid")
    return int(value)


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(callable(geteuid) and geteuid() == 0)


def _prepare_path(path: Path, uid: int, gid: int, *, recursive: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    targets = [path]
    if recursive:
        for root, directories, files in os.walk(path, followlinks=False):
            root_path = Path(root)
            targets.extend(root_path / name for name in directories)
            targets.extend(root_path / name for name in files)

    for target in targets:
        try:
            target_stat = target.lstat()
            if stat.S_ISLNK(target_stat.st_mode):
                continue
            os.chown(target, uid, gid)
            required_mode = 0o770 if stat.S_ISDIR(target_stat.st_mode) else 0o660
            os.chmod(target, stat.S_IMODE(target_stat.st_mode) | required_mode)
        except FileNotFoundError:
            continue


def _drop_privileges(uid: int, gid: int, supplementary_gids: list[int] | None = None) -> None:
    groups = sorted({int(value) for value in (supplementary_gids or []) if int(value) != gid})
    os.setgroups(groups)
    os.setgid(gid)
    os.setuid(uid)


def _ensure_runtime_secret(name: str, path: Path, uid: int, gid: int) -> None:
    """Load an explicit secret or create a stable container-local fallback."""
    configured = str(os.getenv(name, "")).strip()
    if configured:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = secrets.token_urlsafe(48)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            value = ""
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{value}\n")
                handle.flush()
                os.fsync(handle.fileno())

    if not value and path.exists():
        for _ in range(20):
            value = path.read_text(encoding="utf-8").strip()
            if value:
                break
            time.sleep(0.01)
    if not value:
        raise RuntimeError(f"runtime secret file is empty: {path}")
    os.chmod(path, 0o600)
    if _running_as_root():
        os.chown(path.parent, uid, gid)
        os.chown(path, uid, gid)
    os.environ[name] = value


def main() -> None:
    if len(sys.argv) < 2:
        raise RuntimeError("container entrypoint requires an application command")

    uid = _numeric_env("APP_UID", 10001)
    gid = _numeric_env("APP_GID", 10001)
    configured_role = str(os.getenv("FNOS_PROCESS_ROLE") or "").strip().lower()
    runtime_role = configured_role or (
        "web" if str(os.getenv("APP_ENV") or "").strip().lower() == "production" else "all"
    )
    if _running_as_root():
        # config/data/logs 里保存应用配置和数据，需要修复已有文件。
        for value in ("/app/config", "/app/data", "/app/logs", "/home/app"):
            _prepare_path(Path(value), uid, gid, recursive=True)
        if runtime_role in {"all", "worker"}:
            # rclone 临时文件可能很大，只修复目录根，避免每次启动全量遍历。
            _prepare_path(Path("/config/rclone"), uid, gid, recursive=True)
            for value in ("/cache", "/temp"):
                _prepare_path(Path(value), uid, gid, recursive=False)
            rclone_config = Path(os.getenv("RCLONE_CONFIG_PATH", "/config/rclone/rclone.conf"))
            _prepare_path(rclone_config.parent, uid, gid, recursive=True)
            rclone_config.touch(exist_ok=True)
            os.chown(rclone_config, uid, gid)
            os.chmod(rclone_config, stat.S_IMODE(rclone_config.stat().st_mode) | 0o660)

    secret_path = Path(
        os.getenv(
            "NOTIFICATION_ENCRYPTION_KEY_FILE",
            "/app/data/.secrets/notification_encryption_key",
        )
    )
    _ensure_runtime_secret("NOTIFICATION_ENCRYPTION_KEY", secret_path, uid, gid)
    worker_token_path = Path(
        os.getenv(
            "RCLONE_WORKER_CONTROL_TOKEN_FILE",
            "/app/data/.secrets/rclone_worker_control_token",
        )
    )
    _ensure_runtime_secret("RCLONE_WORKER_CONTROL_TOKEN", worker_token_path, uid, gid)

    if _running_as_root():
        _drop_privileges(uid, gid)

    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
