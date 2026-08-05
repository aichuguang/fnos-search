from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .update_values import as_bool, positive_int


class UpdateNextRunPolicy:
    """Calculates the next update run after an execution result."""

    def __init__(
        self,
        *,
        scheduler_config: Callable[[], dict[str, Any]],
        compute_next_run: Callable[..., str],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.scheduler_config = scheduler_config
        self.compute_next_run = compute_next_run
        self.now = now or (lambda: datetime.now(timezone.utc))

    def next_run(
        self,
        subscription: dict[str, Any],
        *,
        next_episode_value: int | None,
        completed: int,
        submitted: int,
        pending_count: int = 0,
        failed_episodes: set[int] | None = None,
        unresolved_episodes: set[int] | None = None,
        target_episodes: set[tuple[int | None, int]],
        raw_data: dict[str, Any],
        pre_air_probe: bool = False,
    ) -> str:
        scheduler = self.scheduler_config()
        now = self.now()
        data = {**subscription, "next_episode": next_episode_value, "raw_data": raw_data}
        failed_episodes = {int(item) for item in failed_episodes or set() if int(item) > 0}
        unresolved_episodes = {int(item) for item in unresolved_episodes or set() if int(item) > 0}
        if failed_episodes:
            self._clear_tmdb_discovery_state(raw_data)
            raw_data.pop("missing_episode_retry", None)
            interval = max(
                5,
                int(
                    scheduler.get("failure_retry_interval_minutes")
                    or scheduler.get("empty_retry_interval_minutes")
                    or 30
                ),
            )
            raw_data["failed_import_retry"] = {
                "episodes": sorted(failed_episodes),
                "last_failure_at": _utc_now(),
                "interval_minutes": interval,
                "reason": "部分新增集入库失败，保留缺集并自动重试",
            }
            return _utc_text(now + timedelta(minutes=interval))
        raw_data.pop("failed_import_retry", None)
        if submitted and not completed:
            self._clear_tmdb_discovery_state(raw_data)
            raw_data.pop("missing_episode_retry", None)
            interval = self._pending_interval(scheduler)
            previous = raw_data.get("pending_import") if isinstance(raw_data.get("pending_import"), dict) else {}
            raw_data["pending_import"] = {
                "episode": min((episode for _season, episode in target_episodes), default=next_episode_value or 0),
                "submitted_count": submitted,
                "created_at": previous.get("created_at") or _utc_now(),
                "last_check_at": _utc_now(),
                "interval_minutes": interval,
                "reason": "新增集已提交入库，等待 OpenList/Organizer 完整确认",
            }
            return _utc_text(now + timedelta(minutes=interval))
        if pending_count:
            self._clear_tmdb_discovery_state(raw_data)
            raw_data.pop("missing_episode_retry", None)
            interval = self._pending_interval(scheduler)
            previous = raw_data.get("pending_import") if isinstance(raw_data.get("pending_import"), dict) else {}
            raw_data["pending_import"] = {
                **previous,
                "last_check_at": _utc_now(),
                "interval_minutes": interval,
                "reason": "已有追更候选提交后仍在等待整理确认",
            }
            return _utc_text(now + timedelta(minutes=interval))
        if completed and unresolved_episodes:
            self._clear_tmdb_discovery_state(raw_data)
            interval = max(
                5,
                int(
                    scheduler.get("failure_retry_interval_minutes")
                    or scheduler.get("empty_retry_interval_minutes")
                    or 30
                ),
            )
            raw_data["missing_episode_retry"] = {
                "episodes": sorted(unresolved_episodes),
                "last_check_at": _utc_now(),
                "interval_minutes": interval,
                "reason": "本轮已有集数完成，但仍有历史缺集未找到或未入库，继续自动补查",
            }
            return _utc_text(now + timedelta(minutes=interval))
        raw_data.pop("missing_episode_retry", None)
        if str(subscription.get("schedule_kind") or "") != "tmdb":
            self._clear_tmdb_discovery_state(raw_data)
            return self.compute_next_run(data, after=now + timedelta(seconds=1))

        retry_state = raw_data.get("tmdb_retry") if isinstance(raw_data.get("tmdb_retry"), dict) else {}
        if completed:
            for key in (
                "tmdb_probe",
                "tmdb_retry",
                "tmdb_wait",
                "pending_import",
                "pending_import_status",
                "pending_import_status_items",
            ):
                raw_data.pop(key, None)
            return self.compute_next_run(data, after=now + timedelta(seconds=1))

        target_episode = min((episode for _season, episode in target_episodes), default=0)
        if not target_episode:
            raw_data.pop("tmdb_probe", None)
            raw_data.pop("tmdb_retry", None)
            tmdb_schedule = raw_data.get("tmdb_schedule") if isinstance(raw_data.get("tmdb_schedule"), dict) else {}
            scheduled = self.compute_next_run(data, after=now + timedelta(seconds=1))
            wait_hours = max(1, int(scheduler.get("tmdb_wait_interval_hours") or scheduler.get("empty_retry_exhausted_interval_hours") or 6))
            local_latest = _to_int(subscription.get("last_success_episode"))
            if next_episode_value and next_episode_value > 1:
                local_latest = max(local_latest or 0, next_episode_value - 1)
            raw_data["tmdb_wait"] = {
                "local_latest_episode": local_latest,
                "next_episode": next_episode_value,
                "tmdb_next_air_episode": _to_int(tmdb_schedule.get("next_air_episode")) or _to_int(tmdb_schedule.get("episode")),
                "tmdb_latest_aired_episode": _to_int(tmdb_schedule.get("latest_aired_episode")) or _to_int(tmdb_schedule.get("last_air_episode")),
                "interval_hours": wait_hours,
                "last_check_at": _utc_now(),
                "next_run_at": scheduled,
                "reason": "当前没有到达 TMDB 播出日检查时间的目标集，等待下一集时间再检查",
            }
            return scheduled or _utc_text(now + timedelta(hours=wait_hours))

        if pre_air_probe:
            # 播出日前提前探测：miss 属预期，不消耗 tmdb_retry 补查预算，稍后再次探测
            raw_data.pop("tmdb_retry", None)
            raw_data.pop("tmdb_wait", None)
            interval = max(5, int(scheduler.get("empty_retry_interval_minutes") or 30))
            raw_data["tmdb_probe"] = {
                "episode": target_episode,
                "last_check_at": _utc_now(),
                "interval_minutes": interval,
                "reason": "播出日前提前探测未发现新增文件，稍后再次探测",
            }
            return _utc_text(now + timedelta(minutes=interval))

        raw_data.pop("tmdb_probe", None)
        raw_data.pop("tmdb_wait", None)
        retry_enabled = _as_bool(scheduler.get("empty_retry_enabled"), True)
        max_attempts = max(0, int(scheduler.get("empty_retry_max_attempts") or 4))
        interval = max(5, int(scheduler.get("empty_retry_interval_minutes") or 30))
        max_window_hours = max(0, int(scheduler.get("empty_retry_max_window_hours") or 12))
        exhausted_hours = max(1, int(scheduler.get("empty_retry_exhausted_interval_hours") or 6))
        same_episode = retry_state.get("episode") == target_episode
        attempts = int(retry_state.get("attempts") or 0) if same_episode else 0
        first_empty_at = str(retry_state.get("first_empty_at") or "") if same_episode else ""
        first_empty_at = first_empty_at or _utc_now()
        first_empty_dt = _parse_utc(first_empty_at)
        window_exceeded = bool(max_window_hours and first_empty_dt and now > first_empty_dt + timedelta(hours=max_window_hours))
        if retry_enabled and attempts < max_attempts and not window_exceeded:
            raw_data["tmdb_retry"] = {
                "episode": target_episode,
                "attempts": attempts + 1,
                "first_empty_at": first_empty_at,
                "last_empty_at": _utc_now(),
                "interval_minutes": interval,
                "max_attempts": max_attempts,
                "max_window_hours": max_window_hours,
                "reason": "TMDB 播出日已到但未发现新增文件，进入补查窗口",
            }
            return _utc_text(now + timedelta(minutes=interval))
        raw_data["tmdb_retry"] = {
            "episode": target_episode,
            "attempts": attempts,
            "first_empty_at": first_empty_at,
            "last_empty_at": _utc_now(),
            "interval_minutes": interval,
            "max_attempts": max_attempts,
            "max_window_hours": max_window_hours,
            "exhausted": True,
            "reason": "补查次数或补查窗口已耗尽，转为低频复查",
        }
        return _utc_text(now + timedelta(hours=exhausted_hours))

    @staticmethod
    def _pending_interval(config: dict[str, Any]) -> int:
        return max(5, int(config.get("pending_import_check_interval_minutes") or config.get("empty_retry_interval_minutes") or 30))

    @staticmethod
    def _clear_tmdb_discovery_state(raw_data: dict[str, Any]) -> None:
        for key in ("tmdb_probe", "tmdb_retry", "tmdb_wait"):
            raw_data.pop(key, None)


def _utc_now() -> str:
    return _utc_text(datetime.now(timezone.utc))


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    return positive_int(value)


def _as_bool(value: Any, default: bool = False) -> bool:
    return as_bool(value, default)
