"""Notification Engine-owned durable requests and fake deliveries."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiops.contracts.notification import NotificationContractError, notification_request
from apps.service_http import record_sqlite_error

from .presentation import render_feishu_webhook


JSON = dict[str, object]
Sender = Callable[[JSON], JSON]
NoiseEvaluator = Callable[[str, JSON, int, float | None], JSON]
logger = logging.getLogger(__name__)
_TERMINAL_RETENTION_SECONDS = 90 * 24 * 60 * 60
_CLEANUP_BATCH_SIZE = 1000
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
_SCHEMA_V5 = """
ALTER TABLE notification_deliveries RENAME TO notification_deliveries_v4;
DROP INDEX notification_deliveries_due;
CREATE TABLE notification_deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'failed', 'sent', 'dead_letter', 'suppressed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL,
    lease_id TEXT,
    lease_until REAL,
    last_error TEXT,
    message_id TEXT,
    updated_at REAL NOT NULL,
    template_id TEXT,
    template_version INTEGER,
    presentation_json TEXT CHECK (presentation_json IS NULL OR json_valid(presentation_json)),
    noise_result TEXT NOT NULL CHECK (noise_result IN ('immediate', 'quiet_hours', 'hourly_limit', 'digest', 'silence')),
    noise_reason TEXT,
    UNIQUE (event_id, destination),
    FOREIGN KEY (event_id) REFERENCES notification_requests(event_id)
);
INSERT INTO notification_deliveries
SELECT id, event_id, destination, status, attempt_count, next_attempt_at, lease_id, lease_until,
       last_error, message_id, updated_at, template_id, template_version, presentation_json, 'immediate', NULL
FROM notification_deliveries_v4;
DROP TABLE notification_deliveries_v4;
CREATE INDEX notification_deliveries_due ON notification_deliveries(status, next_attempt_at, lease_until);
CREATE TABLE notification_destination_noise_controls (
    destination_id TEXT PRIMARY KEY,
    timezone TEXT NOT NULL,
    quiet_start TEXT,
    quiet_end TEXT,
    hourly_limit INTEGER CHECK (hourly_limit IS NULL OR hourly_limit > 0),
    digest_interval_seconds INTEGER CHECK (digest_interval_seconds IS NULL OR digest_interval_seconds >= 60),
    updated_at REAL NOT NULL,
    FOREIGN KEY (destination_id) REFERENCES notification_destinations(id) ON DELETE CASCADE
);
CREATE TABLE notification_silences (
    id TEXT PRIMARY KEY,
    match_json TEXT NOT NULL CHECK (json_valid(match_json)),
    reason TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX notification_silences_active ON notification_silences(expires_at);
"""
_SCHEMA_V6 = """
ALTER TABLE notification_requests ADD COLUMN request_id TEXT;
ALTER TABLE notification_deliveries ADD COLUMN redelivery_count INTEGER NOT NULL DEFAULT 0 CHECK (redelivery_count >= 0);
CREATE TABLE notification_delivery_attempts (
    id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL,
    redelivery_count INTEGER NOT NULL CHECK (redelivery_count >= 0),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('delivering', 'failed', 'sent', 'dead_letter')),
    retryable INTEGER CHECK (retryable IS NULL OR retryable IN (0, 1)),
    error TEXT,
    started_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE (delivery_id, redelivery_count, attempt_number),
    FOREIGN KEY (delivery_id) REFERENCES notification_deliveries(id)
);
"""
_SCHEMA_V7 = """
ALTER TABLE notification_destinations ADD COLUMN revision TEXT;
UPDATE notification_destinations
SET revision = 'notification-destination-revision:' || lower(hex(randomblob(16)))
WHERE revision IS NULL;
CREATE UNIQUE INDEX notification_destination_revision ON notification_destinations(revision);

