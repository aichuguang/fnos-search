from __future__ import annotations

import copy

import pytest

from fnos_media_import.organizer.identity import (
    IDENTITY_MANIFEST_NAME,
    Identity,
    build_identity_manifest,
    parse_identity_manifest,
    serialize_identity_manifest,
)
from fnos_media_import.organizer.identity_pipeline import (
    IdentityReviewRequired,
    execute_manifest_write,
    manifest_operation,
    plan_identity,
    restore_manifest,
    revalidate_identity_plan,
    validate_identity_mappings,
)
from fnos_media_import.organizer.openlist_client import OpenListItem


ROOT = "/移动云/电视剧"
SOURCE = "/移动云/_入库暂存/电视剧/job-1"


class MemoryLibrary:
    def __init__(self) -> None:
        self.entries: list[OpenListItem] = []
        self.manifests: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.fail_verification_once = False
        self._read_failure_pending = False

    def list_dir(self, path: str, *, refresh: bool = False) -> list[OpenListItem]:
        assert refresh is True
        if path == ROOT:
            return self.entries
        return [OpenListItem(IDENTITY_MANIFEST_NAME, target, False) for target in self.manifests if target.rsplit("/", 1)[0] == path]

    def read_identity_manifest(self, path: str) -> str | None:
        if self._read_failure_pending:
            self._read_failure_pending = False
            raise RuntimeError("post-write read timed out")
        return self.manifests.get(path)

    def write_identity_manifest(self, path: str, content: str) -> bool:
        self.writes.append((path, content))
        self.manifests[path] = content
        if self.fail_verification_once:
            self.fail_verification_once = False
            self._read_failure_pending = True
        return True

    def remove_file(self, path: str) -> bool:
        self.removed.append(path)
        self.manifests.pop(path, None)
        return True


def planned(client: MemoryLibrary | None = None) -> tuple[MemoryLibrary, dict, dict, list[dict]]:
    client = client or MemoryLibrary()
    context = plan_identity(
        client, root=ROOT, media_type="tv", title="测试剧", year=2026,
        tmdb_item={"id": 123, "title": "测试剧", "year": 2026, "media_type": "tv"}, request={},
    )
    canonical = context["canonical_dir"]
    target = canonical + "/Season 01/测试剧 (2026) - S01E01.mkv"
    mapping = {
        "status": "ready", "source_path": SOURCE + "/01.mkv", "target_path": target,
        "media_type": "tv", "title": "测试剧", "year": 2026, "season": 1, "episode": 1,
        "raw_data": {"organizer_identity": context, "parsed": {"has_explicit_season": True}, "staging_file": True},
    }
    operations = ([
        {"type": "create_dir", "target_path": canonical, "status": "pending", "raw_data": {"identity_root": True, "allow_create": True}},
    ] if context["is_new"] else []) + [
        manifest_operation(context),
        {"type": "create_dir", "target_path": canonical + "/Season 01", "status": "pending"},
        {"type": "move_file", "source_path": mapping["source_path"], "target_path": target, "status": "pending", "raw_data": {"staging_file": True, "fail_if_target_exists": True}},
    ]
    return client, context, mapping, operations


def seed_context(client: MemoryLibrary, context: dict, *, extra_aliases: tuple = ()) -> None:
    canonical = context["canonical_dir"]
    client.entries.append(OpenListItem(canonical.rsplit("/", 1)[1], canonical, True))
    manifest = parse_identity_manifest(context["manifest"])
    if extra_aliases:
        manifest = build_identity_manifest(manifest.identity, [*manifest.aliases, *extra_aliases])
    client.manifests[canonical + "/" + IDENTITY_MANIFEST_NAME] = serialize_identity_manifest(manifest)


def test_planning_and_prewrite_revalidation_have_no_side_effects() -> None:
    client, context, mapping, operations = planned()
    assert context["is_new"] and not context["errors"]
    assert context["previous_manifest"] is None
    revalidate_identity_plan(client, [mapping], operations, ROOT)
    assert not client.writes and not client.removed


