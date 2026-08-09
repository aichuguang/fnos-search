"""CMCC upload verification safety retained after the V1 hard cutover.

Legacy rclone queue/staging cancellation tests were removed.  Durable V1
cancellation is covered by test_job_cancellation_v1 and transfer repository
guard tests.
"""

from __future__ import annotations

from tests._repository_v1_fakes import RepositoryFakeMixin

import unittest
from pathlib import Path
from unittest.mock import patch

from fnos_media_import.cmcc.client import CmccApiError
from fnos_media_import.cmcc.uploader import CmccUploader


class _CmccClientWithLookupFailure:
    def check_exists(self, **_kwargs):
        raise ConnectionResetError("query reset")


class _CmccClientWithoutPostCompleteVisibility(RepositoryFakeMixin):
    def __init__(self) -> None:
        self.lookup_count = 0

    def check_exists(self, **_kwargs) -> dict:
        self.lookup_count += 1
        return {"exist": False, "file_id": "", "size": None}

    def create_file(self, **_kwargs) -> dict:
        return {
            "fileId": "file-1",
            "uploadId": "upload-1",
            "parentFileId": "parent-1",
            "partInfos": [{"partNumber": 1, "uploadUrl": "https://upload.invalid/1"}],
        }

    def complete_file(self, **_kwargs) -> dict:
        return {"success": True}


class CmccVerificationSafetyTests(unittest.TestCase):
    def test_lookup_exception_is_not_converted_to_missing_file(self) -> None:
        uploader = CmccUploader(client=_CmccClientWithLookupFailure())

        with self.assertRaises(CmccApiError):
            uploader._find_existing("parent-1", "E01.mkv")

    def test_completed_upload_without_visible_matching_file_is_not_verified(self) -> None:
        client = _CmccClientWithoutPostCompleteVisibility()
        uploader = CmccUploader(client=client)
        uploader._upload_parts = lambda **_kwargs: None
        local_file = Path(__file__)
        with patch("fnos_media_import.cmcc.uploader._atomic_write_json") as write_manifest:
            result = uploader.upload_file(
                local_file=local_file,
                target_parent_file_id="parent-1",
                target_name="E01.mkv",
            )

            self.assertFalse(result.success)
            self.assertEqual(result.status, "failed")
            self.assertIn("verification failed", result.message)
            payloads = [
                call.args[1]
                for call in write_manifest.call_args_list
                if len(call.args) > 1
            ]
            self.assertTrue(payloads)
            self.assertNotIn(
                "verified",
                {str(payload.get("status")) for payload in payloads},
            )


if __name__ == "__main__":
    unittest.main()
