from __future__ import annotations

import copy
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fnos_media_import.transfer import ErrorCode, TransferPlan
from fnos_media_import.transfer.dispatch import TransferDispatchService
from tests.test_transfer_v1 import _FakeDownloadAdapter, _FakeUploader, make_plan


ROOT = Path(__file__).resolve().parents[1]


class RelayAuthRevisionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.factory_calls = []
        self.uploaders = []
        self.next_job_id = 0
        adapters = {"quark": object()}
        for provider in ("mobile", "tianyi"):
            def uploader_for(account_id, revision, kind=provider):
                uploader = _FakeUploader(provider_kind=kind)
                self.factory_calls.append((kind, account_id, revision))
                self.uploaders.append(uploader)
                return uploader

            adapters[provider] = SimpleNamespace(
                capabilities=lambda: {"supports_relay_upload": True},
                relay_uploader_for=uploader_for,
            )
        self.accounts = Mock()
        self.dispatch = TransferDispatchService(
            database=object(), accounts_service=self.accounts,
            target_service=object(), mount_service=object(),
            registry=SimpleNamespace(get=adapters.__getitem__),
            rclone_service=object(), locator_resolver=object(),
            relay_root=str(ROOT / "codex_tmp" / "auth-revision-relay"),
            local_target_root=str(ROOT / "codex_tmp" / "auth-revision-targets"),
        )

    def _executor(self, account="account-A", revision=1, *, provider="mobile", target="target-A"):
        self.next_job_id += 1
        job_id = self.next_job_id
        raw = make_plan(
            job_id=job_id, source_kind="quark", dest_kind=provider,
            strategy="relay_upload_provider", landing=f"/landing/job-{job_id}",
            final=f"/targets/{target}",
            local_temp=f"{self.dispatch.relay_root}/job-{job_id}".replace("\\", "/"),
            idempotency_key=f"auth-revision-job-{job_id}",
        )
        raw["source"].update(account_id="source-account", account_revision=19)
        raw["destination"].update(id=target, account_id=account, account_revision=revision)
        raw["paths"]["destination_final"]["namespace_id"] = f"native:{provider}:{target}"
        executor = self.dispatch.build_executors(raw)["relay_upload_provider"]
        executor.rclone_adapter = _FakeDownloadAdapter(status="completed")
        executor.bind(TransferPlan.from_dict(raw))
        return executor, self.uploaders[-1]

    @staticmethod
    def _run(executor):
        return executor.poll(executor.submit())

    def _expire(self, account="account-A", revision=1, **kwargs):
        executor, uploader = self._executor(account, revision, **kwargs)
        uploader.auth_expired = True
        status = self._run(executor)
        self.assertEqual((status.status, status.error_code), ("failed", ErrorCode.AUTH_EXPIRED))
        self.assertEqual(len(uploader.calls), 1)
        return executor

    def test_mobile_and_tianyi_new_revisions_resume_but_old_revisions_stay_stopped(self):
        for provider in ("mobile", "tianyi"):
            with self.subTest(provider=provider):
                self._expire(provider=provider)
                old, old_uploader = self._executor(provider=provider)
                fresh, fresh_uploader = self._executor(revision=2, provider=provider)
                self.assertEqual(self._run(old).error_code, ErrorCode.AUTH_EXPIRED)
                self.assertEqual(old_uploader.calls, [])
                self.assertEqual(self._run(fresh).status, "completed")
                self.assertEqual(len(fresh_uploader.calls), 1)
                old_again, old_again_uploader = self._executor(provider=provider)
                self.assertEqual(self._run(old_again).error_code, ErrorCode.AUTH_EXPIRED)
                self.assertEqual(old_again_uploader.calls, [])
        self.assertEqual(self.accounts.mock_calls, [], "execution must not fall back to current account selection")
        self.assertIn(("mobile", "account-A", 2), self.factory_calls)
        self.assertIn(("tianyi", "account-A", 2), self.factory_calls)

    def test_different_account_is_not_stopped(self):
        self._expire()
        other, uploader = self._executor(account="account-B")
        self.assertEqual(self._run(other).status, "completed")
        self.assertEqual(len(uploader.calls), 1)

    def test_same_account_text_in_different_provider_is_not_stopped(self):
        self._expire(provider="mobile")
        other, uploader = self._executor(provider="tianyi")
        self.assertEqual(self._run(other).status, "completed")
        self.assertEqual(len(uploader.calls), 1)

    def test_same_credentials_share_auth_stop_across_targets_not_across_accounts(self):
        self._expire(target="movies")
        same_account, blocked = self._executor(target="series")
        self.assertEqual(self._run(same_account).error_code, ErrorCode.AUTH_EXPIRED)
        self.assertEqual(blocked.calls, [])
        other_account, allowed = self._executor(account="account-B", target="movies")
        self.assertEqual(self._run(other_account).status, "completed")
        self.assertEqual(len(allowed.calls), 1)

    def test_source_auth_failure_does_not_stop_destination_credentials(self):
        executor, uploader = self._executor()
        executor.rclone_adapter.check = Mock(return_value={
            "status": "failed", "error_code": ErrorCode.AUTH_EXPIRED,
        })
        self.assertEqual(self._run(executor).error_code, ErrorCode.AUTH_EXPIRED)
        self.assertEqual(uploader.calls, [])
        self.assertEqual(self.dispatch._halted_relay_account_ids, set())
        same_target, next_uploader = self._executor()
        self.assertEqual(self._run(same_target).status, "completed")
        self.assertEqual(len(next_uploader.calls), 1)

    def test_unversioned_old_set_cannot_poison_first_new_revision_or_old_revision(self):
        legacy = {"account-A", "unrelated-source-account"}
        self.dispatch._halted_relay_account_ids = legacy
        fresh, uploader = self._executor(revision=2)
        self.assertIs(fresh.halted_account_ids, legacy)
        self.assertEqual(self._run(fresh).status, "completed")
        self.assertEqual(len(uploader.calls), 1)
        older, older_uploader = self._executor(revision=1)
        self.assertEqual(self._run(older).status, "completed")
        self.assertEqual(len(older_uploader.calls), 1)
        self.assertEqual(legacy, {"unrelated-source-account"})

    def test_removing_unversioned_entry_preserves_known_old_revision_stop(self):
        self._expire()
        stopped_keys = set(self.dispatch._halted_relay_account_ids)
        self.dispatch._halted_relay_account_ids.add("account-A")
        fresh, uploader = self._executor(revision=2)
        self.assertEqual(self._run(fresh).status, "completed")
        self.assertEqual(len(uploader.calls), 1)
        self.assertEqual(self.dispatch._halted_relay_account_ids, stopped_keys)
        older, older_uploader = self._executor()
        self.assertEqual(self._run(older).error_code, ErrorCode.AUTH_EXPIRED)
        self.assertEqual(older_uploader.calls, [])

    def test_revisionless_legacy_call_stays_stopped_without_poisoning_real_revision(self):
        legacy = {"account-A"}
        self.dispatch._halted_relay_account_ids = legacy
        old, blocked = self._executor(revision=0)
        self.assertEqual(self._run(old).error_code, ErrorCode.AUTH_EXPIRED)
        self.assertEqual(blocked.calls, [])
        fresh, allowed = self._executor(revision=1)
        self.assertEqual(self._run(fresh).status, "completed")
        self.assertEqual(len(allowed.calls), 1)

    def test_legacy_destination_fallback_does_not_collide_with_account_or_other_target(self):
        self.dispatch._halted_relay_account_ids.add("target-A")
        old_target, blocked = self._executor(account="", revision=0)
        self.assertEqual(self._run(old_target).error_code, ErrorCode.AUTH_EXPIRED)
        self.assertEqual(blocked.calls, [])
        other_target, allowed = self._executor(account="", revision=0, target="target-B")
        real_account, account_uploader = self._executor(account="target-A", revision=0)
        self.assertEqual(self._run(other_target).status, "completed")
        self.assertEqual(self._run(real_account).status, "completed")
        self.assertEqual((len(allowed.calls), len(account_uploader.calls)), (1, 1))

    def test_old_auth_response_arriving_after_new_revision_success_cannot_stop_new_work(self):
        old, old_uploader = self._executor()
        old_uploader.auth_expired = True
        original_upload = old_uploader.upload_local_dir
        entered = threading.Event()
        release = threading.Event()

        def delayed_upload(**kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("old upload was not released")
            return original_upload(**kwargs)

        old_uploader.upload_local_dir = delayed_upload
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self._run, old)
            try:
                self.assertTrue(entered.wait(2))
                fresh, fresh_uploader = self._executor(revision=2)
                self.assertEqual(self._run(fresh).status, "completed")
                self.assertEqual(len(fresh_uploader.calls), 1)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2).error_code, ErrorCode.AUTH_EXPIRED)
        newer, next_uploader = self._executor(revision=2)
        self.assertEqual(self._run(newer).status, "completed")
        self.assertEqual(len(next_uploader.calls), 1)

    def test_concurrent_same_revision_failures_emit_one_auth_alert(self):
        alert = Mock()
        barrier = threading.Barrier(2)
        executors = []
        for _ in range(2):
            executor, uploader = self._executor()
            uploader.auth_expired = True
            upload = uploader.upload_local_dir

            def fail_together(method=upload, **kwargs):
                barrier.wait(timeout=2)
                return method(**kwargs)

            uploader.upload_local_dir = fail_together
            executor.alert_service = alert
            executors.append(executor)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self._run, executors))
        self.assertEqual([item.error_code for item in results], [ErrorCode.AUTH_EXPIRED] * 2)
        alert.assert_called_once()
        self.assertEqual(len(self.dispatch._halted_relay_account_ids), 1)

    def test_known_upload_results_take_precedence_over_auth_stop_without_submission(self):
        self._expire()
        for stored_status, expected in (("unknown", "unknown"), ("completed", "completed")):
            with self.subTest(stored_status=stored_status):
                executor, uploader = self._executor()
                receipt = executor.submit()
                receipt.metadata.update(
                    upload_outcome={"status": stored_status},
                    upload_submission_fence={"state": stored_status, "operation_key": "frozen-upload"},
                )
                metadata = copy.deepcopy(receipt.metadata)
                result = executor.poll(receipt)
                self.assertEqual(result.status, expected)
                self.assertEqual(receipt.metadata, metadata)
                self.assertEqual(uploader.calls, [])

    def test_new_authentication_never_resubmits_an_unknown_upload_receipt(self):
        self._expire()
        executor, uploader = self._executor(revision=2)
        receipt = executor.submit()
        receipt.metadata.update(step="upload", upload_outcome={"status": "unknown"})
        result = executor.poll(receipt)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(uploader.calls, [])

    def test_non_auth_upload_failure_does_not_stop_same_credentials(self):
        executor, uploader = self._executor()
        uploader.status = "failed"
        self.assertEqual(self._run(executor).status, "failed")
        self.assertEqual(self.dispatch._halted_relay_account_ids, set())
        retry, next_uploader = self._executor()
        self.assertEqual(self._run(retry).status, "completed")
        self.assertEqual(len(next_uploader.calls), 1)
