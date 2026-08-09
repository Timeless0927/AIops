"""Transport timeout classification for Kubernetes Change execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .connector_commands import ConnectorCommands
from .gateway_audit import insert_admin_audit
from .gateway_db import GatewayDatabase


def reconcile_transport_failures(
    database: GatewayDatabase,
    commands: ConnectorCommands,
    record_result_in: Callable[[Any, str, dict[str, object], float], None],
    *,
    now: float,
    request_id: str,
) -> None:
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT execution.*, step.id AS step_id, step.command_id,
                   step.status AS step_status
            FROM kubernetes_change_executions execution
            JOIN kubernetes_change_execution_steps step ON step.execution_id = execution.id
            WHERE step.status IN ('dispatched', 'started') AND step.command_id IS NOT NULL
            """,
        ).fetchall()
        by_command = {str(row["command_id"]): row for row in rows}
        failures = commands.claim_transport_failures_in(
            conn,
            [
                (command_id, str(row["step_status"]))
                for command_id, row in by_command.items()
            ],
            now=now,
        )
        if not failures:
            conn.rollback()
            return
        for command_id, code in failures:
            row = by_command[command_id]
            unknown = code == "execution_outcome_unknown"
            result = {
                "status": "failed", "stdout": "", "stderr": "", "exit_code": None,
                "truncated": False, "error_code": code, "error_message": code,
            }
            record_result_in(conn, command_id, result, now)
            outcome = "unknown_outcome" if unknown else "failed"
            insert_admin_audit(
                conn, actor_id=None, target_type="kubernetes_change_executions",
                target_id=str(row["id"]), action="kubernetes_change_execution_reconcile",
                reason="Connector transport did not produce a trustworthy terminal outcome",
                before={"status": row["step_status"]},
                after={"status": outcome, "error_code": code},
                result=code, request_id=request_id,
            )
        conn.commit()
