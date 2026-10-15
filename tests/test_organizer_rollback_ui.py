from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(shutil.which("node") is None, "node is unavailable")
class OrganizerRollbackUiTests(unittest.TestCase):
    def test_rollback_status_buttons_and_conflict_feedback(self):
        script = """
const fs = require('node:fs');
const vm = require('node:vm');
global.window = globalThis;
require(%s);
require(%s);
const admin = fs.readFileSync(%s, 'utf8');
const labels = admin.slice(admin.indexOf('function statusText('), admin.indexOf('let adminMediaModule'));
const rolledBackLabel = vm.runInNewContext(labels + '; statusText("rolled_back")');
let handler;
let bindButton = false;
const messages = [];
const statuses = [];
const button = { dataset: { organizerRollback: '3' }, addEventListener: (_event, callback) => { handler = callback; } };
const box = { innerHTML: '', querySelectorAll: () => bindButton ? [button] : [] };
const state = {
    pagination: { organizerRuns: {} },
    organizerRuns: [
        { id: 1, task_id: 4, status: 'rolled_back', undo_data: [{}], summary: { rollback: { status: 'completed' } } },
        { id: 2, task_id: 4, status: 'done', undo_data: [], summary: { kind: 'rollback', rollback: { status: 'completed' } } },
    ],
};
const organizer = FnosAdminOrganizer.create({
    state, getElement: (id) => id === 'organizerRuns' ? box : null,
    escapeHtml: FnosUI.escapeHtml, formatDate: () => '-', renderPager: () => {},
    statusPill: (status) => { statuses.push(status); return status; },
    toast: (message, tone) => messages.push({ message, tone }),
    confirmDialog: async () => true, withButtonBusy: async (_button, _label, callback) => callback(),
    api: async () => { const error = new Error('rollback conflict'); error.status = 409; throw error; },
});
(async () => {
    organizer.renderOrganizerRuns();
    const rendered = box.innerHTML;
    state.organizerRuns = [{ id: 3, task_id: 4, status: 'done', undo_data: [{}] }];
    bindButton = true;
    organizer.renderOrganizerRuns();
    await handler();
    process.stdout.write(JSON.stringify({ rolledBackLabel, rendered, statuses, messages }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
""" % tuple(json.dumps(str(ROOT / "static" / name)) for name in ("ui-core.js", "admin-organizer.js", "admin.js"))
        completed = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True, timeout=10,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["rolledBackLabel"], "已回滚")
        self.assertEqual(result["statuses"][:2], ["rolled_back", "rolled_back"])
        self.assertNotIn("data-organizer-rollback", result["rendered"])
        self.assertEqual(result["messages"][-1], {"message": "rollback conflict", "tone": "error"})


if __name__ == "__main__":
    unittest.main()
