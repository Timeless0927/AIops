"""Gateway-owned durable Connector Command lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from .gateway_db import GatewayDatabase, insert_admin_audit, register_migrations


_SCHEMA_VERSION = 10
_SCHEMA = """
CREATE TABLE connector_commands (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action = 'get_resource'),
    parameters_json TEXT NOT NULL CHECK (json_valid(parameters_json)),
    status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'started', 'succeeded', 'failed', 'rejected')),
    lease_id TEXT,
    lease_expires_at REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    result_hash TEXT,
    result_received_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (connector_id) REFERENCES connector_enrollments(connector_id),
    FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id)
);
CREATE INDEX connector_commands_poll ON connector_commands(connector_id, cluster_id, status, lease_expires_at, created_at);

CREATE TABLE command_leases (
    lease_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    started_at REAL,
    FOREIGN KEY (command_id) REFERENCES connector_commands(id) ON DELETE CASCADE
);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_RESOURCE_KINDS = {"pods", "deployments", "services", "events"}
_OUTPUTS = {"json", "yaml", "wide"}
_TERMINAL = {"succeeded", "failed", "rejected"}


class ConnectorCommandError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConnectorCommands:
    def __init__(
        self,
        database: GatewayDatabase,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
        lease_seconds: float = 30.0,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")
        self._lease_seconds = lease_seconds

    def queue_read(
        self,
        *,
        cluster_id: str,
        namespace: str,
        action: str,
        parameters: object,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        cluster_id = _required_text(cluster_id, "cluster_id")
        namespace = _dns_label(namespace, "namespace")
        normalized = _validate_read_action(action, parameters)
        now = self._clock()
        command_id = self._id_factory("command")
        with self._database.connect() as conn:
            cluster = conn.execute(
                "SELECT connector_id FROM clusters WHERE cluster_id = ?", (cluster_id,)
            ).fetchone()
            if cluster is None:
                raise ConnectorCommandError("cluster_not_found", "registered Cluster not found")
            conn.execute(
                """
                INSERT INTO connector_commands (
                    id, connector_id, cluster_id, namespace, action, parameters_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (command_id, cluster["connector_id"], cluster_id, namespace, action, _json(normalized), now, now),
            )
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="connector_commands",
                target_id=command_id,
                action="connector_command_queue",
                reason=reason,
                before=None,
                after={"cluster_id": cluster_id, "namespace": namespace, "action": action},
                result="success",
                request_id=request_id,
            )
        return self.get(command_id)

    def poll(self, connector_id: str, cluster_id: str, wait_seconds: float) -> dict[str, object] | None:
        connector_id = _required_text(connector_id, "connector_id")
        cluster_id = _required_text(cluster_id, "cluster_id")
        deadline = self._clock() + min(max(float(wait_seconds), 0.0), 25.0)
        while True:
            command = self._lease_next(connector_id, cluster_id)
            if command is not None or self._clock() >= deadline:
                return command
            time.sleep(min(0.1, max(0.0, deadline - self._clock())))

    def start(self, command_id: str, connector_id: str, cluster_id: str, lease_id: str) -> dict[str, object]:
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._owned_command(conn, command_id, connector_id, cluster_id)
            lease = conn.execute(
                "SELECT * FROM command_leases WHERE lease_id = ? AND command_id = ? AND connector_id = ?",
                (lease_id, command_id, connector_id),
            ).fetchone()
            if lease is None or row["lease_id"] != lease_id:
                raise ConnectorCommandError("invalid_command_lease", "Command Lease is not current")
            if row["status"] == "started" and lease["started_at"] is not None:
                conn.commit()
                return {"id": command_id, "status": "started", "acknowledged": True}
            if row["status"] != "leased" or float(lease["expires_at"]) < now or int(row["attempt_count"]) >= 3:
                raise ConnectorCommandError("command_lease_expired", "Command Lease expired before start")
            conn.execute("UPDATE command_leases SET started_at = ? WHERE lease_id = ?", (now, lease_id))
            conn.execute(
                "UPDATE connector_commands SET status = 'started', attempt_count = attempt_count + 1, updated_at = ? WHERE id = ?",
                (now, command_id),
            )
            conn.commit()
        return {"id": command_id, "status": "started", "acknowledged": True}

    def submit_result(
        self,
        command_id: str,
        connector_id: str,
        cluster_id: str,
        lease_id: str,
        result: object,
        *,
        request_id: str,
    ) -> dict[str, object]:
        normalized = _validate_result(result)
        encoded = _json(normalized)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._owned_command(conn, command_id, connector_id, cluster_id)
            lease = conn.execute(
                "SELECT * FROM command_leases WHERE lease_id = ? AND command_id = ? AND connector_id = ?",
                (lease_id, command_id, connector_id),
            ).fetchone()
            if lease is None or lease["started_at"] is None:
                raise ConnectorCommandError("command_not_started", "result requires an acknowledged start")
            if row["result_hash"]:
                if row["result_hash"] == digest:
                    conn.commit()
                    return {"id": command_id, "status": row["status"], "idempotent": True, "late": True}
                insert_admin_audit(
                    conn,
                    actor_id=None,
                    target_type="connector_commands",
                    target_id=command_id,
                    action="connector_command_result_conflict",
                    reason="Connector submitted a different terminal result",
                    before={"result_hash": row["result_hash"]},
                    after={"result_hash": digest},
                    result="conflicting_result",
                    request_id=request_id,
                )
                conn.commit()
                raise ConnectorCommandError("conflicting_result", "terminal result conflicts with the accepted result")
            late = float(lease["expires_at"]) < now or row["lease_id"] != lease_id
            conn.execute(
                """
                UPDATE connector_commands
                SET status = ?, result_json = ?, result_hash = ?, result_received_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized["status"], encoded, digest, now, now, command_id),
            )
            if late:
                insert_admin_audit(
                    conn,
                    actor_id=None,
                    target_type="connector_commands",
                    target_id=command_id,
                    action="connector_command_late_result",
                    reason="Result arrived after its Command Lease",
                    before=None,
                    after={"status": normalized["status"]},
                    result="reconciled",
                    request_id=request_id,
                )
            conn.commit()
        return {"id": command_id, "status": normalized["status"], "idempotent": False, "late": late}

    def get(self, command_id: str) -> dict[str, object]:
        with self._database.connect() as conn:
            row = conn.execute("SELECT * FROM connector_commands WHERE id = ?", (command_id,)).fetchone()
        if row is None:
            raise ConnectorCommandError("command_not_found", "Connector Command not found")
        return _command_record(row)

    def summarize_clusters(self, clusters: list[dict[str, object]]) -> list[dict[str, object]]:
        # ponytail: scans retained history; use a window query when command volume makes this measurable.
        with self._database.connect() as conn:
            rows = conn.execute("SELECT * FROM connector_commands ORDER BY created_at DESC").fetchall()
        by_cluster: dict[str, list[Any]] = {}
        for row in rows:
            by_cluster.setdefault(str(row["cluster_id"]), []).append(row)
        return [
            {
                **cluster,
                "pending_read_commands": sum(row["status"] not in _TERMINAL for row in by_cluster.get(str(cluster["cluster_id"]), [])),
                "last_read_command": _command_summary(by_cluster[str(cluster["cluster_id"])][0])
                if by_cluster.get(str(cluster["cluster_id"]))
                else None,
                "last_read_result": next(
                    (_result_summary(row) for row in by_cluster.get(str(cluster["cluster_id"]), []) if row["status"] in _TERMINAL),
                    None,
                ),
            }
            for cluster in clusters
        ]

    def _lease_next(self, connector_id: str, cluster_id: str) -> dict[str, object] | None:
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM connector_commands
                WHERE connector_id = ? AND cluster_id = ? AND (
                    status = 'queued'
                    OR (status = 'leased' AND lease_expires_at <= ?)
                    OR (status = 'started' AND lease_expires_at <= ? AND attempt_count < 3)
                )
                ORDER BY created_at LIMIT 1
                """,
                (connector_id, cluster_id, now, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            lease_id = self._id_factory("lease")
            expires_at = now + self._lease_seconds
            conn.execute(
                "INSERT INTO command_leases (lease_id, command_id, connector_id, granted_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (lease_id, row["id"], connector_id, now, expires_at),
            )
            conn.execute(
                """
                UPDATE connector_commands SET status = 'leased', lease_id = ?, lease_expires_at = ?,
                    updated_at = ? WHERE id = ?
                """,
                (lease_id, expires_at, now, row["id"]),
            )
            leased = conn.execute("SELECT * FROM connector_commands WHERE id = ?", (row["id"],)).fetchone()
            conn.commit()
        return _command_record(leased)

    @staticmethod
    def _owned_command(conn: Any, command_id: str, connector_id: str, cluster_id: str) -> Any:
        row = conn.execute("SELECT * FROM connector_commands WHERE id = ?", (command_id,)).fetchone()
        if row is None:
            raise ConnectorCommandError("command_not_found", "Connector Command not found")
        if row["connector_id"] != connector_id or row["cluster_id"] != cluster_id:
            raise ConnectorCommandError("identity_mismatch", "Connector Command belongs to another identity")
        return row


def _validate_read_action(action: object, parameters: object) -> dict[str, object]:
    if action != "get_resource" or not isinstance(parameters, dict) or set(parameters) - {
        "resource_kind", "name", "selector", "output"
    }:
        raise ConnectorCommandError("invalid_read_command", "unsupported read action or parameters")
    resource_kind = parameters.get("resource_kind")
    if resource_kind not in _RESOURCE_KINDS:
        raise ConnectorCommandError("invalid_read_command", "unsupported Kubernetes resource kind")
    output = parameters.get("output", "json")
    if output not in _OUTPUTS:
        raise ConnectorCommandError("invalid_read_command", "unsupported output format")
    normalized: dict[str, object] = {"resource_kind": resource_kind, "output": output}
    for field in ("name", "selector"):
        value = parameters.get(field)
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > 253 or any(c in value for c in ";|`\n\r"):
                raise ConnectorCommandError("invalid_read_command", f"invalid {field}")
            normalized[field] = value.strip()
    if resource_kind == "events" and "name" in normalized:
        raise ConnectorCommandError("invalid_read_command", "events do not accept a resource name")
    return normalized


def _validate_result(result: object) -> dict[str, object]:
    allowed = {"status", "stdout", "stderr", "exit_code", "truncated", "error_code", "error_message"}
    if not isinstance(result, dict) or set(result) != allowed or result.get("status") not in _TERMINAL:
        raise ConnectorCommandError("invalid_command_result", "invalid terminal result")
    if not isinstance(result["stdout"], str) or not isinstance(result["stderr"], str):
        raise ConnectorCommandError("invalid_command_result", "stdout and stderr must be strings")
    if len(result["stdout"].encode()) > 1024 * 1024 or len(result["stderr"].encode()) > 1024 * 1024:
        raise ConnectorCommandError("invalid_command_result", "result exceeds output limit")
    if result["exit_code"] is not None and not isinstance(result["exit_code"], int):
        raise ConnectorCommandError("invalid_command_result", "exit_code must be an integer or null")
    if not isinstance(result["truncated"], bool):
        raise ConnectorCommandError("invalid_command_result", "truncated must be boolean")
    for field in ("error_code", "error_message"):
        if result[field] is not None and not isinstance(result[field], str):
            raise ConnectorCommandError("invalid_command_result", f"{field} must be a string or null")
    return dict(result)


def _command_record(row: Any) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "cluster_id": str(row["cluster_id"]),
        "namespace": str(row["namespace"]),
        "action": str(row["action"]),
        "parameters": json.loads(str(row["parameters_json"])),
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "lease_id": str(row["lease_id"]) if row["lease_id"] else None,
        "lease_expires_at": float(row["lease_expires_at"]) if row["lease_expires_at"] else None,
        "created_at": float(row["created_at"]),
        "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
    }


def _command_summary(row: Any) -> dict[str, object]:
    return {key: _command_record(row)[key] for key in ("id", "namespace", "action", "status", "attempt_count", "created_at")}


def _result_summary(row: Any) -> dict[str, object]:
    result = json.loads(str(row["result_json"]))
    return {
        **_command_summary(row),
        "result_received_at": float(row["result_received_at"]),
        "error_code": result.get("error_code"),
    }


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ConnectorCommandError("invalid_request", f"{field} is required")
    return value.strip()


def _dns_label(value: object, field: str) -> str:
    text = _required_text(value, field)
    if len(text) > 63 or _DNS_LABEL.fullmatch(text) is None:
        raise ConnectorCommandError("invalid_request", f"{field} must be a DNS label")
    return text


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
