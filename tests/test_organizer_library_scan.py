from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from fnos_media_import.organizer.identity import (
    IDENTITY_MANIFEST_NAME,
    Identity,
    build_identity_manifest,
    format_dir_name,
    make_local_identity,
    serialize_identity_manifest,
)
from fnos_media_import.organizer.library_scan import (
    DuplicateIdentity,
    LibraryScanError,
    resolve_target_dir,
    scan_library,
)
from fnos_media_import.organizer.openlist_client import OpenListItem


ROOT = "/移动云/电视剧"


def item(identity: Identity, root: str = ROOT) -> OpenListItem:
    name = format_dir_name(identity)
    return OpenListItem(name, root + "/" + name, True)


def client(*entries: OpenListItem) -> Mock:
    return Mock(list_dir=Mock(return_value=list(entries)))


def test_scan_refreshes_exact_scope_and_resolver_is_read_only() -> None:
    identity = Identity("tv", "tmdb", "123", "测试剧", 2026)
    openlist = client(item(identity))
    index = scan_library(openlist, ROOT, media_type="tv")
    decision = resolve_target_dir(identity, index, is_first_import=False)
    assert decision.success and not decision.is_new
    assert decision.canonical_dir == item(identity).path
    openlist.list_dir.assert_called_once_with(ROOT, refresh=True)
    openlist.mkdir.assert_not_called()
    openlist.rename.assert_not_called()


def test_first_import_decides_creation_but_update_never_creates_missing_work() -> None:
    identity = Identity("tv", "tmdb", "123", "测试剧", 2026)
    openlist = client()
    index = scan_library(openlist, ROOT, media_type="tv")
    first = resolve_target_dir(identity, index, is_first_import=True)
    assert first.success and first.is_new
    assert first.canonical_dir == item(identity).path
    update = resolve_target_dir(identity, index, is_first_import=False)
    assert update.errors and not update.canonical_dir and not update.is_new
    openlist.mkdir.assert_not_called()


def test_same_identity_directory_rename_refreshes_metadata_without_creating_root() -> None:
    original = Identity("tv", "tmdb", "123", "Original", 2026)
    renamed = replace(original, title="Renamed")
    previous = build_identity_manifest(original)
    index = scan_library(
        client(item(renamed)), ROOT, media_type="tv",
        read_manifest=lambda _path: serialize_identity_manifest(previous),
    )
    decision = resolve_target_dir(original, index, is_first_import=False)
    assert decision.success and not decision.is_new
    assert decision.canonical_dir == item(renamed).path
    assert decision.manifest.identity == renamed
    assert decision.previous_manifest == previous


def test_same_numeric_movie_and_tv_id_cannot_cross_scope() -> None:
    tv = Identity("tv", "tmdb", "123", "测试剧", 2026)
    movie = Identity("movie", "tmdb", "123", "电影", 2026)
    index = scan_library(client(item(tv)), ROOT, media_type="tv")
    assert resolve_target_dir(movie, index, is_first_import=True).errors


def test_duplicate_primary_id_is_rejected_without_selecting_first_directory() -> None:
    first = Identity("tv", "tmdb", "123", "测试剧", 2026)
    second = Identity("tv", "tmdb", "123", "另一标题", 2026)
    with pytest.raises(DuplicateIdentity) as raised:
        scan_library(client(item(first), item(second)), ROOT, media_type="tv")
    assert set(raised.value.paths) == {item(first).path, item(second).path}


def test_manifest_aliases_survive_task_database_loss_and_local_upgrade() -> None:
    local = make_local_identity("测试剧", 2026)
    tmdb = Identity("tv", "tmdb", "123", "测试剧", 2026)
    initial = scan_library(client(item(local)), ROOT, media_type="tv")
    upgrade = resolve_target_dir(tmdb, initial, is_first_import=False, aliases=[local.key])
    assert upgrade.success and not upgrade.is_new
    assert upgrade.canonical_dir == item(local).path
    assert upgrade.manifest.identity == local
    assert upgrade.previous_manifest is None
    assert tmdb.key in upgrade.manifest.keys
    persisted = serialize_identity_manifest(upgrade.manifest)
    read = Mock(return_value=persisted)
    rebuilt = scan_library(client(item(local)), ROOT, media_type="tv", read_manifest=read)
    read.assert_called_once_with(item(local).path + "/" + IDENTITY_MANIFEST_NAME)
    resumed = resolve_target_dir(tmdb, rebuilt, is_first_import=False)
    assert resumed.canonical_dir == item(local).path
    assert resumed.previous_manifest == upgrade.manifest


