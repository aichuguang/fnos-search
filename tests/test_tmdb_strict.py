from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from fnos_media_import.organizer.tmdb import TmdbClient, TmdbLookupError, tmdb_match_rejection_reason


def tmdb_client(data: object = None, *, status: int = 200) -> TmdbClient:
    client = TmdbClient({"token": "test-token", "use_env_proxy": False})
    response = Mock(status_code=status)
    response.json.return_value = data
    client.session.get = Mock(return_value=response)
    return client


def test_empty_strict_search_is_a_valid_non_match() -> None:
    assert tmdb_client({"results": []}).search("Example", "tv", strict=True) == []


@pytest.mark.parametrize("status", [401, 404, 429, 500])
def test_strict_http_failure_is_distinct_from_empty_search(status: int) -> None:
    client = tmdb_client({}, status=status)
    assert client.search("Example", "tv") == []
    with pytest.raises(TmdbLookupError, match=str(status)):
        client.search("Example", "tv", strict=True)


@pytest.mark.parametrize("data", [{}, [], {"success": False}, {"results": None}])
def test_strict_search_rejects_malformed_result(data: object) -> None:
    with pytest.raises(TmdbLookupError):
        tmdb_client(data).search("Example", "tv", strict=True)


def test_strict_network_failure_does_not_become_a_local_identity() -> None:
    client = tmdb_client()
    client.session.get.side_effect = requests.exceptions.Timeout("timeout")
    assert client.search("Example", "tv") == []
    with pytest.raises(TmdbLookupError):
        client.search("Example", "tv", strict=True)


def test_strict_invalid_json_raises() -> None:
    client = tmdb_client()
    client.session.get.return_value.json.side_effect = ValueError("invalid json")
    with pytest.raises(TmdbLookupError):
        client.search("Example", "tv", strict=True)


def test_strict_requires_configured_token() -> None:
    client = TmdbClient({})
    with pytest.raises(TmdbLookupError):
        client.search("Example", "tv", strict=True)


def test_details_preserve_original_title_for_alias_validation() -> None:
    client = tmdb_client({"id": 42, "name": "示例", "original_name": "Example", "first_air_date": "2025-01-01", "external_ids": {"imdb_id": "tt123", "tvdb_id": 456}})
    result = client.details(42, "tv", strict=True)
    assert result["original_title"] == "Example"
    assert result["year"] == "2025"
    assert result["external_ids"] == {"imdb_id": "tt123", "tvdb_id": 456}
    assert client.session.get.call_args.kwargs["params"]["append_to_response"] == "external_ids"


def test_search_only_exposes_external_ids_supplied_by_the_same_result() -> None:
    rows = [
        {"id": 42, "name": "Example", "external_ids": {"imdb_id": "tt123"}},
        {"id": 43, "name": "Another"},
    ]
    result = tmdb_client({"results": rows}).search("Example", "tv", strict=True)
    assert result[0]["external_ids"] == {"imdb_id": "tt123"}
    assert result[1]["external_ids"] == {}


def test_strict_details_failure_is_not_none() -> None:
    with pytest.raises(TmdbLookupError):
        tmdb_client({}, status=503).details(42, "tv", strict=True)


def test_year_gap_greater_than_one_is_rejected() -> None:
    result = {"media_type": "tv", "title": "Example", "year": "2026"}
    assert tmdb_match_rejection_reason("Example", "2024", "tv", result)
    assert tmdb_match_rejection_reason("Example", "2025", "tv", result) == ""
