"""Gateway-owned durable Notification Request outbox and Engine handoff."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Any
from aiops.contracts.notification import notification_request
from apps.service_http import record_sqlite_error

from .gateway_db import GatewayDatabase, register_migrations


JSON = dict[str, object]
Sender = Callable[[JSON], tuple[int, JSON]]
logger = logging.getLogger(__name__)
_SCHEMA_VERSION = 15
_SCHEMA = """
CREATE TABLE notification_requests (
    event_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    accepted_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX notification_requests_handoff
ON notification_requests(status, next_attempt_at, created_at);

CREATE TABLE notification_connector_presence (
    cluster_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    observed_status TEXT NOT NULL CHECK (observed_status IN ('online', 'offline')),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    updated_at REAL NOT NULL
);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


class NotificationOutbox:
    def __init__(
        self,
        database: GatewayDatabase,
        *,
        clock: Callable[[], float] = time.time,
        retry_seconds: float = 5.0,
    ) -> None:
        self._database = database
        self._clock = clock
        self._retry_seconds = max(0.0, retry_seconds)
        with self._database.connect():
            pass

    def enqueue_in(self, conn: sqlite3.Connection, payload: JSON) -> bool:
        return enqueue_notification_in(conn, payload, now=self._clock())

    def list_requests(self) -> list[JSON]:
        with self._database.connect() as conn:
            rows = conn.execute("SELECT * FROM notification_requests ORDER BY created_at, event_id").fetchall()
        return [_projection(row) for row in rows]

    def reconcile_connector_presence(self) -> int:
        now = self._clock()
        changed = 0
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT cluster_id, connector_id, environment, runtime_status, last_heartbeat FROM clusters"
            ).fetchall()
            for row in rows:
                status = "offline" if now - float(row["last_heartbeat"]) > 120 else "online"
                previous = conn.execute(
                    "SELECT observed_status, sequence FROM notification_connector_presence WHERE cluster_id = ?",
                    (row["cluster_id"],),
                ).fetchone()
                if previous is None:
                    conn.execute(
                        "INSERT INTO notification_connector_presence VALUES (?, ?, ?, 1, ?)",
                        (row["cluster_id"], row["connector_id"], status, now),
                    )
                    if status == "online":
                        continue
                    sequence = 1
                else:
                    if previous["observed_status"] == status:
                        continue
                    sequence = int(previous["sequence"]) + 1
                    conn.execute(
                        "UPDATE notification_connector_presence SET observed_status = ?, sequence = ?, updated_at = ? WHERE cluster_id = ?",
                        (status, sequence, now, row["cluster_id"]),
                    )
                event_type = f"connector.{status if status == 'offline' else 'recovered'}"
                enqueue_notification_in(
                    conn,
                    {
                        "event_id": f"{event_type}:{row['cluster_id']}:{sequence}",
                        "event_type": event_type,
                        "occurred_at": now,
                        "severity": "critical" if status == "offline" else "info",
                        "subject": {
                            "type": "connector",
                            "id": str(row["connector_id"]),
                            "version": sequence,
                        },
                        "scope": {"environment": str(row["environment"]), "cluster_id": str(row["cluster_id"])},
                        "summary": f"Connector {row['connector_id']} is {status}",
                        "facts": {
                            "connector_id": str(row["connector_id"]),
                            "cluster_id": str(row["cluster_id"]),
                            "status": event_type.rsplit(".", 1)[-1],
                        },
                        "console_path": "/admin/clusters",
                    },
                    now=now,
                )
                changed += 1
            conn.commit()
        return changed

    def run_handoff_once(self, sender: Sender) -> bool:
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM notification_requests WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY created_at, event_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return False
            attempt = int(row["attempt_count"]) + 1
            conn.execute(
                "UPDATE notification_requests SET attempt_count = ?, next_attempt_at = ?, updated_at = ? WHERE event_id = ?",
                (attempt, now + self._retry_seconds * (2 ** min(attempt - 1, 6)), now, row["event_id"]),
            )
            conn.commit()
        payload = json.loads(str(row["request_json"]))
        try:
            status, response = sender(payload)
            accepted = status == HTTPStatus.ACCEPTED and response.get("status") == "accepted" and response.get("event_id") == row["event_id"]
            message = None if accepted else str(response.get("error") or response.get("status") or f"HTTP {status}")
        except Exception as exc:
            accepted = False
            message = f"{type(exc).__name__}: {exc}"
        with self._database.connect() as conn:
            conn.execute(
                "UPDATE notification_requests SET status = ?, accepted_at = ?, last_error = ?, updated_at = ? WHERE event_id = ? AND status = 'pending'",
                ("accepted" if accepted else "pending", now if accepted else None, message, now, row["event_id"]),
            )
        return True

