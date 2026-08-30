"""原生转存入库暂存链路的 Organizer 边界用例。

rclone 链路的暂存目录在分类目录之外（``/挂载/_fnos_staging/分类/job-N``），
原生转存链路则相反：Provider 只能把文件转存进自己的分类目录，所以暂存目录被
冻结成「分类目录 / _入库暂存 / job-N」，扫描根反而嵌套在搬运目标根内部。

这套用例专门覆盖这个倒置形状：
* ``paths.organizer_final`` 必须压过天翼云式复合 ``destination_final``；
* 扫描根只能精确等于嵌套暂存目录，不能是分类根；
* 暂存 → 分类目录的映射能过全部包含性校验，越界目标仍被拒；
* 搬空目录清理被限制在暂存目录内，永远不会去删分类根。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fnos_media_import.constants import JOB_REVIEW
from fnos_media_import.organizer.openlist_client import OpenListError
from fnos_media_import.organizer.parser import (
    V1_DESTINATION_FINAL_FIELD,
    V1_DESTINATION_IS_RESOURCE_FIELD,
)
from fnos_media_import.organizer.service import OrganizerService
from fnos_media_import.planning import NATIVE_STAGING_DIR
from tests._organizer_v1_fixtures import (
    attach_organizer_v1_task,
    bind_task_openlist_resolver,
    organizer_v1_job,
    organizer_v1_plan,
)
from tests._repository_v1_fakes import RepositoryFakeMixin


JOB_ID = 47
# 天翼云的原生定位器是 "{root_folder_id}::{相对路径}" 复合值，OpenList 无法当
# 目标根使用；这正是必须冻结 organizer_final 的原因。
NATIVE_FINAL = "root-folder-id::电影"
FINAL_ROOT = "/天翼/电影"
STAGING_ROOT = f"{FINAL_ROOT}/{NATIVE_STAGING_DIR}/job-{JOB_ID}"
RESOURCE_ROOT = f"{FINAL_ROOT}/流浪地球 (2019)"
PLAN = organizer_v1_plan(
    JOB_ID,
    category="movie",
    destination_final=NATIVE_FINAL,
    native_staging=True,
    native_locator_kind="folder_path",
    organizer_final=FINAL_ROOT,
)
JOB = organizer_v1_job(PLAN, status="waiting_organizer", title="流浪地球")


class _PlanDatabase(RepositoryFakeMixin):
    @staticmethod
    def get_job(_job_id: int) -> dict:
        return JOB


def _task(**raw_overrides: object) -> dict:
    return attach_organizer_v1_task(
        {
            "id": 9,
            "raw_data": dict(raw_overrides),
        },
        PLAN,
    )


def _service() -> OrganizerService:
    service = OrganizerService.__new__(OrganizerService)
    service.db = _PlanDatabase()
    service.organizer_config = {"max_scan_depth": 8, "max_files_per_task": 500}
    service.categories = {"movie": {"label": "电影"}}
    service.openlist = SimpleNamespace(exists=lambda _path: False)
    bind_task_openlist_resolver(service, None, PLAN)
    return service


def _video_mapping() -> dict:
    return {
        "source_path": f"{STAGING_ROOT}/LiuLangDiQiu.2019.2160p/LiuLangDiQiu.2019.mkv",
        "source_name": "LiuLangDiQiu.2019.mkv",
        "target_path": f"{RESOURCE_ROOT}/流浪地球 (2019).mkv",
        "target_name": "流浪地球 (2019).mkv",
        "status": "ready",
        "raw_data": {"staging_file": True},
    }


class _ApplyBoundaryDatabase(RepositoryFakeMixin):
    def __init__(self, mapping: dict) -> None:
        self.task = {
            **_task(),
            "status": "manual_confirmed",
            "mappings": [{"id": 91, **mapping}],
            "operations": [],
            "evidence": {},
        }
        self.job = {**JOB, "source_provider_kind": "quark", "source_form": "share"}
        self.run_updates: list[tuple[int, str, dict]] = []
        self.events: list[tuple] = []

    def get_organizer_task(self, _task_id: int, include_children: bool = True) -> dict:
        return self.task

    def get_job(self, _job_id: int) -> dict:
        return self.job

    def replace_organizer_operations(self, _task_id: int, operations: list[dict]) -> None:
        self.task["operations"] = [
            {"id": index, **operation}
            for index, operation in enumerate(operations, start=1)
        ]

    def update_organizer_task(self, _task_id: int, **values) -> None:
        self.task.update(values)

    def update_organizer_run(self, run_id: int, status: str, **values) -> None:
        self.run_updates.append((run_id, status, values))

    def update_job(self, _job_id: int, **values) -> None:
        self.job.update(values)

    def update_job_if_status(
        self,
        _job_id: int,
        expected_statuses: set[str],
        **values,
    ) -> bool:
        if str(self.job.get("status") or "") not in expected_statuses:
            return False
        self.job.update(values)
        return True

    def add_event(self, *args) -> None:
        self.events.append(args)

    @staticmethod
    def list_guest_requests_by_job(_job_id: int) -> list[dict]:
        return []

    @staticmethod
    def release_organizer_locks(**_values) -> None:
        return None


class NativeStagingFrozenPathTests(unittest.TestCase):
    def test_plan_freezes_staging_inside_the_final_category_directory(self) -> None:
        paths = PLAN["paths"]

        self.assertEqual(paths["organizer"]["path"], STAGING_ROOT)
        self.assertEqual(paths["organizer_final"]["path"], FINAL_ROOT)
        self.assertEqual(paths["destination_staging"]["role"], "native_staging")
        self.assertEqual(
            paths["destination_staging"]["path"],
            f"{NATIVE_FINAL}/{NATIVE_STAGING_DIR}/job-{JOB_ID}",
        )
        # 与 rclone 形状相反：扫描根在搬运目标根内部。
        self.assertTrue(STAGING_ROOT.startswith(f"{FINAL_ROOT}/"))

    def test_target_category_prefers_organizer_final_over_native_composite(self) -> None:
        service = _service()

        target_category = service._target_category_for_task(
            _task(),
            service.categories["movie"],
        )

        # destination_final 是 "root-folder-id::电影"，OpenList 不能拿它当目标根。
        self.assertEqual(target_category[V1_DESTINATION_FINAL_FIELD], FINAL_ROOT)
        self.assertEqual(target_category["source_category_root_path"], STAGING_ROOT)
        self.assertTrue(target_category[V1_DESTINATION_IS_RESOURCE_FIELD])

    def test_runtime_target_root_override_cannot_redirect_native_staging_plan(self) -> None:
        service = _service()
        unsafe_task = _task(target_root_path="/其它挂载/电影")

        target_category = service._target_category_for_task(
            unsafe_task,
            service.categories["movie"],
        )

        self.assertEqual(target_category[V1_DESTINATION_FINAL_FIELD], FINAL_ROOT)


class NativeStagingScanRootTests(unittest.TestCase):
    def test_scan_root_must_be_the_nested_staging_dir_not_the_category_root(self) -> None:
        service = _service()
        target_category = service._target_category_for_task(
            _task(),
            service.categories["movie"],
        )

        service._validate_v1_task_boundaries(
            _task(),
            scan_root=STAGING_ROOT,
            target_category=target_category,
        )
        unsafe_roots = (
            FINAL_ROOT,
            f"{FINAL_ROOT}/{NATIVE_STAGING_DIR}",
            f"{STAGING_ROOT}/LiuLangDiQiu.2019.2160p",
        )
        for unsafe_root in unsafe_roots:
            with self.subTest(scan_root=unsafe_root), self.assertRaises(OpenListError):
                service._validate_v1_task_boundaries(
                    _task(),
                    scan_root=unsafe_root,
                    target_category=target_category,
                )


class NativeStagingMappingBoundaryTests(unittest.TestCase):
    def _validate(self, mapping: dict) -> None:
        service = _service()
        task = {**_task(), "mappings": [{"id": 91, **mapping}]}
        service._validate_v1_mapping_boundaries(task)
        service._validate_approval_mappings(task, task["mappings"])

    def test_staging_to_final_category_mapping_passes_all_boundaries(self) -> None:
        self._validate(_video_mapping())

    def test_target_left_inside_the_staging_dir_is_rejected(self) -> None:
        # 暂存目录本身就在 final_root 之下，destination_final 包含性检查会放行，
        # 必须由专门的暂存根检查拦住，否则文件永远留在 _入库暂存 里。
        service = _service()
        unsafe_targets = (
            f"{STAGING_ROOT}/流浪地球 (2019).mkv",
            f"{STAGING_ROOT}/流浪地球 (2019)/流浪地球 (2019).mkv",
            f"{FINAL_ROOT}/{NATIVE_STAGING_DIR}/job-{JOB_ID}/深层/文件.mkv",
        )
        for target_path in unsafe_targets:
            task = {
                **_task(),
                "mappings": [{"id": 91, **_video_mapping(), "target_path": target_path}],
            }
            with self.subTest(target_path=target_path), self.assertRaisesRegex(
                OpenListError, "target path stayed inside frozen staging root"
            ):
                service._validate_v1_mapping_boundaries(task)

    def test_target_outside_final_category_root_is_rejected(self) -> None:
        service = _service()
        task = {
            **_task(),
            "mappings": [
                {
                    "id": 91,
                    **_video_mapping(),
                    "target_path": "/天翼/电视剧/流浪地球 (2019)/流浪地球 (2019).mkv",
                }
            ],
        }

        with self.assertRaisesRegex(
            OpenListError, "target path escaped frozen destination_final"
        ):
            service._validate_v1_mapping_boundaries(task)

    def test_source_outside_nested_staging_root_is_rejected(self) -> None:
        service = _service()
        unsafe_sources = (
            # 分类根下的既有资源不属于本次入库，绝不能被当成搬运源。
            f"{FINAL_ROOT}/已入库资源/旧文件.mkv",
            STAGING_ROOT,
            FINAL_ROOT,
        )
        for source_path in unsafe_sources:
            task = {
                **_task(),
                "mappings": [{"id": 91, **_video_mapping(), "source_path": source_path}],
            }
            with self.subTest(source_path=source_path), self.assertRaisesRegex(
                OpenListError, "source path escaped frozen organizer root"
            ):
                service._validate_v1_mapping_boundaries(task)


class NativeStagingOperationTests(unittest.TestCase):
    def _category(self) -> dict:
        return _service()._target_category_for_task(
            _task(),
            {"label": "电影"},
        )

    def test_operations_move_out_of_staging_into_the_resource_directory(self) -> None:
        service = _service()

        operations = service._operations_for_mappings([_video_mapping()], self._category())

        move = next(item for item in operations if item["type"] == "move_file")
        self.assertEqual(move["source_path"], _video_mapping()["source_path"])
        self.assertEqual(move["target_path"], _video_mapping()["target_path"])
        # 暂存文件重试必须撞目标而不是删源文件。
        self.assertTrue(move["raw_data"]["fail_if_target_exists"])
        self.assertFalse(move["raw_data"]["delete_source_if_target_exists"])
        created = [item["target_path"] for item in operations if item["type"] == "create_dir"]
        self.assertIn(RESOURCE_ROOT, created)

    def test_cleanup_stays_inside_staging_and_never_touches_category_root(self) -> None:
        service = _service()

        operations = service._operations_for_mappings([_video_mapping()], self._category())

        cleanup = [item["target_path"] for item in operations if item["type"] == "cleanup_empty_dir"]
        self.assertEqual(cleanup, [f"{STAGING_ROOT}/LiuLangDiQiu.2019.2160p"])
        for unsafe in (FINAL_ROOT, STAGING_ROOT, f"{FINAL_ROOT}/{NATIVE_STAGING_DIR}"):
            self.assertNotIn(unsafe, cleanup)
        move_indexes = [i for i, item in enumerate(operations) if item["type"] == "move_file"]
        cleanup_indexes = [i for i, item in enumerate(operations) if item["type"] == "cleanup_empty_dir"]
        self.assertLess(max(move_indexes), min(cleanup_indexes))

    def test_source_cleanup_dirs_stop_at_the_staging_job_root(self) -> None:
        service = _service()

        dirs = service._source_cleanup_dirs(
            f"{STAGING_ROOT}/一层/二层/LiuLangDiQiu.2019.mkv",
            STAGING_ROOT,
        )

        self.assertEqual(
            dirs,
            [f"{STAGING_ROOT}/一层/二层", f"{STAGING_ROOT}/一层"],
        )
        self.assertNotIn(FINAL_ROOT, dirs)
        self.assertNotIn(STAGING_ROOT, dirs)


class NativeStagingApplyGuardTests(unittest.TestCase):
    def _assert_apply_rejects_mapping(self, mapping: dict, expected_message: str) -> None:
        database = _ApplyBoundaryDatabase(mapping)
        service = _service()
        service.db = database
        calls: list[str] = []
        service._operations_for_mappings = lambda *_args, **_kwargs: [
            {
                "type": "move_file",
                "source_path": mapping["source_path"],
                "target_path": mapping["target_path"],
                "status": "pending",
            }
        ]
        service._lock_keys = lambda _task: calls.append("lock") or []
        service._execute_operation = lambda _operation: calls.append("execute")

        result = service.apply_task(9, run_id=71)

        self.assertFalse(result["success"])
        self.assertIn(expected_message, result["message"])
        self.assertEqual(calls, [])
        self.assertEqual(database.task["status"], "failed")
        self.assertEqual(database.job["status"], JOB_REVIEW)
        self.assertEqual(database.run_updates[-1][0:2], (71, "failed"))
        self.assertTrue(all(item["status"] == "pending" for item in database.task["operations"]))

    def test_apply_rejects_target_redirected_outside_final_category_root(self) -> None:
        self._assert_apply_rejects_mapping(
            {
                **_video_mapping(),
                "target_path": "/其它挂载/电影/流浪地球 (2019)/流浪地球 (2019).mkv",
            },
            "target path escaped frozen destination_final",
        )

    def test_apply_rejects_source_taken_from_the_final_category_root(self) -> None:
        self._assert_apply_rejects_mapping(
            {
                **_video_mapping(),
                "source_path": f"{FINAL_ROOT}/已入库资源/旧文件.mkv",
            },
            "source path escaped frozen organizer root",
        )

    def test_apply_rejects_the_staging_root_itself_as_a_file(self) -> None:
        self._assert_apply_rejects_mapping(
            {**_video_mapping(), "source_path": STAGING_ROOT},
            "source path escaped frozen organizer root",
        )


if __name__ == "__main__":
    unittest.main()
