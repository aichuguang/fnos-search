from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock

from fnos_media_import.importers.quark import QuarkImporter


def _row(fid, name, parent, *, directory=False, token=None):
    return {
        "fid": fid, "file_name": name, "pdir_fid": parent,
        "file_type": 0 if directory else 1,
        "share_fid_token": token or f"token-{fid}",
    }


class QuarkNestedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.importer = QuarkImporter({"cookie": "test-cookie"})
        self.tree = {
            "0": [_row("base", "Collection", "0", directory=True), _row("root-extra", "extra.mkv", "0")],
            "base": [
                _row("season-1", "Season 1", "base", directory=True),
                _row("season-2", "Season 2", "base", directory=True),
                _row("direct", "direct.mkv", "base"),
            ],
            "season-1": [_row("file-1", "episode1.mkv", "season-1"), _row("file-2", "episode2.mkv", "season-1")],
            "season-2": [_row("file-3", "episode3.mkv", "season-2")],
        }
        self.fail_parent = None
        self.importer.http.get_json = Mock(side_effect=self._get_json)
        self.importer.http.post_json = Mock(side_effect=self._post_json)

    def _get_json(self, _url, **kwargs):
        parent = str(kwargs["params"]["pdir_fid"])
        if parent == self.fail_parent:
            return 200, {"code": "E_LIST_FAILURE", "message": "Synthetic directory read failed"}
        # Real Quark successes have code=0/status=200, not success=True.
        rows = copy.deepcopy(self.tree.get(parent, []))
        return 200, {"status": 200, "code": 0, "data": {"list": rows}, "metadata": {"_total": len(rows)}}

    @staticmethod
    def _post_json(url, _payload, **_kwargs):
        if url.endswith("/share/sharepage/token"):
            return 200, {"code": 0, "data": {"stoken": "test-stoken"}}
        if url.endswith("/file/info/path_list"):
            return 200, {"code": 0, "data": [{"fid": "target-fid"}]}
        if url.endswith("/share/sharepage/save"):
            return 200, {"code": 0, "data": {"task_id": "native-task-17"}}
        raise AssertionError(f"Unexpected write endpoint: {url}")

    @staticmethod
    def _selection(*items):
        return {
            "mode": "subdir_items",
            "base_dir": {"fid": "base", "name": "Collection", "is_dir": True},
            "selected_items": list(items),
        }

    def _import(self, selection):
        return self.importer.import_resource(
            title="Selected media", share_url="https://pan.quark.cn/s/test-share",
            save_path="/controlled/job-17", quark_selection=selection,
        )

    def _save_payloads(self):
        return [call.args[1] for call in self.importer.http.post_json.call_args_list if call.args[0].endswith("/share/sharepage/save")]

    def _assert_no_destination_or_save(self, result, *, selection_error=True):
        self.assertFalse(result.success)
        if selection_error:
            self.assertEqual(result.raw_data["error_code"], "SHARE_INVALID")
        self.assertEqual(self._save_payloads(), [])
        self.assertTrue(all(call.args[0].endswith("/share/sharepage/token") for call in self.importer.http.post_json.call_args_list))

    def test_real_code_zero_payload_resolves_nested_file_without_success_boolean(self):
        result = self._import(self._selection({"fid": "file-1", "path": "Season 1/episode1.mkv"}))
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.external_task_id, "native-task-17")
        self.assertEqual(len(self._save_payloads()), 1)
        payload = self._save_payloads()[0]
        self.assertEqual(payload["fid_list"], ["file-1"])
        self.assertEqual(payload["fid_token_list"], ["token-file-1"])
        self.assertEqual(payload["pdir_fid"], "base")
        self.assertNotIn("base", payload["fid_list"])
        self.assertNotIn("root-extra", payload["fid_list"])

    def test_numeric_zero_directory_flag_survives_normalization(self):
        for key in ("file_type", "fileType", "type"):
            with self.subTest(key=key):
                self.assertTrue(self.importer._file_item({"fid": "base", key: 0})["is_dir"])
        result = self._import({"mode": "root_dirs", "selected_dirs": [{"fid": "base", "is_dir": True}]})
        self.assertTrue(result.success, result.message)
        self.assertEqual(self._save_payloads()[0]["fid_list"], ["base"])
        self.assertEqual(self._save_payloads()[0]["pdir_fid"], "0")

    def test_child_listing_requests_share_context_and_uses_fresh_provider_token(self):
        result = self._import(self._selection({
            "fid": "file-1", "path": "Season 1/episode1.mkv",
            "fid_token": "stale-browser-token", "share_fid_token": "another-browser-token",
        }))
        self.assertTrue(result.success, result.message)
        child_requests = [
            call.kwargs["params"] for call in self.importer.http.get_json.call_args_list
            if call.kwargs["params"]["pdir_fid"] != "0"
        ]
        self.assertEqual([item["pdir_fid"] for item in child_requests], ["base", "season-1"])
        self.assertTrue(all(item["_fetch_share"] == 1 for item in child_requests))
        self.assertEqual(self._save_payloads()[0]["fid_token_list"], ["token-file-1"])
        self.assertEqual(self._save_payloads()[0]["pdir_fid"], "base")

    def test_direct_child_uses_selected_parent_not_share_root(self):
        result = self._import(self._selection({"fid": "direct", "path": "direct.mkv"}))
        self.assertTrue(result.success, result.message)
        self.assertEqual(self._save_payloads()[0]["pdir_fid"], "base")
        self.assertEqual(self._save_payloads()[0]["fid_list"], ["direct"])

    def test_multi_level_selection_keeps_one_save_and_selected_subtree_context(self):
        result = self._import(self._selection(
            {"fid": "file-1", "path": "Season 1/episode1.mkv"},
            {"fid": "file-3", "path": "Season 2/episode3.mkv"},
        ))
        self.assertTrue(result.success, result.message)
        self.assertEqual(len(self._save_payloads()), 1)
        payload = self._save_payloads()[0]
        self.assertEqual(payload["pdir_fid"], "base")
        self.assertEqual(payload["fid_list"], ["file-1", "file-3"])
        self.assertEqual(payload["fid_token_list"], ["token-file-1", "token-file-3"])

    def test_repeated_parent_paths_reuse_only_this_selection_attempt_listing(self):
        result = self._import(self._selection(
            {"fid": "file-1", "path": "Season 1/episode1.mkv"},
            {"fid": "file-2", "path": "Season 1/episode2.mkv"},
        ))
        self.assertTrue(result.success, result.message)
        parents = [call.kwargs["params"]["pdir_fid"] for call in self.importer.http.get_json.call_args_list]
        self.assertEqual(parents, ["0", "base", "season-1"])

    def test_same_name_replacement_with_new_fid_is_not_transferred(self):
        self.tree["season-1"][0] = _row("replacement", "episode1.mkv", "season-1")
        result = self._import(self._selection({"fid": "file-1", "path": "Season 1/episode1.mkv"}))
        self._assert_no_destination_or_save(result)
        self.assertIn("文件 ID", result.message)

    def test_one_missing_explicit_item_rejects_whole_selection_not_subset(self):
        result = self._import(self._selection(
            {"fid": "file-1", "path": "Season 1/episode1.mkv"},
            {"fid": "missing", "path": "Season 1/missing.mkv"},
        ))
        self._assert_no_destination_or_save(result)
        self.assertIn("缺失", result.message)

    def test_one_missing_root_id_cannot_fall_back_to_remaining_root_directories(self):
        result = self._import({"mode": "root_dirs", "selected_dirs": [{"fid": "base"}, {"fid": "missing"}]})
        self._assert_no_destination_or_save(result)
        self.assertIn("ID 未匹配", result.message)

    def test_missing_token_never_borrows_a_descendant_token_or_returns_subset(self):
        self.tree["season-1"][1].pop("share_fid_token")
        self.tree["season-1"][1]["children"] = [{"share_fid_token": "unrelated-token"}]
        result = self._import(self._selection(
            {"fid": "file-1", "path": "Season 1/episode1.mkv"},
            {"fid": "file-2", "path": "Season 1/episode2.mkv"},
        ))
        self._assert_no_destination_or_save(result)
        self.assertIn("转存令牌缺失", result.message)

    def test_nested_listing_error_is_not_misreported_as_empty_selection(self):
        self.fail_parent = "season-1"
        result = self._import(self._selection({"fid": "file-1", "path": "Season 1/episode1.mkv"}))
        self._assert_no_destination_or_save(result, selection_error=False)
        self.assertIn("子目录读取失败", result.message)
        self.assertIn("Synthetic directory read failed", result.message)
        self.assertNotIn("分享中没有可转存", result.message)

    def test_explicit_empty_malformed_or_unknown_selection_never_becomes_all(self):
        selections = (
            {"mode": "root_dirs", "selected_dirs": []},
            {"mode": "root_items", "selected_items": []},
            self._selection(),
            self._selection({"name": "missing-id"}),
            {"mode": "unknown", "selected_dirs": [{"fid": "base"}]},
            {"selected_items": [{"fid": "root-extra"}]},
            [],
        )
        for selection in selections:
            with self.subTest(selection=selection):
                self.importer.http.post_json.reset_mock()
                self._assert_no_destination_or_save(self._import(selection))

    def test_path_traversal_and_non_directory_intermediate_stop_before_save(self):
        result = self._import(self._selection({"fid": "file-1", "path": "../Season 1/episode1.mkv"}))
        self._assert_no_destination_or_save(result)
        self.tree["base"][0]["file_type"] = 1
        result = self._import(self._selection({"fid": "file-1", "path": "Season 1/episode1.mkv"}))
        self._assert_no_destination_or_save(result)

    def test_share_scope_parent_is_not_replaced_by_owner_drive_metadata(self):
        self.tree["0"][0]["pdir_fid"] = "owner-drive-folder"
        result = self._import({"mode": "root_dirs", "selected_dirs": [{"fid": "base"}]})
        self.assertTrue(result.success, result.message)
        self.assertEqual(self._save_payloads()[0]["pdir_fid"], "0")
        self.assertEqual(self._save_payloads()[0]["fid_list"], ["base"])

    def test_token_and_fid_aliases_keep_exact_selected_identity(self):
        self.tree["season-1"][0] = {
            "fileId": "file-1", "fileName": "episode1.mkv", "fileType": 1,
            "parentId": "season-1", "fidToken": "alias-token",
        }
        result = self._import(self._selection({"fileId": "file-1", "path": "Season 1/episode1.mkv"}))
        self.assertTrue(result.success, result.message)
        self.assertEqual(self._save_payloads()[0]["fid_token_list"], ["alias-token"])
        self.assertEqual(self._save_payloads()[0]["fid_list"], ["file-1"])

    def test_materialization_keeps_nested_selection_and_frozen_landing_path(self):
        result = self.importer.materialize_resource(
            title="Selected media", share_url="https://pan.quark.cn/s/test-share",
            root_locator={"kind": "path", "value": "/source-root"}, landing_path="/ingress/job-17",
            selection=self._selection({"fid": "file-1", "path": "Season 1/episode1.mkv"}),
        )
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.target_path, "/source-root/ingress/job-17")
        self.assertEqual(self._save_payloads()[0]["fid_list"], ["file-1"])
        self.assertEqual(self._save_payloads()[0]["pdir_fid"], "base")


if __name__ == "__main__":
    unittest.main()
