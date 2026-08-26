from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from fnos_media_import.constants import JOB_SUBMITTED
from fnos_media_import.importers.base import AdapterCheckResult
from fnos_media_import.importers.quark import QuarkImporter
from fnos_media_import.providers.active import (
    ActiveProviderImporter,
    ActiveProviderUnavailable,
    get_optional_provider_method,
)
from fnos_media_import.providers.adapters import QuarkProviderAdapter
from fnos_media_import.runtime_builder import RuntimeBuilder


class QuarkImporterV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.importer = QuarkImporter({"cookie": "sid=secret"})

    def test_missing_cookie_is_not_ready(self) -> None:
        result = QuarkImporter({}).probe()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unconfigured")

    def test_active_provider_proxy_uses_resolved_account(self) -> None:
        active = QuarkImporter({"cookie": "new-account-cookie"})
        wrapper = ActiveProviderImporter("夸克", lambda: active)

        result = wrapper.check_configuration()

        self.assertTrue(result.ok)
        self.assertEqual(wrapper.cookie, "new-account-cookie")

    def test_resolver_failure_fails_without_fallback(self) -> None:
        def fail_resolution():
            raise RuntimeError("账号凭证解密失败")

        wrapper = ActiveProviderImporter("夸克", fail_resolution)

        with self.assertRaisesRegex(ActiveProviderUnavailable, "账号凭证解密失败"):
            wrapper.check_configuration()
        self.assertFalse(wrapper.configured)

    def test_active_wrapper_recovers_after_resolver_becomes_available(self) -> None:
        active = QuarkImporter({"cookie": "new-account-cookie"})
        calls = 0

        def resolve():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("数据库暂时不可用")
            return active

        wrapper = ActiveProviderImporter("夸克", resolve)

        with self.assertRaises(ActiveProviderUnavailable):
            wrapper.check_configuration()
        self.assertTrue(wrapper.check_configuration().ok)
        self.assertEqual(wrapper.cookie, "new-account-cookie")

    def test_optional_method_lookup_fails_closed_for_inactive_provider(self) -> None:
        def unavailable():
            raise ActiveProviderUnavailable("当前六盘账号未激活")

        wrapper = ActiveProviderImporter("六盘", unavailable)

        self.assertIsNone(get_optional_provider_method(wrapper, "describe"))
        self.assertIsNone(get_optional_provider_method(wrapper, "probe"))

    def test_runtime_builder_binds_resolver_for_replacement_builds(self) -> None:
        active = QuarkImporter({"cookie": "new-account-cookie"})
        builder = RuntimeBuilder(
            object(),
            organizer_repositories=object(),  # type: ignore[arg-type]
            provider_importer_resolver=lambda _provider_kind: active,
        )

        wrapper = builder._build_quark_importer()

        self.assertTrue(wrapper.check_configuration().ok)
        self.assertEqual(wrapper.cookie, "new-account-cookie")

    def test_account_probe_builds_candidate_importer_instead_of_active_wrapper(self) -> None:
        class _SourceRecords:
            @staticmethod
            def resolve(_source_record_id, _revision):
                return {}

        adapter = QuarkProviderAdapter(
            importer_factory=lambda _account_id, _revision: QuarkImporter({}),
            account_importer_factory=QuarkImporter,
            source_record_service=_SourceRecords(),
        )
        with patch.object(
            QuarkImporter,
            "probe",
            autospec=True,
            return_value=AdapterCheckResult(
                True,
                "ready",
                "ok",
                {"identity_hint": "candidate-user"},
            ),
        ) as probe:
            result = adapter.test_account(
                {
                    "config": {"base_url": "https://drive-pc.quark.cn"},
                    "credentials": {"cookie": "candidate-cookie"},
                }
            )

        candidate = probe.call_args.args[0]
        self.assertIsInstance(candidate, QuarkImporter)
        self.assertEqual(candidate.cookie, "candidate-cookie")
        self.assertTrue(result.ok)
        self.assertEqual(result.identity_hint, "candidate-user")

    def test_probe_accepts_quark_string_ok_success_code(self) -> None:
        self.importer.http.get_json = Mock(
            return_value=(200, {"code": "OK", "data": {"nickname": "测试账号"}})
        )

        result = self.importer.probe()

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ready")
        self.assertIn("测试账号", result.message)
        self.assertEqual(result.details["identity_hint"], "测试账号")

    def test_unknown_business_code_still_fails_closed(self) -> None:
        with self.assertRaisesRegex(Exception, "夸克 API code=E_UNKNOWN"):
            self.importer._validate_response(200, {"code": "E_UNKNOWN"})

    def test_list_folders_returns_only_drive_folders(self) -> None:
        self.importer.http.get_json = Mock(
            return_value=(
                200,
                {
                    "code": 0,
                    "data": {
                        "list": [
                            {"fid": "dir-1", "file_name": "电影", "file_type": "folder", "dir": True},
                            {"fid": "file-1", "file_name": "a.mkv", "file_type": "file", "dir": False},
                            {"fid": "dir-2", "file_name": "电视剧", "file_type": "folder"},
                        ]
                    },
                },
            )
        )

        folders = self.importer.list_folders("0")

        self.assertEqual(
            folders,
            [
                {"folder_id": "dir-1", "name": "电影", "parent_folder_id": "0"},
                {"folder_id": "dir-2", "name": "电视剧", "parent_folder_id": "0"},
            ],
        )
        url = self.importer.http.get_json.call_args.args[0]
        self.assertIn("/1/clouddrive/file/sort", url)
        self.assertEqual(self.importer.http.get_json.call_args.kwargs["params"]["pdir_fid"], "0")

    def test_list_folders_requires_cookie(self) -> None:
        importer = QuarkImporter({})
        with self.assertRaisesRegex(Exception, "夸克"):
            importer.list_folders("0")

    def test_list_folders_missing_cookie_and_passes_through_child_parent(self) -> None:
        self.importer.http.get_json = Mock(
            return_value=(
                200,
                {
                    "code": 0,
                    "data": {
                        "list": [
                            {"fid": "dir-9", "file_name": "子目录", "file_type": "folder"},
                        ]
                    },
                },
            )
        )

        folders = self.importer.list_folders("dir-1")

        self.assertEqual(folders[0]["parent_folder_id"], "dir-1")
        self.assertEqual(self.importer.http.get_json.call_args.kwargs["params"]["pdir_fid"], "dir-1")

    def test_check_share_uses_token_then_detail(self) -> None:
        self.importer.http.post_json = Mock(return_value=(200, {"code": 0, "data": {"stoken": "st-1"}}))
        self.importer.http.get_json = Mock(
            return_value=(200, {"code": 0, "data": {"list": [{"fid": "f-1", "share_fid_token": "ft-1"}]}})
        )

        ok, payload = self.importer.check_share("https://pan.quark.cn/s/pwd-1", password="1234")

        self.assertTrue(ok)
        self.assertEqual(payload["pwd_id"], "pwd-1")
        self.assertEqual(payload["stoken"], "st-1")
        token_call = self.importer.http.post_json.call_args
        self.assertIn("/1/clouddrive/share/sharepage/token", token_call.args[0])
        self.assertEqual(token_call.args[1], {"pwd_id": "pwd-1", "passcode": "1234"})
        detail_call = self.importer.http.get_json.call_args
        self.assertIn("/1/clouddrive/share/sharepage/detail", detail_call.args[0])
        self.assertEqual(detail_call.kwargs["params"]["stoken"], "st-1")

    def test_import_resource_submits_native_save_payload(self) -> None:
        self.importer.http.post_json = Mock(
            side_effect=[
                (200, {"code": 0, "data": {"stoken": "st-1"}}),
                (200, {"code": 0, "data": [{"fid": "target-1"}]}),
                (200, {"code": 0, "data": {"task_id": "task-1"}}),
            ]
        )
        self.importer.http.get_json = Mock(
            return_value=(200, {"code": 0, "data": {"list": [{"fid": "f-1", "share_fid_token": "ft-1"}]}})
        )

        result = self.importer.import_resource(
            title="Movie",
            share_url="https://pan.quark.cn/s/pwd-1",
            save_path="/Media",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status, JOB_SUBMITTED)
        self.assertEqual(result.external_task_id, "task-1")
        save_call = self.importer.http.post_json.call_args_list[-1]
        self.assertIn("/1/clouddrive/share/sharepage/save", save_call.args[0])
        self.assertEqual(save_call.args[1]["fid_list"], ["f-1"])
        self.assertEqual(save_call.args[1]["fid_token_list"], ["ft-1"])
        self.assertEqual(save_call.args[1]["to_pdir_fid"], "target-1")

    def test_poll_status_two_is_completed(self) -> None:
        self.importer.http.get_json = Mock(return_value=(200, {"status": 200, "data": {"status": 2}}))
        receipt = type("Receipt", (), {"external_ref": "task-1", "operation_id": "task-1"})()
        self.assertEqual(self.importer.poll_operation(receipt)["status"], "completed")

    def test_runtime_puus_cookie_is_merged_and_persisted(self) -> None:
        updates: list[dict[str, str]] = []
        importer = QuarkImporter(
            {"cookie": "sid=secret; __puus=old; kps=value"},
            cookie_update_callback=updates.append,
        )
        response = requests.Response()
        response.cookies.set("__puus", "rotated")

        returned = importer._capture_runtime_cookie(response)

        self.assertIs(returned, response)
        self.assertEqual(importer.cookie, "sid=secret; __puus=rotated; kps=value")
        self.assertEqual(importer.http.session.headers["Cookie"], importer.cookie)
        self.assertEqual(updates, [{"cookie": importer.cookie}])

        importer._capture_runtime_cookie(response)
        self.assertEqual(len(updates), 1)

    def test_runtime_puus_falls_back_to_set_cookie_header(self) -> None:
        importer = QuarkImporter({"cookie": "sid=secret"})
        response = requests.Response()
        response.headers["Set-Cookie"] = "foo=bar; Path=/, __puus=from-header; Path=/"

        importer._capture_runtime_cookie(response)

        self.assertEqual(importer.cookie, "sid=secret; __puus=from-header")

    def test_cookie_persistence_failure_does_not_fail_provider_response(self) -> None:
        def fail_persistence(_credentials: dict[str, str]) -> None:
            raise RuntimeError("database unavailable")

        importer = QuarkImporter(
            {"cookie": "sid=secret"},
            cookie_update_callback=fail_persistence,
        )
        response = requests.Response()
        response.cookies.set("__puus", "rotated")

        self.assertIs(importer._capture_runtime_cookie(response), response)
        self.assertIn("__puus=rotated", importer.cookie)
        self.assertEqual(importer.cookie_update_error, "database unavailable")

        updates: list[dict[str, str]] = []
        importer.cookie_update_callback = updates.append
        importer._capture_runtime_cookie(requests.Response())
        self.assertEqual(updates, [{"cookie": importer.cookie}])
        self.assertEqual(importer.cookie_update_error, "")

    def test_expired_root_session_requires_new_qr_login(self) -> None:
        with self.assertRaisesRegex(Exception, "重新扫码登录"):
            self.importer._validate_response(200, {"code": 40001})

    def test_expired_share_code_is_not_reported_as_cookie_expired(self) -> None:
        # 10000 是路径双关码：/share/ 下是分享失效，其他路径才是 Cookie 失效。
        # 混为一谈会把管理员支去重新扫码，而账号本来就是好的。
        with self.assertRaisesRegex(Exception, "分享不存在或已失效"):
            self.importer._validate_response(
                200,
                {"code": 10000},
                path="/1/clouddrive/share/sharepage/detail",
            )

    def test_non_share_path_still_treats_10000_as_cookie_expired(self) -> None:
        with self.assertRaisesRegex(Exception, "重新扫码登录"):
            self.importer._validate_response(
                200,
                {"code": 10000},
                path="/account/info",
            )

    def test_share_specific_codes_are_reported_as_share_failures(self) -> None:
        for code in (41001, "41001", 41002, 41003):
            with self.subTest(code=code):
                with self.assertRaisesRegex(Exception, "分享不存在或已失效"):
                    self.importer._validate_response(200, {"code": code})


if __name__ == "__main__":
    unittest.main()
