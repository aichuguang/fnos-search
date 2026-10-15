"""Durable, job-scoped checkpoints shared by the relay worker and coordinator."""

from __future__ import annotations

import argparse
import copy
import errno
import json
import os
import re
import shutil
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from .relay_pipeline_config import RelayPipelineConfig
from .relay_upload_manifest import write_operation_manifest


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_LOCK_CONTEXT = threading.local()


class RelayFileState:
    NAME = ".fnos-relay-upload-sequential.json"
    LOCK_NAME = ".fnos-relay-upload-cache.lock"
    RESERVE_BYTES = 64 * 1024 * 1024

    def __init__(self, local_dir: str, *, job_id: int, plan_key: str) -> None:
        self.root = Path(local_dir).absolute()
        self.job_id = int(job_id)
        self.plan_key = str(plan_key)
        if self.job_id <= 0 or self.root.name != f"job-{self.job_id}" or not self.plan_key.strip():
            raise ValueError("逐文件中继缺少冻结的任务目录或操作身份")
        self._check_path(self.root)
        self.path = self.root / self.NAME

    @contextmanager
    def _lock(self) -> Iterator[None]:
        # All jobs share this short ledger lock. It is never held over network
        # operations; the separate rclone lock still serializes actual copies.
        path = self.root.parent / self.LOCK_NAME
        self._check_path(path)
        self.root.parent.mkdir(parents=True, exist_ok=True)
        identity = os.path.normcase(str(path))
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(identity, threading.RLock())
        with thread_lock:
            held = getattr(_LOCK_CONTEXT, "held", None)
            if held is None:
                held = _LOCK_CONTEXT.held = set()
            if identity in held:
                # RMW helpers and cancellation predicates can read recursively.
                # Re-enter the thread lock, not a second OS lock descriptor.
                yield
                return
            self._check_path(path)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "r+b", buffering=0) as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("中继锁文件不是常规文件")
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"\0")
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                held.add(identity)
                try:
                    self._check_path(path)
                    yield
                finally:
                    held.remove(identity)
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _check_path(path: Path) -> None:
        if any(part.is_symlink() for part in (path, *path.parents)) or (
            os.path.normcase(str(path.resolve())) != os.path.normcase(str(path.absolute()))
        ):
            raise ValueError("中继路径包含符号链接或重定向，拒绝操作")

    def file_path(self, relative: str) -> str:
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise ValueError("中继文件路径无效")
        parts = relative.split("/")
        if (
            PurePosixPath(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts)
            or any(":" in part or part.startswith(".fnos-relay-upload-") for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in relative)
        ):
            raise ValueError("中继文件路径越界或与回执文件冲突")
        target = self.root.joinpath(*parts)
        self._check_path(target)
        return str(target)

    def _validate(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or (
            type(state.get("version")) is not int or state.get("version") not in {1, 2}
            or state.get("job_id") != self.job_id
            or state.get("plan_key") != self.plan_key
            or not isinstance(state.get("files"), list) or not state["files"]
        ):
            raise ValueError("逐文件中继清单与冻结任务不一致")
        pipeline = state["version"] == 2
        config = RelayPipelineConfig.from_dict(state.get("pipeline")) if pipeline else None
        if pipeline and (
            type(state.get("revision")) is not int or state["revision"] < 0
            or not isinstance(state.get("prefetch_paused"), str)
        ):
            raise ValueError("中继并发清单缺少修订号或暂停状态")
        paths = set()
        active_seen = False
        if any(not isinstance(item, dict) or "state" not in item for item in state["files"]):
            raise ValueError("逐文件中继记录无效")
        next_index = self.next_index(state)
        reservations = 0
        for index, item in enumerate(state["files"]):
            if not isinstance(item, dict):
                raise ValueError("逐文件中继记录无效")
            relative = item.get("path")
            self.file_path(relative)
            if relative in paths:
                raise ValueError("逐文件中继清单含重复路径")
            paths.add(relative)
            phase = item.get("state")
            size = item.get("size")
            if phase not in {"pending", "downloaded", "verified", "cleaned"}:
                raise ValueError("逐文件中继状态无效")
            if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
                raise ValueError("逐文件中继大小无效")
            if phase != "pending" and size is None:
                raise ValueError("逐文件中继缺少已确认大小")
            if not pipeline and active_seen and phase != "pending":
                raise ValueError("前一个文件尚未释放，不允许推进后续文件")
            if pipeline:
                reserved = item.get("download_reserved", False)
                receipt = item.get("download_receipt")
                if type(reserved) is not bool or (receipt is not None and not isinstance(receipt, dict)):
                    raise ValueError("中继下载预留或回执无效")
                if reserved and (phase != "pending" or size is None):
                    raise ValueError("中继下载预留必须绑定已知大小的待下载文件")
                reservations += int(reserved)
                occupied = phase in {"downloaded", "verified"} or reserved or bool(receipt) or item.get("download_submitting")
                if (phase == "cleaned" and index >= next_index) or (phase == "verified" and index != next_index):
                    raise ValueError("中继上传验证和清理必须按冻结清单顺序进行")
                if phase != "cleaned" and occupied and index > next_index + config.prefetch_files:
                    raise ValueError("中继文件超出同任务有界预取窗口")
                if phase != "cleaned" and index > next_index and item.get("upload_started"):
                    raise ValueError("同一任务只允许一个当前文件上传")
            if phase != "cleaned":
                active_seen = True
            if phase in {"verified", "cleaned"}:
                verification = item.get("verification") or {}
                if (
                    not item.get("upload_key") or verification.get("status") != "ok"
                    or verification.get("file_count") != 1 or verification.get("total_size") != size
                ):
                    raise ValueError("中继缺少已验证的逐文件成功回执")
        if pipeline and reservations > 1:
            raise ValueError("中继只允许一个在途下载预留")
        return state

    def load(self) -> dict[str, Any] | None:
        # A Windows reader can temporarily deny the atomic rename even though
        # every writer is serialized. Share the short lock with readers too.
        with self._lock():
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, Any] | None:
        self._check_path(self.path)
        try:
            with self.path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return None
        return self._validate(value)

    def save(self, state: dict[str, Any]) -> None:
        with self._lock():
            current = self.load()
            if state.get("version") == 2 and (current is None or current["version"] != 2):
                raise ValueError("中继并发清单必须通过enable_pipeline安全升级")
            if current is not None and current["version"] == 2:
                if state.get("version") != 2 or state.get("revision") != current["revision"]:
                    raise BlockingIOError(errno.EAGAIN, "中继清单已更新，禁止陈旧整表覆盖，请重新读取或使用mutate")
                if state.get("pipeline") != current["pipeline"]:
                    raise ValueError("不允许修改进行中任务已冻结的中继预算")
            self._write_unlocked(state, current)

    def _write_unlocked(self, state: dict[str, Any], previous: dict[str, Any] | None) -> None:
        value = state
        if state.get("version") == 2:
            value = dict(state, revision=(previous or {}).get("revision", 0) + 1)
        self._validate(value)
        self._check_path(self.path)
        self.root.mkdir(parents=True, exist_ok=True)
        if not write_operation_manifest(str(self.path), value):
            raise OSError("无法持久化逐文件中继回执，禁止删除缓存或开始下一文件")
        if value is not state:
            state["revision"] = value["revision"]

    def mutate(self, callback: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Read, mutate and durably publish one current revision under a short lock."""
        with self._lock():
            current = self.load()
            if current is None:
                raise ValueError("逐文件中继清单尚未初始化")
            state = copy.deepcopy(current)
            callback(state)
            if state.get("version") != current["version"]:
                raise ValueError("中继版本只能通过安全升级接口变更")
            if state.get("pipeline") != current.get("pipeline"):
                raise ValueError("不允许修改进行中任务已冻结的中继预算")
            if [item.get("path") for item in state["files"]] != [item["path"] for item in current["files"]]:
                raise ValueError("不允许修改已冻结的中继文件清单")
            self._write_unlocked(state, current)
            return state

    def update_entry(self, index: int, **fields: Any) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> None:
            self._entry_from(state, index, window=False).update(copy.deepcopy(fields))

        return self.mutate(update)

    def record_download(self, index: int, receipt_dict: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(receipt_dict, dict) or not receipt_dict:
            raise ValueError("中继下载回执不能为空")
        return self.update_entry(index, download_receipt=receipt_dict)

    def pause_prefetch(self, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("暂停预取必须记录原因")

        def pause(state: dict[str, Any]) -> None:
            # Cancellation is terminal for this pipeline, even if another
            # worker reports an older failure after cancellation was persisted.
            if state.get("prefetch_paused") != "cancelled":
                state["prefetch_paused"] = reason.strip()

        return self.mutate(pause)

    def resume_prefetch(self) -> dict[str, Any]:
        # Only the coordinator's reconciled, safe poll path may call this.
        def resume(state: dict[str, Any]) -> None:
            # Check inside the RMW lock, not against the caller's stale poll.
            if state.get("prefetch_paused") != "cancelled":
                state["prefetch_paused"] = ""

        return self.mutate(resume)

    def enable_pipeline(self, config: RelayPipelineConfig | None = None) -> dict[str, Any] | None:
        with self._lock():
            current = self.load()
            if current is None or current["version"] == 2:
                return current
            config = config or RelayPipelineConfig.from_env()
            self._wait_for_other_task_cache()
            state = copy.deepcopy(current)
            state.update(version=2, pipeline=config.to_dict(), revision=0, prefetch_paused="")
            for entry in state["files"]:
                # A v1 prepare may already have launched a worker. Its durable
                # size becomes a full reservation, including after a restart.
                entry["download_reserved"] = entry["state"] == "pending" and entry["size"] is not None
            self._write_unlocked(state, current)
            return state

    def increase_cache_budget(self, new_limit: int) -> dict[str, Any]:
        """Explicitly raise only the cache cap so an oversized file can resume."""
        if type(new_limit) is not int or new_limit <= 0:
            raise ValueError("中继缓存新上限必须是正整数")
        with self._lock():
            current = self.load()
            if current is None or current["version"] != 2:
                raise ValueError("只能提高已启用流水线任务的缓存上限")
            if new_limit <= current["pipeline"]["cache_max_bytes"]:
                raise ValueError("中继缓存上限只能提高，不能降低或重复设置")
            state = copy.deepcopy(current)
            state["pipeline"]["cache_max_bytes"] = new_limit
            self._write_unlocked(state, current)
            return state

    def apply_environment_budget_increase(self) -> dict[str, Any] | None:
        """Apply an explicitly configured increase, never a default or decrease."""
        current = self.load()
        raw = os.environ.get("RELAY_CACHE_MAX_BYTES")
        if raw is None or current is None or current["version"] != 2:
            return current
        raw = raw.strip()
        if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
            raise ValueError("RELAY_CACHE_MAX_BYTES 必须是正整数，现有中继预算未修改")
        new_limit = int(raw)
        if new_limit <= current["pipeline"]["cache_max_bytes"]:
            return current
        try:
            return self.increase_cache_budget(new_limit)
        except ValueError:
            # Another poll/worker may already have raised the cap after our
            # read. Never undo its larger value or fail an idempotent replay.
            latest = self.load()
            if latest and latest["version"] == 2 and latest["pipeline"]["cache_max_bytes"] >= new_limit:
                return latest
            raise

    def initialize(self, paths: list[str]) -> dict[str, Any]:
        if len(paths) != len(set(paths)):
            raise ValueError("来源清单存在重复文件")
        paths = sorted(paths)
        with self._lock():
            old = self.load()
            if old is not None:
                if [item["path"] for item in old["files"]] != paths:
                    raise ValueError("来源文件清单在中继过程中发生变化，保留现有回执并停止")
                return old
            state = {
                "version": 1, "job_id": self.job_id, "plan_key": self.plan_key,
                "files": [{"path": path, "state": "pending", "size": None} for path in paths],
            }
            self._write_unlocked(state, None)
            return state

    @staticmethod
    def next_index(state: dict[str, Any]) -> int:
        return next((index for index, item in enumerate(state["files"]) if item["state"] != "cleaned"), len(state["files"]))

    def _entry(self, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self.load()
        if state is None:
            raise ValueError("逐文件中继清单尚未初始化")
        return state, self._entry_from(state, index)

    def _entry_from(self, state: dict[str, Any], index: int, *, window: bool = True) -> dict[str, Any]:
        if type(index) is not int or index < 0 or index >= len(state["files"]):
            raise ValueError("逐文件中继序号不在冻结清单内")
        depth = RelayPipelineConfig.from_dict(state["pipeline"]).prefetch_files if state["version"] == 2 else 0
        if window and index > self.next_index(state) + depth:
            if state["version"] == 2:
                raise BlockingIOError(errno.EAGAIN, "文件尚未进入同任务预取窗口，等待当前文件上传并清理")
            raise ValueError("上一个文件尚未清理，不允许开始下载下一个")
        return state["files"][index]

    def candidate_indexes(self) -> list[int]:
        state = self.load()
        if state is None or state.get("prefetch_paused"):
            return []
        first = self.next_index(state)
        depth = RelayPipelineConfig.from_dict(state["pipeline"]).prefetch_files if state["version"] == 2 else 0
        pending = state["files"][first:first + depth + 1]
        if any(item["state"] == "pending" and (
            item.get("download_reserved") or item.get("download_submitting") or item.get("download_receipt")
        ) for item in pending):
            return []
        return [
            index for index in range(first, min(len(state["files"]), first + depth + 1))
            if state["files"][index]["state"] == "pending" and not state["files"][index].get("download_receipt")
        ]

    def prepare_file(self, index: int, size: int) -> None:
        if type(size) is not int or size < 0:
            raise ValueError("下载前必须确认文件大小，禁止未知大小的预下载")
        with self._lock():
            state, entry = self._entry(index)
            if state["version"] == 2:
                self._prepare_pipeline(state, entry, index, size)
            else:
                self._prepare_serial(state, entry, size)
            previous = self.load()
            self._write_unlocked(state, previous)

    def _prepare_serial(self, state: dict[str, Any], entry: dict[str, Any], size: int) -> None:
        if entry["state"] != "pending":
            raise ValueError("当前文件不处于待下载状态")
        if entry["size"] is not None and entry["size"] != size:
            raise ValueError("源文件大小已变化，拒绝复用旧下载回执")
        self._wait_for_other_task_cache()
        target = Path(self.file_path(entry["path"]))
        complete = target.is_file() and target.stat().st_size == size
        free = shutil.disk_usage(self.root).free
        required = (0 if complete else size) + self.RESERVE_BYTES
        if free < required:
            raise OSError(
                errno.ENOSPC,
                f"中继盘空间不足：{entry['path']} 需要 {required} 字节（含64MiB安全余量），可用 {free} 字节；未开始下载",
            )
        entry["size"] = size

    def _prepare_pipeline(self, state: dict[str, Any], entry: dict[str, Any], index: int, size: int) -> None:
        if entry["state"] != "pending":
            raise ValueError("当前文件不处于待下载状态")
        if entry["size"] is not None and entry["size"] != size:
            raise ValueError("源文件大小已变化，拒绝复用旧下载回执")
        if state.get("prefetch_paused"):
            raise BlockingIOError(errno.EAGAIN, f"中继预取已暂停：{state['prefetch_paused']}")
        if any(item.get("download_reserved") for number, item in enumerate(state["files"]) if number != index):
            raise BlockingIOError(errno.EAGAIN, "同任务已有一个在途下载，等待其完成或恢复")
        self._wait_for_other_task_cache()
        config = RelayPipelineConfig.from_dict(state["pipeline"])
        local_files = self._cache_files(self.root)
        owned_paths = {item["path"] for item in state["files"] if item["state"] != "cleaned"}
        if set(local_files) - owned_paths:
            raise BlockingIOError(errno.EAGAIN, "中继目录含未登记缓存或旧版临时文件，已保留，需确认归属并清理后恢复")
        active_paths = {
            item["path"] for item in state["files"] if item["state"] in {"downloaded", "verified"}
            or item.get("download_reserved") or (item["state"] == "pending" and (
                item.get("download_receipt") or item.get("download_submitting")
            ))
        }
        active_paths.add(entry["path"])
        if len(active_paths | set(local_files)) > config.prefetch_files + 1:
            raise BlockingIOError(errno.EAGAIN, "当前任务缓存及预留文件已达到有界上限，保留已有文件并等待释放")

        # Full files are charged once. Missing/in-flight bytes remain charged
        # after crashes. Unknown partial files are never removed or discounted.
        committed = sum(local_files.values())
        remaining = 0
        for number, item in enumerate(state["files"]):
            if item["state"] == "cleaned":
                continue
            expected = size if number == index else item.get("size")
            occupied = number == index or item["state"] in {"downloaded", "verified"} or item.get("download_reserved")
            if not occupied or expected is None:
                continue
            actual = local_files.get(item["path"], 0)
            # The v2 worker writes the exact target using --inplace. Existing
            # target bytes consume this same reservation, not a second copy.
            # Old temporary/orphan paths are rejected above, never deleted.
            missing = max(0, expected - actual)
            committed += missing
            if item["state"] == "pending":
                remaining += missing
        if committed > config.cache_max_bytes:
            raise BlockingIOError(errno.EAGAIN, f"中继全局缓存预算不足：需{committed}字节，上限{config.cache_max_bytes}字节")
        free = shutil.disk_usage(self.root).free
        if free < remaining + config.disk_reserve_bytes:
            raise BlockingIOError(
                errno.EAGAIN,
                f"中继磁盘余量不足：需{remaining + config.disk_reserve_bytes}字节，可用{free}字节，等待释放缓存",
            )
        entry.update(size=size, download_reserved=True)

    def check_space(self, index: int) -> None:
        state, entry = self._entry(index)
        if state["version"] == 1:
            return
        if entry["state"] != "pending" or not entry.get("download_reserved"):
            raise ValueError("下载过程缺少有效的持久预留")
        config = RelayPipelineConfig.from_dict(state["pipeline"])
        if state.get("prefetch_paused"):
            raise BlockingIOError(errno.EAGAIN, f"中继预取已暂停：{state['prefetch_paused']}")
        if shutil.disk_usage(self.root).free < config.disk_reserve_bytes:
            raise BlockingIOError(errno.EAGAIN, "中继实际磁盘余量不足，停止当前下载并保留部分文件")

    def _cache_files(self, directory: Path) -> dict[str, int]:
        self._check_path(directory)
        files = {}

        def failed(error: OSError) -> None:
            raise error

        for parent, directories, names in os.walk(directory, followlinks=False, onerror=failed):
            for name in directories:
                self._check_path(Path(parent, name))
            for name in names:
                path = Path(parent, name)
                self._check_path(path)
                if name.startswith(".fnos-relay-upload-") or name.endswith(".fnos-cmcc-upload.json"):
                    continue
                try:
                    details = path.stat()
                except FileNotFoundError:
                    continue  # The coordinator may have just released a file.
                if not stat.S_ISREG(details.st_mode):
                    raise ValueError("中继缓存包含非常规文件，拒绝估算空间")
                files[path.relative_to(directory).as_posix()] = details.st_size
        return files

    def _wait_for_other_task_cache(self) -> None:
        # All download workers share the existing rclone flock. Checking here
        # (inside that lock, immediately before download) closes the queue race
        # where task B starts downloading while task A is still uploading.
        for directory in self.root.parent.iterdir():
            if directory == self.root or not re.fullmatch(r"job-[1-9][0-9]*", directory.name):
                continue
            checkpoint = directory / self.NAME
            try:
                if self._cache_files(directory):
                    raise BlockingIOError(errno.EAGAIN, f"{directory.name} 仍有缓存或部分文件，等待其恢复或清理")
                if not checkpoint.exists():
                    continue
                self._check_path(checkpoint)
                with checkpoint.open(encoding="utf-8") as handle:
                    peer = json.load(handle)
                if not isinstance(peer, dict) or peer.get("version") not in {1, 2} or not isinstance(peer.get("files"), list):
                    raise ValueError("无效中继清单")
                peer_store = RelayFileState(str(directory), job_id=int(directory.name[4:]), plan_key=peer.get("plan_key", ""))
                peer_store._validate(peer)
                occupied = any(
                    isinstance(item, dict) and (
                        item.get("state") in {"downloaded", "verified"} or item.get("download_reserved")
                        or (item.get("state") == "pending" and item.get("size") is not None)
                    )
                    for item in peer["files"]
                )
            except FileNotFoundError:
                continue  # A concurrently completed task was safely cleaned.
            except (OSError, ValueError):
                raise BlockingIOError(errno.EAGAIN, f"{directory.name} 的缓存回执暂不可确认，等待其恢复或清理")
            if occupied:
                raise BlockingIOError(errno.EAGAIN, f"{directory.name} 的文件尚未上传验证并释放缓存，暂不下载下一任务")

    def mark_downloaded(self, index: int, size: int) -> None:
        def update(state: dict[str, Any]) -> None:
            entry = self._entry_from(state, index)
            target = Path(self.file_path(entry["path"]))
            if (
                type(size) is not int or entry["state"] != "pending" or entry["size"] != size
                or not target.is_file() or target.stat().st_size != size
                or (state["version"] == 2 and not entry.get("download_reserved"))
            ):
                raise ValueError("下载文件与冻结清单大小不符，拒绝确认")
            entry["state"] = "downloaded"
            if state["version"] == 2:
                entry["download_reserved"] = False

        self.mutate(update)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("select", "prepare", "downloaded", "enable", "version", "check-space"))
    parser.add_argument("--local-dir", required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--plan-key", required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--size", type=int)
    args = parser.parse_args()
    try:
        store = RelayFileState(args.local_dir, job_id=args.job_id, plan_key=args.plan_key)
        if args.action == "select":
            prefix = f"job-{args.job_id}/"
            raw_paths = sys.stdin.read().splitlines()
            if not raw_paths or any(not path.startswith(prefix) for path in raw_paths):
                raise ValueError("来源清单不属于当前任务")
            store.initialize([path[len(prefix):] for path in raw_paths])
            _state, entry = store._entry(args.index)
            if entry["state"] == "pending":
                print(prefix + entry["path"])
        elif args.action == "prepare":
            store.prepare_file(args.index, args.size)
        elif args.action == "enable":
            if store.enable_pipeline() is None:
                raise ValueError("逐文件中继清单尚未初始化")
            store.apply_environment_budget_increase()
        elif args.action == "version":
            state = store.load()
            if state is None:
                raise ValueError("逐文件中继清单尚未初始化")
            print(state["version"])
        elif args.action == "check-space":
            store.check_space(args.index)
        else:
            store.mark_downloaded(args.index, args.size)
        return 0
    except BlockingIOError as exc:
        print(f"逐文件中继等待缓存：{exc}", file=sys.stderr)
        return 75
    except (OSError, ValueError, TypeError) as exc:
        print(f"逐文件中继安全检查失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
