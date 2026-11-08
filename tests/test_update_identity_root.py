from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from fnos_media_import.importers.cloud139 import _VIDEO_EXTENSIONS, _is_video_name
from fnos_media_import.organizer.identity import Identity, build_identity_manifest, format_dir_name, make_local_identity, serialize_identity_manifest
from fnos_media_import.organizer.openlist_client import OpenListItem, VIDEO_EXTENSIONS
from fnos_media_import.services.update_service import UpdateService


ROOT = "/OpenList/Main/TV"


def root_service(identities=(), *, aliases=()) -> tuple[UpdateService, Mock]:
    client = Mock(configured=True)
    entries = [OpenListItem(format_dir_name(identity), f"{ROOT}/{format_dir_name(identity)}", True) for identity in identities]
    client.list_dir.return_value = entries
    manifests = {
        f"{entry.path}/.fnos-identity.json": serialize_identity_manifest(build_identity_manifest(identity, aliases))
        for entry, identity in zip(entries, identities)
    }
    client.read_identity_manifest.side_effect = lambda path: manifests.get(path)
    client.scan_videos.return_value = []
    service = object.__new__(UpdateService)
    service.db = SimpleNamespace(update=Mock())
    service.openlist_session_resolver = lambda *_args: {"client": client, "category_root": ROOT, "destination_id": "main"}
    service._seen_episodes = Mock(return_value=set())
    service._scheduler_config = Mock(return_value={})
    service._record_openlist_seen_item = Mock()
    return service, client


def bound_subscription(**overrides) -> dict:
    return {
        "category": "tv", "media_type": "tv", "title": "Old Title", "year": "2026", "season": 1,
        "identity_ns": "tmdb", "identity_value": "123", "canonical_dir": f"{ROOT}/Old Title (2026) [tmdb-123]",
        "raw_data": {}, **overrides,
    }


def test_bound_subscription_follows_renamed_directory_by_identity_and_refreshes_cache() -> None:
    identity = Identity("tv", "tmdb", "123", "New Title", 2026)
    service, client = root_service([identity])
    subscription = bound_subscription(id=7)
    context = service._resolve_update_root_context(subscription)
    assert context["canonical_openlist_root"] == f"{ROOT}/{format_dir_name(identity)}"
    assert context["canonical_title"] == "New Title"
    assert not context["needs_manual_selection"] and not context["auto_create_pending"]
    assert subscription["canonical_dir"] == context["canonical_openlist_root"]
    client.list_dir.assert_called_once_with(ROOT, refresh=True)
    assert service.db.update.update_update_subscription.call_args.args[1]["canonical_dir"] == subscription["canonical_dir"]
    client.mkdir.assert_not_called()
    client.write_identity_manifest.assert_not_called()


def test_bound_missing_identity_never_falls_back_to_title_or_new_directory() -> None:
    service, client = root_service()
    subscription = bound_subscription()
    context = service._resolve_update_root_context(subscription)
    assert context["canonical_openlist_root"] == ""
    assert context["expected_openlist_root"] == ""
    assert context["needs_manual_selection"] and not context["auto_create_pending"]
    assert service.scan_existing_episodes(subscription, force_refresh=True) == set()
    client.scan_videos.assert_not_called()
    client.mkdir.assert_not_called()


def test_first_unbound_subscription_gets_tagged_expected_root_without_writing() -> None:
    service, client = root_service()
    subscription = {"category": "tv", "title": "First Show", "year": "2026", "raw_data": {}}
    identity = make_local_identity("First Show", 2026, "tv")
    context = service._resolve_update_root_context(subscription)
    assert context["canonical_openlist_root"] == f"{ROOT}/{format_dir_name(identity)}"
    assert context["auto_create_pending"] and not context["needs_manual_selection"]
    assert subscription["raw_data"]["media_identity"]["value"] == identity.value
    assert "canonical_dir" not in subscription
    assert service._resolve_update_root_context(subscription)["auto_create_pending"]
    assert service.scan_existing_episodes(subscription, force_refresh=True) == set()
    client.mkdir.assert_not_called()
    client.write_identity_manifest.assert_not_called()
    client.scan_videos.assert_not_called()


def test_manifest_alias_can_resolve_bound_imdb_subscription() -> None:
    identity = Identity("tv", "tmdb", "123", "New Title", 2026)
    service, _client = root_service([identity], aliases=[("tv", "imdb", "tt456")])
    subscription = bound_subscription(identity_ns="imdb", identity_value="tt456")
    context = service._resolve_update_root_context(subscription)
    assert context["canonical_openlist_root"] == f"{ROOT}/{format_dir_name(identity)}"
    assert not context["needs_manual_selection"]


def test_duplicate_identity_or_failed_scan_requires_review() -> None:
    service, client = root_service([Identity("tv", "tmdb", "123", "One", 2026), Identity("tv", "tmdb", "123", "Two", 2026)])
    assert service._resolve_update_root_context(bound_subscription())["needs_manual_selection"]
    client.list_dir.side_effect = RuntimeError("permission denied")
    context = service._resolve_update_root_context(bound_subscription())
    assert context["needs_manual_selection"]
    assert context["canonical_openlist_root"] == ""
    client.mkdir.assert_not_called()


def test_legacy_untagged_binding_is_not_guessed() -> None:
    service, client = root_service()
    subscription = {"category": "tv", "title": "Old Title", "year": "2026", "raw_data": {"canonical_openlist_root": f"{ROOT}/Old Title"}}
    context = service._resolve_update_root_context(subscription)
    assert context["needs_manual_selection"]
    assert context["canonical_openlist_root"] == ""
    client.list_dir.assert_not_called()


def test_movie_identity_cannot_search_a_tv_scope() -> None:
    service, client = root_service()
    assert service._resolve_update_root_context(bound_subscription(media_type="movie"))["needs_manual_selection"]
    client.list_dir.assert_not_called()


def test_full_baseline_uses_identity_directory_title_after_rename() -> None:
    identity = Identity("tv", "tmdb", "123", "New Title", 2026)
    service, client = root_service([identity])
    subscription = bound_subscription()
    path = f"{ROOT}/{format_dir_name(identity)}/Season 01/New Title - S01E02.mp4"
    client.scan_videos.return_value = [OpenListItem("New Title - S01E02.mp4", path, False, 123)]
    context = service._resolve_update_root_context(subscription)
    assert service.scan_existing_episodes(subscription, force_refresh=True) == {(1, 2)}
    client.scan_videos.assert_called_once_with(context["canonical_openlist_root"], max_depth=8, max_files=1200)


def test_cloud139_classification_uses_the_shared_video_whitelist() -> None:
    assert _VIDEO_EXTENSIONS is VIDEO_EXTENSIONS
    assert _is_video_name("episode.OGV") and _is_video_name("episode.divx")
    assert not _is_video_name("episode.iso") and not _is_video_name("episode.tar")
