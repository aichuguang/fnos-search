from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for the UI regression")
class RequestManualReviewUiTests(unittest.TestCase):
    def run_ui(self, body: str) -> dict:
        script = r"""
globalThis.window = globalThis;
globalThis.renderListPagers = () => {};
globalThis.mountOrganizerPagedList = () => {};
require(process.argv[1]);
require(process.argv[2]);
class Element {
    constructor(attributes = {}, text = '') {
        this.attributes = { ...attributes };
        this.dataset = Object.fromEntries(Object.entries(attributes).filter(([key]) => key.startsWith('data-'))
            .map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase()), value]));
        this.listeners = new Map();
        this.disabled = false;
        this.value = '';
        this.innerHTML = text;
    }
    set innerHTML(value) {
        this.html = value;
        this.buttons = Array.from(value.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g), match => {
            const attributes = Object.fromEntries(Array.from(match[1].matchAll(/([\w-]+)="([^"]*)"/g), item => [item[1], item[2]]));
            return new Element(attributes, match[2]);
        });
    }
    get innerHTML() { return this.html; }
    set textContent(value) { this.innerHTML = value; }
    get textContent() { return this.html; }
    getAttribute(key) { return this.attributes[key] ?? null; }
    setAttribute(key, value) { this.attributes[key] = value; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(kind, listener) { this.listeners.set(kind, listener); }
    querySelectorAll(selector) {
        const match = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
        if (!match) return [];
        return this.buttons.filter(button => button.getAttribute(match[1]) !== null
            && (match[2] === undefined || button.getAttribute(match[1]) === match[2]));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    async click() {
        if (this.disabled) return null;
        return this.listeners.get('click')?.({ currentTarget: this, target: this });
    }
}
const elements = new Map();
const getElement = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
};
const job = {
    id: 14, title: 'Example', status: 'review', category: 'movie',
    official_save_path: '/source/staging/14', target_path: '/destination/14',
    transfer_execution: {
        phase: 'manual_review', row_version: 9,
        review: { required: true, reason: 'Submission receipt missing', resume_phase: 'native_submitting',
            can_resume: false, can_confirm_not_submitted: true, guidance: 'Inspect the provider before deciding.' },
    },
};
const request = { id: 3, job_id: 14, request_token: 'request-3', title: 'Example', category: 'movie', status: 'review' };
const state = {
    requests: [request], jobs: [job], overviewJobs: [], dashboardSummary: {},
    pagination: { requests: { page: 1, per_page: 20 }, jobs: { page: 1, per_page: 20 } },
};
const requests = [];
const messages = [];
const prompts = [];
const confirmations = [];
let note = 'Checked source and destination folders and active/history provider tasks at 10:30; no execution found.';
let confirmed = true;
let mutationResult = { success: true };
let networkFailure = false;
let mutationErrorData = null;
let refreshFailure = false;
let detailFailureAfterMutation = false;
let promptGate = null;
let mutationGate = null;
let opened = 0;
let closed = 0;
const ui = FnosAdminJobs.create({
    state, getElement, escapeHtml: FnosUI.escapeHtml, icon: value => value,
    statusPill: value => `<span>${FnosUI.escapeHtml(value)}</span>`, statusText: value => value,
    formatDate: value => value || '', normalizePagination: (_value, fallback) => fallback,
    renderTimelineRawData: () => '', loadRclone: async () => {},
    toast: (message, tone) => messages.push({ message, tone }),
    promptDialog: async options => { prompts.push(options); if (promptGate) await promptGate; return note; },
    confirmDialog: async options => { confirmations.push(options); return confirmed; },
    openDrawer: (_title, html) => { opened++; getElement('adminDrawerBody').innerHTML = html; },
    closeDrawer: () => { closed++; },
    api: async (path, options = {}) => {
        requests.push({ path, method: options.method || 'GET', body: options.body, allowFailure: options.allowFailure });
        if (options.method === 'POST') {
            if (mutationGate) await mutationGate;
            if (networkFailure) throw new Error('network unavailable');
            if (mutationErrorData) {
                const error = new Error(mutationErrorData.message);
                error.status = 503;
                error.data = mutationErrorData;
                throw error;
            }
            return mutationResult;
        }
        if (path === '/api/admin/requests/3') return { success: true, request, job, events: [] };
        if (path === '/api/admin/jobs/14') {
            if (detailFailureAfterMutation && requests.some(item => item.method === 'POST')) throw new Error('detail refresh unavailable');
            return { success: true, job };
        }
        if (refreshFailure) throw new Error('refresh unavailable');
        if (path === '/api/admin/dashboard') return { summary: { recent_jobs: [], status_counts: {}, guest_request_status_counts: {} } };
        return { items: [] };
    },
});
function button(selector) { return getElement('adminDrawerBody').querySelector(selector); }
function snapshot() {
    return { requests, messages, prompts, confirmations, opened, closed, detail: getElement('adminDrawerBody').innerHTML };
}
(async () => {
""" + body + r"""
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "ui-core.js"), str(ROOT / "static" / "admin-jobs.js")],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        return json.loads(completed.stdout)

    def test_review_rows_and_drawer_open_existing_job_instead_of_approving_again(self) -> None:
        result = self.run_ui("""
