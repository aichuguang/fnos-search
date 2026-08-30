from __future__ import annotations

import unittest
from unittest.mock import Mock

import requests

from fnos_media_import.http_client import HttpClient, HttpJsonDecodeError
from fnos_media_import.importers.generic import GenericWebhookImporter
from fnos_media_import.importers.quark import QuarkApiError, QuarkImporter


class _Response:
    def __init__(self, status_code: int, payload: object = None, *, text: str = ""):
        self.status_code = status_code
        self.payload = payload
        self.text = text
        self.closed = False

    def json(self) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self) -> None:
        self.closed = True


class HttpClientTests(unittest.TestCase):
    def _client(self, *responses: object, retries: int = 2) -> tuple[HttpClient, Mock, Mock]:
        session = Mock(spec=requests.Session)
        session.request.side_effect = list(responses)
        sleep = Mock()
        client = HttpClient(
            timeout=20,
            connect_timeout=3,
            request_retries=retries,
            retry_delay_seconds=0.1,
            session=session,
            sleep=sleep,
        )
        return client, session, sleep

    def test_get_retries_temporary_connection_failure(self) -> None:
        client, session, sleep = self._client(
            requests.ConnectTimeout("connect timeout"),
            _Response(200, {"ok": True}),
        )

        status, payload = client.get_json("https://example.invalid/data")

        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertEqual(session.request.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_plain_post_does_not_retry(self) -> None:
        first = _Response(503, {"ok": False})
        client, session, sleep = self._client(first, _Response(200, {"ok": True}))

        status, payload = client.post_json("https://example.invalid/write", {"id": 1})

        self.assertEqual((status, payload), (503, {"ok": False}))
        self.assertEqual(session.request.call_count, 1)
        self.assertFalse(first.closed)
        sleep.assert_not_called()

    def test_read_only_post_retries_retryable_status(self) -> None:
        first = _Response(503, {"ok": False})
        client, session, sleep = self._client(first, _Response(200, {"ok": True}))

        status, payload = client.post_json(
            "https://example.invalid/search",
            {"keyword": "test"},
            read_only=True,
        )

        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertEqual(session.request.call_count, 2)
        self.assertTrue(first.closed)
        sleep.assert_called_once_with(0.1)

    def test_uses_separate_connect_and_read_timeouts(self) -> None:
        client, session, _sleep = self._client(_Response(200, {}), retries=0)

        client.get_json("https://example.invalid/data")

        self.assertEqual(session.request.call_args.kwargs["timeout"], (3.0, 20.0))

    def test_invalid_json_raises_explicit_error(self) -> None:
        client, _session, _sleep = self._client(
            _Response(200, ValueError("invalid"), text="<html>bad gateway</html>"),
            retries=0,
        )

        with self.assertRaises(HttpJsonDecodeError) as raised:
            client.get_json("https://example.invalid/data")

        self.assertEqual(raised.exception.status_code, 200)
        self.assertEqual(raised.exception.response_text, "<html>bad gateway</html>")

    def test_generic_webhook_does_not_treat_invalid_json_as_success(self) -> None:
        importer = GenericWebhookImporter(
            "测试接口",
            {"endpoint": "https://example.invalid/import"},
        )
        importer.http.post_json = Mock(
            side_effect=HttpJsonDecodeError(
                "POST",
                "https://example.invalid/import",
                200,
                "accepted",
            )
        )

        result = importer.import_resource(
            "测试资源",
            "https://example.invalid/share",
            {"label": "TV", "native_locator": {"value": "/tv"}},
        )

        self.assertFalse(result.success)
        self.assertIn("无效 JSON", result.message)

    def test_quark_maps_invalid_json_to_provider_error(self) -> None:
        importer = QuarkImporter({"cookie": "sid=test"})
        importer.http.get_json = Mock(
            side_effect=HttpJsonDecodeError(
                "GET",
                "https://drive-pc.quark.cn/test",
                200,
                "<html>invalid</html>",
            )
        )

        with self.assertRaises(QuarkApiError) as raised:
            importer._get("/test")

        self.assertIn("无效 JSON", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
