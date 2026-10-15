from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.repositories.worker_task_repository import WorkerTaskRepository
from fnos_media_import.repositories.update_repository import HISTORY_CLEANUP_TABLES, HistoryCleanupBlockedError
from fnos_media_import.notifications.sender import deliver_task
from fnos_media_import.services.settings_service import SettingsDependencies, SettingsService


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _workspace():
    test_root = ROOT / "codex_tmp"
    test_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="history-safety-", dir=test_root) as directory:
        database = Database(Path(directory) / "test.db")
        database.init_schema()
        backup = Mock(side_effect=AssertionError("Direct cleanup must not create a backup"))
        yield database, RepositorySet.build(database.connect, backup), backup


def _job(repositories, *, status="done", key="history-job"):
    with repositories.connect() as connection:
        return int(connection.execute(
            "INSERT INTO import_jobs (title, category, category_label, source_provider_kind, source_form, "
            "source_url, status, created_at, updated_at, idempotency_key, destination_id, plan_version) "
            "VALUES (?, 'tv', 'tv', 'quark', 'share_link', 'https://example.invalid/share', ?, ?, ?, ?, 'test-target', 1)",
            (key, status, "2026-10-11T00:00:00Z", "2026-10-11T00:00:00Z", key),
        ).lastrowid)


def _lifecycle_rows(repositories):
    job_id = _job(repositories)
    request_id = repositories.guest_request_commands.create({
        "request_token": "history-request", "title": "History", "status": "pending_review",
    })
    worker_id, _ = repositories.worker_tasks.enqueue("transfer_execute", {"job_id": job_id}, "history-worker")
    repositories.worker_tasks.claim("history-test-owner")
    repositories.worker_tasks.complete(worker_id, "history-test-owner")
    run = repositories.rclone.create_rclone_run("history")
    repositories.rclone.update_rclone_run(run, "success", exit_code=0)
    task_id = repositories.organizer.create_organizer_task(
        category="tv", openlist_root_path="/history/job-1", job_id=job_id, status="waiting_review",
    )
    organizer_run = repositories.organizer.create_organizer_run(task_id, status="success")
    subscription = repositories.update.create_update_subscription({"title": "Retained show", "category": "tv"})
    update_run = repositories.update.create_update_run(subscription, "manual")
    repositories.update.update_update_run(update_run, status="success")
    trending_run = repositories.trending.create_run(status="success")
    with repositories.connect() as connection:
        connection.execute(
            "INSERT INTO transfer_executions(job_id, strategy, phase, receipt_json, created_at, updated_at) "
            "VALUES (?, 'rclone_copy', 'completed', ?, ?, ?)",
            (job_id, '{"operation_key":"history-operation"}', "2026-10-11T00:00:00Z", "2026-10-11T00:00:00Z"),
        )
    return {
        "worker_tasks": worker_id, "import_jobs": job_id, "guest_requests": request_id,
        "rclone_runs": run, "transfer_executions": job_id, "organizer_tasks": task_id,
        "organizer_runs": organizer_run, "update_runs": update_run, "trending_discovery_runs": trending_run,
    }


def _history_snapshot(repositories):
    with repositories.connect() as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in (*HISTORY_CLEANUP_TABLES, "transfer_executions", "sqlite_sequence", "notification_deliveries")
        }


def _service(cleanup: Mock) -> SettingsService:
    def payload_bool(payload: dict, key: str, default: bool) -> bool:
        value = payload.get(key, default)
        return value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}

    return SettingsService(
        SettingsDependencies(
            db=SimpleNamespace(update=SimpleNamespace(cleanup_history_records=cleanup)),
            raw_config=lambda: {},
            redact_config=lambda config: config,
            advanced_response=lambda _raw, _stored: {},
            normalize_advanced=lambda payload, **_kwargs: payload,
            advanced_key="advanced_config",
            reload_runtime=lambda: {},
            effective_settings=lambda: {},
            payload_bool=payload_bool,
        )
    )


