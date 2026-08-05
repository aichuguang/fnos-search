from __future__ import annotations

import unittest
from pathlib import Path


class PublicSearchRankingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path("static/public-search.js").read_text(encoding="utf-8")

    def test_frontend_uses_backend_ranking_without_route_promotion(self) -> None:
        start = self.source.index("function sortPublicResources(")
        end = self.source.index("function inferQuality(", start)
        sorter = self.source[start:end]

        self.assertIn("ranking_score", sorter)
        self.assertIn("a.rank", sorter)
        self.assertNotIn("instantDelta", sorter)
        self.assertNotIn("fastDelta", sorter)
        self.assertNotIn("isSixpanCandidate", sorter)


if __name__ == "__main__":
    unittest.main()
