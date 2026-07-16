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

from .database import migrate_notification_database
from .presentation import render_feishu_webhook


JSON = dict[str, object]
Sender = Callable[[JSON], JSON]
NoiseEvaluator = Callable[[str, JSON, int, float | None], JSON]
logger = logging.getLogger(__name__)
_TERMINAL_RETENTION_SECONDS = 90 * 24 * 60 * 60
_CLEANUP_BATCH_SIZE = 1000
_CLEANUP_ELIGIBLE_GROUP = """
    FROM notification_requests r
    LEFT JOIN notification_deliveries d ON d.event_id = r.event_id
    GROUP BY r.event_id, r.accepted_at
    HAVING COALESCE(MAX(d.updated_at), r.accepted_at) <= ?
       AND SUM(CASE WHEN d.status IS NOT NULL AND (
                          (d.is_test = 0 AND d.status NOT IN ('sent', 'suppressed'))
                       OR (d.is_test = 1 AND d.status NOT IN ('sent', 'suppressed', 'dead_letter'))
                    ) THEN 1 ELSE 0 END) = 0
       AND NOT EXISTS (
         SELECT 1 FROM notification_deliveries retained
         WHERE retained.event_id = r.event_id AND retained.is_test = 1
           AND retained.id = (
             SELECT latest.id FROM notification_deliveries latest
             JOIN notification_destination_operations operation
               ON operation.operation_id = latest.id AND operation.action = 'test'
             WHERE latest.destination = retained.destination
               AND latest.destination_revision = retained.destination_revision
               AND latest.is_test = 1
             ORDER BY operation.created_at DESC, operation.rowid DESC LIMIT 1
           )
       )
"""
_CURRENT_REVISION_ELIGIBLE = """
    d.paused_reason IS NULL
    AND (d.destination_revision IS NULL OR d.destination = 'builtin-fake' OR EXISTS (
        SELECT 1 FROM notification_destinations destination
        WHERE destination.id = d.destination
          AND destination.revision = d.destination_revision
    ))
"""


class NotificationRequestError(ValueError):
    pass


