from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from fnos_media_import.cmcc.client import CmccApiError, CmccClient
from fnos_media_import.http_client import HttpJsonDecodeError
from fnos_media_import.importers.cloud139 import Cloud139Importer
from fnos_media_import.importers.quark import QuarkApiError, QuarkImporter
from fnos_media_import.importers.sixpan import SixPanApiError, SixPanClient, SixPanOfflineImporter
from fnos_media_import.importers.tianyi import TianyiApiError, TianyiImporter
from fnos_media_import.provider_errors import (
    SHARE_INVALID_MESSAGE,
    is_share_resource_unavailable,
    provider_error_details,
    share_error_details,
)
from fnos_media_import.providers.adapters import QuarkProviderAdapter
from fnos_media_import.transfer import ErrorCode, TransferError, receipt_from_import_result


QUARK_TOKEN = "/1/clouddrive/share/sharepage/token"
QUARK_DETAIL = "/1/clouddrive/share/sharepage/detail"
TIANYI_INFO = "/api/open/share/getShareInfoByCodeV2.action"
MOBILE_INFO = "/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6"


def response(status: int, payload=None, *, html: bool = False) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    result._content = ("<html>Not Found</html>" if html else json.dumps(payload or {})).encode("utf-8")
    result.headers["Content-Type"] = "text/html" if html else "application/json"
    return result


