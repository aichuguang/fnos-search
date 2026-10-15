from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for the UI regression")
class OrganizerMappingConfirmationUiTests(unittest.TestCase):
    def run_ui(self, body: str) -> dict:
        script = """
globalThis.window = globalThis;
require(process.argv[1]);
require(process.argv[2]);
const task = { id: 7, status: 'waiting_review', mappings: [{
    id: 9, status: 'need_edit', source_path: '/staging/ad.mp4', target_path: '/library/ad.mp4',
    title: 'Sample', year: '2026', media_type: 'movie', season: null, episode: null, tmdb_id: null,
}], evidence: {} };
function mappingRow(mapping, taskId = 7) {
    const fields = Object.entries({
        status: mapping.status, target_path: mapping.target_path, title: mapping.title,
        year: mapping.year, season: mapping.season, episode: mapping.episode,
        tmdb_id: mapping.tmdb_id, media_type: mapping.media_type,
    }).map(([key, raw]) => {
        const value = String(raw ?? '');
        const choices = key === 'status' ? ['ready', 'need_edit', 'conflict', 'delete_ad', 'skipped']
            : key === 'media_type' ? ['movie', 'tv'] : null;
        return { dataset: { mapField: key }, value, defaultValue: value,
            tagName: choices ? 'SELECT' : key === 'target_path' ? 'TEXTAREA' : 'INPUT',
            options: choices ? choices.map(option => ({ value: option, defaultSelected: option === value })) : [],
            dispatchEvent(event) { this.lastEvent = event.type; return true; },
        };
    });
    return { dataset: { organizerMappingRow: String(mapping.id), organizerMappingTask: String(taskId) },
        fields, querySelectorAll: selector => selector === '[data-map-field]' ? fields : [] };
}
let rows = [mappingRow(task.mappings[0])];
const requests = [];
const messages = [];
const confirmations = [];
let confirmed = true;
let saveFailure = '';
let recomputeTarget = false;
let opened = 0;
let closed = 0;
let rendered = '';
const drawer = { querySelectorAll: selector => selector === '[data-organizer-mapping-row]' ? rows : [] };
globalThis.document = { querySelectorAll: selector => drawer.querySelectorAll(selector) };
const ui = FnosAdminOrganizer.create({
    state: { pagination: { organizerTasks: {}, organizerRuns: {} }, organizerDetailPaging: {} },
    getElement: id => id === 'adminDrawerBody' ? drawer : null,
    escapeHtml: FnosUI.escapeHtml, statusPill: value => value, formatBytes: value => String(value),
    normalizePagination: (_value, fallback) => fallback,
    toast: (message, tone) => messages.push({ message, tone }),
    confirmDialog: async options => { confirmations.push(options); return confirmed; },
    withButtonBusy: async (_button, _label, callback) => callback(),
    openDrawer: (_title, html) => {
        opened++;
        rendered = html;
        const visibleIds = new Set(Array.from(html.matchAll(/data-organizer-mapping-row="([0-9]+)"/g), match => match[1]));
        rows = task.mappings.filter(mapping => visibleIds.has(String(mapping.id))).map(mapping => mappingRow(mapping));
    },
    closeDrawer: () => { closed++; },
    api: async (path, options = {}) => {
        requests.push({ path, method: options.method || 'GET', body: options.body });
        if (options.method === 'PATCH') {
            if (saveFailure === 'network') throw new Error('offline');
            if (saveFailure === 'validation') return { success: false, message: 'mapping conflict' };
            const mapping = task.mappings.find(item => String(item.id) === path.split('/').at(-1));
            Object.assign(mapping, JSON.parse(options.body));
            if (recomputeTarget) mapping.target_path = '/library/server-recomputed.mp4';
            return { success: true };
        }
        if (options.method === 'POST') return { success: true, queued: true };
        if (path === '/api/admin/organizer/tasks/7') return { task };
        return { items: [], status: {} };
    },
});
function field(key, rowIndex = 0) { return rows[rowIndex].fields.find(item => item.dataset.mapField === key); }
function snapshot() { return { requests: [...requests], messages: [...messages], confirmations: [...confirmations], opened, closed }; }
(async () => {
""" + body + """
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "ui-core.js"), str(ROOT / "static" / "admin-organizer.js")],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        return json.loads(completed.stdout)

    def test_unsaved_ad_choice_blocks_apply_and_retry_without_losing_selection(self) -> None:
        result = self.run_ui("""
