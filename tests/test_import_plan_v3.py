"""阶段 2：Plan v3、目标级幂等与不可变执行计划的契约测试。"""

from __future__ import annotations

import unittest
import uuid
import copy
from pathlib import Path

from fnos_media_import.constants import (
    JOB_CREATED,
    ROUTE_CLOUD139_DIRECT,
    ROUTE_QUARK_TO_MOBILE,
)
from fnos_media_import.database import Database
from fnos_media_import.services.import_job_service import ImportJobCreationService, ImportJobRetryService
from fnos_media_import.services.import_planner import (
    build_execution_plan,
    execution_plan_from_job,
    resolve_destination_id,
    StoragePlanError,
    validate_public_destination,
    validated_execution_plan_from_job,
)
from fnos_media_import.services.import_staging_service import map_execution_path_to_organizer


def _raw_config(**storage_overrides: object) -> dict:
    return {
        "storage": {
            "enabled": True,
            "plan_version": 3,
            "default_target": "mobile_main",
            "targets": {
                "mobile_main": {
                    "name": "移动云主盘",
                    "driver": "cmcc_api",
                    "enabled": True,
                    "guest_selectable": True,
                    "namespace_id": "cmcc:primary",
                    "native_ingress": ROUTE_CLOUD139_DIRECT,
                    "host": "https://example.com/hcy",
                    "access_token": "secret-token",
                    "categories": {
                        "movie": {
                            "final_root": "电影",
                            "organizer_namespace_id": "openlist:main",
                            "organizer_root": "/移动云盘A/电影",
                        }
                    },
                },
                "quark_archive": {
                    "name": "夸克归档",
                    "driver": "rclone",
                    "enabled": True,
                    "guest_selectable": False,
                    "remote": "quark_dst",
                    "categories": {
                        "movie": {
                            "final_root": "影视/电影",
                            "organizer_namespace_id": "openlist:main",
                            "organizer_root": "/夸克/影视/电影",
                        }
                    },
                },
            },
        },
        "organizer": {"enabled": True, "staging_enabled": True, "staging_dir_name": "_入库暂存"},
        "openlist": {"base_url": "http://127.0.0.1:5244"},
        "rclone": {"remote_name": "MP"},
    }


def _storage_disabled_config() -> dict:
    raw = _raw_config()
    raw["storage"] = {"enabled": False, "plan_version": 3, "default_target": "mobile_main", "targets": raw["storage"]["targets"]}
    return raw


class _Config:
    def __init__(self, raw: dict) -> None:
        self.raw = raw

    def category(self, key: str) -> dict:
        return {"label": "电影", "quark_save_path": "/离线下载/电影"}


class _Link:
    route = ROUTE_QUARK_TO_MOBILE
    source_type = "quark"
    url = "https://pan.quark.cn/s/plan-v3"
    password = ""
    supported = True

    @staticmethod
    def to_dict() -> dict:
        return {"route": ROUTE_QUARK_TO_MOBILE, "source_type": "quark"}


def _new_database() -> tuple[Database, Path]:
    root = Path(__file__).resolve().parents[1]
    path = root / f"plan-v3-{uuid.uuid4().hex}.db"
    database = Database(path)
    database.init_schema()
    return database, path


