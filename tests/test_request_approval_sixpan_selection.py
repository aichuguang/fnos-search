from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import copy
import unittest
from types import SimpleNamespace

from flask import Flask, jsonify, request

from fnos_media_import.public_web import (
    _public_request_payload,
)
from fnos_media_import.importers.tianyi import TianyiImporter
from fnos_media_import.blueprints.requests import RequestsRouteContext, create_requests_blueprint
from fnos_media_import.services.request_approval_service import (
    RequestApprovalDependencies,
    RequestApprovalService,
)


class _Requests(RepositoryFakeMixin):
    def __init__(self, item: dict) -> None:
        self.item = copy.deepcopy(item)

    def get(self, request_id: int) -> dict | None:
        if int(self.item.get("id") or 0) != request_id:
            return None
        return copy.deepcopy(self.item)


class _Commands(RepositoryFakeMixin):
    def __init__(self, requests: _Requests) -> None:
        self.requests = requests

    def transition_with_event(self, request_id: int, **kwargs) -> bool:
        if int(self.requests.item.get("id") or 0) != request_id:
            return False
        expected = kwargs.get("expected_statuses") or set()
        if expected and self.requests.item.get("status") not in expected:
            return False
        for key in ("status", "public_status", "raw_data"):
            if key in kwargs:
                self.requests.item[key] = copy.deepcopy(kwargs[key])
        self.requests.item.update(copy.deepcopy(kwargs.get("request_updates") or {}))
        return True

    def bind_job_with_event(self, _request_id: int, **_kwargs) -> str:
        return "bound"


class _Jobs:
    @staticmethod
    def get(_job_id: int) -> dict | None:
        return None


def _service(item: dict, captured: dict) -> RequestApprovalService:
    requests = _Requests(item)

    def coordinate_import(**kwargs):
        captured.update(copy.deepcopy(kwargs))
        return SimpleNamespace(
            result={"success": True, "queued": True, "message": "queued"},
            job={},
            bind_outcome="queued",
        )

    return RequestApprovalService(
        RequestApprovalDependencies(
            requests=requests,
            commands=_Commands(requests),
            jobs=_Jobs(),
            coordinate_import=coordinate_import,
            public_status=lambda status: status,
            safe_result=lambda result: result,
            merge_raw_data=lambda current, patch: {**(current or {}), **patch},
            sanitize_request_payload=_public_request_payload,
            category_label=lambda key: key,
        )
    )


def _request_item(request_payload: dict) -> dict:
    return {
        "id": 41,
        "request_token": "request-token-41",
        "title": "Example",
        "category": "tv",
        "source_url": "magnet:?xt=urn:btih:abc",
        "password": "",
        "status": "pending_review",
        "raw_data": {"request": request_payload},
    }


class RequestApprovalSixPanSelectionTests(unittest.TestCase):
    def test_approval_preserves_only_sanitized_sixpan_fields(self) -> None:
        captured: dict = {}
        service = _service(
            _request_item(
                {
                    "ignore_files": ["episode-02.mkv", "episode-02.mkv", 7, "x" * 513],
                    "sixpan_selection": {
                        "total_count": "12",
                        "selected_count": 99,
                        "ignored_count": 999,
                        "parse_status": "FILES_READY",
                        "parse_error": "preview note",
                        "slow": True,
                        "ignore_files": ["stale-nested-value"],
                        "target_path": "/untrusted/path",
                        "vendor_payload": {"arbitrary": True},
                    },
                }
            ),
            captured,
        )

        result, status = service.approve(41, {}, admin="admin")

        self.assertEqual(status, 202)
        self.assertTrue(result["queued"])
        payload = captured["submit_payload"]
        self.assertEqual(payload["ignore_files"], ["episode-02.mkv", "7"])
        self.assertEqual(
            payload["sixpan_selection"],
            {
                "total_count": 12,
                "selected_count": 12,
                "ignored_count": 2,
                "parse_status": "files_ready",
                "parse_error": "preview note",
                "slow": True,
                "ignore_files": ["episode-02.mkv", "7"],
            },
        )
        self.assertNotIn("target_path", payload["sixpan_selection"])
        self.assertNotIn("vendor_payload", payload["sixpan_selection"])
        for field in ("destination_id", "idempotency_key", "executor_id", "config_revision"):
            self.assertNotIn(field, payload)

    def test_approval_falls_back_to_nested_ignore_files(self) -> None:
        captured: dict = {}
        service = _service(
            _request_item(
                {
                    "sixpan_selection": {
                        "parse_status": "empty_files",
                        "ignore_files": ["sample.mkv", "sample.mkv"],
                    }
                }
            ),
            captured,
        )

        service.approve(41, {}, admin="admin")

        payload = captured["submit_payload"]
        self.assertEqual(payload["ignore_files"], ["sample.mkv"])
        self.assertEqual(payload["sixpan_selection"]["ignore_files"], ["sample.mkv"])
        self.assertEqual(payload["sixpan_selection"]["ignored_count"], 1)

    def test_public_request_snapshot_uses_same_selection_whitelist(self) -> None:
        snapshot = _public_request_payload(
            {
                "title": "Example",
                "category": "tv",
                "ignore_files": ["sample.mkv"],
                "sixpan_selection": {
                    "total_count": 4,
                    "selected_count": 3,
                    "ignored_count": 500,
                    "parse_status": "files_ready",
                    "slow": False,
                    "ignore_files": ["stale.mkv"],
                    "unknown": "must-not-persist",
                },
            }
        )

        self.assertEqual(snapshot["ignore_files"], ["sample.mkv"])
        self.assertEqual(
            snapshot["sixpan_selection"],
            {
                "total_count": 4,
                "selected_count": 3,
                "ignored_count": 1,
                "parse_status": "files_ready",
                "slow": False,
                "ignore_files": ["sample.mkv"],
            },
        )


