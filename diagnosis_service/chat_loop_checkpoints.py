"""Diagnosis-owned durable checkpoints for Chat model/tool execution."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from diagnosis_service.database import connect, migrate


JSON = dict[str, Any]
_RETENTION_SECONDS = 30 * 24 * 60 * 60
_MIGRATIONS = ((
    8,
    """
    CREATE TABLE chat_loop_checkpoints (
        request_id TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
        checkpoint_json TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        expires_at REAL NOT NULL CHECK (expires_at > created_at)
    );
    CREATE INDEX chat_loop_checkpoints_expiry ON chat_loop_checkpoints(expires_at);
    """,
),)


class ChatLoopCheckpointError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ChatLoopCheckpoints:
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path)
        self._clock = clock
        migrate(self.db_path, _MIGRATIONS)

    def accept(self, request_id: str, payload: JSON) -> None:
        encoded = _canonical(payload)
        request_hash = hashlib.sha256(encoded.encode()).hexdigest()
        now = self._clock()
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM chat_loop_checkpoints WHERE expires_at <= ?", (now,))
            row = connection.execute(
                "SELECT request_hash FROM chat_loop_checkpoints WHERE request_id = ?", (request_id,),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != request_hash:
                    raise ChatLoopCheckpointError("request_conflict", "Chat execution request conflicts with accepted work")
                return
            connection.execute(
                "INSERT INTO chat_loop_checkpoints VALUES (?, ?, NULL, ?, ?, ?)",
                (request_id, request_hash, now, now, now + _RETENTION_SECONDS),
            )

    def load_loop_checkpoint(self, request_id: str) -> JSON | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT checkpoint_json FROM chat_loop_checkpoints WHERE request_id = ? AND expires_at > ?",
                (request_id, self._clock()),
            ).fetchone()
        if row is None or not row["checkpoint_json"]:
            return None
        value = json.loads(str(row["checkpoint_json"]))
        return value if isinstance(value, dict) else None

    def save_loop_checkpoint(self, request_id: str, checkpoint: JSON) -> None:
        encoded = _canonical(checkpoint)
        if len(encoded.encode()) > 256 * 1024:
            raise ValueError("Chat loop checkpoint exceeds 256 KiB")
        with connect(self.db_path) as connection:
            updated = connection.execute(
                "UPDATE chat_loop_checkpoints SET checkpoint_json = ?, updated_at = ? "
                "WHERE request_id = ? AND expires_at > ?",
                (encoded, self._clock(), request_id, self._clock()),
            ).rowcount
        if updated != 1:
            raise ChatLoopCheckpointError("checkpoint_rejected", "Chat execution checkpoint was not accepted")


def _canonical(value: JSON) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
