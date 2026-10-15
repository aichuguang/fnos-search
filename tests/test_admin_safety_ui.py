from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from fnos_media_import.services.backup_service import FULL_BACKUP_VERSION


ROOT = Path(__file__).resolve().parents[1]


class AdminBackupVersionContractTests(unittest.TestCase):
    def test_browser_import_and_server_export_use_same_complete_backup_version(self) -> None:
        source = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
        version = re.search(r"const FULL_BACKUP_VERSION = (\d+);", source)
        self.assertIsNotNone(version)
        self.assertEqual(int(version.group(1)), FULL_BACKUP_VERSION)
        self.assertEqual(FULL_BACKUP_VERSION, 2)


@unittest.skipIf(shutil.which("node") is None, "node 不可用，跳过真实后台渲染模块校验")
class AdminReviewTextEscapingTests(unittest.TestCase):
    def test_review_list_and_detail_escape_every_reason_fallback(self) -> None:
        markup = '<b data-audit="marker">用户提交标题 & 审核理由</b>'
        script = """
globalThis.window = globalThis;
globalThis.renderListPagers = () => {};
const elements = new Map();
const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, {
        innerHTML: '', querySelectorAll: () => [], querySelector: () => null,
    });
    return elements.get(id);
};
require(%s);
require(%s);
const escapeHtml = window.FnosUI.escapeHtml;
const marker = %s;
(async () => {
    const results = [];
    for (const field of ['public_message', 'reason', 'admin_message']) {
        const item = {
            id: 1, title: 'Example', request_token: 'request-1',
            category: 'movie', status: 'pending_review',
            raw_data: { content_guard: {
                review_required: true, [field]: marker, label: marker,
                score: marker, safety_score: marker,
                evidence: [{ where: marker, label: marker, match: marker }],
            } },
        };
        let detail = '';
        const jobs = window.FnosAdminJobs.create({
            state: { requests: [item], pagination: { requests: { page: 1, per_page: 20 } } },
            getElement, escapeHtml, api: async () => ({ request: item, events: [] }),
            formatDate: (value) => value || '', statusText: (value) => value,
            statusPill: (value) => `<span>${escapeHtml(value)}</span>`,
            openDrawer: (_title, html) => { detail = html; },
        });
        jobs.renderRequests();
        await jobs.showRequestDetail(1);
        results.push({ field, list: getElement('adminRequestsBody').innerHTML, detail });
    }
    process.stdout.write(JSON.stringify({ escaped: escapeHtml(marker), results }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
""" % (
            json.dumps(str(ROOT / "static" / "ui-core.js")),
            json.dumps(str(ROOT / "static" / "admin-jobs.js")),
            json.dumps(markup, ensure_ascii=False),
        )
        completed = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True, timeout=10,
        )
        rendered = json.loads(completed.stdout)
        for result in rendered["results"]:
            with self.subTest(field=result["field"]):
                for surface in ("list", "detail"):
                    self.assertIn(rendered["escaped"], result[surface])
                    self.assertNotIn(markup, result[surface])


if __name__ == "__main__":
    unittest.main()