def pause_destination_deliveries_for_revision_change(
    conn: sqlite3.Connection,
    destination_id: str,
    *,
    now: float,
) -> None:
    """Pause unfinished Delivery rows inside the caller's shared transaction."""
    conn.execute(
        """UPDATE notification_deliveries
           SET paused_reason = 'configuration_changed', updated_at = ?
           WHERE destination = ? AND status IN ('pending', 'failed', 'delivering')""",
        (now, destination_id),
    )


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
        self._max_attempts = min(3, max(1, max_attempts))
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
                           WHERE d.destination = ? AND d.is_test = 0
                             AND d.status != 'suppressed' AND d.updated_at > ?
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
                        template_id, template_version, presentation_json, noise_result, noise_reason,
                        destination_revision, paused_reason)
                       VALUES (?, ?, ?, ?, 0, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (f"delivery:{normalized['event_id']}:{destination}", normalized["event_id"], destination,
                     status, noise["next_attempt_at"], now, delivery["template_id"], delivery["template_version"],
                     _json(delivery["presentation"]) if delivery["presentation"] else None,
                     noise["result"], noise["reason"], delivery.get("destination_revision"),
                     delivery.get("paused_reason")),
                )
        _log("request_accepted", request_id=request_id, correlation_id=normalized["event_id"], duplicate=False)
        return {"status": "accepted", "event_id": normalized["event_id"], "duplicate": False}

    def accept_test(self, destination_id: str, *, expected_revision: str, operation_id: str) -> JSON:
        now = self._clock()
        normalized = notification_request(
            event_id=operation_id,
            event_type="connector.recovered",
            occurred_at=now,
            severity="info",
            subject={"type": "connector", "id": destination_id, "version": 1},
            scope={"environment": "pilot"},
            summary="AIOps Notification Destination test",
            facts={"connector_id": destination_id, "cluster_id": "notification-engine", "status": "recovered"},
            console_path="/admin",
        )
        encoded = _json(normalized)
        mutation_hash = hashlib.sha256(
            _json({"destination_id": destination_id, "revision": expected_revision}).encode()
        ).hexdigest()
        presentation = {
            "title": "[TEST] AIOps Notification Destination",
            "body": f"AIOps Notification Destination test\nTest ID: {operation_id}\nTime: {int(now)}",
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                """SELECT mutation_hash, action, destination_id
                   FROM notification_destination_operations WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()
            if operation is not None:
                if (
                    str(operation["mutation_hash"]) != mutation_hash
                    or str(operation["action"]) != "test"
                    or str(operation["destination_id"]) != destination_id
                ):
                    raise NotificationRequestError("operation_id conflict")
                row = conn.execute("SELECT * FROM notification_deliveries WHERE id = ?", (operation_id,)).fetchone()
                return _test_result(row)
            destination = conn.execute(
                "SELECT revision FROM notification_destinations WHERE id = ?", (destination_id,)
            ).fetchone()
            if destination is None:
                raise NotificationRequestError("destination not found")
            if str(destination["revision"]) != expected_revision:
                raise NotificationRequestError("destination revision conflict")
            conn.execute(
                "INSERT INTO notification_requests VALUES (?, ?, ?, ?, ?)",
                (operation_id, hashlib.sha256(encoded.encode()).hexdigest(), encoded, now, operation_id),
            )
            conn.execute(
                """INSERT INTO notification_deliveries
                   (id, event_id, destination, status, attempt_count, next_attempt_at,
                    lease_id, lease_until, last_error, message_id, updated_at,
                    template_id, template_version, presentation_json, noise_result, noise_reason,
                    redelivery_count, destination_revision, is_test, paused_reason, last_reason_code)
                   VALUES (?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, NULL, ?,
                           NULL, NULL, ?, 'immediate', NULL, 0, ?, 1, NULL, NULL)""",
                (operation_id, operation_id, destination_id, now, now, _json(presentation), expected_revision),
            )
            conn.execute(
                "INSERT INTO notification_destination_operations VALUES (?, ?, 'test', ?, ?)",
                (operation_id, mutation_hash, destination_id, now),
            )
            row = conn.execute("SELECT * FROM notification_deliveries WHERE id = ?", (operation_id,)).fetchone()
        return _test_result(row)

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
            _recover_expired_paused_claims(conn, now)
            row = conn.execute(
                f"""
                SELECT d.*, r.request_json, r.request_id FROM notification_deliveries d
                JOIN notification_requests r ON r.event_id = d.event_id
                WHERE ((d.status IN ('pending', 'failed') AND d.next_attempt_at <= ?)
                    OR (d.status = 'delivering' AND d.lease_until <= ?))
                  AND {_CURRENT_REVISION_ELIGIBLE}
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
                       WHERE d.destination = ? AND d.is_test = 0
                         AND d.status = 'sent' AND d.updated_at > ?
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
                    f"""SELECT d.*, r.request_json, r.request_id FROM notification_deliveries d
                       JOIN notification_requests r ON r.event_id = d.event_id
                       WHERE d.destination = ? AND d.noise_result = 'digest' AND d.next_attempt_at = ?
                         AND {_CURRENT_REVISION_ELIGIBLE}
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
        delivery.update({
            "destination_revision": row["destination_revision"],
            "is_test": bool(row["is_test"]),
        })
        if len(rows) > 1:
            items = [_delivery_payload(str(item["destination"]), item["event_id"], request, presentation, self._console_base_url) for item, request, presentation in zip(rows, request_payloads, presentations)]
            delivery = {
                "destination": str(row["destination"]),
                "event_id": row["event_id"],
                "destination_revision": row["destination_revision"],
                "is_test": False,
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
        provider_identity: object = None
        try:
            response = sender(delivery)
            provider_identity = response.get("message_id")
            sent = (
                response.get("ok") is True
                and isinstance(provider_identity, str)
                and 0 < len(provider_identity.strip()) <= 300
            )
            if response.get("ok") is True and not sent:
                response = {**response, "retryable": False, "reason_code": "invalid_response"}
            message = None if sent else str(response.get("error") or "fake destination failed")[:500]
        except Exception as exc:
            sent = False
            response = {}
            message = f"{type(exc).__name__}: {exc}"[:500]
        completed_at = self._clock()
        if response.get("paused") and response.get("reason_code") == "configuration_changed":
            with self._connect() as conn:
                _rollback_claims_for_configuration_change(
                    conn, rows, attempt=attempt, lease_id=lease_id, now=completed_at,
                )
            return True
        retryable = bool(response.get("retryable", True))
        reason_code = None if sent else _bounded_delivery_reason(response.get("reason_code"), retryable)
        if not sent:
            message = _safe_delivery_error(reason_code)
        status = "sent" if sent else "dead_letter" if not retryable or attempt >= self._max_attempts else "failed"
        retry_after = response.get("retry_after")
        backoff = min(300.0, self._retry_base_seconds * (2 ** (attempt - 1)))
        retry_delay = min(300.0, max(0.0, float(retry_after))) if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and math.isfinite(retry_after) else backoff
        next_attempt_at = completed_at + retry_delay if status == "failed" else None
        with self._connect() as conn:
            for item in rows:
                updated = conn.execute(
                    f"""UPDATE notification_deliveries AS d
                        SET status = ?, next_attempt_at = ?, lease_id = NULL, lease_until = NULL,
                            last_error = ?, last_reason_code = ?, message_id = ?, updated_at = ?
                        WHERE id = ? AND status = 'delivering' AND lease_id = ?
                          AND {_CURRENT_REVISION_ELIGIBLE}""",
                    (status, next_attempt_at, message, reason_code, provider_identity.strip() if sent else None, completed_at, item["id"], lease_id),
                )
                if updated.rowcount:
                    _record_destination_outcome(conn, item, sent=sent, status=status, reason_code=reason_code, now=completed_at)
                    conn.execute(
                        """UPDATE notification_delivery_attempts
                           SET outcome = ?, retryable = ?, error = ?, completed_at = ?
                           WHERE id = ?""",
                        (status, int(retryable), message, completed_at,
                         f"{item['id']}:{item['redelivery_count']}:{attempt}"),
                    )
                    _log(
                        "delivery_attempted", correlation_id=item["event_id"], destination_id=item["destination"],
                        request_id=item["request_id"], outcome=status, attempt=attempt, retryable=retryable,
                    )
                else:
                    stale = conn.execute(
                        """SELECT * FROM notification_deliveries
                           WHERE id = ? AND status = 'delivering' AND lease_id = ?
                             AND (paused_reason = 'configuration_changed' OR NOT EXISTS (
                               SELECT 1 FROM notification_destinations destination
                               WHERE destination.id = notification_deliveries.destination
                                 AND destination.revision = notification_deliveries.destination_revision
                             ))""",
                        (item["id"], lease_id),
                    ).fetchone()
                    if stale is not None:
                        _rollback_claims_for_configuration_change(
                            conn, [stale], attempt=attempt, lease_id=lease_id, now=completed_at,
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
                """SELECT d.*, r.request_json, r.request_id FROM notification_deliveries d
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
                """SELECT d.*, r.request_json, r.request_id FROM notification_deliveries d
                   JOIN notification_requests r ON r.event_id = d.event_id WHERE d.id = ?""",
                (delivery_id,),
            ).fetchone()
            attempts = _attempt_history(conn, [delivery_id])
        return _delivery_result(row, attempts.get(delivery_id, []))

    def cleanup_expired(self) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""SELECT r.event_id {_CLEANUP_ELIGIBLE_GROUP}
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
            conn.execute(
                f"""DELETE FROM notification_destination_operations
                    WHERE action = 'test' AND operation_id IN ({placeholders})""",
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
                f"SELECT COUNT(*) FROM (SELECT r.event_id {_CLEANUP_ELIGIBLE_GROUP})",
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
        "request_id": row["request_id"],
        "request": request,
        "provider_identity": row["message_id"],
        "destination_id": str(row["destination"]),
        "severity": str(request["severity"]),
        "summary": str(request["summary"]),
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "redelivery_count": int(row["redelivery_count"]),
        "last_error": row["last_error"],
        "last_reason_code": row["last_reason_code"],
        "attempts": attempts,
        "noise_result": str(row["noise_result"]),
        "noise_reason": row["noise_reason"],
        "next_attempt_at": row["next_attempt_at"],
        "updated_at": float(row["updated_at"]),
        "destination_revision": row["destination_revision"],
        "is_test": bool(row["is_test"]),
        "paused_reason": row["paused_reason"],
    }


