from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RcloneUiHarness:
    def run_ui(self, body: str) -> dict:
        script = r"""
globalThis.window = globalThis;
require(process.argv[1]);
require(process.argv[2]);
const elements = new Map();
class Element {
    constructor(id = '') {
        this.id = id;
        this.innerHTML = '';
        this.textContent = '';
        this.value = '';
        this.hidden = false;
        this.open = false;
        this.scrollTop = 100;
        this.clientHeight = 100;
        this.scrollHeight = 200;
        this.attributes = {};
        this.dataset = {};
        this.listeners = {};
        const classes = new Set();
        this.classList = {
            add: value => classes.add(value),
            remove: value => classes.delete(value),
            contains: value => classes.has(value),
            toggle: (value, force) => {
                const enabled = force === undefined ? !classes.has(value) : force;
                if (enabled) classes.add(value); else classes.delete(value);
                return enabled;
            },
        };
    }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key] ?? null; }
    addEventListener(kind, callback) { this.listeners[kind] = callback; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    insertAdjacentElement(_position, element) { elements.set(element.id, element); }
}
for (const id of ['adminRcloneStatus', 'adminRcloneLogs', 'adminSystemLogs', 'adminRcloneRuns', 'adminTaskLogs',
    'taskLogKeyword', 'taskLogStatus', 'taskLogDateFrom', 'taskLogDateTo', 'adminRcloneServiceStatus',
    'adminTransferService', 'transferSearch', 'transferVisibilityHint', 'transferHistory', 'adminTransferHistory',
    'transferQueueCount', 'transferQueueSummary', 'transferCurrentTabCount', 'adminTransferHistoryJobsBtn',
    'transferAttentionTabCount', 'transferHistoryCount', 'adminRcloneStatusRefreshError',
    'adminRcloneServiceStatusRefreshError']) elements.set(id, new Element(id));
for (const [id, element] of elements) if (id.endsWith('RefreshError')) element.hidden = true;
elements.get('transferHistory').hidden = true;
const filterButtons = ['current', 'attention'].map(value => {
    const button = new Element();
    button.dataset.transferFilter = value;
    return button;
});
globalThis.document = {
    createElement: () => new Element(),
    querySelectorAll: selector => selector === '[data-transfer-filter]' ? filterButtons : [],
};
let intervalCallback = null;
let intervalMs = null;
globalThis.setInterval = (callback, milliseconds) => { intervalCallback = callback; intervalMs = milliseconds; return 1; };
const getElement = id => elements.get(id) || null;
const requests = [];
const messages = [];
const gates = {};
const failures = {};
let synchronousFailure = null;
let statusValue = { running: false, status: 'success', queue_count: 0, transfers: [] };
let logLines = ['initial download log'];
let systemLines = ['initial system log'];
let invalidLogs = false;
const state = { activeTab: 'rclone', rcloneLiveLoading: false, pagination: { rcloneRuns: {}, taskLogs: {} } };
const ui = FnosAdminRclone.create({
    state, getElement, escapeHtml: FnosUI.escapeHtml,
    statusPill: value => `<span>${FnosUI.escapeHtml(value)}</span>`, statusText: value => value,
    formatDate: value => value || '', normalizePagination: (_value, fallback) => fallback, renderPager: () => {},
    showJobDetail: async () => {}, loadOverview: async () => {},
    toast: (message, tone) => messages.push({ message, tone }),
    api: (path, options = {}) => {
        const channel = path === '/api/admin/rclone/status' ? 'status'
            : path.startsWith('/api/admin/rclone/logs?') ? 'logs'
            : path.startsWith('/api/admin/system/logs?') ? 'system'
            : path.startsWith('/api/admin/system/task-logs?') ? 'tasks' : 'runs';
        requests.push({ path, channel, ...options });
        if (synchronousFailure === channel) throw new Error('synchronous transport failure');
        return (async () => {
            if (gates[channel]) await gates[channel];
            if (failures[channel]) throw new Error(failures[channel]);
            if (channel === 'status') return { success: true, status: statusValue };
            if (channel === 'logs') return invalidLogs ? {} : { items: [...logLines] };
            if (channel === 'system') return { lines: [...systemLines] };
            return { items: [] };
        })();
    },
});
const transfer = {
    job_id: 15, run_id: 51, stage: 'upload', completed: false,
    updated_at: '2026-10-11 22:10:00', age_seconds: 3, stale: false,
    download: { state: 'completed', scope: 'file', filename: 'Example.mkv', percent: 100, bytes_done: 1048576, bytes_total: 1048576 },
    upload: { state: 'running', scope: 'file', filename: 'Example.mkv', percent: 45, bytes_done: 471859, bytes_total: 1048576, speed_bps: 262144, eta_seconds: 3 },
};
function snapshot() {
    return {
        status: getElement('adminRcloneStatus').innerHTML,
        history: getElement('adminTransferHistory').innerHTML,
        historyOpen: getElement('transferHistory').open,
        historyHidden: getElement('transferHistory').hidden,
        queueHidden: getElement('transferQueueSummary').hidden,
        service: getElement('adminRcloneServiceStatus').innerHTML,
        transferService: getElement('adminTransferService').innerHTML,
        counts: Object.fromEntries([...elements].filter(([id]) => id.startsWith('transfer') && id.endsWith('Count'))
            .map(([id, element]) => [id, String(element.textContent)])),
        filters: filterButtons.map(button => ({ value: button.dataset.transferFilter, ...button.attributes })),
        hint: getElement('transferVisibilityHint').textContent,
        logs: getElement('adminRcloneLogs').textContent,
        system: getElement('adminSystemLogs').textContent,
        notices: Object.fromEntries([...elements].filter(([id]) => id.endsWith('RefreshError')).map(([id, element]) => [id, { text: element.textContent, hidden: element.hidden }])),
        requests: [...requests], messages: [...messages],
    };
}
function metric(kind, html = getElement('adminRcloneStatus').innerHTML) {
    return html.match(new RegExp('<section class="rclone-transfer-metric" data-transfer-metric="' + kind + '"[\\s\\S]*?</section>'))?.[0] || '';
}
function jobIds(html = getElement('adminRcloneStatus').innerHTML) {
    return [...html.matchAll(/data-transfer-job-id="([^"]*)"/g)].map(match => match[1]);
}
function historyIds() { return jobIds(getElement('adminTransferHistory').innerHTML); }
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
""" + body + r"""
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "ui-core.js"), str(ROOT / "static" / "admin-rclone.js")],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        return json.loads(completed.stdout)


@unittest.skipUnless(shutil.which("node"), "Node is required for the live-progress UI regression")
class RcloneLiveProgressUiTests(RcloneUiHarness, unittest.TestCase):
    def test_download_success_and_idle_process_still_show_active_upload(self) -> None:
        result = self.run_ui("""
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), download: metric('download'), upload: metric('upload') }));
""")
        self.assertIn("下载进程空闲", result["service"])
        self.assertIn('data-transfer-completed="false"', result["status"])
        self.assertEqual(result["download"], "")
        self.assertIn('aria-valuenow="45"', result["upload"])
        self.assertIn("已完成", result["status"])
        self.assertIn("进行中", result["upload"])
        self.assertIn("单文件进度", result["upload"])
        self.assertIn("256 KiB/s", result["upload"])
        self.assertEqual(result["status"].count('class="rclone-transfer-metric"'), 1)
        self.assertNotIn('data-transfer-completed="true"', result["status"])

    def test_unknown_metrics_have_no_fabricated_percent_or_animated_progress(self) -> None:
        result = self.run_ui("""