ui.renderRequests();
const list = getElement('adminRequestsBody').innerHTML;
await getElement('adminRequestsBody').querySelector('[data-request-job]').click();
await ui.showRequestDetail(3);
const drawer = getElement('adminDrawerBody').innerHTML;
await button('[data-drawer-job]').click();
const pending = ui.requestActionButtons({ id: 8, status: 'pending_review' });
const linkedPending = ui.requestActionButtons({ id: 8, status: 'pending_review', job_id: 14 });
const risks = ['review', 'manual_review', 'provider_submitting'].map(status => ({
    risk: ui.riskPill({ ...request, status }), suggestion: ui.suggestionText({ ...request, status }),
}));
process.stdout.write(JSON.stringify({ ...snapshot(), list, drawer, pending, linkedPending, risks }));
""")
        for surface in (result["list"], result["drawer"]):
            self.assertIn("处理确认", surface)
            self.assertNotIn("低风险", surface)
            self.assertNotIn("可自动通过", surface)
            self.assertNotIn("data-request-approve", surface)
            self.assertNotIn("data-drawer-approve", surface)
        self.assertIn('data-request-approve="8"', result["pending"])
        self.assertNotIn("data-request-approve", result["linkedPending"])
        self.assertEqual([request["path"] for request in result["requests"]], [
            "/api/admin/jobs/14", "/api/admin/requests/3", "/api/admin/jobs/14",
        ])
        for item in result["risks"]:
            self.assertNotIn("低风险", item["risk"])
            self.assertNotIn("可自动通过", item["suggestion"])

    def test_unknown_submission_shows_evidence_instructions_not_generic_retry(self) -> None:
        result = self.run_ui("""
await ui.showJobDetail(14);
process.stdout.write(JSON.stringify(snapshot()));
""")
        detail = result["detail"]
        self.assertIn("Submission receipt missing", detail)
        self.assertIn("Inspect the provider before deciding.", detail)
        self.assertIn("源端暂存", detail)
        self.assertIn("/source/staging/14", detail)
        self.assertIn("/destination/14", detail)
        self.assertIn("网盘的任务列表", detail)
        self.assertIn("重复转存", detail)
        self.assertIn("保留原任务和相关证据", detail)
        self.assertIn('data-job-review-action="confirm_not_submitted"', detail)
        self.assertNotIn('data-job-review-action="resume"', detail)
        self.assertNotIn("data-job-retry-now", detail)
        self.assertNotIn("重试整个任务", detail)

    def test_confirmed_resubmission_sends_version_and_note_and_refreshes_original_job(self) -> None:
        result = self.run_ui("""