def test_update_missing_identity_is_review_not_first_import() -> None:
    client = MemoryLibrary()
    context = plan_identity(
        client, root=ROOT, media_type="tv", title="测试剧", year=2026,
        tmdb_item={"id": 123, "title": "测试剧", "year": 2026, "media_type": "tv"},
        request={"organizer_intent": "update_existing"},
    )
    assert context["errors"] and not context["is_new"]
    assert not client.writes


def test_revalidate_rejects_traversing_create_directory_operation() -> None:
    client, context, mapping, operations = planned()
    operations.insert(2, {"type": "create_dir", "target_path": context["canonical_dir"] + "/../../unrelated", "status": "pending"})
    with pytest.raises(IdentityReviewRequired):
        revalidate_identity_plan(client, [mapping], operations, ROOT)
    assert not client.writes


def test_revalidate_rejects_unowned_cleanup_directory_operation() -> None:
    client, _, mapping, operations = planned()
    operations.append({"type": "cleanup_empty_dir", "target_path": "/unrelated/important-empty-folder", "status": "pending"})
    with pytest.raises(IdentityReviewRequired):
        revalidate_identity_plan(client, [mapping], operations, ROOT)


def test_validate_rejects_episode_number_prefix_collision() -> None:
    _, _, mapping, _ = planned()
    mapping["target_path"] = mapping["target_path"].replace("S01E01.mkv", "S01E010.mkv")
    with pytest.raises(IdentityReviewRequired):
        validate_identity_mappings([mapping], ROOT)


def test_validate_rejects_backslash_path_escape_before_openlist_normalization() -> None:
    _, context, mapping, _ = planned()
    mapping["target_path"] = context["canonical_dir"] + "/Season 01/x\\..\\..\\..\\escaped - S01E01.mkv"
    with pytest.raises(IdentityReviewRequired):
        validate_identity_mappings([mapping], ROOT)


def test_validate_rejects_subtitle_only_library_writes() -> None:
    _, _, video, _ = planned()
    subtitle = copy.deepcopy(video)
    subtitle["source_path"] = subtitle["source_path"].replace(".mkv", ".ass")
    subtitle["target_path"] = subtitle["target_path"].replace(".mkv", ".ass")
    subtitle["raw_data"]["companion_file"] = True
    video["status"] = "skipped"
    with pytest.raises(IdentityReviewRequired):
        validate_identity_mappings([video, subtitle], ROOT)


def test_validate_accepts_matching_video_and_subtitle() -> None:
    _, _, video, _ = planned()
    subtitle = copy.deepcopy(video)
    subtitle["source_path"] = subtitle["source_path"].replace(".mkv", ".zh.ass")
    subtitle["target_path"] = subtitle["target_path"].replace(".mkv", ".zh.ass")
    subtitle["raw_data"]["companion_file"] = True
    assert validate_identity_mappings([video, subtitle], ROOT)


def test_conflicting_known_external_id_is_not_attached_as_an_alias() -> None:
    client = MemoryLibrary()
    with pytest.raises(IdentityReviewRequired):
        plan_identity(
            client, root=ROOT, media_type="tv", title="测试剧", year=2026,
            tmdb_item={"id": 123, "title": "测试剧", "year": 2026, "media_type": "tv", "external_ids": {"imdb_id": "tt999"}},
            request={"media_identity": {"ns": "imdb", "value": "tt123", "title": "测试剧", "year": 2026, "media_type": "tv"}},
        )
    assert not client.writes


def test_manifest_write_checks_previous_content_and_verifies_result() -> None:
    client, _, _, operations = planned()
    op = operations[1]
    inverse = execute_manifest_write(client, op)
    assert len(client.writes) == 1
    assert client.manifests[op["target_path"]] == op["raw_data"]["content"]
    assert inverse["previous_content"] is None
    assert inverse["written_content"] == op["raw_data"]["content"]


def test_manifest_write_does_not_overwrite_unexpected_concurrent_content() -> None:
    client, _, _, operations = planned()
    op = operations[1]
    other = build_identity_manifest(Identity("tv", "tmdb", "999", "另一部", 2026))
    client.manifests[op["target_path"]] = serialize_identity_manifest(other)
    with pytest.raises(IdentityReviewRequired):
        execute_manifest_write(client, op)
    assert not client.writes