def _test_result(row: sqlite3.Row | None) -> JSON:
    if row is None:
        raise NotificationRequestError("test delivery not found")
    status = str(row["status"])
    state = "verified" if status == "sent" else "failed" if status == "dead_letter" else "verifying"
    return {
        "operation_id": str(row["id"]),
        "delivery_id": str(row["id"]),
        "revision": str(row["destination_revision"]),
        "state": state,
    }


def _bounded_delivery_reason(value: object, retryable: bool) -> str:
    allowed = {
        "authentication_failed", "rate_limited", "timeout", "provider_unavailable",
        "provider_rejected", "invalid_response",
    }
    return str(value) if value in allowed else "provider_unavailable" if retryable else "provider_rejected"


def _safe_delivery_error(reason_code: str) -> str:
    return {
        "authentication_failed": "Notification provider authentication failed",
        "rate_limited": "Notification provider rate limited the delivery",
        "timeout": "Notification provider timed out",
        "provider_unavailable": "Notification provider is unavailable",
        "provider_rejected": "Notification provider rejected the delivery",
        "invalid_response": "Notification provider returned an invalid response",
    }[reason_code]


def _rollback_claims_for_configuration_change(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    attempt: int,
    lease_id: str,
    now: float,
) -> None:
    for row in rows:
        updated = conn.execute(
            """UPDATE notification_deliveries
               SET status = 'pending', attempt_count = MAX(0, attempt_count - 1),
                   next_attempt_at = ?, lease_id = NULL, lease_until = NULL,
                   last_error = NULL, last_reason_code = NULL,
                   paused_reason = 'configuration_changed', updated_at = ?
               WHERE id = ? AND status = 'delivering' AND lease_id = ?""",
            (now, now, row["id"], lease_id),
        )
        if updated.rowcount:
            conn.execute(
                "DELETE FROM notification_delivery_attempts WHERE id = ?",
                (f"{row['id']}:{row['redelivery_count']}:{attempt}",),
            )


