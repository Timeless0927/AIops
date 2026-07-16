"""Incident Recovery Observation lifecycle and projection."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .evidence_decisions import stale_incident_actions
from .notification_requests import enqueue_incident_event


def start_recovery_if_ready(
    conn: sqlite3.Connection,
    incident_id: str,
    now: float,
    *,
    stabilization_seconds: float,
    id_factory: Callable[[str], str],
    resolved_webhook_request_id: object,
) -> None:
    incident = conn.execute(
        "SELECT evidence_revision FROM incidents WHERE id = ? AND status = 'active'",
        (incident_id,),
    ).fetchone()
    if incident is None or resolved_webhook_request_id is None:
        return
    incomplete = conn.execute(
        """
        SELECT 1
        FROM alert_signals
        WHERE incident_id = ?
          AND (status != 'recovered' OR recovered_webhook_request_id IS NULL)
        LIMIT 1
        """,
        (incident_id,),
    ).fetchone()
    if incomplete is not None:
        return
    revision = int(incident["evidence_revision"]) + 1
    conn.execute(
        "INSERT INTO recovery_observations (id, incident_id, evidence_revision, observed_at, stabilizes_at, resolved_webhook_request_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            id_factory("recovery"), incident_id, revision, now,
            now + stabilization_seconds, resolved_webhook_request_id,
        ),
    )
    conn.execute(
        "UPDATE incidents SET lifecycle_state = 'stabilizing', evidence_revision = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
        (revision, now, incident_id),
    )
    stale_incident_actions(conn, incident_id)
    resolve_due_recoveries(conn, now)


def cancel_recovery(conn: sqlite3.Connection, incident_id: str, now: float) -> None:
    changed = conn.execute(
        "UPDATE recovery_observations SET cancelled_at = ? WHERE incident_id = ? AND cancelled_at IS NULL AND resolved_at IS NULL",
        (now, incident_id),
    ).rowcount
    if changed:
        incident = conn.execute(
            "SELECT reopened_at FROM incidents WHERE id = ?", (incident_id,),
        ).fetchone()
        state = "reopened" if incident["reopened_at"] is not None else "firing"
        conn.execute(
            "UPDATE incidents SET lifecycle_state = ?, evidence_revision = evidence_revision + 1, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (state, now, incident_id),
        )
        stale_incident_actions(conn, incident_id)


def resolve_due_recoveries(conn: sqlite3.Connection, now: float) -> int:
    due = conn.execute(
        """
        SELECT ro.id, ro.incident_id, ro.observed_at, ro.stabilizes_at,
               ro.resolved_webhook_request_id
        FROM recovery_observations ro JOIN incidents i ON i.id = ro.incident_id
        WHERE i.status = 'active' AND ro.cancelled_at IS NULL AND ro.resolved_at IS NULL
          AND ro.stabilizes_at <= ?
          AND ro.resolved_webhook_request_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM alert_signals a WHERE a.incident_id = ro.incident_id)
          AND NOT EXISTS (
              SELECT 1 FROM alert_signals a
              WHERE a.incident_id = ro.incident_id
                AND (a.status != 'recovered' OR a.recovered_webhook_request_id IS NULL)
          )
        """,
        (now,),
    ).fetchall()
    resolved = 0
    for observation in due:
        if _incident_has_blocking_change(conn, str(observation["incident_id"])):
            continue
        resolved_at = float(observation["stabilizes_at"])
        conn.execute(
            "UPDATE recovery_observations SET resolved_at = ? WHERE id = ?",
            (resolved_at, observation["id"]),
        )
        conn.execute(
            "UPDATE incidents SET status = 'resolved', lifecycle_state = 'resolved', resolved_at = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (resolved_at, resolved_at, observation["incident_id"]),
        )
        enqueue_incident_event(
            conn,
            event_type="incident.resolved",
            incident_id=str(observation["incident_id"]),
            now=resolved_at,
            recovery_observation_id=str(observation["id"]),
            resolved_webhook_request_id=str(observation["resolved_webhook_request_id"]),
            recovery_observed_at=float(observation["observed_at"]),
            stabilizes_at=float(observation["stabilizes_at"]),
            resolved_at=resolved_at,
        )
        resolved += 1
    return resolved


def project_recovery(row: sqlite3.Row) -> dict[str, object]:
    status = (
        "cancelled" if row["cancelled_at"] is not None
        else "resolved" if row["resolved_at"] is not None
        else "stabilizing"
    )
    return {
        "id": str(row["id"]),
        "evidence_revision": int(row["evidence_revision"]),
        "status": status,
        "observed_at": float(row["observed_at"]),
        "stabilizes_at": float(row["stabilizes_at"]),
        "cancelled_at": float(row["cancelled_at"]) if row["cancelled_at"] is not None else None,
        "resolved_at": float(row["resolved_at"]) if row["resolved_at"] is not None else None,
        "resolved_webhook_request_id": row["resolved_webhook_request_id"],
    }


def _incident_has_blocking_change(conn: sqlite3.Connection, incident_id: str) -> bool:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kubernetes_change_executions'"
    ).fetchone() is None:
        return False
    return conn.execute(
        """
        SELECT 1
        FROM kubernetes_change_executions execution
        JOIN change_requests request ON request.id = execution.change_request_id
        WHERE request.incident_id = ?
          AND execution.status IN (
              'queued', 'dispatched', 'started', 'unknown_outcome',
              'cancel_requested', 'rolling_back'
          )
        LIMIT 1
        """,
        (incident_id,),
    ).fetchone() is not None
