from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for the Quark selection regression")
class QuarkSelectionUiTests(unittest.TestCase):
    def run_ui(self, body: str) -> dict:
        script = r"""
globalThis.window = globalThis;
require(process.argv[1]);
require(process.argv[2]);
require(process.argv[3]);
require(process.argv[4]);
const decode = value => value.replaceAll('&quot;', '"').replaceAll('&#039;', "'").replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');
class Node {
    constructor(tag = 'div', attributes = {}, raw = '') {
        this.tagName = tag.toUpperCase();
        this.attributes = attributes;
        this.dataset = Object.fromEntries(Object.entries(attributes).filter(([key]) => key.startsWith('data-'))
            .map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase()), value]));
        this.checked = /\bchecked\b/.test(raw);
        this.disabled = /\bdisabled\b/.test(raw);
        this.value = attributes.value || '';
        this.listeners = new Map();
        this.nodes = [];
        this.html = '';
    }
    set innerHTML(html) {
        this.html = html;
        this.nodes = Array.from(html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/g), match => {
            const attributes = Object.fromEntries(Array.from(match[2].matchAll(/([\w-]+)="([^"]*)"/g), item => [item[1], decode(item[2])]));
            return new Node(match[1], attributes, match[2]);
        });
    }
    get innerHTML() { return this.html; }
    matches(selector) {
        if (selector.startsWith('#')) return this.attributes.id === selector.slice(1);
        const match = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
        if (match) return Object.hasOwn(this.attributes, match[1]) && (match[2] === undefined || this.attributes[match[1]] === match[2]);
        return this.tagName.toLowerCase() === selector;
    }
    querySelectorAll(selector) { return this.nodes.filter(node => selector.split(',').some(part => node.matches(part.trim()))); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return selector.split(',').some(part => this.matches(part.trim())) ? this : null; }
    addEventListener(type, callback) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(callback);
    }
    async trigger(type) {
        const event = { target: this, currentTarget: this, preventDefault() {}, stopPropagation() {} };
        await Promise.all((this.listeners.get(type) || []).map(callback => callback(event)));
    }
}
const drawer = new Node();
const publicRoot = new Node();
globalThis.document = {
    getElementById: id => id === 'adminDrawerBody' ? drawer : drawer.querySelector('#' + id),
    querySelector: selector => drawer.querySelector(selector),
};
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
const flush = () => new Promise(resolve => setImmediate(resolve));
const rootA = { fid: '000A', name: 'A', is_dir: true, can_expand: true };
const folderB = { fid: '000B', name: 'B', is_dir: true, can_expand: true };
const fileC = { fid: '000C', name: 'C.mkv', is_dir: false };
const fileD = { fid: '000D', name: 'D.mkv', is_dir: false };
const unselected = { fid: 'UNSELECTED', name: 'keep-out.txt', is_dir: false };
const publicState = {
    quarkSelections: {},
    filePreviews: {
        'r:000A': { open: true, success: true, items: [folderB, unselected] },
        'r:000B': { open: true, success: true, items: [fileC, fileD] },
    },
    details: { r: { detail: { inspection: { items: [rootA] } } } },
};
const publicUi = FnosPublicSubmit.create({
    state: publicState, getElement: () => null, escapeHtml: FnosUI.escapeHtml, icon: () => '',
    refreshPublicResourceViews: () => {},
    api: async () => { throw new Error('Public tree should use its supplied fixture cache'); },
});
function renderPublic() {
    publicRoot.innerHTML = publicUi.renderQuarkFilePreview('r', [rootA]);
    return publicRoot.innerHTML;
}
async function openPublicRoot() {
    renderPublic();
    await publicUi.handleQuarkFolderOpen(publicRoot.querySelectorAll('[data-quark-open]').find(node => node.dataset.quarkFid === rootA.fid));
}
function selectPublic(fid, checked = true) {
    renderPublic();
    const input = publicRoot.querySelectorAll('[data-quark-select]').find(node => node.dataset.quarkFid === fid);
    if (!input) throw new Error('Missing public checkbox for ' + fid);
    input.checked = checked;
    publicUi.handleQuarkSelectionChange(input);
}
const candidate = { id: 8, title: 'Test movie', media_type: 'movie', status: 'discovered' };
const folderC = { fid: 'DIR-C', name: 'C', is_dir: true, can_expand: true };
const leafD = { fid: 'LEAF-D', name: 'D.mkv', is_dir: false };
const leafE = { fid: 'LEAF-E', name: 'E.mkv', is_dir: false };
const detailResponse = { success: true, detail: { provider_kind: 'quark', title: 'Test movie', inspection: { items: [rootA] } } };
const folderItems = { '000A': [folderB, unselected], '000B': [folderC], 'DIR-C': [leafD, leafE] };
const requests = [];
const trendingUi = FnosAdminTrending.create({
    state: { searchProviders: [{ key: 'pansou', name: 'Pansou', enabled: true, configured: true }] },
    getElement: id => document.getElementById(id), escapeHtml: FnosUI.escapeHtml, icon: () => '', formatDate: () => '',
    toast: () => {}, openDrawer: (_title, html) => { drawer.innerHTML = html; }, confirmDialog: async () => true,
    api: async (path, options = {}) => {
        requests.push({ path, method: options.method || 'GET', body: options.body });
        if (path.endsWith('/search')) return { success: true, candidate, items: [{ public_id: 'r', provider_kind: 'quark', title: 'Test movie' }] };
        if (path.endsWith('/detail')) return detailResponse;
        if (path.includes('/files?')) return { success: true, items: folderItems[new URL(path, 'http://local.invalid').searchParams.get('fid')] || [] };
        if (path.endsWith('/import')) return { success: false, message: 'Payload captured by isolated test' };
        throw new Error('Unexpected fixture route: ' + path);
    },
});
async function workbenchClick(selector, predicate = () => true) {
    const node = drawer.querySelectorAll(selector).find(predicate);
    if (!node) throw new Error('Missing workbench control ' + selector);
    await node.trigger('click');
    await flush();
}
async function selectWorkbench(fid) {
    const input = drawer.querySelectorAll('[data-workbench-file-key]').find(node => node.dataset.workbenchFileKey === fid);
    if (!input) throw new Error('Missing workbench checkbox for ' + fid);
    input.checked = true;
    await input.trigger('change');
}
function imports() { return requests.filter(item => item.path.endsWith('/import')).map(item => JSON.parse(item.body)); }
(async () => {
""" + body + r"""
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "ui-core.js"),
             str(ROOT / "static" / "resource-search-workbench.js"), str(ROOT / "static" / "public-submit.js"),
             str(ROOT / "static" / "admin-trending.js")],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        return json.loads(completed.stdout)

    def test_nested_file_selection_keeps_base_ids_paths_and_unselected_sibling_out(self) -> None:
        result = self.run_ui("""
