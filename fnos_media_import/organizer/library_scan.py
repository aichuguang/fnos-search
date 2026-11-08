"""Read-only, library-scoped identity lookup and target decisions.

The apply layer must hold its write lease/lock and rescan immediately before
executing a decision. A scan is evidence, never a reservation or write lock.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .identity import (
    IDENTITY_MANIFEST_NAME,
    Identity,
    IdentityError,
    IdentityKey,
    IdentityManifest,
    build_identity_manifest,
    format_dir_name,
    normalize_identity_title,
    parse_dir_name,
    parse_identity_manifest,
)


class LibraryScanError(RuntimeError):
    """A complete and trustworthy library snapshot could not be produced."""


class DuplicateIdentity(LibraryScanError):
    def __init__(self, key: IdentityKey, paths: Iterable[str]) -> None:
        self.key = key
        self.paths = tuple(sorted(set(paths)))
        super().__init__(f"Duplicate identity {key}: {', '.join(self.paths)}")


@dataclass(frozen=True)
class LibraryEntry:
    canonical_dir: str
    identity: Identity
    manifest: IdentityManifest
    has_manifest: bool = False


@dataclass(frozen=True)
class LibraryIndex:
    root: str
    media_type: str
    entries: tuple[LibraryEntry, ...] = ()
    id_map: Mapping[IdentityKey, tuple[str, ...]] = field(default_factory=dict)
    occupied_paths: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TargetDecision:
    canonical_dir: str = ""
    is_new: bool = False
    errors: tuple[str, ...] = ()
    manifest: IdentityManifest | None = None
    previous_manifest: IdentityManifest | None = None

    @property
    def success(self) -> bool:
        return bool(self.canonical_dir) and not self.errors


def _library_root(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value or "://" in value:
        raise LibraryScanError("Library root must be an absolute OpenList virtual path")
    if any(part in {".", ".."} for part in value.split("/")) or re.search(r"[\x00-\x1f\x7f]", value):
        raise LibraryScanError("Library root contains unsafe path components")
    root = posixpath.normpath(value)
    if root == "/":
        raise LibraryScanError("The OpenList global root is not a media library scope")
    return root


def scan_library(
    openlist: Any,
    root: str,
    *,
    media_type: str,
    read_manifest: Callable[[str], str | bytes | Mapping[str, Any] | None] | None = None,
) -> LibraryIndex:
    """Scan one category root with refresh, rejecting corrupt/duplicate IDs.

    ``read_manifest`` receives the exact manifest file path. It may return None
    only for a confirmed missing file; permission, transport and parsing errors
    must raise. No caller may turn an unsuccessful scan into an empty index.
    """
    root = _library_root(root)
    if media_type not in {"tv", "movie"}:
        raise LibraryScanError("Library media type must be tv or movie")
    try:
        rows = openlist.list_dir(root, refresh=True)
    except Exception as exc:
        raise LibraryScanError(f"Cannot refresh library {root}: {exc}") from exc
    if not isinstance(rows, (list, tuple)):
        raise LibraryScanError("Library listing did not return a complete item list")
    entries: list[LibraryEntry] = []
    occupied: set[str] = set()
    id_map: dict[IdentityKey, tuple[str, ...]] = {}
    for row in rows:
        name = getattr(row, "name", None)
        path = getattr(row, "path", None)
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise LibraryScanError("Library listing contains an unsafe entry name")
        expected_path = root + "/" + name
        if path != expected_path:
            raise LibraryScanError("Library listing contains an entry outside its exact scope")
        if path in occupied:
            raise LibraryScanError(f"Library listing contains a duplicate entry: {path}")
        occupied.add(path)
        if not getattr(row, "is_dir", False):
            continue
        identity = parse_dir_name(name, media_type=media_type)
        if identity is None:
            if re.search(r"\[(?:tmdb|imdb|tvdb|douban|local)-", name, re.I):
                raise LibraryScanError(f"Invalid canonical identity directory: {path}")
            continue
        payload = None
        if read_manifest is not None:
            try:
                payload = read_manifest(path + "/" + IDENTITY_MANIFEST_NAME)
            except Exception as exc:
                raise LibraryScanError(f"Cannot read identity manifest in {path}: {exc}") from exc
        try:
            manifest = parse_identity_manifest(payload) if payload is not None else build_identity_manifest(identity)
        except (IdentityError, TypeError, ValueError) as exc:
            raise LibraryScanError(f"Invalid identity manifest in {path}: {exc}") from exc
        if manifest.identity.key != identity.key:
            raise LibraryScanError(f"Manifest primary identity does not match its directory: {path}")
        for key in manifest.keys:
            paths = (*id_map.get(key, ()), path)
            if len(set(paths)) > 1:
                raise DuplicateIdentity(key, paths)
            id_map[key] = paths
        entries.append(LibraryEntry(path, identity, manifest, payload is not None))
    return LibraryIndex(root, media_type, tuple(entries), id_map, frozenset(occupied))


def resolve_target_dir(
    identity: Identity,
    index: LibraryIndex,
    *,
    is_first_import: bool,
    aliases: Iterable[IdentityKey] = (),
) -> TargetDecision:
    """Return a pure decision. Never mkdir, rename, or silently accept a split."""
    if not isinstance(identity, Identity) or not isinstance(index, LibraryIndex):
        return TargetDecision(errors=("A validated identity and library snapshot are required",))
    if identity.media_type != index.media_type:
        return TargetDecision(errors=("Identity media type crosses the library scope",))
    try:
        _library_root(index.root)
        incoming = build_identity_manifest(identity, aliases)
    except (IdentityError, LibraryScanError, TypeError) as exc:
        return TargetDecision(errors=(str(exc),))
    paths: set[str] = set()
    matched_keys: set[IdentityKey] = set()
    for key in incoming.keys:
        matches = index.id_map.get(key, ())
        if matches:
            matched_keys.add(key)
            paths.update(matches)
    if len(paths) > 1:
        return TargetDecision(errors=(f"Identity aliases resolve to multiple directories: {', '.join(sorted(paths))}",))
    if paths:
        path = next(iter(paths))
        entry = next((entry for entry in index.entries if entry.canonical_dir == path), None)
        if entry is None or posixpath.dirname(path) != index.root:
            return TargetDecision(errors=("Identity index contains an invalid or out-of-scope directory",))
        if all(key[1] == "local" for key in matched_keys):
            titles_agree = normalize_identity_title(identity.title).casefold() == entry.identity.title.casefold()
            years_agree = identity.year is None or entry.identity.year is None or identity.year == entry.identity.year
            if not titles_agree or not years_agree:
                return TargetDecision(errors=("Weak local identity evidence conflicts with the existing title or year",))
        try:
            # Keep the physical directory's primary key. A stronger newly found
            # identity becomes a durable alias, not an unjournaled directory rename.
            manifest = build_identity_manifest(entry.identity, (*entry.manifest.keys, *incoming.keys))
        except IdentityError as exc:
            return TargetDecision(errors=(str(exc),))
        return TargetDecision(path, False, (), manifest, entry.manifest if entry.has_manifest else None)
    if not is_first_import:
        return TargetDecision(errors=("Update identity has no existing library directory; creation is forbidden",))
    path = index.root + "/" + format_dir_name(identity)
    if path in index.occupied_paths:
        return TargetDecision(errors=("The canonical target path is occupied but does not have the requested identity",))
    return TargetDecision(path, True, (), incoming)
