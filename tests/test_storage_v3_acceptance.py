"""Cross-module acceptance contracts for frozen storage destinations."""

from __future__ import annotations

import unittest
from typing import Any

from fnos_media_import.services.trending_initial_import_service import TrendingInitialImportService
from fnos_media_import.services.update_candidate_import_service import UpdateCandidateImportService


class TrendingDestinationFreezeTests(unittest.TestCase):
    def test_default_destination_is_frozen_into_initial_import_identity(self) -> None:
        service = TrendingInitialImportService(
            repository=object(),
            search_service=lambda: None,
            import_service=lambda: None,
            get_resource=lambda _resource_id: None,
            get_job=lambda _job_id: None,
            categories=lambda: {"movie": {"label": "Movie"}},
            resolve_destination=lambda requested: "mobile_main" if not requested else requested,
        )
        payload = service._import_payload(  # noqa: SLF001 - contract test for durable payload construction
            candidate={"id": 11, "title": "Film"},
            resource={"id": 22, "url": "https://example.test/file"},
            category="movie",
            request_payload={},
        )
        self.assertEqual(payload["destination_id"], "mobile_main")
        other_service = TrendingInitialImportService(
            repository=object(),
            search_service=lambda: None,
            import_service=lambda: None,
            get_resource=lambda _resource_id: None,
            get_job=lambda _job_id: None,
            categories=lambda: {"movie": {"label": "Movie"}},
            resolve_destination=lambda _requested: "quark_archive",
        )
        other = other_service._import_payload(  # noqa: SLF001
            candidate={"id": 11, "title": "Film"},
            resource={"id": 22, "url": "https://example.test/file"},
            category="movie",
            request_payload={},
        )
        self.assertNotEqual(payload["idempotency_key"], other["idempotency_key"])

    def test_explicit_unavailable_destination_fails_closed(self) -> None:
        service = TrendingInitialImportService(
            repository=object(),
            search_service=lambda: None,
            import_service=lambda: None,
            get_resource=lambda _resource_id: None,
            get_job=lambda _job_id: None,
            categories=lambda: {"movie": {"label": "Movie"}},
            resolve_destination=lambda _requested: None,
        )
        with self.assertRaisesRegex(ValueError, "存储目标"):
            service._import_payload(  # noqa: SLF001
                candidate={"id": 11, "title": "Film"},
                resource={"id": 22, "url": "https://example.test/file"},
                category="movie",
                request_payload={"destination_id": "disabled_target"},
            )


class UpdateCandidateDestinationFreezeTests(unittest.TestCase):
    def test_candidate_payload_cannot_override_subscription_destination(self) -> None:
        captured: list[dict[str, Any]] = []

        class Database:
            def get_update_candidate(self, _candidate_id: int) -> dict[str, Any]:
                return {
                    "id": 9,
                    "subscription_id": 3,
                    "title": "Episode",
                    "source_url": "https://example.test/episode",
                    "password": "",
                    "raw_data": {
                        "candidate": {
                            "import_payload": {"destination_id": "attacker_target"},
                        }
                    },
                }

            def get_update_subscription(self, _subscription_id: int, *, include_sources: bool) -> dict[str, Any]:
                return {
                    "id": 3,
                    "title": "Series",
                    "category": "movie",
                    "raw_data": {"destination_id": "mobile_main"},
                }

            def update_update_candidate(self, *_args: Any, **_kwargs: Any) -> None:
                return None

            def add_update_event(self, *_args: Any, **_kwargs: Any) -> None:
                return None

        class ImportService:
            def create_import_job(self, payload: dict[str, Any]) -> dict[str, Any]:
                captured.append(payload)
                return {"success": True, "job": {"id": 101, "status": "submitted", "raw_data": {}}}

        service = UpdateCandidateImportService(
            database=Database(),
            import_service=lambda: ImportService(),
            import_handler=lambda: None,
            mark_seen=lambda *_args, **_kwargs: None,
        )
        result = service.import_candidate(9)
        self.assertTrue(result["success"])
        self.assertEqual(captured[0]["destination_id"], "mobile_main")


if __name__ == "__main__":
    unittest.main()