def start_notification_handoff(
    outbox: NotificationOutbox,
    *,
    sender: Sender,
    interval_seconds: float = 1.0,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    stop = stop_event or threading.Event()

    def work() -> None:
        while not stop.is_set():
            try:
                outbox.reconcile_connector_presence()
                worked = outbox.run_handoff_once(sender)
            except Exception as exc:
                if isinstance(exc, sqlite3.Error):
                    record_sqlite_error("aiops-k8s-gateway")
                logger.exception("Notification Request handoff iteration failed")
                worked = False
            if not worked:
                stop.wait(interval_seconds)

    worker = threading.Thread(target=work, name="notification-handoff-worker", daemon=True)
    worker.start()
    return worker


def enqueue_notification_in(conn: sqlite3.Connection, payload: JSON, *, now: float) -> bool:
    normalized = notification_request(**payload)
    encoded = _json(normalized)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    existing = conn.execute(
        "SELECT content_hash FROM notification_requests WHERE event_id = ?", (normalized["event_id"],)
    ).fetchone()
    if existing is not None:
        if str(existing["content_hash"]) != digest:
            raise ValueError("Notification Request event_id conflict")
        return False
    conn.execute(
        "INSERT INTO notification_requests VALUES (?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)",
        (normalized["event_id"], digest, encoded, now, now, now),
    )
    return True


def enqueue_incident_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    incident_id: str,
    now: float,
    previous_severity: str | None = None,
) -> bool:
    row = _incident_row(conn, incident_id)
    facts: JSON = {"incident_id": incident_id, "status": event_type.rsplit(".", 1)[-1]}
    if previous_severity is not None:
        facts.update(previous_severity=previous_severity, severity=str(row["severity"]))
    return _enqueue_for_incident(
        conn,
        row=row,
        event_id=f"{event_type}:{incident_id}:{row['revision']}",
        event_type=event_type,
        subject={"type": "incident", "id": incident_id, "version": int(row["revision"])},
        severity=_severity(str(row["severity"])),
        summary=f"{row['title']}: {event_type.rsplit('.', 1)[-1].replace('_', ' ')}",
        facts=facts,
        console_path=f"/incidents/{incident_id}",
        now=now,
    )


