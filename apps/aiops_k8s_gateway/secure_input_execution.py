"""Secure Input availability transitions for Kubernetes plan execution."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .change_plan_phases import ChangePlanPhases
from .connector_commands import ConnectorCommands
from .gateway_audit import insert_admin_audit
from .gateway_db import GatewayDatabase
from .kubernetes_execution_codec import canonical_json
from .secure_inputs import SecureInputs


def schedule_secure_input_cleanup_in(
    conn: sqlite3.Connection,
    inputs: SecureInputs | None,
    *,
    execution_id: str,
    revision_id: str,
    released_at: float,
    delete_after: float,
) -> None:
    if inputs is None:
        return
    inputs.release_revision_in(
        conn, revision_id, released_at=released_at, delete_after=delete_after,
    )
    input_ids = {
        str(item["id"])
        for step in conn.execute(
            "SELECT secure_inputs_json FROM kubernetes_change_execution_steps WHERE execution_id = ?",
            (execution_id,),
        ).fetchall()
        for item in json.loads(str(step["secure_inputs_json"]))
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    inputs.schedule_cleanup_in(conn, sorted(input_ids), delete_after=delete_after)


def mark_secure_input_unavailable(
    database: GatewayDatabase,
    commands: ConnectorCommands,
    phases: ChangePlanPhases,
    inputs: SecureInputs | None,
    row: Any,
    *,
    request_id: str,
    now: float,
) -> None:
    result = {
        "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
        "truncated": False, "error_code": "secure_input_unavailable",
        "error_message": "Secure Input is unavailable",
    }
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = mark_secure_input_unavailable_in(
            conn, commands, phases, inputs, row,
            result=result, request_id=request_id, now=now,
        )
        if not changed:
            conn.rollback()
            return
        conn.commit()


def mark_secure_input_unavailable_in(
    conn: sqlite3.Connection,
    commands: ConnectorCommands,
    phases: ChangePlanPhases,
    inputs: SecureInputs | None,
    row: Any,
    *,
    result: dict[str, object],
    request_id: str,
    now: float,
) -> bool:
    current = conn.execute(
        "SELECT status FROM kubernetes_change_execution_steps WHERE id = ?",
        (row["step_id"],),
    ).fetchone()
    if current is None or current["status"] not in {"pending", "queued", "dispatched", "started"}:
        return False
    command_id = str(
        row["step_command_id"] if "step_command_id" in row.keys() else row["command_id"]
    )
    conn.execute(
        "UPDATE kubernetes_change_execution_steps SET status = 'failed', "
        "result_json = ?, completed_at = ? WHERE id = ?",
        (canonical_json(result), now, row["step_id"]),
    )
    conn.execute(
        "UPDATE kubernetes_change_execution_steps SET status = 'cancelled', completed_at = ? "
        "WHERE execution_id = ? AND status = 'pending'",
        (now, row["id"]),
    )
    conn.execute(
        "UPDATE kubernetes_execution_grants SET revoked_at = ? "
        "WHERE execution_id = ? AND consumed_at IS NULL AND revoked_at IS NULL",
        (now, row["id"]),
    )
    conn.execute(
        "UPDATE kubernetes_change_executions SET status = 'failed', "
        "availability_status = 'secure_input_unavailable', result_json = ?, "
        "completed_at = ? WHERE id = ?",
        (canonical_json(result), now, row["id"]),
    )
    phases.record_secure_input_unavailable_in(
        conn,
        change_request_id=str(row["change_request_id"]),
        phase_id=str(row["phase_id"]),
        execution_id=str(row["id"]),
        command_id=command_id,
        now=now,
    )
    schedule_secure_input_cleanup_in(
        conn,
        inputs,
        execution_id=str(row["id"]),
        revision_id=str(row["revision_id"]),
        released_at=now,
        delete_after=now + int(row["execution_timeout_seconds"]),
    )
    commands.redact_secure_inputs_in(conn, command_id)
    insert_admin_audit(
        conn,
        actor_id=None,
        target_type="kubernetes_change_execution_steps",
        target_id=str(row["step_id"]),
        action="kubernetes_change_execution_secure_input",
        reason="Secure Input key or ciphertext was unavailable before a trustworthy outcome",
        before={"status": current["status"]},
        after={"status": "secure_input_unavailable"},
        result="secure_input_unavailable",
        request_id=request_id,
    )
    return True


def handle_terminal_result_in(
    conn: sqlite3.Connection,
    commands: ConnectorCommands,
    phases: ChangePlanPhases,
    inputs: SecureInputs | None,
    row: Any,
    result: dict[str, object],
    *,
    command_id: str,
    error_code: str | None,
    now: float,
) -> bool:
    commands.redact_secure_inputs_in(conn, command_id)
    if error_code != "secure_input_unavailable":
        return False
    return mark_secure_input_unavailable_in(
        conn,
        commands,
        phases,
        inputs,
        row,
        result=result,
        request_id=f"connector-result:{command_id}",
        now=now,
    )