await ui.showJobDetail(14);
const clicked = button('[data-job-review-action="confirm_not_submitted"]');
await clicked.click();
process.stdout.write(JSON.stringify({ ...snapshot(), disabled: clicked.disabled, busy: clicked.getAttribute('aria-busy') }));
""")
        mutations = [request for request in result["requests"] if request["method"] == "POST"]
        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0]["path"], "/api/admin/jobs/14/review")
        body = json.loads(mutations[0]["body"])
        self.assertEqual(body["action"], "confirm_not_submitted")
        self.assertIs(body["confirmed"], True)
        self.assertEqual(body["expected_row_version"], 9)
        self.assertIn("Checked source", body["note"])
        self.assertTrue(mutations[0]["allowFailure"])
        prompt = result["prompts"][0]
        self.assertTrue(prompt["required"])
        self.assertEqual(prompt["defaultValue"], "")
        self.assertIn("/source/staging/14", prompt["message"])
        self.assertIn("/destination/14", prompt["message"])
        self.assertIn("任务列表", prompt["message"])
        self.assertIn("已核对不存在本次文件或进行中的任务，允许重新提交", result["confirmations"][0]["message"])
        paths = [request["path"] for request in result["requests"]]
        self.assertEqual(paths.count("/api/admin/jobs/14"), 2)
        self.assertTrue(any(path.startswith("/api/admin/requests?") for path in paths))
        self.assertTrue(any(path.startswith("/api/admin/jobs?") for path in paths))
        self.assertIn("/api/admin/dashboard", paths)
        self.assertEqual(result["closed"], 0)
        self.assertTrue(result["disabled"])
        self.assertIsNone(result["busy"])
        self.assertEqual(result["messages"][0]["tone"], "success")

    def test_empty_note_and_cancelling_either_dialog_never_submit(self) -> None:
        for note_value, confirmed, confirmation_count in ((None, True, 0), ("   ", True, 0), ("checked", False, 1)):
            with self.subTest(note=note_value, confirmed=confirmed):
                result = self.run_ui("""
note = %s;
confirmed = %s;
await ui.showJobDetail(14);
const clicked = button('[data-job-review-action="confirm_not_submitted"]');
await clicked.click();
process.stdout.write(JSON.stringify({ ...snapshot(), disabled: clicked.disabled }));
""" % (json.dumps(note_value), json.dumps(confirmed)))
                self.assertFalse(any(request["method"] == "POST" for request in result["requests"]))
                self.assertEqual(len(result["confirmations"]), confirmation_count)
                self.assertEqual((result["opened"], result["closed"]), (1, 0))
                self.assertFalse(result["disabled"])

    def test_business_and_network_failures_preserve_detail_and_restore_button(self) -> None:
        for network_failure in (False, True):
            with self.subTest(network_failure=network_failure):
                result = self.run_ui("""
mutationResult = { success: false, message: 'Version conflict; reload before deciding.' };
networkFailure = %s;
await ui.showJobDetail(14);
const clicked = button('[data-job-review-action="confirm_not_submitted"]');
const before = clicked.innerHTML;
await clicked.click();
process.stdout.write(JSON.stringify({ ...snapshot(), disabled: clicked.disabled, busy: clicked.getAttribute('aria-busy'), restored: clicked.innerHTML === before }));
""" % json.dumps(network_failure))
                self.assertEqual((result["opened"], result["closed"]), (1, 0))
                self.assertEqual(len(result["requests"]), 2)
                self.assertEqual(result["messages"][-1]["tone"], "error")
                self.assertFalse(any(message["tone"] == "success" for message in result["messages"]))
                self.assertFalse(result["disabled"])
                self.assertIsNone(result["busy"])
                self.assertTrue(result["restored"])

    def test_double_click_and_second_button_cannot_duplicate_dialog_or_request(self) -> None:
        result = self.run_ui("""
