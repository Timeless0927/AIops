"""Read projection for one persisted Change Request aggregate."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .kubernetes_change_validation import KubernetesChangeValidation


class ChangeRequestProjectionError(ValueError):
    pass


def project_change_request_in(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    validation: KubernetesChangeValidation | None,
) -> dict[str, object]:
    phase = conn.execute(
        "SELECT * FROM change_plan_phases WHERE change_request_id = ? ORDER BY sequence DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    if phase is None:
        raise ChangeRequestProjectionError("Change Request has no active Phase")
    revisions = conn.execute(
        "SELECT * FROM change_plan_revisions WHERE change_request_id = ? ORDER BY revision",
        (row["id"],),
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM change_request_events WHERE change_request_id = ? ORDER BY event_id",
        (row["id"],),
    ).fetchall()
    projected_revisions = [_revision(conn, item, validation) for item in revisions]
    active = next(
        (item for item in reversed(projected_revisions) if item["status"] != "superseded"),
        None,
    )
    phase_status = str(
        phase["availability_status"] or phase["orchestration_status"]
        or phase["execution_status"] or phase["approval_status"] or phase["status"]
    )
    return {
        "id": str(row["id"]),
        "incident_id": str(row["incident_id"]),
        "submitted_by": str(row["actor_id"]),
        "desired_outcome": str(row["desired_outcome"]),
        "context": str(row["context"]),
        "status": phase_status,
        "active_phase": {
            "id": str(phase["id"]),
            "sequence": int(phase["sequence"]),
            "status": phase_status,
            "created_at": float(phase["created_at"]),
            "updated_at": float(phase["updated_at"]),
        },
        "active_revision": active,
        "revisions": projected_revisions,
        "events": [
            {
                "id": int(event["event_id"]),
                "type": str(event["type"]),
                "actor_id": event["actor_id"],
                "payload": json.loads(str(event["payload_json"])),
                "created_at": float(event["created_at"]),
            }
            for event in events
        ],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _revision(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    validation: KubernetesChangeValidation | None,
) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "number": int(row["revision"]),
        "status": str(row["status"]),
        "question": row["question"],
        "plan": json.loads(str(row["plan_json"])) if row["plan_json"] is not None else None,
        "validation": validation.projection_in(conn, str(row["id"])) if validation else None,
        "created_at": float(row["created_at"]),
        "superseded_at": float(row["superseded_at"]) if row["superseded_at"] is not None else None,
    }
