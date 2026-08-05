from __future__ import annotations

from typing import Any, Callable

from ..constants import (
    COMPLETION_STAGE_WAITING_OPENLIST,
    COMPLETION_STAGE_WAITING_ORGANIZER,
    JOB_CANCELLED,
    JOB_CONFIRMING,
    JOB_FAILED,
    JOB_ORGANIZING,
    JOB_REVIEW,
    JOB_STATUS_LABELS,
    JOB_UNSUPPORTED,
    JOB_WAITING_OPENLIST,
    JOB_WAITING_ORGANIZER,
)
from ..database import utc_now

from .update_values import positive_int


EpisodeKey = tuple[int | None, int]


class UpdateCompletionSyncService:
    """Synchronizes update candidates from their associated import jobs."""

    def __init__(self, *, database: Any, mark_seen: Callable[..., None]) -> None:
        self.database = database
        self.mark_seen = mark_seen

    def sync(self, subscription_id: int) -> dict[str, Any]:
        subscription = self.database.get_update_subscription(subscription_id, include_sources=True)
        if not subscription:
            return {"checked": 0, "completed_count": 0, "message": "订阅不存在"}
        candidates = self._list_all_candidates(subscription_id)
        checked = 0
        completed: list[int] = []
        failed: list[int] = []
        review: list[int] = []
        pending: list[int] = []
        completed_keys: set[EpisodeKey] = set()
        failed_keys: set[EpisodeKey] = set()
        review_keys: set[EpisodeKey] = set()
        pending_keys: set[EpisodeKey] = set()
        terminal_candidate_updates: list[tuple[int, str, str, dict[str, Any]]] = []
        status_items: list[dict[str, Any]] = []
        subscription_season = _to_season(subscription.get("season"))

        for candidate in candidates:
            decision = str(candidate.get("decision") or "")
            if decision not in {"submitted", "imported", "review"} or not candidate.get("job_id"):
                continue
            checked += 1
            job = self.database.get_job(int(candidate["job_id"]))
            if not job:
                continue
            status = str(job.get("status") or "")
            raw = dict(candidate.get("raw_data")) if isinstance(candidate.get("raw_data"), dict) else {}
            completion = (job.get("raw_data") or {}).get("completion") if isinstance(job.get("raw_data"), dict) else {}
            raw["job_completion"] = {
                "job_id": job.get("id"),
                "status": status,
                "completion": completion if isinstance(completion, dict) else {},
                "synced_at": utc_now(),
            }
            candidate_for_sync = dict(candidate)
            episode_key = self._episode_key(candidate_for_sync, default_season=subscription_season)
            if episode_key and _to_season(candidate_for_sync.get("season")) is None and episode_key[0] is not None:
                candidate_for_sync["season"] = episode_key[0]
            candidate_for_sync["raw_data"] = raw
            episode = episode_key[1] if episode_key else None
            candidate_id = int(candidate["id"])
            if status in {"done", "success"}:
                raw_candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
                self.mark_seen(
                    subscription,
                    candidate_for_sync,
                    raw_candidate,
                    candidate_id,
                    job,
                    auto=True,
                    completion_state="done",
                )
                terminal_candidate_updates.append(
                    (candidate_id, "completed", "关联入库任务已完成标准整理确认", raw)
                )
                if episode:
                    completed.append(episode)
                if episode_key:
                    completed_keys.add(episode_key)
            elif status in {"failed", "cancelled", "unsupported"}:
                terminal_candidate_updates.append(
                    (candidate_id, "failed", f"关联入库任务状态：{status}", raw)
                )
                if episode:
                    failed.append(episode)
                if episode_key:
                    failed_keys.add(episode_key)
                status_items.append(self.status_from_job(candidate_for_sync, job, status=status, decision="failed"))
            elif status == JOB_REVIEW and self._provider_completed_review(completion):
                # 六盘旧流程可能已完成文件下载，仅媒体库刷新失败。此时 Job 仍需
                # 保持 review 供管理员处理，但追更侧必须把该集视为已入库，否则
                # missing 补查会创建新的 Provider 任务并重复下载。
                raw_candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
                self.mark_seen(
                    subscription,
                    candidate_for_sync,
                    raw_candidate,
                    candidate_id,
                    job,
                    auto=True,
                    completion_state="provider_done_review",
                )
                terminal_candidate_updates.append(
                    (candidate_id, "completed", "网盘文件已完成，仅媒体库刷新等待人工处理", raw)
                )
                if episode:
                    completed.append(episode)
                if episode_key:
                    completed_keys.add(episode_key)
            elif status == JOB_REVIEW:
                terminal_candidate_updates.append(
                    (candidate_id, "review", "关联入库任务等待人工确认", raw)
                )
                if episode:
                    review.append(episode)
                if episode_key:
                    review_keys.add(episode_key)
                status_items.append(self.status_from_job(candidate_for_sync, job, status=status, decision="review"))
            else:
                self.database.update_update_candidate(candidate_id, decision="submitted", raw_data=raw)
                if episode:
                    pending.append(episode)
                if episode_key:
                    pending_keys.add(episode_key)
                status_items.append(self.status_from_job(candidate_for_sync, job, status=status, decision="submitted"))

        if checked:
            raw_data = dict(subscription.get("raw_data")) if isinstance(subscription.get("raw_data"), dict) else {}
            failed_or_review_keys = failed_keys | review_keys
            remaining = [
                item
                for item in status_items
                if self._episode_key(item, default_season=subscription_season) not in completed_keys
            ]
            self._apply_pending_status(raw_data, remaining)
            pending_import = raw_data.get("pending_import") if isinstance(raw_data.get("pending_import"), dict) else {}
            if self._episode_key(pending_import, default_season=subscription_season) in completed_keys:
                raw_data.pop("pending_import", None)

            previous_last_success = _to_int(subscription.get("last_success_episode")) or 0
            progress_completed_episodes = {
                episode
                for season, episode in completed_keys
                if subscription_season is None or season == subscription_season
            }
            last_success_episode = max([previous_last_success, *progress_completed_episodes])
            next_episode = _to_int(subscription.get("next_episode"))
            if progress_completed_episodes and last_success_episode and (not next_episode or next_episode <= last_success_episode):
                next_episode = last_success_episode + 1

            missing_episode_keys = self._missing_episode_keys(
                subscription,
                raw_data,
                default_season=subscription_season,
            )
            previous_missing_episode_keys = set(missing_episode_keys)
            if progress_completed_episodes and previous_last_success and last_success_episode > previous_last_success:
                missing_episode_keys.update(
                    (subscription_season, episode)
                    for episode in range(previous_last_success + 1, last_success_episode + 1)
                )
            # 较新集可能先完成，较旧集当时仍在途。该较旧集后续若失败、取消或
            # 进入人工审核，必须重新回到 missing；否则 next_episode 已经越过它，
            # 后续追更将再也不会自动补齐。若同一集另有任务仍在途，则继续等待，
            # 避免并发候选造成重复入库。
            missing_episode_keys.update(failed_or_review_keys)
            resolved_episode_keys = (
                self._known_episode_keys(subscription_id, default_season=subscription_season)
                | completed_keys
                | pending_keys
            )
            missing_episode_keys.difference_update(resolved_episode_keys)
            # 旧数据在订阅未记录 season 时只有集号，会被解析为 (None, episode)。
            # 这种未知季的兼容项可被任何已确认季的同集号清除，但显式的
            # (S1, E4) 绝不会清除 (S2, E4)。
            resolved_known_season_episodes = {
                episode for season, episode in resolved_episode_keys if season is not None
            }
            missing_episode_keys = {
                key
                for key in missing_episode_keys
                if not (key[0] is None and key[1] in resolved_known_season_episodes)
            }
            requeued_episode_keys = {
                key
                for key in failed_or_review_keys
                if key in missing_episode_keys
                and not self._was_missing(previous_missing_episode_keys, key)
            }
            missing_episodes = self._current_season_episode_numbers(
                missing_episode_keys,
                current_season=subscription_season,
            )
            raw_data["missing_episode_keys"] = self._serialize_episode_keys(missing_episode_keys)

            subscription_updates: dict[str, Any] = {
                "missing_episodes": missing_episodes,
                "raw_data": raw_data,
            }
            if progress_completed_episodes:
                subscription_updates.update(
                    {
                        "last_success_at": utc_now(),
                        "last_success_episode": last_success_episode,
                        "next_episode": next_episode,
                    }
                )
            self.database.update_update_subscription(subscription_id, subscription_updates)
            subscription.update(subscription_updates)

            # 订阅进度/缺集先成功落库，再写入 completed/failed/review 终态。
            # 否则订阅更新失败后 candidate 会因已进入终态而被下轮跳过，
            # 造成进度或缺集永久遗漏。completed 分支的 mark_seen 也位于订阅更新之前，
            # 三步任意一步失败都可依赖幂等操作在下轮恢复。
            for candidate_id, decision, reason, candidate_raw_data in terminal_candidate_updates:
                self.database.update_update_candidate(
                    candidate_id,
                    decision=decision,
                    reason=reason,
                    raw_data=candidate_raw_data,
                )

            if requeued_episode_keys:
                self.database.add_update_event(
                    subscription_id,
                    None,
                    "warn",
                    "追更入库未完成的集数已重新加入缺集补查",
                    {
                        "missing_episodes": self._current_season_episode_numbers(
                            requeued_episode_keys,
                            current_season=subscription_season,
                        ),
                        "missing_episode_keys": self._serialize_episode_keys(requeued_episode_keys),
                    },
                )

        if completed:
            self.database.add_update_event(
                subscription_id,
                None,
                "info",
                "已同步追更入库完成状态",
                {"completed_episodes": completed},
            )
        return {
            "checked": checked,
            "completed_count": len(completed),
            "completed_episodes": completed,
            "pending_episodes": pending,
            "failed_episodes": failed,
            "review_episodes": review,
        }

    def _list_all_candidates(self, subscription_id: int) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        offset = 0
        page_size = 500
        while True:
            try:
                page = self.database.list_update_candidates(
                    subscription_id=subscription_id,
                    limit=page_size,
                    offset=offset,
                )
            except TypeError:
                # 兼容只实现旧签名的轻量数据库适配器。正式 Database 支持 offset。
                return list(
                    self.database.list_update_candidates(
                        subscription_id=subscription_id,
                        limit=page_size,
                    )
                    or []
                )
            page = list(page or [])
            new_items: list[dict[str, Any]] = []
            for item in page:
                try:
                    candidate_id = int(item.get("id") or 0)
                except (AttributeError, TypeError, ValueError):
                    candidate_id = 0
                if candidate_id and candidate_id in seen_ids:
                    continue
                if candidate_id:
                    seen_ids.add(candidate_id)
                if isinstance(item, dict):
                    new_items.append(item)
            candidates.extend(new_items)
            if len(page) < page_size or not new_items:
                break
            offset += len(page)
        return candidates

    def _known_episode_keys(self, subscription_id: int, *, default_season: int | None) -> set[EpisodeKey]:
        list_seen = getattr(self.database, "list_update_seen_episodes", None)
        if not callable(list_seen):
            return set()
        try:
            rows = list_seen(subscription_id) or set()
        except Exception:  # noqa: BLE001
            return set()
        result: set[EpisodeKey] = set()
        for item in rows:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            episode = _to_int(item[1])
            if not episode:
                continue
            season = _to_season(item[0])
            if season is None:
                season = default_season
            result.add((season, episode))
        return result

    @staticmethod
    def _episode_key(item: dict[str, Any], *, default_season: int | None) -> EpisodeKey | None:
        episode = _to_int(item.get("episode"))
        if not episode:
            return None
        season = _to_season(item.get("season"))
        if season is None:
            season = default_season
        return (season, episode)

    @classmethod
    def _missing_episode_keys(
        cls,
        subscription: dict[str, Any],
        raw_data: dict[str, Any],
        *,
        default_season: int | None,
    ) -> set[EpisodeKey]:
        result: set[EpisodeKey] = set()
        raw_items = raw_data.get("missing_episode_keys")
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, dict):
                    key = cls._episode_key(item, default_season=default_season)
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    key = cls._episode_key(
                        {"season": item[0], "episode": item[1]},
                        default_season=default_season,
                    )
                else:
                    key = None
                if key:
                    result.add(key)
        for value in subscription.get("missing_episodes") or []:
            episode = _to_int(value)
            if episode:
                result.add((default_season, episode))
        return result

    @staticmethod
    def _serialize_episode_keys(keys: set[EpisodeKey]) -> list[dict[str, int | None]]:
        return [
            {"season": season, "episode": episode}
            for season, episode in sorted(keys, key=lambda key: (key[0] is not None, key[0] or 0, key[1]))
        ]

    @staticmethod
    def _current_season_episode_numbers(
        keys: set[EpisodeKey],
        *,
        current_season: int | None,
    ) -> list[int]:
        return sorted(
            {
                episode
                for season, episode in keys
                if current_season is None or season == current_season
            }
        )

    @staticmethod
    def _was_missing(previous_keys: set[EpisodeKey], key: EpisodeKey) -> bool:
        return key in previous_keys or (key[0] is not None and (None, key[1]) in previous_keys)

    @staticmethod
    def _provider_completed_review(completion: Any) -> bool:
        return bool(
            isinstance(completion, dict)
            and completion.get("provider_completed")
            and str(completion.get("retry_action") or "").strip().lower() == "media_refresh_only"
        )

    @staticmethod
    def _apply_pending_status(raw_data: dict[str, Any], items: list[dict[str, Any]]) -> None:
        if items:
            raw_data["pending_import_status_items"] = items[:5]
            raw_data["pending_import_status"] = items[0]
        else:
            raw_data.pop("pending_import_status", None)
            raw_data.pop("pending_import_status_items", None)

    @staticmethod
    def status_from_job(candidate: dict[str, Any], job: dict[str, Any], *, status: str, decision: str) -> dict[str, Any]:
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), dict) else {}
        completion = raw_data.get("completion") if isinstance(raw_data.get("completion"), dict) else {}
        stage = str(completion.get("stage") or status or "").strip()
        organizer_task_id = _to_int(completion.get("organizer_task_id"))
        message = str(completion.get("message") or job.get("error_message") or "").strip()
        if not message:
            if status == JOB_WAITING_OPENLIST or stage == COMPLETION_STAGE_WAITING_OPENLIST:
                message = "已提交，正在等待 OpenList 可见"
            elif status in {JOB_WAITING_ORGANIZER, JOB_ORGANIZING, JOB_CONFIRMING} or stage in {COMPLETION_STAGE_WAITING_ORGANIZER, JOB_ORGANIZING, JOB_CONFIRMING}:
                message = "已提交，正在等待标准整理完成"
            elif status == JOB_REVIEW:
                message = "整理未完成，需要检查 Organizer 任务"
            elif status in {JOB_FAILED, JOB_CANCELLED, JOB_UNSUPPORTED}:
                message = "入库任务未完成，请检查任务异常"
            else:
                message = "已提交，等待完整入库确认"
        reason = message
        if stage == COMPLETION_STAGE_WAITING_OPENLIST:
            reason = "OpenList 暂未看到新文件，系统会自动重试"
        elif status == JOB_REVIEW:
            reason = message or "Organizer 等待确认"
        elif status in {JOB_FAILED, JOB_CANCELLED, JOB_UNSUPPORTED}:
            reason = job.get("error_message") or message
        return {
            "episode": _to_int(candidate.get("episode")),
            "season": _to_season(candidate.get("season")),
            "candidate_id": candidate.get("id"),
            "job_id": job.get("id"),
            "organizer_task_id": organizer_task_id,
            "job_status": status,
            "status_label": JOB_STATUS_LABELS.get(status, status),
            "stage": stage,
            "stage_label": JOB_STATUS_LABELS.get(stage, stage),
            "decision": decision,
            "message": message,
            "reason": reason,
            "needs_attention": status in {JOB_FAILED, JOB_CANCELLED, JOB_UNSUPPORTED, JOB_REVIEW},
            "checked_at": utc_now(),
        }


def _to_int(value: Any) -> int | None:
    return positive_int(value)


def _to_season(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
