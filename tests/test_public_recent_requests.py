from __future__ import annotations

import unittest
from pathlib import Path


class PublicRecentRequestsTests(unittest.TestCase):
    def test_query_tab_contains_compact_recent_request_list(self) -> None:
        template = Path("templates/submit.html").read_text(encoding="utf-8")

        self.assertIn('id="requestRecentSection"', template)
        self.assertIn('id="requestRecentList"', template)

    def test_recent_requests_are_limited_and_click_to_query(self) -> None:
        source = Path("static/public-submit.js").read_text(encoding="utf-8")

        self.assertIn('const RECENT_REQUESTS_LIMIT = 5;', source)
        self.assertIn('items.slice(0, RECENT_REQUESTS_LIMIT)', source)
        self.assertIn('queryRequestStatus(token);', source)
        self.assertIn('data-recent-request-token', source)

    def test_recent_request_storage_excludes_share_credentials(self) -> None:
        source = Path("static/public-submit.js").read_text(encoding="utf-8")
        storage_block = source[source.index("function loadRecentRequests()") : source.index("function isPublicSubmitConfirmOpen")]

        self.assertNotIn("password", storage_block)
        self.assertNotIn("source_url", storage_block)
        self.assertNotIn("note", storage_block)


if __name__ == "__main__":
    unittest.main()