field('status').value = 'delete_ad';
for (const action of ['apply', 'retry']) {
    for (const keepDrawer of [true, false]) await ui.organizerTaskAction(7, action, keepDrawer);
}
process.stdout.write(JSON.stringify({ ...snapshot(), selection: field('status').value }));
""")
        self.assertEqual(result["requests"], [])
        self.assertEqual(result["confirmations"], [])
        self.assertEqual((result["opened"], result["closed"]), (0, 0))
        self.assertEqual(result["selection"], "delete_ad")
        self.assertEqual(len(result["messages"]), 4)
        for message in result["messages"]:
            self.assertIn("保存本条", message["message"])
            self.assertEqual(message["tone"], "warning")

    def test_saved_ad_choice_remains_visible_and_can_be_confirmed(self) -> None:
        result = self.run_ui("""
field('status').value = 'delete_ad';
await ui.saveOrganizerMapping(7, 9);
const saved = { ...snapshot(), rendered, selection: field('status').value };
await ui.organizerTaskAction(7, 'apply', true);
process.stdout.write(JSON.stringify({ ...snapshot(), saved }));
""")
        self.assertEqual(result["saved"]["selection"], "delete_ad")
        self.assertIn('data-organizer-mapping-task="7"', result["saved"]["rendered"])
        self.assertIn('value="delete_ad" selected', result["saved"]["rendered"])
        self.assertIn('data-organizer-drawer-action="apply"', result["saved"]["rendered"])
        mutations = [request for request in result["requests"] if request["method"] in {"POST", "PATCH"}]
        self.assertEqual([request["method"] for request in mutations], ["PATCH", "POST"])
        self.assertEqual(json.loads(mutations[0]["body"])["status"], "delete_ad")
        self.assertEqual(mutations[1]["path"], "/api/admin/organizer/tasks/7/apply")
        self.assertEqual(len(result["confirmations"]), 1)
        self.assertIn("彻底删除", result["confirmations"][0]["message"])

    def test_cancelling_confirmation_does_not_submit_or_refresh(self) -> None:
        result = self.run_ui("""
confirmed = false;
await ui.organizerTaskAction(7, 'apply', true);
process.stdout.write(JSON.stringify(snapshot()));
""")
        self.assertEqual(result["requests"], [])
        self.assertEqual((result["opened"], result["closed"]), (0, 0))
        self.assertEqual(len(result["confirmations"]), 1)

    def test_saving_one_row_preserves_other_unsaved_choices_without_approving_them(self) -> None:
        result = self.run_ui("""
task.mappings.push({ ...task.mappings[0], id: 10, source_path: '/staging/ad2.mp4' });
rows.push(mappingRow(task.mappings[1]));
field('status').value = 'delete_ad';
field('status', 1).value = 'delete_ad';
field('target_path', 1).value = '/library/unsaved-custom.mp4';
recomputeTarget = true;
await ui.saveOrganizerMapping(7, 9);
const afterFirst = { selections: rows.map(row => row.fields.find(item => item.dataset.mapField === 'status').value),
    savedTarget: field('target_path').value, otherTarget: field('target_path', 1).value,
    otherChangeEvent: field('status', 1).lastEvent, persistedOtherStatus: task.mappings[1].status };
