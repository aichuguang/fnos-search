from __future__ import annotations

import json
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
        "file_id": None, "run_id": None, "active": False,
    }


def _event(message: str) -> dict[str, Any] | None:
    if message.startswith("relay-progress:"):
        try:
            payload = json.loads(message[len("relay-progress:"):].strip())
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or _positive_id(payload.get("job_id")) is None:
            return None
        if not all(isinstance(payload.get(key), str) and payload[key].strip() for key in ("file_id", "filename", "message")):
            return None
        nested_message = payload["message"]
        # Only wrap ordinary upload observations, never durable milestones or
        # another protocol envelope supplied by a provider diagnostic.
        if nested_message.startswith("relay-"):
            return None
        nested = _event(nested_message)
        if nested is None or nested["kind"] not in {"upload_start", "progress", "heartbeat", "channel_done"}:
            return None
        if nested.get("channel", nested.get("heartbeat_channel")) not in {"upload", "hash"}:
            return None
        return {**nested, "job_id": payload["job_id"], "identity": {
            "file_id": payload["file_id"][:2048], "filename": payload["filename"][:2048],
            "run_id": _positive_id(payload.get("run_id")),
        }}
    for prefix, kind in (("relay-buffer:", "buffer"), ("relay-file:", "file_context")):
        if not message.startswith(prefix):
            continue
        try:
            payload = json.loads(message[len(prefix):].strip())
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or _positive_id(payload.get("job_id")) is None:
            return None
        if kind == "buffer":
            depth, ready, reserved = (payload.get(key) for key in ("depth", "ready", "reserved"))
            if (type(depth) is not int or depth not in {0, 1, 2}
                    or type(ready) is not int or ready < 0 or ready > depth
                    or type(reserved) is not int or reserved < 0 or reserved > depth
                    or type(payload.get("waiting")) is not bool):
                return None
            return {"kind": kind, "job_id": payload["job_id"], "values": {
                "enabled": payload.get("enabled") is True, "depth": depth, "ready": ready,
                "reserved": reserved, "waiting": payload["waiting"], "reason": str(payload.get("reason") or "")[:512],
            }}
        channel = payload.get("channel")
        filename, file_id = payload.get("filename"), payload.get("file_id")
        if channel not in {"download", "upload"} or not isinstance(filename, str) or not filename.strip():
            return None
        if not isinstance(file_id, str) or not file_id.strip():
            return None
        return {"kind": kind, "job_id": payload["job_id"], "stage": channel, "channel": channel,
                "values": {"filename": filename[:2048], "file_id": file_id[:2048],
                           "run_id": _positive_id(payload.get("run_id")), "state": "waiting"}}
    milestone = _MILESTONE.fullmatch(message)
    if milestone:
        phase = milestone["phase"]
        return {"kind": "milestone", "stage": phase} if phase in _PHASES else None
    if message.startswith("开始执行 rclone 搬运任务"):
        return {"kind": "start", "stage": "download"}
    if message.startswith("开始上传：") or message.startswith("开始上传:"):
        return {"kind": "upload_start", "stage": "upload", "channel": "upload", "values": {"state": "waiting"}}
    if message.startswith("上传请求仍在执行：") or message.startswith("上传请求仍在执行:"):
        return {"kind": "heartbeat", "stage": "upload", "heartbeat_channel": "upload"}
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
        if event is None:
            return False
        protocol_job = _positive_id(event.get("job_id"))
        if protocol_job is not None:
            # Structured relay logs carry their own task identity. An explicit
            # bridge scope must agree; the current download can be another job.
            explicit_job, _, _ = log_scope(message, job_id=job_id)
            if explicit_job is not None and explicit_job != protocol_job:
                return False
            target_job = protocol_job
        if target_job is None:
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
            kind = event["kind"]
            reset = kind == "milestone" and event["stage"] in _START_PHASES
            new_run = kind == "start" and run_id is not None and (previous is None or previous["run_id"] != run_id)
            terminal_update = kind == "milestone" and event["stage"] in _TERMINAL_PHASES
            if previous and previous["stage"] in _TERMINAL_PHASES and not (reset or new_run):
                if not terminal_update or previous["stage"] == "completed":
                    return False
            if previous and previous["stage"] in _POST_TRANSFER_PHASES and kind != "milestone" and not new_run:
                return False
            pipeline = bool(previous and previous.get("relay_buffer", {}).get("enabled") is True)
            keep_upload = new_run and pipeline and previous["stage"] not in _TERMINAL_PHASES | _POST_TRANSFER_PHASES
            if previous is None or (reset and event["stage"] == "planning") or (new_run and not keep_upload):
                item = {"job_id": target_job, "run_id": run_id, "stage": "transferring", "completed": False,
                        "_upload_done": set(), "_upload_bytes_done": 0, "_upload_seen": {},
                        **{name: _channel() for name in _CHANNELS}}
            else:
                item = previous
                if run_id is not None and (kind == "start" or event.get("channel") == "download"):
                    item["run_id"] = run_id
            item.setdefault("_upload_done", set())
            item.setdefault("_upload_bytes_done", 0)
            item.setdefault("_upload_seen", {})
            if reset:
                item.pop("relay_buffer", None)
                item.pop("_file_contexts", None)
                item["_upload_done"] = set()
                item["_upload_bytes_done"] = 0
                item["_upload_seen"] = {}
                pipeline = False
            if kind == "buffer":
                item["relay_buffer"] = {**event["values"], "updated_at": timestamp}
                item.setdefault("updated_at", timestamp)
                item.setdefault("message", "")
                item.setdefault("_seen_at", now)
                self._remember(target_job, item)
                return True
            identity = event.get("identity")
            if identity:
                contexts = item.setdefault("_file_contexts", {})
                current_identity = contexts.get("upload")
                if current_identity and current_identity["file_id"] != identity["file_id"]:
                    return False
                if current_identity is None:
                    contexts["upload"] = identity
            if new_run and keep_upload:
                context = item.get("_file_contexts", {}).get("download", {})
                if context.get("run_id") not in {None, run_id}:
                    item["_file_contexts"].pop("download", None)
                    context = {}
                item["download"] = {**_channel(), **context}
            if kind == "start" and run_id is not None:
                context = item.get("_file_contexts", {}).get("download")
                if context is not None:
                    context["run_id"] = run_id
            channel = event.get("channel")
            if channel:
                old_values = item[channel]
                contexts = item.setdefault("_file_contexts", {})
                context_key = "upload" if channel == "hash" else channel
                if kind == "file_context":
                    context = {key: event["values"][key] for key in ("filename", "file_id", "run_id")}
                    if contexts.get(context_key) == context:
                        return True
                    contexts[context_key] = context
                context = contexts.get(context_key, {})
                upload_key = ""
                if channel == "upload":
                    upload_key = str(
                        old_values.get("file_id")
                        or context.get("file_id")
                        or old_values.get("filename")
                        or context.get("filename")
                        or ""
                    )
                    upload_total = old_values.get("bytes_total")
                    if upload_key and upload_total is not None:
                        item["_upload_seen"].setdefault(upload_key, _number(upload_total))
                if kind == "channel_done":
                    if channel == "upload" and upload_key and upload_key not in item["_upload_done"]:
                        item["_upload_done"].add(upload_key)
                        completed_bytes = (
                            old_values.get("bytes_total")
                            if old_values.get("bytes_total") is not None
                            else old_values.get("bytes_done")
                        )
                        item["_upload_bytes_done"] += int(_number(completed_bytes) or 0)
                    old_values.update(state="completed", active=False, speed_bps=None, eta_seconds=None,
                                      updated_at=timestamp)
                    if channel == "upload":
                        item["hash"].update(active=False, speed_bps=None, eta_seconds=None)
                else:
                    values = dict(event["values"])
                    filename = values.get("filename")
                    if context and values.get("scope") == "stage":
                        values["scope"] = "file"
                    if context and filename and kind != "file_context":
                        expected = context["filename"].replace("\\", "/")
                        observed = filename.replace("\\", "/")
                        if observed not in {expected, expected.rsplit("/", 1)[-1]}:
                            return False
                        values["filename"] = context["filename"]
                    next_file = bool(filename and filename != old_values.get("filename") and not context)
                    if old_values["state"] == "completed" and kind == "progress" and not next_file:
                        return False
                    changed = any(
                        values.get(key) is not None and values.get(key) != old_values.get(key)
                        for key in ("filename", "percent", "bytes_done", "bytes_total")
                    )
                    progress_at = old_values.get("_progress_at", now)
                    waiting = old_values.get("_waiting", False)
                    if changed or kind in {"initial", "upload_start", "file_context"}:
                        progress_at, waiting = now, False
                    elif kind == "progress" and (
                        waiting or now - progress_at > self._stale_after
                    ):
                        values.update(state="waiting", speed_bps=None, eta_seconds=None)
                    # Each observation describes one file, not aggregate task
                    # bytes. Channel clocks/identity never follow another lane.
                    identity_run = context.get("run_id") if context else (run_id if channel == "download" or not pipeline else None)
                    item[channel] = {**_channel(), **values, **context, "run_id": identity_run,
                                     "active": True, "updated_at": timestamp, "_seen_at": now,
                                     "_progress_at": progress_at, "_waiting": waiting}
                if kind != "channel_done":
                    for other in _CHANNELS:
                        if other != channel and (not pipeline or {other, channel} <= {"hash", "upload"}):
                            item[other]["active"] = False
                    if kind in {"upload_start", "file_context"} and channel == "upload":
                        # A new file's preparation cannot retain an old hash.
                        item["hash"] = _channel()
            if kind == "heartbeat":
                active = event["heartbeat_channel"]
                if item[active]["state"] in {"running", "waiting"} and item[active]["active"]:
                    item[active]["_waiting"] = True
                    item[active]["state"] = "waiting"
                    item[active]["speed_bps"] = None
                    item[active]["eta_seconds"] = None
                    item[active]["updated_at"] = timestamp
            stage = item["stage"] if kind == "heartbeat" else event["stage"]
            if pipeline and kind == "channel_done":
                # A file/run boundary is not the task's verification boundary.
                stage = "transferring"
            if stage in _TERMINAL_PHASES:
                for name in _CHANNELS:
                    if item[name]["state"] in {"waiting", "running"}:
                        item[name]["state"] = stage
                    item[name].update(active=False, speed_bps=None, eta_seconds=None)
            elif stage in _POST_TRANSFER_PHASES:
                for name in _CHANNELS:
                    item[name].update(active=False, speed_bps=None, eta_seconds=None)
            item.update({
                "stage": stage, "completed": stage == "completed",
                "updated_at": timestamp, "message": text,
            })
            if kind in {"milestone", "start", "initial", "upload_start", "channel_done", "file_context"} or "_seen_at" not in item:
                item["_seen_at"] = now
            self._remember(target_job, item)
        return True

    def _remember(self, job_id: int, item: dict[str, Any]) -> None:
        self._tasks[job_id] = item
        self._tasks.move_to_end(job_id)
        while len(self._tasks) > self._max_tasks:
            self._tasks.popitem(last=False)

    def snapshot(self) -> list[dict[str, Any]]:
        now = self._monotonic()
        with self._lock:
            result = []
            for item in reversed(self._tasks.values()):
                clocks = [value["_progress_at"] for name in _CHANNELS
                          if (value := item[name]).get("active") and "_progress_at" in value]
                age = max(0.0, now - max(clocks or [item["_seen_at"]]))
                stale = age > self._stale_after and item["stage"] not in _TERMINAL_PHASES
                channels = {}
                for name in _CHANNELS:
                    source = item[name]
                    values = {key: value for key, value in source.items() if not key.startswith("_")}
                    channel_age = max(0.0, now - source.get("_progress_at", now)) if source["active"] else None
                    channel_stale = channel_age is not None and channel_age > self._stale_after
                    values.update(age_seconds=round(channel_age, 3) if channel_age is not None else None, stale=channel_stale)
                    if channel_stale or not source["active"]:
                        values.update(speed_bps=None, eta_seconds=None)
                    channels[name] = values
                result.append({
                    **{key: value for key, value in item.items() if not key.startswith("_")},
                    **channels,
                    **({"relay_buffer": dict(item["relay_buffer"])} if "relay_buffer" in item else {}),
                    "upload_files_done": len(item.get("_upload_done") or ()),
                    "upload_bytes_done": int(item.get("_upload_bytes_done") or 0),
                    "upload_files_seen": len(item.get("_upload_seen") or ()),
                    "age_seconds": round(age, 3),
                    "stale": stale,
                })
            return result
