from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from typing import Any

from fnos_media_import.database import Database
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.services.organizer_admin_service import OrganizerAdminCommandDependencies, OrganizerAdminCommandService


class _FakeOrganizer:
    def __init__(self, task: dict[str, Any]) -> None:
        self._task = task
        self.calls: list[tuple[int, dict[str, Any]]] = []
        self.batch_calls: list[tuple[int, dict[str, Any]]] = []
        self.batch_failure: dict[str, Any] | None = None

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        if int(task_id) != int(self._task.get("id") or 0):
            return None
        return dict(self._task)

    def update_mapping(self, mapping_id: int, payload: dict[str, Any], *, task_id: int) -> dict[str, Any]:
        self.calls.append((mapping_id, dict(payload)))
        for mapping in self._task.get("mappings") or []:
            if int(mapping.get("id") or 0) == int(mapping_id):
                mapping.update(dict(payload))
                if "title" in payload:
                    mapping["target_path"] = (
                        f"/移动云/动漫/{payload['title']}/Season 01/"
                        f"{payload['title']} - S01E{int(mapping.get('episode') or 1):02d}.mp4"
                    )
        return {"success": True}

    def batch_update_mappings(self, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.batch_calls.append((int(task_id), dict(payload)))
        if self.batch_failure is not None:
            return dict(self.batch_failure)
        updated = [dict(mapping) for mapping in self._task.get("mappings") or []]
        for mapping in updated:
            mapping.update(dict(payload))
            title = str(mapping.get("title") or "")
            season = int(mapping.get("season") or 1)
            episode = int(mapping.get("episode") or 1)
            mapping["target_path"] = (
                f"/移动云/动漫/{title}/Season {season:02d}/"
                f"{title} - S{season:02d}E{episode:02d}.mp4"
            )
        self._task["mappings"] = updated
        return {"success": True, "changed": len(updated), "task": self.get_task(task_id)}

    def fail_update(self) -> None:
        self._fail = True

    def update_mapping_failing(self, mapping_id: int, payload: dict[str, Any], *, task_id: int) -> dict[str, Any]:
        return {"success": False, "message": "任务已开始执行，拒绝修改映射"}


def _service(task: dict[str, Any]) -> tuple[OrganizerAdminCommandService, _FakeOrganizer]:
    fake = _FakeOrganizer(task)
    deps = OrganizerAdminCommandDependencies(organizer=fake)
    return OrganizerAdminCommandService(deps), fake


def _task() -> dict[str, Any]:
    return {
        "id": 15,
        "status": "waiting_review",
        "category": "anime",
        "mappings": [
            {"id": 1, "title": "百变猪猪侠 全", "season": 1, "episode": 1, "target_path": "/移动云/动漫/百变猪猪侠 全/Season 01/百变猪猪侠 全 - S01E01.mp4"},
            {"id": 2, "title": "百变猪猪侠 全", "season": 1, "episode": 2, "target_path": "/移动云/动漫/百变猪猪侠 全/Season 01/百变猪猪侠 全 - S01E02.mp4"},
        ],
    }


class OrganizerBatchRenameTests(unittest.TestCase):
    def test_batch_rename_title_updates_all_mappings(self) -> None:
        service, fake = _service(_task())
        result, status = service.batch_update_mappings(15, {"title": "百变猪猪侠"})
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        self.assertEqual(result["changed"], 2)
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.batch_calls, [(15, {"title": "百变猪猪侠"})])

    def test_batch_rename_season_updates_all_mappings(self) -> None:
        service, fake = _service(_task())
        result, status = service.batch_update_mappings(15, {"season": 2})
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.batch_calls, [(15, {"season": 2})])

    def test_batch_failure_does_not_fall_back_to_partial_updates(self) -> None:
        original = _task()
        service, fake = _service(original)
        fake.batch_failure = {"success": False, "message": "原子写入失败且已回滚"}
        before = [dict(item) for item in original["mappings"]]

        result, status = service.batch_update_mappings(15, {"title": "不会写入"})

        self.assertEqual(status, 400)
        self.assertFalse(result["success"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(original["mappings"], before)

    def test_batch_rename_without_fields_rejected(self) -> None:
        service, _fake = _service(_task())
        result, status = service.batch_update_mappings(15, {})
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])

    def test_batch_rename_invalid_season_rejected(self) -> None:
        service, _fake = _service(_task())
        result, status = service.batch_update_mappings(15, {"season": "abc"})
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])

    def test_batch_rename_empty_mappings_rejected(self) -> None:
        service, _fake = _service({"id": 15, "status": "waiting_review", "category": "anime", "mappings": []})
        result, status = service.batch_update_mappings(15, {"title": "某片"})
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])

    def test_batch_rename_missing_task_404(self) -> None:
        service, _fake = _service(_task())
        result, status = service.batch_update_mappings(9999, {"title": "某片"})
        self.assertEqual(status, 404)


class OrganizerAtomicBatchRenameDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.db_path = root / f"organizer-batch-rename-{uuid.uuid4().hex}.db"
        self.database = Database(self.db_path)
        self.database.init_schema()

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    def _create_task(self, *, duplicate_episode: bool = False, mapping_status: str = "ready") -> int:
        task_id = self.database.create_organizer_task(
            category="anime",
            openlist_root_path="/暂存/动漫/job-15",
            status="waiting_review",
            evidence={"source": "test"},
        )
        mappings = []
        for index, name in enumerate(("a.mp4", "b.mp4"), start=1):
            episode = 1 if duplicate_episode else index
            mappings.append(
                {
                    "source_path": f"/暂存/动漫/job-15/{name}",
                    "source_name": name,
                    "target_path": f"/移动云/动漫/旧片{index}/Season 01/旧片{index} - S01E{episode:02d}.mp4",
                    "target_name": f"旧片{index} - S01E{episode:02d}.mp4",
                    "media_type": "tv",
                    "title": f"旧片{index}",
                    "season": 1,
                    "episode": episode,
                    "status": mapping_status,
                    "reason": [],
                    "raw_data": {},
                }
            )
        self.assertTrue(
            self.database.replace_organizer_plan(
                task_id,
                files=[
                    {"path": "/暂存/动漫/job-15/a.mp4", "name": "a.mp4"},
                    {"path": "/暂存/动漫/job-15/b.mp4", "name": "b.mp4"},
                ],
                mappings=mappings,
                operations=[
                    {
                        "type": "move_file",
                        "source_path": mappings[0]["source_path"],
                        "target_path": mappings[0]["target_path"],
                        "status": "pending",
                        "reason": ["old-plan"],
                    }
                ],
            )
        )
        return task_id

    def _organizer(self) -> OrganizerService:
        service = OrganizerService.__new__(OrganizerService)
        service.db = self.database
        service.categories = {"anime": {"label": "动漫", "openlist_root_path": "/移动云/动漫"}}
        return service

    def test_service_updates_mappings_operations_and_evidence_atomically(self) -> None:
        task_id = self._create_task()
        before = self.database.get_organizer_task(task_id)

        result = self._organizer().batch_update_mappings(task_id, {"title": "新片", "season": 2})

        self.assertTrue(result["success"], result)
        after = self.database.get_organizer_task(task_id)
        self.assertEqual(int(after["revision"]), int(before["revision"]) + 1)
        self.assertEqual({item["title"] for item in after["mappings"]}, {"新片"})
        self.assertEqual({item["season"] for item in after["mappings"]}, {2})
        self.assertTrue(all("/新片/Season 02/" in item["target_path"] for item in after["mappings"]))
        move_targets = {item["target_path"] for item in after["operations"] if item["type"] == "move_file"}
        self.assertEqual(move_targets, {item["target_path"] for item in after["mappings"]})
        self.assertIn("episode_completeness", after["evidence"])

    def test_repository_exception_rolls_back_all_mapping_and_plan_writes(self) -> None:
        task_id = self._create_task()
        before = self.database.get_organizer_task(task_id)
        updates = [
            {"id": int(item["id"]), "updates": {"title": "不应保留"}}
            for item in before["mappings"]
        ]

        with self.assertRaises(TypeError):
            self.database.update_organizer_mappings_and_plan(
                task_id,
                mapping_updates=updates,
                operations=[
                    {
                        "type": "move_file",
                        "source_path": before["mappings"][0]["source_path"],
                        "target_path": "/移动云/动漫/坏计划.mp4",
                        "raw_data": {"not_json": {1, 2}},
                    }
                ],
                evidence={"changed": True},
                expected_status=str(before["status"]),
                expected_revision=int(before["revision"]),
            )

        after = self.database.get_organizer_task(task_id)
        self.assertEqual(
            [(item["id"], item["title"], item["target_path"]) for item in after["mappings"]],
            [(item["id"], item["title"], item["target_path"]) for item in before["mappings"]],
        )
        self.assertEqual(
            [(item["type"], item["source_path"], item["target_path"]) for item in after["operations"]],
            [(item["type"], item["source_path"], item["target_path"]) for item in before["operations"]],
        )
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertEqual(after["revision"], before["revision"])

    def test_duplicate_target_preflight_leaves_database_unchanged(self) -> None:
        task_id = self._create_task(duplicate_episode=True)
        before = self.database.get_organizer_task(task_id)

        result = self._organizer().batch_update_mappings(task_id, {"title": "同一片名"})

        self.assertFalse(result["success"])
        self.assertTrue(result.get("conflict"))
        after = self.database.get_organizer_task(task_id)
        self.assertEqual(
            [(item["title"], item["target_path"]) for item in after["mappings"]],
            [(item["title"], item["target_path"]) for item in before["mappings"]],
        )
        self.assertEqual(after["revision"], before["revision"])

    def test_stale_single_mapping_edit_cannot_overwrite_a_concurrent_batch(self) -> None:
        task_id = self._create_task()
        before = self.database.get_organizer_task(task_id)
        mapping_id = int(before["mappings"][0]["id"])
        service = self._organizer()
        original_updater = self.database.update_organizer_mappings_and_plan
        injected = False

        def racing_updater(*args: Any, **kwargs: Any) -> bool:
            nonlocal injected
            if not injected:
                injected = True
                winner = self._organizer().batch_update_mappings(task_id, {"title": "并发新片"})
                self.assertTrue(winner["success"], winner)
            return original_updater(*args, **kwargs)

        self.database.update_organizer_mappings_and_plan = racing_updater  # type: ignore[method-assign]
        result = service.update_mapping(mapping_id, {"season": 2}, task_id=task_id)

        self.assertFalse(result["success"])
        self.assertTrue(result.get("conflict"))
        after = self.database.get_organizer_task(task_id)
        self.assertEqual({item["title"] for item in after["mappings"]}, {"并发新片"})
        self.assertEqual({item["season"] for item in after["mappings"]}, {1})
        self.assertTrue(all("/并发新片/Season 01/" in item["target_path"] for item in after["mappings"]))
        self.assertEqual(int(after["revision"]), int(before["revision"]) + 1)

    def test_approve_rejects_unresolved_mappings_without_writing(self) -> None:
        task_id = self._create_task(mapping_status="need_edit")
        before = self.database.get_organizer_task(task_id)

        result = self._organizer().approve_task(task_id)

        self.assertFalse(result["success"], result)
        self.assertTrue(result.get("conflict"))
        after = self.database.get_organizer_task(task_id)
        self.assertEqual(after["status"], "waiting_review")
        self.assertEqual({item["status"] for item in after["mappings"]}, {"need_edit"})
        self.assertEqual(int(after["revision"]), int(before["revision"]))

    def test_rejected_approve_does_not_invoke_atomic_writer(self) -> None:
        task_id = self._create_task(mapping_status="need_edit")
        before = self.database.get_organizer_task(task_id)
        service = self._organizer()
        invoked = False

        def unexpected_updater(*_args: Any, **_kwargs: Any) -> bool:
            nonlocal invoked
            invoked = True
            return True

        self.database.update_organizer_mappings_and_plan = unexpected_updater  # type: ignore[method-assign]
        result = service.approve_task(task_id)

        self.assertFalse(result["success"])
        self.assertTrue(result.get("conflict"))
        self.assertFalse(invoked)
        after = self.database.get_organizer_task(task_id)
        self.assertEqual(after["status"], "waiting_review")
        self.assertEqual({item["status"] for item in after["mappings"]}, {"need_edit"})
        self.assertEqual(
            {item["title"] for item in after["mappings"]},
            {item["title"] for item in before["mappings"]},
        )
        self.assertEqual(int(after["revision"]), int(before["revision"]))

    def test_approve_accepts_resolved_mappings_and_rebuilds_plan_atomically(self) -> None:
        task_id = self._create_task(mapping_status="ready")
        before = self.database.get_organizer_task(task_id)

        result = self._organizer().approve_task(task_id)

        self.assertTrue(result["success"], result)
        after = self.database.get_organizer_task(task_id)
        self.assertEqual(after["status"], "manual_confirmed")
        self.assertEqual(int(after["revision"]), int(before["revision"]) + 1)
        move_targets = {
            item["target_path"]
            for item in after["operations"]
            if item["type"] == "move_file"
        }
        self.assertEqual(move_targets, {item["target_path"] for item in after["mappings"]})

    def test_approve_rejects_source_outside_task_root(self) -> None:
        task_id = self._create_task(mapping_status="ready")
        before = self.database.get_organizer_task(task_id)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE organizer_mappings SET source_path=? WHERE id=?",
                ("/其他目录/a.mp4", int(before["mappings"][0]["id"])),
            )

        result = self._organizer().approve_task(task_id)

        self.assertFalse(result["success"], result)
        self.assertTrue(result.get("conflict"))
        self.assertIn("不在任务扫描目录", result["message"])

    def test_approve_rejects_duplicate_targets(self) -> None:
        task_id = self._create_task(mapping_status="ready")
        before = self.database.get_organizer_task(task_id)
        self.database.update_organizer_mapping(
            int(before["mappings"][1]["id"]),
            target_path=before["mappings"][0]["target_path"],
        )

        result = self._organizer().approve_task(task_id)

        self.assertFalse(result["success"], result)
        self.assertTrue(result.get("conflict"))
        self.assertIn("重复目标路径", result["message"])


if __name__ == "__main__":
    unittest.main()