class HistoryCleanupServiceTests(unittest.TestCase):
    def test_default_and_explicit_false_skip_backup(self) -> None:
        for payload in ({"confirm": True}, {"confirm": True, "backup": False}):
            with self.subTest(payload=payload):
                cleanup = Mock(return_value={"backup": {}, "cleared_total": 3, "after": {"cleanup_total": 0}})

                body, status = _service(cleanup).cleanup_history(payload)

                cleanup.assert_called_once_with(backup=False, vacuum=True, backup_prefix="app_before_history_cleanup")
                self.assertEqual(status, 200)
                self.assertTrue(body["success"])
                self.assertEqual(body["summary"], {"cleanup_total": 0})
                self.assertNotIn("备份", body["message"])

    def test_explicit_backup_remains_available(self) -> None:
        cleanup = Mock(return_value={"backup": {"path": "requested-backup.db"}, "cleared_total": 0})

        body, status = _service(cleanup).cleanup_history({"confirm": True, "backup": True, "vacuum": False})

        cleanup.assert_called_once_with(backup=True, vacuum=False, backup_prefix="app_before_history_cleanup")
        self.assertEqual(status, 200)
        self.assertIn("requested-backup.db", body["message"])

    def test_missing_or_negative_confirmation_never_cleans(self) -> None:
        for payload in ({}, {"confirm": False}, {"confirm": "false"}):
            with self.subTest(payload=payload):
                cleanup = Mock()

                body, status = _service(cleanup).cleanup_history(payload)

                cleanup.assert_not_called()
                self.assertEqual(status, 400)
                self.assertFalse(body["success"])

    def test_active_tasks_return_conflict_with_actionable_details(self) -> None:
        blockers = [{"table": "rclone_runs", "label": "rclone 运行", "first_id": 7, "status": "running", "count": 1}]
        cleanup = Mock(side_effect=HistoryCleanupBlockedError(blockers))

        body, status = _service(cleanup).cleanup_history({"confirm": True})

        self.assertEqual(status, 409)
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "HISTORY_CLEANUP_BUSY")
        self.assertEqual(body["blockers"], blockers)
        self.assertIn("#7", body["message"])
        self.assertIn("未清理任何历史记录", body["message"])

    def test_nonobject_payload_is_rejected(self) -> None:
        cleanup = Mock()
        for payload in (["confirm"], "true", True):
            with self.subTest(payload=payload):
                self.assertEqual(_service(cleanup).cleanup_history(payload)[1], 400)
        cleanup.assert_not_called()


