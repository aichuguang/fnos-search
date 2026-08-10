from __future__ import annotations

import base64
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fnos_media_import.openlist.webdav_client import OpenListWebDAVClient


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *args: Any) -> None:
        del args


class OpenListWebDAVRedirectTests(unittest.TestCase):
    def test_cross_origin_download_redirect_drops_basic_auth(self) -> None:
        class DownloadHandler(_QuietHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                self.server.received_authorization = self.headers.get("Authorization")  # type: ignore[attr-defined]
                if self.server.received_authorization:  # type: ignore[attr-defined]
                    body = b"<Error><Code>InvalidArgument</Code></Error>"
                    self.send_response(400)
                else:
                    body = b"probe-content"
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        download_server, download_thread = self._serve(DownloadHandler)

        class OpenListHandler(_QuietHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                self.server.received_authorization = self.headers.get("Authorization")  # type: ignore[attr-defined]
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{download_server.server_port}/object",
                )
                self.end_headers()

        openlist_server, openlist_thread = self._serve(OpenListHandler)
        self.addCleanup(
            self._stop_servers,
            ((openlist_server, openlist_thread), (download_server, download_thread)),
        )

        client = OpenListWebDAVClient(
            base_url=f"http://127.0.0.1:{openlist_server.server_port}/dav",
            username="admin",
            password="password",
        )

        self.assertEqual(client.read_bytes("/probe.bin"), b"probe-content")
        expected = "Basic " + base64.b64encode(b"admin:password").decode("ascii")
        self.assertEqual(openlist_server.received_authorization, expected)  # type: ignore[attr-defined]
        self.assertIsNone(download_server.received_authorization)  # type: ignore[attr-defined]

    def test_same_origin_redirect_keeps_basic_auth(self) -> None:
        class OpenListHandler(_QuietHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                if self.path == "/dav/start":
                    self.send_response(302)
                    self.send_header("Location", "/dav/final")
                    self.end_headers()
                    return
                self.server.final_authorization = self.headers.get("Authorization")  # type: ignore[attr-defined]
                body = b"same-origin"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server, thread = self._serve(OpenListHandler)
        self.addCleanup(self._stop_servers, ((server, thread),))
        client = OpenListWebDAVClient(
            base_url=f"http://127.0.0.1:{server.server_port}/dav",
            username="admin",
            password="password",
        )

        self.assertEqual(client.read_bytes("/start"), b"same-origin")
        expected = "Basic " + base64.b64encode(b"admin:password").decode("ascii")
        self.assertEqual(server.final_authorization, expected)  # type: ignore[attr-defined]

    @staticmethod
    def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    @staticmethod
    def _stop_servers(
        servers: tuple[tuple[ThreadingHTTPServer, threading.Thread], ...],
    ) -> None:
        for server, _thread in servers:
            server.shutdown()
            server.server_close()
        for _server, thread in servers:
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
