from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, Callable, Iterator

from ..accounts.crypto import PURPOSE_SOURCE_RECORD, decrypt, encrypt
from ..domain.models import SelectionSnapshot, SourceDescriptor
from ..time_utils import utc_now_iso


class SourceRecordService:
    """Persist encrypted, immutable source access snapshots used by adapters."""

    def __init__(self, database: Any, master_key_provider: Callable[[], bytes]) -> None:
        self.database = database
        self.master_key_provider = master_key_provider
        self._bound_connection: ContextVar[Any | None] = ContextVar(
            f"source_record_connection_{id(self)}", default=None
        )

    @contextmanager
    def bind_connection(self, connection: Any) -> Iterator[None]:
        """Expose an existing transaction to validator read operations."""
        token = self._bound_connection.set(connection)
        try:
            yield
        finally:
            self._bound_connection.reset(token)

    @contextmanager
    def _read_connection(self, connection: Any | None = None) -> Iterator[Any]:
        current = connection or self._bound_connection.get()
        if current is not None:
            yield current
            return
        with self.database.connect() as owned:
            yield owned

    def capture(
        self,
        source: SourceDescriptor,
        record: dict[str, Any],
        *,
        connection: Any | None = None,
    ) -> SourceDescriptor:
        """Capture a source revision.

        When ``connection`` is supplied the caller owns the transaction.  This
        is important for V1 job creation: the source revision, job, immutable
        plan, execution row and queue task must commit (or roll back) as one
        unit.  The historical two-argument API remains safe for callers that
        need a standalone snapshot and opens its own transaction.
        """
        if connection is not None:
            return self.capture_with_connection(connection, source, record)
        with self.database.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            return self.capture_with_connection(owned, source, record)

    def capture_with_connection(
        self,
        connection: Any,
        source: SourceDescriptor,
        record: dict[str, Any],
    ) -> SourceDescriptor:
        """Capture without opening/committing a nested transaction."""
        if connection is None:
            raise ValueError("connection is required")
        selection = source.selection or SelectionSnapshot.from_payload(source.provider_kind, record)
        if not selection.is_valid:
            raise ValueError("selection snapshot hash is invalid")
        # Keep one canonical selection alongside compatibility aliases used by
        # existing Provider importers. Credentials remain inside the encrypted
        # payload; they are never part of the selection/idempotency hash.
        payload = {
            "provider_kind": source.provider_kind,
            "source_form": source.source_form,
            "search_source_id": source.search_source_id,
            "canonical_source_id": source.canonical_source_id,
            "url": str(record.get("url") or record.get("source_url") or "").strip(),
            "password": str(record.get("password") or ""),
            "selection": selection.to_dict(),
            "selected_items": [*selection.selected_items],
            "ignore_files": list(selection.ignore_files),
            "provider_selection": selection.provider_selection,
            "expected_file_count": max(0, int(source.expected_file_count or 0)),
            "expected_total_bytes": max(0, int(source.expected_total_bytes or 0)),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        record_id = _source_record_id(source.search_source_id, source.canonical_source_id)
        now = utc_now_iso()
        current = connection.execute(
            "SELECT current_revision, search_source_id, provider_kind, source_form, canonical_source_id "
            "FROM source_records WHERE source_record_id=?",
            (record_id,),
        ).fetchone()
        if current is not None:
            # The row identity is immutable.  Never let a reused canonical ID
            # silently combine records from different source catalogs/forms.
            identity_pairs = (
                ("search_source_id", source.search_source_id),
                ("provider_kind", source.provider_kind),
                ("source_form", source.source_form),
                ("canonical_source_id", source.canonical_source_id),
            )
            for field, expected in identity_pairs:
                actual = str(current[field] or "")
                if actual != str(expected or ""):
                    raise ValueError(f"source record identity mismatch: {field}")
            existing = connection.execute(
                "SELECT revision, selected_items_hash, expected_file_count, expected_total_bytes "
                "FROM source_record_revisions "
                "WHERE source_record_id=? AND access_payload_hash=? ORDER BY revision DESC LIMIT 1",
                (record_id, payload_hash),
            ).fetchone()
            if existing is not None:
                return replace(
                    source,
                    selected_items_hash=selection.selection_hash,
                    selection=selection,
                    source_record_id=record_id,
                    source_record_revision=int(existing["revision"]),
                )
            revision = int(current["current_revision"] or 0) + 1
            connection.execute(
                "UPDATE source_records SET current_revision=?, updated_at=? WHERE source_record_id=?",
                (revision, now, record_id),
            )
        else:
            revision = 1
            connection.execute(
                "INSERT INTO source_records(source_record_id, search_source_id, provider_kind, source_form, "
                "canonical_source_id, current_revision, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (record_id, source.search_source_id, source.provider_kind, source.source_form,
                 source.canonical_source_id, revision, now, now),
            )
        ciphertext, key_id = encrypt(
            self.master_key_provider(), encoded, purpose=PURPOSE_SOURCE_RECORD
        )
        connection.execute(
            "INSERT INTO source_record_revisions(source_record_id, revision, access_payload_ciphertext, "
            "credential_key_id, selected_items_json, selected_items_hash, expected_file_count, "
            "expected_total_bytes, created_at, access_payload_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record_id, revision, ciphertext, key_id,
             json.dumps(payload["selected_items"], ensure_ascii=False),
             selection.selection_hash, payload["expected_file_count"],
             payload["expected_total_bytes"], now, payload_hash),
        )
        return replace(
            source,
            selected_items_hash=selection.selection_hash,
            selection=selection,
            source_record_id=record_id,
            source_record_revision=revision,
        )

    def resolve(
        self,
        source_record_id: str,
        revision: int,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        with self._read_connection(connection) as connection:
            row = connection.execute(
                "SELECT r.access_payload_ciphertext, r.credential_key_id, r.access_payload_hash, "
                "r.selected_items_json, r.selected_items_hash, r.expected_file_count, r.expected_total_bytes, "
                "s.provider_kind, s.source_form, s.canonical_source_id, s.search_source_id "
                "FROM source_record_revisions r JOIN source_records s "
                "ON s.source_record_id=r.source_record_id "
                "WHERE r.source_record_id=? AND r.revision=?",
                (source_record_id, int(revision)),
            ).fetchone()
        if row is None:
            raise ValueError(f"来源快照不存在：{source_record_id}@{revision}")
        plaintext = decrypt(
            self.master_key_provider(),
            row["credential_key_id"],
            row["access_payload_ciphertext"],
            purpose=PURPOSE_SOURCE_RECORD,
        )
        value = json.loads(plaintext.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("source snapshot must be an object")
        # Identity and integrity metadata are duplicated in the encrypted
        # payload and relational columns.  Require both copies to agree;
        # silently filling missing values would permit a split-brain snapshot.
        for field in ("provider_kind", "source_form", "canonical_source_id", "search_source_id"):
            stored = str(row[field] or "")
            supplied = str(value.get(field) or "")
            if not stored or not supplied or stored != supplied:
                raise ValueError(f"source snapshot identity mismatch: {field}")
        expected_record_id = _source_record_id(
            str(row["search_source_id"] or ""), str(row["canonical_source_id"] or "")
        )
        if expected_record_id != str(source_record_id):
            raise ValueError("source snapshot record id mismatch")
        for field, column in (("expected_file_count", "expected_file_count"), ("expected_total_bytes", "expected_total_bytes")):
            try:
                supplied = max(0, int(value.get(field) or 0))
                stored = max(0, int(row[column] or 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"source snapshot metadata invalid: {field}") from exc
            if supplied != stored:
                raise ValueError(f"source snapshot metadata mismatch: {field}")
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_hash = str(row["access_payload_hash"] or "")
        if not expected_hash or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != expected_hash:
            raise ValueError("source snapshot payload hash mismatch")
        selection_payload = value.get("selection") if isinstance(value.get("selection"), dict) else value
        selection = SelectionSnapshot.from_dict(
            selection_payload,
            provider_kind=str(value.get("provider_kind") or row["provider_kind"] or ""),
        )
        value["selection"] = selection.to_dict()
        if selection.selection_hash != str(row["selected_items_hash"] or ""):
            raise ValueError("source snapshot selection hash mismatch")
        if not selection.is_valid:
            raise ValueError("source snapshot selection hash is invalid")
        try:
            stored_items = json.loads(str(row["selected_items_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("source snapshot selected_items_json is invalid") from exc
        if stored_items != [*selection.selected_items]:
            raise ValueError("source snapshot selected items mismatch")
        value.setdefault("selected_items", [*selection.selected_items])
        value.setdefault("ignore_files", list(selection.ignore_files))
        value.setdefault("provider_selection", selection.provider_selection)
        return value

    def resolve_selection(
        self,
        source_record_id: str,
        revision: int,
        *,
        connection: Any | None = None,
    ) -> SelectionSnapshot:
        payload = self.resolve(source_record_id, revision, connection=connection)
        value = payload.get("selection") if isinstance(payload.get("selection"), dict) else payload
        return SelectionSnapshot.from_dict(value, provider_kind=str(payload.get("provider_kind") or ""))

    def has_revision(
        self,
        source_record_id: str,
        revision: int,
        *,
        connection: Any | None = None,
    ) -> bool:
        with self._read_connection(connection) as connection:
            row = connection.execute(
                "SELECT 1 FROM source_record_revisions WHERE source_record_id=? AND revision=?",
                (str(source_record_id), int(revision)),
            ).fetchone()
        return row is not None


def _selected_items(record: dict[str, Any]) -> Any:
    if "selected_items" in record:
        return record.get("selected_items")
    raw = record.get("raw_data") if isinstance(record.get("raw_data"), dict) else {}
    if "selected_items" in raw:
        return raw.get("selected_items")
    selection = raw.get("selection") if isinstance(raw.get("selection"), dict) else {}
    return selection.get("selected_items") or []


def _source_record_id(search_source_id: str, canonical_source_id: str) -> str:
    return "src_" + hashlib.sha256(
        f"{str(search_source_id or '')}|{str(canonical_source_id or '')}".encode("utf-8")
    ).hexdigest()[:24]
