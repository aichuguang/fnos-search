from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from fnos_media_import.services.trending_initial_import_service import (
    TrendingInitialImportError,
    TrendingInitialImportService,
)
from fnos_media_import.services.update_candidate_import_service import (
    UpdateCandidateImportService,
)


class TrendingInternalImportTests(unittest.TestCase):
    @staticmethod
    def _service() -> TrendingInitialImportService:
        service = object.__new__(TrendingInitialImportService)
        service.sanitize_quark_selection = lambda _value: {}
        service.sanitize_cloud139_selection = lambda _value: {}
        service.sanitize_string_list = lambda _value: []
        return service

    def test_trending_payload_cannot_select_infrastructure(self) -> None:
        service = self._service()
        payload = service._import_payload(
            candidate={"id": 11, "title": "Example"},
            resource={
                "id": 22,
                "url": "https://pan.quark.cn/s/example",
                "title": "Example",
                "_trending_public_id": "public-22",
            },
            category="movie",
            request_payload={
                "destination_id": "attacker-target",
                "executor_id": "attacker",
                "config_revision": 999,
            },
        )

        self.assertEqual(payload["resource_id"], 22)
        self.assertEqual(payload["public_id"], "public-22")
        for field in ("destination_id", "idempotency_key", "executor_id", "config_revision"):
            self.assertNotIn(field, payload)

    def test_existing_trending_job_is_matched_by_safe_resource_identity(self) -> None:
        service = self._service()
        service.get_job = lambda _job_id: {
            "id": 9,
            "status": "transferring",
            "raw_data": {"request": {"resource_id": 22, "public_id": "public-22"}},
        }
        service.repository = SimpleNamespace(
            bind_trending_candidate_initial_import_job=lambda *_args, **_kwargs: True,
            get_trending_candidate=lambda _candidate_id: None,
        )
        candidate = {"id": 11, "status": "importing", "initial_import_job_id": 9}

        result = service._existing_import_result(
            candidate,
            {"resource_id": 22, "public_id": "public-22"},
        )
        self.assertIsNotNone(result)
        with self.assertRaises(TrendingInitialImportError):
            service._existing_import_result(
                candidate,
                {"resource_id": 23, "public_id": "public-23"},
            )


class UpdateCandidateInternalImportTests(unittest.TestCase):
    def test_update_candidate_uses_internal_command_without_old_routing_fields(self) -> None:
        captured: dict[str, Any] = {}

        class _Database:
            @staticmethod
            def get_update_candidate(_candidate_id: int) -> dict[str, Any]:
                return {
                    "id": 7,
                    "subscription_id": 3,
                    "title": "Episode 2",
                    "source_url": "https://pan.quark.cn/s/example",
                    "password": "",
                    "raw_data": {
                        "candidate": {
                            "import_payload": {
                                "destination_id": "attacker-target",
                                "target_path": "/attacker",
                                "executor_id": "attacker",
                                "config_revision": 999,
                            }
                        }
                    },
                }

            @staticmethod
            def get_update_subscription(_subscription_id: int, **_kwargs: Any) -> dict[str, Any]:
                return {"id": 3, "title": "Example", "category": "tv"}

            @staticmethod
            def update_update_candidate(*_args: Any, **_kwargs: Any) -> None:
                return None

            @staticmethod
            def add_update_event(*_args: Any, **_kwargs: Any) -> None:
                return None

        class _Imports:
            @staticmethod
            def create_import_job(_payload: dict[str, Any]) -> dict[str, Any]:
                raise AssertionError("internal workflow must not use the public command")

            @staticmethod
            def create_internal_import_job(payload: dict[str, Any]) -> dict[str, Any]:
                captured.update(payload)
                return {
                    "success": True,
                    "job": {"id": 42, "status": "created", "raw_data": {}},
                }

        service = UpdateCandidateImportService(
            database=_Database(),
            import_service=lambda: _Imports(),
            import_handler=lambda: None,
            mark_seen=lambda *_args, **_kwargs: None,
        )

        result = service.import_candidate(7)

        self.assertTrue(result["success"])
        for field in (
            "destination_id",
            "target_path",
            "executor_id",
            "config_revision",
        ):
            self.assertNotIn(field, captured)


if __name__ == "__main__":
    unittest.main()
