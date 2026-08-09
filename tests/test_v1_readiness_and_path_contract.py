from __future__ import annotations

import unittest
from types import SimpleNamespace

from fnos_media_import.domain.models import NativeLocator, SourceDescriptor
from fnos_media_import.planning.job_plan import (
    attach_plan_integrity,
    compute_operation_key,
    validated_v1_execution_plan_from_job,
)
from fnos_media_import.planning.plan_validator import PlanValidator
from fnos_media_import.planning.planner import ImportPlanner
from fnos_media_import.transfer import TransferError, TransferPlan
from fnos_media_import.transfer.provider_relay_executor import ProviderRelayExecutor


class _Mounts:
    class _Connections:
        @staticmethod
        def get_revision(connection_id: str, revision: int):
            return {"connection_id": connection_id, "revision": revision}

    connection_service = _Connections()

    @staticmethod
    def get_mount(mount_id: str):
        if mount_id == "source":
            return {
                "mount_id": "source",
                "namespace_id": "openlist:ol:source",
                "readable": True,
                "writable": False,
                "storage_identity_id": "provider:quark:account:src",
                "identity_root": "/",
            }
        if mount_id == "destination":
            return {
                "mount_id": "destination",
                "namespace_id": "openlist:ol:destination",
                "readable": True,
                "writable": False,
                "storage_identity_id": "provider:mobile:account:dst",
                "identity_root": "/",
            }
        return None

    @staticmethod
    def get_revision(mount_id: str, revision: int):
        if mount_id == "source" and revision == 1:
            return {
                "connection_id": "ol",
                "connection_revision": 1,
                "namespace_id": "openlist:ol:source",
                "provider_kind": "",
                "account_id": "",
                "storage_identity_id": "provider:quark:account:src",
                "identity_root": "/",
                "readable": True,
                "writable": False,
                "enabled": True,
                "identity_status": "declared",
                "native_locator": {"kind": "path", "value": "/FNOS"},
            }
        if mount_id == "destination" and revision == 1:
            return {
                "connection_id": "ol",
                "connection_revision": 1,
                "namespace_id": "openlist:ol:destination",
                "provider_kind": "mobile",
                "account_id": "dst",
                "storage_identity_id": "provider:mobile:account:dst",
                "identity_root": "/",
                "readable": True,
                "writable": False,
                "enabled": True,
                "identity_status": "verified",
                "native_locator": {
                    "kind": "folder_id",
                    "value": "root-folder-id",
                    "upload_verified": True,
                    "organizer_mutation_verified": True,
                },
            }
        return None