class HistoryCleanupRepositoryTests(unittest.TestCase):
    def test_direct_cleanup_preserves_settings_subscriptions_and_sources(self) -> None:
        test_root = ROOT / "codex_tmp"
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="history-cleanup-", dir=test_root) as directory:
            database = Database(Path(directory) / "test.db")
            database.init_schema()
            backup = Mock(side_effect=AssertionError("Direct history cleanup must not create a backup"))
            repositories = RepositorySet.build(database.connect, backup)
            repositories.app_settings.set_many({"history_cleanup_test": {"enabled": True}})
            subscription_id = repositories.update.create_update_subscription(
                {"title": "Retained show", "category": "tv", "next_episode": 4},
                [{"type": "search", "name": "Retained source", "url": "https://example.invalid/source"}],
            )
            run_id = repositories.update.create_update_run(subscription_id, "manual")
            repositories.update.update_update_run(run_id, status="success")
            repositories.update.add_update_event(subscription_id, run_id, "info", "History to clear")
            repositories.search_cache_commands.save(
                "history-cleanup-cache", "show", {"title": "Cached show", "url": "https://example.invalid/item"},
            )

            def preserved_rows() -> dict:
                with database.connect() as connection:
                    return {
                        table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                        for table in ("app_settings", "update_subscriptions", "update_sources")
                    }

            preserved = preserved_rows()
            result = repositories.update.cleanup_history_records()

            backup.assert_not_called()
            self.assertEqual(result["backup"], {})
            self.assertEqual(result["cleared_total"], 3)
            self.assertEqual(result["after"]["cleanup_total"], 0)
            self.assertTrue(all(count == 0 for count in result["after"]["cleanup_tables"].values()))
            self.assertEqual(result["vacuum_error"], "")
            self.assertEqual(preserved_rows(), preserved)

            repeated = repositories.update.cleanup_history_records(backup=False, vacuum=False)
            self.assertEqual(repeated["cleared_total"], 0)
            self.assertEqual(preserved_rows(), preserved)
            backup.assert_not_called()

    def test_each_active_or_queued_state_preserves_all_records_and_sequences(self) -> None:
        active = {
            "worker_tasks": ("pending", "running"),
            "import_jobs": ("created", "provider_submitting", "checking", "submitted", "waiting_transfer", "transferring", "waiting_openlist", "waiting_organizer", "organizing", "confirming", "refreshing"),
            "guest_requests": ("submitted", "processing"),
            "rclone_runs": ("queued", "starting", "running", "recovering", "cancel_pending"),
            "transfer_executions": ("planning", "source_materializing", "source_waiting_visible", "native_submitting", "native_waiting", "transferring", "target_verifying", "organizing", "finalizing", "final_verifying", "ack_pending", "cleanup_pending", "retry_wait", "cancel_pending", "future_phase"),
            "organizer_tasks": ("pending", "waiting_openlist", "stabilizing", "scanning", "auto_approved", "manual_confirmed", "executing"),
            "organizer_runs": ("running",),
            "update_runs": ("running",),
            "trending_discovery_runs": ("running",),
        }
        with _workspace() as (_database, repositories, backup):
            ids = _lifecycle_rows(repositories)
            for table, statuses in active.items():
                column = "phase" if table == "transfer_executions" else "status"
                with repositories.connect() as connection:
                    original = connection.execute(f"SELECT {column} FROM {table}").fetchone()[0]
                for status in statuses:
                    with self.subTest(table=table, status=status):
                        with repositories.connect() as connection:
                            connection.execute(f"UPDATE {table} SET {column}=?", (status,))
                        snapshot = _history_snapshot(repositories)
                        with self.assertRaises(HistoryCleanupBlockedError) as raised:
                            repositories.update.cleanup_history_records(backup=True, vacuum=False)
                        self.assertTrue(any(item["table"] == table and item["first_id"] == ids[table] for item in raised.exception.blockers))
                        self.assertEqual(_history_snapshot(repositories), snapshot)
                with repositories.connect() as connection:
                    connection.execute(f"UPDATE {table} SET {column}=?", (original,))
            backup.assert_not_called()

    def test_terminal_and_review_history_cleans_without_reusing_job_or_run_ids(self) -> None:
        with _workspace() as (_database, repositories, _backup):
            ids = _lifecycle_rows(repositories)
            with repositories.connect() as connection:
                connection.execute("UPDATE import_jobs SET status='review'")
                connection.execute("UPDATE transfer_executions SET phase='manual_review'")
                connection.execute("UPDATE guest_requests SET job_id=?, status='transferring'", (ids["import_jobs"],))
                sequences = {row["name"]: row["seq"] for row in connection.execute("SELECT * FROM sqlite_sequence")}

            result = repositories.update.cleanup_history_records(vacuum=False)

            self.assertEqual(result["after"]["cleanup_total"], 0)
            with repositories.connect() as connection:
                self.assertEqual({row["name"]: row["seq"] for row in connection.execute("SELECT * FROM sqlite_sequence")}, sequences)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM transfer_executions").fetchone()[0], 0)
            self.assertGreater(_job(repositories, key="new-job"), ids["import_jobs"])
            self.assertGreater(repositories.rclone.create_rclone_run("new-run"), ids["rclone_runs"])
            new_worker, _ = repositories.worker_tasks.enqueue("new-test", {}, "new-worker")
            self.assertGreater(new_worker, ids["worker_tasks"])

    def test_failed_delete_rolls_back_records_and_sequence_repair(self) -> None:
        with _workspace() as (_database, repositories, _backup):
            _lifecycle_rows(repositories)
            repositories.notification_deliveries.record(task_id=41, event_type="old", channel="webhook", status="success")
            with repositories.connect() as connection:
                connection.execute(
                    "CREATE TRIGGER fail_history_delete BEFORE DELETE ON import_jobs "
                    "BEGIN SELECT RAISE(ABORT, 'test cleanup failure'); END"
                )
            snapshot = _history_snapshot(repositories)

            with self.assertRaisesRegex(sqlite3.IntegrityError, "test cleanup failure"):
                repositories.update.cleanup_history_records(vacuum=False)

            self.assertEqual(_history_snapshot(repositories), snapshot)

    def test_legacy_notification_high_water_is_repaired_and_new_notification_sends(self) -> None:
        with _workspace() as (_database, repositories, _backup):
            repositories.notification_deliveries.record(task_id=41, event_type="old", channel="webhook", status="success")
            # Reproduce an older cleanup: the sequence is absent, but its
            # successfully delivered worker task IDs remain in the audit log.
            repositories.update.cleanup_history_records(vacuum=False)
            task_id, _ = repositories.worker_tasks.enqueue(
                "notification_deliver", {"event_type": "new", "channels": ["webhook"]}, "new-notification",
            )
            self.assertGreater(task_id, 41)
            self.assertEqual(repositories.notification_deliveries.latest_status_by_channel(task_id), {})
            with (
                patch("fnos_media_import.notifications.sender.notify_config.read_config", return_value={"enabled": True}),
                patch("fnos_media_import.notifications.sender.notify_config.channel_enabled", return_value=True),
                patch("fnos_media_import.notifications.sender.notify_config.channel_revision", return_value=""),
                patch("fnos_media_import.notifications.sender._send_one") as send,
            ):
                result = deliver_task(repositories, repositories.worker_tasks.get(task_id))
            self.assertEqual(result["worker_outcome"], "completed")
            send.assert_called_once()
            self.assertEqual(repositories.notification_deliveries.latest_status_by_channel(41), {"webhook": "success"})

    def test_legacy_notification_high_water_never_lowers_a_newer_sequence(self) -> None:
        with _workspace() as (_database, repositories, _backup):
            repositories.notification_deliveries.record(task_id=41, event_type="old", channel="webhook", status="success")
            with repositories.connect() as connection:
                connection.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('worker_tasks', 1000)")
            repositories.update.cleanup_history_records(vacuum=False)
            task_id, _ = repositories.worker_tasks.enqueue("new-test", {}, "new-task")
            self.assertEqual(task_id, 1001)

    def test_enqueue_repairs_legacy_high_water_without_another_cleanup(self) -> None:
        for caller_owned in (False, True):
            with self.subTest(caller_owned=caller_owned), _workspace() as (_database, repositories, _backup):
                repositories.notification_deliveries.record(task_id=41, event_type="old", channel="webhook", status="success")
                with repositories.connect() as connection:
                    connection.execute("DELETE FROM worker_tasks")
                    connection.execute("DELETE FROM sqlite_sequence WHERE name='worker_tasks'")
                payload = {"event_type": "new", "channels": ["webhook"]}
                if caller_owned:
                    # The repair must roll back together with the business
                    # transaction, not consume a new identity out of band.
                    with self.assertRaisesRegex(RuntimeError, "abort enqueue"):
                        with repositories.connect() as connection:
                            connection.execute("BEGIN IMMEDIATE")
                            repositories.worker_tasks.enqueue_with_connection(connection, "notification_deliver", payload, "aborted")
                            raise RuntimeError("abort enqueue")
                    with repositories.connect() as connection:
                        self.assertIsNone(connection.execute("SELECT seq FROM sqlite_sequence WHERE name='worker_tasks'").fetchone())
                        connection.execute("BEGIN IMMEDIATE")
                        task_id, created = repositories.worker_tasks.enqueue_with_connection(connection, "notification_deliver", payload, "new")
                else:
                    task_id, created = repositories.worker_tasks.enqueue("notification_deliver", payload, "new")
                self.assertTrue(created)
                self.assertEqual(task_id, 42)
                self.assertEqual(repositories.notification_deliveries.latest_status_by_channel(task_id), {})
                self.assertEqual(repositories.notification_deliveries.latest_status_by_channel(41), {"webhook": "success"})
                with repositories.connect() as connection:
                    connection.execute("UPDATE worker_tasks SET status='completed' WHERE id=?", (task_id,))
                self.assertEqual(repositories.worker_tasks.enqueue("notification_deliver", payload, "new"), (task_id, False))
                self.assertEqual(repositories.worker_tasks.get(task_id)["status"], "completed")

    def test_enqueue_allows_no_delivery_table_but_does_not_hide_invalid_schema(self) -> None:
        with _workspace() as (database, _repositories, _backup), closing(sqlite3.connect(":memory:")) as connection:
            with database.connect() as source:
                schema = source.execute("SELECT sql FROM sqlite_master WHERE name='worker_tasks'").fetchone()[0]
            connection.executescript(schema)
            connection.row_factory = sqlite3.Row
            repository = WorkerTaskRepository(lambda: connection, dict)
            self.assertEqual(repository.enqueue("test", {}, "without-notifications"), (1, True))
            connection.execute("CREATE TABLE notification_deliveries (wrong_column INTEGER)")
            with self.assertRaisesRegex(sqlite3.OperationalError, "task_id"):
                repository.enqueue("test", {}, "invalid-notifications")
            self.assertEqual(len(repository.list()), 1)

    def test_organizer_retry_and_unfinished_rollback_receipts_are_retained(self) -> None:
        cases = (
            ("failed", {"openlist_visibility_retry": {"next_retry_at": "2026-10-11T01:00:00Z"}}, {}, True),
            ("failed", {"openlist_visibility_retry": {"next_retry_at": "2026-10-11T01:00:00Z", "exhausted": True}}, {}, False),
            ("failed", {}, {}, False),
            ("waiting_review", {}, {"rollback": {"status": "running"}}, True),
            ("waiting_review", {}, {"rollback": {"status": "failed"}}, True),
            ("waiting_review", {}, {"rollback": {"status": "completed"}}, False),
        )
        with _workspace() as (_database, repositories, _backup):
            for status, raw, evidence, blocked in cases:
                with self.subTest(status=status, raw=raw, evidence=evidence):
                    task_id = repositories.organizer.create_organizer_task(
                        category="tv", openlist_root_path="/history", status=status, raw_data=raw, evidence=evidence,
                    )
                    if blocked:
                        with self.assertRaises(HistoryCleanupBlockedError):
                            repositories.update.cleanup_history_records(vacuum=False)
                        self.assertIsNotNone(repositories.organizer.get_organizer_task(task_id))
                        with repositories.connect() as connection:
                            connection.execute("DELETE FROM organizer_tasks WHERE id=?", (task_id,))
                    else:
                        self.assertEqual(repositories.update.cleanup_history_records(vacuum=False)["after"]["cleanup_total"], 0)

    def test_live_organizer_locks_block_but_expired_locks_can_be_cleared(self) -> None:
        with _workspace() as (_database, repositories, _backup):
            for expiry in (None, "2999-01-01T00:00:00Z"):
                with self.subTest(expiry=expiry):
                    with repositories.connect() as connection:
                        connection.execute(
                            "INSERT INTO organizer_locks(lock_key, expires_at, created_at) VALUES ('test-lock', ?, ?)",
                            (expiry, "2026-10-11T00:00:00Z"),
                        )
                    with self.assertRaises(HistoryCleanupBlockedError):
                        repositories.update.cleanup_history_records(vacuum=False)
                    with repositories.connect() as connection:
                        connection.execute("UPDATE organizer_locks SET expires_at='2000-01-01T00:00:00Z'")
                    self.assertEqual(repositories.update.cleanup_history_records(vacuum=False)["after"]["cleanup_total"], 0)

    def test_write_lock_prevents_new_work_between_check_and_delete(self) -> None:
        with _workspace() as (database, repositories, _backup):
            checked, proceed = threading.Event(), threading.Event()
            original = repositories.update._history_cleanup_blockers

            def pause_after_check(connection):
                blockers = original(connection)
                checked.set()
                if not proceed.wait(5):
                    raise AssertionError("cleanup synchronization timed out")
                return blockers

            with patch.object(repositories.update, "_history_cleanup_blockers", side_effect=pause_after_check), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(repositories.update.cleanup_history_records, vacuum=False)
                try:
                    self.assertTrue(checked.wait(5))
                    # A separate SQLite connection stands in for another Web
                    # or Worker process; no process-local mutex is involved.
                    with closing(sqlite3.connect(database.path, timeout=0.05)) as contender:
                        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                            contender.execute("UPDATE worker_tasks SET status=status")
                finally:
                    proceed.set()
                self.assertEqual(future.result(timeout=5)["after"]["cleanup_total"], 0)
            task_id, _ = repositories.worker_tasks.enqueue("new-work", {}, "after-cleanup")
            self.assertEqual(repositories.worker_tasks.get(task_id)["status"], "pending")

    def test_work_committed_before_cleanup_lock_is_detected_not_deleted(self) -> None:
        with _workspace() as (database, _repositories, backup):
            attempted = threading.Event()

            @contextmanager
            def traced_connection():
                with database.connect() as connection:
                    connection.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)
                    yield connection

            repositories = RepositorySet.build(traced_connection, backup)
            with ThreadPoolExecutor(max_workers=1) as pool:
                with database.connect() as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    task_id, _ = repositories.worker_tasks.enqueue_with_connection(writer, "new-work", {}, "before-cleanup")
                    future = pool.submit(repositories.update.cleanup_history_records, vacuum=False)
                    self.assertTrue(attempted.wait(5))
                    self.assertFalse(future.done())
                with self.assertRaises(HistoryCleanupBlockedError):
                    future.result(timeout=5)
            self.assertEqual(repositories.worker_tasks.get(task_id)["status"], "pending")

    def test_explicit_backup_is_created_before_a_quiescent_cleanup(self) -> None:
        with _workspace() as (database, _repositories, _backup):
            repositories = RepositorySet.from_database(database)
            _job(repositories)
            result = repositories.update.cleanup_history_records(backup=True, vacuum=False)
            self.assertTrue(Path(result["backup"]["path"]).is_relative_to(database.path.parent))
            with closing(sqlite3.connect(result["backup"]["path"])) as saved:
                self.assertEqual(saved.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0], 1)
            self.assertEqual(result["after"]["cleanup_total"], 0)


