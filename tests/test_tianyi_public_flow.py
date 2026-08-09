from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fnos_media_import.classifiers.link_classifier import detect_link
from fnos_media_import.domain.models import SelectionSnapshot
from fnos_media_import.importers.tianyi import TianyiImporter
from fnos_media_import.providers.pansou import PanSouClient
from fnos_media_import.public_web import (
    _detail_capability_for_source,
    _public_request_payload,
    _public_resource_child_files,
    _public_resource_detail,
    _public_resource_item,
    _safe_public_tianyi_selection,
    _preflight_public_submission,
)
from fnos_media_import.services.import_job_service import ImportJobCreationService
from fnos_media_import.services.public_manual_preview_service import (
    PublicManualPreviewService,
)
from fnos_media_import.services.public_submission_preparation_service import (
    PublicSubmissionPreparationService,
)


class _Cloud139Stub:
    configured = False
    check_before_save = False


class _TianyiStub(RepositoryFakeMixin):
    def __init__(
        self,
        *,
        configured: bool = True,
        share_ok: bool = True,
        share_data: Any | None = None,
        child_data: dict[str, Any] | None = None,
    ) -> None:
        self.configured = configured
        self.share_ok = share_ok
        self.share_data = share_data
        self.child_data = child_data
        self.check_calls: list[tuple[str, str, str]] = []
        self.list_calls: list[dict[str, Any]] = []

    def check_share(
        self,
        share_url: str,
        title: str = "",
        password: str = "",
    ) -> tuple[bool, Any]:
        self.check_calls.append((share_url, title, password))
        if self.share_data is not None:
            return self.share_ok, self.share_data
        if not self.share_ok:
            return False, {"success": False, "message": "share expired"}
        return True, {
            "success": True,
            "share_id": "share-1",
            "share_mode": 1,
            "root_file_id": "root-1",
            "file_name": "Example",
            "is_folder": True,
            "items": [
                {
                    "file_id": "file-1",
                    "file_name": "movie.mkv",
                    "is_dir": False,
                    "size": 1024,
                },
                {
                    "file_id": "dir-1",
                    "file_name": "Season 01",
                    "is_dir": True,
                    "size": 0,
                },
            ],
        }

    def list_files(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls.append(dict(kwargs))
        return self.child_data or {
            "success": True,
            "items": [
                {
                    "file_id": "nested-1",
                    "file_name": "episode-01.mkv",
                    "is_dir": False,
                    "size": 2048,
                }
            ],
        }


class _ManualCache(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def save_search_cache(
        self,
        public_id: str,
        *,
        keyword: str,
        item: dict[str, Any],
        expires_minutes: int,
    ) -> int:
        del keyword, expires_minutes
        raw = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else {}
        self.records[public_id] = {
            "public_id": public_id,
            "title": item.get("title"),
            "source_type": item.get("source_type"),
            "source_url": item.get("url"),
            "password": item.get("password"),
            "raw_data": raw,
        }
        return 1

    def get_search_cache(self, public_id: str) -> dict[str, Any] | None:
        return self.records.get(public_id)


class _CachedDatabase(RepositoryFakeMixin):
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    def get_search_cache(self, public_id: str) -> dict[str, Any] | None:
        return self.record if public_id == self.record.get("public_id") else None


class TianyiPublicFlowTests(unittest.TestCase):
    def test_cloud189_link_is_supported_without_legacy_routes(self) -> None:
        link = detect_link("https://cloud.189.cn/t/AbC123")

        self.assertEqual(link.provider_kind, "tianyi")
        self.assertEqual(link.source_form, "share")
        self.assertEqual(link.search_source_id, "tianyi")
        self.assertTrue(link.supported)

    def test_pansou_tianyi_result_is_available_to_guest_search(self) -> None:
        client = PanSouClient({})

        item = client._row_to_item(
            "Example",
            {
                "url": "https://cloud.189.cn/t/AbC123",
                "title": "Example",
                "password": "4321",
            },
            "tianyi",
        )

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["source_type"], "cloud189")
        self.assertTrue(item["supported"])
        public_item = _public_resource_item(item, public_id="PUBLIC-189")
        self.assertTrue(public_item["supported"])
        self.assertEqual(public_item["source_type"], "cloud189")

    def test_public_selection_is_bounded_deduplicated_and_sanitized(self) -> None:
        files = [
            {
                "fid": f"file-{index}",
                "name": f"File {index}",
                "path": f"/unsafe/{index}",
                "account_id": "attacker",
                "share_id": "forged",
            }
            for index in range(305)
        ]
        files.insert(1, {"fid": "file-0", "name": "duplicate"})
        folders = [
            {"fileId": f"dir-{index}", "fileName": f"Dir {index}", "mount_id": "forged"}
            for index in range(105)
        ]

        selection = _safe_public_tianyi_selection(
            {
                "mode": "items",
                "selected_files": files,
                "selected_folders": folders,
                "account_id": "attacker",
                "target_path": "/etc",
            }
        )

        self.assertEqual(selection["mode"], "items")
        self.assertEqual(len(selection["selected_files"]), 299)
        self.assertEqual(len(selection["selected_folders"]), 100)
        self.assertEqual(selection["selected_files"][0]["fid"], "file-0")
        self.assertEqual(selection["selected_folders"][0]["fid"], "dir-0")
        for item in selection["selected_files"] + selection["selected_folders"]:
            self.assertLessEqual(set(item), {"fid", "name", "type", "is_dir", "path"})
            self.assertNotIn("account_id", item)
            self.assertNotIn("share_id", item)
            self.assertNotIn("mount_id", item)

    def test_search_detail_inspects_tianyi_and_returns_expandable_items(self) -> None:
        importer = _TianyiStub()
        cached = self._cached_resource()

        detail = _public_resource_detail(
            cached,
            quark_importer=object(),
            cloud139_importer=_Cloud139Stub(),
            tianyi_importer=importer,
        )

        self.assertEqual(detail["provider_kind"], "tianyi")
        self.assertEqual(detail["source_form"], "share")
        self.assertEqual(detail["search_source_id"], "tianyi")
        self.assertTrue(detail["supported"])
        self.assertTrue(detail["detail_capability"]["available"])
        self.assertEqual(detail["inspection"]["provider"], "tianyi")
        self.assertTrue(detail["inspection"]["success"])
        self.assertEqual(
            [(item["fid"], item["is_dir"], item["can_expand"]) for item in detail["inspection"]["items"]],
            [("file-1", False, False), ("dir-1", True, True)],
        )
        self.assertEqual(
            importer.check_calls,
            [("https://cloud.189.cn/t/AbC123", "Example", "4321")],
        )

    def test_child_directory_rechecks_share_and_uses_tianyi_share_context(self) -> None:
        importer = _TianyiStub()

        result = _public_resource_child_files(
            self._cached_resource(),
            fid="dir-1",
            quark_importer=object(),
            cloud139_importer=_Cloud139Stub(),
            tianyi_importer=importer,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["items"][0]["fid"], "nested-1")
        self.assertEqual(
            importer.list_calls,
            [
                {
                    "share_id": "share-1",
                    "file_id": "dir-1",
                    "share_mode": 1,
                    "password": "4321",
                }
            ],
        )

    def test_manual_tianyi_link_uses_same_preview_chain(self) -> None:
        cache = _ManualCache()
        importer = _TianyiStub()
        service = PublicManualPreviewService(
            cache=cache,
            new_public_id=lambda: "MANUAL-189",
            detect_link=detect_link,
            resource_detail=_public_resource_detail,
            present_item=_public_resource_item,
            detail_capability=_detail_capability_for_source,
            quark_importer=lambda: object(),
            cloud139_importer=lambda: _Cloud139Stub(),
            sixpan_importer=lambda: None,
            hide_full_links=lambda: True,
            tianyi_importer=lambda: importer,
        )

        result = service.preview(
            url="https://cloud.189.cn/t/AbC123",
            password="4321",
            title="Manual Example",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["public_id"], "MANUAL-189")
        self.assertEqual(result["item"]["source_type"], "cloud189")
        self.assertTrue(result["item"]["supported"])
        self.assertEqual(result["detail"]["inspection"]["provider"], "tianyi")
        self.assertTrue(result["detail"]["inspection"]["success"])
        self.assertEqual(cache.records["MANUAL-189"]["password"], "4321")

    def test_tianyi_preflight_allows_valid_and_rejects_invalid_or_unconfigured_share(self) -> None:
        link = detect_link("https://cloud.189.cn/t/AbC123", password="4321")
        common = {
            "title": "Example",
            "raw": {"size": 1024},
            "quark_importer": object(),
            "cloud139_importer": _Cloud139Stub(),
        }

        allowed = _preflight_public_submission(
            link,
            tianyi_importer=_TianyiStub(),
            **common,
        )
        invalid = _preflight_public_submission(
            link,
            tianyi_importer=_TianyiStub(share_ok=False),
            **common,
        )
        non_dict_failure = _preflight_public_submission(
            link,
            tianyi_importer=_TianyiStub(share_ok=False, share_data="provider denied"),
            **common,
        )
        unconfigured = _preflight_public_submission(
            link,
            tianyi_importer=TianyiImporter({}),
            **common,
        )

        self.assertTrue(allowed["allowed"])
        self.assertTrue(allowed["checked"])
        self.assertEqual(allowed["provider"], "tianyi")
        self.assertFalse(invalid["allowed"])
        self.assertEqual(invalid["inspection"]["status"], "failed")
        self.assertFalse(non_dict_failure["allowed"])
        self.assertEqual(non_dict_failure["inspection"]["provider"], "tianyi")
        self.assertFalse(unconfigured["allowed"])
        self.assertFalse(unconfigured["checked"])
        self.assertEqual(unconfigured["inspection"]["status"], "unconfigured")

    def test_preparation_preserves_sanitized_tianyi_selection_for_cached_search_result(self) -> None:
        cached = self._cached_resource()
        service = PublicSubmissionPreparationService(
            search_cache=lambda public_id: cached if public_id == "PUBLIC-189" else None,
            categories=lambda: {"movie": {"label": "Movie"}},
            category=lambda _key: {"label": "Movie"},
            limited_text=lambda value, *_args, **_kwargs: str(value or ""),
            validate_url=lambda value, _security: str(value or ""),
            safe_string_list=lambda *_args, **_kwargs: [],
            safe_quark_selection=lambda _value: {},
            safe_cloud139_selection=lambda _value: {},
            safe_tianyi_selection=_safe_public_tianyi_selection,
            detect_link=detect_link,
            security_config=lambda: {},
            config_int=lambda _config, _key, default: default,
        )

        prepared = service.prepare(
            {
                "public_id": "PUBLIC-189",
                "category": "movie",
                "tianyi_selection": {
                    "mode": "items",
                    "selected_files": [
                        {
                            "fid": "nested-1",
                            "name": "episode-01.mkv",
                            "account_id": "attacker",
                        }
                    ],
                },
            }
        )

        self.assertTrue(prepared.scoped_selection)
        self.assertEqual(prepared.link.provider_kind, "tianyi")
        self.assertEqual(prepared.link.source_form, "share")
        self.assertEqual(prepared.link.search_source_id, "tianyi")
        self.assertEqual(
            prepared.payload["tianyi_selection"]["selected_files"],
            [
                {
                    "fid": "nested-1",
                    "name": "episode-01.mkv",
                    "type": "file",
                    "is_dir": False,
                }
            ],
        )

    def test_guest_request_snapshot_keeps_only_sanitized_tianyi_selection(self) -> None:
        request = _public_request_payload(
            {
                "public_id": "PUBLIC-189",
                "category": "movie",
                "tianyi_selection": {
                    "mode": "items",
                    "selected_files": [
                        {
                            "fid": "nested-1",
                            "name": "episode-01.mkv",
                            "account_id": "attacker",
                        }
                    ],
                    "destination_id": "forged",
                },
            }
        )

        self.assertEqual(
            request["tianyi_selection"],
            {
                "mode": "items",
                "selected_files": [
                    {
                        "fid": "nested-1",
                        "name": "episode-01.mkv",
                        "type": "file",
                        "is_dir": False,
                    }
                ],
                "selected_folders": [],
            },
        )

    def test_cached_search_record_merges_selection_without_trusting_client_url(self) -> None:
        cached = self._cached_resource()
        database = _CachedDatabase(cached)
        service = ImportJobCreationService(
            database=database,
            config=SimpleNamespace(raw={}),
            source_resolver=object(),
            destination_resolver=object(),
            plan_import=lambda **_kwargs: {},
            validate_plan=lambda *_args, **_kwargs: None,
        )
        selection = {
            "mode": "items",
            "selected_files": [{"fid": "nested-1", "name": "episode-01.mkv"}],
            "selected_folders": [],
        }

        merged = service._cached_record(
            {
                "public_id": "PUBLIC-189",
                "url": "https://attacker.invalid/forged",
                "password": "forged",
                "tianyi_selection": selection,
            }
        )

        self.assertIsNotNone(merged)
        assert merged is not None
        self.assertEqual(merged["source_url"], "https://cloud.189.cn/t/AbC123")
        self.assertEqual(merged["password"], "4321")
        self.assertEqual(merged["raw_data"]["tianyi_selection"], selection)
        snapshot = SelectionSnapshot.from_payload("tianyi", merged)
        self.assertEqual(snapshot.provider_selection["tianyi_selection"], selection)

    def test_importer_accepts_nested_v1_provider_selection_and_expanded_items(self) -> None:
        rows = [
            {
                "file_id": "root-file",
                "file_name": "root.mkv",
                "is_dir": False,
                "md5": "root-md5",
            }
        ]
        selection = {
            "provider_selection": {
                "tianyi_selection": {
                    "selected_files": [
                        {"fid": "root-file", "name": "root.mkv"},
                        {"fid": "nested-file", "name": "episode-01.mkv"},
                    ],
                    "selected_folders": [
                        {"fid": "nested-dir", "name": "Season 01", "is_dir": True}
                    ],
                }
            }
        }

        selected = TianyiImporter._select_items(rows, selection)

        self.assertEqual(
            [item["file_id"] for item in selected],
            ["root-file", "nested-file", "nested-dir"],
        )
        self.assertEqual(selected[0]["md5"], "root-md5")
        self.assertFalse(selected[1]["is_folder"])
        self.assertTrue(selected[2]["is_folder"])

    def test_frontend_contract_exposes_tianyi_preview_selection_and_submission(self) -> None:
        submit = Path("static/public-submit.js").read_text(encoding="utf-8")
        search = Path("static/public-search.js").read_text(encoding="utf-8")
        workbench = Path("static/resource-search-workbench.js").read_text(encoding="utf-8")
        template = Path("templates/submit.html").read_text(encoding="utf-8")
        request_template = Path("templates/request_status.html").read_text(encoding="utf-8")
        admin_template = Path("templates/admin.html").read_text(encoding="utf-8")
        exports = submit[submit.rindex("return Object.freeze({") :]

        for symbol in (
            "tianyiSelectionState",
            "tianyiSelectionSummaryText",
            "renderTianyiFilePreview",
            "renderTianyiFilePreviewItem",
            "renderTianyiFolderChildren",
            "tianyiItemPayloadFromDataset",
            "handleTianyiSelectionChange",
            "tianyiSelectionPayload",
        ):
            self.assertIn(symbol, exports)
        self.assertIn("data-tianyi-select", submit)
        self.assertIn("data-tianyi-open", submit)
        self.assertIn("...tianyiPayload", submit)
        self.assertIn("handleTianyiSelectionChange", search)
        self.assertIn("tianyi_selection", workbench)
        self.assertEqual(template.count("20260809-v1-cutover-v001"), 4)
        self.assertEqual(request_template.count("20260809-v1-cutover-v001"), 4)
        self.assertIn(
            "/static/resource-search-workbench.js?v=20260809-v1-cutover-v001",
            admin_template,
        )

    @staticmethod
    def _cached_resource() -> dict[str, Any]:
        return {
            "public_id": "PUBLIC-189",
            "title": "Example",
            "source_type": "cloud189",
            "source_url": "https://cloud.189.cn/t/AbC123",
            "password": "4321",
            "raw_data": {
                "title": "Example",
                "url": "https://cloud.189.cn/t/AbC123",
                "password": "4321",
                "source_type": "cloud189",
                "size": 3072,
            },
        }


if __name__ == "__main__":
    unittest.main()