let releasePrompt;
let releaseMutation;
promptGate = new Promise(resolve => { releasePrompt = resolve; });
mutationGate = new Promise(resolve => { releaseMutation = resolve; });
await ui.showJobDetail(14);
const clicked = button('[data-job-review-action="confirm_not_submitted"]');
const first = clicked.click();
await clicked.click();
await ui.reviewJob(job, 'confirm_not_submitted', new Element());
const duringPrompt = { disabled: clicked.disabled, busy: clicked.getAttribute('aria-busy'), prompts: prompts.length };
releasePrompt();
await new Promise(resolve => setImmediate(resolve));
await ui.reviewJob(job, 'confirm_not_submitted', new Element());
const duringMutation = { disabled: clicked.disabled, mutations: requests.filter(item => item.method === 'POST').length };
releaseMutation();
await first;
process.stdout.write(JSON.stringify({ ...snapshot(), duringPrompt, duringMutation, disabled: clicked.disabled }));
""")
        self.assertEqual(result["duringPrompt"], {"disabled": True, "busy": "true", "prompts": 1})
        self.assertEqual(result["duringMutation"], {"disabled": True, "mutations": 1})
        self.assertEqual(len(result["prompts"]), 1)
        self.assertEqual(len(result["confirmations"]), 1)
        self.assertTrue(result["disabled"])

    def test_resume_uses_existing_retry_only_after_confirmation(self) -> None:
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                result = self.run_ui("""
Object.assign(job.transfer_execution.review, { can_resume: true, can_confirm_not_submitted: false, resume_phase: 'target_verifying' });
confirmed = %s;
await ui.showJobDetail(14);
const before = getElement('adminDrawerBody').innerHTML;
await button('[data-job-review-action="resume"]').click();
process.stdout.write(JSON.stringify({ ...snapshot(), before }));
""" % json.dumps(confirmed))
                self.assertIn('data-job-review-action="resume"', result["before"])
                self.assertNotIn('data-job-review-action="confirm_not_submitted"', result["before"])
                self.assertNotIn("data-job-retry-now", result["before"])
                self.assertEqual(result["prompts"], [])
                self.assertIn("保留已有回执", result["confirmations"][0]["message"])
                mutations = [request for request in result["requests"] if request["method"] == "POST"]
                self.assertEqual(len(mutations), int(confirmed))
                if confirmed:
                    self.assertEqual(mutations[0]["path"], "/api/admin/jobs/14/retry")
                    self.assertEqual(json.loads(mutations[0]["body"]), {})

    def test_retry_business_failure_is_never_reported_as_success(self) -> None:
        result = self.run_ui("""
mutationResult = { success: false, message: 'Submission result still unknown.' };
await ui.retryJob(14);
process.stdout.write(JSON.stringify(snapshot()));
""")
        self.assertEqual(len(result["requests"]), 1)
        self.assertEqual(result["requests"][0]["path"], "/api/admin/jobs/14/retry")
        self.assertTrue(result["requests"][0]["allowFailure"])
        self.assertEqual(result["messages"], [{"message": "Submission result still unknown.", "tone": "error"}])

    def test_backend_gates_and_missing_version_fail_closed(self) -> None:
        result = self.run_ui("""
Object.assign(job.transfer_execution.review, { can_resume: false, can_confirm_not_submitted: false });
await ui.showJobDetail(14);
const blocked = getElement('adminDrawerBody').innerHTML;
await ui.reviewJob(job, 'confirm_not_submitted');
await ui.reviewJob(job, 'resume');
job.transfer_execution.review.can_confirm_not_submitted = true;
delete job.transfer_execution.row_version;
await ui.reviewJob(job, 'confirm_not_submitted');
for (const version of [0, -1]) {
    job.transfer_execution.row_version = version;
    await ui.reviewJob(job, 'confirm_not_submitted');
}
process.stdout.write(JSON.stringify({ ...snapshot(), blocked }));
""")
        self.assertNotIn("data-job-review-action", result["blocked"])
        self.assertNotIn("data-job-retry-now", result["blocked"])
        self.assertIn("保留原任务和相关证据", result["blocked"])
        self.assertEqual(len(result["requests"]), 1)
        self.assertEqual(result["prompts"], [])
        self.assertEqual(result["confirmations"], [])
        self.assertEqual(len(result["messages"]), 5)

    def test_note_length_is_checked_before_the_final_confirmation(self) -> None:
        result = self.run_ui("""
note = '核'.repeat(1001);
await ui.showJobDetail(14);
const clicked = button('[data-job-review-action="confirm_not_submitted"]');
await clicked.click();
process.stdout.write(JSON.stringify({ ...snapshot(), disabled: clicked.disabled }));
""")
        self.assertEqual(result["confirmations"], [])
        self.assertFalse(any(request["method"] == "POST" for request in result["requests"]))
        self.assertIn("1000", result["messages"][-1]["message"])
        self.assertFalse(result["disabled"])

    def test_frozen_directories_are_used_in_review_and_prompt_with_chinese_phases(self) -> None:
        result = self.run_ui(r"""
