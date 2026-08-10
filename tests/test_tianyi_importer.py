from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import call, patch

from fnos_media_import.constants import JOB_DONE, JOB_SUBMITTED
from fnos_media_import.importers.tianyi import (
    TIANYI_SESSION_IP_MISMATCH_MESSAGE,
    TianyiApiError,
    TianyiImporter,
)
from fnos_media_import.transfer import OperationReceipt


def _item(file_id: str, name: str, *, folder: bool = False, md5: str = "") -> dict[str, object]:
    return {
        "id": file_id,
        "file_id": file_id,
        "name": name,
        "file_name": name,
        "is_folder": folder,
        "is_dir": folder,
        "md5": md5,
        "size": 1,
    }


class TianyiImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        TianyiImporter._auth_cache.clear()
        self.importer = TianyiImporter({"cookie": "SSON=test-cookie", "identity": "user***1"})

    def test_auth_cache_key_isolated_by_password_and_endpoint(self) -> None:
        first = TianyiImporter(
            {
                "username": "same-user",
                "password": "password-a",
                "api_url": "https://api-a.example.test",
            }
        )
        second = TianyiImporter(
            {
                "username": "same-user",
                "password": "password-b",
                "api_url": "https://api-a.example.test",
            }
        )
        third = TianyiImporter(
            {
                "username": "same-user",
                "password": "password-a",
                "api_url": "https://api-b.example.test",
            }
        )
        self.assertNotEqual(first._auth_cache_key(), second._auth_cache_key())
        self.assertNotEqual(first._auth_cache_key(), third._auth_cache_key())

    def test_auth_cache_key_isolated_by_local_account_namespace(self) -> None:
        credentials = {
            "username": "same-user",
            "password": "same-password",
        }
        first = TianyiImporter(
            credentials,
            cache_namespace="account:acc-a",
        )
        second = TianyiImporter(
            credentials,
            cache_namespace="account:acc-b",
        )

        self.assertNotEqual(first._auth_cache_key(), second._auth_cache_key())

    def test_auth_cache_write_prunes_expired_entries(self) -> None:
        TianyiImporter._auth_cache["expired"] = (time.monotonic() - 1, {"sessionKey": "old"})
        self.importer._cache_auth("current", {"sessionKey": "new"})
        self.assertNotIn("expired", TianyiImporter._auth_cache)
        self.assertEqual(TianyiImporter._auth_cache["current"][1]["sessionKey"], "new")

    def test_probe_uses_operational_file_api_instead_of_portal_capacity_gate(self) -> None:
        importer = TianyiImporter(
            {
                "username": "18900000000",
                "session_key": "session-1",
            }
        )
        with patch.object(importer, "_get", return_value={"res_code": 0}) as get:
            result = importer.probe()

        self.assertTrue(result.ok)
        self.assertEqual(result.details["identity_hint"], "18900000000")
        get.assert_called_once_with(
            "/listFiles.action",
            folderId="-11",
            fileType=0,
            mediaAttr=0,
            orderBy="lastOpTime",
            descending=True,
            pageNum=1,
            pageSize=1,
            iconOption=5,
            recursive=0,
        )

    def test_normalize_item_detects_cloud189_folder_without_is_folder(self) -> None:
        importer = TianyiImporter({})
        folder = importer._normalize_item({"id": 0, "parentId": "-11", "name": "同步盘"})
        self.assertTrue(folder["is_folder"])
        self.assertEqual(folder["file_id"], "0")

        file_item = importer._normalize_item(
            {"fileId": "abc", "name": "a.mkv", "isFolder": False}
        )
        self.assertFalse(file_item["is_folder"])
        self.assertEqual(file_item["file_id"], "abc")

    def test_list_drive_folders_uses_pc_contract(self) -> None:
        importer = TianyiImporter({"session_key": "session-1", "session_secret": "secret-1"})
        with patch.object(
            importer,
            "_get",
            return_value={
                "res_code": 0,
                "fileListAO": {
                    "fileList": [{"id": "file-1", "name": "a.mkv", "isFolder": False}],
                    "folderList": [
                        {"id": "dir-1", "name": "电影", "isFolder": True},
                        {"id": "dir-2", "name": "电视剧", "folderId": "dir-2"},
                    ],
                },
            },
        ) as get:
            folders = importer.list_drive_folders("-11")

        self.assertEqual(
            folders,
            [
                {"folder_id": "dir-1", "name": "电影", "parent_folder_id": "-11"},
                {"folder_id": "dir-2", "name": "电视剧", "parent_folder_id": "-11"},
            ],
        )
        get.assert_called_once_with(
            "/listFiles.action",
            folderId="-11",
            fileType=0,
            mediaAttr=0,
            orderBy="lastOpTime",
            descending=True,
            pageNum=1,
            pageSize=1000,
            iconOption=5,
            recursive=0,
        )

    def test_list_drive_items_raises_when_response_lacks_file_list_ao(self) -> None:
        importer = TianyiImporter({"session_key": "session-1", "session_secret": "secret-1"})
        with patch.object(importer, "_get", return_value={"res_code": 0}):
            with self.assertRaisesRegex(Exception, "fileListAO"):
                importer._list_drive_items("-11")

    def test_is_auth_rejection_distinguishes_business_403(self) -> None:
        self.assertTrue(TianyiImporter._is_auth_rejection("InvalidSessionKey", "check ip error"))
        self.assertTrue(TianyiImporter._is_auth_rejection("", "usersessionboisnull"))
        self.assertFalse(
            TianyiImporter._is_auth_rejection(
                "InsufficientStorageSpace", "user free space has been used up"
            )
        )
        self.assertFalse(
            TianyiImporter._is_auth_rejection("InvalidPartSize", "invalid part size")
        )

    def test_clean_cloud189_message_strips_noise_and_maps_quota(self) -> None:
        self.assertEqual(
            TianyiImporter._clean_cloud189_message(
                "sessionKey=abc-123,user free space has been used up,requestId=xyz"
            ),
            "天翼云盘免费空间已用尽，请先清理空间或升级容量后再试",
        )
        self.assertEqual(
            TianyiImporter._clean_cloud189_message("sessionKey=abc,invalid part Size,requestId=x"),
            "天翼云上传分片参数无效",
        )
        self.assertEqual(
            TianyiImporter._clean_cloud189_message("some other error"),
            "some other error",
        )

    def test_upload_403_business_error_is_not_mislabeled_as_auth(self) -> None:
        class Response:
            status_code = 403

            def __init__(self) -> None:
                self.text = json.dumps(
                    {
                        "code": "InsufficientStorageSpace",
                        "msg": "sessionKey=s,user free space has been used up,requestId=r",
                    }
                )

            def json(self) -> dict[str, object]:
                return {
                    "code": "InsufficientStorageSpace",
                    "msg": "sessionKey=s,user free space has been used up,requestId=r",
                }

        importer = TianyiImporter(
            {"session_key": "session-1", "session_secret": "0123456789abcdef0123456789abcdef"}
        )
        with patch.object(importer.http.session, "request", return_value=Response()):
            with self.assertRaises(TianyiApiError) as raised:
                importer._upload_request(
                    "GET",
                    "/person/initMultiUpload",
                    params={"parentFolderId": "0", "fileName": "x.bin"},
                )

        self.assertEqual(raised.exception.code, "InsufficientStorageSpace")
        self.assertIn("空间已用尽", str(raised.exception))

    def test_share_inspection_uses_v2_contract_and_normalizes_items(self) -> None:
        with patch.object(
            self.importer,
            "_web_get",
            side_effect=[
                {
                    "shareId": "share-1",
                    "fileId": "root-1",
                    "fileName": "Example",
                    "shareMode": 0,
                    "isFolder": True,
                },
                {
                    "fileListAO": {
                        "fileList": [{"id": "file-1", "name": "a.mkv", "isFolder": False, "md5": "abc"}],
                        "folderList": [{"id": "dir-1", "name": "Season 01", "isFolder": True}],
                    }
                },
            ],
        ) as get:
            ok, share = self.importer.check_share("https://cloud.189.cn/t/AbC123")

        self.assertTrue(ok)
        self.assertEqual(share["share_id"], "share-1")
        self.assertEqual([item["file_id"] for item in share["items"]], ["file-1", "dir-1"])
        self.assertEqual(
            get.call_args_list[0],
            call("/api/open/share/getShareInfoByCodeV2.action", shareCode="AbC123"),
        )
        self.assertEqual(get.call_args_list[1].kwargs["shareMode"], 0)
        self.assertTrue(get.call_args_list[1].kwargs["isFolder"])

    def test_private_share_requires_and_validates_access_code(self) -> None:
        private_info = {
            "fileId": "root-1",
            "fileName": "Private",
            "shareMode": 1,
            "isFolder": True,
        }
        with patch.object(self.importer, "_web_get", return_value=private_info):
            ok, failure = self.importer.check_share("https://cloud.189.cn/t/Secret")
        self.assertFalse(ok)
        self.assertEqual(failure["error_code"], "ACCESS_CODE_REQUIRED")

        with patch.object(
            self.importer,
            "_web_get",
            side_effect=[
                private_info,
                {"shareId": "private-share"},
                {"fileListAO": {"fileList": [], "folderList": []}},
            ],
        ) as get:
            ok, share = self.importer.check_share(
                "https://cloud.189.cn/t/Secret",
                password="4321",
            )
        self.assertTrue(ok)
        self.assertEqual(share["share_id"], "private-share")
        self.assertEqual(get.call_args_list[1].kwargs["accessCode"], "4321")
        self.assertTrue(get.call_args_list[1].kwargs["uuid"])

    def test_folder_transfer_creates_resource_folder_and_batch_task(self) -> None:
        share = {
            "share_id": "share-1",
            "is_folder": True,
            "file_name": "Original",
            "items": [
                _item("file-1", "a.mkv", md5="md5-a"),
                _item("dir-1", "Season 01", folder=True),
            ],
        }
        with (
            patch.object(self.importer, "check_share", return_value=(True, share)),
            patch.object(self.importer, "_ensure_child_folder", return_value="resource-folder") as ensure,
            patch.object(self.importer, "_post", return_value={"taskId": "task-1"}) as post,
        ):
            result = self.importer.import_resource(
                "Example Show",
                "https://cloud.189.cn/t/AbC123",
                "target-folder",
            )

        self.assertTrue(result.success)
        self.assertEqual(result.status, JOB_SUBMITTED)
        self.assertEqual(result.external_task_id, "task-1")
        self.assertEqual(result.target_path, "resource-folder")
        ensure.assert_called_once_with("target-folder", "Example Show")
        self.assertEqual(post.call_args.args, ("/batch/createBatchTask.action",))
        form = post.call_args.kwargs["form"]
        self.assertEqual(form["type"], "SHARE_SAVE")
        self.assertEqual(form["targetFolderId"], "resource-folder")
        self.assertEqual(form["shareId"], "share-1")
        self.assertEqual(
            json.loads(form["taskInfos"]),
            [
                {"fileId": "file-1", "fileName": "a.mkv", "isFolder": 0, "md5": "md5-a"},
                {"fileId": "dir-1", "fileName": "Season 01", "isFolder": 1},
            ],
        )

    def test_selected_items_are_the_only_batch_entries(self) -> None:
        share = {
            "share_id": "share-1",
            "is_folder": False,
            "items": [_item("file-1", "a.mkv"), _item("file-2", "b.mkv")],
        }
        with (
            patch.object(self.importer, "check_share", return_value=(True, share)),
            patch.object(self.importer, "_post", return_value={"taskId": "task-2"}) as post,
        ):
            result = self.importer.import_resource(
                "Example",
                "https://cloud.189.cn/t/AbC123",
                "target-folder",
                native_selection={"selected_items": [{"file_id": "file-2"}]},
            )

        self.assertTrue(result.success)
        self.assertEqual(result.target_path, "target-folder")
        task_infos = json.loads(post.call_args.kwargs["form"]["taskInfos"])
        self.assertEqual([item["fileId"] for item in task_infos], ["file-2"])

    def test_empty_folder_is_materialized_without_a_batch_task(self) -> None:
        share = {"share_id": "share-1", "is_folder": True, "file_name": "Empty", "items": []}
        with (
            patch.object(self.importer, "check_share", return_value=(True, share)),
            patch.object(self.importer, "_ensure_child_folder", return_value="empty-folder"),
            patch.object(self.importer, "_post") as post,
        ):
            result = self.importer.import_resource(
                "Empty",
                "https://cloud.189.cn/t/AbC123",
                "target-folder",
            )
        self.assertTrue(result.success)
        self.assertEqual(result.status, JOB_DONE)
        self.assertTrue(result.raw_data["empty_share"])
        post.assert_not_called()

    def test_materialize_creates_task_scoped_landing_before_save(self) -> None:
        with (
            patch.object(self.importer, "_ensure_folder_path", return_value="landing-folder") as ensure,
            patch.object(self.importer, "import_resource") as save,
        ):
            save.return_value = type(
                "Result",
                (),
                {"success": True, "target_path": "resource-folder", "raw_data": {}},
            )()
            result = self.importer.materialize_resource(
                title="Example",
                share_url="https://cloud.189.cn/t/AbC123",
                root_locator={"kind": "folder_id", "value": "root-folder"},
                landing_path="/_fnos_ingress/job-7",
                selection={"selected_items": [{"file_id": "file-1"}]},
            )

        ensure.assert_called_once_with("root-folder", "/_fnos_ingress/job-7")
        self.assertEqual(save.call_args.kwargs["save_path"], "landing-folder")
        self.assertEqual(save.call_args.kwargs["native_selection"]["selected_items"][0]["file_id"], "file-1")
        self.assertEqual(result.raw_data["landing_folder_id"], "landing-folder")

    def test_poll_resolves_conflicts_and_maps_documented_states(self) -> None:
        receipt = OperationReceipt(
            operation_id="tianyi:task-1",
            status="running",
            provider_kind="tianyi",
            external_ref="task-1",
        )
        with (
            patch.object(self.importer, "_check_batch_task", return_value={"taskId": "task-1", "taskStatus": 2}),
            patch.object(self.importer, "_resolve_conflicts") as conflicts,
        ):
            status = self.importer.poll_operation(receipt)
        self.assertEqual(status["status"], "running")
        conflicts.assert_called_once_with("task-1")

        with patch.object(self.importer, "_check_batch_task", return_value={"taskStatus": 4}):
            self.assertEqual(self.importer.poll_operation(receipt)["status"], "completed")
        with patch.object(self.importer, "_check_batch_task", return_value={"taskStatus": 5, "res_msg": "failed"}):
            self.assertEqual(self.importer.poll_operation(receipt)["status"], "failed")

    def test_conflict_resolution_uses_manage_batch_form_contract(self) -> None:
        with patch.object(
            self.importer,
            "_post",
            side_effect=[
                {"targetFolderId": "target", "taskInfos": [{"fileId": "file-1", "dealWay": 0}]},
                {},
            ],
        ) as post:
            self.importer._resolve_conflicts("task-1")
        self.assertEqual(post.call_args_list[0].kwargs["form"]["type"], "SHARE_SAVE")
        managed = post.call_args_list[1].kwargs["form"]
        self.assertEqual(managed["targetFolderId"], "target")
        self.assertEqual(json.loads(managed["taskInfos"])[0]["dealWay"], 1)

    def test_verify_checks_expected_names_in_target_folder(self) -> None:
        receipt = OperationReceipt(
            operation_id="tianyi:task-1",
            status="running",
            provider_kind="tianyi",
            external_ref="task-1",
            metadata={
                "target_path": "target-folder",
                "raw": {"expected_names": ["a.mkv", "Season 01"]},
            },
        )
        with (
            patch.object(self.importer, "poll_operation", return_value={"status": "completed"}),
            patch.object(
                self.importer,
                "_list_drive_items",
                return_value=[_item("1", "a.mkv"), _item("2", "Season 01", folder=True)],
            ),
        ):
            from fnos_media_import.transfer.verifier import VerifyRequest

            verified = self.importer.verify_native_result(
                VerifyRequest(operation=receipt)
            )
        self.assertEqual(verified["status"], "ok")

    def test_share_code_supports_reference_project_url_formats(self) -> None:
        cases = {
            "https://cloud.189.cn/t/AbC123": "AbC123",
            "https://cloud.189.cn/web/share?code=Web123": "Web123",
            "https://h5.cloud.189.cn/share.html#/t/H5Code": "H5Code",
            "https://content.21cn.com/path#/view?shareCode=Feed123": "Feed123",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(self.importer._share_code(url), expected)

    def test_signature_matches_cloud189_sdk_sorting_and_js_scalars(self) -> None:
        values = {
            "taskInfos": [{"fileId": "1"}, {"fileId": "2"}],
            "type": "SHARE_SAVE",
            "Timestamp": "123",
            "AppKey": "600100422",
        }
        source = "AppKey=600100422&Timestamp=123&taskInfos=[object Object],[object Object]&type=SHARE_SAVE"
        self.assertEqual(self.importer._signature(values), hashlib.md5(source.encode()).hexdigest())

    def test_request_uses_189cloudpc_hmac_signature_contract(self) -> None:
        response = type(
            "Response",
            (),
            {
                "status_code": 200,
                "text": '{"res_code": 0}',
                "json": lambda self: {"res_code": 0},
            },
        )()
        importer = TianyiImporter(
            {"session_key": "session-1", "session_secret": "secret-1"}
        )
        with (
            patch.object(importer.http.session, "request", return_value=response) as request,
            patch.object(
                importer,
                "_pc_client_suffix",
                return_value={
                    "clientType": "TELEPC",
                    "version": "6.2",
                    "channelId": "web_cloud.189.cn",
                    "rand": "1_2",
                },
            ),
            patch.object(
                importer,
                "_pc_signature_headers",
                return_value={
                    "Date": "Sun, 09 Aug 2026 02:02:23 GMT",
                    "SessionKey": "session-1",
                    "X-Request-ID": "request-1",
                    "Signature": "SIGNED",
                },
            ) as signature,
        ):
            importer._get("/listFiles.action", folderId="-11", descending=True)

        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["params"]["clientType"], "TELEPC")
        self.assertEqual(kwargs["params"]["version"], "6.2")
        self.assertEqual(kwargs["params"]["descending"], "true")
        self.assertNotIn("sessionKey", kwargs["params"])
        self.assertEqual(kwargs["headers"]["SessionKey"], "session-1")
        self.assertEqual(kwargs["headers"]["Signature"], "SIGNED")
        signature.assert_called_once_with(
            "https://api.cloud.189.cn/listFiles.action",
            "GET",
            session_key="session-1",
            session_secret="secret-1",
        )

    def test_pc_signature_is_uppercase_hmac_sha1_of_openlist_source(self) -> None:
        date = "Sun, 09 Aug 2026 02:02:23 GMT"
        headers = self.importer._pc_signature_headers(
            "https://api.cloud.189.cn/listFiles.action?folderId=-11",
            "GET",
            session_key="session-1",
            session_secret="secret-1",
            date=date,
            request_id="request-1",
        )
        source = (
            "SessionKey=session-1&Operate=GET"
            "&RequestURI=/listFiles.action&Date=" + date
        )
        expected = hmac.new(
            b"secret-1", source.encode("utf-8"), hashlib.sha1
        ).hexdigest().upper()
        self.assertEqual(headers["Signature"], expected)
        self.assertEqual(headers["X-Request-ID"], "request-1")

    def test_session_ip_mismatch_rebinds_from_access_token_and_retries_once(self) -> None:
        class Response:
            status_code = 200

            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload

            def json(self) -> dict[str, object]:
                return self.payload

        importer = TianyiImporter(
            {
                "session_key": "session-old",
                "session_secret": "secret-old",
                "access_token": "access-current",
            }
        )
        mismatch = {
            "res_code": "AccountCheckFailed",
            "res_message": "check ip error - curIp=36.133.96.186, cookiesIp=36.133.96.188",
        }
        with (
            patch.object(
                importer.http.session,
                "request",
                side_effect=[Response(mismatch), Response({"res_code": 0, "capacity": 1})],
            ) as request,
            patch.object(
                importer,
                "_login_by_access_token",
                return_value={"session_key": "session-new", "session_secret": "secret-new"},
            ) as login,
        ):
            value = importer._get("/listFiles.action")

        self.assertEqual(value["capacity"], 1)
        login.assert_called_once_with("access-current")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["headers"]["SessionKey"], "session-old")
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["SessionKey"], "session-new")
        self.assertEqual(importer._auth_data["sessionKey"], "session-new")
        self.assertEqual(importer._auth_data["sessionSecret"], "secret-new")

    def test_session_ip_mismatch_without_token_returns_safe_actionable_error(self) -> None:
        class Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, object]:
                return {
                    "res_code": "AccountCheckFailed",
                    "res_message": "check ip error - curIp=36.133.96.186, cookiesIp=36.133.96.188",
                }

        importer = TianyiImporter(
            {"session_key": "session-only", "session_secret": "secret-only"}
        )
        with patch.object(importer.http.session, "request", return_value=Response()):
            with self.assertRaises(TianyiApiError) as raised:
                importer._get("/listFiles.action")

        self.assertEqual(raised.exception.code, "SESSION_IP_MISMATCH")
        self.assertEqual(str(raised.exception), TIANYI_SESSION_IP_MISMATCH_MESSAGE)
        self.assertNotIn("36.133.96", str(raised.exception))

    def test_password_login_returns_captcha_challenge_with_encrypted_state(self) -> None:
        class Response:
            text = "1"
            content = b"captcha-image-content-more-than-twenty-bytes"

            @staticmethod
            def raise_for_status() -> None:
                return None

        state = {
            "captcha_token": "captcha-token",
            "req_id": "request-id",
            "lt": "login-ticket",
            "param_id": "param-id",
            "rsa_username": "encrypted-user",
            "rsa_password": "encrypted-password",
        }
        with (
            patch.object(self.importer, "_password_login_state", return_value=state),
            patch.object(self.importer.http.session, "post", return_value=Response()) as post,
            patch.object(self.importer.http.session, "get", return_value=Response()) as get,
        ):
            result = self.importer.begin_password_login("18900000000", "secret")

        self.assertEqual(result["status"], "captcha_required")
        self.assertTrue(result["captcha_image"].startswith("data:image/png;base64,"))
        self.assertEqual(result["login_state"]["rsa_password"], "encrypted-password")
        self.assertEqual(post.call_args.kwargs["data"]["userName"], "encrypted-user")
        self.assertEqual(get.call_args.kwargs["params"]["token"], "captcha-token")

    def test_needcaptcha_treats_every_nonzero_provider_value_as_required(self) -> None:
        class Response:
            text = "2"

            @staticmethod
            def json() -> int:
                return 2

        self.assertTrue(self.importer._captcha_required(Response()))

    def test_captcha_continuation_restores_complete_cookie_jar(self) -> None:
        self.importer.http.session.cookies.set(
            "LOGIN_COOKIE",
            "cookie-value",
            domain=".189.cn",
            path="/api/logbox",
            secure=True,
            expires=4102444800,
        )
        state = {"cookies": self.importer._export_login_cookies()}
        continued = TianyiImporter({})
        continued._restore_login_cookies(state)

        cookie = next(iter(continued.http.session.cookies))
        self.assertEqual(cookie.name, "LOGIN_COOKIE")
        self.assertEqual(cookie.value, "cookie-value")
        self.assertEqual(cookie.domain, ".189.cn")
        self.assertEqual(cookie.path, "/api/logbox")
        self.assertTrue(cookie.secure)
        self.assertEqual(cookie.expires, 4102444800)

    def test_captcha_error_refreshes_image_and_keeps_login_waiting(self) -> None:
        class LoginResponse:
            status_code = 200

            @staticmethod
            def json() -> dict[str, object]:
                return {"result": -2, "msg": "验证码错误"}

        class ImageResponse:
            status_code = 200
            content = b"new-captcha-image-content-more-than-twenty-bytes"
            headers = {"Content-Type": "image/png"}

        state = {
            "captcha_token": "captcha-token",
            "req_id": "request-id",
            "lt": "login-ticket",
            "param_id": "param-id",
            "return_url": "https://cloud.189.cn/return",
            "rsa_username": "{RSA}USER",
            "rsa_password": "{RSA}PASSWORD",
        }
        with (
            patch.object(self.importer.http.session, "post", return_value=LoginResponse()),
            patch.object(self.importer.http.session, "get", return_value=ImageResponse()) as get,
        ):
            result = self.importer._submit_password_login(state, "WRONG")

        self.assertEqual(result["status"], "captcha_required")
        self.assertTrue(result["captcha_image"].startswith("data:image/png;base64,"))
        self.assertIn("验证码错误", result["error"])
        get.assert_called_once()

    def test_password_login_submit_matches_189cloudpc_contract(self) -> None:
        class Response:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict[str, str]:
                return {"toUrl": "https://cloud.189.cn/redirect"}

        state = {
            "captcha_token": "captcha-token",
            "req_id": "request-id",
            "lt": "login-ticket",
            "param_id": "param-id",
            "return_url": "https://cloud.189.cn/return",
            "rsa_username": "{RSA}encrypted-user",
            "rsa_password": "{RSA}encrypted-password",
        }
        with (
            patch.object(self.importer.http.session, "post", return_value=Response()) as post,
            patch.object(
                self.importer,
                "_get_pc_session",
                return_value={"sessionKey": "session", "loginName": "189****0000"},
            ),
        ):
            result = self.importer._submit_password_login(state, "A1B2")

        headers = post.call_args.kwargs["headers"]
        data = post.call_args.kwargs["data"]
        self.assertNotIn("Cookie", headers)
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")
        self.assertTrue(headers["Referer"].endswith("/unifyAccountLogin.do"))
        self.assertEqual(data["clientType"], "10020")
        self.assertEqual(data["cb_SaveName"], "1")
        self.assertEqual(data["state"], "")
        self.assertEqual(data["returnUrl"], state["return_url"])
        self.assertEqual(data["password"], state["rsa_password"])
        self.assertEqual(data["userName"], state["rsa_username"])
        self.assertNotIn("epd", data)
        self.assertNotIn("mailSuffix", data)
        self.assertEqual(
            set(data),
            {
                "appKey",
                "accountType",
                "userName",
                "password",
                "validateCode",
                "captchaToken",
                "returnUrl",
                "dynamicCheck",
                "clientType",
                "cb_SaveName",
                "isOauth2",
                "state",
                "paramId",
            },
        )
        self.assertEqual(result["status"], "completed")

    def test_password_login_accepts_redirect_url_alias_and_result_errors(self) -> None:
        class SuccessResponse:
            status_code = 200

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict[str, str]:
                return {"redirectUrl": "https://cloud.189.cn/redirect"}

        state = {
            "captcha_token": "captcha-token",
            "req_id": "request-id",
            "lt": "login-ticket",
            "param_id": "param-id",
            "return_url": "https://cloud.189.cn/return",
            "rsa_username": "{RSA}encrypted-user",
            "rsa_password": "{RSA}encrypted-password",
        }
        with (
            patch.object(self.importer.http.session, "post", return_value=SuccessResponse()),
            patch.object(self.importer, "_get_pc_session", return_value={"sessionKey": "session"}),
        ):
            result = self.importer._submit_password_login(state, "")
        self.assertEqual(result["status"], "completed")

        class ErrorResponse(SuccessResponse):
            @staticmethod
            def json() -> dict[str, object]:
                return {"result": -5, "msg": "bad credentials"}

        with patch.object(self.importer.http.session, "post", return_value=ErrorResponse()):
            with self.assertRaisesRegex(Exception, "bad credentials") as raised:
                self.importer._submit_password_login(state, "")
        self.assertEqual(getattr(raised.exception, "code", ""), "-5")

    def test_password_login_state_uses_page_metadata_and_dynamic_rsa_prefix(self) -> None:
        html = """
        <input type='hidden' name='captchaToken' value='captcha-1'>
        <input id='j_rsaKey' value='page-key'>
        <script>
        var lt = "lt-1";
        var paramId = "param-1";
        var reqId = "req-1";
        var returnUrl = 'https://example.test/return';
        var pageKey = "normal";
        var accountType = '02';
        var clientType = '10020';
        var mailSuffix = '';
        var isOauth2 = "false";
        var state = "";
        </script>
        """

        class Response:
            status_code = 200
            content = b""

            def __init__(self) -> None:
                self.text = html

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict[str, object]:
                return {"result": 0, "data": {"pre": "{NRP}", "pubKey": "dynamic-key"}}

        with (
            patch.object(self.importer.http.session, "get", return_value=Response()) as get,
            patch.object(self.importer.http.session, "post", return_value=Response()) as post,
            patch.object(self.importer, "_rsa_encrypt", side_effect=["user-rsa", "password-rsa"]) as encrypt,
        ):
            state = self.importer._password_login_state("18900000000", "secret")

        self.assertEqual(state["rsa_username"], "{NRP}user-rsa")
        self.assertEqual(state["rsa_password"], "{NRP}password-rsa")
        self.assertNotIn("mail_suffix", state)
        self.assertNotIn("page_key", state)
        self.assertEqual(
            get.call_args.args[0],
            "https://cloud.189.cn/api/portal/unifyLoginForPC.action",
        )
        self.assertEqual(get.call_args.kwargs["params"]["clientType"], "10020")
        self.assertEqual(post.call_args.kwargs["data"]["appId"], "8025431004")
        self.assertEqual(encrypt.call_args_list[0].args[1], "18900000000")

    def test_login_form_uses_only_openlist_189pc_portal_route(self) -> None:
        class Response:
            content = b""
            status_code = 400
            text = ""

            @staticmethod
            def json() -> dict[str, object]:
                return {}

        with patch.object(
            self.importer.http.session,
            "get",
            return_value=Response(),
        ) as get:
            with self.assertRaises(TianyiApiError):
                self.importer._get_login_form_response(
                    {
                        "appId": self.importer.APP_ID,
                        "clientType": self.importer.CLIENT_TYPE,
                        "returnURL": self.importer.RETURN_URL,
                    }
                )

        get.assert_called_once()
        self.assertEqual(
            get.call_args.args[0],
            "https://cloud.189.cn/api/portal/unifyLoginForPC.action",
        )

    def test_password_login_state_preserves_exact_username_before_rsa(self) -> None:
        html = """
        <input type='hidden' name='captchaToken' value='captcha-1'>
        <input id='j_rsaKey' value='page-key'>
        <script>
        var lt = "lt-1"; var paramId = "param-1"; var reqId = "req-1";
        var returnUrl = 'https://example.test/return';
        var pageKey = "normal"; var accountType = '02'; var clientType = '10020';
        var mailSuffix = '@189.cn'; var isOauth2 = "false"; var state = "";
        </script>
        """

        class Response:
            status_code = 200
            content = b""
            text = html

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict[str, object]:
                return {"result": 1, "data": {"pre": "{RSA}", "pubKey": "dynamic-key"}}

        with (
            patch.object(self.importer.http.session, "get", return_value=Response()),
            patch.object(self.importer.http.session, "post", return_value=Response()),
            patch.object(self.importer, "_rsa_encrypt", side_effect=["user-rsa", "password-rsa"]) as encrypt,
        ):
            state = self.importer._password_login_state("alice", "secret")

        self.assertEqual(state["login_name"], "alice")
        self.assertEqual(encrypt.call_args_list[0].args[1], "alice")

    def test_rsa_ciphertext_is_uppercase_hex(self) -> None:
        cipher = type("Cipher", (), {"encrypt": lambda self, _value: b"\xab\xcd\x01"})()
        with (
            patch("fnos_media_import.importers.tianyi.RSA.import_key", return_value=object()),
            patch("fnos_media_import.importers.tianyi.PKCS1_v1_5.new", return_value=cipher),
        ):
            value = self.importer._rsa_encrypt("public-key", "secret")
        self.assertEqual(value, "ABCD01")

    def test_password_redirect_session_uses_telepc_post_contract(self) -> None:
        class Response:
            def __init__(self, status_code: int, payload: dict[str, object]) -> None:
                self.status_code = status_code
                self._payload = payload
                self.text = json.dumps(payload)

            def json(self) -> dict[str, object]:
                return self._payload

        with (
            patch.object(
                self.importer.http.session,
                "post",
                return_value=Response(
                    200,
                    {
                        "sessionKey": "session-1",
                        "sessionSecret": "secret-1",
                        "accessToken": "access-1",
                    },
                ),
            ) as post,
            patch.object(self.importer.http.session, "get") as get,
            patch.object(
                self.importer,
                "_pc_client_suffix",
                return_value={
                    "clientType": "TELEPC",
                    "version": "6.2",
                    "channelId": "web_cloud.189.cn",
                    "rand": "1_2",
                },
            ),
        ):
            value = self.importer._get_pc_session(redirect_url="https://example.test/redirect")

        self.assertEqual(value["sessionKey"], "session-1")
        post.assert_called_once()
        get.assert_not_called()
        self.assertEqual(post.call_args.kwargs["params"]["appId"], "8025431004")
        self.assertEqual(post.call_args.kwargs["params"]["clientType"], "TELEPC")
        self.assertEqual(post.call_args.kwargs["params"]["version"], "6.2")
        self.assertEqual(
            post.call_args.kwargs["params"]["redirectURL"],
            "https://example.test/redirect",
        )
        self.assertTrue(post.call_args.kwargs["headers"]["X-Request-ID"])

    def test_access_token_session_uses_telepc_get_contract(self) -> None:
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, object]:
                return {"sessionKey": "session-2", "sessionSecret": "secret-2"}

        with (
            patch.object(self.importer.http.session, "get", return_value=Response()) as get,
            patch.object(self.importer.http.session, "post") as post,
            patch.object(
                self.importer,
                "_pc_client_suffix",
                return_value={
                    "clientType": "TELEPC",
                    "version": "6.2",
                    "channelId": "web_cloud.189.cn",
                    "rand": "3_4",
                },
            ),
        ):
            value = self.importer._get_pc_session(access_token="access-2")

        self.assertEqual(value["accessToken"], "access-2")
        get.assert_called_once()
        post.assert_not_called()
        self.assertEqual(get.call_args.kwargs["params"]["accessToken"], "access-2")
        self.assertEqual(get.call_args.kwargs["params"]["clientType"], "TELEPC")
        self.assertTrue(get.call_args.kwargs["headers"]["X-Request-ID"])

    def test_refresh_token_is_used_and_persisted_before_session_login(self) -> None:
        updates: list[dict[str, object]] = []
        importer = TianyiImporter(
            {
                "refresh_token": "refresh-old",
                "access_token": "access-old",
                "access_token_expires_at": 1,
            },
            token_update_callback=updates.append,
        )
        with (
            patch.object(
                importer,
                "_refresh_access_token",
                return_value={"accessToken": "access-new", "refreshToken": "refresh-new", "expiresIn": 3600},
            ) as refresh,
            patch.object(
                importer,
                "_login_by_access_token",
                return_value={"sessionKey": "session-new", "sessionSecret": "secret-new"},
            ) as login,
        ):
            self.assertEqual(importer._session_key(), "session-new")

        refresh.assert_called_once_with("refresh-old")
        login.assert_called_once_with("access-new")
        self.assertEqual(updates[0]["access_token"], "access-new")
        self.assertEqual(updates[0]["refresh_token"], "refresh-new")
        self.assertGreater(int(updates[0]["access_token_expires_at"]), int(time.time()))

    def test_session_key_is_reused_for_probe_and_is_exported_as_credential(self) -> None:
        importer = TianyiImporter(
            {
                "session_key": "session-from-login",
                "session_secret": "secret-from-login",
            }
        )
        self.assertTrue(importer.check_configuration().ok)
        self.assertEqual(importer._session_key(), "session-from-login")
        self.assertEqual(
            importer.token_credentials(
                {"sessionKey": "session-2", "sessionSecret": "secret-2"}
            ),
            {"session_key": "session-2", "session_secret": "secret-2"},
        )

    def test_fresh_persisted_pc_session_does_not_exchange_a_second_session(self) -> None:
        importer = TianyiImporter(
            {"session_key": "session-1", "session_secret": "secret-1"}
        )
        self.assertGreater(importer._session_refreshed_at, 0)
        self.assertGreater(importer._last_keep_alive_at, 0)
        with patch.object(importer, "_authenticate") as authenticate:
            value = importer._pc_session_data()
        authenticate.assert_not_called()
        self.assertEqual(value["sessionKey"], "session-1")
        self.assertEqual(value["sessionSecret"], "secret-1")

    def test_pc_response_parser_accepts_xml_errors(self) -> None:
        class Response:
            status_code = 400
            text = (
                "<error><code>InvalidSessionKey</code>"
                "<message>session expired</message></error>"
            )

            @staticmethod
            def json() -> dict[str, object]:
                raise ValueError("not json")

        value = self.importer._response_payload(
            Response(), "Cloud189 PC API", stage="provider_request"
        )
        self.assertEqual(value["code"], "InvalidSessionKey")
        self.assertEqual(value["message"], "session expired")

    def test_ss_key_access_token_exchange_uses_reference_api_contract(self) -> None:
        response = type(
            "Response",
            (),
            {
                "status_code": 200,
                "json": lambda self: {"res_code": 0, "accessToken": "access-1"},
            },
        )()
        with (
            patch.object(self.importer.http.session, "get", return_value=response) as get,
            patch("fnos_media_import.importers.tianyi.time.time", return_value=1.234),
        ):
            value = self.importer._get_access_token_by_ss_key("session-1")

        self.assertEqual(value, {"accessToken": "access-1"})
        self.assertEqual(
            get.call_args.args[0],
            "https://api.cloud.189.cn/open/oauth2/getAccessTokenBySsKey.action",
        )
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["params"], {"sessionKey": "session-1"})
        self.assertEqual(kwargs["headers"]["AppKey"], "601102120")
        self.assertEqual(kwargs["headers"]["Timestamp"], "1234")
        self.assertEqual(
            kwargs["headers"]["Signature"],
            self.importer._signature(
                {"AppKey": "601102120", "Timestamp": "1234", "sessionKey": "session-1"}
            ),
        )

    def test_session_key_is_included_in_runtime_refresh_callback(self) -> None:
        updates: list[dict[str, object]] = []
        importer = TianyiImporter({}, token_update_callback=updates.append)
        importer._publish_tokens(
            {"sessionKey": "session-1", "sessionSecret": "secret-1"}
        )
        self.assertEqual(
            updates,
            [{"session_key": "session-1", "session_secret": "secret-1"}],
        )
        self.assertEqual(importer._session_key(), "session-1")


if __name__ == "__main__":
    unittest.main()
