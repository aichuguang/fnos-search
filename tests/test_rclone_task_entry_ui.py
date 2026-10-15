from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RcloneTaskEntryTemplateTests(unittest.TestCase):
    def test_legacy_start_button_describes_task_navigation_instead_of_global_start(self) -> None:
        source = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        button = re.search(r'<button[^>]+id="adminRcloneStartBtn"[^>]*>([^<]+)</button>', source)
        self.assertIsNotNone(button)
        self.assertEqual(button.group(1), "查看入库任务")
        self.assertIn("不支持无任务全局扫描", source)
        self.assertNotIn("启动搬运</button>", source)


@unittest.skipUnless(shutil.which("node"), "Node is required for the UI regression")
class RcloneTaskEntryUiTests(unittest.TestCase):
    def run_ui(self, *, has_task_entry: bool) -> dict:
        script = """
globalThis.window = globalThis;
require(process.argv[1]);
const state = { activeTab: 'rclone' };
const requests = [];
const messages = [];
let clicks = 0;
let focused = false;
const jobsTab = { click() { clicks++; state.activeTab = 'jobs'; }, focus() { focused = true; } };
const ui = FnosAdminRclone.create({
    state,
    getElement: id => id === 'adminTabJobs' && %s ? jobsTab : null,
    api: async (path, options = {}) => { requests.push({ path, method: options.method || 'GET' }); return { success: true }; },
    toast: (message, tone) => messages.push({ message, tone }),
});
(async () => {
    await ui.startRclone();
    process.stdout.write(JSON.stringify({ activeTab: state.activeTab, clicks, focused, requests, messages }));
})().catch(error => { console.error(error); process.exitCode = 1; });
""" % json.dumps(has_task_entry)
        completed = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "admin-rclone.js")],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        return json.loads(completed.stdout)

    def test_legacy_start_function_opens_jobs_without_calling_global_start(self) -> None:
        result = self.run_ui(has_task_entry=True)
        self.assertEqual(result["activeTab"], "jobs")
        self.assertEqual(result["clicks"], 1)
        self.assertTrue(result["focused"])
        self.assertEqual(result["requests"], [])
        self.assertEqual(result["messages"][0]["tone"], "info")
        self.assertIn("具体入库任务", result["messages"][0]["message"])

    def test_missing_navigation_reports_error_without_falling_back_to_global_start(self) -> None:
        result = self.run_ui(has_task_entry=False)
        self.assertEqual(result["activeTab"], "rclone")
        self.assertEqual(result["clicks"], 0)
        self.assertEqual(result["requests"], [])
        self.assertEqual(result["messages"][0]["tone"], "error")


if __name__ == "__main__":
    unittest.main()