await openPublicRoot();
selectPublic(fileC.fid);
selectPublic(fileD.fid);
renderPublic();
const parent = publicRoot.querySelectorAll('[data-quark-select]').find(node => node.dataset.quarkFid === rootA.fid);
process.stdout.write(JSON.stringify({ state: publicUi.quarkSelectionState('r'), payload: publicUi.quarkSelectionPayload('r'), parentChecked: parent.checked }));
""")
        selection = result["payload"]["quark_selection"]
        self.assertEqual(selection["mode"], "subdir_items")
        self.assertEqual(selection["base_dir"]["fid"], "000A")
        self.assertEqual([(item["fid"], item["path"]) for item in selection["selected_items"]], [
            ("000C", "B/C.mkv"), ("000D", "B/D.mkv"),
        ])
        self.assertEqual(result["state"]["rootDirs"], {})
        self.assertFalse(result["parentChecked"])
        self.assertNotIn("selected_dirs", selection)
        self.assertNotIn("UNSELECTED", json.dumps(selection))

    def test_all_visible_children_even_a_single_file_do_not_expand_to_hidden_content(self) -> None:
        for visible_count in (1, 2):
            with self.subTest(visible_count=visible_count):
                result = self.run_ui("""
const visible = [fileC, fileD].slice(0, %d);
publicState.filePreviews['r:000A'] = { open: true, success: true, items: visible, total: 3, has_more: true };
await openPublicRoot();
for (const item of visible) selectPublic(item.fid);
const covered = publicUi.quarkSubdirSelectionCoversParent('r', publicUi.quarkSelectionState('r'));
const collapsed = publicUi.collapseQuarkSubdirSelectionIfComplete('r');
process.stdout.write(JSON.stringify({ covered, collapsed, state: publicUi.quarkSelectionState('r'), payload: publicUi.quarkSelectionPayload('r') }));
""" % visible_count)
                self.assertTrue(result["covered"])
                self.assertFalse(result["collapsed"])
                self.assertEqual(result["state"]["mode"], "subdir_items")
                self.assertEqual(result["state"]["baseDir"]["fid"], "000A")
                selection = result["payload"]["quark_selection"]
                self.assertEqual(selection["mode"], "subdir_items")
                self.assertEqual(len(selection["selected_items"]), visible_count)
                self.assertNotIn("selected_dirs", selection)

    def test_payload_collection_never_rewrites_a_complete_cached_subselection(self) -> None:
        result = self.run_ui("""
