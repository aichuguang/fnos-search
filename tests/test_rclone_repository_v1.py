from __future__ import annotations

import unittest

from fnos_media_import.domain.models import SelectionSnapshot
from fnos_media_import.planning.job_plan import (
    attach_plan_integrity,
    compute_operation_key,
)
from fnos_media_import.repositories.rclone_repository import (
    _job_matches_staging_callback_paths,
    _job_owns_staging_directory,
)


def _v1_job(job_id: int = 12, strategy: str = "rclone_copy") -> dict:
    selection = SelectionSnapshot(provider_kind="quark")
    plan = {
        "version": 1,
        "plan_schema_version": 1,
        "job_id": job_id,
        "category": "movie",
        "source": {
            "provider_kind": "quark",
            "source_form": "share",
            "search_source_id": "quark",
            "canonical_source_id": "quark:share:test",
            "record_id": "src_test",
            "record_revision": 1,
            "account_id": "acct_quark",
            "account_revision": 1,
            "account_selection_revision": 1,
            "selection_hash": selection.selection_hash,
            "selection": selection.to_dict(),
        },
        "destination": {
            "id": "mobile_main" if strategy == "relay_upload_mobile" else "tianyi_main",
            "provider_kind": "mobile" if strategy == "relay_upload_mobile" else "tianyi",
            "target_revision": 1,
            "binding_id": "binding_mobile" if strategy == "relay_upload_mobile" else "binding_tianyi",
            "binding_revision": 1,
        },
        "routing": {
            "assignment_id": "assignment_quark_movie",
            "assignment_revision": 1,
            "search_source_id": "quark",
            "category_key": "movie",
            "destination_id": "mobile_main" if strategy == "relay_upload_mobile" else "tianyi_main",
            "enabled": True,
        },
        "target_category": {"category_key": "movie", "public_label": "电影"},
        "verification": {
            "expected_file_count": 0,
            "expected_total_size": 0,
            "minimum": ["file_count", "total_size"],
            "selection_hash": selection.selection_hash,
        },
        "transfer": {
            "strategy": strategy,
            "executor": "rclone" if strategy == "rclone_copy" else "mobile_relay",
            "idempotency_key": "",
        },
        "paths": {
            "source_landing": {
                "kind": "openlist",
                "namespace_id": "openlist:ol:quark",
                "path": f"/_fnos_ingress/job-{job_id}",
            },
            "destination_staging": None if strategy == "relay_upload_mobile" else {
                "kind": "openlist",
                "namespace_id": "openlist:ol:tianyi",
                "path": f"/_fnos_staging/movie/job-{job_id}",
            },
            "destination_final": {
                "kind": "native" if strategy == "relay_upload_mobile" else "openlist",
                "namespace_id": "native:mobile:mobile_lib" if strategy == "relay_upload_mobile" else "openlist:ol:tianyi",
                "path": f"/电影/job-{job_id}",
            },
            "local_temp": {
                "kind": "relay",
                "namespace_id": "local:relay",
                "path": f"/data/relay/job-{job_id}",
            } if strategy == "relay_upload_mobile" else None,
        },
    }
    plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
    attach_plan_integrity(plan)
    return {
        "id": job_id,
        "plan_version": 1,
        "category": "movie",
        "destination_id": "mobile_main" if strategy == "relay_upload_mobile" else "tianyi_main",
        "raw_data": {},
        "_canonical_execution_plan": plan,
    }


class RcloneRepositoryV1Tests(unittest.TestCase):
    def test_callback_ownership_uses_only_v1_execution_plan(self) -> None:
        job = _v1_job()
        self.assertTrue(_job_owns_staging_directory(job, 12))
        matched, has_roots = _job_matches_staging_callback_paths(
            job,
            "/_fnos_ingress/job-12/电影.mkv",
            "/_fnos_staging/movie/job-12/电影.mkv",
        )
        self.assertTrue(has_roots)
        self.assertTrue(matched)

    def test_legacy_staging_plan_is_never_callback_ownership(self) -> None:
        legacy = {
            "id": 12,
            "plan_version": 2,
            "raw_data": {
                "staging_plan": {
                    "enabled": True,
                    "job_id": 12,
                    "openlist_job_root": "/_fnos_staging/movie/job-12",
                }
            },
        }
        self.assertFalse(_job_owns_staging_directory(legacy, 12))
        self.assertEqual(
            _job_matches_staging_callback_paths(
                legacy,
                "/_fnos_ingress/job-12/电影.mkv",
                "/_fnos_staging/movie/job-12/电影.mkv",
            ),
            (False, False),
        )

    def test_callback_path_outside_frozen_job_roots_is_rejected(self) -> None:
        matched, has_roots = _job_matches_staging_callback_paths(
            _v1_job(),
            "/_fnos_ingress/job-99/其它.mkv",
            "/_fnos_staging/movie/job-99/其它.mkv",
        )
        self.assertTrue(has_roots)
        self.assertFalse(matched)

    def test_relay_callback_ownership_uses_source_and_local_temp_roots(self) -> None:
        job = _v1_job(strategy="relay_upload_mobile")
        self.assertTrue(_job_owns_staging_directory(job, 12))
        matched, has_roots = _job_matches_staging_callback_paths(
            job,
            "/_fnos_ingress/job-12/电影.mkv",
            "/data/relay/job-12/电影.mkv",
        )
        self.assertTrue(has_roots)
        self.assertTrue(matched)

        outside, has_roots = _job_matches_staging_callback_paths(
            job,
            "/_fnos_staging/movie/job-12/电影.mkv",
        )
        self.assertTrue(has_roots)
        self.assertFalse(outside)


if __name__ == "__main__":
    unittest.main()