class RequestApprovalRouteTests(unittest.TestCase):
    def test_approve_route_passes_sanitized_selection_to_coordinator(self) -> None:
        captured: dict = {}
        service = _service(
            _request_item(
                {
                    "sixpan_selection": {
                        "parse_status": "files_ready",
                        "ignore_files": ["trailer.mkv"],
                        "unsupported": "drop-me",
                    }
                }
            ),
            captured,
        )
        app = Flask(__name__)

        def approve(request_id: int):
            result, status = service.approve(
                request_id, request.get_json(silent=True) or {}, admin="route-admin"
            )
            return jsonify(result), status

        handlers = {
            "dashboard": lambda: jsonify({}),
            "requests": lambda: jsonify({}),
            "request_detail": lambda _request_id: jsonify({}),
            "request_approve": approve,
            "request_reject": lambda _request_id: jsonify({}),
            "request_cancel": lambda _request_id: jsonify({}),
        }
        app.register_blueprint(
            create_requests_blueprint(
                RequestsRouteContext(admin_required=lambda view: view, handlers=handlers)
            )
        )

        response = app.test_client().post(
            "/api/admin/requests/41/approve",
            json={"title": "Approved title", "category": "anime"},
        )

        self.assertEqual(response.status_code, 202)
        payload = captured["submit_payload"]
        self.assertEqual(payload["title"], "Approved title")
        self.assertEqual(payload["category"], "anime")
        self.assertEqual(
            payload["sixpan_selection"],
            {
                "parse_status": "files_ready",
                "ignore_files": ["trailer.mkv"],
                "ignored_count": 1,
            },
        )


class RequestApprovalTianyiSelectionTests(unittest.TestCase):
    def test_review_retains_only_chosen_files_and_media_identity(self) -> None:
        captured: dict = {}
        original = {
            "tianyi_selection": {
                "mode": "items", "selected_files": [{"fid": "ep1", "name": "第一集.mkv", "untrusted": True}],
                "selected_folders": [], "target_path": "/not-allowed",
            },
            "media_identity": {
                "tmdb_id": 101, "media_type": "tv", "title": "正式片名", "year": "2026",
                "season_number": 1, "original_query": "搜索词", "untrusted": "drop",
            },
            "destination_id": "untrusted-destination",
        }
        item = _request_item(original)
        item["source_url"] = "https://cloud.189.cn/t/example"
        result, status = _service(item, captured).approve(41, {}, admin="admin")
        self.assertEqual(status, 202, result)
        payload = captured["submit_payload"]
        selected = TianyiImporter._select_items([
            {"file_id": "ep1", "name": "第一集.mkv"},
            {"file_id": "ep2", "name": "第二集.mkv"},
        ], payload["tianyi_selection"])
        self.assertEqual([entry["file_id"] for entry in selected], ["ep1"])
        self.assertNotIn("target_path", payload["tianyi_selection"])
        self.assertNotIn("untrusted", payload["tianyi_selection"]["selected_files"][0])
        self.assertEqual(payload["media_identity"], {key: value for key, value in original["media_identity"].items() if key != "untrusted"})
        self.assertNotIn("destination_id", payload)
        self.assertEqual(original["tianyi_selection"]["selected_files"][0]["untrusted"], True)

    def test_saved_invalid_selection_does_not_fall_back_to_whole_share(self) -> None:
        captured: dict = {}
        service = _service(_request_item({"tianyi_selection": {"mode": "items", "selected_files": [{}]}}), captured)
        result, status = service.approve(41, {}, admin="admin")
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])
        self.assertEqual(captured, {})

    def test_administrator_rename_is_not_overridden_by_old_tmdb_title(self) -> None:
        captured: dict = {}
        service = _service(_request_item({"media_identity": {"tmdb_id": 101, "media_type": "tv", "title": "旧片名"}}), captured)
        _, status = service.approve(41, {"title": "人工修正片名"}, admin="admin")
        self.assertEqual(status, 202)
        self.assertEqual(captured["submit_payload"]["title"], "人工修正片名")
        self.assertNotIn("media_identity", captured["submit_payload"])


if __name__ == "__main__":
    unittest.main()