def enqueue_investigation_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    investigation_id: str,
    now: float,
    reason: str,
) -> bool:
    row = conn.execute(
        """
        SELECT inc.*, i.sequence AS investigation_sequence
        FROM investigations i JOIN incidents inc ON inc.id = i.incident_id
        WHERE i.id = ?
        """,
        (investigation_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Investigation was not found for notification")
    return _enqueue_for_incident(
        conn,
        row=row,
        event_id=f"{event_type}:{investigation_id}",
        event_type=event_type,
        subject={"type": "investigation", "id": investigation_id, "version": int(row["investigation_sequence"])},
        severity="error" if event_type == "investigation.failed" else "warning",
        summary=f"Investigation {event_type.rsplit('.', 1)[-1].replace('_', ' ')}",
        facts={
            "incident_id": str(row["id"]),
            "investigation_id": investigation_id,
            "status": event_type.rsplit(".", 1)[-1],
            "reason": reason,
        },
        console_path=f"/incidents/{row['id']}",
        now=now,
    )


def enqueue_approval_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    action_id: str,
    investigation_id: str,
    now: float,
    approval_id: str | None = None,
) -> bool:
    row = conn.execute(
        """
        SELECT inc.*, ra.version AS action_version, ra.summary AS action_summary
        FROM recommended_actions ra
        JOIN investigations i ON i.id = ra.investigation_id
        JOIN incidents inc ON inc.id = i.incident_id
        WHERE ra.id = ? AND ra.investigation_id = ?
        ORDER BY ra.version DESC LIMIT 1
        """,
        (action_id, investigation_id),
    ).fetchone()
    if row is None:
        raise ValueError("Recommended Action was not found for notification")
    subject_type = "approval" if approval_id else "recommended_action"
    subject_id = approval_id or action_id
    facts: JSON = {
        "incident_id": str(row["id"]),
        "investigation_id": investigation_id,
        "action_id": action_id,
        "status": event_type.rsplit(".", 1)[-1],
    }
    if approval_id:
        facts["approval_id"] = approval_id
    return _enqueue_for_incident(
        conn,
        row=row,
        event_id=f"{event_type}:{subject_id}:{row['action_version']}",
        event_type=event_type,
        subject={"type": subject_type, "id": subject_id, "version": int(row["action_version"])},
        severity="warning" if event_type == "approval.required" else "info",
        summary=str(row["action_summary"]),
        facts=facts,
        console_path=f"/incidents/{row['id']}",
        now=now,
    )


def enqueue_execution_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    command_id: str,
    now: float,
    error_code: str | None = None,
) -> bool:
    row = conn.execute(
        """
        SELECT inc.*, c.action, c.attempt_count
        FROM connector_commands c
        JOIN execution_grants eg ON eg.id = c.execution_grant_id
        JOIN approvals a ON a.id = eg.approval_id
        JOIN incidents inc ON inc.id = a.incident_id
        WHERE c.id = ?
        """,
        (command_id,),
    ).fetchone()
    if row is None:
        return False
    facts: JSON = {
        "incident_id": str(row["id"]),
        "command_id": command_id,
        "action": str(row["action"]),
        "status": event_type.rsplit(".", 1)[-1],
    }
    if error_code:
        facts["error_code"] = error_code
    return _enqueue_for_incident(
        conn,
        row=row,
        event_id=f"{event_type}:{command_id}",
        event_type=event_type,
        subject={"type": "connector_command", "id": command_id, "version": max(1, int(row["attempt_count"]))},
        severity="info" if event_type == "execution.succeeded" else "critical" if event_type == "execution.outcome_unknown" else "error",
        summary=f"{row['action']}: {event_type.rsplit('.', 1)[-1].replace('_', ' ')}",
        facts=facts,
        console_path=f"/incidents/{row['id']}",
        now=now,
    )


def _enqueue_for_incident(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    event_id: str,
    event_type: str,
    subject: JSON,
    severity: str,
    summary: str,
    facts: JSON,
    console_path: str,
    now: float,
) -> bool:
    cluster = conn.execute("SELECT environment FROM clusters WHERE cluster_id = ?", (row["cluster_id"],)).fetchone()
    scope = {
        key: value
        for key, value in {
            "environment": cluster["environment"] if cluster else None,
            "team_id": row["team_id"],
            "service_id": row["service_id"],
            "cluster_id": row["cluster_id"],
            "namespace": row["namespace"],
            "resource_type": row["workload_kind"],
            "resource_id": row["workload_name"],
        }.items()
        if value is not None and str(value).strip()
    }
    return enqueue_notification_in(
        conn,
        {
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": now,
            "severity": severity,
            "subject": subject,
            "scope": scope,
            "summary": summary,
            "facts": facts,
            "console_path": console_path,
        },
        now=now,
    )


def _incident_row(conn: sqlite3.Connection, incident_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        raise ValueError("Incident was not found for notification")
    return row


def _severity(value: str) -> str:
    return {"critical": "critical", "high": "error", "medium": "warning", "low": "info"}.get(value, "warning")


def _projection(row: sqlite3.Row) -> JSON:
    return {
        **json.loads(str(row["request_json"])),
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "last_error": row["last_error"],
        "accepted_at": row["accepted_at"],
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
