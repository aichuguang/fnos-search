from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

from fnos_media_import.classifiers.link_classifier import detect_link
from fnos_media_import.domain.models import DestinationDescriptor, NativeLocator
from fnos_media_import.importers.quark import QuarkImporter
from fnos_media_import.planning.source_resolver import SourceResolver
from fnos_media_import.providers.adapters import QuarkProviderAdapter
from fnos_media_import.public_web import _public_request_payload, _safe_public_quark_selection, _safe_public_string_list
from fnos_media_import.services.public_submission_preparation_service import (
    PublicSubmissionPreparationError,
    PublicSubmissionPreparationService,
)
from fnos_media_import.services.request_approval_service import RequestApprovalDependencies, RequestApprovalService
from fnos_media_import.services.source_record_service import SourceRecordService
from fnos_media_import.transfer import NativeSaveRequest


class _MemorySourceDatabase:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE source_records (
                source_record_id TEXT PRIMARY KEY, search_source_id TEXT, provider_kind TEXT, source_form TEXT,
                canonical_source_id TEXT, current_revision INTEGER, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE source_record_revisions (
                source_record_id TEXT, revision INTEGER, access_payload_ciphertext BLOB, credential_key_id TEXT,
                selected_items_json TEXT, selected_items_hash TEXT, expected_file_count INTEGER,
                expected_total_bytes INTEGER, created_at TEXT, access_payload_hash TEXT,
                PRIMARY KEY(source_record_id, revision)
            );
        """)

    @contextmanager
    def connect(self):
        with self.connection:
            yield self.connection


class _QuarkHTTP:
    def __init__(self) -> None:
        self.calls = []
        self.saves = []
        self.tokens = []
        self.listings = {
            "0": [
                {"fid": "base-dir", "file_name": "Series", "file_type": 0},
                {"fid": "unselected-root", "file_name": "root-extra.mkv", "file_type": 1, "dir": False},
            ],
            "base-dir": [
                {"fid": "season-a", "file_name": "Season A", "file_type": 0},
                {"fid": "season-b", "file_name": "Season B", "file_type": 0},
                {"fid": "unselected-base", "file_name": "base-extra.mkv", "file_type": 1, "dir": False},
            ],
            "season-a": [
                {"fid": "wanted-a", "file_name": "chosen.mkv", "file_type": 1, "dir": False},
                {"fid": "unselected-a", "file_name": "sibling.mkv", "file_type": 1, "dir": False},
            ],
            "season-b": [
                {"fid": "wanted-b", "file_name": "another.mkv", "file_type": 1, "dir": False},
                {"fid": "unselected-b", "file_name": "sibling.mkv", "file_type": 1, "dir": False},
            ],
        }

    def get_json(self, url, *, params, **_kwargs):
        path = urlparse(url).path
        self.calls.append(("GET", path, copy.deepcopy(params)))
        if path != "/1/clouddrive/share/sharepage/detail":
            raise AssertionError(f"Unexpected local fake GET: {path}")
        if not self.tokens or params.get("stoken") != self.tokens[-1]:
            raise AssertionError("Only the freshly fetched share token may list selected files")
        parent = str(params.get("pdir_fid") or "0")
        rows = copy.deepcopy(self.listings[parent])
        if params.get("_fetch_share") == 1:
            for row in rows:
                row["share_fid_token"] = f"fresh-file:{self.tokens[-1]}:{row['fid']}"
        return 200, {"code": 0, "data": {"list": rows}, "metadata": {"_total": len(rows)}}

    def post_json(self, url, payload, **_kwargs):
        path = urlparse(url).path
        self.calls.append(("POST", path, copy.deepcopy(payload)))
        if path == "/1/clouddrive/share/sharepage/token":
            self.tokens.append(f"fresh-stoken-{len(self.tokens) + 1}")
            return 200, {"code": 0, "data": {"stoken": self.tokens[-1]}}
        if path == "/1/clouddrive/file/info/path_list":
            return 200, {"code": 0, "data": [{"fid": "landing-target"}]}
        if path == "/1/clouddrive/share/sharepage/save":
            self.saves.append(copy.deepcopy(payload))
            return 200, {"code": 0, "data": {"task_id": f"native-task-{len(self.saves)}"}}
        raise AssertionError(f"Unexpected local fake POST: {path}")


class QuarkSelectionPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _MemorySourceDatabase()
        self.addCleanup(self.db.connection.close)
        self.sources = SourceRecordService(self.db, lambda: b"s" * 32)
        self.resolver = SourceResolver(detect_link)
        self.http = _QuarkHTTP()
        self.importer = QuarkImporter({"cookie": "isolated-test-cookie"})
        self.addCleanup(self.importer.http.session.close)
        self.importer.http.get_json = self.http.get_json
        self.importer.http.post_json = self.http.post_json
        self.account_calls = []
        self.adapter = QuarkProviderAdapter(
            importer_factory=self._importer_for_account,
            account_importer_factory=lambda _values: self.importer,
            source_record_service=self.sources,
        )
        self.preparer = PublicSubmissionPreparationService(
            search_cache=lambda _public_id: None,
            categories=lambda: {"tv": {"label": "TV"}},
            category=lambda _key: {"label": "TV"},
            limited_text=lambda value, _label, limit: str(value or "").strip()[:limit],
            validate_url=lambda value, _security: str(value),
            safe_string_list=_safe_public_string_list,
            safe_quark_selection=_safe_public_quark_selection,
            safe_cloud139_selection=lambda _value: {},
            detect_link=detect_link,
            security_config=lambda: {},
            config_int=lambda values, name, default: int(values.get(name, default)),
        )

    def _importer_for_account(self, account_id, revision):
        self.account_calls.append((account_id, revision))
        return self.importer

    @staticmethod
    def _payload():
        return {
            "title": "Series", "url": "https://pan.quark.cn/s/pipeline-share", "password": "1234", "category": "tv",
            "quark_selection": {
                "mode": "subdir_items",
                "base_dir": {"fid": "base-dir", "name": "Series", "file_type": 0, "checked": True},
                "selected_items": [{
                    "fid": "wanted-a", "name": "chosen.mkv", "path": "Season A/chosen.mkv", "file_type": 1, "dir": False,
                    "stoken": "stale-browser-stoken", "fid_token": "stale-browser-file-token", "pdir_fid": "season-a",
                }],
                "stoken": "stale-browser-stoken",
                "selected_dirs": [{"fid": "base-dir", "type": "dir", "is_dir": True}],
                "unknown_selection_override": {"mode": "all", "fid_list": ["unselected-root"]},
            },
        }

    def _execute(self, payload, *, job_id=17, mutate_after_freeze=False):
        prepared = self.preparer.prepare(copy.deepcopy(payload)).payload
        record = {"url": prepared["url"], "password": prepared["password"], "raw_data": prepared}
        source = self.sources.capture(self.resolver.resolve(record), record)
        frozen = self.sources.resolve(source.source_record_id, source.source_record_revision)
        if mutate_after_freeze:
            prepared["quark_selection"]["base_dir"]["fid"] = "0"
            prepared["quark_selection"]["selected_items"] = [{"fid": "unselected-root"}]
        request = NativeSaveRequest(
            idempotency_key=f"landing:job-{job_id}",
            source=replace(source, selection=None),
            destination=DestinationDescriptor("mobile-target", "mobile"),
            locator=NativeLocator("provider_path", "/SourceLanding"),
            account_id="frozen-quark-account", account_revision=7, category_key="tv",
            landing_path=f"/_入库暂存/job-{job_id}",
        )
        with patch.object(self.importer, "materialize_resource", wraps=self.importer.materialize_resource) as materialize:
            receipt = self.adapter.materialize_source(request)
            actual_selection = copy.deepcopy(materialize.call_args.kwargs["selection"])
        return source, frozen, actual_selection, receipt

    def _approve(self, original):
        item = {
            "id": 41, "request_token": "local-request-41", "title": "Series", "category": "tv",
            "source_url": "https://pan.quark.cn/s/pipeline-share", "password": "1234",
            "status": "pending_review", "raw_data": {"request": copy.deepcopy(original)},
        }
        captured = []

        def transition(_request_id, **changes):
            if item["status"] not in changes["expected_statuses"]:
                return False
            item.update(status=changes["status"], raw_data=changes["raw_data"])
            return True

        def coordinate(**kwargs):
            captured.append(copy.deepcopy(kwargs))
            return SimpleNamespace(result={"success": True, "queued": True}, job={}, bind_outcome="queued")

        service = RequestApprovalService(RequestApprovalDependencies(
            requests=SimpleNamespace(get=lambda _request_id: copy.deepcopy(item)),
            commands=SimpleNamespace(transition_with_event=transition),
            jobs=SimpleNamespace(get=lambda _job_id: None),
            coordinate_import=coordinate, public_status=lambda value: value, safe_result=lambda value: value,
            merge_raw_data=lambda original, patch: {**(original or {}), **patch},
            sanitize_request_payload=_public_request_payload, category_label=lambda value: value,
        ))
        body, status = service.approve(41, {}, admin="local-test-admin")
        return body, status, captured

    def test_subdir_scope_is_frozen_and_only_selected_descendant_is_submitted(self):
        payload = self._payload()
        expected = _safe_public_quark_selection(payload["quark_selection"])
        source, frozen, actual, receipt = self._execute(payload, mutate_after_freeze=True)
        self.assertEqual(receipt.status, "submitted", receipt.message)
        self.assertEqual(self.account_calls, [("frozen-quark-account", 7)])
        self.assertEqual(frozen["provider_selection"]["quark_selection"], expected)
        self.assertEqual(actual["mode"], "subdir_items")
        self.assertEqual(actual["base_dir"], expected["base_dir"])
        self.assertEqual(actual["selected_items"], expected["selected_items"])
        self.assertEqual(source.selected_items_hash, frozen["selection"]["selection_hash"])
        self.assertNotIn("stale-browser", json.dumps(frozen))
        self.assertNotIn("selected_dirs", actual)
        self.assertEqual(len(self.http.saves), 1)
        saved = self.http.saves[0]
        self.assertEqual(saved["fid_list"], ["wanted-a"])
        self.assertEqual(saved["fid_token_list"], [f"fresh-file:{self.http.tokens[-1]}:wanted-a"])
        self.assertEqual(saved["stoken"], self.http.tokens[-1])
        self.assertEqual(saved["pdir_fid"], "base-dir")
        self.assertEqual(saved["to_pdir_fid"], "landing-target")

    def test_multiple_descendants_share_one_declared_base_context_without_siblings(self):
        payload = self._payload()
        payload["quark_selection"]["selected_items"].append({
            "fid": "wanted-b", "name": "another.mkv", "path": "Season B/another.mkv", "type": "file",
        })
        _source, _frozen, _actual, receipt = self._execute(payload)
        self.assertEqual(receipt.status, "submitted", receipt.message)
        self.assertEqual(len(self.http.saves), 1)
        self.assertEqual(set(self.http.saves[0]["fid_list"]), {"wanted-a", "wanted-b"})
        self.assertEqual(self.http.saves[0]["pdir_fid"], "base-dir")

    def test_review_approval_reuses_the_same_selection_and_refreshes_share_tokens(self):
        prepared = self.preparer.prepare(self._payload()).payload
        direct_source, _frozen, direct_selection, first = self._execute(prepared)
        body, status, captured = self._approve(_public_request_payload(prepared))
        self.assertTrue(body["success"])
        self.assertEqual(status, 202)
        approved = captured[0]["submit_payload"]
        self.assertEqual(approved["quark_selection"], prepared["quark_selection"])
        approved_source, _frozen, approved_selection, second = self._execute(approved, job_id=18)
        self.assertEqual(first.status, "submitted", first.message)
        self.assertEqual(second.status, "submitted", second.message)
        self.assertEqual(approved_source.selected_items_hash, direct_source.selected_items_hash)
        self.assertEqual(approved_source.source_record_revision, direct_source.source_record_revision)
        self.assertEqual(approved_selection["base_dir"], direct_selection["base_dir"])
        self.assertEqual(approved_selection["selected_items"], direct_selection["selected_items"])
        self.assertNotEqual(self.http.saves[0]["stoken"], self.http.saves[1]["stoken"])
        self.assertTrue(all(saved["fid_list"] == ["wanted-a"] for saved in self.http.saves))

    def test_directory_flags_survive_normalization_without_truthy_false_or_partial_lists(self):
        for directory in ({"file_type": 0}, {"file_type": "folder"}, {"fileType": "0"}, {"is_dir": True}, {"dir": "true"}):
            with self.subTest(directory=directory):
                selection = copy.deepcopy(self._payload()["quark_selection"])
                selection["base_dir"] = {"fid": "base-dir", "name": "Series", **directory}
                selection["selected_items"][0]["dir"] = "false"
                clean = _safe_public_quark_selection(selection)
                self.assertTrue(clean["base_dir"]["is_dir"])
                self.assertFalse(clean["selected_items"][0]["is_dir"])
                self.assertEqual(clean["selected_items"][0]["path"], "Season A/chosen.mkv")
        selection = copy.deepcopy(self._payload()["quark_selection"])
        selection["selected_items"] *= 2
        self.assertEqual(len(_safe_public_quark_selection(selection)["selected_items"]), 1)

    def test_invalid_explicit_selection_is_rejected_at_intake_and_review_not_expanded_to_all(self):
        valid = self._payload()["quark_selection"]
        invalid = [
            {**valid, "selected_items": []},
            {**valid, "selected_items": [valid["selected_items"][0], {}]},
            {**valid, "selected_items": [{"fid": f"item-{index}"} for index in range(201)]},
            {**valid, "selected_items": [{"fid": "x" * 257}]},
            {**valid, "base_dir": {"fid": "file-not-directory", "type": "file"}},
            {"mode": "root_dirs", "selected_dirs": [{"fid": "base-dir", "file_type": 0}, {"fid": "file", "type": "file"}]},
            {"mode": "root_items", "selected_items": [{"fid": "wanted-a"}]},
        ]
        for selection in invalid:
            with self.subTest(selection=selection):
                self.assertEqual(_safe_public_quark_selection(selection), {})
                payload = {**self._payload(), "quark_selection": selection}
                with self.assertRaises(PublicSubmissionPreparationError):
                    self.preparer.prepare(payload)
                body, status, captured = self._approve({"quark_selection": selection})
                self.assertFalse(body["success"])
                self.assertEqual(status, 400)
                self.assertEqual(captured, [])
        self.assertEqual(self.http.calls, [])

    def test_internal_top_level_selection_carriers_cannot_widen_public_selection(self):
        carriers = {
            "native_selection": {"mode": "all"},
            "provider_selection": {"override": {"mode": "all"}},
            "selection": {"mode": "all"},
            "selection_snapshot": {"selected_items": [{"fid": "unselected-root"}]},
            "selected_items": [{"fid": "unselected-root"}],
            "selected_dirs": [{"fid": "base-dir", "type": "dir"}],
        }
        for key, value in carriers.items():
            with self.subTest(key=key):
                payload = {**self._payload(), key: value}
                try:
                    self.preparer.prepare(payload)
                except PublicSubmissionPreparationError:
                    continue
                _source, _frozen, _actual, receipt = self._execute(payload)
                self.assertEqual(receipt.status, "submitted", receipt.message)
                self.assertEqual(self.http.saves[-1]["fid_list"], ["wanted-a"])

    def test_omitted_or_empty_legacy_selection_still_means_not_provided(self):
        for selection in (None, {}):
            with self.subTest(selection=selection):
                payload = {**self._payload(), "quark_selection": selection}
                self.assertEqual(self.preparer.prepare(payload).payload["quark_selection"], {})
                self.assertEqual(_safe_public_quark_selection(selection), {})


if __name__ == "__main__":
    unittest.main()
