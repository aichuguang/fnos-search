from __future__ import annotations

from typing import Any, Callable

from ..constants import EVENT_ERROR, RCLONE_RUN_SUCCESS


class RcloneWaitingJobRecoveryService:
    """Repairs waiting jobs from completed historical rclone file events."""

    def __init__(
        self,
        *,
        database: Any,
        state_lock: Any,
        is_running: Callable[[], bool],
        file_identity: Callable[[dict[str, Any]], str],
        is_pollution_file: Callable[[str], bool],
        finalize_run: Callable[[int, int], None],
    ) -> None:
        self.database = database
        self.state_lock = state_lock
        self.is_running = is_running
        self.file_identity = file_identity
        self.is_pollution_file = is_pollution_file
        self.finalize_run = finalize_run

    def repair(self, limit: int = 50) -> dict[str, Any]:
        if not self.database:
            return {"success": False, "message": "数据库未初始化", "run_ids": []}
        with self.state_lock:
            if self.is_running():
                return {"success": False, "message": "rclone 正在运行，跳过历史兜底修复", "run_ids": []}
        relinked = self._relink_unmatched_events(max(limit * 10, 200))
        jobs = self._waiting_jobs(limit)
        if not jobs:
            return {"success": True, "message": "没有等待搬运或搬运中的任务需要修复", "run_ids": [], "relinked_event_ids": relinked}
        run_ids = self._completed_run_ids(jobs)
        if not run_ids:
            return {"success": True, "message": "等待任务没有可用于兜底的历史完成记录", "run_ids": [], "relinked_event_ids": relinked}
        recent_runs = self._runs_by_ids(run_ids)
        repaired: list[int] = []
        errors: list[dict[str, Any]] = []
        for run_id in run_ids[:limit]:
            exit_code = _historical_exit_code(recent_runs.get(run_id, {}))
            try:
                self.finalize_run(run_id, exit_code)
                repaired.append(run_id)
            except Exception as exc:  # noqa: BLE001
                errors.append({"run_id": run_id, "message": str(exc)})
                self.database.add_rclone_event(
                    run_id, EVENT_ERROR, f"历史 rclone run 兜底修复异常：{exc}"
                )
        return {
            "success": not errors,
            "message": f"已尝试用历史 rclone 完成记录修复 {len(repaired)} 个 run",
            "run_ids": repaired,
            "relinked_event_ids": relinked,
            "errors": errors,
        }

    def _relink_unmatched_events(self, limit: int) -> list[int]:
        relink_limit = max(1, int(limit or 200))
        relinked: list[int] = []
        page_size = 500
        before_id: int | None = None
        loader = getattr(self.database, "list_unmatched_rclone_file_events", None)
        if not callable(loader):
            events = [
                event
                for event in self._all_file_events()
                if not _positive_int(event.get("job_id"))
            ]
            pages = [events]
        else:
            pages = None

        while len(relinked) < relink_limit:
            if pages is not None:
                if not pages:
                    break
                events = pages.pop(0)
            else:
                events = loader(limit=page_size, before_id=before_id)
            if not events:
                break

            for event in events:
                if _positive_int(event.get("job_id")):
                    continue
                status = str(event.get("status") or "").strip().lower()
                if status not in {"transferring", "processing", "done", "success", "skipped_existing"}:
                    continue
                matched = self.database.find_job_for_rclone_callback(
                    category=str(event.get("category") or ""),
                    filename=str(event.get("filename") or ""),
                    source_path=str(event.get("source_path") or ""),
                    target_path=str(event.get("target_path") or ""),
                )
                event_id = _positive_int(event.get("id"))
                job_id = _positive_int((matched or {}).get("id"))
                if not event_id or not job_id:
                    continue
                if self.database.attach_rclone_file_event_to_job(event_id, job_id):
                    relinked.append(event_id)
                    if len(relinked) >= relink_limit:
                        break

            if pages is not None or len(events) < page_size:
                break
            page_ids = [_positive_int(event.get("id")) for event in events]
            next_before_id = min((event_id for event_id in page_ids if event_id), default=0)
            if not next_before_id or (before_id is not None and next_before_id >= before_id):
                break
            before_id = next_before_id
        return relinked

    def _waiting_jobs(self, limit: int) -> list[dict[str, Any]]:
        jobs: dict[int, dict[str, Any]] = {}
        page_size = max(50, min(500, int(limit or 50)))
        for status in ("waiting_transfer", "waiting_openlist", "transferring"):
            for source_type in ("quark", "uc", "magnet", "torrent"):
                offset = 0
                while True:
                    page = self.database.list_jobs(
                        limit=page_size,
                        offset=offset,
                        status=status,
                        source_type=source_type,
                    )
                    for job in page:
                        job_id = _positive_int(job.get("id"))
                        if job_id:
                            jobs[job_id] = job
                    if len(page) < page_size:
                        break
                    offset += len(page)
        return list(jobs.values())

    def _runs_by_ids(self, run_ids: list[int]) -> dict[int, dict[str, Any]]:
        wanted = {int(run_id) for run_id in run_ids if _positive_int(run_id)}
        found: dict[int, dict[str, Any]] = {}
        offset = 0
        page_size = 500
        while wanted:
            page = self.database.list_rclone_runs(limit=page_size, offset=offset)
            if not page:
                break
            for item in page:
                run_id = _positive_int(item.get("id"))
                if run_id in wanted:
                    found[run_id] = item
                    wanted.discard(run_id)
            if len(page) < page_size:
                break
            offset += len(page)
        return found

    def _completed_run_ids(self, jobs: list[dict[str, Any]]) -> list[int]:
        run_ids: list[int] = []
        for job in jobs:
            job_id = _positive_int(job.get("id"))
            if not job_id:
                continue
            events = self._all_file_events(job_id=job_id)
            completed = any(
                str(event.get("status") or "").strip().lower()
                in {"done", "success", "skipped_existing"}
                and not self.is_pollution_file(self.file_identity(event))
                for event in events
            )
            if not completed:
                continue
            for event in events:
                run_id = _positive_int(event.get("run_id"))
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)
        return run_ids

    def _all_file_events(self, **filters: Any) -> list[dict[str, Any]]:
        requested_limit = filters.pop("limit", None)
        if requested_limit is not None:
            return self.database.list_rclone_file_events(
                limit=max(1, min(int(requested_limit or 1), 1000)),
                offset=0,
                **filters,
            )
        loader = getattr(self.database, "list_all_rclone_file_events", None)
        if callable(loader):
            events = loader(**filters)
        else:
            events = self.database.list_rclone_file_events(limit=1000, **filters)
        return events


def _historical_exit_code(run: dict[str, Any]) -> int:
    if run.get("exit_code") is None:
        return 0 if run.get("status") == RCLONE_RUN_SUCCESS else 1
    try:
        return int(run.get("exit_code"))
    except (TypeError, ValueError):
        return 1


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
