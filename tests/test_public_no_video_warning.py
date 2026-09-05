from __future__ import annotations

import unittest
from pathlib import Path


class PublicNoVideoWarningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.submit_js = Path("static/public-submit.js").read_text(encoding="utf-8")
        cls.public_js = Path("static/public.js").read_text(encoding="utf-8")
        cls.bootstrap_js = Path("static/public-bootstrap.js").read_text(encoding="utf-8")
        cls.submit_template = Path("templates/submit.html").read_text(encoding="utf-8")

    def function_source(self, name: str, next_name: str) -> str:
        start = self.submit_js.index(f"function {name}(")
        end = self.submit_js.index(f"function {next_name}(", start)
        return self.submit_js[start:end]

    def test_video_extension_set_only_common_formats(self) -> None:
        block = self.function_source("isPlayableVideoFileName", "isPreviewDirEntry")
        self.assertIn('.iso"', self.submit_js)
        for ext in (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".rmvb", ".ts"):
            self.assertIn(f'"{ext}"', self.submit_js)
        for ext in (".m2ts", ".mts", ".mpg", ".mpeg", ".mpe", ".m2v", ".rm", ".vob", ".3gp", ".3g2", ".asf", ".divx", ".f4v", ".tp", ".trp", ".dv"):
            self.assertNotIn(f'"{ext}"', self.submit_js)
        self.assertIn("toLowerCase()", block)
        self.assertIn('lastIndexOf(".")', block)
        self.assertIn("VIDEO_FILE_EXTENSION_SET.has", block)

    def test_detection_prefers_explicit_selection_and_never_warns_without_files(self) -> None:
        detect = self.function_source("submissionLacksPlayableVideo", "openPublicSubmitConfirm")

        self.assertIn("collectSubmissionEntries(publicId)", detect)
        self.assertIn("selectedFiles.length", detect)
        self.assertIn("knownFiles.length", detect)
        self.assertIn("return false;", detect)
        self.assertIn('entry.mediaType === "video"', detect)
        self.assertIn("isPlayableVideoFileName(entry.name)", detect)

    def test_confirm_submit_gates_on_no_video_warning_before_disabling_button(self) -> None:
        start = self.submit_js.index("async function confirmPublicResourceSubmit(")
        end = self.submit_js.index("\n        }", start)
        source = self.submit_js[start:end]

        hook = source.index("options.skipNoVideoWarning")
        self.assertIn("submissionLacksPlayableVideo(publicId)", source)
        self.assertIn("openNoVideoSubmitWarning(publicId)", source)
        disable = source.index('$("publicConfirmSubmit").disabled = true')
        self.assertLess(hook, disable, "no-video 检查必须在提交按钮禁用之前执行")

    def test_continue_resubmits_with_skip_flag(self) -> None:
        handler = self.function_source("continueNoVideoSubmit", "isPlayableVideoFileName")

        self.assertIn("publicState.pendingNoVideoSubmit", handler)
        self.assertIn("skipNoVideoWarning: true", handler)
        self.assertIn("confirmPublicResourceSubmit(", handler)

    def test_module_exports_no_video_warning_helpers(self) -> None:
        for name in (
            "isPlayableVideoFileName",
            "submissionLacksPlayableVideo",
            "openNoVideoSubmitWarning",
            "closeNoVideoSubmitWarning",
            "continueNoVideoSubmit",
        ):
            self.assertIn(f"\n            {name},", self.submit_js)
        self.assertIn("closeNoVideoSubmitWarning,", self.public_js)
        self.assertIn("continueNoVideoSubmit,", self.public_js)
        self.assertIn("pendingNoVideoSubmit: null", self.public_js)

    def test_template_has_styled_modal_and_no_native_dialog(self) -> None:
        template = self.submit_template
        for element_id in (
            "noVideoSubmitConfirm",
            "noVideoSubmitResourceName",
            "noVideoSubmitReason",
            "noVideoSubmitCancel",
            "noVideoSubmitContinue",
            "noVideoSubmitClose",
        ):
            self.assertIn(element_id, template)
        self.assertIn("当前提交好像不含可播放的影视资源，请确认是否提交？", template)
        self.assertIn("仍要提交", template)
        self.assertIn("取消，再检查一下", template)
        self.assertNotIn("window.confirm(", template)
        self.assertNotIn("window.alert(", template)

    def test_bootstrap_binds_no_video_modal_events(self) -> None:
        self.assertIn('$("noVideoSubmitCancel")?.addEventListener("click", closeNoVideoSubmitWarning)', self.bootstrap_js)
        self.assertIn('$("noVideoSubmitContinue")?.addEventListener("click", continueNoVideoSubmit)', self.bootstrap_js)
        self.assertIn('if (!$("noVideoSubmitConfirm")?.classList.contains("hidden"))', self.bootstrap_js)
        self.assertIn("closeNoVideoSubmitWarning();", self.bootstrap_js)

    def test_static_assets_version_bumped(self) -> None:
        request_template = Path("templates/request_status.html").read_text(encoding="utf-8")
        marker = "20260902-no-video-warn-v002"
        self.assertEqual(self.submit_template.count(marker), 3)
        self.assertEqual(request_template.count(marker), 3)


if __name__ == "__main__":
    unittest.main()
