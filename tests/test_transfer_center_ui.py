from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

from tests.test_rclone_live_progress_ui import ROOT, RcloneUiHarness


@unittest.skipUnless(shutil.which("node"), "Node is required for the transfer-center UI regression")
class TransferCenterUiTests(RcloneUiHarness, unittest.TestCase):
    def test_pipeline_renders_two_real_file_lanes_with_independent_staleness_and_buffer(self) -> None:
        result = self.run_ui("""
transfer.relay_buffer = { enabled: true, depth: 2, ready: 1, reserved: 1, waiting: true };
transfer.download = { state: 'running', active: true, scope: 'file', filename: 'Next Episode.mkv',
    percent: 20, speed_bps: 1048576, updated_at: 'download-time', stale: false, age_seconds: 1 };
Object.assign(transfer.upload, { active: true, stale: true, age_seconds: 30, updated_at: 'upload-time' });
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), ids: jobIds(), download: metric('download'), upload: metric('upload') }));
""")
        self.assertEqual(result["ids"], ["15"])
        self.assertEqual(result["status"].count('class="rclone-transfer-metric"'), 2)
        self.assertIn("预下载缓存 1 / 2", result["status"])
        self.assertIn("等待缓存释放", result["status"])
        self.assertIn("Next Episode.mkv", result["download"])
        self.assertNotIn("Example.mkv", result["download"])
        self.assertIn("Example.mkv", result["upload"])
        self.assertNotIn("Next Episode.mkv", result["upload"])
        self.assertIn('aria-valuenow="20"', result["download"])
        self.assertIn('aria-valuenow="45"', result["upload"])
        self.assertIn("1 MiB/s", result["download"])
        self.assertNotIn("256 KiB/s", result["upload"])
        self.assertIn("download-time", result["download"])
        self.assertIn("upload-time", result["upload"])
        self.assertIn("反馈较旧", result["upload"])
        self.assertNotIn("反馈较旧", result["download"])

    def test_pipeline_hash_is_the_only_outbound_lane_and_unknown_download_is_not_invented(self) -> None:
        result = self.run_ui("""
transfer.relay_buffer = { enabled: true, depth: 2, ready: 0, reserved: 2, waiting: false };
transfer.stage = 'hash';
transfer.download = { state: 'waiting', active: true, scope: 'file', filename: 'Next.mkv' };
transfer.hash = { state: 'running', active: true, scope: 'file', filename: 'Current.mkv', percent: 65, bytes_done: 650 };
transfer.upload.active = false;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), download: metric('download'), upload: metric('upload'), hash: metric('hash') }));
""")
        self.assertEqual(result["status"].count('class="rclone-transfer-metric"'), 2)
        self.assertEqual(result["upload"], "")
        self.assertIn("Next.mkv", result["download"])
        self.assertNotIn("aria-valuenow", result["download"])
        self.assertNotIn("rclone-progress-fill", result["download"])
        self.assertIn("已校验", result["hash"])
        self.assertNotIn("已传输", result["hash"])
        self.assertIn("不是已上传字节", result["hash"])

    def test_dual_lanes_require_pipeline_or_explicit_concurrent_activity_not_old_running_state(self) -> None:
        result = self.run_ui("""
transfer.download.state = 'running';
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
const legacy = snapshot();
transfer.upload.active = true;
transfer.download.active = true;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
const concurrent = snapshot();
transfer.download.stale = true;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ legacy, concurrent, stale: snapshot() }));
""")
        self.assertEqual(result["legacy"]["status"].count('class="rclone-transfer-metric"'), 1)
        self.assertEqual(result["concurrent"]["status"].count('class="rclone-transfer-metric"'), 2)
        self.assertEqual(result["stale"]["status"].count('class="rclone-transfer-metric"'), 1)
        self.assertNotIn("预下载缓存", result["legacy"]["status"])

    def test_pipeline_business_phases_hide_all_old_lanes_and_cache_notice(self) -> None:
        result = self.run_ui("""
const results = {};
for (const stage of ['organizing', 'target_verifying', 'manual_review', 'failed', 'completed']) {
    ui.renderRcloneStatus({ ...statusValue, transfers: [{ ...transfer, stage,
        relay_buffer: { enabled: true, depth: 2, ready: 1, waiting: true },
        download: { ...transfer.download, state: 'running', active: true },
        upload: { ...transfer.upload, active: true },
    }] });
    results[stage] = snapshot();
}
process.stdout.write(JSON.stringify(results));
""")
        for rendered in result.values():
            self.assertNotIn('role="progressbar"', rendered["status"])
            self.assertNotIn("预下载缓存", rendered["status"])
            self.assertNotIn("等待缓存释放", rendered["status"])

    def test_pipeline_filename_is_escaped_in_its_own_lane(self) -> None:
        result = self.run_ui("""
const marker = '<img src=x onerror="alert(1)">';
transfer.relay_buffer = { enabled: true, depth: 1, ready: 0, waiting: false };
transfer.upload.filename = marker;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ html: metric('upload'), marker, escaped: FnosUI.escapeHtml(marker) }));
""")
        self.assertIn(result["escaped"], result["html"])
        self.assertNotIn(result["marker"], result["html"])

    def test_zero_prefetch_shows_only_current_file_without_cache_or_parallel_claims(self) -> None:
        result = self.run_ui("""
transfer.relay_buffer = { enabled: true, depth: 0, ready: 0, reserved: 0, waiting: true };
transfer.download = { state: 'running', active: true, filename: 'Old.mkv', percent: 80 };
transfer.upload.active = true;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), download: metric('download'), upload: metric('upload') }));
""")
        self.assertEqual(result["status"].count('class="rclone-transfer-metric"'), 1)
        self.assertEqual(result["download"], "")
        self.assertIn("Example.mkv", result["upload"])
        self.assertNotIn("预下载缓存", result["status"])
        self.assertNotIn("等待缓存释放", result["status"])
        self.assertNotIn("并行", result["status"])

    def test_hash_capacity_is_labelled_checked_not_transferred(self) -> None:
        result = self.run_ui("""
transfer.stage = 'hash';
transfer.hash = { state: 'running', scope: 'file', percent: 67, bytes_done: 700000, bytes_total: 1048576 };
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ html: metric('hash') }));
""")
        self.assertIn("已校验", result["html"])
        self.assertNotIn("已传输", result["html"])
        self.assertIn("不是已上传字节", result["html"])

    def test_default_view_hides_success_and_cancelled_but_keeps_actionable_tasks(self) -> None:
        result = self.run_ui("""
const transfers = [
    { ...transfer, job_id: 1 },
    { ...transfer, job_id: 2, stage: 'completed', completed: true },
    { ...transfer, job_id: 3, stage: 'cancelled' },
    ...['failed', 'manual_review', 'review'].map((stage, index) => ({ ...transfer, job_id: index + 4, stage })),
];
ui.renderRcloneStatus({ ...statusValue, queue_count: 7, transfers });
const current = { ...snapshot(), ids: jobIds() };
ui.setTransferFilter('attention');
const attention = { ...snapshot(), ids: jobIds() };
ui.setTransferHistoryOpen(true);
process.stdout.write(JSON.stringify({ current, attention, history: { ...snapshot(), ids: jobIds(), historyIds: historyIds() } }));
""")
        self.assertCountEqual(result["current"]["ids"], ["1", "4", "5", "6"])
        self.assertCountEqual(result["attention"]["ids"], ["4", "5", "6"])
        self.assertCountEqual(result["history"]["ids"], ["4", "5", "6"])
        self.assertCountEqual(result["history"]["historyIds"], ["2", "3"])
        self.assertEqual(result["current"]["counts"], {
            "transferQueueCount": "7", "transferCurrentTabCount": "4",
            "transferAttentionTabCount": "3", "transferHistoryCount": "2",
        })
        self.assertEqual(result["current"]["history"], "")
        self.assertFalse(result["current"]["historyOpen"])
        self.assertIn("完整记录", result["history"]["hint"])
        for view in ("current", "attention", "history"):
            with self.subTest(view=view):
                selected = [item["value"] for item in result[view]["filters"] if item["aria-pressed"] == "true"]
                self.assertEqual(selected, ["attention" if view == "history" else view])
                self.assertEqual(result[view]["requests"], [])

    def test_completion_refresh_removes_current_card_and_history_remains_compact(self) -> None:
        result = self.run_ui("""
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
const before = jobIds();
transfer.completed = true;
transfer.stage = 'completed';
transfer.stale = true;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
const current = { ...snapshot(), ids: jobIds() };
ui.setTransferHistoryOpen(true);
process.stdout.write(JSON.stringify({ before, current, history: { ...snapshot(), ids: jobIds(), historyIds: historyIds() } }));
""")
        self.assertEqual(result["before"], ["15"])
        self.assertEqual(result["current"]["ids"], [])
        self.assertEqual(result["history"]["ids"], [])
        self.assertEqual(result["history"]["historyIds"], ["15"])
        self.assertEqual(result["current"]["history"], "")
        history = result["history"]["history"]
        self.assertIn('data-transfer-completed="true"', history)
        self.assertIn('data-transfer-job="15"', history)
        self.assertIn("Example.mkv", history)
        self.assertIn("入库完成", history)
        for stale_content in ("rclone-transfer-metric", "rclone-progress-track", "等待真实进度反馈", "反馈较旧", "预计剩余"):
            self.assertNotIn(stale_content, history)

    def test_completed_stage_or_truthy_value_is_not_a_confirmed_job_terminal(self) -> None:
        result = self.run_ui("""
const values = [false, 'true', 1, undefined, null, true];
ui.renderRcloneStatus({ ...statusValue, transfers: values.map((completed, index) => ({
    ...transfer, job_id: index + 1, stage: 'completed', completed,
    upload: { ...transfer.upload, percent: 100 },
})) });
const current = { ...snapshot(), ids: jobIds() };
ui.setTransferHistoryOpen(true);
process.stdout.write(JSON.stringify({ current, history: { ...snapshot(), ids: historyIds() } }));
""")
        self.assertCountEqual(result["current"]["ids"], ["1", "2", "3", "4", "5"])
        self.assertIn("等待入库终态确认", result["current"]["status"])
        self.assertNotIn("rclone-transfer-metric", result["current"]["status"])
        self.assertEqual(result["history"]["ids"], ["6"])

    def test_post_transfer_stages_hide_stale_download_hash_and_upload_bars(self) -> None:
        result = self.run_ui("""
const results = {};
for (const stage of ['organizing', 'verifying', 'target_verifying', 'finalizing', 'final_verifying',
    'cleanup', 'cleanup_pending', 'ack_pending', 'completed', 'done']) {
    ui.renderRcloneStatus({ ...statusValue, transfers: [{ ...transfer, stage,
        download: { ...transfer.download, state: 'running' },
        hash: { state: 'running', percent: 100 }, upload: { ...transfer.upload, percent: 100 },
    }] });
    results[stage] = { ...snapshot(), ids: jobIds() };
}
process.stdout.write(JSON.stringify(results));
""")
        for stage, rendered in result.items():
            with self.subTest(stage=stage):
                self.assertEqual(rendered["ids"], ["15"])
                self.assertNotIn("rclone-transfer-metric", rendered["status"])
                self.assertNotIn('role="progressbar"', rendered["status"])
                self.assertNotIn("进行中 · 100%", rendered["status"])
                self.assertIn('data-transfer-completed="false"', rendered["status"])

    def test_running_at_one_hundred_percent_waits_for_confirmation_without_archival(self) -> None:
        result = self.run_ui("""
const results = {};
for (const stage of ['download', 'hash', 'upload']) {
    ui.renderRcloneStatus({ ...statusValue, transfers: [{ ...transfer, stage,
        [stage]: { state: 'running', scope: 'file', filename: 'Example.mkv', percent: 100 },
    }] });
    results[stage] = { html: metric(stage), ids: jobIds() };
}
process.stdout.write(JSON.stringify(results));
""")
        for stage, rendered in result.items():
            with self.subTest(stage=stage):
                self.assertEqual(rendered["ids"], ["15"])
                self.assertIn('aria-valuenow="100"', rendered["html"])
                self.assertIn("等待阶段", rendered["html"])
                self.assertNotIn("进行中", rendered["html"])

    def test_confirmed_metric_has_summary_not_an_empty_bar_even_in_its_own_stage(self) -> None:
        result = self.run_ui("""
transfer.upload = { state: 'completed', scope: 'file', percent: null, bytes_done: null, bytes_total: null };
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify(snapshot()));
""")
        self.assertIn("已确认完成，未提供字节统计", result["status"])
        self.assertNotIn('role="progressbar"', result["status"])
        self.assertNotIn("等待真实进度反馈", result["status"])
        self.assertIn('data-transfer-completed="false"', result["status"])

    def test_attention_and_cancelled_do_not_keep_live_progress_or_stale_warnings(self) -> None:
        result = self.run_ui("""
ui.renderRcloneStatus({ ...statusValue, transfers: ['failed', 'manual_review', 'review', 'cancelled'].map((stage, index) => ({
    ...transfer, job_id: index + 1, stage, stale: true, message: 'requires a deliberate task action',
})) });
ui.setTransferFilter('attention');
const attention = snapshot();
ui.setTransferHistoryOpen(true);
process.stdout.write(JSON.stringify({ attention, history: snapshot() }));
""")
        self.assertIn("requires a deliberate task action", result["attention"]["status"])
        self.assertIn("取消", result["history"]["history"])
        for view in ("attention", "history"):
            html = result[view]["history" if view == "history" else "status"]
            self.assertNotIn('role="progressbar"', html)
            self.assertNotIn("反馈较旧", html)
            self.assertNotIn("等待真实进度反馈", html)

    def test_search_is_case_insensitive_trimmed_filter_local_and_network_free(self) -> None:
        result = self.run_ui("""
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer,
    { ...transfer, job_id: 42, download: {}, upload: { ...transfer.upload, filename: 'Another Film.mkv' } },
    { ...transfer, job_id: 8, stage: 'completed', completed: true },
] });
const counts = snapshot().counts;
ui.setTransferSearch('  eXAMple.MKV  ');
const byFilename = jobIds();
ui.setTransferSearch('42');
const byId = jobIds();
ui.setTransferSearch('  ');
const cleared = jobIds();
ui.setTransferSearch('example');
ui.setTransferHistoryOpen(true);
const history = historyIds();
ui.setTransferSearch('<nonexistent>');
const unmatched = { ...snapshot(), ids: jobIds() };
process.stdout.write(JSON.stringify({ counts, byFilename, byId, cleared, history, unmatched }));
""")
        self.assertEqual(result["byFilename"], ["15"])
        self.assertEqual(result["byId"], ["42"])
        self.assertCountEqual(result["cleared"], ["15", "42"])
        self.assertEqual(result["history"], ["8"])
        self.assertEqual(result["unmatched"]["ids"], [])
        self.assertIn("没有匹配", result["unmatched"]["status"])
        self.assertEqual(result["unmatched"]["counts"], result["counts"])
        self.assertEqual(result["unmatched"]["requests"], [])

    def test_filter_and_search_controls_work_without_fetching(self) -> None:
        result = self.run_ui("""
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer,
    { ...transfer, job_id: 42, stage: 'failed' }, { ...transfer, job_id: 8, completed: true },
] });
filterButtons.find(button => button.dataset.transferFilter === 'attention').listeners.click();
const attention = jobIds();
getElement('transferSearch').listeners.input({ target: { value: 'missing' } });
const filtered = jobIds();
getElement('transferSearch').listeners.input({ target: { value: '42' } });
const found = jobIds();
process.stdout.write(JSON.stringify({ attention, filtered, found, requests }));
""")
        self.assertEqual(result["attention"], ["42"])
        self.assertEqual(result["filtered"], [])
        self.assertEqual(result["found"], ["42"])
        self.assertEqual(result["requests"], [])

    def test_service_summary_is_separate_and_escapes_backend_text(self) -> None:
        result = self.run_ui("""
const marker = '<img src=x onerror="alert(1)">';
ui.renderRcloneStatus({ ...statusValue, last_error: marker, current_run_id: marker, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), marker, escaped: FnosUI.escapeHtml(marker) }));
""")
        for field in ("service", "transferService"):
            self.assertIn("下载进程空闲", result[field])
            self.assertIn(result["escaped"], result[field])
            self.assertNotIn(result["marker"], result[field])
            self.assertNotIn("data-transfer-job-id", result[field])
            self.assertNotIn("rclone-transfer-metric", result[field])
        self.assertNotIn("下载批次状态", result["status"])
        self.assertIn('data-transfer-job-id="15"', result["status"])

    def test_load_transfers_only_reads_status_and_keeps_existing_logs_untouched(self) -> None:
        result = self.run_ui("""
ui.renderRcloneLiveLogs(['existing download log']);
getElement('adminSystemLogs').textContent = 'existing system log';
statusValue.transfers = [transfer];
await ui.loadTransfers();
process.stdout.write(JSON.stringify(snapshot()));
""")
        self.assertEqual([item["path"] for item in result["requests"]], ["/api/admin/rclone/status"])
        self.assertTrue(all(item.get("method", "GET") == "GET" for item in result["requests"]))
        self.assertEqual(result["logs"], "existing download log")
        self.assertEqual(result["system"], "existing system log")
        self.assertIn('data-transfer-job-id="15"', result["status"])

    def test_polling_reads_only_the_visible_surface_and_stops_on_other_tabs(self) -> None:
        result = self.run_ui("""
ui.startRcloneLivePolling();
state.activeTab = 'transfers';
intervalCallback(); await flush();
const transfers = snapshot();
requests.length = 0;
state.activeTab = 'rclone';
intervalCallback(); await flush();
const logs = snapshot();
requests.length = 0;
for (const tab of ['overview', 'jobs', 'media', 'adapters']) {
    state.activeTab = tab; intervalCallback(); await flush();
}
process.stdout.write(JSON.stringify({ transfers, logs, inactive: snapshot(), intervalMs }));
""")
        self.assertEqual(result["intervalMs"], 3000)
        self.assertEqual([item["channel"] for item in result["transfers"]["requests"]], ["status"])
        self.assertCountEqual([item["channel"] for item in result["logs"]["requests"]], ["status", "logs", "system"])
        self.assertEqual(result["inactive"]["requests"], [])

    def test_transfer_and_log_refresh_share_status_dedup_without_blocking_logs(self) -> None:
        result = self.run_ui("""
let releaseStatus;
gates.status = new Promise(resolve => { releaseStatus = resolve; });
const transferRefresh = ui.loadTransfers();
await flush();
const logRefresh = ui.loadRcloneLive();
await flush();
const during = snapshot();
releaseStatus();
await Promise.all([transferRefresh, logRefresh]);
process.stdout.write(JSON.stringify({ during, final: snapshot() }));
""")
        self.assertEqual(result["during"]["logs"], "initial download log")
        self.assertEqual(result["during"]["system"], "initial system log")
        self.assertEqual(sum(item["channel"] == "status" for item in result["during"]["requests"]), 1)
        self.assertIn("下载进程空闲", result["final"]["service"])

    def test_status_failure_preserves_filtered_snapshot_and_recovers_both_notices(self) -> None:
        result = self.run_ui("""
statusValue.transfers = [transfer, { ...transfer, job_id: 8, stage: 'completed', completed: true }];
await ui.loadTransfers();
ui.setTransferHistoryOpen(true);
ui.setTransferSearch('Example');
const initial = snapshot();
failures.status = 'temporary status outage';
await ui.loadTransfers();
const failed = snapshot();
delete failures.status;
await ui.loadTransfers();
process.stdout.write(JSON.stringify({ initial, failed, recovered: { ...snapshot(), ids: jobIds(), historyIds: historyIds() } }));
""")
        self.assertEqual(result["failed"]["status"], result["initial"]["status"])
        self.assertEqual(result["failed"]["service"], result["initial"]["service"])
        self.assertEqual(result["failed"]["history"], result["initial"]["history"])
        for notice in ("adminRcloneStatusRefreshError", "adminRcloneServiceStatusRefreshError"):
            self.assertFalse(result["failed"]["notices"][notice]["hidden"])
            self.assertIn("temporary status outage", result["failed"]["notices"][notice]["text"])
            self.assertTrue(result["recovered"]["notices"][notice]["hidden"])
        self.assertEqual(result["recovered"]["ids"], ["15"])
        self.assertEqual(result["recovered"]["historyIds"], ["8"])

    def test_many_completed_tasks_stay_out_of_primary_view_and_history_is_limited(self) -> None:
        result = self.run_ui("""
const ended = Array.from({ length: 50 }, (_, index) => ({ ...transfer, job_id: 100 + index,
    stage: 'completed', completed: true, updated_at: '2026-10-14 01:00:' + String(index).padStart(2, '0') }));
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer, ...ended] });
const closed = { ...snapshot(), ids: jobIds() };
ui.setTransferHistoryOpen(true);
const opened = { ...snapshot(), ids: jobIds(), historyIds: historyIds() };
ui.setTransferHistoryOpen(false);
process.stdout.write(JSON.stringify({ closed, opened, reclosed: snapshot() }));
""")
        self.assertEqual(result["closed"]["ids"], ["15"])
        self.assertEqual(result["closed"]["history"], "")
        self.assertFalse(result["closed"]["historyOpen"])
        self.assertEqual(result["closed"]["counts"]["transferHistoryCount"], "50")
        self.assertEqual(result["opened"]["ids"], ["15"])
        self.assertEqual(result["opened"]["historyIds"], ["149", "148", "147", "146", "145"])
        self.assertIn("最近 5 条", result["opened"]["hint"])
        for token in ("<article", "<h3", "transfer-card", "transfer-file-icon", 'role="progressbar"'):
            self.assertNotIn(token, result["opened"]["history"])
        self.assertEqual(result["reclosed"]["history"], "")
        self.assertFalse(result["reclosed"]["historyOpen"])
        self.assertEqual(result["opened"]["requests"], [])

    def test_native_history_toggle_and_polling_preserve_only_explicit_expansion(self) -> None:
        result = self.run_ui("""
const status = { ...statusValue, transfers: [transfer, { ...transfer, job_id: 8, completed: true }] };
ui.renderRcloneStatus(status);
ui.renderRcloneStatus(status);
const before = snapshot();
getElement('transferHistory').open = true;
getElement('transferHistory').listeners.toggle();
ui.renderRcloneStatus(status);
const opened = snapshot();
getElement('transferHistory').open = false;
getElement('transferHistory').listeners.toggle();
ui.renderRcloneStatus(status);
process.stdout.write(JSON.stringify({ before, opened, closed: snapshot() }));
""")
        self.assertFalse(result["before"]["historyOpen"])
        self.assertEqual(result["before"]["history"], "")
        self.assertTrue(result["opened"]["historyOpen"])
        self.assertIn('data-transfer-job-id="8"', result["opened"]["history"])
        self.assertFalse(result["closed"]["historyOpen"])
        self.assertEqual(result["closed"]["history"], "")

    def test_idle_view_has_no_completed_cards_or_celebration_and_hides_empty_metadata(self) -> None:
        result = self.run_ui("""
ui.renderRcloneStatus(statusValue);
const empty = snapshot();
ui.renderRcloneStatus({ ...statusValue, transfers: [{ ...transfer, completed: true }] });
process.stdout.write(JSON.stringify({ empty, finished: snapshot() }));
""")
        self.assertTrue(result["empty"]["historyHidden"])
        self.assertTrue(result["empty"]["queueHidden"])
        self.assertFalse(result["finished"]["historyHidden"])
        self.assertFalse(result["finished"]["historyOpen"])
        self.assertEqual(result["finished"]["history"], "")
        for value in result.values():
            self.assertNotIn("transfer-card", value["status"])
            self.assertNotIn("transfer-empty-icon", value["status"])
            self.assertNotIn("Example.mkv", value["status"])

    def test_completed_workflow_steps_are_inside_closed_records_not_the_primary_card(self) -> None:
        result = self.run_ui("""
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify(snapshot()));
""")
        html = result["status"]
        before_details, record = html.split('<details class="transfer-technical"', 1)
        self.assertIn("Example.mkv", before_details)
        self.assertIn('data-transfer-metric="upload"', before_details)
        self.assertNotIn("transfer-steps", before_details)
        self.assertNotIn("is-done", before_details)
        self.assertIn("transfer-steps", record)
        self.assertNotIn(" open", record.split(">", 1)[0])

    def test_compact_history_escapes_provider_data(self) -> None:
        result = self.run_ui("""
const marker = '<img src=x onerror="alert(1)">';
ui.renderRcloneStatus({ ...statusValue, transfers: [{ ...transfer, completed: true,
    title: marker, job_id: marker, updated_at: marker }] });
ui.setTransferHistoryOpen(true);
process.stdout.write(JSON.stringify({ ...snapshot(), marker, escaped: FnosUI.escapeHtml(marker) }));
""")
        self.assertIn(result["escaped"], result["history"])
        self.assertNotIn(result["marker"], result["history"])
        self.assertEqual(result["requests"], [])

    def test_current_filename_is_separate_from_task_title_and_escaped(self) -> None:
        result = self.run_ui("""
const filename = '<img src=x onerror="alert(1)">.mkv';
transfer.title = 'A Different Series';
transfer.upload.filename = filename;
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), filename, escaped: FnosUI.escapeHtml(filename) }));
""")
        self.assertIn("A Different Series", result["status"])
        self.assertIn(result["escaped"], result["status"])
        self.assertNotIn(result["filename"], result["status"])
        markup = _AdminMarkup(result["status"])
        filenames = [node for node in markup.nodes if "transfer-current-file" in node["attrs"].get("class", "").split()]
        self.assertEqual(len(filenames), 1)
        self.assertIn(result["filename"], filenames[0]["text"])

    def test_live_metric_keeps_accessible_progress_and_three_readable_stat_columns(self) -> None:
        result = self.run_ui("""
Object.assign(transfer.upload, { bytes_done: 524288, bytes_total: 1048576 });
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), upload: metric('upload') }));
""")
        markup = _AdminMarkup(result["upload"])
        metrics = [node for node in markup.nodes if node["attrs"].get("data-transfer-metric") == "upload"]
        self.assertEqual(len(metrics), 1)
        bars = [node for node in markup.nodes if node["attrs"].get("role") == "progressbar"]
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["attrs"]["aria-valuenow"], "45")
        self.assertEqual(bars[0]["attrs"]["aria-valuemin"], "0")
        self.assertEqual(bars[0]["attrs"]["aria-valuemax"], "100")
        self.assertTrue(bars[0]["attrs"].get("aria-label"))
        stats = [node for node in markup.nodes if "transfer-metric-stats" in node["attrs"].get("class", "").split()]
        self.assertEqual(len(stats), 1)
        columns = stats[0]["children"]
        self.assertEqual(len(columns), 3)
        self.assertIn("512 KiB", columns[0]["text"])
        self.assertIn("1 MiB", columns[0]["text"])
        self.assertIn("速度", columns[1]["text"])
        self.assertIn("256 KiB/s", columns[1]["text"])
        self.assertIn("预计剩余", columns[2]["text"])
        self.assertIn("3 秒", columns[2]["text"])

    def test_unknown_metric_columns_keep_placeholders_without_invented_progress(self) -> None:
        result = self.run_ui("""
transfer.upload = { state: 'running', scope: 'file', filename: 'Example.mkv',
    percent: null, bytes_done: null, bytes_total: null, speed_bps: null, eta_seconds: null };
ui.renderRcloneStatus({ ...statusValue, transfers: [transfer] });
process.stdout.write(JSON.stringify({ ...snapshot(), upload: metric('upload') }));
""")
        html = result["upload"]
        self.assertIn('data-transfer-metric="upload"', html)
        self.assertIn('role="progressbar"', html)
        self.assertNotIn("aria-valuenow", html)
        self.assertNotIn("rclone-progress-fill", html)
        self.assertIn("等待真实进度反馈", html)
        stats = [node for node in _AdminMarkup(html).nodes if "transfer-metric-stats" in node["attrs"].get("class", "").split()]
        self.assertEqual(len(stats), 1)
        self.assertEqual(len(stats[0]["children"]), 3)
        for column in stats[0]["children"]:
            self.assertIn("—", column["text"])
            self.assertIsNone(re.search(r"\d", column["text"]))

    def test_history_keeps_task_identity_time_and_terminal_status_without_progress(self) -> None:
        result = self.run_ui("""
ui.renderRcloneStatus({ ...statusValue, transfers: [
    { ...transfer, job_id: 81, title: 'Finished Series', completed: true, updated_at: '2026-10-15 10:01:00' },
    { ...transfer, job_id: 82, title: 'Cancelled Series', stage: 'cancelled', updated_at: '2026-10-15 10:02:00' },
] });
ui.setTransferHistoryOpen(true);
process.stdout.write(JSON.stringify(snapshot()));
""")
        html = result["history"]
        rows = {node["attrs"]["data-transfer-job-id"]: node for node in _AdminMarkup(html).nodes
                if "transfer-history-entry" in node["attrs"].get("class", "").split()}
        self.assertCountEqual(rows, ["81", "82"])
        for job_id, title, timestamp, completed, label in (
            ("81", "Finished Series", "2026-10-15 10:01:00", "true", "入库完成"),
            ("82", "Cancelled Series", "2026-10-15 10:02:00", "false", "已取消"),
        ):
            with self.subTest(job_id=job_id):
                self.assertEqual(rows[job_id]["attrs"]["data-transfer-completed"], completed)
                for text in (title, f"#{job_id}", timestamp, label):
                    self.assertIn(text, rows[job_id]["text"])
                self.assertIn(f'data-transfer-job="{job_id}"', html)
        self.assertNotIn('role="progressbar"', html)
        self.assertNotIn("rclone-transfer-metric", html)
        self.assertNotIn("rclone-progress-fill", html)