def test_manifest_successful_write_with_failed_read_can_resume_without_rewrite() -> None:
    client, _, _, operations = planned()
    client.fail_verification_once = True
    with pytest.raises(RuntimeError, match="timed out"):
        execute_manifest_write(client, operations[1])
    inverse = execute_manifest_write(client, operations[1])
    assert len(client.writes) == 1
    assert inverse["previous_content"] is None


def test_manifest_rollback_removes_only_unchanged_metadata_from_empty_root() -> None:
    client, _, _, operations = planned()
    inverse = execute_manifest_write(client, operations[1])
    restore_manifest(client, inverse)
    assert client.removed == [operations[1]["target_path"]]
    assert not client.manifests


def test_manifest_rollback_refuses_later_alias_edits() -> None:
    client, _, _, operations = planned()
    inverse = execute_manifest_write(client, operations[1])
    current = parse_identity_manifest(client.manifests[inverse["target_path"]])
    later = build_identity_manifest(current.identity, [*current.aliases, ("tv", "imdb", "tt123")])
    client.manifests[inverse["target_path"]] = serialize_identity_manifest(later)
    with pytest.raises(IdentityReviewRequired, match="后续修改"):
        restore_manifest(client, inverse)
    assert not client.removed


def test_revalidation_rejects_alias_snapshot_changed_after_planning() -> None:
    client, initial, _, _ = planned()
    seed_context(client, initial)
    _, context, mapping, operations = planned(client)
    assert not context["is_new"]
    current = parse_identity_manifest(client.manifests[context["canonical_dir"] + "/" + IDENTITY_MANIFEST_NAME])
    updated = build_identity_manifest(current.identity, [*current.aliases, ("tv", "imdb", "tt123")])
    client.manifests[context["canonical_dir"] + "/" + IDENTITY_MANIFEST_NAME] = serialize_identity_manifest(updated)
    with pytest.raises(IdentityReviewRequired):
        revalidate_identity_plan(client, [mapping], operations, ROOT)
    assert not client.writes


def test_revalidation_rejects_disappeared_existing_root_even_for_create_or_match() -> None:
    client, initial, _, _ = planned()
    seed_context(client, initial)
    _, context, mapping, operations = planned(client)
    assert not context["is_new"]
    client.entries.clear()
    client.manifests.clear()
    with pytest.raises(IdentityReviewRequired):
        revalidate_identity_plan(client, [mapping], operations, ROOT)


def test_revalidation_rejects_same_identity_concurrently_created_at_different_path() -> None:
    client, _, mapping, operations = planned()
    other = Identity("tv", "tmdb", "123", "另一种标题写法", 2026)
    canonical = ROOT + "/另一种标题写法 (2026) [tmdb-123]"
    client.entries.append(OpenListItem(canonical.rsplit("/", 1)[1], canonical, True))
    client.manifests[canonical + "/" + IDENTITY_MANIFEST_NAME] = serialize_identity_manifest(build_identity_manifest(other))
    with pytest.raises(IdentityReviewRequired):
        revalidate_identity_plan(client, [mapping], operations, ROOT)


def test_revalidation_allows_same_new_root_already_created_by_retry() -> None:
    client, context, mapping, operations = planned()
    seed_context(client, context)
    operations[0]["status"] = "done"
    operations[1]["status"] = "done"
    revalidate_identity_plan(client, [mapping], operations, ROOT)
    assert not client.writes


def test_revalidation_rejects_media_move_before_identity_prerequisite() -> None:
    client, _, mapping, operations = planned()
    reordered = [operations[0], operations[2], operations[3], operations[1]]
    with pytest.raises(IdentityReviewRequired):
        revalidate_identity_plan(client, [mapping], reordered, ROOT)


def test_revalidation_rejects_duplicate_manifest_operation() -> None:
    client, _, mapping, operations = planned()
    operations.insert(2, copy.deepcopy(operations[1]))
    with pytest.raises(IdentityReviewRequired):
        revalidate_identity_plan(client, [mapping], operations, ROOT)
