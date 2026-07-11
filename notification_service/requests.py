"""Notification Engine-owned durable requests and fake deliveries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiops.contracts.notification import NotificationContractError, notification_request

from .presentation import render_feishu_card


JSON = dict[str, object]
Sender = Callable[[JSON], JSON]
_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE notification_requests (
    event_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    accepted_at REAL NOT NULL
);
CREATE TABLE notification_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    destination TEXT NOT NULL CHECK (destination = 'builtin-fake'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'failed', 'sent', 'dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    message_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id)
);
CREATE INDEX notification_deliveries_due ON notification_deliveries(status, next_attempt_at);
"""


class NotificationRequestError(ValueError):
    pass


class NotificationStore:
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        console_base_url: str = "https://aiops.invalid",
        max_attempts: int = 3,
    ) -> None:
        self.db_path = Path(db_path)
        self._clock = clock
        self._console_base_url = console_base_url.rstrip("/")
        self._max_attempts = max(1, max_attempts)
        self._migrate()

    def accept(self, payload: JSON) -> JSON:
        try:
            normalized = notification_request(**payload)
        except NotificationContractError as exc:
            raise NotificationRequestError(str(exc)) from exc
        encoded = _json(normalized)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT content_hash FROM notification_requests WHERE event_id = ?", (normalized["event_id"],)
            ).fetchone()
            if row is not None:
                if str(row["content_hash"]) != digest:
                    raise NotificationRequestError("event_id conflict")
                return {"status": "accepted", "event_id": normalized["event_id"], "duplicate": True}
            conn.execute(
                "INSERT INTO notification_requests VALUES (?, ?, ?, ?)",
                (normalized["event_id"], digest, encoded, now),
            )
            conn.execute(
                "INSERT INTO notification_deliveries VALUES (?, ?, 'builtin-fake', 'pending', 0, ?, NULL, NULL, ?)",
                (f"delivery:{normalized['event_id']}", normalized["event_id"], now, now),
            )
        return {"status": "accepted", "event_id": normalized["event_id"], "duplicate": False}

    def get_request(self, event_id: str) -> JSON:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.request_json, d.status, d.attempt_count, d.last_error, d.message_id
                FROM notification_requests r JOIN notification_deliveries d ON d.event_id = r.event_id
                WHERE r.event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            raise NotificationRequestError("request not found")
        return {
            **json.loads(str(row["request_json"])),
            "delivery_status": str(row["status"]),
            "delivery_attempt_count": int(row["attempt_count"]),
            "delivery_error": row["last_error"],
            "message_id": row["message_id"],
        }

    def run_delivery_once(self, sender: Sender) -> bool:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT d.*, r.request_json FROM notification_deliveries d
                JOIN notification_requests r ON r.event_id = d.event_id
                WHERE d.status IN ('pending', 'failed') AND d.next_attempt_at <= ?
                ORDER BY d.updated_at, d.id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return False
            attempt = int(row["attempt_count"]) + 1
            conn.execute(
                "UPDATE notification_deliveries SET attempt_count = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (attempt, now + min(300, 2 ** attempt), now, row["id"]),
            )
            conn.commit()
        request_payload = json.loads(str(row["request_json"]))
        delivery = {
            "destination": "builtin-fake",
            "event_id": row["event_id"],
            "card": render_feishu_card(request_payload, self._console_base_url),
        }
        try:
            response = sender(delivery)
            sent = bool(response.get("ok"))
            message = None if sent else str(response.get("error") or "fake destination failed")
        except Exception as exc:
            sent = False
            response = {}
            message = f"{type(exc).__name__}: {exc}"
        status = "sent" if sent else "dead_letter" if attempt >= self._max_attempts else "failed"
        with self._connect() as conn:
            conn.execute(
                "UPDATE notification_deliveries SET status = ?, last_error = ?, message_id = ?, updated_at = ? WHERE id = ?",
                (status, message, response.get("message_id") if sent else None, now, row["id"]),
            )
        return True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _migrate(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (_SCHEMA_VERSION,)).fetchone() is None:
                conn.executescript(
                    f"BEGIN IMMEDIATE;\n{_SCHEMA}\nINSERT INTO schema_migrations VALUES ({_SCHEMA_VERSION}, strftime('%s', 'now'));\nCOMMIT;"
                )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def start_delivery_worker(
    store: NotificationStore,
    *,
    sender: Sender,
    interval_seconds: float = 1.0,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    stop = stop_event or threading.Event()

    def work() -> None:
        while not stop.is_set():
            if not store.run_delivery_once(sender):
                stop.wait(interval_seconds)

    worker = threading.Thread(target=work, name="notification-delivery-worker", daemon=True)
    worker.start()
    return worker
