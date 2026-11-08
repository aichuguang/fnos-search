from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fnos_media_import.database import Database
from fnos_media_import.migrations import Migration, MigrationRunner
from fnos_media_import.planning.job_plan import canonical_plan_json
from fnos_media_import.v1_cutover import (
    CUTOVER_MESSAGE,
    CUTOVER_NAME,
    STRM_PENDING_CUTOVER_MESSAGE,
    V1JobContractHardCutover,
    analyze_database,
    apply_database,
)
from tests._organizer_v1_fixtures import organizer_v1_plan


NOW = "2026-08-09T00:00:00+00:00"


def _encoded_plan(job_id: int) -> tuple[dict[str, object], str, str]:
    plan = organizer_v1_plan(job_id)
    encoded = canonical_plan_json(plan)
    return plan, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class V1CutoverMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1] / "codex_tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"v1-cutover-{uuid.uuid4().hex}.db"

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                candidate.unlink()
        backup_dir = self.path.parent / "backups"
        if backup_dir.exists():
            for version in (29, 30):
                for candidate in backup_dir.glob(f"app_before_schema_migration_v{version}_*.db"):
                    candidate.unlink()
            try:
                backup_dir.rmdir()
            except OSError:
                pass

    def test_v28_mixed_database_keeps_only_valid_v1_jobs_and_cleans_relations(self) -> None:
        self._build_v28_mixed_database()
        dry_run = analyze_database(self.path)

        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(dry_run["jobs"]["kept"], 1)
        self.assertEqual(dry_run["jobs"]["deleted"], 7)
        self.assertEqual(
            dry_run["jobs"]["invalid_reasons"],
            {
                "execution_plan_json": 1,
                "execution_plan_not_canonical": 1,
                "execution_plan_row_hash": 1,
                "execution_plan_schema_version": 1,
                "execution_plan_validation": 1,
                "job_plan_version": 1,
                "missing_execution_plan": 1,
            },
        )
        self.assertEqual(dry_run["cleanup"]["advanced_config_fields_removed"], 5)
        self.assertEqual(dry_run["cleanup"]["organizer_strm_pending_tasks"], 1)
        self.assertEqual(dry_run["cleanup"]["organizer_strm_worker_tasks"], 1)
        self.assertEqual(dry_run["cleanup"]["organizer_ai_suggestions"], 1)
        self.assertEqual(dry_run["cleanup"]["organizer_tmdb_matches"], 1)
        self.assertEqual(dry_run["cleanup"]["worker_tasks"], 5)

        database = Database(self.path)
        with self.assertRaisesRegex(RuntimeError, "v28.*v1_cutover"):
            database.init_schema()
        summary = apply_database(self.path)

        backup_path = Path(str(summary.get("backup_path") or ""))
        self.assertTrue(backup_path.is_file())
        self.assertEqual(summary.get("migration"), CUTOVER_NAME)
        self.assertEqual(summary.get("jobs", {}).get("kept"), 1)
        self.assertEqual(summary.get("jobs", {}).get("deleted"), 7)
        self.assertTrue(summary.get("jobs", {}).get("deleted_job_ids_sha256"))
        self.assertNotIn("deleted_job_ids", summary.get("jobs", {}))
        self.assertEqual(30, summary["target_schema_version"])
        self.assertEqual([30], summary["post_cutover_migrations"])

        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(import_jobs)")
            }
            self.assertIn("source_provider_kind", columns)
            self.assertIn("source_form", columns)
            self.assertNotIn("source_type", columns)
            self.assertNotIn("target_route", columns)

            jobs = connection.execute(
                "SELECT id, source_provider_kind, source_form, password, plan_version "
                "FROM import_jobs ORDER BY id"
            ).fetchall()
            self.assertEqual([(1, "quark", "share", "", 1)], [tuple(row) for row in jobs])
            self.assertEqual(
                [(1,)],
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT job_id FROM import_execution_plans ORDER BY job_id"
                    ).fetchall()
                ],
            )
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM job_events").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM transfer_executions").fetchone()[0])

            guest = dict(
                connection.execute(
                    "SELECT job_id, status, public_status, raw_data FROM guest_requests WHERE id=10"
                ).fetchone()
            )
            self.assertIsNone(guest["job_id"])
            self.assertEqual("unsupported", guest["status"])
            self.assertIn(CUTOVER_MESSAGE, guest["raw_data"])
            self.assertEqual(
                CUTOVER_MESSAGE,
                connection.execute(
                    "SELECT message FROM guest_request_events WHERE request_id=10 "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()[0],
            )

            update = connection.execute(
                "SELECT job_id, decision, reason FROM update_candidates WHERE id=20"
            ).fetchone()
            self.assertEqual((None, "rejected", CUTOVER_MESSAGE), tuple(update))
            trending = connection.execute(
                "SELECT initial_import_job_id, status, ignore_reason "
                "FROM trending_candidates WHERE id=30"
            ).fetchone()
            self.assertEqual((None, "import_failed", CUTOVER_MESSAGE), tuple(trending))

            for table in (
                "organizer_operations",
                "organizer_ai_suggestions",
                "organizer_tmdb_matches",
                "organizer_runs",
                "organizer_mappings",
                "organizer_files",
                "organizer_locks",
                "rclone_file_events",
                "rclone_events",
                "rclone_runs",
            ):
                self.assertEqual(
                    0,
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    table,
                )
            strm_task = dict(
                connection.execute(
                    "SELECT status, error_message, raw_data, evidence, revision "
                    "FROM organizer_tasks WHERE id=56"
                ).fetchone()
            )
            self.assertEqual("waiting_review", strm_task["status"])
            self.assertEqual(STRM_PENDING_CUTOVER_MESSAGE, strm_task["error_message"])
            self.assertEqual(2, strm_task["revision"])
            for field in ("raw_data", "evidence"):
                payload = json.loads(strm_task[field])
                completion = payload["strm_completion"]
                self.assertEqual("retired_by_v1_cutover", completion["status"])
                self.assertNotIn("next_retry_at", completion)
                self.assertEqual(CUTOVER_NAME, payload["v1_cutover"]["migration"])
            self.assertEqual(
                [(63,)],
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT id FROM worker_tasks ORDER BY id"
                    ).fetchall()
                ],
            )
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            advanced = json.loads(
                connection.execute(
                    "SELECT value FROM app_settings WHERE key='advanced_config'"
                ).fetchone()[0]
            )
            self.assertEqual(
                advanced,
                {
                    "openlist": {"timeout": 30},
                    "pansou": {"base_url": "http://pansou"},
                    "rclone": {"staging_retry_delay_seconds": 30},
                },
            )
            self.assertEqual(
                [(29, CUTOVER_NAME)],
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT version, name FROM schema_migrations WHERE version=29"
                    ).fetchall()
                ],
            )

        Database(self.path).init_schema()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM import_execution_plans").fetchone()[0])
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=29"
                ).fetchone()[0],
            )
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_offline_apply_backs_up_and_old_runner_rejects_v30(self) -> None:
        self._build_v28_mixed_database()

        summary = apply_database(self.path)

        self.assertFalse(summary["dry_run"])
        self.assertEqual(summary["jobs"]["kept"], 1)
        self.assertEqual(summary["jobs"]["deleted"], 7)
        self.assertTrue(Path(summary["backup_path"]).is_file())
        self.assertFalse(self.path.with_name(f".{self.path.name}.v1-hard-cutover.lock").exists())

        old_runner = MigrationRunner(
            Migration(version, f"legacy_{version}", lambda _connection: None)
            for version in range(1, 29)
        )
        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaisesRegex(RuntimeError, "v30.*v28"):
                old_runner.ensure_compatible(connection)

    def test_offline_apply_does_not_remove_another_process_lock(self) -> None:
        self._build_v28_mixed_database()
        lock_path = self.path.with_name(f".{self.path.name}.v1-hard-cutover.lock")
        lock_path.write_text("pid=4242\n", encoding="ascii")

        try:
            with self.assertRaises(FileExistsError):
                apply_database(self.path)

            self.assertEqual("pid=4242\n", lock_path.read_text(encoding="ascii"))
        finally:
            lock_path.unlink(missing_ok=True)

    def test_dry_run_does_not_modify_source_database(self) -> None:
        self._build_v28_mixed_database()
        before = self.path.read_bytes()

        summary = analyze_database(self.path)

        self.assertTrue(summary["dry_run"])
        self.assertTrue(summary["simulation_validated"])
        self.assertEqual(before, self.path.read_bytes())

    def test_startup_rejection_does_not_modify_legacy_database(self) -> None:
        self._build_v28_mixed_database()
        before = self.path.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "v28.*v1_cutover"):
            Database(self.path).init_schema()

        self.assertEqual(before, self.path.read_bytes())

    def test_apply_rejects_active_writer_transaction(self) -> None:
        self._build_v28_mixed_database()
        owner = sqlite3.connect(self.path, isolation_level=None)
        try:
            owner.execute("BEGIN IMMEDIATE")
            owner.execute(
                "UPDATE app_settings SET value=value WHERE key='advanced_config'"
            )
            with self.assertRaisesRegex(RuntimeError, "数据库仍被其他进程占用"):
                apply_database(self.path)
        finally:
            owner.rollback()
            owner.close()

    def test_staged_cutover_failure_keeps_source_database_unchanged(self) -> None:
        self._build_v28_mixed_database()
        before = self.path.read_bytes()

        with patch(
            "fnos_media_import.v1_cutover._apply_v29_to_prepared_copy",
            side_effect=RuntimeError("staged failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "staged failure"):
                apply_database(self.path)

        self.assertEqual(before, self.path.read_bytes())

    def test_cutover_preserves_caller_owned_exclusive_transaction(self) -> None:
        self._build_v28_mixed_database()
        owner = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        owner.row_factory = sqlite3.Row
        contender = sqlite3.connect(self.path, timeout=0.05, isolation_level=None)
        try:
            owner.execute("PRAGMA foreign_keys=OFF")
            owner.execute("BEGIN EXCLUSIVE")

            V1JobContractHardCutover().apply(owner)

            self.assertTrue(owner.in_transaction)
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            if owner.in_transaction:
                owner.rollback()
            contender.close()
            owner.close()

    def test_fresh_database_uses_v30_schema_and_v29_contract_directly(self) -> None:
        database = Database(self.path)
        database.init_schema()

        with closing(sqlite3.connect(self.path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(import_jobs)")
            }
            self.assertIn("source_provider_kind", columns)
            self.assertIn("source_form", columns)
            self.assertNotIn("source_type", columns)
            self.assertNotIn("target_route", columns)
            self.assertEqual(30, connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
            self.assertEqual(
                (CUTOVER_NAME,),
                connection.execute(
                    "SELECT name FROM schema_migrations WHERE version=29"
                ).fetchone(),
            )

    def test_v29_additive_upgrade_preserves_strm_history_idempotently(self) -> None:
        Database(self.path).init_schema()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "INSERT INTO organizer_tasks("
                "id,category,openlist_root_path,status,error_message,evidence,raw_data,"
                "revision,scan_owner,scan_lease_expires_at,created_at,updated_at"
                ") VALUES (1,'movie','/organizer','strm_pending','旧错误',?,?,"
                "3,'old-owner','2099-01-01T00:00:00+00:00',?,?)",
                (
                    json.dumps(
                        {
                            "strm_completion": {
                                "status": "pending",
                                "next_retry_at": "2099-01-01T00:00:00+00:00",
                            }
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "strm_completion": {
                                "status": "pending",
                                "next_retry_at": "2099-01-01T00:00:00+00:00",
                            }
                        },
                        ensure_ascii=False,
                    ),
                    NOW,
                    NOW,
                ),
            )
            connection.executemany(
                "INSERT INTO worker_tasks("
                "id,task_type,payload,status,idempotency_key,available_at,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (
                    (
                        1,
                        "organizer_process",
                        json.dumps({"task_id": 1}),
                        "pending",
                        "organizer-process:1",
                        NOW,
                        NOW,
                        NOW,
                    ),
                    (
                        2,
                        "organizer_apply",
                        json.dumps({"task_id": 1}),
                        "completed",
                        "organizer-apply:1",
                        NOW,
                        NOW,
                        NOW,
                    ),
                    (
                        3,
                        "organizer_process",
                        json.dumps({"task_id": 999}),
                        "pending",
                        "organizer-process:999",
                        NOW,
                        NOW,
                        NOW,
                    ),
                ),
            )
            connection.execute("DELETE FROM schema_migrations WHERE version=30")
            connection.commit()

        summary = apply_database(self.path)
        self.assertTrue(summary["additive_only"])
        self.assertEqual(29, summary["upgraded_from_schema_version"])
        self.assertEqual(30, summary["target_schema_version"])
        self.assertEqual([30], summary["post_cutover_migrations"])
        self.assertNotIn("cleanup", summary)
        self.assertTrue(Path(summary["backup_path"]).is_file())

        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            task = dict(
                connection.execute(
                    "SELECT status,error_message,raw_data,evidence,revision,scan_owner,"
                    "scan_lease_expires_at FROM organizer_tasks WHERE id=1"
                ).fetchone()
            )
            self.assertEqual("strm_pending", task["status"])
            self.assertEqual("旧错误", task["error_message"])
            self.assertEqual(3, task["revision"])
            self.assertEqual("old-owner", task["scan_owner"])
            self.assertEqual("2099-01-01T00:00:00+00:00", task["scan_lease_expires_at"])
            self.assertEqual(
                "pending",
                json.loads(task["raw_data"])["strm_completion"]["status"],
            )
            self.assertEqual(
                "pending",
                json.loads(task["evidence"])["strm_completion"]["status"],
            )
            self.assertEqual(
                [(1, "pending"), (2, "completed"), (3, "pending")],
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT id,status FROM worker_tasks ORDER BY id"
                    ).fetchall()
                ],
            )

        repeated = apply_database(self.path)
        self.assertTrue(repeated["already_applied"])
        self.assertTrue(repeated["additive_only"])
        self.assertEqual([], repeated["post_cutover_migrations"])
        self.assertNotIn("cleanup", repeated)
        self.assertFalse(repeated.get("backup_path"))

    def _build_v28_mixed_database(self) -> None:
        Database(self.path).init_schema()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript(
                """
                DROP TABLE import_execution_plans;
                DROP TABLE import_jobs;
                CREATE TABLE import_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    category_label TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    password TEXT,
                    target_route TEXT NOT NULL,
                    target_path TEXT,
                    status TEXT NOT NULL,
                    external_task_id TEXT,
                    error_message TEXT,
                    raw_data TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    idempotency_key TEXT,
                    config_revision INTEGER NOT NULL DEFAULT 1,
                    executor_id TEXT,
                    destination_id TEXT,
                    plan_version INTEGER
                );
                CREATE UNIQUE INDEX idx_jobs_idempotency_key ON import_jobs(idempotency_key);
                CREATE INDEX idx_jobs_status ON import_jobs(status);
                CREATE INDEX idx_jobs_source_type ON import_jobs(source_type);
                CREATE TABLE import_execution_plans (
                    job_id INTEGER PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    plan_json TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES import_jobs(id) ON DELETE CASCADE
                );
                DELETE FROM schema_migrations WHERE version>=29;
                """
            )
            for job_id in range(1, 9):
                plan_version = 2 if job_id == 6 else 1
                connection.execute(
                    "INSERT INTO import_jobs("
                    "id,title,category,category_label,source_type,source_url,password,"
                    "target_route,target_path,status,raw_data,created_at,updated_at,"
                    "idempotency_key,config_revision,executor_id,destination_id,plan_version"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        f"job-{job_id}",
                        "movie",
                        "Movie",
                        "quark",
                        f"source-record:record-{job_id}@1",
                        "legacy-secret",
                        "quark_direct",
                        f"/target/{job_id}",
                        "created",
                        "{}",
                        NOW,
                        NOW,
                        f"v1:{job_id}",
                        1,
                        "worker",
                        "native-main",
                        plan_version,
                    ),
                )

            _plan1, encoded1, hash1 = _encoded_plan(1)
            connection.execute(
                "INSERT INTO import_execution_plans VALUES (1,1,?,?,?)",
                (encoded1, hash1, NOW),
            )
            _plan3, encoded3, hash3 = _encoded_plan(3)
            connection.execute(
                "INSERT INTO import_execution_plans VALUES (3,2,?,?,?)",
                (encoded3, hash3, NOW),
            )
            broken_json = "{not-json"
            connection.execute(
                "INSERT INTO import_execution_plans VALUES (4,1,?,?,?)",
                (
                    broken_json,
                    hashlib.sha256(broken_json.encode("utf-8")).hexdigest(),
                    NOW,
                ),
            )
            _plan5, encoded5, _hash5 = _encoded_plan(5)
            connection.execute(
                "INSERT INTO import_execution_plans VALUES (5,1,?,?,?)",
                (encoded5, "0" * 64, NOW),
            )
            plan7, _encoded7, _hash7 = _encoded_plan(7)
            pretty7 = json.dumps(plan7, ensure_ascii=False, sort_keys=True, indent=2)
            connection.execute(
                "INSERT INTO import_execution_plans VALUES (7,1,?,?,?)",
                (
                    pretty7,
                    hashlib.sha256(pretty7.encode("utf-8")).hexdigest(),
                    NOW,
                ),
            )
            plan8, _encoded8, _hash8 = _encoded_plan(8)
            plan8["integrity"]["plan_hash"] = "invalid-integrity-hash"
            encoded8 = canonical_plan_json(plan8)
            connection.execute(
                "INSERT INTO import_execution_plans VALUES (8,1,?,?,?)",
                (
                    encoded8,
                    hashlib.sha256(encoded8.encode("utf-8")).hexdigest(),
                    NOW,
                ),
            )

            connection.execute(
                "INSERT INTO job_events(id,job_id,level,message,created_at) "
                "VALUES (9,2,'info','old',?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO guest_requests("
                "id,request_token,job_id,title,category,source_type,source_url,"
                "source_url_hash,status,public_status,raw_data,created_at,updated_at"
                ") VALUES (10,'REQ-10',2,'request','movie','quark','masked','hash',"
                "'submitted','submitted','{}',?,?)",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO guest_request_events(id,request_id,level,message,created_at) "
                "VALUES (11,10,'info','created',?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO update_subscriptions("
                "id,title,category,media_type,schedule_kind,timezone,source_strategy,"
                "auto_import_policy,status,created_at,updated_at"
                ") VALUES (1,'subscription','tv','tv','interval','Asia/Shanghai',"
                "'search','manual','active',?,?)",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO update_candidates("
                "id,subscription_id,job_id,title,source_type,source_url,source_url_hash,"
                "decision,created_at"
                ") VALUES (20,1,2,'candidate','quark','masked','hash','imported',?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO trending_candidates("
                "id,canonical_key,source,title,status,first_seen_at,last_seen_at,"
                "initial_import_job_id,created_at,updated_at"
                ") VALUES (30,'trend-30','tmdb','trend','importing',?,?,2,?,?)",
                (NOW, NOW, NOW, NOW),
            )
            connection.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (
                    "advanced_config",
                    json.dumps(
                        {
                            "quark": {"token": "legacy"},
                            "openlist": {"base_url": "http://legacy", "timeout": 30},
                            "pansou": {
                                "base_url": "http://pansou",
                                "unknown_option": True,
                            },
                            "routes": {"quark": {"enabled": True}},
                            "rclone": {
                                "auto_interval_minutes": 15,
                                "staging_retry_delay_seconds": 30,
                            },
                        },
                        ensure_ascii=False,
                    ),
                    NOW,
                ),
            )
            connection.execute(
                "INSERT INTO rclone_runs("
                "id,operation_key,job_id,copy_descriptor_json,status,started_at,updated_at"
                ") VALUES (40,'operation-40',2,'{}','running',?,?)",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO rclone_events(id,run_id,level,message,created_at) "
                "VALUES (41,40,'info','running',?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO rclone_file_events("
                "id,run_id,job_id,status,level,filename,created_at"
                ") VALUES (42,40,2,'running','info','movie.mkv',?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO organizer_tasks("
                "id,job_id,request_id,rclone_run_id,category,openlist_root_path,status,"
                "created_at,updated_at"
                ") VALUES (50,2,10,40,'movie','/organizer','pending',?,?)",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO organizer_files(id,task_id,path,name,created_at) "
                "VALUES (51,50,'/source/movie.mkv','movie.mkv',?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO organizer_mappings("
                "id,task_id,file_id,source_path,target_path,status,created_at,updated_at"
                ") VALUES (52,50,51,'/source/movie.mkv','/target/movie.mkv','planned',?,?)",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO organizer_runs(id,task_id,status,started_at) "
                "VALUES (53,50,'running',?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO organizer_operations("
                "id,task_id,mapping_id,run_id,type,status,created_at,updated_at"
                ") VALUES (54,50,52,53,'move','pending',?,?)",
                (NOW, NOW),
            )
            connection.execute(
                "INSERT INTO organizer_locks(id,lock_key,task_id,run_id,created_at) "
                "VALUES (55,'lock-55',50,53,?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO organizer_ai_suggestions("
                "id,task_id,provider,model,prompt,response,parsed,created_at"
                ") VALUES (57,50,'test','model','prompt','response','{}',?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO organizer_tmdb_matches("
                "id,task_id,query,media_type,tmdb_id,title,year,score,raw_data,created_at"
                ") VALUES (58,50,'query','movie',123,'Movie','2026',1.0,'{}',?)",
                (NOW,),
            )
            connection.execute(
                "INSERT INTO organizer_tasks("
                "id,job_id,category,openlist_root_path,status,error_message,evidence,raw_data,"
                "revision,scan_owner,scan_lease_expires_at,created_at,updated_at"
                ") VALUES (56,1,'movie','/organizer/kept','strm_pending','旧等待原因',?,?,"
                "1,'legacy-worker','2099-01-01T00:00:00+00:00',?,?)",
                (
                    json.dumps(
                        {
                            "strm_completion": {
                                "status": "pending",
                                "next_retry_at": "2099-01-01T00:00:00+00:00",
                            }
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "strm_completion": {
                                "status": "pending",
                                "next_retry_at": "2099-01-01T00:00:00+00:00",
                            }
                        },
                        ensure_ascii=False,
                    ),
                    NOW,
                    NOW,
                ),
            )
            connection.execute(
                "INSERT INTO transfer_executions("
                "job_id,strategy,phase,organizer_task_id,created_at,updated_at"
                ") VALUES (2,'native_save','organizing',50,?,?)",
                (NOW, NOW),
            )
            worker_rows = (
                (60, json.dumps({"job_id": 2}), "worker-job-2"),
                (61, json.dumps({"task_id": 50}), "worker-task-50"),
                (62, json.dumps({"guest_request_id": 10}), "worker-request-10"),
                (63, json.dumps({"job_id": 1}), "worker-job-1"),
            )
            connection.executemany(
                "INSERT INTO worker_tasks("
                "id,task_type,payload,idempotency_key,available_at,created_at,updated_at"
                ") VALUES (?,'import',?,?,?, ?, ?)",
                [
                    (worker_id, payload, key, NOW, NOW, NOW)
                    for worker_id, payload, key in worker_rows
                ],
            )
            connection.execute(
                "INSERT INTO worker_tasks("
                "id,task_type,payload,idempotency_key,available_at,created_at,updated_at"
                ") VALUES (64,'rclone_repair','{}','retired-rclone-repair',?,?,?)",
                (NOW, NOW, NOW),
            )
            connection.execute(
                "INSERT INTO worker_tasks("
                "id,task_type,payload,status,idempotency_key,available_at,created_at,updated_at"
                ") VALUES (65,'organizer_apply',?,'pending','organizer-apply:56',?,?,?)",
                (json.dumps({"task_id": 56}), NOW, NOW, NOW),
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())


if __name__ == "__main__":
    unittest.main()
