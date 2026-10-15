from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify

from fnos_media_import.openapi import get_openapi_spec
from fnos_media_import.services.rclone_webdav_config_service import RcloneWebdavConfigError


ROOT = Path(__file__).resolve().parents[1]


class MulticloudApiContractTests(unittest.TestCase):
    def test_route_puts_document_required_snapshots_and_conflict_responses(self):
        spec = get_openapi_spec()
        for path in ("/api/admin/storage-setup/routes", "/api/admin/source-assignments"):
            with self.subTest(path=path):
                operation = spec["paths"][path]["put"]
                schema_name = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
                schema = spec["components"]["schemas"][schema_name]
                self.assertIn("routing_token", schema["required"])
                self.assertEqual(schema["properties"]["routing_token"]["$ref"], "#/components/schemas/RoutingSnapshotToken")
                self.assertTrue({"409", "428"}.issubset(operation["responses"]))
                self.assertEqual(operation["security"], [{"cookieAuth": []}])

    def test_snapshot_gets_document_the_token_and_null_category_revisions(self):
        spec = get_openapi_spec()
        for path in ("/api/admin/storage-setup", "/api/admin/source-assignments", "/api/admin/routing-matrix"):
            schema = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
            self.assertIn("routing_token", schema["required"])
        revision = spec["components"]["schemas"]["StorageSetupRoute"]["properties"]["assignment_revisions"]["additionalProperties"]
        self.assertTrue(revision["nullable"])

    def test_web_and_worker_keep_remote_failure_classification(self):
        tree = ast.parse((ROOT / "fnos_media_import/app.py").read_text(encoding="utf-8"))
        names = {"_rclone_webdav_response", "_rclone_worker_webdav_response"}
        namespace = {"Any": Any, "Callable": Callable, "jsonify": jsonify, "RcloneWebdavConfigError": RcloneWebdavConfigError}
        module = ast.Module(body=[node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "isolated-webdav-response", "exec"), namespace)
        app = Flask(__name__)
        for name in names:
            for code in ("RCLONE_UNAVAILABLE", "RCLONE_SYNC_RESULT_UNKNOWN", "RCLONE_SYNC_CONFLICT"):
                with self.subTest(handler=name, code=code), app.test_request_context():
                    def fail():
                        raise RcloneWebdavConfigError("classified failure", 503, code=code)

                    response, status = namespace[name](fail)
                    self.assertEqual(status, 503)
                    self.assertEqual(response.get_json()["code"], code)


if __name__ == "__main__":
    unittest.main()
