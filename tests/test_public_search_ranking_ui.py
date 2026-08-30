from __future__ import annotations

import unittest
from pathlib import Path


class PublicSearchRankingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path("static/public-search.js").read_text(encoding="utf-8")
        cls.public_source = Path("static/public.js").read_text(encoding="utf-8")
        cls.bootstrap_source = Path("static/public-bootstrap.js").read_text(encoding="utf-8")
        cls.admin_source = Path("static/admin.js").read_text(encoding="utf-8")
        cls.admin_template = Path("templates/admin.html").read_text(encoding="utf-8")
        cls.styles = Path("static/product.css").read_text(encoding="utf-8")

    def test_frontend_uses_backend_ranking_without_route_promotion(self) -> None:
        start = self.source.index("function sortPublicResources(")
        end = self.source.index("function inferQuality(", start)
        sorter = self.source[start:end]

        self.assertIn("ranking_score", sorter)
        self.assertIn("a.rank", sorter)
        self.assertNotIn("instantDelta", sorter)
        self.assertNotIn("fastDelta", sorter)
        self.assertNotIn("isSixpanCandidate", sorter)

    def test_guest_source_tags_use_provider_semantic_classes_and_colors(self) -> None:
        for class_name, color in (
            ("tag-source-quark", "#0d53ff"),
            ("tag-source-mobile", "#669ffb"),
            ("tag-source-tianyi", "#fca21a"),
            ("tag-source-bt", "#dc5d1f"),
            ("tag-source-unknown", "#7f8a9a"),
        ):
            with self.subTest(class_name=class_name):
                self.assertIn(class_name, self.source)
                self.assertIn(f".{class_name}", self.styles)
                self.assertIn(color, self.styles)

        source_tag_start = self.styles.index(".tag-source {")
        source_tag_end = self.styles.index(".tag-gold", source_tag_start)
        source_tag_styles = self.styles[source_tag_start:source_tag_end]
        self.assertIn("background: transparent", source_tag_styles)
        self.assertNotIn("source-tag-background", source_tag_styles)
        self.assertNotIn("box-shadow", source_tag_styles)

    def test_guest_provider_errors_are_hidden_from_messages_and_button_titles(self) -> None:
        self.assertIn("PUBLIC_SHARE_FAILURE_PATTERN", self.public_source)
        self.assertIn("shareUserRightcheck", self.public_source)
        self.assertIn('return "分享链接已失效"', self.public_source)

        start = self.source.index("function publicSubmitButtonMeta(")
        end = self.source.index("function searchPublicResources(", start)
        button_meta = self.source[start:end]
        self.assertIn("publicMessageForDisplay(", button_meta)
        self.assertNotIn('title: item?.availability_message ||', button_meta)

    def test_guest_capability_badges_are_configuration_driven(self) -> None:
        self.assertIn("resultProviderBadges: {}", self.public_source)
        self.assertIn("data.search?.result_provider_badges", self.bootstrap_source)
        self.assertIn("function resultProviderBadgeKey(", self.source)
        self.assertIn('source.providerKind === "btbtla"', self.source)
        self.assertIn(') return "magnet"', self.source)
        self.assertIn("config.enabled !== true", self.source)
        self.assertIn("if (isResourceInvalid(item)) return null", self.source)
        self.assertIn("provider-capability-badge", self.source)
        self.assertIn("has-provider-highlight", self.source)
        self.assertIn("provider-highlight-${providerBadge.provider}", self.source)
        self.assertIn("is-instant-import", self.source)
        self.assertIn("is-fast-import", self.source)
        self.assertIn("is-slow-import", self.source)
        self.assertIn(".provider-capability-badge", self.styles)
        badge_start = self.styles.index(".provider-capability-badge {")
        badge_end = self.styles.index(".resource-card.has-provider-badge", badge_start)
        badge_styles = self.styles[badge_start:badge_end]
        self.assertIn("right: -8px", badge_styles)
        self.assertIn("background: var(--source-tag-color", badge_styles)
        self.assertIn("transform: rotateZ(45deg)", badge_styles)
        self.assertIn("border: 0", badge_styles)
        highlight_start = self.styles.index(".resource-card.has-provider-highlight {")
        highlight_end = self.styles.index(".tag-fast", highlight_start)
        highlight_styles = self.styles[highlight_start:highlight_end]
        self.assertIn("background:", highlight_styles)
        self.assertIn("border-left: 4px", highlight_styles)
        self.assertIn("provider-highlight-mobile", highlight_styles)
        self.assertIn("provider-highlight-magnet", highlight_styles)
        self.assertIn(":not(.provider-capability-badge)", self.styles)
        self.assertNotIn('instant-import-badge">秒入库', self.source)
        self.assertNotIn('fast-import-badge">快速入库', self.source)

    def test_admin_provider_priority_unifies_source_and_badge_controls(self) -> None:
        self.assertIn("result_provider_priority", self.admin_source)
        self.assertIn("result_provider_badges", self.admin_source)
        self.assertIn("advPansouProviderPriority", self.admin_source)
        self.assertIn("data-provider-source-enabled", self.admin_source)
        self.assertIn("data-provider-badge-enabled", self.admin_source)
        self.assertIn("data-provider-badge-text", self.admin_source)
        self.assertIn("data-provider-priority-move", self.admin_source)
        self.assertIn('draggable="true"', self.admin_source)
        self.assertIn("2-5 个中文字符", self.admin_source)
        self.assertIn("isValidAdvancedProviderBadgeText", self.admin_source)
        self.assertNotIn("advPansouCloudTypes", self.admin_template)


if __name__ == "__main__":
    unittest.main()
