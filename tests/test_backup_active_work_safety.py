from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock, patch

from tests.test_full_backup_export_import import (
    ROOT_KEY_A,
    _DatabaseWorkspace,
    _seed,
    _seed_auth_session,
    _service,
)


class BackupActiveWorkSafetyTests(unittest.TestCase):
    def test_abandoned_expired_waiting_authorization_does_not_block_import(self) -> None:
        with _DatabaseWorkspace() as repositories:
            _seed(repositories)
            _seed_auth_session(repositories)
            with repositories.connect() as connection:
                connection.execute(
                    "UPDATE provider_auth_sessions SET status='waiting', expires_at='2000-01-01T00:00:00Z'"
                )
            reload_runtime = Mock(return_value={"success": True})
            service = _service(repositories, ROOT_KEY_A, reload_runtime)
            document = service.export_document()[0]["document"]
            result, status = service.import_document(document, confirm=True)
            self.assertEqual(status, 200, result)
            with repositories.connect() as connection:
                row = connection.execute("SELECT status, row_version FROM provider_auth_sessions").fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["row_version"], 2)
            reload_runtime.assert_called_once()

    def test_expiration_does_not_release_polling_or_frozen_authorization_results(self) -> None:
        with _DatabaseWorkspace() as repositories:
            _seed(repositories)
            _seed_auth_session(repositories)
            service = _service(repositories, ROOT_KEY_A)
            document = service.export_document()[0]["document"]
            for auth_status in ("polling", "persisting", "persisting_active"):
                with self.subTest(auth_status=auth_status):
                    with repositories.connect() as connection:
                        connection.execute(
                            "UPDATE provider_auth_sessions SET status=?, expires_at='2000-01-01T00:00:00Z'",
                            (auth_status,),
                        )
                    result, status = service.import_document(document, confirm=True)
                    self.assertEqual(status, 409, result)
                    with repositories.connect() as connection:
                        row = connection.execute("SELECT status, row_version FROM provider_auth_sessions").fetchone()
                        self.assertEqual(row["status"], auth_status)
                        self.assertEqual(row["row_version"], 1)

    def test_import_rechecks_queued_work_inside_write_transaction(self) -> None:
        with _DatabaseWorkspace() as repositories:
            _seed(repositories)
            reload_runtime = Mock(return_value={"success": True})
            service = _service(repositories, ROOT_KEY_A, reload_runtime)
            document = service.export_document()[0]["document"]
            incoming = copy.deepcopy(document)
            incoming["app_settings"]["submission.mode"] = "auto"
            worker_ids = []

            def enqueue_after_validation(_tables):
                worker_id, _created = repositories.worker_tasks.enqueue(
                    "transfer_execute", {"job_id": 1}, "backup-race",
                )
                worker_ids.append(worker_id)
                return None

            with patch.object(service, "_verify_credentials", side_effect=enqueue_after_validation):
                result, status = service.import_document(incoming, confirm=True)

            self.assertEqual(status, 409, result)
            self.assertEqual(result["error_code"], "CONFIG_RESTORE_BUSY")
            self.assertEqual(repositories.worker_tasks.get(worker_ids[0])["status"], "pending")
            self.assertEqual(service.export_document()[0]["document"]["app_settings"], document["app_settings"])
            self.assertEqual(service.export_document()[0]["document"]["provider_accounts"], document["provider_accounts"])
            reload_runtime.assert_not_called()

    def test_import_preserves_active_organizer_rollback(self) -> None:
        with _DatabaseWorkspace() as repositories:
            service = _service(repositories, ROOT_KEY_A)
            document = service.export_document()[0]["document"]
            task_id = repositories.organizer.create_organizer_task(
                category="movie", openlist_root_path="/staging/job-1", status="waiting_review",
            )
            repositories.organizer.update_organizer_task(
                task_id, evidence={"rollback": {"status": "failed", "run_id": 1}},
            )
            result, status = service.import_document(document, confirm=True)
            self.assertEqual(status, 409, result)
            self.assertTrue(any(item["status"] == "rollback_pending" for item in result["blockers"]))

    def test_incomplete_authorizations_block_config_replacement(self) -> None:
        with _DatabaseWorkspace() as repositories:
            _seed(repositories)
            _seed_auth_session(repositories)
            reload_runtime = Mock(return_value={"success": True})
            service = _service(repositories, ROOT_KEY_A, reload_runtime)
            document = service.export_document()[0]["document"]
            document["app_settings"]["submission.mode"] = "auto"
            for auth_status in ("waiting", "polling", "persisting", "persisting_active"):
                with self.subTest(auth_status=auth_status):
                    with repositories.connect() as connection:
                        connection.execute("UPDATE provider_auth_sessions SET status=?", (auth_status,))
                    result, status = service.import_document(document, confirm=True)
                    self.assertEqual(status, 409, result)
                    self.assertEqual(result["blockers"][0]["table"], "provider_auth_sessions")
                    self.assertEqual(repositories.app_settings.get_all()["submission.mode"], "review")
                    with repositories.connect() as connection:
                        row = connection.execute("SELECT status, account_id FROM provider_auth_sessions").fetchone()
                        self.assertEqual((row["status"], row["account_id"]), (auth_status, "acc-1"))
            reload_runtime.assert_not_called()

    def test_reload_failure_does_not_rollback_revisions_used_by_new_work(self) -> None:
        for activity in ("worker", "authorization"):
            with self.subTest(activity=activity), _DatabaseWorkspace() as repositories:
                _seed(repositories)
                document = _service(repositories, ROOT_KEY_A).export_document()[0]["document"]
                document["app_settings"]["submission.mode"] = "auto"
                worker_ids = []

                def start_work_then_fail_reload():
                    if activity == "worker":
                        worker_id, _created = repositories.worker_tasks.enqueue(
                            "transfer_execute", {"job_id": 1}, "backup-reload-race",
                        )
                        worker_ids.append(worker_id)
                    else:
                        _seed_auth_session(repositories)
                        with repositories.connect() as connection:
                            connection.execute("UPDATE provider_auth_sessions SET status='persisting_active'")
                    raise RuntimeError("reload interrupted")

                result, status = _service(repositories, ROOT_KEY_A, start_work_then_fail_reload).import_document(
                    document, confirm=True,
                )
                self.assertEqual(status, 500, result)
                self.assertFalse(result["rolled_back"])
                self.assertEqual(repositories.app_settings.get_all()["submission.mode"], "auto")
                if activity == "worker":
                    self.assertEqual(repositories.worker_tasks.get(worker_ids[0])["status"], "pending")
                else:
                    with repositories.connect() as connection:
                        row = connection.execute("SELECT status, account_id FROM provider_auth_sessions").fetchone()
                        self.assertEqual((row["status"], row["account_id"]), ("persisting_active", "acc-1"))


if __name__ == "__main__":
    unittest.main()