transfer.download = { state: 'waiting', scope: 'file', percent: null, bytes_done: null, bytes_total: null };
transfer.upload = { state: 'running', scope: 'file', percent: NaN, speed_bps: null, eta_seconds: null };
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
const unknown = snapshot();
transfer.upload.percent = 0;
transfer.upload.bytes_done = 0;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ unknown, zero: metric('upload') }));
""")
        self.assertIn("等待真实进度反馈", result["unknown"]["status"])
        self.assertIn("速度 —", result["unknown"]["status"])
        self.assertIn("预计剩余 —", result["unknown"]["status"])
        self.assertNotIn("aria-valuenow", result["unknown"]["status"])
        self.assertNotIn("rclone-progress-fill", result["unknown"]["status"])
        self.assertNotIn("<progress", result["unknown"]["status"])
        self.assertIn('aria-valuenow="0"', result["zero"])
        self.assertIn("0 B / —", result["zero"])

    def test_hash_is_separate_from_upload_and_does_not_report_uploaded_bytes(self) -> None:
        result = self.run_ui("""
transfer.stage = 'hash';
transfer.hash = { state: 'running', scope: 'file', filename: 'Example.mkv', percent: 67, bytes_done: 700000, bytes_total: 1048576 };
transfer.upload = { state: 'waiting', scope: 'file', percent: null };
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), hash: metric('hash'), upload: metric('upload') }));
""")
        self.assertIn("秒传校验 / 上传准备", result["hash"])
        self.assertIn('aria-valuenow="67"', result["hash"])
        self.assertIn("不是已上传字节", result["hash"])
        self.assertEqual(result["upload"], "")
        self.assertEqual(result["status"].count('class="rclone-transfer-metric"'), 1)

    def test_only_completed_boolean_marks_job_complete_and_stale_retains_timestamp(self) -> None:
        result = self.run_ui("""
