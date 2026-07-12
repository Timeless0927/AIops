"""Notification Engine-owned durable requests and fake deliveries."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiops.contracts.notification import NotificationContractError, notification_request

from .presentation import render_feishu_webhook


JSON = dict[str, object]
Sender = Callable[[JSON], JSON]
logger = logging.getLogger(__name__)
_SCHEMA_V1 = """
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
_SCHEMA_V2 = """
ALTER TABLE notification_deliveries RENAME TO notification_deliveries_v1;
DROP INDEX notification_deliveries_due;
CREATE TABLE notification_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    destination TEXT NOT NULL CHECK (destination = 'builtin-fake'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'failed', 'sent', 'dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    lease_id TEXT,
    lease_until REAL,
    last_error TEXT,
    message_id TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id)
);
INSERT INTO notification_deliveries (
    id, event_id, destination, status, attempt_count, next_attempt_at,
    last_error, message_id, updated_at
)
SELECT id, event_id, destination, status, attempt_count, next_attempt_at,
       last_error, message_id, updated_at
FROM notification_deliveries_v1;
DROP TABLE notification_deliveries_v1;
CREATE INDEX notification_deliveries_due ON notification_deliveries(status, next_attempt_at, lease_until);
"""
_SCHEMA_V3 = """
ALTER TABLE notification_deliveries RENAME TO notification_deliveries_v2;
DROP INDEX notification_deliveries_due;
CREATE TABLE notification_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'failed', 'sent', 'dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    lease_id TEXT,
    lease_until REAL,
    last_error TEXT,
    message_id TEXT,
    updated_at REAL NOT NULL,
    UNIQUE (event_id, destination),
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id)
);
INSERT INTO notification_deliveries SELECT * FROM notification_deliveries_v2;
DROP TABLE notification_deliveries_v2;
CREATE INDEX notification_deliveries_due ON notification_deliveries(status, next_attempt_at, lease_until);
CREATE TABLE notification_destinations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL CHECK (provider IN ('feishu', 'dingtalk', 'smtp')),
    config_ciphertext TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    tested_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE notification_routes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL UNIQUE,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    match_json TEXT NOT NULL CHECK (json_valid(match_json)),
    destination_ids_json TEXT NOT NULL CHECK (json_valid(destination_ids_json)),
    suppress_reason TEXT,
    is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
INSERT INTO notification_routes VALUES (
    'route:default-suppress', 'Default suppress', 2147483647, 1, '{}', '[]',
    'No notification destination configured', 1, strftime('%s', 'now'), strftime('%s', 'now')
);
CREATE TABLE notification_route_results (
    event_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL,
    suppressed_reason TEXT,
    routed_at REAL NOT NULL,
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id),
    FOREIGN KEY (route_id) REFERENCES notification_routes(id)
);
"""
_SCHEMA_V4 = """
CREATE TABLE notification_templates (
    id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    name TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('feishu', 'dingtalk', 'smtp')),
    event_type TEXT NOT NULL,
    is_builtin INTEGER NOT NULL CHECK (is_builtin IN (0, 1)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    validated_at REAL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    color TEXT NOT NULL,
    button_label TEXT NOT NULL,
    subject TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (id, version)
);
ALTER TABLE notification_routes ADD COLUMN template_id TEXT;
ALTER TABLE notification_deliveries ADD COLUMN template_id TEXT;
ALTER TABLE notification_deliveries ADD COLUMN template_version INTEGER;
ALTER TABLE notification_deliveries ADD COLUMN presentation_json TEXT CHECK (presentation_json IS NULL OR json_valid(presentation_json));
"""
_MIGRATIONS = ((1, _SCHEMA_V1), (2, _SCHEMA_V2), (3, _SCHEMA_V3), (4, _SCHEMA_V4))


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
        delivery_lease_seconds: float = 30.0,
        fake_signing_secret: str = "builtin-fake-signing-secret",
        router: Callable[[JSON], JSON] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._clock = clock
        self._console_base_url = console_base_url.rstrip("/")
        self._max_attempts = max(1, max_attempts)
        self._delivery_lease_seconds = max(1.0, delivery_lease_seconds)
        self._fake_signing_secret = fake_signing_secret
        self._router = router
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
            route = self._router(normalized) if self._router else {"route_id": None, "destination_ids": ["builtin-fake"], "suppressed_reason": None}
            if route["route_id"] is not None:
                conn.execute(
                    "INSERT INTO notification_route_results VALUES (?, ?, ?, ?)",
                    (normalized["event_id"], route["route_id"], route["suppressed_reason"], now),
                )
            deliveries = route.get("deliveries") or [
                {"destination_id": destination, "template_id": None, "template_version": None, "presentation": None}
                for destination in route["destination_ids"]
            ]
            for delivery in deliveries:
                destination = delivery["destination_id"]
                conn.execute(
                    """INSERT INTO notification_deliveries
                       (id, event_id, destination, status, attempt_count, next_attempt_at,
                        lease_id, lease_until, last_error, message_id, updated_at,
                        template_id, template_version, presentation_json)
                       VALUES (?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?)""",
                    (f"delivery:{normalized['event_id']}:{destination}", normalized["event_id"], destination, now, now,
                     delivery["template_id"], delivery["template_version"], _json(delivery["presentation"]) if delivery["presentation"] else None),
                )
        return {"status": "accepted", "event_id": normalized["event_id"], "duplicate": False}

    def get_request(self, event_id: str) -> JSON:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.request_json, d.status, d.attempt_count, d.last_error, d.message_id
                FROM notification_requests r LEFT JOIN notification_deliveries d ON d.event_id = r.event_id
                WHERE r.event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            raise NotificationRequestError("request not found")
        return {
            **json.loads(str(row["request_json"])),
            "delivery_status": str(row["status"]) if row["status"] is not None else "suppressed",
            "delivery_attempt_count": int(row["attempt_count"] or 0),
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
                WHERE (d.status IN ('pending', 'failed') AND d.next_attempt_at <= ?)
                   OR (d.status = 'delivering' AND d.lease_until <= ?)
                ORDER BY d.updated_at, d.id LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return False
            attempt = int(row["attempt_count"]) + 1
            lease_id = uuid.uuid4().hex
            conn.execute(
                "UPDATE notification_deliveries SET status = 'delivering', attempt_count = ?, next_attempt_at = ?, lease_id = ?, lease_until = ?, updated_at = ? WHERE id = ?",
                (attempt, now + min(300, 2 ** attempt), lease_id, now + self._delivery_lease_seconds, now, row["id"]),
            )
            conn.commit()
        request_payload = json.loads(str(row["request_json"]))
        presentation = json.loads(str(row["presentation_json"])) if row["presentation_json"] else None
        delivery = {
            "destination": str(row["destination"]),
            "event_id": row["event_id"],
            **(presentation or {"title": str(request_payload["summary"]), "body": "\n".join(
                (
                    str(request_payload["summary"]),
                    f"Event: {request_payload['event_type']}",
                    f"Severity: {request_payload['severity']}",
                    f"Open: {self._console_base_url}{request_payload['console_path']}",
                )
            )}),
        }
        if row["destination"] == "builtin-fake":
            delivery.update(render_feishu_webhook(
                request_payload,
                self._console_base_url,
                timestamp=str(int(now)),
                signing_secret=self._fake_signing_secret,
            ))
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
                "UPDATE notification_deliveries SET status = ?, lease_id = NULL, lease_until = NULL, last_error = ?, message_id = ?, updated_at = ? WHERE id = ? AND status = 'delivering' AND lease_id = ?",
                (status, message, response.get("message_id") if sent else None, now, row["id"], lease_id),
            )
        return True

    def list_deliveries(self, event_id: str) -> list[JSON]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_deliveries WHERE event_id = ? ORDER BY id", (event_id,)
            ).fetchall()
        return [
            {
                "id": str(row["id"]), "destination_id": str(row["destination"]), "status": str(row["status"]),
                "template_id": row["template_id"], "template_version": row["template_version"],
                "presentation": json.loads(str(row["presentation_json"])) if row["presentation_json"] else None,
            }
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _migrate(self) -> None:
        migrate_notification_database(self.db_path)


def migrate_notification_database(db_path: Path | str) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, schema in _MIGRATIONS:
                if version in applied:
                    continue
                conn.executescript(
                    f"BEGIN IMMEDIATE;\n{schema}\nINSERT INTO schema_migrations VALUES ({version}, strftime('%s', 'now'));\nCOMMIT;"
                )
    finally:
        conn.close()


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
            try:
                worked = store.run_delivery_once(sender)
            except Exception:
                logger.exception("Notification Delivery iteration failed")
                worked = False
            if not worked:
                stop.wait(interval_seconds)

    worker = threading.Thread(target=work, name="notification-delivery-worker", daemon=True)
    worker.start()
    return worker
