"""阶段 5：存储目标管理 API、访客可选目标提示与请求级 destination_id 契约。"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify, request

from fnos_media_import.blueprints.settings import SettingsRouteContext, create_settings_blueprint
from fnos_media_import.config_persistence import normalize_advanced_config_payload
from fnos_media_import.services.settings_service import SettingsDependencies, SettingsService

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SUBMIT_JS = ROOT / "static" / "public-submit.js"
ADMIN_SETTINGS_JS = ROOT / "static" / "admin-settings.js"
ADMIN_HTML = ROOT / "templates" / "admin.html"


def _base_raw_config() -> dict[str, Any]:
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
                    "host": "https://example.com/hcy",
                    "auth_mode": "web_basic",
                    "access_token": "super-secret-token-must-not-leak",
                    "phone": "13800000000",
                    "categories": {"movie": {"final_root": "电影"}},
                },
                "quark_archive": {
                    "name": "夸克归档",
                    "driver": "rclone",
                    "enabled": True,
                    "guest_selectable": False,
                    "remote": "quark_dst",
                },
                "local_library": {
                    "name": "本地影视库",
                    "driver": "rclone",
                    "enabled": True,
                    "guest_selectable": True,
                    "root": "/storage-targets/local-library",
                },
            },
        }
    }


class _Db:
    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}

    def get_app_settings(self) -> dict[str, Any]:
        return self.settings

    def set_app_settings(self, updates: dict[str, Any]) -> None:
        self.settings.update(copy.deepcopy(updates))


def _build_app(*, deny_admin: bool = False) -> tuple[Flask, SettingsService, _Db, dict[str, Any]]:
    app = Flask(__name__)
    raw = _base_raw_config()
    db = _Db()

    def protected(handler: Callable[..., Any]) -> Callable[..., Any]:
        if deny_admin:

            def denied(*_args: Any, **_kwargs: Any) -> Any:
                return jsonify({"success": False}), 401

            return denied

        return handler

    service = SettingsService(
        SettingsDependencies(
            db=db,
            raw_config=lambda: raw,
            redact_config=lambda cfg: cfg,
            advanced_response=lambda effective, settings: {
                "config": effective,
                "stored": settings,
                "meta": {"database_configured": bool(settings)},
            },
            normalize_advanced=normalize_advanced_config_payload,
            advanced_key="advanced_config",
            reload_runtime=lambda: {"ok": True},
            effective_settings=lambda: {},
            payload_bool=lambda payload, key, default: bool(payload.get(key, default)),
        )
    )

    # 与 app.py 一致：路由闭包读取 request，向服务方法传参。
    def _storage_test():
        payload = request.get_json(silent=True) or {}
        return service.storage_test(payload.get("destination_id"))

    def _storage_update():
        return service.storage_update(request.get_json(silent=True) or {})

    app.register_blueprint(
        create_settings_blueprint(
            SettingsRouteContext(
                admin_required=protected,
                config=service.config,
                history_summary=service.history_summary,
                cleanup_history=service.cleanup_history,
                advanced_config=service.advanced,
                advanced_config_update=service.update_advanced,
                advanced_export=service.export_advanced,
                settings=service.settings,
                settings_update=service.update_settings,
                settings_update_all=service.update_all,
                storage_status=service.storage_status,
                storage_test=_storage_test,
                storage_update=_storage_update,
                public_storage_targets=service.public_storage_targets,
            )
        )
    )
    return app, service, db, raw


class StorageAdminApiTests(unittest.TestCase):
    def test_admin_storage_status_lists_targets_without_credentials(self) -> None:
        app, _service, _db, _raw = _build_app()
        client = app.test_client()

        response = client.get("/api/admin/settings/storage/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["default_target"], "mobile_main")
        by_id = {item["destination_id"]: item for item in payload["targets"]}
        self.assertEqual(
            set(by_id),
            {"mobile_main", "quark_archive", "local_library"},
        )
        mobile = by_id["mobile_main"]
        self.assertEqual(mobile["name"], "移动云主盘")
        self.assertEqual(mobile["driver"], "cmcc_api")
        self.assertTrue(mobile["enabled"])
        self.assertTrue(mobile["guest_selectable"])
        self.assertTrue(mobile["default"])
        self.assertIn("supports_relay_upload", mobile["capabilities"])
        self.assertEqual(set(mobile.keys()), {"destination_id", "name", "driver", "enabled", "guest_selectable", "default", "mode_label", "capabilities"})

        raw_text = response.get_data(as_text=True)
        self.assertNotIn("access_token", raw_text)
        self.assertNotIn("super-secret-token-must-not-leak", raw_text)
        self.assertNotIn("13800000000", raw_text)
        self.assertNotIn("phone", raw_text)

    def test_admin_storage_status_when_disabled(self) -> None:
        app, _service, _db, raw = _build_app()
        raw["storage"]["enabled"] = False
        client = app.test_client()

        payload = client.get("/api/admin/settings/storage/status").get_json()
        self.assertTrue(payload["success"])
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["targets"], [])

    def test_admin_storage_test_ok_for_valid_cmcc_target(self) -> None:
        app, _service, _db, _raw = _build_app()
        client = app.test_client()

        response = client.post(
            "/api/admin/settings/storage/test",
            json={"destination_id": "mobile_main"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["destination_id"], "mobile_main")
        self.assertEqual(payload["errors"], [])

    def test_admin_storage_test_reports_unknown_driver(self) -> None:
        app, _service, _db, raw = _build_app()
        raw["storage"]["targets"]["mystery"] = {
            "name": "神秘盘",
            "driver": "not_a_real_driver",
            "enabled": True,
            "guest_selectable": True,
        }
        client = app.test_client()

        response = client.post(
            "/api/admin/settings/storage/test",
            json={"destination_id": "mystery"},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertTrue(any("未知存储驱动" in error for error in payload["errors"]))

    def test_admin_storage_test_missing_target(self) -> None:
        app, _service, _db, _raw = _build_app()
        client = app.test_client()

        response = client.post(
            "/api/admin/settings/storage/test",
            json={"destination_id": "does_not_exist"},
        )
        self.assertEqual(response.status_code, 404)

    def test_public_storage_targets_returns_selectable_only(self) -> None:
        app, _service, _db, _raw = _build_app()
        client = app.test_client()

        payload = client.get("/api/public/storage-targets").get_json()
        self.assertTrue(payload["success"])
        targets = payload["targets"]
        ids = [item["destination_id"] for item in targets]
        # quark_archive 是 guest_selectable=False，不得出现。
        self.assertEqual(set(ids), {"mobile_main", "local_library"})
        by_id = {item["destination_id"]: item for item in targets}
        self.assertEqual(by_id["mobile_main"]["name"], "移动云主盘")
        self.assertTrue(by_id["mobile_main"]["default"])

    def test_public_storage_targets_filters_disabled_and_unknown_driver(self) -> None:
        app, _service, _db, raw = _build_app()
        raw["storage"]["targets"]["local_library"]["enabled"] = False
        raw["storage"]["targets"]["mobile_main"]["driver"] = "unknown_driver"
        client = app.test_client()

        payload = client.get("/api/public/storage-targets").get_json()
        self.assertEqual(payload["targets"], [])

    def test_public_storage_targets_disabled_master_switch(self) -> None:
        app, _service, _db, raw = _build_app()
        raw["storage"]["enabled"] = False
        client = app.test_client()

        payload = client.get("/api/public/storage-targets").get_json()
        self.assertEqual(payload["targets"], [])

    def test_public_storage_targets_single_target_keeps_destination_id(self) -> None:
        app, _service, _db, raw = _build_app()
        raw["storage"]["targets"]["local_library"]["guest_selectable"] = False
        client = app.test_client()

        payload = client.get("/api/public/storage-targets").get_json()
        targets = payload["targets"]
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["destination_id"], "mobile_main")

    def test_admin_storage_routes_are_admin_protected_but_public_is_open(self) -> None:
        app, _service, _db, _raw = _build_app(deny_admin=True)
        client = app.test_client()

        self.assertEqual(client.get("/api/admin/settings/storage/status").status_code, 401)
        self.assertEqual(client.post("/api/admin/settings/storage/test", json={}).status_code, 401)
        self.assertEqual(client.post("/api/admin/settings/storage", json={}).status_code, 401)
        self.assertEqual(client.put("/api/admin/settings/storage", json={}).status_code, 401)
        # 访客目标提示端点不受管理员鉴权保护。
        self.assertEqual(client.get("/api/public/storage-targets").status_code, 200)

    def test_storage_update_persists_flag_and_rejects_unknown_target(self) -> None:
        app, service, db, _raw = _build_app()
        client = app.test_client()

        response = client.post(
            "/api/admin/settings/storage",
            json={"destination_id": "quark_archive", "guest_selectable": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        stored = db.settings["advanced_config"]
        self.assertTrue(stored["storage"]["targets"]["quark_archive"]["guest_selectable"])
        # 其余目标不受影响。
        self.assertIn("mobile_main", stored["storage"]["targets"])
        self.assertIn("local_library", stored["storage"]["targets"])

        missing = client.post(
            "/api/admin/settings/storage",
            json={"destination_id": "nope", "enabled": True},
        )
        self.assertEqual(missing.status_code, 404)

    def test_guest_submit_selector_logic_single_target_no_selector(self) -> None:
        source = PUBLIC_SUBMIT_JS.read_text(encoding="utf-8")
        # 只有 0/1 个目标时不创建选择器，而是冻结单个 destination_id。
        self.assertIn("publicState.pendingDestinationId = \"\";", source)
        self.assertIn("if (selectable.length < 2)", source)
        self.assertIn("if (selectable.length === 1) publicState.pendingDestinationId = selectable[0].destination_id;", source)
        # 2+ 个目标时才展示“存入”选择器。
        self.assertIn("const row = document.createElement(\"div\");", source)
        self.assertIn('row.id = "publicConfirmDestinationRow";', source)
        self.assertIn('select.id = "publicConfirmDestination";', source)
        # 提交载荷仍兼容：destination_id 是增量可选字段。
        self.assertIn("if (destinationId) submitPayload.destination_id = destinationId;", source)
        self.assertIn('body: JSON.stringify(submitPayload),', source)
        self.assertNotIn('body: JSON.stringify({ public_id: publicId, category, note', source)

    def test_admin_ui_has_storage_targets_section(self) -> None:
        html = ADMIN_HTML.read_text(encoding="utf-8")
        script = ADMIN_SETTINGS_JS.read_text(encoding="utf-8")
        for element_id in (
            "storageTargetsRefreshBtn",
            "storageTargetsStatus",
            "storageTargetsBody",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("存储目标", html)
        self.assertIn('requestJson("/api/admin/settings/storage/status"', script)
        self.assertIn('"/api/admin/settings/storage/test"', script)
        self.assertIn('method: "PATCH"', script)
        self.assertIn('data-storage-enabled', script)
        self.assertIn('data-storage-guest', script)


if __name__ == "__main__":
    unittest.main()
