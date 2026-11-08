"""Typed media identities and the small, portable on-disk identity manifest.

Local identities are deliberately weak: their hash is deterministic for the
same title/year/type input, not a permanent identifier for every title alias.
This module never performs network or filesystem writes.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


IDENTITY_MANIFEST_NAME = ".fnos-identity.json"
IDENTITY_MANIFEST_VERSION = 1
MAX_IDENTITY_MANIFEST_BYTES = 65536
IdentityKey = tuple[str, str, str]
IDENTITY_NAMESPACES = frozenset({"tmdb", "imdb", "tvdb", "douban", "local"})
_DIRECTORY_PATTERN = re.compile(
    r"^(?P<title>.+?)(?: \((?P<year>(?:18|19|20|21)\d{2})\))? "
    r"\[(?P<ns>tmdb|imdb|tvdb|douban|local)-(?P<value>[A-Za-z0-9]+)\]$"
)
_UNSAFE_TITLE = re.compile(r'[\x00-\x1f\x7f/\\<>:"|?*]')


class IdentityError(ValueError):
    """Untrusted identity data is invalid or internally inconsistent."""


def normalize_identity_title(value: Any) -> str:
    if not isinstance(value, str):
        raise IdentityError("Identity title must be a string")
    title = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
    if not title or title in {".", ".."} or _UNSAFE_TITLE.search(title) or title.endswith("."):
        raise IdentityError("Identity title is empty or unsafe for a directory name")
    if re.search(r"\[(?:tmdb|imdb|tvdb|douban|local)-", title, re.I):
        raise IdentityError("Identity title must not contain an identity tag")
    return title


def normalize_identity_year(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not re.fullmatch(r"\d{4}", str(value)):
        raise IdentityError("Identity year must be a four-digit integer or null")
    year = int(value)
    if not 1800 <= year <= 2199:
        raise IdentityError("Identity year is outside the supported range")
    return year


def normalize_identity_key(value: Iterable[Any]) -> IdentityKey:
    if isinstance(value, (str, bytes, Mapping)):
        raise IdentityError("Identity key must contain media_type, namespace and value")
    try:
        media_type, ns, identifier = value
    except (TypeError, ValueError) as exc:
        raise IdentityError("Identity key must contain exactly three values") from exc
    media_type = str(media_type).strip().lower()
    ns = str(ns).strip().lower()
    if media_type not in {"tv", "movie"}:
        raise IdentityError("Identity media_type must be tv or movie")
    if ns not in IDENTITY_NAMESPACES:
        raise IdentityError("Unknown identity namespace")
    if isinstance(identifier, bool) or identifier is None:
        raise IdentityError("Invalid identity value")
    identifier = str(identifier).strip()
    if len(identifier) > 64:
        raise IdentityError("Identity value is too long")
    if ns in {"tmdb", "tvdb", "douban"}:
        if not re.fullmatch(r"[0-9]+", identifier) or int(identifier) <= 0:
            raise IdentityError("Numeric identity values must be positive integers")
        identifier = str(int(identifier))
    elif ns == "imdb":
        identifier = identifier.lower()
        if not re.fullmatch(r"tt[0-9]+", identifier) or int(identifier[2:]) <= 0:
            raise IdentityError("Invalid IMDB identity value")
    else:
        identifier = identifier.lower()
        if not re.fullmatch(r"[0-9a-f]{10,64}", identifier):
            raise IdentityError("Local identity must be an unprefixed hexadecimal hash")
    return media_type, ns, identifier


@dataclass(frozen=True)
class Identity:
    media_type: str
    ns: str
    value: str
    title: str
    year: int | None = None

    def __post_init__(self) -> None:
        media_type, ns, value = normalize_identity_key((self.media_type, self.ns, self.value))
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "ns", ns)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "title", normalize_identity_title(self.title))
        object.__setattr__(self, "year", normalize_identity_year(self.year))

    @property
    def key(self) -> IdentityKey:
        return self.media_type, self.ns, self.value

    def to_dict(self) -> dict[str, Any]:
        return {"media_type": self.media_type, "ns": self.ns, "value": self.value, "title": self.title, "year": self.year}


def identity_from_mapping(value: Mapping[str, Any]) -> Identity:
    if not isinstance(value, Mapping):
        raise IdentityError("Identity must be an object")
    if set(value) - {"media_type", "ns", "value", "title", "year"}:
        raise IdentityError("Identity contains unsupported fields")
    return Identity(value.get("media_type"), value.get("ns"), value.get("value"), value.get("title"), value.get("year"))


def make_local_identity(title: str, year: Any = None, media_type: str = "tv") -> Identity:
    title = normalize_identity_title(title)
    year = normalize_identity_year(year)
    media_type = str(media_type).strip().lower()
    if media_type not in {"tv", "movie"}:
        raise IdentityError("Identity media_type must be tv or movie")
    key = f"{media_type}|{title.casefold()}|{year or ''}"
    value = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return Identity(media_type, "local", value, title, year)


def identity_from_tmdb(
    hit: Mapping[str, Any], *, expected_media_type: str, expected_year: Any = None, year_tolerance: int = 1,
) -> Identity:
    """Validate an already accepted TMDB candidate; never search or fall back.

    The caller is responsible for reliable title matching. A rejected candidate
    is not rehabilitated by switching to one of its external ID namespaces.
    """
    if not isinstance(hit, Mapping):
        raise IdentityError("A validated TMDB candidate is required")
    expected_media_type = str(expected_media_type).strip().lower()
    if str(hit.get("media_type") or "").strip().lower() != expected_media_type:
        raise IdentityError("TMDB media type does not match the requested type")
    identity = Identity(expected_media_type, "tmdb", hit.get("id"), hit.get("title"), hit.get("year"))
    expected_year = normalize_identity_year(expected_year)
    if isinstance(year_tolerance, bool) or not isinstance(year_tolerance, int) or year_tolerance < 0:
        raise IdentityError("Year tolerance must be a non-negative integer")
    if expected_year is not None:
        if identity.year is None or abs(identity.year - expected_year) > year_tolerance:
            raise IdentityError("TMDB year does not match the requested year")
    return identity


def format_dir_name(identity: Identity) -> str:
    year = f" ({identity.year})" if identity.year is not None else ""
    return f"{identity.title}{year} [{identity.ns}-{identity.value}]"


def parse_dir_name(name: str, *, media_type: str) -> Identity | None:
    """Recognize only the exact new canonical format, never legacy folders."""
    if not isinstance(name, str):
        return None
    match = _DIRECTORY_PATTERN.fullmatch(name)
    if not match:
        return None
    if match["year"] is None and re.search(r"[(（](?:18|19|20|21)\d{2}[)）]$", match["title"]):
        return None
    try:
        identity = Identity(media_type, match["ns"], match["value"], match["title"], match["year"])
    except IdentityError:
        return None
    return identity if format_dir_name(identity) == name else None


@dataclass(frozen=True)
class IdentityManifest:
    identity: Identity
    aliases: tuple[IdentityKey, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, Identity):
            raise IdentityError("Manifest primary identity is invalid")
        if len(self.aliases) > 128:
            raise IdentityError("Identity manifest contains too many aliases")
        aliases = {normalize_identity_key(key) for key in self.aliases}
        aliases.discard(self.identity.key)
        if any(key[0] != self.identity.media_type for key in aliases):
            raise IdentityError("Manifest alias media type crosses the library scope")
        keys = {self.identity.key, *aliases}
        for ns in IDENTITY_NAMESPACES - {"local"}:
            if len({key[2] for key in keys if key[1] == ns}) > 1:
                raise IdentityError(f"Manifest contains conflicting {ns} identities")
        object.__setattr__(self, "aliases", tuple(sorted(aliases)))

    @property
    def keys(self) -> tuple[IdentityKey, ...]:
        return (self.identity.key, *self.aliases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": IDENTITY_MANIFEST_VERSION,
            "identity": self.identity.to_dict(),
            "aliases": [list(key) for key in self.aliases],
        }


def build_identity_manifest(identity: Identity, aliases: Iterable[IdentityKey] = ()) -> IdentityManifest:
    return IdentityManifest(identity, tuple(aliases))


def serialize_identity_manifest(manifest: IdentityManifest) -> str:
    if not isinstance(manifest, IdentityManifest):
        raise IdentityError("Expected an IdentityManifest")
    text = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_IDENTITY_MANIFEST_BYTES:
        raise IdentityError("Identity manifest exceeds the size limit")
    return text


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IdentityError("Identity manifest contains duplicate JSON fields")
        result[key] = value
    return result


def parse_identity_manifest(value: str | bytes | Mapping[str, Any]) -> IdentityManifest:
    if isinstance(value, bytes):
        if len(value) > MAX_IDENTITY_MANIFEST_BYTES:
            raise IdentityError("Identity manifest exceeds the size limit")
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IdentityError("Identity manifest is not valid UTF-8") from exc
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_IDENTITY_MANIFEST_BYTES:
            raise IdentityError("Identity manifest exceeds the size limit")
        try:
            value = json.loads(value, object_pairs_hook=_unique_json_object)
        except (ValueError, TypeError) as exc:
            raise IdentityError(f"Invalid identity manifest JSON: {exc}") from exc
    if not isinstance(value, Mapping) or set(value) != {"version", "identity", "aliases"}:
        raise IdentityError("Identity manifest fields are invalid")
    if type(value["version"]) is not int or value["version"] != IDENTITY_MANIFEST_VERSION:
        raise IdentityError("Unsupported identity manifest version")
    if not isinstance(value["aliases"], list):
        raise IdentityError("Identity manifest aliases must be a list")
    if len(value["aliases"]) > 128:
        raise IdentityError("Identity manifest contains too many aliases")
    return build_identity_manifest(identity_from_mapping(value["identity"]), value["aliases"])
