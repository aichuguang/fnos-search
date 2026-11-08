from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fnos_media_import.domain import EXECUTION_PLAN_VERSION
from fnos_media_import.services.import_job_service import ImportJobRetryService


class _JobQueries:
    def __init__(self) -> None:
        self.job = {
            "id": 42,
            "status": "review",
            "plan_version": EXECUTION_PLAN_VERSION,
            "idempotency_key": "import:job-42",
        }

    def get(self, job_id: int) -> dict[str, Any] | None:
        return dict(self.job) if job_id == 42 else None


class _JobCommands:
    def __init__(self) -> None:
        self.events: list[tuple[int, str, str]] = []

    def add_event(self, job_id: int, level: str, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.events.append((job_id, level, message))


class _Executions:
    def plan(self, _job_id: int) -> dict[str, Any]:
        return {"job_id": 42}

    def get(self, _job_id: int) -> SimpleNamespace:
        return SimpleNamespace(phase="manual_review")

    def recover_manual_review(self, _job_id: int) -> None:
        return None


class _Organizer:
    def __init__(self) -> None:
        self.task = {
            "id": 160,
            "job_id": 42,
            "status": "waiting_review",
            "revision": 2,
            "error_message": "扫描或生成标准化计划失败：Invalid canonical identity directory",
            "evidence": {
                "manual_approval": {"by": "admin"},
                "execution_approval": {"by": "admin"},
                "agent_confirmation": {"plan": "old"},
                "agent_review_required": True,
                "keep_me": "value",
            },
            "raw_data": {"agent_review_required": True, "keep": "value"},
            "operations": [],
        }
        self.updates: list[tuple[int, dict[str, Any]]] = []

    def list_organizer_tasks_by_job(self, job_id: int, limit: int = 20) -> list[dict[str, Any]]:
        assert job_id == 42 and limit == 1
        return [dict(self.task)]

    def get_organizer_task(self, task_id: int, include_children: bool = True) -> dict[str, Any]:
        assert task_id == 160 and include_children is False
        return dict(self.task)

    def update_organizer_task(self, task_id: int, **updates: Any) -> bool:
        self.updates.append((task_id, updates))
        return True


def test_retry_job_resets_and_requeues_linked_organizer_task() -> None:
    organizer = _Organizer()
    transferred: list[int] = []
    enqueued: list[int] = []
    service = ImportJobRetryService(
        database=SimpleNamespace(
            job_queries=_JobQueries(),
            job_commands=_JobCommands(),
            organizer=organizer,
        ),
        config={},
        validate_plan=lambda _plan, _job, **_kwargs: None,
        execution_repository=_Executions(),
        enqueue_transfer=transferred.append,
        enqueue_organizer=enqueued.append,
    )

    result = service.retry(42)

    assert result["success"] is True
    assert transferred == [42]
    assert enqueued == [160]
    assert result["organizer_retry"] == {"task_id": 160, "queued": None}
    assert organizer.updates == [
        (
            160,
            {
                "status": "pending",
                "error_message": "",
                "evidence": {"keep_me": "value"},
                "raw_data": {"agent_review_required": False, "keep": "value"},
                "expected_revision": 2,
                "expected_statuses": {"waiting_review"},
                "bump_revision": True,
                "clear_scan_lease": True,
            },
        )
    ]
