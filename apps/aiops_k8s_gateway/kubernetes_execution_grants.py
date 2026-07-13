"""Authority-checked grant issuance for the next eligible Kubernetes Plan step."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .gateway_db import GatewayDatabase, insert_admin_audit
from .kubernetes_execution_codec import canonical_digest as _digest
from .kubernetes_phase_approvals import KubernetesPhaseApprovalError


def queue_pending_step(
    database: GatewayDatabase,
    *,
    approvals: Any,
    phases: Any,
    id_factory: Callable[[str], str],
    connector_id: str,
    cluster_id: str,
    request_id: str,
    now: float,
    on_failure: Callable[[Any, str, str, float], None],
) -> None:
    with database.connect() as conn:
        candidate = conn.execute(
            """
            SELECT execution.*, step.id AS step_id, step.command_id, step.change_hash,
                   step.change_json, step.direction, step.ordinal
            FROM kubernetes_change_executions execution
            JOIN kubernetes_change_execution_steps step ON step.execution_id = execution.id
            WHERE execution.connector_id = ? AND execution.cluster_id = ?
              AND execution.status IN ('started', 'rolling_back')
              AND step.status = 'pending'
              AND NOT EXISTS (
                  SELECT 1 FROM kubernetes_change_execution_steps active
                  WHERE active.execution_id = execution.id
                    AND active.status IN ('queued', 'dispatched', 'started')
              )
            ORDER BY execution.created_at, execution.id,
                     CASE step.direction WHEN 'forward' THEN 0 ELSE 1 END, step.ordinal
            LIMIT 1
            """,
            (connector_id, cluster_id),
        ).fetchone()
    if candidate is None:
        return
    try:
        approval = approvals.authorize_start(
            str(candidate["phase_id"]), request_id=request_id, stage="grant",
        )
    except KubernetesPhaseApprovalError as exc:
        on_failure(candidate, exc.code, request_id, now)
        return
    if (
        approval.get("id") != candidate["approval_id"]
        or approval.get("cluster_id") != cluster_id
        or _digest(json.loads(str(candidate["change_json"]))) != candidate["change_hash"]
    ):
        on_failure(candidate, "phase_stale", request_id, now)
        return
    grant_id = id_factory("kubernetes-grant")
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        queued = conn.execute(
            "UPDATE kubernetes_change_execution_steps SET status = 'queued' "
            "WHERE id = ? AND status = 'pending' AND NOT EXISTS ("
            "SELECT 1 FROM kubernetes_change_execution_steps active "
            "WHERE active.execution_id = ? AND active.status IN ('queued', 'dispatched', 'started'))",
            (candidate["step_id"], candidate["id"]),
        )
        if queued.rowcount != 1:
            conn.rollback()
            return
        conn.execute(
            """
            INSERT INTO kubernetes_execution_grants (
                id, execution_id, step_id, phase_id, approval_id, command_id,
                change_hash, issued_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                grant_id, candidate["id"], candidate["step_id"], candidate["phase_id"],
                candidate["approval_id"], candidate["command_id"], candidate["change_hash"],
                now, now + 60,
            ),
        )
        phases.record_step_granted_in(
            conn, change_request_id=str(candidate["change_request_id"]),
            phase_id=str(candidate["phase_id"]), execution_id=str(candidate["id"]),
            step_id=str(candidate["step_id"]), grant_id=grant_id,
            direction=str(candidate["direction"]), ordinal=int(candidate["ordinal"]), now=now,
        )
        insert_admin_audit(
            conn, actor_id=str(candidate["actor_id"]),
            target_type="kubernetes_change_execution_steps",
            target_id=str(candidate["step_id"]), action="kubernetes_change_execution_grant",
            reason=str(candidate["reason"]), before={"status": "pending"},
            after={"status": "queued", "grant_id": grant_id},
            result="success", request_id=request_id,
        )
        conn.commit()