class HistoryCleanupUiTests(unittest.TestCase):
    def test_history_script_cache_version_is_refreshed(self) -> None:
        template = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        self.assertIn("/static/admin-system.js?v=20261011-history-cleanup-v001", template)

    @unittest.skipIf(shutil.which("node") is None, "node unavailable")
    def test_confirm_cancel_and_failure_use_direct_cleanup(self) -> None:
        script = r"""
globalThis.window = globalThis;
require(%s);
(async () => {
    const results = {};
    for (const scenario of ['success', 'cancel', 'failure', 'busy']) {
        const before = { cleanup_tables: { update_events: 3 }, cleanup_total: 3 };
        const after = { cleanup_tables: { update_events: 0 }, cleanup_total: 0 };
        const state = { historyMaintenance: before };
        const calls = [], dialogs = [], toasts = [];
        let refreshes = 0;
        const refresh = async () => { refreshes += 1; };
        const system = window.FnosAdminSystem.create({
            state, getElement: () => null, escapeHtml: String,
            confirmDialog: async (options) => {
                dialogs.push(options);
                return scenario !== 'cancel';
            },
            api: async (path, options) => {
                calls.push({ path, ...options, body: JSON.parse(options.body) });
                if (scenario === 'busy') return { success: false, code: 'HISTORY_CLEANUP_BUSY', message: '活动任务未结束，未清理任何记录' };
                return scenario === 'failure'
                    ? { success: false, message: 'cleanup failed' }
                    : { success: true, summary: after };
            },
            toast: (message, tone) => toasts.push({ message, tone }),
            loadOverview: refresh, loadRequests: refresh, loadJobs: refresh,
            loadRclone: refresh, loadOrganizer: refresh, loadUpdates: refresh,
        });
        await system.cleanupHistoryRecords();
        results[scenario] = { calls, dialogs, toasts, refreshes, summary: state.historyMaintenance };
    }
    process.stdout.write(JSON.stringify(results));
})().catch((error) => { console.error(error); process.exitCode = 1; });
""" % json.dumps(str(ROOT / "static" / "admin-system.js"))
        completed = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True, timeout=10,
        )
        results = json.loads(completed.stdout)
        for scenario, result in results.items():
            with self.subTest(scenario=scenario):
                self.assertEqual(len(result["dialogs"]), 1)
                self.assertEqual(result["dialogs"][0]["confirmText"], "直接清理")
                self.assertNotIn("自动备份", result["dialogs"][0]["message"])
                self.assertNotIn("备份并清理", result["dialogs"][0]["message"])
                if scenario == "cancel":
                    self.assertEqual(result["calls"], [])
                    self.assertEqual(result["toasts"], [])
                    self.assertEqual(result["refreshes"], 0)
                    self.assertEqual(result["summary"]["cleanup_total"], 3)
                    continue
                self.assertEqual(len(result["calls"]), 1)
                call = result["calls"][0]
                self.assertEqual(call["path"], "/api/admin/maintenance/cleanup-history")
                self.assertEqual(call["method"], "POST")
                self.assertEqual(call["body"], {"confirm": True, "backup": False, "vacuum": True})
                self.assertNotIn("备份", call["loadingMessage"])
                self.assertEqual(result["toasts"][0]["tone"], "success" if scenario == "success" else "error")
                self.assertEqual(result["refreshes"], 6 if scenario == "success" else 0)
                self.assertEqual(result["summary"]["cleanup_total"], 0 if scenario == "success" else 3)
        self.assertEqual(results["failure"]["toasts"][0]["message"], "cleanup failed")
        self.assertEqual(results["busy"]["toasts"][0]["message"], "活动任务未结束，未清理任何记录")


if __name__ == "__main__":
    unittest.main()
