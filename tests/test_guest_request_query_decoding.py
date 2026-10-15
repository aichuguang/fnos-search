from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fnos_media_import.database import Database
from fnos_media_import.repositories import RepositorySet
from fnos_media_import.repositories.guest_request_query_repository import GuestRequestQueryRepository


class GuestRequestQueryDecodingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="guest-query-decoding-")
        self.addCleanup(directory.cleanup)
        database = Database(Path(directory.name) / "requests.db")
        database.init_schema()
        self.db = RepositorySet.from_database(database)

    def _create(self, token: str, raw_data=None, **extra) -> int:
        return self.db.guest_request_commands.create({
            "request_token": token, "title": "Selected episode", "category": "tv",
            "source_type": "quark", "source_url": "source-record:selected-episode",
            "status": "pending_review", "raw_data": raw_data, **extra,
        })

    def test_request_accessors_decode_selection_and_destination_without_changing_order(self) -> None:
        job_id, _created = self.db.job_commands.create({
            "title": "Selected episode", "category": "tv", "category_label": "TV", "status": "submitted",
            "source_provider_kind": "quark", "source_form": "share", "source_url": "source-record:selected-episode",
            "destination_id": "native-main", "plan_version": 1, "idempotency_key": "guest-query-decoding",
        })
        first_raw = {"request": {"destination_id": "target-a", "quark_file_ids": ["file-1"],
                                  "ignore_files": ["advert.mp4"]}, "selection": {"season": 1, "episode": 2}}
        second_raw = {"request": {"destination_id": "target-b", "quark_file_ids": ["file-2"]}}
        first = self._create("first", first_raw, job_id=job_id)
        second = self._create("second", second_raw, job_id=job_id)
        self._create("failed", {"request": {"destination_id": "target-a"}}, status="failed")
        queries = self.db.guest_request_queries

        self.assertEqual(queries.get(first)["raw_data"], first_raw)
        self.assertEqual(queries.get_by_token("first")["raw_data"], first_raw)
        by_job = queries.list_by_job(job_id)
        self.assertEqual([item["id"] for item in by_job], [first, second])
        self.assertEqual([item["raw_data"] for item in by_job], [first_raw, second_raw])
        newest = queries.list(limit=1, status="pending_review")
        older = queries.list(limit=1, status="pending_review", offset=1)
        self.assertEqual(newest[0]["raw_data"], second_raw)
        self.assertEqual(older[0]["raw_data"], first_raw)
        self.assertEqual(queries.count("pending_review"), 2)
        self.assertEqual(queries.count(), 3)
        for destination, expected in (("target-a", first), ("target-b", second)):
            with self.subTest(destination=destination):
                found = queries.find_recent_by_url(
                    source_url="source-record:selected-episode", category="tv", destination_id=destination,
                )
                self.assertEqual(found["id"], expected)
                self.assertIsInstance(found["raw_data"], dict)
        self.assertIsNone(queries.find_recent_by_url(
            source_url="source-record:selected-episode", category="tv", destination_id="missing-target",
        ))

    def test_empty_malformed_and_already_decoded_raw_data_keep_compatibility(self) -> None:
        request_id = self._create("legacy")
        queries = self.db.guest_request_queries
        for stored, expected in ((None, None), ("", ""), ("{broken", "{broken"), ("{}", {}), ("[]", [])):
            with self.subTest(stored=stored):
                with self.db.connect() as connection:
                    connection.execute("UPDATE guest_requests SET raw_data=? WHERE id=?", (stored, request_id))
                self.assertEqual(queries.get(request_id)["raw_data"], expected)
                self.assertEqual(queries.list()[0]["raw_data"], expected)
        decoded = {"request": {"ignore_files": ["ad.mkv"]}}
        custom_queries = GuestRequestQueryRepository(
            self.db.connect,
            lambda row: {**dict(row), "raw_data": decoded} if row is not None else None,
        )
        self.assertEqual(custom_queries.get(request_id)["raw_data"], decoded)
        self.assertIsNone(queries.get(999999))
        self.assertIsNone(queries.get_by_token("not-found"))

    def test_event_json_and_missing_request_lists_keep_existing_contract(self) -> None:
        request_id = self._create("events", {"request": {"quark_file_ids": ["file-1"]}})
        event_data = {"worker_task_id": 7, "selection": ["file-1"]}
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO guest_request_events (request_id, level, message, raw_data, created_at) VALUES (?, ?, ?, ?, ?)",
                (request_id, "info", "queued", json.dumps(event_data), "2026-10-11T00:00:00+00:00"),
            )
        queries = self.db.guest_request_queries
        self.assertEqual(queries.list_events(request_id)[0]["raw_data"], event_data)
        grouped = queries.list_events_for_requests([request_id, request_id, 999999])
        self.assertEqual(len(grouped[request_id]), 1)
        self.assertEqual(grouped[request_id][0]["raw_data"], event_data)
        self.assertEqual(grouped[999999], [])
        self.assertEqual(queries.list_events_for_requests([]), {})
        self.assertEqual(queries.list_by_job(999999), [])


if __name__ == "__main__":
    unittest.main()
