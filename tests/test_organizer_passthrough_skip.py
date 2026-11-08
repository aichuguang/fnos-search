from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from fnos_media_import.organizer.identity_pipeline import IdentityReviewRequired
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.services.organizer_admin_service import (
    OrganizerAdminCommandDependencies,
    OrganizerAdminCommandService,
)


def organizer_with_io_spies() -> OrganizerService:
    service = object.__new__(OrganizerService)
    service.db = Mock()
    service.openlist = Mock()
    service.start_apply_task = Mock()
    return service


@pytest.mark.parametrize("task_id", [0, 1, 12, -1])
def test_skip_rejects_without_database_or_openlist_io(task_id: int) -> None:
    service = organizer_with_io_spies()

    result = service.skip_task(task_id)

    assert result["success"] is False
    assert result["code"] == "IDENTITY_REQUIRED"
    assert not result.get("ready_for_apply")
    assert not result.get("passthrough")
    assert service.db.mock_calls == []
    assert service.openlist.mock_calls == []
    service.start_apply_task.assert_not_called()


def test_direct_passthrough_preparation_cannot_bypass_identity_gate() -> None:
    service = organizer_with_io_spies()

    with pytest.raises(IdentityReviewRequired):
        service._prepare_v1_passthrough(
            {"id": 1, "status": "waiting_review", "raw_data": {"passthrough_import": {"enabled": True}}},
            {"paths": {"organizer_root": "/staging/job-1", "destination_final": "/library/movies"}},
        )

    assert service.db.mock_calls == []
    assert service.openlist.mock_calls == []
    service.start_apply_task.assert_not_called()


def test_admin_skip_does_not_dispatch_a_worker_or_start_apply() -> None:
    organizer = organizer_with_io_spies()
    dispatcher = Mock()
    admin = OrganizerAdminCommandService(
        OrganizerAdminCommandDependencies(organizer=organizer, worker_dispatcher=dispatcher)
    )

    result, status = admin.skip(12)

    assert status == 400
    assert result["success"] is False
    assert result["code"] == "IDENTITY_REQUIRED"
    assert dispatcher.mock_calls == []
    assert organizer.db.mock_calls == []
    assert organizer.openlist.mock_calls == []
    organizer.start_apply_task.assert_not_called()


def test_ui_removes_skip_action_but_keeps_review_and_retry() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "static" / "admin-organizer.js").read_text(encoding="utf-8")
    template = (root / "templates" / "admin.html").read_text(encoding="utf-8")

    assert 'data-organizer-action="skip"' not in source
    assert 'action: "skip"' not in source
    assert "保留原目录和文件名" not in source
    assert 'data-organizer-action="apply"' in source
    assert 'data-organizer-action="retry"' in source
    assert 'action: "rename", label: "修改计划"' in source
    assert "确认并整理" in source
    assert 'if (!["rename", "apply", "retry", "rebuild", "delete"].includes(action))' in source
    assert 'class="data-table table-organizer"' in template
