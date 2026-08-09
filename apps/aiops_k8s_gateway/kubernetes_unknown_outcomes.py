"""Transport timeout classification for Kubernetes Change execution."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .gateway_audit import insert_admin_audit
from .gateway_db import GatewayDatabase


def reconcile_transport_failures(
    database: GatewayDatabase,
    record_result_in: Callable[[Any, str, dict[str, object], float], None],
    *,
    now: float,
    request_id: str,
) -> None:
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT execution.*, step.id AS step_id, step.command_id,
                   step.status AS step_status,
                   command.status AS command_status
            FROM kubernetes_change_executions execution
            JOIN kubernetes_change_execution_steps step ON step.execution_id = execution.id
            JOIN connector_commands command ON command.id = step.command_id
            WHERE (
                step.status = 'dispatched' AND command.status = 'leased'
                AND command.lease_expires_at <= ?
            ) OR (
                step.status = 'started' AND command.status = 'unknown_outcome'
            )
            """,
            (now,),
        ).fetchall()
        if not rows:
            return
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            unknown = row["step_status"] == "started"
            code = "execution_outcome_unknown" if unknown else "execution_delivery_expired"
            result = {
                "status": "failed", "stdout": "", "stderr": "", "exit_code": None,
                "truncated": False, "error_code": code, "error_message": code,
            }
            record_result_in(conn, str(row["command_id"]), result, now)
            outcome = "unknown_outcome" if unknown else "failed"
            if not unknown:
                conn.execute(
                    "UPDATE connector_commands SET status = 'rejected', result_json = ?, "
                    "result_received_at = ?, updated_at = ? WHERE id = ? AND status = 'leased'",
                    (_json({**result, "status": "rejected"}), now, now, row["command_id"]),
                )
            insert_admin_audit(
                conn, actor_id=None, target_type="kubernetes_change_executions",
                target_id=str(row["id"]), action="kubernetes_change_execution_reconcile",
                reason="Connector transport did not produce a trustworthy terminal outcome",
                before={"status": row["step_status"]},
                after={"status": outcome, "error_code": code},
                result=code, request_id=request_id,
            )
        conn.commit()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
