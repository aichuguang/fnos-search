from __future__ import annotations

import json

import pytest

from fnos_media_import.organizer.identity import (
    Identity,
    IdentityError,
    build_identity_manifest,
    format_dir_name,
    identity_from_mapping,
    identity_from_tmdb,
    make_local_identity,
    parse_dir_name,
    parse_identity_manifest,
    serialize_identity_manifest,
)


def test_identity_normalizes_values_and_keeps_media_type_in_key() -> None:
    tv = Identity(" TV ", "TMDB", 123, "  测试   剧  ", "2026")
    movie = Identity("movie", "tmdb", "00123", "测试电影", 2026)
    assert tv.key == ("tv", "tmdb", "123")
    assert movie.key == ("movie", "tmdb", "123")
    assert tv.key != movie.key
    assert tv.title == "测试 剧"
    assert identity_from_mapping(tv.to_dict()) == tv


@pytest.mark.parametrize("ns,value", [("tmdb", "0"), ("tmdb", True), ("tmdb", "-1"), ("imdb", "123"), ("local", "local-1234567890"), ("local", "../1234567890"), ("unknown", "123")])
def test_identity_rejects_invalid_identifier(ns: str, value: object) -> None:
    with pytest.raises(IdentityError):
        Identity("tv", ns, value, "测试剧", 2026)


@pytest.mark.parametrize("title", ["", "..", "../测试", "测试/第一季", "测试\\第一季", "测试\x00", "测试 [tmdb-12]"])
def test_identity_rejects_unsafe_title(title: str) -> None:
    with pytest.raises(IdentityError):
        Identity("tv", "tmdb", "123", title, 2026)


def test_standard_directory_round_trip_and_unknown_year() -> None:
    identity = Identity("tv", "tmdb", "297253", "伟大的长征", 2026)
    assert format_dir_name(identity) == "伟大的长征 (2026) [tmdb-297253]"
    assert parse_dir_name(format_dir_name(identity), media_type="tv") == identity
    unknown = make_local_identity("没有年份的剧")
    assert "(" not in format_dir_name(unknown)
    assert parse_dir_name(format_dir_name(unknown), media_type="tv") == unknown


@pytest.mark.parametrize("name", ["伟大的长征", "伟大的长征 (2026)", "伟大的长征(2026) [tmdb-297253]", "伟大的长征 （2026） [tmdb-297253]", "伟大的长征 (2026) [tmdb-297253] extra", "伟大的长征 (2026) [TMDB-297253]", "伟大的长征 (2026) [tmdb-0297253]", "伟大的长征 (2026) [tmdb-0]"])
def test_directory_parser_does_not_compatibly_accept_dirty_formats(name: str) -> None:
    assert parse_dir_name(name, media_type="tv") is None


def test_local_hash_is_stable_only_for_identical_normalized_input() -> None:
    first = make_local_identity("  Test   Show ", 2026, "tv")
    assert first.key == make_local_identity("test show", "2026", "tv").key
    assert first.key != make_local_identity("test show", None, "tv").key
    assert first.key != make_local_identity("test show", 2026, "movie").key
    assert first.key != make_local_identity("an alias", 2026, "tv").key


def test_tmdb_year_and_type_mismatch_are_rejected_not_external_id_fallback() -> None:
    hit = {"id": 123, "title": "大话西游", "media_type": "movie", "year": "1995", "external_ids": {"imdb_id": "tt123"}}
    with pytest.raises(IdentityError, match="year"):
        identity_from_tmdb(hit, expected_media_type="movie", expected_year=2016)
    with pytest.raises(IdentityError, match="type"):
        identity_from_tmdb(hit, expected_media_type="tv", expected_year=1995)
    identity = identity_from_tmdb(hit, expected_media_type="movie", expected_year=1995)
    assert identity.key == ("movie", "tmdb", "123")
    with pytest.raises(IdentityError, match="year"):
        identity_from_tmdb({**hit, "year": None}, expected_media_type="movie", expected_year=1995)


def test_manifest_round_trips_utf8_and_has_only_normalized_typed_aliases() -> None:
    primary = make_local_identity("伟大的长征", 2026)
    manifest = build_identity_manifest(primary, [("tv", "TMDB", 297253), ("tv", "imdb", "TT0012345"), primary.key])
    serialized = serialize_identity_manifest(manifest)
    assert "伟大的长征" in serialized
    assert not serialized.encode("utf-8").startswith(b"\xef\xbb\xbf")
    assert parse_identity_manifest(serialized.encode("utf-8")) == manifest
    assert parse_identity_manifest(manifest.to_dict()) == manifest
    assert json.loads(serialized)["aliases"] == [["tv", "imdb", "tt0012345"], ["tv", "tmdb", "297253"]]


def test_manifest_rejects_wrong_scope_conflicting_strong_ids_and_schema() -> None:
    identity = Identity("tv", "tmdb", "123", "测试剧", 2026)
    with pytest.raises(IdentityError, match="scope"):
        build_identity_manifest(identity, [("movie", "imdb", "tt123")])
    with pytest.raises(IdentityError, match="conflicting"):
        build_identity_manifest(identity, [("tv", "tmdb", "456")])
    payload = build_identity_manifest(identity).to_dict()
    with pytest.raises(IdentityError, match="fields"):
        parse_identity_manifest({**payload, "canonical_dir": "/some/other/library"})
    with pytest.raises(IdentityError, match="version"):
        parse_identity_manifest({**payload, "version": True})
    with pytest.raises(IdentityError, match="duplicate"):
        parse_identity_manifest('{"version":1,"version":2,"identity":{},"aliases":[]}')
    with pytest.raises(IdentityError, match="size"):
        parse_identity_manifest(b" " * 65537)
    with pytest.raises(IdentityError, match="UTF-8"):
        parse_identity_manifest(b"\xff")