def test_duplicate_alias_across_manifests_is_rejected() -> None:
    first = Identity("tv", "tmdb", "123", "测试剧", 2026)
    second = Identity("tv", "imdb", "tt123", "另一标题", 2026)
    payloads = {
        item(first).path + "/" + IDENTITY_MANIFEST_NAME: serialize_identity_manifest(build_identity_manifest(first, [second.key])),
        item(second).path + "/" + IDENTITY_MANIFEST_NAME: serialize_identity_manifest(build_identity_manifest(second)),
    }
    with pytest.raises(DuplicateIdentity):
        scan_library(client(item(first), item(second)), ROOT, media_type="tv", read_manifest=payloads.get)


def test_multiple_requested_aliases_resolving_to_different_roots_are_rejected() -> None:
    first = Identity("tv", "tmdb", "123", "测试剧", 2026)
    second = Identity("tv", "imdb", "tt123", "另一标题", 2026)
    index = scan_library(client(item(first), item(second)), ROOT, media_type="tv")
    decision = resolve_target_dir(first, index, is_first_import=False, aliases=[second.key])
    assert decision.errors and "multiple" in decision.errors[0]


def test_weak_local_alias_does_not_authorize_conflicting_title() -> None:
    local = make_local_identity("测试剧", 2026)
    index = scan_library(client(item(local)), ROOT, media_type="tv")
    different = Identity("tv", "tmdb", "123", "不是同一部", 2026)
    decision = resolve_target_dir(different, index, is_first_import=False, aliases=[local.key])
    assert decision.errors and "Weak local" in decision.errors[0]


@pytest.mark.parametrize("failure", [RuntimeError("network unavailable"), PermissionError("access denied")])
def test_listing_failure_is_not_an_empty_library(failure: Exception) -> None:
    openlist = client()
    openlist.list_dir.side_effect = failure
    with pytest.raises(LibraryScanError):
        scan_library(openlist, ROOT, media_type="tv")


def test_manifest_read_failure_and_bad_json_are_not_treated_as_absent() -> None:
    identity = Identity("tv", "tmdb", "123", "测试剧", 2026)
    with pytest.raises(LibraryScanError, match="Cannot read"):
        scan_library(client(item(identity)), ROOT, media_type="tv", read_manifest=Mock(side_effect=RuntimeError("timeout")))
    with pytest.raises(LibraryScanError, match="Invalid identity manifest"):
        scan_library(client(item(identity)), ROOT, media_type="tv", read_manifest=lambda _: "not json")


def test_manifest_primary_must_match_directory_and_media_scope() -> None:
    identity = Identity("tv", "tmdb", "123", "测试剧", 2026)
    mismatched = Identity("movie", "tmdb", "123", "测试剧", 2026)
    payload = serialize_identity_manifest(build_identity_manifest(mismatched))
    with pytest.raises(LibraryScanError, match="does not match"):
        scan_library(client(item(identity)), ROOT, media_type="tv", read_manifest=lambda _: payload)


def test_legacy_is_not_resolved_but_malformed_tagged_directory_fails_closed() -> None:
    old = OpenListItem("旧目录", ROOT + "/旧目录", True)
    assert not scan_library(client(old), ROOT, media_type="tv").entries
    malformed = OpenListItem("测试剧 [tmdb-0]", ROOT + "/测试剧 [tmdb-0]", True)
    with pytest.raises(LibraryScanError, match="Invalid canonical"):
        scan_library(client(malformed), ROOT, media_type="tv")


def test_scan_rejects_out_of_scope_entry_and_global_or_traversing_roots() -> None:
    identity = Identity("tv", "tmdb", "123", "测试剧", 2026)
    with pytest.raises(LibraryScanError, match="outside"):
        scan_library(client(item(identity, "/other")), ROOT, media_type="tv")
    for root in ("/", "/移动云/../other", "relative", "https://example.com/media"):
        with pytest.raises(LibraryScanError):
            scan_library(client(), root, media_type="tv")


def test_existing_non_directory_target_is_not_overwritten() -> None:
    identity = Identity("tv", "tmdb", "123", "测试剧", 2026)
    occupied = replace(item(identity), is_dir=False)
    index = scan_library(client(occupied), ROOT, media_type="tv")
    decision = resolve_target_dir(identity, index, is_first_import=True)
    assert decision.errors and "occupied" in decision.errors[0]
