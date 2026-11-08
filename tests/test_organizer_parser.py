from __future__ import annotations

import pytest

from fnos_media_import.organizer.openlist_client import VIDEO_EXTENSIONS
from fnos_media_import.organizer.parser import (
    invalid_title,
    is_container_dir,
    parse_dir_name,
    parse_file_name,
    parse_season,
    sanitize_candidate,
)


def test_parse_season_after_chinese_title() -> None:
    assert parse_season("博斯S06.Bosch.2020.1080p") == 6
    assert parse_season("博斯S01.Bosch.2014.1080p") == 1
    assert parse_season("双峰S02") == 2


def test_parse_season_keeps_standard_separator_forms() -> None:
    assert parse_season("Bosch.S06.2020.1080p") == 6
    assert parse_season("Bosch Season 6") == 6
    assert parse_season("[S06]") == 6


def test_parse_season_does_not_match_inside_an_english_word() -> None:
    assert parse_season("BluRayS06") is None


@pytest.mark.parametrize(
    ("name", "season", "episode"),
    [
        ("52-4K.mp4", None, 52),
        ("53-4K.mp4", None, 53),
        ("12-4K.高码率.mp4", None, 12),
        ("12-1080P.mkv", None, 12),
        ("04-超清.avi", None, 4),
        ("5-4K.mp4", None, 5),
        ("2-05.4K.mkv", 2, 5),
        ("S02E05.mkv", 2, 5),
        ("S00E01.mkv", 0, 1),
        ("第2季 第5集.rmvb", 2, 5),
        ("Season 02 EP05.mp4", 2, 5),
        ("05.mp4", None, 5),
        ("第5集.mp4", None, 5),
        ("2026.mp4", None, None),
        ("x264-10bit.mp4", None, None),
        ("1080P.mp4", None, None),
    ],
)
def test_episode_quality_and_explicit_seasons(name: str, season: int | None, episode: int | None) -> None:
    parsed = parse_file_name(name)
    assert (parsed.season, parsed.episode) == (season, episode)
    assert not parsed.review_reasons


def test_numeric_directory_is_not_a_season_hint() -> None:
    assert parse_season("52") is None
    parsed = parse_file_name("04-超清.mp4", current_dir="04", parent_dir="52")
    assert (parsed.season, parsed.episode) == (None, 4)
    assert not parsed.has_explicit_season
    assert parsed.title == ""


def test_explicit_season_zero_survives_directory_hint() -> None:
    parsed = parse_file_name("EP05.mp4", current_dir="Season 00")
    assert (parsed.season, parsed.episode) == (0, 5)
    assert parsed.has_explicit_season


def test_filename_season_wins_but_conflicting_directory_requires_review() -> None:
    parsed = parse_file_name("S02E05.mkv", current_dir="Season 01", parent_dir="Example")
    assert (parsed.season, parsed.episode) == (2, 5)
    assert parsed.review_reasons
    assert parsed.has_explicit_season


def test_nearest_directory_season_wins_but_parent_conflict_is_visible() -> None:
    parsed = parse_file_name("EP05.mp4", current_dir="Season 02", parent_dir="Example Season 01")
    assert (parsed.season, parsed.episode) == (2, 5)
    assert parsed.review_reasons


@pytest.mark.parametrize("value", ["mobile share f62d7b770123", "mobile_share_f62d7b770123", "job 110", "job_110", "52", "新建文件夹 (2)", "_入库暂存", "d34db33fd34db33f"])
def test_container_names_are_not_title_candidates(value: str) -> None:
    assert is_container_dir(value)
    assert invalid_title(value)
    assert sanitize_candidate(value) == ""
    assert parse_dir_name(value)["container"] is True


def test_videos_inside_container_are_still_parsed() -> None:
    parsed = parse_file_name("S01E05.mp4", current_dir="job 110", parent_dir="藏海传 (2025)")
    assert (parsed.title, parsed.season, parsed.episode) == ("藏海传", 1, 5)


@pytest.mark.parametrize("value", ["更新至", "更新至第52集", "持续更新", "全40集", "完结"])
def test_status_only_titles_are_rejected(value: str) -> None:
    assert invalid_title(value)
    assert sanitize_candidate(value) == ""


def test_status_words_are_removed_without_losing_title() -> None:
    assert sanitize_candidate("藏海传 更新至第52集") == "藏海传"
    assert sanitize_candidate("藏海传 持续更新") == "藏海传"


def test_identity_tag_is_not_selected_as_an_english_title() -> None:
    assert sanitize_candidate("A Very Long English Series Title (2026) [tmdb-123]") == "A Very Long English Series Title"


def test_parse_standard_directory_name() -> None:
    assert parse_dir_name("伟大的长征 (2026) [tmdb-297253]") == {
        "container": False,
        "title": "伟大的长征",
        "year": 2026,
        "ns": "tmdb",
        "id": "297253",
    }
    assert parse_dir_name("伟大的长征 (2026)")["ns"] is None
    assert parse_dir_name("Unknown Title [local-9f3a1c7d20]")["year"] is None
    assert parse_dir_name("Example [tmdb-123] suffix")["ns"] is None


def test_video_extensions_do_not_include_archives_or_companions() -> None:
    assert VIDEO_EXTENSIONS == {
        ".mp4", ".mkv", ".avi", ".wmv", ".flv", ".mov", ".m4v", ".ts", ".m2ts", ".mts",
        ".webm", ".rmvb", ".rm", ".mpg", ".mpeg", ".3gp", ".ogv", ".divx", ".vob",
    }
    assert not VIDEO_EXTENSIONS.intersection({".iso", ".tar", ".zip", ".rar", ".7z", ".nfo", ".srt", ".ass"})
