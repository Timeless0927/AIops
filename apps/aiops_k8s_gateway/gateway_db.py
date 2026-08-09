"""Shared Gateway database constraints."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from aiops.domain.identity import SQLiteIdentityStore


_MIGRATIONS: dict[int, str] = {}
_MIGRATION_LOCK = threading.Lock()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register_migrations(migrations: Iterable[tuple[int, str]]) -> None:
    for version, sql in migrations:
        previous = _MIGRATIONS.setdefault(version, sql)
        if previous != sql:
            raise RuntimeError(f"Gateway migration version {version} is already registered")


_BASE_MIGRATIONS = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS session_actors (
            actor_id TEXT PRIMARY KEY,
            actor_json TEXT NOT NULL CHECK (json_valid(actor_json))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
            actor_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL CHECK (expires_at > created_at),
            FOREIGN KEY (actor_id) REFERENCES session_actors(actor_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS sessions_by_expiry ON sessions(expires_at);

        CREATE TABLE IF NOT EXISTS incidents (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL CHECK (length(title) > 0),
            severity TEXT NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
            status TEXT NOT NULL CHECK (status IN ('active', 'resolved')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL CHECK (updated_at >= created_at)
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE sessions ADD COLUMN fresh_at REAL;
        UPDATE sessions SET fresh_at = created_at WHERE fresh_at IS NULL;

        CREATE TABLE teams (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE CHECK (length(name) > 0),
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE team_memberships (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (user_id, team_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
        );

        CREATE TABLE role_bindings (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('platform_administrator', 'sre')),
            scope_type TEXT NOT NULL CHECK (scope_type IN ('platform', 'team')),
            scope_id TEXT,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK (
                (role = 'platform_administrator' AND scope_type = 'platform' AND scope_id IS NULL)
                OR (role = 'sre' AND scope_type = 'team' AND scope_id IS NOT NULL)
            ),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (scope_id) REFERENCES teams(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX role_binding_identity
            ON role_bindings(user_id, role, scope_type, IFNULL(scope_id, ''));

        CREATE TABLE admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id TEXT,
            target_type TEXT NOT NULL,
            target_id TEXT,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            result TEXT NOT NULL,
            request_id TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX admin_audit_recent ON admin_audit(created_at DESC);
        """,
    ),
)
register_migrations(_BASE_MIGRATIONS)


class GatewayDatabase:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self._configured_path = Path(db_path).expanduser() if db_path else None

    @property
    def db_path(self) -> Path:
        return self._configured_path or Path(os.getenv("AIOPS_DATA_DIR", "data")).expanduser() / "gateway.db"

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        SQLiteIdentityStore(self.db_path).close()
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        with _MIGRATION_LOCK:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in sorted(_MIGRATIONS.items()):
                if version in applied:
                    continue
                try:
                    conn.executescript(
                        f"BEGIN IMMEDIATE;\n{sql}\n"
                        f"INSERT INTO schema_migrations (version, applied_at) VALUES ({version}, strftime('%s', 'now'));\n"
                        "COMMIT;"
                    )
                except BaseException:
                    if conn.in_transaction:
                        conn.rollback()
                    conn.close()
                    raise
        return conn
