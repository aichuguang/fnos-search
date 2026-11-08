from __future__ import annotations

from unittest.mock import MagicMock, Mock
from urllib.parse import unquote

import pytest
import requests

from fnos_media_import.organizer.openlist_client import OpenListClient, OpenListError, OpenListItem


PATH = "/library/Example/.fnos-identity.json"


def client_with_manifest(*, size: int | None = 2, is_dir: bool = False) -> OpenListClient:
    client = OpenListClient({"base_url": "http://openlist.invalid", "token": "private-jwt"})
    client.list_dir = Mock(return_value=[OpenListItem(".fnos-identity.json", PATH, is_dir, size)])
    client._request = Mock(return_value={"code": 200, "data": {"raw_url": "https://provider.invalid/signed-file", "is_dir": False}})
    return client


def download_mock(monkeypatch: pytest.MonkeyPatch, chunks: list[bytes], *, status: int = 200, length: str | None = None) -> MagicMock:
    session = MagicMock(spec=requests.Session)
    session.__enter__.return_value = session
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = status
    response.headers = {"Content-Length": length} if length is not None else {}
    response.iter_content.return_value = iter(chunks)
    session.get.return_value = response
    monkeypatch.setattr("fnos_media_import.organizer.openlist_client.requests.Session", Mock(return_value=session))
    return session


def test_missing_manifest_requires_successful_refreshed_parent_listing() -> None:
    client = client_with_manifest()
    client.list_dir.return_value = []
    assert client.read_identity_manifest(PATH) is None
    client.list_dir.assert_called_once_with("/library/Example", refresh=True)
    client._request.assert_not_called()


def test_parent_listing_error_is_not_missing_manifest() -> None:
    client = client_with_manifest()
    client.list_dir.side_effect = OpenListError("permission denied")
    with pytest.raises(OpenListError, match="permission denied"):
        client.read_identity_manifest(PATH)


@pytest.mark.parametrize("path", ["/library/video.mp4", "/library/../.fnos-identity.json", "https://host/.fnos-identity.json"])
def test_manifest_io_rejects_other_files_and_traversal(path: str) -> None:
    client = client_with_manifest()
    with pytest.raises(OpenListError):
        client.read_identity_manifest(path)
    with pytest.raises(OpenListError):
        client.write_identity_manifest(path, "{}")
    client.list_dir.assert_not_called()
    client._request.assert_not_called()


@pytest.mark.parametrize(("size", "is_dir"), [(65537, False), (2, True)])
def test_manifest_metadata_prevents_non_file_or_large_download(size: int, is_dir: bool) -> None:
    client = client_with_manifest(size=size, is_dir=is_dir)
    with pytest.raises(OpenListError):
        client.read_identity_manifest(PATH)
    client._request.assert_not_called()


def test_manifest_download_uses_isolated_session_without_openlist_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_with_manifest()
    content = '{"title":"藏海传"}'
    session = download_mock(monkeypatch, [content.encode("utf-8")])
    assert client.read_identity_manifest(PATH) == content
    assert session is not client.session
    assert session.trust_env is False
    args, kwargs = session.get.call_args
    assert args == ("https://provider.invalid/signed-file",)
    assert "headers" not in kwargs and "auth" not in kwargs and "cookies" not in kwargs
    assert kwargs["stream"] is True


@pytest.mark.parametrize("chunks", [[b"a" * 65537], [b"\xff"]], ids=["oversized", "invalid-utf8"])
def test_invalid_or_oversized_download_is_not_missing(monkeypatch: pytest.MonkeyPatch, chunks: list[bytes]) -> None:
    client = client_with_manifest(size=None)
    download_mock(monkeypatch, chunks)
    with pytest.raises(OpenListError):
        client.read_identity_manifest(PATH)


def test_reported_download_size_is_checked_before_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_with_manifest(size=None)
    session = download_mock(monkeypatch, [b"{}"], length="65537")
    with pytest.raises(OpenListError):
        client.read_identity_manifest(PATH)
    session.get.return_value.iter_content.assert_not_called()


def test_download_permission_failure_is_not_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_with_manifest()
    download_mock(monkeypatch, [], status=403)
    with pytest.raises(OpenListError, match="403"):
        client.read_identity_manifest(PATH)


def test_download_network_failure_is_not_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_with_manifest()
    session = download_mock(monkeypatch, [])
    session.get.side_effect = requests.exceptions.Timeout("timed out")
    with pytest.raises(OpenListError):
        client.read_identity_manifest(PATH)


def test_signed_local_download_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_with_manifest()
    client._request.return_value = {"code": 200, "data": {"sign": "signed:token", "is_dir": False}}
    session = download_mock(monkeypatch, [b"{}"])
    assert client.read_identity_manifest(PATH) == "{}"
    assert session.get.call_args.args[0] == "http://openlist.invalid/d/library/Example/.fnos-identity.json?sign=signed%3Atoken"


def test_write_manifest_is_bounded_sync_utf8_put() -> None:
    client = client_with_manifest()
    client._request.return_value = {"code": 200, "data": None}
    path = "/library/藏海传/.fnos-identity.json"
    content = '{"title":"藏海传"}'
    assert client.write_identity_manifest(path, content)
    args, kwargs = client._request.call_args
    assert args == ("PUT", "/api/fs/put")
    assert kwargs["data"] == content.encode("utf-8")
    assert unquote(kwargs["headers"]["File-Path"]) == path
    assert kwargs["headers"]["As-Task"] == "false"
    assert kwargs["allow_redirects"] is False
    assert kwargs["retryable"] is False


@pytest.mark.parametrize("content", ["a" * 65537, "\ud800"], ids=["oversized", "invalid-utf8"])
def test_invalid_manifest_write_never_calls_network(content: str) -> None:
    client = client_with_manifest()
    with pytest.raises(OpenListError):
        client.write_identity_manifest(PATH, content)
    client._request.assert_not_called()


def test_write_requires_explicit_success_response() -> None:
    client = client_with_manifest()
    client._request.return_value = {}
    with pytest.raises(OpenListError):
        client.write_identity_manifest(PATH, "{}")
