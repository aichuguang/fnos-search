from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable


_CHANNELS = ("download", "upload", "hash")
_TERMINAL_PHASES = frozenset({"completed", "failed", "manual_review", "cancelled"})
_POST_TRANSFER_PHASES = frozenset({
    "verifying", "target_verifying", "organizing", "finalizing", "final_verifying", "ack_pending", "cleanup_pending",
})
_START_PHASES = frozenset({"planning", "retry_wait"})
_PHASES = frozenset({
    "planning", "source_materializing", "source_waiting_visible", "native_submitting", "native_waiting",
    "transferring", "target_verifying", "organizing", "finalizing", "final_verifying", "ack_pending",
    "cleanup_pending", "retry_wait", "cancel_pending", "manual_review", "completed", "failed", "cancelled",
})
_TIMESTAMP = re.compile(r"^\[\d{4}[-/]\d{2}[-/]\d{2}[^\]]*\]\s*")
_JOB_PREFIX = re.compile(r"^任务\s*#(?P<job_id>\d+)\s*(?P<message>.*)$")
_MILESTONE = re.compile(r"^入库阶段[：:]\s*(?P<phase>[a-z_]+)(?:[；;]\s*(?P<message>.*))?$")
_PROGRESS = re.compile(
    r"^(?P<label>下载中|上传中|秒传校验)\s*(?P<filename>.*)[：:]\s*"
    r"(?P<done>[^/，,]+)\s*/\s*(?P<total>[^，,]+)[，,]\s*(?P<percent>[^%％，,]+)[%％]"
    r"(?:[，,]\s*速度\s*(?P<speed>[^，,]+))?(?:[，,]\s*(?:预计剩余|ETA)\s*(?P<eta>.+))?\s*$"
)
_PERCENT_ONLY = re.compile(r"^(?P<label>下载中|上传中|秒传校验)\s*[：:]\s*(?P<percent>\d+(?:\.\d+)?)\s*[%％]$")
_DOWNLOAD_FILE = re.compile(r"^下载文件[：:]\s*(?P<filename>.*?)\s+size\s*=\s*(?P<size>\S+(?:\s+[A-Za-z]+)?)\s*$")
_SIZE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]*)$")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|d|h|m|s|天|小时|分钟|分|秒)")
_STAGE_LABELS = {"下载中": "download", "上传中": "upload", "秒传校验": "hash"}


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def log_scope(
    message: str, *, job_id: int | None = None, current_job_id: int | None = None,
) -> tuple[int | None, int | None, str]:
    text = _TIMESTAMP.sub("", str(message or "").strip(), count=1)[:8192]
    prefix = _JOB_PREFIX.fullmatch(text)
    prefixed_job = _positive_id(prefix["job_id"]) if prefix else None
    if prefix:
        text = prefix["message"].strip()
    current = _positive_id(current_job_id)
    return _positive_id(job_id) or prefixed_job or current, current, text


