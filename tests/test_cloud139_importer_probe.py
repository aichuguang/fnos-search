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

    def test_list_folders_returns_only_drive_folders(self) -> None:
        class Client:
            phone = "13800000000"

            def __init__(self) -> None:
                self.calls: list[str] = []

            def list_dir(
                self,
                parent_file_id: str,
                *,
                page_size: int = 100,
                max_pages: int = 100,
            ):
                del page_size, max_pages
                self.calls.append(parent_file_id)
                return [
                    {"fileId": "dir-1", "name": "电影", "type": "folder"},
                    {"fileId": "file-1", "name": "a.mkv", "type": "file"},
                    {"fileId": "dir-2", "fileName": "电视剧", "isFolder": True},
                    {"fileId": "file-2", "name": "b.mkv", "fileType": "file"},
                ]

        importer = self._importer()
        client = Client()
        with patch.object(importer, "_cmcc", return_value=client):
            folders = importer.list_folders("/")

        self.assertEqual(
            folders,
            [
                {"folder_id": "dir-1", "name": "电影", "parent_folder_id": "/"},
                {"folder_id": "dir-2", "name": "电视剧", "parent_folder_id": "/"},
            ],
        )
        self.assertEqual(client.calls, ["/"])

    def test_list_folders_unconfigured_raises(self) -> None:
        importer = Cloud139Importer({})
        with self.assertRaisesRegex(RuntimeError, "未配置"):
            importer.list_folders("/")


if __name__ == "__main__":
    unittest.main()