ALTER TABLE notification_deliveries ADD COLUMN destination_revision TEXT;
ALTER TABLE notification_deliveries ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1));
ALTER TABLE notification_deliveries ADD COLUMN paused_reason TEXT;
ALTER TABLE notification_deliveries ADD COLUMN last_reason_code TEXT;
UPDATE notification_deliveries
SET destination_revision = (
    SELECT revision FROM notification_destinations
    WHERE notification_destinations.id = notification_deliveries.destination
)
WHERE destination != 'builtin-fake';

ALTER TABLE notification_routes ADD COLUMN selected_destination_revision TEXT;

CREATE TABLE notification_destination_availability (
    destination_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('available', 'degraded', 'unavailable')),
    observed_at REAL NOT NULL,
    reason_code TEXT,
    FOREIGN KEY (destination_id) REFERENCES notification_destinations(id) ON DELETE CASCADE
);

CREATE TABLE notification_destination_operations (
    operation_id TEXT PRIMARY KEY,
    mutation_hash TEXT NOT NULL CHECK (length(mutation_hash) = 64),
    action TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    created_at REAL NOT NULL,
    FOREIGN KEY (destination_id) REFERENCES notification_destinations(id) ON DELETE CASCADE
);
"""
_MIGRATIONS = (
    (1, _SCHEMA_V1), (2, _SCHEMA_V2), (3, _SCHEMA_V3), (4, _SCHEMA_V4),
    (5, _SCHEMA_V5), (6, _SCHEMA_V6), (7, _SCHEMA_V7),
)


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
        retry_base_seconds: float = 2.0,
        delivery_lease_seconds: float = 30.0,
        fake_signing_secret: str = "builtin-fake-signing-secret",
        router: Callable[[JSON], JSON] | None = None,
        noise_evaluator: NoiseEvaluator | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._clock = clock
        self._console_base_url = console_base_url.rstrip("/")
        self._max_attempts = max(1, max_attempts)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._delivery_lease_seconds = max(1.0, delivery_lease_seconds)
        self._fake_signing_secret = fake_signing_secret
        self._router = router
        self._noise_evaluator = noise_evaluator
        self._migrate()

    def accept(self, payload: JSON, *, request_id: str | None = None) -> JSON:
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
                _log("request_accepted", request_id=request_id, correlation_id=normalized["event_id"], duplicate=True)
                return {"status": "accepted", "event_id": normalized["event_id"], "duplicate": True}
            conn.execute(
                "INSERT INTO notification_requests (event_id, content_hash, request_json, accepted_at, request_id) VALUES (?, ?, ?, ?, ?)",
                (normalized["event_id"], digest, encoded, now, str(request_id)[:128] if request_id else None),
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
                noise = delivery.get("noise")
                if noise is None and self._noise_evaluator is not None:
                    count, oldest = conn.execute(
                        """SELECT COUNT(*), MIN(d.updated_at) FROM notification_deliveries d
                           JOIN notification_requests r ON r.event_id = d.event_id
                           WHERE d.destination = ? AND d.status != 'suppressed' AND d.updated_at > ?
                             AND json_extract(r.request_json, '$.severity') != 'critical'""",
                        (destination, now - 3600),
                    ).fetchone()
                    noise = self._noise_evaluator(destination, normalized, int(count), float(oldest) if oldest is not None else None)
                noise = noise or {"result": "immediate", "next_attempt_at": now, "reason": None}
                status = "suppressed" if noise["result"] == "silence" else "pending"
                conn.execute(
                    """INSERT INTO notification_deliveries
                       (id, event_id, destination, status, attempt_count, next_attempt_at,
                        lease_id, lease_until, last_error, message_id, updated_at,
                        template_id, template_version, presentation_json, noise_result, noise_reason)
                       VALUES (?, ?, ?, ?, 0, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)""",
                    (f"delivery:{normalized['event_id']}:{destination}", normalized["event_id"], destination,
                     status, noise["next_attempt_at"], now, delivery["template_id"], delivery["template_version"],
                     _json(delivery["presentation"]) if delivery["presentation"] else None,
                     noise["result"], noise["reason"]),
                )
        _log("request_accepted", request_id=request_id, correlation_id=normalized["event_id"], duplicate=False)
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
                SELECT d.*, r.request_json, r.request_id FROM notification_deliveries d
                JOIN notification_requests r ON r.event_id = d.event_id
                WHERE (d.status IN ('pending', 'failed') AND d.next_attempt_at <= ?)
                   OR (d.status = 'delivering' AND d.lease_until <= ?)
                ORDER BY d.updated_at, d.id LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return False
            if row["noise_result"] in {"quiet_hours", "hourly_limit"} and self._noise_evaluator is not None:
                request_payload = json.loads(str(row["request_json"]))
                count, oldest = conn.execute(
                    """SELECT COUNT(*), MIN(d.updated_at) FROM notification_deliveries d
                       JOIN notification_requests r ON r.event_id = d.event_id
                       WHERE d.destination = ? AND d.status = 'sent' AND d.updated_at > ?
                         AND json_extract(r.request_json, '$.severity') != 'critical'""",
                    (row["destination"], now - 3600),
                ).fetchone()
                noise = self._noise_evaluator(
                    str(row["destination"]),
                    request_payload,
                    int(count),
                    float(oldest) if oldest is not None else None,
                )
                if noise["result"] != "immediate":
                    conn.execute(
                        "UPDATE notification_deliveries SET status = ?, next_attempt_at = ?, noise_result = ?, noise_reason = ?, updated_at = ? WHERE id = ?",
                        ("suppressed" if noise["result"] == "silence" else "pending", noise["next_attempt_at"], noise["result"], noise["reason"], now, row["id"]),
                    )
                    return True
            rows = [row]
            if row["noise_result"] == "digest":
                rows = conn.execute(
                    """SELECT d.*, r.request_json, r.request_id FROM notification_deliveries d
                       JOIN notification_requests r ON r.event_id = d.event_id
                       WHERE d.destination = ? AND d.noise_result = 'digest' AND d.next_attempt_at = ?
                         AND ((d.status IN ('pending', 'failed') AND d.next_attempt_at <= ?)
                           OR (d.status = 'delivering' AND d.lease_until <= ?))
                       ORDER BY d.updated_at, d.id""",
                    (row["destination"], row["next_attempt_at"], now, now),
                ).fetchall()
            attempt = max(int(item["attempt_count"]) for item in rows) + 1
            lease_id = uuid.uuid4().hex
            ids = [str(item["id"]) for item in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE notification_deliveries SET status = 'delivering', attempt_count = ?, next_attempt_at = ?, lease_id = ?, lease_until = ?, updated_at = ? WHERE id IN ({placeholders})",
                (attempt, now + min(300, 2 ** attempt), lease_id, now + self._delivery_lease_seconds, now, *ids),
            )
            for item in rows:
                conn.execute(
                    "INSERT INTO notification_delivery_attempts VALUES (?, ?, ?, ?, 'delivering', NULL, NULL, ?, NULL)",
                    (f"{item['id']}:{item['redelivery_count']}:{attempt}", item["id"], item["redelivery_count"], attempt, now),
                )
            conn.commit()
        request_payloads = [json.loads(str(item["request_json"])) for item in rows]
        presentations = [json.loads(str(item["presentation_json"])) if item["presentation_json"] else None for item in rows]
        delivery = _delivery_payload(str(row["destination"]), row["event_id"], request_payloads[0], presentations[0], self._console_base_url)
        if len(rows) > 1:
            items = [_delivery_payload(str(item["destination"]), item["event_id"], request, presentation, self._console_base_url) for item, request, presentation in zip(rows, request_payloads, presentations)]
            delivery = {
                "destination": str(row["destination"]),
                "event_id": row["event_id"],
                "title": f"{len(items)} AIOps notifications",
                "body": "\n\n".join(f"{item['title']}\n{item['body']}" for item in items),
                "digest_count": len(items),
            }
            if all("html" in item for item in items):
                delivery.update({
                    "subject": delivery["title"],
                    "html": "<hr>".join(str(item["html"]) for item in items),
                    "plain_text": "\n\n".join(str(item.get("plain_text") or item["body"]) for item in items),
                })
        if row["destination"] == "builtin-fake":
            delivery.update(render_feishu_webhook(
                request_payloads[0],
                self._console_base_url,
                timestamp=str(int(now)),
                signing_secret=self._fake_signing_secret,
            ))
        try:
            response = sender(delivery)
            sent = bool(response.get("ok"))
            message = None if sent else str(response.get("error") or "fake destination failed")[:500]
        except Exception as exc:
            sent = False
            response = {}
            message = f"{type(exc).__name__}: {exc}"[:500]
        retryable = bool(response.get("retryable", True))
        status = "sent" if sent else "dead_letter" if not retryable or attempt >= self._max_attempts else "failed"
        retry_after = response.get("retry_after")
        backoff = min(300.0, self._retry_base_seconds * (2 ** (attempt - 1)))
        retry_delay = min(300.0, max(backoff, float(retry_after))) if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and math.isfinite(retry_after) else backoff
        next_attempt_at = now + retry_delay if status == "failed" else None
        with self._connect() as conn:
            for item in rows:
                updated = conn.execute(
                    "UPDATE notification_deliveries SET status = ?, next_attempt_at = ?, lease_id = NULL, lease_until = NULL, last_error = ?, message_id = ?, updated_at = ? WHERE id = ? AND status = 'delivering' AND lease_id = ?",
                    (status, next_attempt_at, message, response.get("message_id") if sent else None, now, item["id"], lease_id),
                )
                if updated.rowcount:
                    conn.execute(
                        """UPDATE notification_delivery_attempts
                           SET outcome = ?, retryable = ?, error = ?, completed_at = ?
                           WHERE id = ?""",
                        (status, int(retryable), message, now,
                         f"{item['id']}:{item['redelivery_count']}:{attempt}"),
                    )
                    _log(
                        "delivery_attempted", correlation_id=item["event_id"], destination_id=item["destination"],
                        request_id=item["request_id"], outcome=status, attempt=attempt, retryable=retryable,
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
                "noise_result": str(row["noise_result"]), "noise_reason": row["noise_reason"],
                "template_id": row["template_id"], "template_version": row["template_version"],
                "presentation": json.loads(str(row["presentation_json"])) if row["presentation_json"] else None,
            }
            for row in rows
        ]

    def list_delivery_results(self, limit: int = 200) -> list[JSON]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT d.*, r.request_json, r.request_id FROM notification_deliveries d
                   JOIN notification_requests r ON r.event_id = d.event_id
                   ORDER BY d.updated_at DESC, d.id LIMIT ?""",
                (max(1, min(limit, 200)),),
            ).fetchall()
            attempts = _attempt_history(conn, [str(row["id"]) for row in rows])
        return [_delivery_result(row, attempts.get(str(row["id"]), [])) for row in rows]

    def get_delivery_results(self, event_id: str) -> list[JSON]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT d.*, r.request_json FROM notification_deliveries d
                   JOIN notification_requests r ON r.event_id = d.event_id
                   WHERE d.event_id = ? ORDER BY d.id""",
                (event_id,),
            ).fetchall()
            if not rows:
                raise NotificationRequestError("delivery results not found")
            attempts = _attempt_history(conn, [str(row["id"]) for row in rows])
        return [_delivery_result(row, attempts.get(str(row["id"]), [])) for row in rows]

    def redeliver(self, delivery_id: str) -> JSON:
        now = self._clock()
        with self._connect() as conn:
            updated = conn.execute(
                """UPDATE notification_deliveries
                   SET status = 'pending', attempt_count = 0, next_attempt_at = ?, lease_id = NULL,
                       lease_until = NULL, last_error = NULL, message_id = NULL,
                       redelivery_count = redelivery_count + 1, updated_at = ?
                   WHERE id = ? AND status = 'dead_letter'""",
                (now, now, delivery_id),
            )
            if not updated.rowcount:
                raise NotificationRequestError("dead-letter delivery not found")
            row = conn.execute(
                """SELECT d.*, r.request_json FROM notification_deliveries d
                   JOIN notification_requests r ON r.event_id = d.event_id WHERE d.id = ?""",
                (delivery_id,),
            ).fetchone()
            attempts = _attempt_history(conn, [delivery_id])
        return _delivery_result(row, attempts.get(delivery_id, []))

    def cleanup_expired(self) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT r.event_id
                   FROM notification_requests r
                   LEFT JOIN notification_deliveries d ON d.event_id = r.event_id
                   GROUP BY r.event_id, r.accepted_at
                   HAVING COALESCE(MAX(d.updated_at), r.accepted_at) <= ?
                      AND SUM(CASE WHEN d.status IS NOT NULL
                                        AND d.status NOT IN ('sent', 'suppressed')
                                   THEN 1 ELSE 0 END) = 0
                   ORDER BY COALESCE(MAX(d.updated_at), r.accepted_at), r.event_id
                   LIMIT ?""",
                (self._clock() - _TERMINAL_RETENTION_SECONDS, _CLEANUP_BATCH_SIZE),
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in rows]
            if not event_ids:
                return 0
            placeholders = ",".join("?" for _ in event_ids)
            conn.execute(
                f"""DELETE FROM notification_delivery_attempts
                    WHERE delivery_id IN (
                        SELECT id FROM notification_deliveries WHERE event_id IN ({placeholders})
                    )""",
                event_ids,
            )
            conn.execute(f"DELETE FROM notification_deliveries WHERE event_id IN ({placeholders})", event_ids)
            conn.execute(f"DELETE FROM notification_route_results WHERE event_id IN ({placeholders})", event_ids)
            conn.execute(f"DELETE FROM notification_requests WHERE event_id IN ({placeholders})", event_ids)
            conn.commit()
        return len(event_ids)

    def metrics(self) -> str:
        with self._connect() as conn:
            delivery_counts = dict(conn.execute("SELECT status, COUNT(*) FROM notification_deliveries GROUP BY status"))
            attempt_counts = dict(conn.execute("SELECT outcome, COUNT(*) FROM notification_delivery_attempts GROUP BY outcome"))
            oldest = conn.execute(
                """SELECT MIN(requests.accepted_at) FROM notification_deliveries deliveries
                   JOIN notification_requests requests ON requests.event_id = deliveries.event_id
                   WHERE deliveries.status IN ('pending', 'delivering', 'failed')"""
            ).fetchone()[0]
            cleanup_eligible = conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT r.event_id FROM notification_requests r
                       LEFT JOIN notification_deliveries d ON d.event_id = r.event_id
                       GROUP BY r.event_id, r.accepted_at
                       HAVING COALESCE(MAX(d.updated_at), r.accepted_at) <= ?
                          AND SUM(CASE WHEN d.status IS NOT NULL
                                            AND d.status NOT IN ('sent', 'suppressed')
                                       THEN 1 ELSE 0 END) = 0
                   )""",
                (self._clock() - _TERMINAL_RETENTION_SECONDS,),
            ).fetchone()[0]
        lines = [
            "# HELP aiops_notification_deliveries Current Notification Deliveries by status",
            "# TYPE aiops_notification_deliveries gauge",
        ]
        for status in ("pending", "delivering", "failed", "sent", "dead_letter", "suppressed"):
            lines.append(f'aiops_notification_deliveries{{status="{status}"}} {int(delivery_counts.get(status, 0))}')
        lines.extend((
            "# HELP aiops_notification_delivery_attempts Durable Notification Delivery attempts retained by outcome",
            "# TYPE aiops_notification_delivery_attempts gauge",
        ))
        for outcome in ("delivering", "failed", "sent", "dead_letter"):
            lines.append(f'aiops_notification_delivery_attempts{{outcome="{outcome}"}} {int(attempt_counts.get(outcome, 0))}')
        lines.extend((
            "# HELP aiops_notification_delivery_oldest_age_seconds Age of the oldest unfinished Notification Delivery",
            "# TYPE aiops_notification_delivery_oldest_age_seconds gauge",
            f"aiops_notification_delivery_oldest_age_seconds {max(0.0, self._clock() - float(oldest)) if oldest else 0.0:.1f}",
            "# HELP aiops_notification_cleanup_eligible Terminal Notification Requests currently eligible for cleanup",
            "# TYPE aiops_notification_cleanup_eligible gauge",
            f"aiops_notification_cleanup_eligible {int(cleanup_eligible)}",
        ))
        return "\n".join(lines) + "\n"

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


def _log(event: str, **fields: object) -> None:
    logger.info(_json({"service": "notification-engine", "event": event, **fields}))


def _delivery_payload(destination: str, event_id: object, request: JSON, presentation: JSON | None, console_base_url: str) -> JSON:
    return {
        "destination": destination,
        "event_id": event_id,
        **(presentation or {"title": str(request["summary"]), "body": "\n".join(
            (
                str(request["summary"]),
                f"Event: {request['event_type']}",
                f"Severity: {request['severity']}",
                f"Open: {console_base_url}{request['console_path']}",
            )
        )}),
    }


def _delivery_result(row: sqlite3.Row, attempts: list[JSON]) -> JSON:
    request = json.loads(str(row["request_json"]))
    return {
        "id": str(row["id"]),
        "event_id": str(row["event_id"]),
        "destination_id": str(row["destination"]),
        "severity": str(request["severity"]),
        "summary": str(request["summary"]),
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "redelivery_count": int(row["redelivery_count"]),
        "last_error": row["last_error"],
        "attempts": attempts,
        "noise_result": str(row["noise_result"]),
        "noise_reason": row["noise_reason"],
        "next_attempt_at": row["next_attempt_at"],
        "updated_at": float(row["updated_at"]),
    }


def _attempt_history(conn: sqlite3.Connection, delivery_ids: list[str]) -> dict[str, list[JSON]]:
    if not delivery_ids:
        return {}
    placeholders = ",".join("?" for _ in delivery_ids)
    rows = conn.execute(
        f"SELECT * FROM notification_delivery_attempts WHERE delivery_id IN ({placeholders}) ORDER BY started_at, redelivery_count, attempt_number",
        delivery_ids,
    ).fetchall()
    result: dict[str, list[JSON]] = {}
    for row in rows:
        result.setdefault(str(row["delivery_id"]), []).append({
            "attempt": int(row["attempt_number"]),
            "redelivery": int(row["redelivery_count"]),
            "outcome": str(row["outcome"]),
            "retryable": bool(row["retryable"]) if row["retryable"] is not None else None,
            "error": row["error"],
            "started_at": float(row["started_at"]),
            "completed_at": float(row["completed_at"]) if row["completed_at"] is not None else None,
        })
    return result


def start_delivery_worker(
    store: NotificationStore,
    *,
    sender: Sender,
    interval_seconds: float = 1.0,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    stop = stop_event or threading.Event()

    def work() -> None:
        next_cleanup_at = 0.0
        while not stop.is_set():
            try:
                monotonic_now = time.monotonic()
                if monotonic_now >= next_cleanup_at:
                    store.cleanup_expired()
                    next_cleanup_at = monotonic_now + 60 * 60
                worked = store.run_delivery_once(sender)
            except Exception as exc:
                if isinstance(exc, sqlite3.Error):
                    record_sqlite_error("notification-engine")
                logger.exception("Notification Delivery iteration failed")
                worked = False
            if not worked:
                stop.wait(interval_seconds)

    worker = threading.Thread(target=work, name="notification-delivery-worker", daemon=True)
    worker.start()
    return worker
