from __future__ import annotations

from fnos_media_import.importers.cloud139 import _normalize_selected_files, _normalize_selected_folders


def test_episode_files_with_same_parent_path_are_kept() -> None:
    rows = _normalize_selected_files(
        [
            {"fid": "episode-01", "name": "01.mkv", "path": "show/season-01"},
            {"fid": "episode-02", "name": "02.mkv", "path": "show/season-01"},
        ]
    )
    assert [row["fid"] for row in rows] == ["episode-01", "episode-02"]


def test_folders_with_same_parent_path_are_kept_by_fid() -> None:
    rows = _normalize_selected_folders(
        [
            {"fid": "season-01", "name": "Season 1", "path": "show"},
            {"fid": "season-02", "name": "Season 2", "path": "show"},
        ]
    )
    assert [row["fid"] for row in rows] == ["season-01", "season-02"]
