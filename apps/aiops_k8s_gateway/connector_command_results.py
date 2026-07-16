"""Terminal result acceptance for Gateway-owned Connector Commands."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from typing import Any

from .gateway_db import GatewayDatabase, insert_admin_audit

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
    now = clock()
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _owned_command(conn, command_id, connector_id, cluster_id)
        normalized = _validate_result(result, action=str(row["action"]))
        encoded = _json(normalized)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
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


def _validate_result(result: object, *, action: str) -> dict[str, object]:
    base = {"status", "stdout", "stderr", "exit_code", "truncated", "error_code", "error_message"}
    allowed = {frozenset(base)}
    if action == "execute_kubernetes_change":
        allowed.add(frozenset(base | {"execution"}))
    if not isinstance(result, dict) or frozenset(result) not in allowed or result.get("status") not in _TERMINAL:
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
    execution = result.get("execution")
    if action == "execute_kubernetes_change" and result["status"] == "succeeded" and execution is None:
        raise ConnectorCommandResultError("invalid_command_result", "successful Kubernetes execution requires typed execution facts")
    if execution is not None:
        _validate_execution(execution)
    return dict(result)


def _validate_execution(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"operation", "target", "post_checks"}:
        raise ConnectorCommandResultError("invalid_command_result", "typed execution facts are invalid")
    target, checks = value.get("target"), value.get("post_checks")
    if value.get("operation") not in {"create", "patch", "delete"}:
        raise ConnectorCommandResultError("invalid_command_result", "typed execution operation is invalid")
    if (
        not isinstance(target, dict)
        or set(target) != {"exists", "uid", "resource_version"}
        or not isinstance(target.get("exists"), bool)
        or any(item is not None and not isinstance(item, str) for item in (target.get("uid"), target.get("resource_version")))
        or any(isinstance(item, str) and len(item) > 512 for item in (target.get("uid"), target.get("resource_version")))
    ):
        raise ConnectorCommandResultError("invalid_command_result", "typed execution target is invalid")
    if not isinstance(checks, list) or not checks or len(checks) > 100 or any(
        not isinstance(item, dict)
        or set(item) != {"type", "status"}
        or not isinstance(item.get("type"), str)
        or not item["type"]
        or len(item["type"]) > 100
        or item.get("status") not in {"succeeded", "failed"}
        for item in checks
    ):
        raise ConnectorCommandResultError("invalid_command_result", "typed execution post-checks are invalid")


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
