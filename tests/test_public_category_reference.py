from __future__ import annotations

import unittest
from pathlib import Path


class PublicCategoryReferenceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.search_source = Path("static/public-search.js").read_text(encoding="utf-8")
        cls.submit_template = Path("templates/submit.html").read_text(encoding="utf-8")

    def test_tmdb_media_type_is_only_a_reference_in_category_chain(self) -> None:
        start = self.search_source.index("function tmdbReferenceCategory(")
        end = self.search_source.index("function categoryForPublicResource(", start)
        reference = self.search_source[start:end]
        self.assertIn("media_type", reference)
        # TMDB 只有 movie/tv，其它类型一律不提供参考分类。
        self.assertNotIn("anime", reference)
        self.assertNotIn("variety", reference)

    def test_category_chain_priority_keeps_tmdb_below_keyword_suggestion(self) -> None:
        for function in ("categoryForPublicResource", "recommendedCategoryForItem"):
            start = self.search_source.index(f"function {function}(")
            end = self.search_source.index("\n    function ", start + 1) if "\n    function " in self.search_source[start + 1:] else len(self.search_source)
            body = self.search_source[start:end]
            # 用户手动选择以早返回守卫形式最优先。
            self.assertIn("if (publicState.categoryTouched) return", body)
            self.assertLess(
                body.index("if (publicState.categoryTouched) return"),
                body.index("tmdbReferenceCategory"),
            )
            # 关键词建议（能区分动漫/综艺）优先于 TMDB 参考。
            self.assertLess(
                body.index("category_suggestion"),
                body.index("tmdbReferenceCategory"),
            )

    def test_submit_template_uses_current_public_search_asset(self) -> None:
        self.assertIn("public-search.js?v=20260901-category-ref-v001", self.submit_template)


if __name__ == "__main__":
    unittest.main()