def _number(value: Any, *, maximum: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 and (maximum is None or number <= maximum) else None


def _bytes(value: Any) -> int | None:
    matched = _SIZE.fullmatch(str(value or "").strip())
    if not matched:
        return None
    number = _number(matched["number"])
    unit = matched["unit"].lower()
    if number is None:
        return None
    if unit in {"", "b", "byte", "bytes"}:
        multiplier = 1
    else:
        unit = unit.replace("bytes", "b").replace("byte", "b")
        prefixes = {"k": 1, "m": 2, "g": 3, "t": 4, "p": 5}
        if unit[:1] not in prefixes or unit[1:] not in {"", "b", "ib"}:
            return None
        multiplier = (1024 if "i" in unit else 1000) ** prefixes[unit[0]]
    amount = number * multiplier
    return round(amount) if math.isfinite(amount) else None


def _seconds(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}", text):
        parts = [int(part) for part in text.split(":")]
        if any(part >= 60 for part in parts[1:]):
            return None
        return float(sum(part * (60 ** index) for index, part in enumerate(reversed(parts))))
    matches = list(_DURATION.finditer(text))
    if not matches or _DURATION.sub("", text).strip():
        return None
    scales = {"d": 86400, "天": 86400, "h": 3600, "小时": 3600, "m": 60, "分": 60, "分钟": 60, "s": 1, "秒": 1, "ms": 0.001}
    total = sum(float(match[1]) * scales[match[2]] for match in matches)
    return round(total, 3) if math.isfinite(total) else None


def _channel() -> dict[str, Any]:
    return {
        "state": "unknown", "scope": "file", "filename": None, "percent": None,
        "bytes_done": None, "bytes_total": None, "speed_bps": None, "eta_seconds": None,
    }


def _event(message: str) -> dict[str, Any] | None:
    milestone = _MILESTONE.fullmatch(message)
    if milestone:
        phase = milestone["phase"]
        return {"kind": "milestone", "stage": phase} if phase in _PHASES else None
    if message.startswith("开始执行 rclone 搬运任务"):
        return {"kind": "start", "stage": "download"}
    if message.startswith("开始上传：") or message.startswith("开始上传:"):
        return {"kind": "upload_start", "stage": "upload", "channel": "upload", "values": {"state": "waiting"}}
    if message.startswith("上传请求仍在执行：") or message.startswith("上传请求仍在执行:"):
        return {"kind": "heartbeat", "stage": "upload"}
    if message.startswith(("等待进度：", "等待进度:", "进度暂未变化：", "进度暂未变化:")):
        return {"kind": "heartbeat", "stage": "download", "heartbeat_channel": "download"}
    if message.startswith(("本地校验仍在执行：", "本地校验仍在执行:")):
        return {"kind": "heartbeat", "stage": "hash", "heartbeat_channel": "hash"}
    initial = _DOWNLOAD_FILE.fullmatch(message)
    if initial:
        return {
            "kind": "initial", "stage": "waiting_download", "channel": "download",
            "values": {"state": "waiting", "filename": initial["filename"].strip() or None, "bytes_total": _bytes(initial["size"])},
        }
    matched = _PROGRESS.fullmatch(message)
    if matched:
        channel = _STAGE_LABELS[matched["label"]]
        return {
            "kind": "progress", "stage": channel, "channel": channel,
            "values": {
                "state": "running", "filename": matched["filename"].strip() or None,
                "percent": _number(matched["percent"].strip(), maximum=100),
                "bytes_done": _bytes(matched["done"]), "bytes_total": _bytes(matched["total"]),
                "speed_bps": _bytes(re.sub(r"/s$", "", str(matched["speed"] or "").strip())),
                "eta_seconds": _seconds(matched["eta"]),
            },
        }
    matched = _PERCENT_ONLY.fullmatch(message)
    if matched:
        channel = _STAGE_LABELS[matched["label"]]
        return {
            "kind": "progress", "stage": channel, "channel": channel,
            "values": {"state": "running", "scope": "stage", "filename": None, "percent": _number(matched["percent"], maximum=100)},
        }
    if message.startswith("rclone 下载完成") and "已移交网盘上传" in message:
        return {"kind": "channel_done", "stage": "waiting_upload", "channel": "download"}
    if message.startswith("上传完成：") or message.startswith("上传完成:"):
        return {"kind": "channel_done", "stage": "verifying", "channel": "upload"}
    return None


class RcloneLiveProgress:
    """Bounded, read-side telemetry; never a source of business completion."""

    def __init__(
        self, *, max_tasks: int = 50, stale_after: float = 15,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._max_tasks = min(50, max(1, int(max_tasks)))
        self._stale_after = max(0, float(stale_after))
        self._monotonic = monotonic
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._tasks: OrderedDict[int, dict[str, Any]] = OrderedDict()

    def record(
        self, message: str, *, job_id: int | None = None,
        current_job_id: int | None = None, current_run_id: int | None = None,
        trusted_milestone: bool = False,
    ) -> bool:
        target_job, context_job, text = log_scope(message, job_id=job_id, current_job_id=current_job_id)
        event = _event(text)
        if target_job is None or event is None:
            return False
        if event["kind"] == "milestone" and trusted_milestone is not True:
            return False
        # A different job's upload can arrive while the current rclone run is
        # downloading another job. Never assign that run id to the upload.
        run_id = _positive_id(current_run_id) if target_job == context_job else None
        now = self._monotonic()
        timestamp = self._utc_now().isoformat(timespec="milliseconds")
        with self._lock:
            previous = self._tasks.get(target_job)
            reset = event["kind"] == "milestone" and event["stage"] in _START_PHASES
            new_run = event["kind"] == "start" and run_id is not None and (previous is None or previous["run_id"] != run_id)
            terminal_update = event["kind"] == "milestone" and event["stage"] in _TERMINAL_PHASES
            if previous and previous["stage"] in _TERMINAL_PHASES and not (reset or new_run):
                if not terminal_update or previous["stage"] == "completed":
                    return False
            if previous and previous["stage"] in _POST_TRANSFER_PHASES and event["kind"] != "milestone" and not new_run:
                return False
            if previous is None or (reset and event["stage"] == "planning") or new_run:
                item = {"job_id": target_job, "run_id": run_id, "completed": False, **{name: _channel() for name in _CHANNELS}}
            else:
                item = previous
                if run_id is not None:
                    item["run_id"] = run_id
            channel = event.get("channel")
            if channel:
                if item[channel]["state"] == "completed" and event["kind"] == "progress":
                    return False
                if event["kind"] == "channel_done":
                    item[channel]["state"] = "completed"
                    item[channel]["speed_bps"] = None
                    item[channel]["eta_seconds"] = None
                else:
                    old_values = item[channel]
                    values = dict(event["values"])
                    changed = any(
                        values.get(key) is not None and values.get(key) != old_values.get(key)
                        for key in ("filename", "percent", "bytes_done", "bytes_total")
                    )
                    if changed or event["kind"] in {"initial", "upload_start"}:
                        item["_progress_at"] = now
                        item.pop("_waiting_channel", None)
                    elif event["kind"] == "progress" and (
                        item.get("_waiting_channel") == channel
                        or now - item.get("_progress_at", now) > self._stale_after
                    ):
                        values.update(state="waiting", speed_bps=None, eta_seconds=None)
                    # Each observation describes one file, not aggregate task
                    # bytes. A new file must not inherit the previous metrics.
                    item[channel] = {**_channel(), **values}
                item["_active_channel"] = channel
            if event["kind"] == "heartbeat":
                active = event.get("heartbeat_channel") or item.get("_active_channel", event["stage"])
                if item[active]["state"] not in {"completed", "failed", "manual_review", "cancelled"}:
                    item["_waiting_channel"] = active
                    item[active]["state"] = "waiting"
                    item[active]["speed_bps"] = None
                    item[active]["eta_seconds"] = None
            stage = item["stage"] if event["kind"] == "heartbeat" and "stage" in item else event["stage"]
            if stage in _TERMINAL_PHASES:
                for name in _CHANNELS:
                    if item[name]["state"] in {"waiting", "running"}:
                        item[name]["state"] = stage
                        item[name]["speed_bps"] = None
                        item[name]["eta_seconds"] = None
            elif stage in _POST_TRANSFER_PHASES:
                for name in _CHANNELS:
                    item[name]["speed_bps"] = None
                    item[name]["eta_seconds"] = None
            item.update({
                "stage": stage, "completed": stage == "completed",
                "updated_at": timestamp, "message": text,
            })
            if event["kind"] in {"milestone", "start", "initial", "upload_start", "channel_done"} or "_seen_at" not in item:
                item["_seen_at"] = now
            self._tasks[target_job] = item
            self._tasks.move_to_end(target_job)
            while len(self._tasks) > self._max_tasks:
                self._tasks.popitem(last=False)
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        now = self._monotonic()
        with self._lock:
            result = []
            for item in reversed(self._tasks.values()):
                age = max(0.0, now - item.get("_progress_at", item["_seen_at"]))
                stale = age > self._stale_after and item["stage"] not in _TERMINAL_PHASES
                channels = {name: dict(item[name]) for name in _CHANNELS}
                if stale:
                    for values in channels.values():
                        values.update(speed_bps=None, eta_seconds=None)
                result.append({
                    **{key: value for key, value in item.items() if not key.startswith("_")},
                    **channels,
                    "age_seconds": round(age, 3),
                    "stale": stale,
                })
            return result