await ui.organizerTaskAction(7, 'apply', true);
const blocked = snapshot();
recomputeTarget = false;
await ui.saveOrganizerMapping(7, 10);
await ui.organizerTaskAction(7, 'apply', true);
process.stdout.write(JSON.stringify({ ...snapshot(), afterFirst, blocked }));
""")
        self.assertEqual(result["afterFirst"]["selections"], ["delete_ad", "delete_ad"])
        self.assertEqual(result["afterFirst"]["savedTarget"], "/library/server-recomputed.mp4")
        self.assertEqual(result["afterFirst"]["otherTarget"], "/library/unsaved-custom.mp4")
        self.assertEqual(result["afterFirst"]["otherChangeEvent"], "change")
        self.assertEqual(result["afterFirst"]["persistedOtherStatus"], "need_edit")
        self.assertFalse(any(request["method"] == "POST" for request in result["blocked"]["requests"]))
        self.assertEqual(result["blocked"]["confirmations"], [])
        mutations = [request for request in result["requests"] if request["method"] in {"PATCH", "POST"}]
        self.assertEqual([request["method"] for request in mutations], ["PATCH", "PATCH", "POST"])
        self.assertEqual(json.loads(mutations[1]["body"])["status"], "delete_ad")
        self.assertEqual(json.loads(mutations[1]["body"])["target_path"], "/library/unsaved-custom.mp4")

    def test_save_validation_and_network_failures_preserve_choices_without_refresh(self) -> None:
        result = self.run_ui("""
field('status').value = 'delete_ad';
field('title').value = 'Unsaved title';
for (const failure of ['validation', 'network']) {
    saveFailure = failure;
    await ui.saveOrganizerMapping(7, 9);
}
process.stdout.write(JSON.stringify({ ...snapshot(), selection: field('status').value, title: field('title').value }));
""")
        self.assertEqual([request["method"] for request in result["requests"]], ["PATCH", "PATCH"])
        self.assertEqual((result["opened"], result["closed"]), (0, 0))
        self.assertEqual(result["selection"], "delete_ad")
        self.assertEqual(result["title"], "Unsaved title")
        self.assertEqual([message["message"] for message in result["messages"] if message["tone"] == "error"],
                         ["mapping conflict", "offline"])

    def test_unsaved_ready_row_survives_being_pushed_past_preview_limit(self) -> None:
        result = self.run_ui("""
for (let index = 0; index < 21; index++) {
    task.mappings.push({ ...task.mappings[0], id: 10 + index, status: 'ready', source_path: `/staging/${index}.mp4` });
}
await ui.showOrganizerTask(7);
const pendingRow = rows.find(row => row.dataset.organizerMappingRow === '29');
pendingRow.fields.find(item => item.dataset.mapField === 'status').value = 'delete_ad';
field('status').value = 'ready';
await ui.saveOrganizerMapping(7, 9);
const retained = rows.find(row => row.dataset.organizerMappingRow === '29');
const afterSave = { rowIds: rows.map(row => row.dataset.organizerMappingRow), rendered,
    pendingStatus: retained?.fields.find(item => item.dataset.mapField === 'status').value ?? null };
await ui.organizerTaskAction(7, 'apply', true);
process.stdout.write(JSON.stringify({ ...snapshot(), afterSave }));
""")
        self.assertEqual(result["afterSave"]["pendingStatus"], "delete_ad")
        self.assertEqual(len(result["afterSave"]["rowIds"]), 21)
        self.assertNotIn("30", result["afterSave"]["rowIds"])
        self.assertIn("未保存", result["afterSave"]["rendered"])
        self.assertFalse(any(request["method"] == "POST" for request in result["requests"]))
        self.assertEqual(result["confirmations"], [])
        self.assertIn("保存本条", result["messages"][-1]["message"])

    def test_text_edit_is_blocked_but_reverting_and_other_task_edits_do_not_block(self) -> None:
        result = self.run_ui("""
field('target_path').value = '/library/new-name.mp4';
await ui.organizerTaskAction(7, 'apply', false);
const blocked = snapshot();
field('target_path').value = field('target_path').defaultValue;
rows.push(mappingRow({ ...task.mappings[0], id: 10 }, 8));
field('status', 1).value = 'delete_ad';
await ui.organizerTaskAction(7, 'apply', false);
process.stdout.write(JSON.stringify({ ...snapshot(), blocked }));
""")
        self.assertEqual(result["blocked"]["requests"], [])
        self.assertEqual(result["blocked"]["confirmations"], [])
        self.assertEqual(len([request for request in result["requests"] if request["method"] == "POST"]), 1)
        self.assertEqual(len(result["confirmations"]), 1)


if __name__ == "__main__":
    unittest.main()
