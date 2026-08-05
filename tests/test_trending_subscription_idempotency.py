from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from fnos_media_import.database import Database
from fnos_media_import.services.update_subscription_command_service import UpdateSubscriptionCommandService
from fnos_media_import.services.update_subscription_normalizer import UpdateSubscriptionNormalizer


def _subscription_data() -> dict[str, object]:
    return {
        "title": "仙逆",
        "category": "anime",
        "category_label": "动漫",
        "media_type": "tv",
        "season": 1,
        "year": "2023",
        "tmdb_id": 100,
        "schedule_kind": "tmdb",
        "timezone": "Asia/Shanghai",
        "status": "enabled",
    }


class TrendingSubscriptionIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._temp_dir.name) / "app.db")
        self.db.init_schema()
        self.candidate_id = self.db.upsert_trending_candidate(
            item={
                "canonical_key": "test:100",
                "source": "test",
                "source_id": "100",
                "title": "仙逆",
                "year": "2023",
                "media_type": "anime",
                "status": "task_exists",
            }
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_existing_matching_subscription_is_bound_without_duplicate(self) -> None:
        existing_id = self.db.create_update_subscription(_subscription_data(), [])

        subscription_id, created = self.db.update.get_or_create_trending_subscription(
            self.candidate_id,
            _subscription_data(),
            [],
        )

        self.assertFalse(created)
        self.assertEqual(subscription_id, existing_id)
        self.assertEqual(self.db.count_update_subscriptions(), 1)
        self.assertEqual(self.db.get_trending_candidate(self.candidate_id)["subscription_id"], existing_id)

    def test_task_exists_candidate_can_bind_existing_subscription_without_tmdb_lookup(self) -> None:
        existing_id = self.db.create_update_subscription(_subscription_data(), [])
        bound = self.db.update.bind_trending_candidate_subscription(self.candidate_id, existing_id)
        self.assertTrue(bound)
        self.assertEqual(self.db.get_trending_candidate(self.candidate_id)["subscription_id"], existing_id)

    def test_archived_subscription_is_not_bound_or_reused(self) -> None:
        archived_data = {**_subscription_data(), "status": "archived"}
        archived_id = self.db.create_update_subscription(archived_data, [])
        self.db.update_trending_candidate(self.candidate_id, subscription_id=archived_id)

        subscription_id, created = self.db.update.get_or_create_trending_subscription(
            self.candidate_id,
            _subscription_data(),
            [],
        )

        self.assertTrue(created)
        self.assertNotEqual(subscription_id, archived_id)
        self.assertEqual(self.db.count_update_subscriptions(), 2)
        self.assertEqual(self.db.get_trending_candidate(self.candidate_id)["subscription_id"], subscription_id)

    def test_regular_identity_distinguishes_unknown_season_from_specials(self) -> None:
        unknown_id, unknown_created = self.db.create_update_subscription_with_outcome(
            {**_subscription_data(), "season": None},
            [],
        )
        specials_id, specials_created = self.db.create_update_subscription_with_outcome(
            {**_subscription_data(), "season": 0},
            [],
        )

        self.assertTrue(unknown_created)
        self.assertTrue(specials_created)
        self.assertNotEqual(unknown_id, specials_id)
        self.assertEqual(self.db.count_update_subscriptions(), 2)

    def test_subscription_normalizer_preserves_specials_season_zero(self) -> None:
        normalizer = UpdateSubscriptionNormalizer(
            categories=lambda: {"anime": {"label": "动漫"}},
            tmdb_schedule_hint=lambda _tmdb_id, _media_type, _payload: {},
            tmdb_basic_hint=lambda _tmdb_id, _media_type: {"title": "特别篇", "year": "2024"},
            path_health=lambda _category_key, _category, _raw_data: {"success": True},
            normalize_source=lambda source: dict(source),
        )

        data, _sources = normalizer.normalize({"tmdb_id": 100, "category": "anime", "season": 0})

        self.assertEqual(data["season"], 0)

    def test_trending_identity_does_not_reuse_unknown_season_for_specials(self) -> None:
        unknown_id = self.db.create_update_subscription({**_subscription_data(), "season": None}, [])
        self.db.update_trending_candidate(self.candidate_id, subscription_id=unknown_id)

        specials_id, created = self.db.update.get_or_create_trending_subscription(
            self.candidate_id,
            {**_subscription_data(), "season": 0},
            [],
        )

        self.assertTrue(created)
        self.assertNotEqual(specials_id, unknown_id)
        self.assertEqual(self.db.get_update_subscription(specials_id)["season"], 0)
        self.assertEqual(self.db.get_trending_candidate(self.candidate_id)["subscription_id"], specials_id)

    def test_seen_episode_identity_distinguishes_unknown_season_from_specials(self) -> None:
        subscription_id = self.db.create_update_subscription(_subscription_data(), [])
        self.db.upsert_update_seen_item(
            {
                "subscription_id": subscription_id,
                "fingerprint": "specials-episode-1",
                "season": 0,
                "episode": 1,
            }
        )
        self.db.upsert_update_seen_item(
            {
                "subscription_id": subscription_id,
                "fingerprint": "unknown-season-episode-2",
                "season": None,
                "episode": 2,
            }
        )

        self.assertTrue(self.db.update_seen_episode_exists(subscription_id, 0, 1))
        self.assertFalse(self.db.update_seen_episode_exists(subscription_id, None, 1))
        self.assertTrue(self.db.update_seen_episode_exists(subscription_id, None, 2))
        self.assertFalse(self.db.update_seen_episode_exists(subscription_id, 0, 2))

    def test_mismatched_bound_subscription_is_revalidated_by_tmdb_identity(self) -> None:
        wrong_id = self.db.create_update_subscription({**_subscription_data(), "tmdb_id": 101}, [])
        self.db.update_trending_candidate(self.candidate_id, subscription_id=wrong_id)

        subscription_id, created = self.db.update.get_or_create_trending_subscription(
            self.candidate_id,
            _subscription_data(),
            [],
        )

        self.assertTrue(created)
        self.assertNotEqual(subscription_id, wrong_id)
        self.assertEqual(self.db.get_update_subscription(subscription_id)["tmdb_id"], 100)
        self.assertEqual(self.db.get_trending_candidate(self.candidate_id)["subscription_id"], subscription_id)

    def test_concurrent_requests_create_only_one_subscription(self) -> None:
        barrier = threading.Barrier(2)
        results: list[tuple[int, bool]] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(
                    self.db.update.get_or_create_trending_subscription(
                        self.candidate_id,
                        _subscription_data(),
                        [],
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual({subscription_id for subscription_id, _created in results}, {results[0][0]})
        self.assertEqual(sorted(created for _subscription_id, created in results), [False, True])
        self.assertEqual(self.db.count_update_subscriptions(), 1)
        self.assertEqual(self.db.get_trending_candidate(self.candidate_id)["subscription_id"], results[0][0])

    def test_regular_and_trending_create_share_one_atomic_identity(self) -> None:
        barrier = threading.Barrier(2)
        results: list[int] = []
        errors: list[BaseException] = []

        def regular_create() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(self.db.create_update_subscription(_subscription_data(), []))
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def trending_create() -> None:
            try:
                barrier.wait(timeout=5)
                subscription_id, _created = self.db.update.get_or_create_trending_subscription(
                    self.candidate_id,
                    _subscription_data(),
                    [],
                )
                results.append(subscription_id)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=regular_create), threading.Thread(target=trending_create)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.db.count_update_subscriptions(), 1)
        self.assertEqual(self.db.get_trending_candidate(self.candidate_id)["subscription_id"], results[0])

    def test_regular_create_reuses_identity_without_events_refresh_or_source_overwrite(self) -> None:
        refresh_calls: list[int] = []

        def normalize(payload: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
            data = dict(payload)
            sources = list(data.pop("sources", []))
            return data, sources

        def refresh_context(subscription_id: int) -> dict[str, object]:
            refresh_calls.append(subscription_id)
            return self.db.get_update_subscription(subscription_id, include_sources=True) or {}

        service = UpdateSubscriptionCommandService(
            database=self.db,
            normalize=normalize,
            compute_next_run=lambda _data: "2026-08-02T04:00:00+00:00",
            refresh_context=refresh_context,
            get_subscription=lambda subscription_id: self.db.get_update_subscription(
                subscription_id,
                include_sources=True,
            ),
        )
        original_source = {
            "type": "search",
            "name": "原始来源",
            "url": "https://example.test/original",
            "priority": 10,
        }
        first = service.create({**_subscription_data(), "sources": [original_source]})

        self.assertTrue(first["_created"])
        self.assertEqual(refresh_calls, [first["id"]])
        first_events = self.db.list_update_events(subscription_id=first["id"])
        self.assertEqual(len(first_events), 1)
        self.assertEqual(first_events[0]["message"], "创建定时追更订阅")

        duplicate = service.create(
            {
                **_subscription_data(),
                "title": "不应覆盖的标题",
                "sources": [
                    {
                        "type": "search",
                        "name": "新来源",
                        "url": "https://example.test/replacement",
                        "priority": 1,
                    }
                ],
            }
        )

        self.assertFalse(duplicate["_created"])
        self.assertIn("未覆盖原配置", duplicate["message"])
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(refresh_calls, [first["id"]])
        self.assertEqual(len(self.db.list_update_events(subscription_id=first["id"])), 1)
        self.assertEqual(self.db.count_update_subscriptions(), 1)
        stored = self.db.get_update_subscription(first["id"], include_sources=True)
        self.assertEqual(stored["title"], "仙逆")
        self.assertEqual(len(stored["sources"]), 1)
        self.assertEqual(stored["sources"][0]["name"], "原始来源")
        self.assertEqual(stored["sources"][0]["url"], "https://example.test/original")

    def test_year_aware_candidate_key_migrates_legacy_row_in_place(self) -> None:
        legacy_id = self.db.upsert_trending_candidate(
            item={
                "canonical_key": "同名作品|tv",
                "source": "legacy",
                "source_id": "legacy-unknown",
                "title": "同名作品",
                "year": None,
                "media_type": "tv",
                "status": "ignored",
            }
        )

        migrated_id = self.db.upsert_trending_candidate(
            item={
                "canonical_key": "同名作品|tv|2024",
                "legacy_canonical_key": "同名作品|tv",
                "source": "current",
                "source_id": "current-2024",
                "title": "同名作品",
                "year": 2024,
                "media_type": "tv",
                "status": "discovered",
            }
        )

        self.assertEqual(migrated_id, legacy_id)
        self.assertEqual(self.db.count_trending_candidates(), 2)  # includes self.candidate_id from setUp
        migrated = self.db.get_trending_candidate(migrated_id)
        self.assertEqual(migrated["canonical_key"], "同名作品|tv|2024")
        self.assertEqual(migrated["status"], "ignored")

        repeated_id = self.db.upsert_trending_candidate(
            item={
                "canonical_key": "同名作品|tv|2024",
                "legacy_canonical_key": "同名作品|tv",
                "source": "current",
                "source_id": "current-2024",
                "title": "同名作品",
                "year": 2024,
                "media_type": "tv",
            }
        )
        self.assertEqual(repeated_id, legacy_id)
        self.assertEqual(self.db.count_trending_candidates(), 2)

        unknown_year_id = self.db.upsert_trending_candidate(
            item={
                "canonical_key": "同名作品|tv",
                "legacy_canonical_key": "同名作品|tv",
                "source": "current-no-year",
                "source_id": "current-unknown",
                "title": "同名作品",
                "year": None,
                "media_type": "tv",
            }
        )
        self.assertEqual(unknown_year_id, legacy_id)
        self.assertEqual(self.db.get_trending_candidate(legacy_id)["year"], 2024)
        self.assertEqual(self.db.count_trending_candidates(), 2)

    def test_unknown_year_does_not_choose_between_two_known_remakes(self) -> None:
        first_remake_id = self.db.upsert_trending_candidate(
            item={
                "canonical_key": "翻拍作品|tv|2023",
                "legacy_canonical_key": "翻拍作品|tv",
                "source": "current",
                "source_id": "current-2023",
                "title": "翻拍作品",
                "year": 2023,
                "media_type": "tv",
            }
        )
        remake_id = self.db.upsert_trending_candidate(
            item={
                "canonical_key": "翻拍作品|tv|2024",
                "legacy_canonical_key": "翻拍作品|tv",
                "source": "current",
                "source_id": "current-2024",
                "title": "翻拍作品",
                "year": 2024,
                "media_type": "tv",
            }
        )
        unknown_id = self.db.upsert_trending_candidate(
            item={
                "canonical_key": "翻拍作品|tv",
                "legacy_canonical_key": "翻拍作品|tv",
                "source": "unknown",
                "source_id": "unknown-year",
                "title": "翻拍作品",
                "year": None,
                "media_type": "tv",
            }
        )
        repeated_remake_id = self.db.upsert_trending_candidate(
            item={
                "canonical_key": "翻拍作品|tv|2024",
                "legacy_canonical_key": "翻拍作品|tv",
                "source": "current-repeat",
                "source_id": "current-2024-repeat",
                "title": "翻拍作品",
                "year": 2024,
                "media_type": "tv",
            }
        )

        self.assertEqual(repeated_remake_id, remake_id)
        self.assertEqual(len({first_remake_id, remake_id, unknown_id}), 3)
        self.assertEqual(self.db.get_trending_candidate(first_remake_id)["year"], 2023)
        self.assertEqual(self.db.get_trending_candidate(remake_id)["year"], 2024)
        self.assertIsNone(self.db.get_trending_candidate(unknown_id)["year"])


if __name__ == "__main__":
    unittest.main()
