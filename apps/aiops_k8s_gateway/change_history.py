"""Frozen governance history projection owned by Generic Change."""

from __future__ import annotations

import json
import sqlite3


JSON = dict[str, object]


def snapshot_change_history(
    conn: sqlite3.Connection,
    incident_id: str,
) -> list[JSON]:
    """Project the complete Generic Change history for an Incident Report."""

    if not _table_exists(conn, "change_requests"):
        return []
    requests = conn.execute(
        "SELECT * FROM change_requests WHERE incident_id = ? ORDER BY created_at, id",
        (incident_id,),
    ).fetchall()
    history = []
    for request in requests:
        request_id = str(request["id"])
        phases = conn.execute(
            "SELECT * FROM change_plan_phases WHERE change_request_id = ? ORDER BY sequence, id",
            (request_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM change_request_events WHERE change_request_id = ? ORDER BY event_id",
            (request_id,),
        ).fetchall()
        history.append({
            "id": request_id,
            "submitted_by": str(request["actor_id"]),
            "desired_outcome": str(request["desired_outcome"]),
            "context": str(request["context"]),
            "created_at": float(request["created_at"]),
            "updated_at": float(request["updated_at"]),
            "phases": [_phase_history(conn, phase) for phase in phases],
            "events": [{
                "id": int(event["event_id"]),
                "type": str(event["type"]),
                "actor_id": event["actor_id"],
                "payload": json.loads(str(event["payload_json"])),
                "created_at": float(event["created_at"]),
            } for event in events],
        })
    return history


def _phase_history(conn: sqlite3.Connection, phase: sqlite3.Row) -> JSON:
    phase_id = str(phase["id"])
    revisions = conn.execute(
        "SELECT * FROM change_plan_revisions WHERE phase_id = ? ORDER BY revision, id",
        (phase_id,),
    ).fetchall()
    approval = conn.execute(
        "SELECT * FROM kubernetes_phase_approvals WHERE phase_id = ?", (phase_id,),
    ).fetchone()
    execution = conn.execute(
        "SELECT * FROM kubernetes_change_executions WHERE phase_id = ?", (phase_id,),
    ).fetchone()
    status = next((
        str(phase[field]) for field in (
            "availability_status", "reconciliation_status", "orchestration_status",
            "execution_status", "approval_status", "status",
        ) if phase[field] is not None
    ), str(phase["status"]))
    return {
        "id": phase_id,
        "sequence": int(phase["sequence"]),
        "status": status,
        "created_at": float(phase["created_at"]),
        "updated_at": float(phase["updated_at"]),
        "revisions": [{
            "id": str(revision["id"]),
            "number": int(revision["revision"]),
            "status": str(revision["status"]),
            "question": revision["question"],
            "plan": json.loads(str(revision["plan_json"])) if revision["plan_json"] else None,
            "created_at": float(revision["created_at"]),
            "superseded_at": float(revision["superseded_at"])
            if revision["superseded_at"] else None,
        } for revision in revisions],
        "approval": _phase_approval(approval) if approval is not None else None,
        "execution": _phase_execution(conn, execution) if execution is not None else None,
    }


def _phase_approval(row: sqlite3.Row) -> JSON:
    return {
        "id": str(row["id"]),
        "revision_id": str(row["revision_id"]),
        "approver_id": str(row["approver_id"]),
        "authority_ids": json.loads(str(row["authority_ids_json"])),
        "reason": str(row["reason"]),
        "request_id": str(row["request_id"]),
        "rollback_policy": str(row["rollback_policy"]),
        "target_confirmations": json.loads(str(row["target_confirmations_json"])),
        "frozen_changes": json.loads(str(row["frozen_changes_json"])),
        "dry_run_expires_at": float(row["dry_run_expires_at"]),
        "approved_at": float(row["approved_at"]),
        "start_expires_at": float(row["start_expires_at"]),
    }


def _phase_execution(conn: sqlite3.Connection, row: sqlite3.Row) -> JSON:
    execution_id = str(row["id"])
    steps = conn.execute(
        "SELECT * FROM kubernetes_change_execution_steps "
        "WHERE execution_id = ? ORDER BY created_at, direction, ordinal, id",
        (execution_id,),
    ).fetchall()
    reconciliations = conn.execute(
        "SELECT * FROM kubernetes_change_reconciliations "
        "WHERE execution_id = ? ORDER BY created_at, id",
        (execution_id,),
    ).fetchall()
    return {
        "id": execution_id,
        "actor_id": str(row["actor_id"]),
        "reason": str(row["reason"]),
        "request_id": str(row["request_id"]),
        "rollback_policy": str(row["rollback_policy"]),
        "status": str(row["status"]),
        "result": _result_summary(row["result_json"]),
        "created_at": float(row["created_at"]),
        "started_at": float(row["started_at"]) if row["started_at"] else None,
        "completed_at": float(row["completed_at"]) if row["completed_at"] else None,
        "steps": [{
            "id": str(step["id"]),
            "ordinal": int(step["ordinal"]),
            "direction": str(step["direction"]),
            "source_step_id": step["source_step_id"],
            "command_id": str(step["command_id"]),
            "change_hash": str(step["change_hash"]),
            "change": json.loads(str(step["change_json"])),
            "inverse_change": json.loads(str(step["inverse_change_json"]))
            if step["inverse_change_json"] else None,
            "status": str(step["status"]),
            "result": _result_summary(step["result_json"]),
            "started_at": float(step["started_at"]) if step["started_at"] else None,
            "completed_at": float(step["completed_at"]) if step["completed_at"] else None,
        } for step in steps],
        "reconciliations": [{
            "id": str(item["id"]),
            "step_id": str(item["step_id"]),
            "mutation_command_id": str(item["mutation_command_id"]),
            "observation_command_id": str(item["observation_command_id"]),
            "classification": str(item["classification"]),
            "state": str(item["state"]),
            "evidence": json.loads(str(item["evidence_json"])),
            "evidence_sha256": str(item["evidence_sha256"]),
            "observed_at": float(item["observed_at"]) if item["observed_at"] else None,
            "accepted_by": item["accepted_by"],
            "acceptance_reason": item["acceptance_reason"],
            "accepted_at": float(item["accepted_at"]) if item["accepted_at"] else None,
        } for item in reconciliations],
    }


def _result_summary(value: object) -> JSON | None:
    if value is None:
        return None
    result = json.loads(str(value))
    return {
        field: result.get(field)
        for field in ("status", "outcome", "error_code", "error_message")
        if field in result
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
    ).fetchone() is not None
