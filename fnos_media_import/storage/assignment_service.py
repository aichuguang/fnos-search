from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any, Callable

from ..config import CATEGORY_KEYS
from ..domain.models import SourceDestinationAssignment
from ..providers.manifests import BUILTIN_MANIFESTS
from ..time_utils import utc_now_iso
from .target_service import TargetNotFoundError, TargetValidationError


SEARCH_SOURCES: dict[str, dict[str, str]] = {}
for _provider_kind, _manifest in BUILTIN_MANIFESTS.items():
    for _raw_source in _manifest.search_sources:
        _source_id = str(_raw_source.get("search_source_id") or "").strip()
        if not _source_id:
            continue
        SEARCH_SOURCES[_source_id] = {
            "label": str(_raw_source.get("label") or _manifest.display_name),
            "provider_kind": _provider_kind,
            "source_form": str(_raw_source.get("source_form") or "share"),
            "icon_text": str(_raw_source.get("icon_text") or _manifest.icon_text),
        }


class AssignmentError(ValueError):
    code = "ASSIGNMENT_INVALID"


class AssignmentConflictError(AssignmentError):
    """当前 assignment 已被其他管理员修改，调用方必须重新读取。"""

    code = "ASSIGNMENT_CONFLICT"


def assignment_id(search_source_id: str, category_key: str) -> str:
    """Return the stable identity of a source/category route."""

    source = str(search_source_id or "").strip()
    category = str(category_key or "").strip()
    digest = hashlib.sha256(f"{source}\0{category}".encode("utf-8")).hexdigest()[:24]
    return f"asgn_{digest}"


