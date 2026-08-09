from __future__ import annotations

import unittest
from unittest.mock import patch

from fnos_media_import.importers.cloud139 import Cloud139Importer


class Cloud139ImporterProbeTests(unittest.TestCase):
    @staticmethod
    def _importer() -> Cloud139Importer:
        return Cloud139Importer(
            {},
            cmcc_config={
                "authorization": "Basic test-credential",
                "phone": "13800000000",
            },
        )

    def test_probe_performs_minimal_read_only_provider_request(self) -> None:
        class Client:
            phone = "13800000000"

            def __init__(self) -> None:
                self.calls: list[tuple[str, int, int]] = []

            def list_dir(
                self,
                parent_file_id: str,
                *,
                page_size: int = 100,
                max_pages: int = 100,
            ):
                self.calls.append((parent_file_id, page_size, max_pages))
                return []

        importer = self._importer()
        client = Client()
        with patch.object(importer, "_cmcc", return_value=client):
            result = importer.probe()

        self.assertTrue(result.ok)
        self.assertEqual(client.calls, [("/", 1, 1)])
        self.assertEqual(result.message, "139 官方直转认证检测通过")

    def test_probe_rejects_invalid_or_expired_authorization(self) -> None:
        class Client:
            phone = "13800000000"

            @staticmethod
            def list_dir(
                _parent_file_id: str,
                *,
                page_size: int = 100,
                max_pages: int = 100,
            ):
                del page_size, max_pages
                raise RuntimeError("authorization expired")

        importer = self._importer()
        with patch.object(importer, "_cmcc", return_value=Client()):
            result = importer.probe()

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "probe_failed")
        self.assertIn("认证检测失败", result.message)


if __name__ == "__main__":
    unittest.main()
