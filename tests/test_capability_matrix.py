"""阶段 6：ImportPlanner 能力矩阵与策略选择优先级。"""

from __future__ import annotations

import unittest

from fnos_media_import.services.import_planner import build_execution_plan


def _raw(*, with_local: bool = False) -> dict:
    targets = {
        "mobile_main": {
            "name": "移动云主盘", "driver": "cmcc_api", "enabled": True, "guest_selectable": True,
            "host": "https://x", "access_token": "t", "namespace_id": "cmcc:primary",
            "native_ingress": "cloud139_direct",
            "categories": {"movie": {"final_root": "电影", "organizer_namespace_id": "openlist:main", "organizer_root": "/移动云盘A/电影"}},
        },
        "quark_archive": {
            "name": "夸克归档", "driver": "rclone", "enabled": True, "guest_selectable": False,
            "remote": "quark_dst",
            "categories": {"movie": {"final_root": "影视/电影", "organizer_namespace_id": "openlist:main", "organizer_root": "/夸克/影视/电影"}},
        },
    }
    if with_local:
        targets["local_library"] = {
            "name": "本地影视库", "driver": "rclone", "enabled": True, "guest_selectable": False,
            "root": "/storage-targets/local-library",
            "categories": {"movie": {"final_root": "Movies", "organizer_namespace_id": "openlist:main", "organizer_root": "/本地影视库/Movies"}},
        }
    return {
        "storage": {"enabled": True, "plan_version": 3, "default_target": "mobile_main", "targets": targets},
        "organizer": {"enabled": True, "staging_enabled": True, "staging_dir_name": "_入库暂存"},
        "openlist": {"base_url": "http://127.0.0.1:5244"},
        "rclone": {"remote_name": "MP"},
    }


def _plan(raw: dict, *, route: str, destination_id: str) -> dict | None:
    return build_execution_plan(
        raw_config=raw,
        route=route,
        source_kind="quark",
        category_key="movie",
        category={"quark_save_path": "/离线下载/电影", "label": "电影"},
        job_id=1,
        destination_id=destination_id,
        staging_enabled=True,
        staging_dir_name="_入库暂存",
    )


class CapabilityMatrixTests(unittest.TestCase):
    def test_quark_to_cmcc_is_relay_upload(self) -> None:
        plan = _plan(_raw(), route="quark_to_mobile", destination_id="mobile_main")
        self.assertEqual(plan["transfer"]["strategy"], "relay_upload")

    def test_quark_to_rclone_target_is_rclone_copy(self) -> None:
        plan = _plan(_raw(), route="quark_to_mobile", destination_id="quark_archive")
        self.assertEqual(plan["transfer"]["strategy"], "rclone_copy")

    def test_quark_to_local_target_is_rclone_copy(self) -> None:
        plan = _plan(_raw(with_local=True), route="quark_to_mobile", destination_id="local_library")
        self.assertEqual(plan["transfer"]["strategy"], "rclone_copy")
        self.assertTrue(plan["paths"]["staging"]["path"].startswith("/storage-targets/local-library/"))

    def test_cloud139_to_cmcc_is_native_save(self) -> None:
        plan = _plan(_raw(), route="cloud139_direct", destination_id="mobile_main")
        self.assertEqual(plan["transfer"]["strategy"], "native_save")
        self.assertEqual(plan["transfer"]["executor"], "none")

    def test_cloud139_native_save_does_not_depend_on_target_id_or_namespace(self) -> None:
        raw = _raw()
        target = raw["storage"]["targets"].pop("mobile_main")
        target["namespace_id"] = "cmcc:archive-account"
        raw["storage"]["targets"]["renamed_primary"] = target
        plan = _plan(raw, route="cloud139_direct", destination_id="renamed_primary")
        self.assertEqual(plan["transfer"]["strategy"], "native_save")

    def test_cloud139_to_second_cmcc_account_is_not_assumed_native(self) -> None:
        raw = _raw()
        raw["storage"]["targets"]["mobile_backup"] = {
            **raw["storage"]["targets"]["mobile_main"],
            "name": "移动云备用盘",
            "namespace_id": "cmcc:backup",
            "native_ingress": "",
        }
        plan = _plan(raw, route="cloud139_direct", destination_id="mobile_backup")
        self.assertEqual(plan["transfer"]["strategy"], "manual_review")

    def test_sixpan_ingress_stays_manual_review_in_v3(self) -> None:
        plan = _plan(_raw(), route="sixpan_offline", destination_id="mobile_main")
        self.assertEqual(plan["transfer"]["strategy"], "manual_review")

    def test_no_combinatorial_route_constants_are_introduced(self) -> None:
        # 策略由 来源×目标能力矩阵 派生，不新增 "quark_to_quark" 之类的组合 route。
        plan = _plan(_raw(), route="quark_to_mobile", destination_id="quark_archive")
        self.assertEqual(plan["source"]["ingress"], "quark_to_mobile")
        self.assertEqual(plan["destination"]["driver"], "rclone")


if __name__ == "__main__":
    unittest.main()
