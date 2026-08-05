from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CandidateBatchImportResult:
    items: list[dict[str, Any]]
    submitted_count: int
    completed_count: int
    failed_count: int


class UpdateCandidateBatchImportService:
    """Submits one highest-scoring candidate per target episode."""

    def __init__(
        self,
        *,
        import_candidate: Callable[..., dict[str, Any]],
        record_stage: Callable[..., None],
        mark_failed: Callable[[int, str], None] | None = None,
    ) -> None:
        self.import_candidate = import_candidate
        self.record_stage = record_stage
        self.mark_failed = mark_failed

    def import_best(
        self,
        *,
        subscription_id: int,
        run_id: int,
        best_by_episode: dict[tuple[int | None, int], tuple[int, dict[str, Any], int]],
    ) -> CandidateBatchImportResult:
        items: list[dict[str, Any]] = []
        submitted = 0
        completed = 0
        failed = 0
        for (season, episode), (_score, candidate, candidate_id) in sorted(
            best_by_episode.items(),
            key=lambda item: ((item[0][0] or 0), item[0][1]),
        ):
            self.record_stage(
                run_id,
                "import",
                "提交新增单集入库",
                {"candidate_id": candidate_id, "episode": episode},
            )
            try:
                result = self.import_candidate(
                    candidate_id,
                    reason=f"update_subscription:{subscription_id}:run:{run_id}",
                    auto=True,
                    candidate_override=candidate,
                )
            except Exception as exc:  # noqa: BLE001
                message = str(exc) or exc.__class__.__name__
                if self.mark_failed:
                    try:
                        self.mark_failed(candidate_id, message)
                    except Exception:  # noqa: BLE001
                        # 失败标记写入异常不能再次中断后续集处理，运行日志仍会保留原始错误。
                        pass
                self.record_stage(
                    run_id,
                    "import_failed",
                    "单集入库失败，已跳过并继续处理后续集",
                    {"candidate_id": candidate_id, "season": season, "episode": episode, "error": message},
                )
                result = {
                    "success": False,
                    "submitted": False,
                    "completed": False,
                    "candidate_id": candidate_id,
                    "season": season,
                    "episode": episode,
                    "message": message,
                    "error": message,
                    "retryable": True,
                }
            items.append(result)
            if result.get("success"):
                submitted += 1
                if result.get("completed"):
                    completed += 1
            else:
                failed += 1
        return CandidateBatchImportResult(items, submitted, completed, failed)