class _AdminMarkup(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.nodes: list[dict] = []
        self.stack: list[tuple[str, dict]] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        node = {"tag": tag, "attrs": attributes, "ancestors": tuple(item["attrs"].get("id") for _, item in self.stack),
                "children": [], "text": ""}
        self.nodes.append(node)
        if self.stack:
            self.stack[-1][1]["children"].append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, node))

    def handle_data(self, data: str) -> None:
        for _, node in self.stack:
            node["text"] += data

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class TransferCenterMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.markup = _AdminMarkup((ROOT / "templates" / "admin.html").read_text(encoding="utf-8"))

    def node(self, identifier: str) -> dict:
        matches = [node for node in self.markup.nodes if node["attrs"].get("id") == identifier]
        self.assertEqual(len(matches), 1, f"Expected one #{identifier}")
        return matches[0]

    def test_transfer_menu_and_panel_have_accessible_navigation_relationship(self) -> None:
        tab = self.node("adminTabTransfers")
        panel = self.node("tab-transfers")
        self.assertEqual(tab["tag"], "button")
        self.assertEqual(tab["attrs"]["data-tab"], "transfers")
        self.assertEqual(tab["attrs"]["role"], "tab")
        self.assertEqual(tab["attrs"]["aria-controls"], "tab-transfers")
        self.assertEqual(panel["attrs"]["role"], "tabpanel")
        self.assertEqual(panel["attrs"]["aria-labelledby"], "adminTabTransfers")
        self.assertIn("hidden", panel["attrs"])

    def test_transfer_layout_limits_width_and_uses_responsive_card_columns(self) -> None:
        styles = (ROOT / "static" / "admin-transfers.css").read_text(encoding="utf-8")

        def rule(selector: str) -> str:
            match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]+)\}}", styles)
            self.assertIsNotNone(match, selector)
            return re.sub(r"\s+", " ", match.group(1))

        self.assertIn("max-width: 1160px", rule("#tab-transfers"))
        self.assertIn("grid-template-columns: repeat(auto-fill, minmax(min(100%, 420px), 1fr))", rule(".transfer-list"))
        self.assertIn("max-width: 620px", rule(".transfer-card"))
        self.assertIn("min-width: 0", rule(".transfer-card"))
        self.assertIn("max-width: 640px", rule(".transfer-empty"))
        self.assertIn("width: 640px", rule(".transfer-history"))
        self.assertIn("max-width: min(100%, 640px)", rule(".transfer-history"))
        self.assertIn("min-width: 0", rule(".transfer-history"))
        self.assertNotIn("max-height: 200px", rule(".transfer-history-body"))
        self.assertNotRegex(rule(".transfer-history-body"), r"overflow(?:-y)?:\s*(?:auto|scroll)")

    def test_live_cards_are_only_in_transfer_panel_and_logs_keep_summary(self) -> None:
        self.assertIn("tab-transfers", self.node("adminRcloneStatus")["ancestors"])
        self.assertNotIn("tab-rclone", self.node("adminRcloneStatus")["ancestors"])
        self.assertIn("tab-rclone", self.node("adminRcloneServiceStatus")["ancestors"])
        self.assertIn("tab-rclone", self.node("adminRcloneLogs")["ancestors"])
        self.assertIn("tab-rclone", self.node("adminSystemLogs")["ancestors"])
        self.assertIn("tab-transfers", self.node("adminRcloneStatusRefreshError")["ancestors"])
        self.assertIn("tab-rclone", self.node("adminRcloneServiceStatusRefreshError")["ancestors"])

    def test_transfer_controls_counts_search_and_errors_have_stable_contract(self) -> None:
        filters = [node for node in self.markup.nodes if "data-transfer-filter" in node["attrs"]]
        self.assertCountEqual([node["attrs"]["data-transfer-filter"] for node in filters], ["current", "attention"])
        for node in filters:
            self.assertIn("tab-transfers", node["ancestors"])
            self.assertEqual(node["tag"], "button")
            self.assertEqual(node["attrs"]["aria-controls"], "adminRcloneStatus")
            self.assertEqual(node["attrs"]["aria-pressed"], "true" if node["attrs"]["data-transfer-filter"] == "current" else "false")
        search = self.node("transferSearch")
        self.assertEqual(search["attrs"]["type"], "search")
        self.assertTrue(search["attrs"].get("aria-label"))
        for identifier in ("adminTransferService", "transferVisibilityHint", "transferQueueCount",
                           "transferCurrentTabCount", "transferAttentionTabCount", "transferHistoryCount", "loadTransfersBtn"):
            self.assertIn("tab-transfers", self.node(identifier)["ancestors"])
        for identifier in ("adminRcloneStatusRefreshError", "adminRcloneServiceStatusRefreshError"):
            self.assertEqual(self.node(identifier)["attrs"]["role"], "status")

    def test_history_and_service_are_collapsed_below_media_without_large_completion_stats(self) -> None:
        history = self.node("transferHistory")
        service = self.node("transferServiceDetails")
        for details in (history, service):
            self.assertEqual(details["tag"], "details")
            self.assertNotIn("open", details["attrs"])
            self.assertGreater(self.markup.nodes.index(details), self.markup.nodes.index(self.node("adminRcloneStatus")))
        self.assertIn("hidden", history["attrs"])
        self.assertIn("transferHistory", self.node("adminTransferHistory")["ancestors"])
        self.assertIn("transferHistory", self.node("transferVisibilityHint")["ancestors"])
        self.assertIn("transferServiceDetails", self.node("adminTransferService")["ancestors"])
        for node in self.markup.nodes:
            self.assertNotIn(node["attrs"].get("id"), {"transferCompletedCount", "transferActiveCount", "transferHistoryTabCount"})
            self.assertNotIn("transfer-overview", node["attrs"].get("class", "").split())

    @unittest.skipUnless(shutil.which("node"), "Node is required for navigation regression")
    def test_navigation_loads_transfers_refreshes_on_revisit_and_wires_cross_links(self) -> None:
        script = r"""
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.FnosUI = { bindRovingTabs: () => {} };
require(process.argv[1]);
const elements = new Map();
function makeElement(id) {
    const element = { id, dataset: {}, attributes: {}, listeners: {}, hidden: false,
        classList: { toggle: () => {}, contains: () => false }, querySelector: () => null,
        setAttribute(key, value) { this.attributes[key] = value; },
        addEventListener(kind, listener) { this.listeners[kind] = listener; },
    };
    elements.set(id, element);
    return element;
}
const tabs = ['transfers', 'rclone', 'jobs'].map(name => {
    const tab = makeElement(`nav-${name}`); tab.dataset.tab = name; return tab;
});
const panels = ['transfers', 'rclone', 'jobs'].map(name => makeElement(`tab-${name}`));
for (const id of ['loadTransfersBtn', 'adminTransfersJobsBtn', 'adminTransfersLogsBtn', 'adminLogsTransfersBtn']) makeElement(id);
globalThis.document = {
    querySelectorAll: selector => selector === '.tab' ? tabs : selector === '.admin-tab-panel' ? panels : [],
    addEventListener: () => {},
};
const state = { activeTab: 'jobs', loadingTabs: {}, loadedTabs: {} };
const calls = [];
const bootstrap = FnosAdminBootstrap.create({
    state, getElement: id => elements.get(id) || null, toast: error => { throw Error(error); },
    bindNotificationSettings: () => {},
    loadTransfers: async () => { calls.push('transfers'); },
    loadRclone: async () => { calls.push('rclone'); },
    loadJobs: async () => { calls.push('jobs'); },
});
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
    bootstrap.bindEvents();
    bootstrap.activateTab('transfers'); await flush();
    const selected = { activeTab: state.activeTab, hidden: elements.get('tab-transfers').hidden,
        selected: tabs.find(tab => tab.dataset.tab === 'transfers').attributes['aria-selected'] };
    elements.get('adminTransfersLogsBtn').listeners.click(); await flush();
    elements.get('adminLogsTransfersBtn').listeners.click(); await flush();
    elements.get('loadTransfersBtn').listeners.click(); await flush();
    elements.get('adminTransfersJobsBtn').listeners.click(); await flush();
    process.stdout.write(JSON.stringify({ selected, calls, activeTab: state.activeTab }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "admin-bootstrap.js")],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["selected"], {"activeTab": "transfers", "hidden": False, "selected": "true"})
        self.assertEqual(result["calls"], ["transfers", "rclone", "transfers", "transfers", "jobs"])
        self.assertEqual(result["activeTab"], "jobs")


if __name__ == "__main__":
    unittest.main()
