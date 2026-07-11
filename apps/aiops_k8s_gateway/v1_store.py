"""Gateway-owned V1 SQLite state."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from aiops.domain.identity import Actor, AuthSession


_MIGRATIONS = (
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
)
_MIGRATION_LOCK = threading.Lock()


class GatewayV1Store:
    def __init__(self, db_path: Path | str | None = None, *, ttl_seconds: int = 8 * 60 * 60) -> None:
        self._configured_path = Path(db_path).expanduser() if db_path else None
        self.ttl_seconds = ttl_seconds

    @property
    def db_path(self) -> Path:
        if self._configured_path is not None:
            return self._configured_path
        return Path(os.getenv("AIOPS_DATA_DIR", "data")).expanduser() / "gateway.db"

    def issue(self, actor: Actor) -> AuthSession:
        now = time.time()
        token = secrets.token_urlsafe(32)
        session = AuthSession(token=token, actor=actor, created_at=now, expires_at=now + self.ttl_seconds)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO session_actors (actor_id, actor_json) VALUES (?, ?) "
                "ON CONFLICT(actor_id) DO UPDATE SET actor_json = excluded.actor_json",
                (actor.actor_id, json.dumps(actor.to_dict(), separators=(",", ":"))),
            )
            conn.execute(
                "INSERT INTO sessions (token_hash, actor_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_token_hash(token), actor.actor_id, session.created_at, session.expires_at),
            )
        return session

    def get(self, token: str) -> AuthSession | None:
        if not token:
            return None
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            row = conn.execute(
                """
                SELECT s.created_at, s.expires_at, a.actor_json
                FROM sessions s
                JOIN session_actors a ON a.actor_id = s.actor_id
                WHERE s.token_hash = ?
                """,
                (_token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        return AuthSession(
            token=token,
            actor=Actor.from_mapping(json.loads(str(row["actor_json"]))),
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
        )

    def revoke(self, token: str) -> None:
        if not token:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM session_actors")

    def list_incidents(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, severity, status, created_at, updated_at FROM incidents ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        with _MIGRATION_LOCK:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in _MIGRATIONS:
                if version in applied:
                    continue
                conn.executescript(sql)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, time.time()),
                )
        return conn


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
