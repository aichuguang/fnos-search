from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable


MigrationApply = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: MigrationApply


class MigrationRunner:
    def __init__(self, migrations: Iterable[Migration]) -> None:
        self._migrations = tuple(sorted(migrations, key=lambda item: item.version))
        versions = [item.version for item in self._migrations]
        if len(versions) != len(set(versions)) or any(version <= 0 for version in versions):
            raise ValueError("migration versions must be positive and unique")

    @property
    def latest_version(self) -> int:
        return self._migrations[-1].version if self._migrations else 0

    def current_version(self, connection: sqlite3.Connection) -> int:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not exists:
            return 0
        row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0] if row else 0)

    def ensure_compatible(self, connection: sqlite3.Connection) -> None:
        current = self.current_version(connection)
        if current > self.latest_version:
            raise RuntimeError(
                f"数据库迁移版本 v{current} 高于当前程序支持的 v{self.latest_version}，"
                "拒绝降级启动；请使用匹配或更高版本的程序。"
            )

    def pending(self, connection: sqlite3.Connection) -> list[Migration]:
        self.ensure_compatible(connection)
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not exists:
            return list(self._migrations)
        applied = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        return [item for item in self._migrations if item.version not in applied]

    def run(self, connection: sqlite3.Connection, applied_at: Callable[[], str]) -> list[int]:
        applied: list[int] = []
        for migration in self.pending(connection):
            migration.apply(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, applied_at()),
            )
            applied.append(migration.version)
        return applied