class V1PathContractTests(unittest.TestCase):
    def test_planner_relay_paths_freeze_native_operation_root_and_local_namespace(self) -> None:
        planner = object.__new__(ImportPlanner)
        planner.config = {"FNOS_RELAY_ROOT": "/data/relay"}
        category = SimpleNamespace(
            native_locator=NativeLocator("path", "/影视"),
            rclone_locator=None,
            organizer_locator=None,
        )
        target = SimpleNamespace(provider_kind="mobile", destination_id="mobile_main")
        paths = planner._build_paths(
            source=SourceDescriptor("quark", "share", "quark:share:1"),
            target=target,
            category=category,
            category_key="movie",
            job_id=7,
            strategy="relay_upload_provider",
            source_mount={
                "namespace_id": "openlist:ol:source",
                "readable": True,
            },
            organizer_enabled=False,
        )

        self.assertIsNotNone(paths["source_landing"])
        self.assertIsNone(paths["destination_staging"])
        self.assertEqual(paths["local_temp"]["namespace_id"], "local:relay")
        self.assertEqual(paths["local_temp"]["path"], "/data/relay/job-7")
        self.assertIsNotNone(paths["destination_final"])

    def test_plan_validator_requires_relay_operation_root(self) -> None:
        validator = object.__new__(PlanValidator)
        validator.config = {"FNOS_RELAY_ROOT": "/data/relay"}
        validator.mount_service = _Mounts()
        errors: list[str] = []
        validator._validate_paths(
            plan={"job_id": 7},
            source={},
            destination={"provider_kind": "mobile", "account_id": "dst"},
            target=None,
            paths={
                "source_landing": {
                    "kind": "openlist",
                    "namespace_id": "openlist:ol:source",
                    "path": "/_fnos_ingress/job-7",
                    "mount_id": "source",
                    "mount_revision": 1,
                    "connection_id": "ol",
                    "connection_revision": 1,
                    "storage_identity_id": "provider:quark:account:src",
                    "identity_root": "/",
                },
                "destination_final": {
                    "kind": "native",
                    "namespace_id": "native:mobile:mobile_main",
                    "path": "/影视",
                },
                "local_temp": {
                    "kind": "relay",
                    "namespace_id": "local:relay",
                    "path": "/data/relay/job-7",
                },
            },
            bindings={
                "source_mount_id": "source",
                "source_mount_revision": 1,
                "source_mount_namespace_id": "openlist:ol:source",
                "source_mount_storage_identity_id": "provider:quark:account:src",
                "source_mount_identity_root": "/",
                "source_connection_id": "ol",
                "source_connection_revision": 1,
                "source_native_locator": {"kind": "path", "value": "/FNOS"},
                "destination_mount_id": "destination",
                "destination_mount_revision": 1,
                "destination_mount_namespace_id": "openlist:ol:destination",
                "destination_mount_storage_identity_id": "provider:mobile:account:dst",
                "destination_mount_identity_root": "/",
                "destination_connection_id": "ol",
                "destination_connection_revision": 1,
            },
            strategy="relay_upload_provider",
            errors=errors,
        )
        self.assertEqual(errors, [])

    def test_transfer_plan_and_job_reader_require_relay_local_temp(self) -> None:
        plan = {
            "version": 1,
            "job_id": 7,
            "source": {"provider_kind": "quark", "source_form": "share", "canonical_source_id": "q:1"},
            "destination": {"id": "mobile_main", "provider_kind": "mobile"},
            "transfer": {"strategy": "relay_upload_provider", "executor": "provider_relay"},
            "paths": {
                "source_landing": {"namespace_id": "openlist:ol:source", "path": "/_fnos_ingress/job-7"},
                "destination_final": {"namespace_id": "native:mobile:mobile_main", "path": "/影视"},
            },
        }
        with self.assertRaisesRegex(TransferError, "local_temp"):
            TransferPlan.from_dict(plan)

        job = {
            "id": 7,
            "plan_version": 1,
            "destination_id": "mobile_main",
            "_canonical_execution_plan": plan,
            "raw_data": {},
        }
        with self.assertRaisesRegex(ValueError, "local_temp"):
            validated_v1_execution_plan_from_job(job)

    def test_job_reader_requires_both_v1_version_markers(self) -> None:
        plan = {
            "version": 1,
            "job_id": 7,
            "category": "movie",
            "source": {
                "provider_kind": "quark",
                "source_form": "share",
                "canonical_source_id": "q:1",
                "selection_hash": "sha256:invalid-for-test",
                "selection": {
                    "provider_kind": "quark",
                    "selection_hash": "sha256:invalid-for-test",
                    "hash": "sha256:invalid-for-test",
                },
            },
            "destination": {"id": "mobile_main", "provider_kind": "mobile"},
            "transfer": {
                "strategy": "relay_upload_provider",
                "executor": "provider_relay",
                "idempotency_key": "import:sha256:invalid-for-test",
            },
            "paths": {
                "source_landing": {
                    "kind": "openlist",
                    "namespace_id": "openlist:ol:source",
                    "path": "/_fnos_ingress/job-7",
                },
                "destination_final": {
                    "kind": "native",
                    "namespace_id": "native:mobile:mobile_main",
                    "path": "/影视",
                },
                "local_temp": {
                    "kind": "relay",
                    "namespace_id": "local:relay",
                    "path": "/data/relay/job-7",
                },
            },
        }
        plan["transfer"]["idempotency_key"] = compute_operation_key(plan)
        attach_plan_integrity(plan)
        job = {
            "id": 7,
            "plan_version": 1,
            "category": "movie",
            "destination_id": "mobile_main",
            "_canonical_execution_plan": plan,
            "raw_data": {},
        }
        with self.assertRaisesRegex(ValueError, "plan_schema_version"):
            validated_v1_execution_plan_from_job(job)

    def test_transfer_plan_detaches_nested_input(self) -> None:
        raw = {
            "version": 1,
            "job_id": 9,
            "source": {"provider_kind": "quark", "selection": {"selected_items": ["a"]}},
            "destination": {"id": "quark_main", "provider_kind": "quark"},
            "transfer": {"strategy": "native_save"},
            "paths": {
                "destination_final": {
                    "kind": "native",
                    "namespace_id": "native:quark:quark_main",
                    "path": "/电影",
                }
            },
        }
        parsed = TransferPlan.from_dict(raw)
        raw["source"]["selection"]["selected_items"].append("forged")
        raw["paths"]["destination_final"]["path"] = "/被篡改"
        self.assertEqual(parsed.source["selection"]["selected_items"], ["a"])
        self.assertEqual(parsed.destination_final["path"], "/电影")

    def test_relay_executor_accepts_frozen_relay_kind(self) -> None:
        plan = TransferPlan.from_dict(
            {
                "version": 1,
                "job_id": 7,
                "source": {"provider_kind": "quark"},
                "destination": {"id": "mobile_main", "provider_kind": "mobile"},
                "transfer": {"strategy": "relay_upload_provider"},
                "paths": {
                    "source_landing": {
                        "kind": "openlist",
                        "namespace_id": "openlist:ol:source",
                        "path": "/_fnos_ingress/job-7",
                    },
                    "destination_final": {
                        "kind": "native",
                        "namespace_id": "native:mobile:mobile_main",
                        "path": "/影视",
                    },
                    "local_temp": {
                        "kind": "relay",
                        "namespace_id": "local:relay",
                        "path": "/data/relay/job-7",
                    },
                },
            }
        )
        executor = ProviderRelayExecutor(object(), object(), relay_root="/data/relay").bind(plan)
        self.assertEqual(executor._relay_dir(), "/data/relay/job-7")


if __name__ == "__main__":
    unittest.main()