transfer.stage = 'completed';
transfer.upload.percent = 100;
transfer.stale = true;
transfer.age_seconds = 125;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
const pending = snapshot();
transfer.completed = true;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
const current = snapshot();
ui.setTransferHistoryOpen(true);
process.stdout.write(JSON.stringify({ pending, current, completed: snapshot() }));
""")
        self.assertIn("等待入库终态确认", result["pending"]["status"])
        self.assertIn("2026-10-11 22:10:00", result["pending"]["status"])
        self.assertIn("2 分 5 秒", result["pending"]["status"])
        self.assertIn("反馈较旧", result["pending"]["status"])
        self.assertNotIn("卡死", result["pending"]["status"])
        self.assertNotIn('data-transfer-completed="true"', result["pending"]["status"])
        self.assertNotIn('data-transfer-job-id="15"', result["current"]["status"])
        self.assertIn('data-transfer-completed="true"', result["completed"]["history"])
        self.assertIn("入库完成", result["completed"]["history"])
        self.assertNotIn("rclone-transfer-metric", result["completed"]["history"])
        self.assertNotIn("等待真实进度反馈", result["completed"]["history"])

    def test_instant_upload_completion_without_byte_metrics_is_not_reported_as_waiting(self) -> None:
        result = self.run_ui("""
transfer.stage = 'verifying';
transfer.completed = false;
transfer.upload = { state: 'completed', scope: 'file', percent: null, bytes_done: null, bytes_total: null };
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify(snapshot()));
""")
        self.assertIn("已确认完成，未提供字节统计", result["status"])
        self.assertNotIn('data-transfer-completed="true"', result["status"])
        self.assertNotIn('role="progressbar"', result["status"])

    def test_progress_text_is_escaped_and_invalid_numeric_values_are_not_styles(self) -> None:
        result = self.run_ui("""
const marker = '<img src=x onerror="alert(1)">';
Object.assign(transfer, { message: marker, job_id: marker, run_id: marker, updated_at: marker });
transfer.upload.filename = marker;
transfer.upload.percent = '45;position:fixed';
transfer.download.percent = 101;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), marker, escaped: FnosUI.escapeHtml(marker) }));
""")
        self.assertIn(result["escaped"], result["status"])
        self.assertNotIn(result["marker"], result["status"])
        self.assertNotIn("aria-valuenow", result["status"])
        self.assertNotIn("position:fixed", result["status"])

    def test_slow_status_and_system_do_not_block_logs_or_next_fast_poll(self) -> None:
        result = self.run_ui("""
