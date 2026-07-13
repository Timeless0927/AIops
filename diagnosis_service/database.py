"""Shared SQLite infrastructure for Diagnosis-owned modules."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path


Migration = tuple[int, str]


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def migrate(path: Path, migrations: Iterable[Migration]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        applied = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for version, schema in migrations:
            if version in applied:
                continue
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{schema}\n"
                f"INSERT INTO schema_migrations VALUES ({version}, strftime('%s', 'now'));\n"
                "COMMIT;"
            )