job.transfer_execution.review.paths = [
    { label: '来源暂存目录', namespace: 'quark:source', path: '/frozen/source' },
    { label: '目标暂存目录', namespace: 'mobile:target', path: '/frozen/target' },
    { label: '最终目录', namespace: 'mobile:target', path: '/frozen/library' },
    { label: '中继目录', namespace: 'local', path: '/frozen/relay' },
];
await ui.showJobDetail(14);
const review = getElement('adminDrawerBody').innerHTML.match(/<section class="notice-box warning job-manual-review">([\s\S]*?)<\/section>/)[1];
note = null;
await button('[data-job-review-action="confirm_not_submitted"]').click();
process.stdout.write(JSON.stringify({ ...snapshot(), review }));
""")
        for surface in (result["review"], result["prompts"][0]["message"]):
            for path in ("/frozen/source", "/frozen/target", "/frozen/library", "/frozen/relay"):
                self.assertIn(path, surface)
            self.assertIn("来源暂存目录", surface)
            self.assertIn("quark:source", surface)
            self.assertNotIn("/source/staging/14", surface)
            self.assertNotIn("/destination/14", surface)
        self.assertIn("等待人工复核", result["review"])
        self.assertIn("提交原生网盘任务", result["review"])
        self.assertNotIn("manual_review", result["review"])
        self.assertNotIn("native_submitting", result["review"])

    def test_recovered_failure_refreshes_detail_and_invalidates_old_confirmation(self) -> None:
        for thrown in (False, True):
            for detail_failure in (False, True):
                with self.subTest(thrown=thrown, detail_failure=detail_failure):
                    result = self.run_ui("""
const recovery = { success: false, recovered: true, message: 'Review recorded; worker queue unavailable.' };
if (%s) mutationErrorData = recovery;
else mutationResult = recovery;
detailFailureAfterMutation = %s;
await ui.showJobDetail(14);
const clicked = button('[data-job-review-action="confirm_not_submitted"]');
await clicked.click();
await clicked.click();
await ui.reviewJob(job, 'confirm_not_submitted');
process.stdout.write(JSON.stringify({ ...snapshot(), disabled: clicked.disabled, busy: clicked.getAttribute('aria-busy') }));
""" % (json.dumps(thrown), json.dumps(detail_failure)))
                    self.assertEqual(len([request for request in result["requests"] if request["method"] == "POST"]), 1)
                    self.assertEqual(len([request for request in result["requests"] if request["path"] == "/api/admin/jobs/14"]), 2)
                    self.assertEqual(result["messages"][0]["tone"], "error")
                    self.assertFalse(any(message["tone"] == "success" for message in result["messages"]))
                    self.assertTrue(result["disabled"])
                    self.assertIsNone(result["busy"])
                    self.assertEqual(result["closed"], 0)
                    if not detail_failure:
                        self.assertNotIn("data-job-review-action", result["detail"])

    def test_review_data_is_escaped_and_normal_job_keeps_existing_retry(self) -> None:
        result = self.run_ui("""
const marker = '<img src=x onerror="alert(1)">';
job.transfer_execution.review.reason = marker;
job.transfer_execution.review.guidance = [marker];
job.transfer_execution.review.resume_phase = marker;
job.official_save_path = marker;
await ui.showJobDetail(14);
const review = getElement('adminDrawerBody').innerHTML;
job.transfer_execution.review.required = false;
job.status = 'failed';
await ui.showJobDetail(14);
process.stdout.write(JSON.stringify({ ...snapshot(), review, marker, escaped: FnosUI.escapeHtml(marker) }));
""")
        self.assertIn(result["escaped"], result["review"])
        self.assertNotIn(result["marker"], result["review"])
        self.assertIn("data-job-retry-now", result["detail"])
        self.assertNotIn("data-job-review-action", result["detail"])

    def test_successful_mutation_with_refresh_failure_is_reported_accurately(self) -> None:
        result = self.run_ui("""
refreshFailure = true;
await ui.showJobDetail(14);
await button('[data-job-review-action="confirm_not_submitted"]').click();
process.stdout.write(JSON.stringify(snapshot()));
""")
        self.assertEqual([message["tone"] for message in result["messages"]], ["success", "warning"])
        self.assertIn("操作已受理", result["messages"][-1]["message"])
        self.assertEqual(result["opened"], 2)


if __name__ == "__main__":
    unittest.main()