let releaseStatus, releaseSystem;
gates.status = new Promise(resolve => { releaseStatus = resolve; });
gates.system = new Promise(resolve => { releaseSystem = resolve; });
getElement('adminRcloneStatus').innerHTML = 'previous status';
const first = ui.loadRcloneLive();
await flush();
const duringFirst = snapshot();
logLines = ['second live line'];
const second = ui.loadRcloneLive();
await flush();
const duringSecond = snapshot();
releaseStatus(); releaseSystem();
await Promise.all([first, second]);
process.stdout.write(JSON.stringify({ duringFirst, duringSecond, final: snapshot() }));
""")
        self.assertEqual(result["duringFirst"]["logs"], "initial download log")
        self.assertEqual(result["duringFirst"]["status"], "previous status")
        self.assertEqual(result["duringSecond"]["logs"], "second live line")
        requests = result["duringSecond"]["requests"]
        self.assertEqual(sum(item["channel"] == "status" for item in requests), 1)
        self.assertEqual(sum(item["channel"] == "system" for item in requests), 1)
        self.assertEqual(sum(item["channel"] == "logs" for item in requests), 2)
        self.assertEqual(result["final"]["system"], "initial system log")

    def test_initial_page_load_renders_logs_before_slow_status_and_task_history(self) -> None:
        result = self.run_ui("""
let releaseStatus, releaseTasks;
gates.status = new Promise(resolve => { releaseStatus = resolve; });
gates.tasks = new Promise(resolve => { releaseTasks = resolve; });
const initial = ui.loadRclone();
await flush();
const during = snapshot();
releaseStatus(); releaseTasks();
await initial;
process.stdout.write(JSON.stringify({ during, final: snapshot() }));
""")
        self.assertEqual(result["during"]["logs"], "initial download log")
        self.assertEqual(result["during"]["system"], "initial system log")
        self.assertEqual(result["during"]["status"], "")
        self.assertIn("下载进程空闲", result["final"]["service"])

    def test_failed_or_invalid_log_response_preserves_content_and_clears_notice_on_recovery(self) -> None:
        result = self.run_ui("""
await ui.loadRcloneLive();
failures.logs = 'log service temporarily unavailable';
failures.status = 'status temporarily unavailable';
systemLines = ['new system line'];
await ui.loadRcloneLive();
const failed = snapshot();
delete failures.logs; delete failures.status;
invalidLogs = true;
await ui.loadRcloneLive();
const malformed = snapshot();
invalidLogs = false; logLines = ['recovered live line'];
await ui.loadRcloneLive();
process.stdout.write(JSON.stringify({ failed, malformed, recovered: snapshot() }));
""")
        self.assertEqual(result["failed"]["logs"], "initial download log")
        self.assertEqual(result["failed"]["system"], "new system line")
        self.assertIn("已保留当前日志", result["failed"]["notices"]["adminRcloneLogsRefreshError"]["text"])
        self.assertFalse(result["failed"]["notices"]["adminRcloneLogsRefreshError"]["hidden"])
        self.assertFalse(result["failed"]["notices"]["adminRcloneServiceStatusRefreshError"]["hidden"])
        self.assertEqual(result["malformed"]["logs"], "initial download log")
        self.assertIn("格式无效", result["malformed"]["notices"]["adminRcloneLogsRefreshError"]["text"])
        self.assertEqual(result["recovered"]["logs"], "recovered live line")
        self.assertTrue(result["recovered"]["notices"]["adminRcloneLogsRefreshError"]["hidden"])
        self.assertTrue(result["recovered"]["notices"]["adminRcloneStatusRefreshError"]["hidden"])
        self.assertTrue(result["recovered"]["notices"]["adminRcloneServiceStatusRefreshError"]["hidden"])

    def test_three_second_polling_recovers_from_synchronous_error_without_erasing_logs(self) -> None:
        result = self.run_ui("""
ui.renderRcloneLiveLogs(['keep this existing log']);
ui.startRcloneLivePolling();
synchronousFailure = 'logs';
intervalCallback();
await flush();
const failed = snapshot();
synchronousFailure = null;
logLines = ['next poll recovered'];
intervalCallback();
await flush();
process.stdout.write(JSON.stringify({ failed, recovered: snapshot(), intervalMs }));
""")
        self.assertEqual(result["intervalMs"], 3000)
        self.assertEqual(result["failed"]["logs"], "keep this existing log")
        self.assertEqual(result["recovered"]["logs"], "next poll recovered")
        self.assertTrue(result["recovered"]["notices"]["adminRcloneLogsRefreshError"]["hidden"])


if __name__ == "__main__":
    unittest.main()