class ShareErrorClassificationTests(unittest.TestCase):
    def test_only_known_share_read_404_and_410_are_classified_without_business_evidence(self):
        for provider, path in (("quark", QUARK_TOKEN), ("quark", QUARK_DETAIL),
                               ("tianyi", TIANYI_INFO), ("mobile", MOBILE_INFO)):
            for status in (404, 410):
                with self.subTest(provider=provider, path=path, status=status):
                    self.assertTrue(is_share_resource_unavailable(provider, path=path, http_status=status))
        for provider, path in (("quark", "/account/info"), ("quark", "/1/clouddrive/file"),
                               ("tianyi", "/api/open/user/getUserInfo.action"),
                               ("mobile", "/file/create"), ("sixpan", "/v6/offline_task/parse")):
            with self.subTest(provider=provider, path=path):
                self.assertFalse(is_share_resource_unavailable(provider, path=path, http_status=404))

    def test_auth_permissions_access_codes_throttling_and_outages_are_not_expired_resources(self):
        cases = (
            {"http_status": 401}, {"http_status": 429}, {"http_status": 503, "code": "ShareExpired"},
            {"http_status": 403, "message": "Permission denied"},
            {"http_status": 404, "code": "InvalidAccessCode"},
            {"http_status": 404, "message": "提取码错误"},
            {"http_status": 404, "message": "Cookie expired"},
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(is_share_resource_unavailable("quark", path=QUARK_TOKEN, **case))

    def test_explicit_provider_business_errors_are_normalized(self):
        for provider, path, code, message in (
            ("quark", QUARK_DETAIL, "10000", ""),
            ("quark", QUARK_TOKEN, "41002", ""),
            ("tianyi", TIANYI_INFO, "ShareExpired", ""),
            ("mobile", MOBILE_INFO, "RESOURCE_NOT_FOUND", ""),
            ("sixpan", "/v6/offline_task/parse", "", "资源已删除"),
            ("sixpan", "/v6/offline_task/add", "SourceNotFound", ""),
        ):
            with self.subTest(provider=provider, code=code):
                self.assertTrue(is_share_resource_unavailable(provider, path=path, code=code, message=message))

    def test_diagnostics_preserve_status_and_path_but_redact_credentials(self):
        messages = (
            "Authorization: Bearer private-token", "Authorization=Basic private-token",
            '"access_token":"private-token"', '"password":"private-token"',
            "Cookie: sid=private-token; __puus=private-cookie", "stoken=private-token&foo=bar",
        )
        for message in messages:
            with self.subTest(message=message), self.assertLogs("fnos_media_import.providers", "WARNING") as logs:
                details = share_error_details("quark", path=QUARK_DETAIL + "?stoken=private-token",
                                              http_status=404, code="ShareExpired", message=message)
            self.assertEqual(details["http_status"], 404)
            self.assertEqual(details["provider_path"], QUARK_DETAIL)
            self.assertEqual(details["provider_code"], "ShareExpired")
            self.assertNotIn("private-", json.dumps(details) + " ".join(logs.output))
            self.assertIn("[REDACTED]", details["technical_message"])


class QuarkShareErrorTests(unittest.TestCase):
    def importer(self):
        importer = QuarkImporter({"cookie": "sid=test"})
        importer.http.get_json = Mock(side_effect=AssertionError("Unexpected network request"))
        importer.http.post_json = Mock(side_effect=AssertionError("Unexpected network request"))
        return importer

    def test_json_or_html_share_404_and_410_have_friendly_text_and_machine_code(self):
        for status in (404, 410):
            for html in (False, True):
                with self.subTest(status=status, html=html):
                    importer = self.importer()
                    if html:
                        importer.http.post_json.side_effect = HttpJsonDecodeError("POST", QUARK_TOKEN, status, "<html>gone</html>")
                    else:
                        importer.http.post_json.side_effect = None
                        importer.http.post_json.return_value = (status, {})
                    ok, data = importer.check_share("https://pan.quark.cn/s/deadbeef")
                    self.assertFalse(ok)
                    self.assertEqual(data["message"], SHARE_INVALID_MESSAGE)
                    self.assertEqual(data["error_code"], ErrorCode.SHARE_INVALID)
                    self.assertEqual(data["error_details"]["http_status"], status)
                    self.assertNotIn("HTTP", data["message"])

    def test_detail_failure_stops_save_and_preserves_nonretryable_code(self):
        importer = self.importer()
        importer.http.post_json.side_effect = None
        importer.http.post_json.return_value = (200, {"code": 0, "data": {"stoken": "test-token"}})
        importer.http.get_json.side_effect = None
        importer.http.get_json.return_value = (404, {})
        result = importer.import_resource("film", "https://pan.quark.cn/s/deadbeef", "/landing")
        self.assertFalse(result.success)
        self.assertIn(SHARE_INVALID_MESSAGE, result.message)
        self.assertNotIn("HTTP 404", result.message)
        self.assertEqual(result.raw_data["error_code"], ErrorCode.SHARE_INVALID)
        self.assertEqual(result.raw_data["http_status"], 404)
        receipt = receipt_from_import_result(result, provider_kind="quark")
        self.assertFalse(TransferError(receipt.error_code, receipt.message).retryable)
        self.assertEqual(importer.http.post_json.call_count, 1)

    def test_account_or_network_errors_keep_their_own_classification(self):
        for status, path, code in ((404, "/account/info", "HTTP_404"), (401, QUARK_TOKEN, "AUTH_EXPIRED"),
                                   (403, QUARK_TOKEN, "AUTH_EXPIRED"), (429, QUARK_TOKEN, ErrorCode.NETWORK_TRANSIENT),
                                   (503, QUARK_TOKEN, ErrorCode.NETWORK_TRANSIENT)):
            with self.subTest(status=status, path=path), self.assertRaises(QuarkApiError) as caught:
                QuarkImporter._validate_response(status, {}, path=path)
            self.assertEqual(caught.exception.code, code)
            self.assertNotEqual(str(caught.exception), SHARE_INVALID_MESSAGE)

    def test_transient_http_and_transport_errors_remain_retryable_through_import_receipt(self):
        for cause in (429, 503, HttpJsonDecodeError("POST", QUARK_TOKEN, 503, "gateway unavailable"),
                      requests.exceptions.ConnectTimeout("connection timed out")):
            with self.subTest(cause=cause):
                importer = self.importer()
                if isinstance(cause, int):
                    importer.http.post_json.side_effect = None
                    importer.http.post_json.return_value = (cause, {})
                else:
                    importer.http.post_json.side_effect = cause
                result = importer.import_resource("film", "https://pan.quark.cn/s/test", "/landing")
                self.assertFalse(result.success)
                receipt = receipt_from_import_result(result, provider_kind="quark")
                self.assertEqual(receipt.error_code, ErrorCode.NETWORK_TRANSIENT)
                self.assertTrue(TransferError(receipt.error_code, receipt.message).retryable)
                self.assertNotIn(SHARE_INVALID_MESSAGE, result.message)
                if isinstance(cause, int):
                    self.assertEqual(result.raw_data["http_status"], cause)


class OtherProviderShareErrorTests(unittest.TestCase):
    def test_tianyi_share_check_maps_json_and_html_gone_responses(self):
        for status, html in ((404, False), (410, True)):
            with self.subTest(status=status, html=html):
                importer = TianyiImporter({"cookie": "SSON=test"})
                importer._session_key = Mock(return_value="test-session")
                importer.http.session.request = Mock(return_value=response(status, html=html))
                ok, data = importer.check_share("https://cloud.189.cn/t/testShare")
                self.assertFalse(ok)
                self.assertEqual(data["message"], SHARE_INVALID_MESSAGE)
                self.assertEqual(data["error_code"], ErrorCode.SHARE_INVALID)
                self.assertEqual(data["error_details"]["http_status"], status)

    def test_tianyi_auth_error_does_not_get_hidden_as_expired_share(self):
        importer = TianyiImporter({"cookie": "SSON=test"})
        importer._session_key = Mock(return_value="test-session")
        importer.http.session.request = Mock(return_value=response(401, {"errorCode": "InvalidAccessToken"}))
        importer._rebind_session_for_ip_mismatch = Mock(return_value=False)
        with self.assertRaises(TianyiApiError) as caught:
            importer._web_request("GET", TIANYI_INFO)
        self.assertNotEqual(caught.exception.code, ErrorCode.SHARE_INVALID)
        self.assertNotEqual(str(caught.exception), SHARE_INVALID_MESSAGE)

    def mobile_client(self, value):
        auth = SimpleNamespace(
            host="https://test.invalid",
            get_credentials=lambda: SimpleNamespace(phone="13800000000"),
            is_auth_error=lambda *_args, **_kwargs: False,
        )
        client = CmccClient(auth=auth)
        client._share_headers = Mock(return_value={})
        client.session.post = Mock(return_value=value)
        return client

    def test_mobile_share_check_propagates_friendly_message_and_details(self):
        client = self.mobile_client(response(404, html=True))
        importer = Cloud139Importer({}, {"access_token": "test"})
        importer._cmcc_client = client
        with patch.object(CmccClient, "extract_share_url", return_value=("share-id", "", "root")):
            ok, data = importer.check_share("https://yun.139.com/shareweb/#/w/i/test")
        self.assertFalse(ok)
        self.assertIn(SHARE_INVALID_MESSAGE, data["message"])
        self.assertNotIn("HTTP", data["message"])
        self.assertEqual(data["error_code"], ErrorCode.SHARE_INVALID)
        self.assertEqual(data["error_details"]["http_status"], 404)

    def test_mobile_non_share_route_404_and_auth_rejection_are_not_share_invalid(self):
        for status, path in ((404, "/unrecognized-api"), (401, MOBILE_INFO)):
            with self.subTest(status=status, path=path):
                client = self.mobile_client(response(status))
                with self.assertRaises(CmccApiError) as caught:
                    client.share_post_json(path, {})
                self.assertNotEqual(caught.exception.code, ErrorCode.SHARE_INVALID)

    def test_mobile_explicit_failure_without_numeric_code_does_not_report_success(self):
        client = self.mobile_client(response(200, {"success": False, "message": "分享已取消"}))
        with self.assertRaises(CmccApiError) as caught:
            client.share_post_json(MOBILE_INFO, {})
        self.assertEqual(caught.exception.code, ErrorCode.SHARE_INVALID)

    def test_mobile_share_transient_http_failure_keeps_retry_classification(self):
        for status in (429, 503):
            with self.subTest(status=status):
                importer = Cloud139Importer({}, {"access_token": "test"})
                importer._cmcc_client = self.mobile_client(response(status))
                with patch.object(CmccClient, "extract_share_url", return_value=("share-id", "", "root")):
                    ok, data = importer.check_share("https://yun.139.com/shareweb/#/w/i/test")
                self.assertFalse(ok)
                self.assertEqual(data["error_code"], ErrorCode.NETWORK_TRANSIENT)
                self.assertEqual(data["error_code"], data["error_details"]["error_code"])
                self.assertEqual(data["error_details"]["http_status"], status)
                self.assertTrue(TransferError(data["error_code"], data["message"]).retryable)

    def test_sixpan_requires_explicit_resource_evidence_not_bare_api_404(self):
        client = SixPanClient({"client_id": "test", "client_secret": "test", "access_token": "test"})
        for status, body, expected in ((404, {}, False), (200, {"code": "RESOURCE_NOT_FOUND", "data": {}}, True)):
            with self.subTest(status=status, body=body):
                client.session.post = Mock(return_value=response(status, body))
                with self.assertRaises(SixPanApiError) as caught:
                    client._post_once("/v6/offline_task/parse", {})
                self.assertEqual(caught.exception.code == ErrorCode.SHARE_INVALID, expected)
                if expected:
                    self.assertEqual(str(caught.exception), SHARE_INVALID_MESSAGE)

    def test_sixpan_optional_parse_never_submits_explicitly_expired_resources(self):
        importer = SixPanOfflineImporter({"client_id": "test", "client_secret": "test", "access_token": "test",
                                          "parse_before_add": True, "parse_required": False})
        importer.parse_resource = Mock(side_effect=SixPanApiError(SHARE_INVALID_MESSAGE, code=ErrorCode.SHARE_INVALID))
        importer.client.session.post = Mock(side_effect=AssertionError("Expired resource must not be submitted"))
        result = importer.import_resource("film", "magnet:?xt=urn:btih:deadbeef", {}, save_path="/landing")
        self.assertFalse(result.success)
        self.assertEqual(result.raw_data["error_code"], ErrorCode.SHARE_INVALID)
        self.assertIn(SHARE_INVALID_MESSAGE, result.message)
        importer.client.session.post.assert_not_called()

    def test_error_details_keep_machine_code_and_redact_generic_diagnostics(self):
        details = provider_error_details(QuarkApiError("Authorization: Bearer private-token", code="HTTP_404"))
        self.assertEqual(details["error_code"], "HTTP_404")
        self.assertNotIn("private-token", details["technical_message"])

    def test_provider_adapter_preserves_failure_classification_and_diagnostics(self):
        for code in (ErrorCode.SHARE_INVALID, ErrorCode.AUTH_EXPIRED, ""):
            with self.subTest(code=code):
                importer = SimpleNamespace(check_share=Mock(return_value=(False, {
                    "message": SHARE_INVALID_MESSAGE if code == ErrorCode.SHARE_INVALID else "Account or transport error",
                    "error_code": code, "error_details": {"http_status": 404},
                })))
                adapter = QuarkProviderAdapter(importer_factory=Mock(),
                                               account_importer_factory=Mock(return_value=importer),
                                               source_record_service=Mock())
                with self.assertRaises(TransferError) as caught:
                    adapter.inspect_source({}, {"url": "https://pan.quark.cn/s/test"})
                self.assertEqual(caught.exception.code, code or ErrorCode.RESULT_UNKNOWN)
                self.assertEqual(caught.exception.details["http_status"], 404)

    def test_unclassified_provider_codes_keep_legacy_retry_fallback(self):
        quark = QuarkImporter({"cookie": "sid=test"})
        quark.http.post_json = Mock(return_value=(200, {"code": "E_BACKEND", "message": "Temporary provider error"}))
        mobile = Cloud139Importer({}, {"access_token": "test"})
        mobile._cmcc_client = self.mobile_client(response(200, {
            "success": False, "code": "E_BACKEND", "message": "Temporary provider error",
        }))
        with patch.object(CmccClient, "extract_share_url", return_value=("share-id", "", "root")):
            checked = [quark.check_share("https://pan.quark.cn/s/test"),
                       mobile.check_share("https://yun.139.com/shareweb/#/w/i/test")]
        for ok, data in checked:
            self.assertFalse(ok)
            self.assertEqual(data["error_code"], "")
            self.assertEqual(data["error_details"]["provider_code"], "E_BACKEND")
            self.assertTrue(TransferError(data["error_code"] or ErrorCode.NETWORK_TRANSIENT, data["message"]).retryable)


if __name__ == "__main__":
    unittest.main()
