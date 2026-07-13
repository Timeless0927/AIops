"""Terminal result acceptance for Gateway-owned Connector Commands."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from typing import Any

from .gateway_db import GatewayDatabase, insert_admin_audit
from .notification_requests import enqueue_execution_event

_READ_ACTIONS = {
    "get_resource", "validate_kubernetes_change", "reconcile_kubernetes_change",
}
_TERMINAL = {"succeeded", "failed", "rejected"}


class ConnectorCommandResultError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def submit_result(
    database: GatewayDatabase,
    clock: Callable[[], float],
    command_id: str,
    connector_id: str,
    cluster_id: str,
    lease_id: str,
    result: object,
    *,
    journal_evidence: object,
    request_id: str,
    result_handler: Callable[[Any, str, dict[str, object], float], None] | None,
) -> dict[str, object]:
    normalized = _validate_result(result)
    encoded = _json(normalized)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    now = clock()
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _owned_command(conn, command_id, connector_id, cluster_id)
        journal_recorded_at = _journal_recorded_at(
            command_id, digest, journal_evidence,
            required=row["action"] not in _READ_ACTIONS,
        )
        if row["result_hash"]:
            if row["result_hash"] == digest:
                conn.commit()
                return {"id": command_id, "status": row["status"], "idempotent": True, "late": True}
            insert_admin_audit(
                conn, actor_id=None, target_type="connector_commands", target_id=command_id,
                action="connector_command_result_conflict",
                reason="Connector submitted a different terminal result",
                before={"result_hash": row["result_hash"]}, after={"result_hash": digest},
                result="conflicting_result", request_id=request_id,
            )
            conn.commit()
            raise ConnectorCommandResultError(
                "conflicting_result", "terminal result conflicts with the accepted result",
            )
        lease = conn.execute(
            "SELECT * FROM command_leases WHERE lease_id = ? AND command_id = ? AND connector_id = ?",
            (lease_id, command_id, connector_id),
        ).fetchone()
        reconciled_without_lease = (
            lease is None and row["lease_id"] == lease_id
            and (
                row["status"] == "unknown_outcome"
                or (row["action"] in _READ_ACTIONS and row["status"] == "failed" and not row["result_hash"])
            )
        )
        if not reconciled_without_lease and (lease is None or lease["started_at"] is None):
            raise ConnectorCommandResultError(
                "command_not_started", "result requires an acknowledged start",
            )
        late = reconciled_without_lease or float(lease["expires_at"]) < now or row["lease_id"] != lease_id
        conn.execute(
            """
            UPDATE connector_commands
            SET status = ?, result_json = ?, result_hash = ?, result_received_at = ?,
                journal_recorded_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (normalized["status"], encoded, digest, now, journal_recorded_at, now, command_id),
        )
        if result_handler is not None:
            result_handler(conn, command_id, normalized, now)
        if late:
            insert_admin_audit(
                conn, actor_id=None, target_type="connector_commands", target_id=command_id,
                action="connector_command_late_result",
                reason="Result arrived after its Command Lease", before=None,
                after={"status": normalized["status"]}, result="reconciled",
                request_id=request_id,
            )
        if row["action"] not in _READ_ACTIONS:
            event_type = (
                "execution.rollback_required"
                if normalized.get("error_code") == "rollback_required"
                else "execution.succeeded" if normalized["status"] == "succeeded"
                else "execution.failed"
            )
            enqueue_execution_event(
                conn, event_type=event_type, command_id=command_id, now=now,
                error_code=str(normalized["error_code"]) if normalized.get("error_code") else None,
            )
        conn.commit()
    return {"id": command_id, "status": normalized["status"], "idempotent": False, "late": late}


def _owned_command(conn: Any, command_id: str, connector_id: str, cluster_id: str) -> Any:
    row = conn.execute("SELECT * FROM connector_commands WHERE id = ?", (command_id,)).fetchone()
    if row is None:
        raise ConnectorCommandResultError("command_not_found", "Connector Command not found")
    if row["connector_id"] != connector_id or row["cluster_id"] != cluster_id:
        raise ConnectorCommandResultError(
            "identity_mismatch", "Connector Command belongs to another identity",
        )
    return row


def _validate_result(result: object) -> dict[str, object]:
    allowed = {"status", "stdout", "stderr", "exit_code", "truncated", "error_code", "error_message"}
    if not isinstance(result, dict) or set(result) != allowed or result.get("status") not in _TERMINAL:
        raise ConnectorCommandResultError("invalid_command_result", "invalid terminal result")
    if not isinstance(result["stdout"], str) or not isinstance(result["stderr"], str):
        raise ConnectorCommandResultError("invalid_command_result", "stdout and stderr must be strings")
    if len(result["stdout"].encode()) > 1024 * 1024 or len(result["stderr"].encode()) > 1024 * 1024:
        raise ConnectorCommandResultError("invalid_command_result", "result exceeds output limit")
    if result["exit_code"] is not None and not isinstance(result["exit_code"], int):
        raise ConnectorCommandResultError(
            "invalid_command_result", "exit_code must be an integer or null",
        )
    if not isinstance(result["truncated"], bool):
        raise ConnectorCommandResultError("invalid_command_result", "truncated must be boolean")
    for field in ("error_code", "error_message"):
        if result[field] is not None and not isinstance(result[field], str):
            raise ConnectorCommandResultError(
                "invalid_command_result", f"{field} must be a string or null",
            )
    return dict(result)


def _journal_recorded_at(
    command_id: str,
    result_sha256: str,
    evidence: object,
    *,
    required: bool,
) -> float | None:
    if evidence is None and not required:
        return None
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"state", "command_id", "result_sha256", "recorded_at"}
        or evidence.get("state") != "terminal"
        or evidence.get("command_id") != command_id
        or evidence.get("result_sha256") != result_sha256
        or isinstance(evidence.get("recorded_at"), bool)
        or not isinstance(evidence.get("recorded_at"), (int, float))
        or not math.isfinite(float(evidence["recorded_at"]))
        or not 0 < float(evidence["recorded_at"])
    ):
        raise ConnectorCommandResultError(
            "untrusted_terminal_result",
            "mutation result requires matching durable Connector journal evidence",
        )
    return float(evidence["recorded_at"])


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
