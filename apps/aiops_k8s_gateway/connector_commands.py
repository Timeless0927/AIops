"""Gateway-owned durable Connector Command lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from aiops.domain.identity import IdentityError

from .connector_command_results import ConnectorCommandResultError, submit_result
from .gateway_audit import insert_admin_audit
from .gateway_db import GatewayDatabase, register_migrations


_SCHEMA_VERSION = 10
_SCHEMA = """
CREATE TABLE connector_commands (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'get_resource', 'validate_kubernetes_change', 'execute_kubernetes_change',
        'reconcile_kubernetes_change'
    )),
    parameters_json TEXT NOT NULL CHECK (json_valid(parameters_json)),
    kubernetes_execution_grant_id TEXT UNIQUE,
    execution_grant_expires_at REAL,
    action_hash TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'leased', 'started', 'succeeded', 'failed', 'rejected', 'unknown_outcome'
    )),
    lease_id TEXT,
    lease_expires_at REAL,
    execution_expires_at REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    result_hash TEXT,
    result_received_at REAL,
    journal_recorded_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (
        (action IN ('get_resource', 'validate_kubernetes_change', 'reconcile_kubernetes_change')
         AND kubernetes_execution_grant_id IS NULL
         AND execution_grant_expires_at IS NULL AND action_hash IS NULL)
        OR (action = 'execute_kubernetes_change'
            AND kubernetes_execution_grant_id IS NOT NULL
            AND execution_grant_expires_at IS NOT NULL AND length(action_hash) = 64)
    ),
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
_CLEANUP_BATCH_SIZE = 1000


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
        available_connector_in: Callable[..., str] | None = None,
        lease_identity_matches_in: Callable[[Any, str, str], bool] | None = None,
        verification_command_ids_in: Callable[[Any], set[str]] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")
        self._lease_seconds = lease_seconds
        self._available_connector_in = available_connector_in
        self._lease_identity_matches_in = lease_identity_matches_in
        self._verification_command_ids_in = verification_command_ids_in

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
            try:
                connector_id = self._require_available_connector_in(
                    conn, cluster_id, now=now,
                )
            except IdentityError as exc:
                raise ConnectorCommandError(exc.code, exc.message) from exc
            conn.execute(
                """
                INSERT INTO connector_commands (
                    id, connector_id, cluster_id, namespace, action, parameters_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (command_id, connector_id, cluster_id, namespace, action, _json(normalized), now, now),
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

    def queue_verification_in(
        self,
        conn: Any,
        *,
        connector_id: str,
        cluster_id: str,
        namespace: str,
        now: float,
    ) -> str:
        command_id = self._id_factory("command")
        conn.execute(
            """
            INSERT INTO connector_commands (
                id, connector_id, cluster_id, namespace, action, parameters_json,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'get_resource', ?, 'queued', ?, ?)
            """,
            (
                command_id,
                connector_id,
                cluster_id,
                namespace,
                _json({"resource_kind": "pods", "output": "json"}),
                now,
                now,
            ),
        )
        return command_id

    def poll(
        self, connector_id: str, cluster_id: str, wait_seconds: float,
        *, dispatcher: Callable[[str, str], dict[str, object] | None] | None = None,
    ) -> dict[str, object] | None:
        connector_id = _required_text(connector_id, "connector_id")
        cluster_id = _required_text(cluster_id, "cluster_id")
        deadline = self._clock() + min(max(float(wait_seconds), 0.0), 25.0)
        while True:
            try:
                with self._database.connect() as conn:
                    self._require_available_connector_in(
                        conn, cluster_id, connector_id=connector_id,
                        now=self._clock(), require_verified=False,
                    )
            except IdentityError as exc:
                raise ConnectorCommandError(exc.code, exc.message) from exc
            self.reconcile_unknown_outcomes()
            command = self._lease_next(connector_id, cluster_id)
            command = command or (
                dispatcher(connector_id, cluster_id) if dispatcher is not None else None
            )
            if command is not None or self._clock() >= deadline:
                return command
            time.sleep(min(0.1, max(0.0, deadline - self._clock())))

    def reconcile_unknown_outcomes(self, *, request_id: str = "connector-command-reconciler") -> int:
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT id FROM connector_commands
                   WHERE action = 'execute_kubernetes_change'
                     AND status = 'started' AND execution_expires_at <= ?""",
                (now,),
            ).fetchall()
            for row in rows:
                command_id = str(row["id"])
                conn.execute(
                    "UPDATE connector_commands SET status = 'unknown_outcome', updated_at = ? WHERE id = ?",
                    (now, command_id),
                )
                insert_admin_audit(
                    conn, actor_id=None, target_type="connector_commands", target_id=command_id,
                    action="connector_command_unknown_outcome",
                    reason="started mutation lease expired without a trustworthy terminal result",
                    before={"status": "started"}, after={"status": "unknown_outcome"},
                    result="unknown_outcome", request_id=request_id,
                )
            exhausted_reads = conn.execute(
                """SELECT id FROM connector_commands
                   WHERE action IN (
                       'get_resource', 'validate_kubernetes_change', 'reconcile_kubernetes_change'
                   ) AND status = 'started'
                     AND attempt_count >= 3 AND lease_expires_at <= ?""",
                (now,),
            ).fetchall()
            exhausted_result = _json({
                "status": "failed", "stdout": "", "stderr": "", "exit_code": None,
                "truncated": False, "error_code": "read_retry_exhausted",
                "error_message": "Connector Command read retry limit exhausted",
            })
            for row in exhausted_reads:
                command_id = str(row["id"])
                conn.execute(
                    """UPDATE connector_commands
                       SET status = 'failed', result_json = ?, result_received_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (exhausted_result, now, now, command_id),
                )
                insert_admin_audit(
                    conn, actor_id=None, target_type="connector_commands", target_id=command_id,
                    action="connector_command_read_retry_exhausted",
                    reason="started read exhausted its bounded retry limit",
                    before={"status": "started"}, after={"status": "failed"},
                    result="failed", request_id=request_id,
                )
            self._cleanup_expired_leases_in(conn, now)
            conn.commit()
        return len(rows) + len(exhausted_reads)

    def cleanup_expired_leases(self) -> int:
        with self._database.connect() as conn:
            deleted = self._cleanup_expired_leases_in(conn, self._clock())
        return deleted

    def metrics(self) -> str:
        now = self._clock()
        with self._database.connect() as conn:
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM connector_commands GROUP BY status"))
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM connector_commands WHERE status IN ('queued', 'leased', 'started')"
            ).fetchone()[0]
            leases = conn.execute(
                """SELECT COUNT(*) FROM command_leases leases
                   JOIN connector_commands commands ON commands.id = leases.command_id
                   WHERE commands.status IN ('leased', 'started')"""
            ).fetchone()[0]
            expired_leases = conn.execute(
                "SELECT COUNT(*) FROM command_leases WHERE expires_at <= ?", (now,)
            ).fetchone()[0]
        lines = [
            "# HELP aiops_gateway_connector_commands Current Connector Commands by bounded status",
            "# TYPE aiops_gateway_connector_commands gauge",
        ]
        for status in ("queued", "leased", "started", "succeeded", "failed", "rejected", "unknown_outcome"):
            lines.append(f'aiops_gateway_connector_commands{{status="{status}"}} {int(counts.get(status, 0))}')
        lines.extend((
            "# HELP aiops_gateway_connector_command_oldest_age_seconds Age of the oldest unfinished Connector Command",
            "# TYPE aiops_gateway_connector_command_oldest_age_seconds gauge",
            f"aiops_gateway_connector_command_oldest_age_seconds {max(0.0, now - float(oldest)) if oldest else 0.0:.1f}",
            "# HELP aiops_gateway_command_leases Current active Command Leases",
            "# TYPE aiops_gateway_command_leases gauge",
            f"aiops_gateway_command_leases {int(leases)}",
            "# HELP aiops_gateway_expired_command_leases Expired Command Leases awaiting cleanup",
            "# TYPE aiops_gateway_expired_command_leases gauge",
            f"aiops_gateway_expired_command_leases {int(expired_leases)}",
        ))
        return "\n".join(lines) + "\n"

    def start(
        self, command_id: str, connector_id: str, cluster_id: str, lease_id: str,
        *, start_handler: Callable[[Any, str, float], None] | None = None,
    ) -> dict[str, object]:
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
            if start_handler is not None:
                start_handler(conn, command_id, now)
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
        journal_evidence: object = None,
        request_id: str,
        result_handler: Callable[[Any, str, dict[str, object], float], None] | None = None,
    ) -> dict[str, object]:
        try:
            return submit_result(
                self._database, self._clock, command_id, connector_id, cluster_id,
                lease_id, result, journal_evidence=journal_evidence,
                request_id=request_id, result_handler=result_handler,
            )
        except ConnectorCommandResultError as exc:
            raise ConnectorCommandError(exc.code, str(exc)) from exc

    def get(self, command_id: str) -> dict[str, object]:
        with self._database.connect() as conn:
            row = conn.execute("SELECT * FROM connector_commands WHERE id = ?", (command_id,)).fetchone()
        if row is None:
            raise ConnectorCommandError("command_not_found", "Connector Command not found")
        return _command_record(row)

    def summarize_clusters(self, clusters: list[dict[str, object]]) -> list[dict[str, object]]:
        # ponytail: scans retained history; use a window query when command volume makes this measurable.
        with self._database.connect() as conn:
            conn.execute("BEGIN")
            verification_ids = (
                self._verification_command_ids_in(conn)
                if self._verification_command_ids_in is not None
                else set()
            )
            rows = conn.execute(
                """SELECT * FROM connector_commands
                   WHERE action = 'get_resource' ORDER BY created_at DESC"""
            ).fetchall()
        by_cluster: dict[str, list[Any]] = {}
        for row in rows:
            if str(row["id"]) in verification_ids:
                continue
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
            if (
                self._lease_identity_matches_in is None
                or not self._lease_identity_matches_in(conn, connector_id, cluster_id)
            ):
                conn.commit()
                return None
            row = conn.execute(
                """
                SELECT * FROM connector_commands
                WHERE connector_id = ? AND cluster_id = ?
                  AND action != 'execute_kubernetes_change'
                  AND (
                    (status = 'queued' AND action IN (
                        'get_resource', 'validate_kubernetes_change', 'reconcile_kubernetes_change'
                    ))
                    OR (status = 'leased' AND lease_expires_at <= ? AND action IN (
                        'get_resource', 'validate_kubernetes_change', 'reconcile_kubernetes_change'
                    ))
                    OR (status = 'started' AND action IN (
                        'get_resource', 'validate_kubernetes_change', 'reconcile_kubernetes_change'
                    ) AND lease_expires_at <= ? AND attempt_count < 3)
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

    def has_unfinished_for_rotation_in(self, conn: Any, cluster_id: str) -> bool:
        return conn.execute(
            """SELECT 1 FROM connector_commands
               WHERE cluster_id = ? AND (
                   status IN ('queued', 'leased', 'started', 'unknown_outcome')
                   OR json_extract(result_json, '$.error_code') = 'rollback_required'
               ) LIMIT 1""",
            (cluster_id,),
        ).fetchone() is not None

    def _require_available_connector_in(
        self,
        conn: Any,
        cluster_id: str,
        **facts: object,
    ) -> str:
        if self._available_connector_in is None:
            raise ConnectorCommandError(
                "cluster_not_ready", "Connector Enrollment owner is unavailable",
            )
        return self._available_connector_in(conn, cluster_id, **facts)

    @staticmethod
    def _owned_command(conn: Any, command_id: str, connector_id: str, cluster_id: str) -> Any:
        row = conn.execute("SELECT * FROM connector_commands WHERE id = ?", (command_id,)).fetchone()
        if row is None:
            raise ConnectorCommandError("command_not_found", "Connector Command not found")
        if row["connector_id"] != connector_id or row["cluster_id"] != cluster_id:
            raise ConnectorCommandError("identity_mismatch", "Connector Command belongs to another identity")
        return row

    @staticmethod
    def _cleanup_expired_leases_in(conn: Any, now: float) -> int:
        return conn.execute(
            """DELETE FROM command_leases WHERE rowid IN (
                   SELECT leases.rowid FROM command_leases leases
                   JOIN connector_commands commands ON commands.id = leases.command_id
                   WHERE leases.expires_at <= ?
                     AND (leases.started_at IS NULL OR commands.status IN (
                         'succeeded', 'failed', 'rejected', 'unknown_outcome'
                     ))
                   LIMIT ?
               )""",
            (now, _CLEANUP_BATCH_SIZE),
        ).rowcount


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


def _command_record(row: Any) -> dict[str, object]:
    parameters = json.loads(str(row["parameters_json"]))
    return {
        "id": str(row["id"]),
        "cluster_id": str(row["cluster_id"]),
        "namespace": str(row["namespace"]),
        "action": str(row["action"]),
        "parameters": parameters,
        "execution_grant_id": str(row["kubernetes_execution_grant_id"])
        if "kubernetes_execution_grant_id" in row.keys() and row["kubernetes_execution_grant_id"]
        else None,
        "execution_grant_expires_at": float(row["execution_grant_expires_at"]) if row["execution_grant_expires_at"] else None,
        "action_hash": str(row["action_hash"]) if row["action_hash"] else None,
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