class SourceAssignmentService:
    """Canonical V1 routing keyed only by search source and media category."""

    def __init__(
        self,
        database: Any,
        target_service: Any,
        source_catalog_provider: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.database = database
        self.target_service = target_service
        self._source_catalog_provider = source_catalog_provider

    def _ensure_history(self, connection: Any) -> None:
        """Create and backfill the append-only assignment history table."""

        # Use individual statements: sqlite3.executescript() may implicitly
        # commit, which would release the BEGIN IMMEDIATE lock used by writers.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_destination_assignment_revisions (
              assignment_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              search_source_id TEXT NOT NULL,
              category_key TEXT NOT NULL,
              destination_id TEXT NOT NULL,
              enabled INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (assignment_id, revision)
            )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assignment_revision_key "
            "ON source_destination_assignment_revisions(search_source_id, category_key, revision)"
        )
        rows = connection.execute(
            "SELECT search_source_id, category_key, destination_id, enabled, revision, created_at "
            "FROM source_destination_assignments"
        ).fetchall()
        for row in rows:
            stable_id = assignment_id(row["search_source_id"], row["category_key"])
            revision = int(row["revision"] or 1)
            if connection.execute(
                "SELECT 1 FROM source_destination_assignment_revisions "
                "WHERE assignment_id=? AND revision=?",
                (stable_id, revision),
            ).fetchone() is not None:
                continue
            connection.execute(
                "INSERT INTO source_destination_assignment_revisions "
                "(assignment_id, revision, search_source_id, category_key, destination_id, enabled, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    stable_id,
                    revision,
                    str(row["search_source_id"]),
                    str(row["category_key"]),
                    str(row["destination_id"]),
                    int(bool(row["enabled"])),
                    str(row["created_at"] or utc_now_iso()),
                ),
            )

    def sources(self) -> dict[str, dict[str, str]]:
        if self._source_catalog_provider is None:
            return {key: dict(value) for key, value in SEARCH_SOURCES.items()}
        result: dict[str, dict[str, str]] = {}
        for raw in self._source_catalog_provider() or []:
            source_id = str(raw.get("search_source_id") or "").strip()
            if not source_id:
                continue
            if source_id in result:
                raise AssignmentError(f"搜索来源重复注册：{source_id}")
            result[source_id] = {
                "label": str(raw.get("label") or source_id),
                "provider_kind": str(raw.get("provider_kind") or ""),
                "source_form": str(raw.get("source_form") or "share"),
                "icon_text": str(raw.get("icon_text") or ""),
            }
        return result

    def set_source_catalog_provider(
        self,
        provider: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self._source_catalog_provider = provider

    def list_assignments(self) -> list[SourceDestinationAssignment]:
        with self.database.connect() as connection:
            self._ensure_history(connection)
            rows = connection.execute(
                "SELECT * FROM source_destination_assignments "
                "ORDER BY search_source_id, category_key"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def get_assignment(
        self, search_source_id: str, category_key: str
    ) -> SourceDestinationAssignment | None:
        with self.database.connect() as connection:
            self._ensure_history(connection)
            row = connection.execute(
                "SELECT * FROM source_destination_assignments "
                "WHERE search_source_id=? AND category_key=?",
                (str(search_source_id), str(category_key)),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def get_revision(self, assignment_id_value: str, revision: int) -> dict[str, Any] | None:
        """Read an immutable routing revision, never the current assignment."""

        stable_id = str(assignment_id_value or "").strip()
        selected_revision = int(revision or 0)
        if not stable_id or selected_revision <= 0:
            return None
        with self.database.connect() as connection:
            self._ensure_history(connection)
            row = connection.execute(
                "SELECT * FROM source_destination_assignment_revisions "
                "WHERE assignment_id=? AND revision=?",
                (stable_id, selected_revision),
            ).fetchone()
        if row is None:
            return None
        return {
            "assignment_id": stable_id,
            "revision": int(row["revision"]),
            "search_source_id": str(row["search_source_id"]),
            "category_key": str(row["category_key"]),
            "destination_id": str(row["destination_id"]),
            "enabled": bool(row["enabled"]),
            "created_at": str(row["created_at"] or ""),
        }

    def get_revision_for_key(
        self, search_source_id: str, category_key: str, revision: int
    ) -> dict[str, Any] | None:
        return self.get_revision(assignment_id(search_source_id, category_key), revision)

    def replace(self, assignments: list[dict[str, Any]]) -> list[SourceDestinationAssignment]:
        parsed = [self._parse(item) for item in assignments]
        keys = {(item.search_source_id, item.category_key) for item in parsed}
        if len(keys) != len(parsed):
            raise AssignmentError("同一搜索源和分类只能配置一个保存目标")
        for item in parsed:
            self._validate(item)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_history(connection)
            current_rows = {
                (str(row["search_source_id"]), str(row["category_key"])): row
                for row in connection.execute("SELECT * FROM source_destination_assignments").fetchall()
            }
            for payload, item in zip(assignments, parsed):
                expected = payload.get("expected_revision", payload.get("revision"))
                if expected in (None, ""):
                    continue
                current = current_rows.get((item.search_source_id, item.category_key))
                actual = int(current["revision"]) if current is not None else None
                if actual != int(expected):
                    raise AssignmentConflictError(
                        f"assignment {item.search_source_id}/{item.category_key} revision conflict: "
                        f"expected {expected}, actual {actual}"
                    )

            desired_keys = {(item.search_source_id, item.category_key) for item in parsed}
            for item in parsed:
                key = (item.search_source_id, item.category_key)
                current = current_rows.get(key)
                if (
                    current is not None
                    and str(current["destination_id"]) == item.destination_id
                    and bool(current["enabled"]) == item.enabled
                ):
                    continue
                next_revision = self._next_revision(connection, *key)
                now = utc_now_iso()
                if current is None:
                    connection.execute(
                        "INSERT INTO source_destination_assignments "
                        "(search_source_id, category_key, destination_id, enabled, revision, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (*key, item.destination_id, int(item.enabled), next_revision, now, now),
                    )
                else:
                    connection.execute(
                        "UPDATE source_destination_assignments SET destination_id=?, enabled=?, revision=?, updated_at=? "
                        "WHERE search_source_id=? AND category_key=?",
                        (item.destination_id, int(item.enabled), next_revision, now, *key),
                    )
                self._insert_history(connection, item, next_revision, now)

            # 删除 current 路由不删除历史。这样在途计划仍可按冻结 revision 校验。
            for key in current_rows:
                if key not in desired_keys:
                    connection.execute(
                        "DELETE FROM source_destination_assignments "
                        "WHERE search_source_id=? AND category_key=?",
                        key,
                    )
        return self.list_assignments()

    def upsert(
        self,
        search_source_id: str,
        category_key: str,
        destination_id: str,
        *,
        enabled: bool = True,
        expected_revision: int | None = None,
    ) -> SourceDestinationAssignment:
        item = SourceDestinationAssignment(
            search_source_id=str(search_source_id or "").strip(),
            category_key=str(category_key or "").strip(),
            destination_id=str(destination_id or "").strip(),
            enabled=bool(enabled),
            revision=1,
            updated_at=utc_now_iso(),
        )
        self._validate(item)
        now = item.updated_at
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_history(connection)
            current = connection.execute(
                "SELECT * FROM source_destination_assignments "
                "WHERE search_source_id=? AND category_key=?",
                (item.search_source_id, item.category_key),
            ).fetchone()
            actual = int(current["revision"]) if current is not None else None
            if expected_revision is not None and actual != int(expected_revision):
                raise AssignmentConflictError(
                    f"assignment {item.search_source_id}/{item.category_key} revision conflict: "
                    f"expected {expected_revision}, actual {actual}"
                )
            if (
                current is not None
                and str(current["destination_id"]) == item.destination_id
                and bool(current["enabled"]) == item.enabled
            ):
                return self._decode(current)

            next_revision = self._next_revision(
                connection, item.search_source_id, item.category_key
            )
            if current is None:
                connection.execute(
                    "INSERT INTO source_destination_assignments "
                    "(search_source_id, category_key, destination_id, enabled, revision, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        item.search_source_id,
                        item.category_key,
                        item.destination_id,
                        int(item.enabled),
                        next_revision,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE source_destination_assignments SET destination_id=?, enabled=?, revision=?, updated_at=? "
                    "WHERE search_source_id=? AND category_key=?",
                    (
                        item.destination_id,
                        int(item.enabled),
                        next_revision,
                        now,
                        item.search_source_id,
                        item.category_key,
                    ),
                )
            self._insert_history(connection, item, next_revision, now)
            row = connection.execute(
                "SELECT * FROM source_destination_assignments "
                "WHERE search_source_id=? AND category_key=?",
                (item.search_source_id, item.category_key),
            ).fetchone()
        assert row is not None
        return self._decode(row)

    def delete(
        self,
        search_source_id: str,
        category_key: str,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_history(connection)
            row = connection.execute(
                "SELECT revision FROM source_destination_assignments "
                "WHERE search_source_id=? AND category_key=?",
                (str(search_source_id), str(category_key)),
            ).fetchone()
            actual = int(row["revision"]) if row is not None else None
            if expected_revision is not None and actual != int(expected_revision):
                raise AssignmentConflictError(
                    f"assignment {search_source_id}/{category_key} revision conflict: "
                    f"expected {expected_revision}, actual {actual}"
                )
            cursor = connection.execute(
                "DELETE FROM source_destination_assignments "
                "WHERE search_source_id=? AND category_key=?",
                (str(search_source_id), str(category_key)),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _next_revision(connection: Any, search_source_id: str, category_key: str) -> int:
        stable_id = assignment_id(search_source_id, category_key)
        row = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) AS revision "
            "FROM source_destination_assignment_revisions WHERE assignment_id=?",
            (stable_id,),
        ).fetchone()
        return int(row["revision"] or 0) + 1

    @staticmethod
    def _insert_history(
        connection: Any,
        item: SourceDestinationAssignment,
        revision: int,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO source_destination_assignment_revisions "
            "(assignment_id, revision, search_source_id, category_key, destination_id, enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                assignment_id(item.search_source_id, item.category_key),
                int(revision),
                item.search_source_id,
                item.category_key,
                item.destination_id,
                int(item.enabled),
                created_at,
            ),
        )

    def resolve_destination(
        self,
        search_source_id: str,
        source_form: str,
        category_key: str,
        audience: str = "guest",
    ) -> str | None:
        del source_form, audience
        item = self.get_assignment(search_source_id, category_key)
        if item is None or not item.enabled:
            return None
        target = self.target_service.get_target(item.destination_id)
        if target is None or not target.enabled:
            return None
        try:
            self.target_service.resolve_binding(item.destination_id, target.current_revision)
        except (TargetValidationError, TargetNotFoundError):
            return None
        return item.destination_id

    def matrix(self) -> dict[str, Any]:
        assignments = {
            (item.search_source_id, item.category_key): item
            for item in self.list_assignments()
        }
        rows: list[dict[str, Any]] = []
        sources = self.sources()
        for source_id, source in sources.items():
            cells = []
            for category_key in CATEGORY_KEYS:
                item = assignments.get((source_id, category_key))
                destination_id = item.destination_id if item and item.enabled else None
                ready = bool(destination_id and self.resolve_destination(
                    source_id, source["source_form"], category_key, "admin"
                ))
                cells.append(
                    {
                        "category_key": category_key,
                        "destination_id": destination_id,
                        "ready": ready,
                        "revision": item.revision if item else None,
                    }
                )
            rows.append({"search_source_id": source_id, **source, "assignments": cells})
        return {
            "success": True,
            "sources": [{"search_source_id": key, **value} for key, value in sources.items()],
            "categories": list(CATEGORY_KEYS),
            "rows": rows,
        }

    def _parse(self, payload: dict[str, Any]) -> SourceDestinationAssignment:
        return SourceDestinationAssignment(
            search_source_id=str(payload.get("search_source_id") or "").strip(),
            category_key=str(payload.get("category_key") or "").strip(),
            destination_id=str(payload.get("destination_id") or "").strip(),
            enabled=bool(payload.get("enabled", True)),
            revision=1,
            updated_at=utc_now_iso(),
        )

    def _validate(self, item: SourceDestinationAssignment) -> None:
        if item.search_source_id not in self.sources():
            raise AssignmentError(f"未知搜索源：{item.search_source_id}")
        if item.category_key not in CATEGORY_KEYS:
            raise AssignmentError(f"未知分类：{item.category_key}")
        target = self.target_service.get_target(item.destination_id)
        if target is None:
            raise TargetNotFoundError(f"目标不存在：{item.destination_id}")
        if not target.enabled:
            raise AssignmentError(f"目标已停用：{item.destination_id}")
        source = self.sources()[item.search_source_id]
        if source.get("source_form") == "magnet" and target.provider_kind != source.get("provider_kind"):
            raise AssignmentError("磁链来源只能分配到同 Provider 的 native_offline 目标")
        binding = self.target_service.resolve_binding(item.destination_id, target.current_revision)
        if source.get("source_form") == "magnet" and binding.provider_kind != source.get("provider_kind"):
            raise AssignmentError("磁链目标 binding 必须绑定同 Provider 账号")

    @staticmethod
    def _decode(row: Any) -> SourceDestinationAssignment:
        return SourceDestinationAssignment(
            search_source_id=str(row["search_source_id"]),
            category_key=str(row["category_key"]),
            destination_id=str(row["destination_id"]),
            enabled=bool(row["enabled"]),
            revision=int(row["revision"]),
            updated_at=str(row["updated_at"]),
        )


def assignment_to_dict(item: SourceDestinationAssignment) -> dict[str, Any]:
    value = asdict(item)
    value["assignment_id"] = assignment_id(item.search_source_id, item.category_key)
    return value


__all__ = [
    "AssignmentError",
    "AssignmentConflictError",
    "SEARCH_SOURCES",
    "SourceAssignmentService",
    "assignment_id",
    "assignment_to_dict",
]