def _recover_expired_paused_claims(conn: sqlite3.Connection, now: float) -> None:
    rows = conn.execute(
        """SELECT * FROM notification_deliveries
           WHERE status = 'delivering' AND paused_reason = 'configuration_changed'
             AND lease_until <= ?""",
        (now,),
    ).fetchall()
    for row in rows:
        _rollback_claims_for_configuration_change(
            conn,
            [row],
            attempt=int(row["attempt_count"]),
            lease_id=str(row["lease_id"]),
            now=now,
        )


def _record_destination_outcome(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    sent: bool,
    status: str,
    reason_code: str | None,
    now: float,
) -> None:
    destination_id = str(row["destination"])
    revision = row["destination_revision"]
    if destination_id == "builtin-fake" or revision is None:
        return
    current = conn.execute(
        "SELECT revision FROM notification_destinations WHERE id = ?", (destination_id,)
    ).fetchone()
    if current is None or current["revision"] != revision:
        return
    state = "available" if sent else "degraded" if reason_code in {
        "rate_limited", "timeout", "provider_unavailable",
    } else "unavailable"
    if reason_code == "invalid_response" and not row["is_test"]:
        return
    conn.execute(
        """INSERT INTO notification_destination_availability VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(destination_id) DO UPDATE SET
             revision = excluded.revision, state = excluded.state,
             observed_at = excluded.observed_at, reason_code = excluded.reason_code""",
        (destination_id, revision, state, now, reason_code),
    )
    if sent and row["is_test"]:
        paused_claims = conn.execute(
            """SELECT * FROM notification_deliveries
               WHERE destination = ? AND id != ? AND status = 'delivering'
                 AND paused_reason = 'configuration_changed' AND is_test = 0""",
            (destination_id, row["id"]),
        ).fetchall()
        for claim in paused_claims:
            _rollback_claims_for_configuration_change(
                conn,
                [claim],
                attempt=int(claim["attempt_count"]),
                lease_id=str(claim["lease_id"]),
                now=now,
            )
        conn.execute(
            """UPDATE notification_deliveries
               SET paused_reason = NULL, destination_revision = ?, next_attempt_at = COALESCE(next_attempt_at, ?), updated_at = ?
               WHERE destination = ? AND id != ? AND status IN ('pending', 'failed') AND is_test = 0""",
            (revision, now, now, destination_id, row["id"]),
        )
    elif status == "dead_letter" and reason_code in {"authentication_failed", "provider_rejected"}:
        conn.execute(
            """UPDATE notification_deliveries SET paused_reason = ?, updated_at = ?
               WHERE destination = ? AND id != ? AND status IN ('pending', 'failed')""",
            (reason_code, now, destination_id, row["id"]),
        )


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
            "id": str(row["id"]),
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