def _cleanup(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


class DestinationAwareDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database, self.path = _new_database()

    def tearDown(self) -> None:
        _cleanup(self.path)

    def _create(self, *, source_url: str, destination_id: str | None) -> tuple[int, bool]:
        return self.database.job_commands.create(
            {
                "title": "T",
                "category": "movie",
                "category_label": "电影",
                "source_type": "quark",
                "source_url": source_url,
                "target_route": ROUTE_QUARK_TO_MOBILE,
                "target_path": "/x",
                "status": JOB_CREATED,
                "destination_id": destination_id,
            }
        )

    def test_same_source_different_destination_can_coexist(self) -> None:
        _, a = self._create(source_url="u1", destination_id="mobile_main")
        _, b = self._create(source_url="u1", destination_id="quark_archive")
        self.assertTrue(a)
        self.assertTrue(b)

    def test_same_source_same_destination_is_deduped(self) -> None:
        _, a = self._create(source_url="u2", destination_id="mobile_main")
        job_id, b = self._create(source_url="u2", destination_id="mobile_main")
        self.assertTrue(a)
        self.assertFalse(b)
        self.assertGreater(job_id, 0)

    def test_legacy_null_destination_only_dedupes_against_null(self) -> None:
        _, a = self._create(source_url="u3", destination_id=None)
        _, b = self._create(source_url="u3", destination_id="mobile_main")
        self.assertTrue(a)
        self.assertTrue(b)
        _, c = self._create(source_url="u3", destination_id=None)
        self.assertFalse(c)


class ImportPlannerTests(unittest.TestCase):
    def test_resolve_prefers_requested_over_default(self) -> None:
        raw = _raw_config()
        self.assertEqual(resolve_destination_id(raw, "quark_archive"), "quark_archive")
        self.assertEqual(resolve_destination_id(raw, None), "mobile_main")
        self.assertIsNone(resolve_destination_id(raw, "disabled_or_missing"))

    def test_storage_disabled_returns_none(self) -> None:
        self.assertIsNone(resolve_destination_id(_storage_disabled_config(), "mobile_main"))

    def test_guest_default_must_be_guest_selectable(self) -> None:
        raw = _raw_config()
        raw["storage"]["targets"]["mobile_main"]["guest_selectable"] = False

        self.assertIsNone(
            resolve_destination_id(raw, None, require_guest_selectable=True)
        )
        with self.assertRaisesRegex(StoragePlanError, "默认存储目标"):
            validate_public_destination(
                raw_config=raw,
                requested=None,
                route=ROUTE_QUARK_TO_MOBILE,
                source_kind="quark",
                category_key="movie",
                category={"quark_save_path": "/离线下载/电影", "label": "电影"},
            )

    def test_guest_default_is_resolved_and_frozen_when_selectable(self) -> None:
        self.assertEqual(
            validate_public_destination(
                raw_config=_raw_config(),
                requested=None,
                route=ROUTE_QUARK_TO_MOBILE,
                source_kind="quark",
                category_key="movie",
                category={"quark_save_path": "/离线下载/电影", "label": "电影"},
            ),
            "mobile_main",
        )

    def test_guest_legacy_ingress_keeps_v2_path_after_default_guest_check(self) -> None:
        self.assertIsNone(
            validate_public_destination(
                raw_config=_raw_config(),
                requested=None,
                route="sixpan_offline",
                source_kind="magnet",
                category_key="movie",
                category={"label": "电影"},
            )
        )

    def test_strategy_relay_for_cmcc_target(self) -> None:
        plan = build_execution_plan(
            raw_config=_raw_config(),
            route=ROUTE_QUARK_TO_MOBILE,
            source_kind="quark",
            category_key="movie",
            category={"quark_save_path": "/离线下载/电影", "label": "电影"},
            job_id=42,
            destination_id="mobile_main",
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        self.assertEqual(plan["transfer"]["strategy"], "relay_upload")
        self.assertEqual(plan["destination"]["id"], "mobile_main")
        self.assertEqual(plan["paths"]["landing"]["path"], "/离线下载/电影/job-42")

    def test_strategy_rclone_copy_for_rclone_target(self) -> None:
        plan = build_execution_plan(
            raw_config=_raw_config(),
            route=ROUTE_QUARK_TO_MOBILE,
            source_kind="quark",
            category_key="movie",
            category={"quark_save_path": "/离线下载/电影", "label": "电影"},
            job_id=7,
            destination_id="quark_archive",
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        self.assertEqual(plan["transfer"]["strategy"], "rclone_copy")
        self.assertEqual(plan["paths"]["staging"]["namespace_id"], "rclone:quark_dst")

    def test_plan_contains_no_credentials(self) -> None:
        plan = build_execution_plan(
            raw_config=_raw_config(),
            route=ROUTE_QUARK_TO_MOBILE,
            source_kind="quark",
            category_key="movie",
            category={"quark_save_path": "/离线下载/电影", "label": "电影"},
            job_id=1,
            destination_id="mobile_main",
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        blob = str(plan)
        self.assertNotIn("secret-token", blob)
        self.assertNotIn("access_token", blob)

    def test_native_plan_freezes_binding_and_rejects_binding_tampering(self) -> None:
        raw = _raw_config()
        target = raw["storage"]["targets"].pop("mobile_main")
        target["namespace_id"] = "cmcc:renamed-primary"
        raw["storage"]["targets"]["renamed_primary"] = target
        raw["storage"]["default_target"] = "renamed_primary"
        plan = build_execution_plan(
            raw_config=raw,
            route=ROUTE_CLOUD139_DIRECT,
            source_kind="cloud139",
            category_key="movie",
            category={"label": "电影"},
            job_id=43,
            destination_id="renamed_primary",
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        self.assertEqual(plan["transfer"]["strategy"], "native_save")
        self.assertEqual(plan["destination"]["native_ingress"], ROUTE_CLOUD139_DIRECT)

        tampered = {
            "id": 43,
            "plan_version": 3,
            "destination_id": "renamed_primary",
            "raw_data": {"execution_plan": copy.deepcopy(plan)},
        }
        tampered["raw_data"]["execution_plan"]["destination"]["native_ingress"] = ""
        with self.assertRaisesRegex(StoragePlanError, "来源与目标绑定"):
            validated_execution_plan_from_job(tampered)

    def test_validated_reader_rejects_job_id_mismatch(self) -> None:
        plan = build_execution_plan(
            raw_config=_raw_config(),
            route=ROUTE_QUARK_TO_MOBILE,
            source_kind="quark",
            category_key="movie",
            category={"quark_save_path": "/离线下载/电影", "label": "电影"},
            job_id=42,
            destination_id="mobile_main",
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        with self.assertRaises(ValueError):
            validated_execution_plan_from_job({
                "id": 99,
                "plan_version": 3,
                "destination_id": "mobile_main",
                "raw_data": {"execution_plan": plan},
            })
        self.assertEqual(
            validated_execution_plan_from_job({
                "id": 42,
                "plan_version": 3,
                "destination_id": "mobile_main",
                "raw_data": {"execution_plan": plan},
            })["version"],
            3,
        )

    def test_validated_reader_rejects_strategy_driver_or_source_namespace_tampering(self) -> None:
        plan = build_execution_plan(
            raw_config=_raw_config(),
            route=ROUTE_QUARK_TO_MOBILE,
            source_kind="quark",
            category_key="movie",
            category={"quark_save_path": "/离线下载/电影", "label": "电影"},
            job_id=42,
            destination_id="quark_archive",
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        job = {
            "id": 42,
            "plan_version": 3,
            "destination_id": "quark_archive",
            "raw_data": {"execution_plan": plan},
        }
        tampered_driver = copy.deepcopy(job)
        tampered_driver["raw_data"]["execution_plan"]["destination"]["driver"] = "cmcc_api"
        with self.assertRaises(StoragePlanError):
            validated_execution_plan_from_job(tampered_driver)

        tampered_source = copy.deepcopy(job)
        tampered_source["raw_data"]["execution_plan"]["paths"]["landing"]["namespace_id"] = "rclone:other"
        with self.assertRaises(StoragePlanError):
            validated_execution_plan_from_job(tampered_source)

    def test_worker_plan_without_organizer_still_uses_task_directory_under_final_root(self) -> None:
        raw = _raw_config()
        raw["organizer"]["enabled"] = False
        plan = build_execution_plan(
            raw_config=raw,
            route=ROUTE_QUARK_TO_MOBILE,
            source_kind="quark",
            category_key="movie",
            category={"quark_save_path": "/离线下载/电影", "label": "电影"},
            job_id=7,
            destination_id="quark_archive",
            staging_enabled=False,
            staging_dir_name="_入库暂存",
        )
        self.assertEqual(plan["paths"]["staging"]["path"], "影视/电影/job-7")

    def test_execution_path_maps_to_frozen_organizer_job_root(self) -> None:
        plan = build_execution_plan(
            raw_config=_raw_config(),
            route=ROUTE_QUARK_TO_MOBILE,
            source_kind="quark",
            category_key="movie",
            category={"quark_save_path": "/离线下载/电影", "label": "电影"},
            job_id=42,
            destination_id="quark_archive",
            staging_enabled=True,
            staging_dir_name="_入库暂存",
        )
        staging = plan["paths"]["staging"]["path"]
        organizer = plan["paths"]["organizer"]["path"]
        self.assertEqual(
            map_execution_path_to_organizer(f"{staging}/episode.mkv", plan),
            f"{organizer}/episode.mkv",
        )
        self.assertEqual(map_execution_path_to_organizer("/other/episode.mkv", plan), "")


class ImportJobCreationServiceV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.database, self.path = _new_database()
        self.submits: list[str] = []

    def tearDown(self) -> None:
        _cleanup(self.path)

    def _service(self, raw: dict) -> ImportJobCreationService:
        config = _Config(raw)

        def submit_quark(job_id, _title, _url, _target, _category_key, _category, **_kw):
            self.submits.append(str(job_id))
            return {"job": self.database.get_job(job_id), "created": True, "success": True}

        return ImportJobCreationService(
            database=self.database,
            config=config,
            detect_link=lambda *_a, **_kw: _Link(),
            job_source_url=lambda url, _p: url,
            target_path=lambda _route, _category, **_kw: "/离线下载/电影",
            staging_plan=None,
            submit_quark=submit_quark,
            submit_cloud139=lambda *_a, **_kw: {},
            submit_generic=lambda *_a, **_kw: {},
        )

    def test_v3_job_persists_destination_and_execution_plan(self) -> None:
        service = self._service(_raw_config())
        result = service.create({"url": "https://pan.quark.cn/s/abc", "category": "movie"})
        self.assertTrue(result["created"])
        job = self.database.get_job(result["job"]["id"])
        self.assertEqual(job["destination_id"], "mobile_main")
        self.assertEqual(job["plan_version"], 3)
        plan = execution_plan_from_job(job)
        self.assertEqual(plan["destination"]["id"], "mobile_main")
        self.assertEqual(plan["transfer"]["strategy"], "relay_upload")

    def test_storage_disabled_job_stays_legacy(self) -> None:
        service = self._service(_storage_disabled_config())
        result = service.create({"url": "https://pan.quark.cn/s/def", "category": "movie"})
        job = self.database.get_job(result["job"]["id"])
        self.assertIsNone(job["destination_id"])
        self.assertIsNone(job["plan_version"])
        self.assertEqual(execution_plan_from_job(job), {})

    def test_requested_destination_wins(self) -> None:
        service = self._service(_raw_config())
        result = service.create(
            {"url": "https://pan.quark.cn/s/ghi", "category": "movie", "destination_id": "quark_archive"}
        )
        job = self.database.get_job(result["job"]["id"])
        self.assertEqual(job["destination_id"], "quark_archive")
        self.assertEqual(execution_plan_from_job(job)["transfer"]["strategy"], "rclone_copy")

    def test_provider_target_path_uses_frozen_execution_plan(self) -> None:
        service = self._service(_raw_config())
        result = service.create({"url": "https://pan.quark.cn/s/jkl", "category": "movie"})
        job = self.database.get_job(result["job"]["id"])
        derived = ImportJobRetryService._provider_target_path(job)
        self.assertEqual(derived, f"/离线下载/电影/job-{job['id']}")


if __name__ == "__main__":
    unittest.main()
