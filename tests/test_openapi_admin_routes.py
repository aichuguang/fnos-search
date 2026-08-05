from __future__ import annotations

import unittest

from flask import Flask, jsonify

from fnos_media_import.blueprints.settings import SettingsRouteContext, create_settings_blueprint
from fnos_media_import.blueprints.sixpan import SixPanRouteContext, create_sixpan_blueprint
from fnos_media_import.openapi import get_openapi_spec


class OpenApiAdminRouteTests(unittest.TestCase):
    def test_sensitive_config_export_is_post_only(self) -> None:
        path = get_openapi_spec()["paths"]["/api/admin/advanced-config/export"]
        self.assertIn("post", path)
        self.assertNotIn("get", path)
        self.assertEqual(
            path["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AdvancedConfigExportRequest",
        )

    def test_new_admin_mutation_routes_are_documented(self) -> None:
        paths = get_openapi_spec()["paths"]
        self.assertIn("post", paths["/api/admin/trending/candidates/{candidate_id}/subscribe"])
        batch = paths["/api/admin/organizer/tasks/{task_id}/mappings/batch"]["post"]
        self.assertEqual(
            batch["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/OrganizerMappingsBatchUpdateRequest",
        )
        retry_refresh = paths["/api/admin/sixpan/jobs/{job_id}/retry-media-refresh"]["post"]
        self.assertEqual(retry_refresh["parameters"][0]["name"], "job_id")
        self.assertEqual(retry_refresh["security"], [{"cookieAuth": []}])

    def test_operational_health_routes_are_documented(self) -> None:
        paths = get_openapi_spec()["paths"]
        self.assertIn("get", paths["/livez"])
        self.assertIn("get", paths["/readyz"])
        dependencies = paths["/dependencies"]["get"]
        self.assertEqual(dependencies["security"], [{"cookieAuth": []}])
        self.assertIn("401", dependencies["responses"])

    def test_settings_blueprint_does_not_expose_sensitive_export_via_get(self) -> None:
        app = Flask(__name__)

        def protected(handler):
            return handler

        def ok():
            return jsonify({"success": True})

        app.register_blueprint(
            create_settings_blueprint(
                SettingsRouteContext(
                    admin_required=protected,
                    config=ok,
                    history_summary=ok,
                    cleanup_history=ok,
                    advanced_config=ok,
                    advanced_config_update=ok,
                    advanced_export=ok,
                    settings=ok,
                    settings_update=ok,
                    settings_update_all=ok,
                )
            )
        )
        client = app.test_client()
        self.assertEqual(client.get("/api/admin/advanced-config/export").status_code, 405)
        self.assertEqual(client.post("/api/admin/advanced-config/export").status_code, 200)
        self.assertEqual(client.post("/api/admin/settings/all").status_code, 200)

    def test_sixpan_media_refresh_retry_route_is_admin_protected(self) -> None:
        app = Flask(__name__)
        calls: list[int] = []

        def protected(_handler):
            def denied(*_args, **_kwargs):
                return jsonify({"success": False}), 401

            return denied

        def ok(*_args, **_kwargs):
            calls.append(1)
            return jsonify({"success": True})

        app.register_blueprint(
            create_sixpan_blueprint(
                SixPanRouteContext(
                    admin_required=protected,
                    handlers={
                        "tasks": ok,
                        "probe": ok,
                        "oauth_device_code": ok,
                        "oauth_device_code_check": ok,
                        "sync": ok,
                        "retry_media_refresh": ok,
                    },
                )
            )
        )

        response = app.test_client().post(
            "/api/admin/sixpan/jobs/21/retry-media-refresh"
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
