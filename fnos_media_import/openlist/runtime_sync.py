"""Small cross-process coordinator for the DB and the single managed Remote.

The change lock spans the Web workflow, not a SQLite transaction. The Remote
lock stays with the Worker across the actual command, including after an HTTP
caller times out. Owner tokens fence delegated calls without deadlocking those
two processes. Files live beside the shared database; memory DBs use local locks.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import OpenListValidationError


class OpenListSyncConflict(OpenListValidationError):
    code = "OPENLIST_SYNC_BUSY"
    status = 409
    retryable = True


_REGISTRY_LOCK = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_MEMORY: dict[str, dict[str, Any]] = {}
_HELD = threading.local()


def managed_remote_identity(remote_name: Any, config_path: Any) -> dict[str, str]:
    remote = str(remote_name or "").strip()
    path = str(config_path or "").strip()
    if not remote or not path:
        return {}
    return {
        "remote_name": remote,
        "config_path": os.path.normcase(os.path.realpath(path)).replace("\\", "/"),
    }


def _kernel_lock(handle: Any, *, unlock: bool = False) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


class _SharedLock:
    def __init__(self, key: str, path: Path | None) -> None:
        self.key = key
        self.path = path
        with _REGISTRY_LOCK:
            self.local = _LOCKS.setdefault(key, threading.RLock())

    @contextmanager
    def hold(self, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        if not self.local.acquire(timeout=max(0.0, timeout)):
            raise OpenListSyncConflict("OpenList 配置正在保存，请稍后重试")
        held = getattr(_HELD, "locks", {})
        _HELD.locks = held
        handle = None
        nested = self.key in held
        try:
            if not nested:
                if self.path is not None:
                    fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
                    handle = os.fdopen(fd, "r+b", buffering=0)
                    if os.fstat(fd).st_size == 0:
                        handle.write(b"0")
                    while True:
                        try:
                            _kernel_lock(handle)
                            break
                        except OSError as exc:
                            if time.monotonic() >= deadline:
                                raise OpenListSyncConflict("OpenList 配置正在被另一进程保存") from exc
                            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                held[self.key] = True
            yield
        finally:
            try:
                if not nested:
                    try:
                        if self.key in held:
                            held.pop(self.key, None)
                            if handle is not None:
                                _kernel_lock(handle, unlock=True)
                    finally:
                        if handle is not None:
                            handle.close()
            finally:
                self.local.release()

    def held_here(self) -> bool:
        return self.key in getattr(_HELD, "locks", {})

    def busy(self) -> bool:
        try:
            with self.hold(0):
                return False
        except OpenListSyncConflict:
            return True


class OpenListRuntimeSync:
    def __init__(self, database: Any) -> None:
        path = getattr(database, "path", None)
        if not isinstance(path, (str, os.PathLike)):
            try:
                with database.connect() as connection:
                    path = next((row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), "")
            except Exception:
                path = ""
        self.base = Path(path).resolve() if path and str(path) != ":memory:" else None
        self.key = os.path.normcase(str(self.base)) if self.base else f"memory:{id(database)}"
        with _REGISTRY_LOCK:
            self.memory = _MEMORY.setdefault(self.key, {})
        self.change_lock = _SharedLock(self.key + ":change", self._path("change.lock"))
        self.remote_lock = _SharedLock(self.key + ":remote", self._path("remote.lock"))
        self.tracks_remote = False

    def _path(self, suffix: str) -> Path | None:
        return self.base.with_name(f"{self.base.name}.openlist-{suffix}") if self.base else None

    def _read(self, suffix: str) -> dict[str, Any]:
        path = self._path(suffix)
        if path is None:
            return dict(self.memory.get(suffix) or {})
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"phase": "unknown"}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {"phase": "unknown"}

    def _write(self, suffix: str, value: dict[str, Any]) -> None:
        path = self._path(suffix)
        if path is None:
            self.memory[suffix] = dict(value)
            return
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def mutation(self, timeout: float = 15.0):
        owners = getattr(_HELD, "owners", {})
        _HELD.owners = owners
        with self.change_lock.hold(timeout):
            nested = self.key in owners
            try:
                if not nested:
                    owners[self.key] = uuid.uuid4().hex
                    self._write("owner.json", {"token": owners[self.key]})
                yield owners[self.key]
            finally:
                if not nested:
                    owners.pop(self.key, None)
                    self._write("owner.json", {})

    def current_token(self) -> str:
        return str(getattr(_HELD, "owners", {}).get(self.key) or "")

    def owns(self, token: str) -> bool:
        if not token:
            return False
        if self.current_token() == token and self.change_lock.held_here():
            return True
        if self._read("owner.json").get("token") != token or not self.change_lock.busy():
            return False
        return self._read("owner.json").get("token") == token

    def state(self) -> dict[str, Any]:
        return self._read("remote-state.json")

    def set_state(
        self, token: str, phase: str, *, fingerprint: str = "",
        managed_remote: dict[str, str] | None = None,
        remote_config_fingerprint: str = "",
    ) -> bool:
        # Callers hold the Remote lock; an old Worker must never overwrite a
        # newer operation's pending/confirmed marker after losing its owner.
        if not self.owns(token):
            return False
        state = {
            "token": token, "phase": phase, "fingerprint": fingerprint,
            "managed_remote": dict(managed_remote or {}),
        }
        if phase == "confirmed":
            state["remote_config_fingerprint"] = str(remote_config_fingerprint or "")
        self._write("remote-state.json", state)
        return True

    def permits(self, fingerprint: str, *, managed_remote: dict[str, str] | None = None) -> bool:
        if self.change_lock.busy():
            return False
        try:
            with self.remote_lock.hold(0):
                state = self.state()
                if not state:
                    # A configured data plane must be explicitly confirmed
                    # after first use/upgrade, not inferred from file absence.
                    return not self.tracks_remote
                if self.tracks_remote and not managed_remote:
                    return False
                return (
                    state.get("phase") == "confirmed"
                    and state.get("fingerprint") == fingerprint
                    and state.get("managed_remote", {}) == dict(managed_remote or {})
                )
        except OpenListSyncConflict:
            return False
