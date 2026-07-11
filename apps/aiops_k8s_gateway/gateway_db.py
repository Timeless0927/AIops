"""Shared Gateway database constraints."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable


_MIGRATIONS: dict[int, str] = {}
_MIGRATION_LOCK = threading.Lock()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register_migrations(migrations: Iterable[tuple[int, str]]) -> None:
    for version, sql in migrations:
        previous = _MIGRATIONS.setdefault(version, sql)
        if previous != sql:
            raise RuntimeError(f"Gateway migration version {version} is already registered")


class GatewayDatabase:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self._configured_path = Path(db_path).expanduser() if db_path else None

    @property
    def db_path(self) -> Path:
        return self._configured_path or Path(os.getenv("AIOPS_DATA_DIR", "data")).expanduser() / "gateway.db"

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
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


def insert_admin_audit(
    conn: sqlite3.Connection,
    *,
    actor_id: str | None,
    target_type: str,
    target_id: str | None,
    action: str,
    reason: str,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
    result: str,
    request_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO admin_audit (
            actor_id, target_type, target_id, action, reason,
            before_json, after_json, result, request_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_id,
            target_type,
            target_id,
            action,
            reason,
            json.dumps(before, ensure_ascii=False, sort_keys=True) if before else None,
            json.dumps(after, ensure_ascii=False, sort_keys=True) if after else None,
            result,
            request_id,
            time.time(),
        ),
    )
