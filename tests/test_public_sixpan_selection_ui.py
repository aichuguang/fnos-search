from __future__ import annotations

import unittest
from pathlib import Path


class PublicSixpanSelectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path("static/public-submit.js").read_text(encoding="utf-8")

    def function_source(self, name: str, next_name: str) -> str:
        start = self.source.index(f"function {name}(")
        end = self.source.index(f"function {next_name}(", start)
        return self.source[start:end]

    def test_select_all_button_becomes_clear_all_when_everything_is_selected(self) -> None:
        render = self.function_source("renderSixpanParseHtml", "renderSixpanFileRow")

        self.assertIn('data-sixpan-select-toggle="1"', render)
        self.assertIn('allSelected ? "none" : "all"', render)
        self.assertIn('allSelected ? "取消全选" : "全选"', render)

    def test_single_checkbox_change_does_not_rerender_the_long_list(self) -> None:
        handler = self.function_source("handleSixpanParseChange", "shouldWarnSlowSixpanSubmit")

        self.assertIn("publicState.sixpanParses[key] =", handler)
        self.assertIn("syncSixpanSelectionUi(key);", handler)
        self.assertNotIn("setSixpanParseState", handler)
        self.assertNotIn("rerenderSixpanParse", handler)

    def test_bulk_selection_updates_existing_controls_without_rerendering(self) -> None:
        action = self.function_source("applySixpanAction", "syncSixpanSelectionUi")
        sync = self.function_source("syncSixpanSelectionUi", "isSixpanPreferredMedia")

        self.assertIn("syncSixpanSelectionUi(key);", action)
        self.assertNotIn("setSixpanParseState", action)
        self.assertIn('document.querySelectorAll("[data-sixpan-key]")', sync)
        self.assertIn("element.checked =", sync)
        self.assertIn("element.textContent = allSelected", sync)

    def test_selection_payload_counts_automatically_filtered_files_consistently(self) -> None:
        payload = self.function_source("sixpanSelectionPayload", "applySixpanAction")

        self.assertIn("const sourceFiles = items.filter", payload)
        self.assertIn("total_count: sourceFiles.length", payload)
        self.assertIn("selectedCount = sourceFiles.filter", payload)
        self.assertIn("ignored_count: ignoreFiles.length", payload)


if __name__ == "__main__":
    unittest.main()
