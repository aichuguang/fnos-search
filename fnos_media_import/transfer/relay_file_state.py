"""Durable, job-scoped checkpoints shared by the relay worker and coordinator."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from .relay_upload_manifest import write_operation_manifest


class RelayFileState:
    NAME = ".fnos-relay-upload-sequential.json"
    RESERVE_BYTES = 64 * 1024 * 1024

    def __init__(self, local_dir: str, *, job_id: int, plan_key: str) -> None:
        self.root = Path(local_dir).absolute()
        self.job_id = int(job_id)
        self.plan_key = str(plan_key)
        if self.job_id <= 0 or self.root.name != f"job-{self.job_id}" or not self.plan_key.strip():
            raise ValueError("逐文件中继缺少冻结的任务目录或操作身份")
        self._check_path(self.root)
        self.path = self.root / self.NAME

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
            state.get("version") != 1 or state.get("job_id") != self.job_id
            or state.get("plan_key") != self.plan_key
            or not isinstance(state.get("files"), list) or not state["files"]
        ):
            raise ValueError("逐文件中继清单与冻结任务不一致")
        paths = set()
        active_seen = False
        for item in state["files"]:
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
            if active_seen and phase != "pending":
                raise ValueError("前一个文件尚未释放，不允许推进后续文件")
            if phase != "cleaned":
                active_seen = True
            if phase in {"verified", "cleaned"}:
                verification = item.get("verification") or {}
                if (
                    not item.get("upload_key") or verification.get("status") != "ok"
                    or verification.get("file_count") != 1 or verification.get("total_size") != size
                ):
                    raise ValueError("中继缺少已验证的逐文件成功回执")
        return state

    def load(self) -> dict[str, Any] | None:
        self._check_path(self.path)
        try:
            with self.path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return None
        return self._validate(value)

    def save(self, state: dict[str, Any]) -> None:
        self._validate(state)
        self._check_path(self.path)
        self.root.mkdir(parents=True, exist_ok=True)
        if not write_operation_manifest(str(self.path), state):
            raise OSError("无法持久化逐文件中继回执，禁止删除缓存或开始下一文件")

    def initialize(self, paths: list[str]) -> dict[str, Any]:
        if len(paths) != len(set(paths)):
            raise ValueError("来源清单存在重复文件")
        paths = sorted(paths)
        old = self.load()
        if old is not None:
            if [item["path"] for item in old["files"]] != paths:
                raise ValueError("来源文件清单在中继过程中发生变化，保留现有回执并停止")
            return old
        state = {
            "version": 1, "job_id": self.job_id, "plan_key": self.plan_key,
            "files": [{"path": path, "state": "pending", "size": None} for path in paths],
        }
        self.save(state)
        return state

    @staticmethod
    def next_index(state: dict[str, Any]) -> int:
        return next((index for index, item in enumerate(state["files"]) if item["state"] != "cleaned"), len(state["files"]))

    def _entry(self, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self.load()
        if state is None or index < 0 or index >= len(state["files"]):
            raise ValueError("逐文件中继序号不在冻结清单内")
        if index > self.next_index(state):
            raise ValueError("上一个文件尚未清理，不允许开始下载下一个")
        return state, state["files"][index]

    def prepare_file(self, index: int, size: int) -> None:
        state, entry = self._entry(index)
        if entry["state"] != "pending" or size < 0:
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
        self.save(state)

    def _wait_for_other_task_cache(self) -> None:
        # All download workers share the existing rclone flock. Checking here
        # (inside that lock, immediately before download) closes the queue race
        # where task B starts downloading while task A is still uploading.
        for directory in self.root.parent.iterdir():
            if directory == self.root or not re.fullmatch(r"job-[1-9][0-9]*", directory.name):
                continue
            checkpoint = directory / self.NAME
            if not checkpoint.exists():
                continue
            try:
                self._check_path(checkpoint)
                with checkpoint.open(encoding="utf-8") as handle:
                    peer = json.load(handle)
                if not isinstance(peer, dict) or peer.get("version") != 1 or not isinstance(peer.get("files"), list):
                    raise ValueError("无效中继清单")
                occupied = any(
                    isinstance(item, dict) and item.get("state") in {"downloaded", "verified"}
                    for item in peer["files"]
                )
            except FileNotFoundError:
                continue  # A concurrently completed task was safely cleaned.
            except (OSError, ValueError):
                raise BlockingIOError(errno.EAGAIN, f"{directory.name} 的缓存回执暂不可确认，等待其恢复或清理")
            if occupied:
                raise BlockingIOError(errno.EAGAIN, f"{directory.name} 的文件尚未上传验证并释放缓存，暂不下载下一任务")

    def mark_downloaded(self, index: int, size: int) -> None:
        state, entry = self._entry(index)
        target = Path(self.file_path(entry["path"]))
        if entry["state"] != "pending" or entry["size"] != size or not target.is_file() or target.stat().st_size != size:
            raise ValueError("下载文件与冻结清单大小不符，拒绝确认")
        entry["state"] = "downloaded"
        self.save(state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("select", "prepare", "downloaded"))
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