publicState.filePreviews['r:000A'].items = [fileC];
await openPublicRoot();
publicState.quarkSelections.r.items = { [fileC.fid]: { ...fileC, path: fileC.name } };
const before = JSON.stringify(publicState.quarkSelections.r);
const payload = publicUi.quarkSelectionPayload('r');
process.stdout.write(JSON.stringify({ payload, unchanged: JSON.stringify(publicState.quarkSelections.r) === before }));
""")
        selection = result["payload"]["quark_selection"]
        self.assertEqual(selection["mode"], "subdir_items")
        self.assertEqual(selection["selected_items"][0]["fid"], "000C")
        self.assertTrue(result["unchanged"])

    def test_explicit_root_checkbox_and_whole_folder_action_still_select_directory(self) -> None:
        result = self.run_ui("""
await openPublicRoot();
selectPublic(fileC.fid);
selectPublic(rootA.fid);
const explicitRoot = publicUi.quarkSelectionPayload('r');
await openPublicRoot();
await publicUi.handleQuarkSelectionAction({ dataset: { quarkPublicId: 'r', quarkAction: 'subdir-all' } });
process.stdout.write(JSON.stringify({ explicitRoot, explicitWholeFolder: publicUi.quarkSelectionPayload('r') }));
""")
        for key in ("explicitRoot", "explicitWholeFolder"):
            with self.subTest(action=key):
                selection = result[key]["quark_selection"]
                self.assertEqual(selection["mode"], "root_dirs")
                self.assertEqual([item["fid"] for item in selection["selected_dirs"]], ["000A"])

    def test_workbench_nested_navigation_preserves_true_root_and_submission_context(self) -> None:
        result = self.run_ui("""
await trendingUi.searchCandidateResources(8);
await workbenchClick('[data-workbench-detail-id]');
for (const fid of ['000A', '000B', 'DIR-C']) {
    await workbenchClick('[data-workbench-folder-public-id]', node => node.dataset.workbenchFolderFid === fid);
}
const deepContext = JSON.parse(JSON.stringify(detailResponse.detail._workbench_selection_context));
await selectWorkbench('LEAF-D');
await selectWorkbench('LEAF-E');
await trendingUi.confirmCandidateImport(8, candidate, 'r');
await workbenchClick('[data-workbench-root-public-id]');
const rootItems = detailResponse.detail.inspection.items;
const contextCleared = !detailResponse.detail._workbench_selection_context;
const checkedAfterReturn = drawer.querySelectorAll('[data-workbench-file-key]').filter(node => node.checked).map(node => node.dataset.workbenchFileKey);
await selectWorkbench('000A');
await trendingUi.confirmCandidateImport(8, candidate, 'r');
process.stdout.write(JSON.stringify({ deepContext, rootItems, contextCleared, checkedAfterReturn, imports: imports() }));
""")
        self.assertEqual(result["deepContext"]["base_dir"]["fid"], "DIR-C")
        self.assertEqual([item["fid"] for item in result["deepContext"]["root_items"]], ["000A"])
        self.assertEqual([item["fid"] for item in result["rootItems"]], ["000A"])
        self.assertTrue(result["contextCleared"])
        self.assertEqual(result["checkedAfterReturn"], [])
        nested, root = [item["quark_selection"] for item in result["imports"]]
        self.assertEqual(nested["mode"], "subdir_items")
        self.assertEqual(nested["base_dir"]["fid"], "DIR-C")
        self.assertEqual([item["fid"] for item in nested["selected_items"]], ["LEAF-D", "LEAF-E"])
        self.assertEqual(root["mode"], "root_dirs")
        self.assertEqual([item["fid"] for item in root["selected_dirs"]], ["000A"])


if __name__ == "__main__":
    unittest.main()
