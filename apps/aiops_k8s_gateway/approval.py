"""Gateway-owned Approval Authority and governed execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable

from .connector_commands import ConnectorCommands
from .evidence_decisions import mutation_action_reasons
from .gateway_db import GatewayDatabase, insert_admin_audit, register_migrations


_SCHEMA_VERSION = 11
_SCHEMA = """
CREATE TABLE approval_authorities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('*', 'prod', 'staging', 'dev', 'test')),
    scope_type TEXT NOT NULL CHECK (scope_type IN ('platform', 'team', 'service', 'deployment_target')),
    scope_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK ((scope_type = 'platform' AND scope_id IS NULL) OR (scope_type != 'platform' AND scope_id IS NOT NULL)),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX approval_authority_identity
    ON approval_authorities(user_id, environment, scope_type, IFNULL(scope_id, ''));

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    investigation_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_version INTEGER NOT NULL,
    action_hash TEXT NOT NULL CHECK (length(action_hash) = 64),
    approver_id TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    frozen_action_json TEXT NOT NULL CHECK (json_valid(frozen_action_json)),
    created_at REAL NOT NULL,
    UNIQUE (approver_id, idempotency_key),
    UNIQUE (investigation_id, action_id, action_version),
    FOREIGN KEY (incident_id) REFERENCES incidents(id),
    FOREIGN KEY (investigation_id) REFERENCES investigations(id),
    FOREIGN KEY (approver_id) REFERENCES users(id),
    FOREIGN KEY (authority_id) REFERENCES approval_authorities(id)
);

CREATE TABLE execution_grants (
    id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE,
    action_hash TEXT NOT NULL CHECK (length(action_hash) = 64),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    expires_at REAL NOT NULL,
    consumed_at REAL NOT NULL,
    command_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY (approval_id) REFERENCES approvals(id)
);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


class ApprovalError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Approvals:
    def __init__(
        self,
        database: GatewayDatabase,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
        grant_seconds: float = 60.0,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")
        self._grant_seconds = grant_seconds

    def list_authorities(self) -> list[dict[str, object]]:
        with self._database.connect() as conn:
            rows = conn.execute("SELECT * FROM approval_authorities ORDER BY created_at, id").fetchall()
        return [_authority(row) for row in rows]

    def create_authority(
        self,
        *,
        user_id: str,
        environment: str,
        scope_type: str,
        scope_id: str | None,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        user_id = _text(user_id, "user_id")
        environment = _environment(environment)
        scope_type, scope_id = _scope(scope_type, scope_id)
        now = self._clock()
        authority_id = self._id_factory("authority")
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute("SELECT disabled FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None or bool(user["disabled"]):
                raise ApprovalError("user_not_found", "Approval Authority requires an active User")
            _validate_scope_reference(conn, scope_type, scope_id)
            try:
                conn.execute(
                    "INSERT INTO approval_authorities (id, user_id, environment, scope_type, scope_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (authority_id, user_id, environment, scope_type, scope_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ApprovalError("approval_authority_exists", "Approval Authority already exists") from exc
            after = self._authority_in(conn, authority_id)
            insert_admin_audit(
                conn, actor_id=actor_id, target_type="approval-authorities", target_id=authority_id,
                action="approval-authorities_create", reason=reason, before=None, after=after,
                result="success", request_id=request_id,
            )
            conn.commit()
        return after

    def update_authority(
        self,
        authority_id: str,
        *,
        active: bool,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before = self._authority_in(conn, authority_id)
            conn.execute(
                "UPDATE approval_authorities SET active = ?, updated_at = ? WHERE id = ?",
                (int(active), self._clock(), authority_id),
            )
            if bool(before["active"]) and not active:
                conn.execute("DELETE FROM sessions WHERE actor_id = ?", (before["user_id"],))
            after = self._authority_in(conn, authority_id)
            insert_admin_audit(
                conn, actor_id=actor_id, target_type="approval-authorities", target_id=authority_id,
                action="approval-authorities_update", reason=reason, before=before, after=after,
                result="success", request_id=request_id,
            )
            conn.commit()
        return after

    def project_workbench(self, snapshot: dict[str, object], actor_id: str) -> dict[str, object]:
        context = snapshot["resource_context"]
        assert isinstance(context, dict)
        now = self._clock()
        with self._database.connect() as conn:
            authority = _matching_authority(conn, actor_id, context)
            approved = {
                (str(row["action_id"]), int(row["action_version"])): row
                for row in conn.execute(
                    """
                    SELECT a.id, a.action_id, a.action_version, g.command_id, c.status, c.result_json
                    FROM approvals a
                    JOIN execution_grants g ON g.approval_id = a.id
                    JOIN connector_commands c ON c.id = g.command_id
                    WHERE a.incident_id = ?
                    """,
                    (str(snapshot["incident"]["id"]),),  # type: ignore[index]
                )
            }
        for action in snapshot["recommended_actions"]:  # type: ignore[index]
            assert isinstance(action, dict)
            execution = approved.get((str(action["id"]), int(action["version"])))
            action["approval_id"] = str(execution["id"]) if execution is not None else None
            action["execution"] = {
                "command_id": str(execution["command_id"]),
                "status": str(execution["status"]),
                "result": json.loads(str(execution["result_json"])) if execution["result_json"] else None,
            } if execution is not None else None
            action["can_approve"] = bool(
                authority and action["approval_id"] is None and not action["stale"]
                and action["gate"]["approvable"] and float(action["expires_at"]) >= now  # type: ignore[index]
            )
        return snapshot

    def approve_and_execute(
        self,
        *,
        incident_id: str,
        action_id: str,
        action_version: int,
        action_hash: str,
        actor_id: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        request = {
            "incident_id": incident_id, "action_id": action_id, "action_version": action_version,
            "action_hash": action_hash, "actor_id": actor_id,
        }
        request_hash = hashlib.sha256(_json(request).encode()).hexdigest()
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT * FROM approvals WHERE approver_id = ? AND idempotency_key = ?",
                (actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise ApprovalError("idempotency_conflict", "idempotency key was used for another action")
                return self._execution_in(conn, str(replay["id"]), idempotent=True)
            row = conn.execute(
                """
                SELECT ra.*, i.incident_id, inc.status AS incident_status, inc.resource_binding_id,
                       c.environment, c.mutation_enabled, c.runtime_status, c.last_heartbeat, c.connector_id,
                       rb.revision AS current_binding_revision, rb.service_id, rb.team_id,
                       dt.id AS current_target_id, dt.cluster_id, dt.namespace, dt.workload_kind, dt.workload_name
                FROM recommended_actions ra
                JOIN investigations i ON i.id = ra.investigation_id
                JOIN incidents inc ON inc.id = i.incident_id
                JOIN clusters c ON c.cluster_id = inc.cluster_id
                LEFT JOIN resource_bindings rb ON rb.id = inc.resource_binding_id
                LEFT JOIN deployment_targets dt ON dt.id = rb.deployment_target_id
                WHERE i.incident_id = ? AND ra.id = ? AND ra.version = ?
                """,
                (incident_id, action_id, action_version),
            ).fetchone()
            if row is None:
                raise ApprovalError("action_stale", "Recommended Action is missing or superseded")
            target = json.loads(str(row["target_json"]))
            _validate_action(conn, row, target, action_hash, now)
            authority = _matching_authority(conn, actor_id, {
                "environment": row["environment"], "team_id": row["team_id"], "service_id": row["service_id"],
                "deployment_target_id": row["current_target_id"],
            })
            if authority is None:
                raise ApprovalError("forbidden", "Approval Authority does not cover this action")
            approval_id = self._id_factory("approval")
            grant_id = self._id_factory("grant")
            command_id = self._id_factory("command")
            frozen = {
                "id": action_id, "version": action_version, "hash": action_hash,
                "action_type": row["action_type"], "target": target,
                "parameters": json.loads(str(row["parameters_json"])),
                "evidence_step_ids": json.loads(str(row["evidence_step_ids_json"])),
                "safeguards": json.loads(str(row["safeguards_json"])),
                "rollback_plan": json.loads(str(row["rollback_plan_json"])),
            }
            conn.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (approval_id, incident_id, row["investigation_id"], action_id, action_version, action_hash,
                 actor_id, authority["id"], idempotency_key, request_hash, _json(frozen), now),
            )
            expires_at = now + self._grant_seconds
            conn.execute(
                "INSERT INTO execution_grants VALUES (?, ?, ?, ?, ?, ?, ?)",
                (grant_id, approval_id, action_hash, _json(target), expires_at, now, command_id),
            )
            ConnectorCommands(self._database, clock=self._clock).queue_mutation_in(
                conn, command_id=command_id, grant_id=grant_id, action_hash=action_hash,
                grant_expires_at=expires_at,
                connector_id=str(row["connector_id"]), cluster_id=str(row["cluster_id"]),
                namespace=str(row["namespace"]), deployment_name=str(row["workload_name"]),
                action=str(row["action_type"]), parameters=frozen["parameters"],
                rollback_plan=frozen["rollback_plan"], now=now,
            )
            insert_admin_audit(
                conn, actor_id=actor_id, target_type="approvals", target_id=approval_id,
                action="approve_and_execute", reason="explicit approve-and-execute control",
                before=None, after={"action_id": action_id, "command_id": command_id},
                result="success", request_id=request_id,
            )
            conn.commit()
        return self._execution(approval_id, idempotent=False)

    def _authority_in(self, conn: sqlite3.Connection, authority_id: str) -> dict[str, object]:
        row = conn.execute("SELECT * FROM approval_authorities WHERE id = ?", (authority_id,)).fetchone()
        if row is None:
            raise ApprovalError("approval_authority_not_found", "Approval Authority not found")
        return _authority(row)

    def _execution(self, approval_id: str, *, idempotent: bool) -> dict[str, object]:
        with self._database.connect() as conn:
            return self._execution_in(conn, approval_id, idempotent=idempotent)

    @staticmethod
    def _execution_in(conn: sqlite3.Connection, approval_id: str, *, idempotent: bool) -> dict[str, object]:
        row = conn.execute(
            "SELECT a.id AS approval_id, a.action_id, a.action_version, g.id AS execution_grant_id, g.expires_at, g.command_id FROM approvals a JOIN execution_grants g ON g.approval_id = a.id WHERE a.id = ?",
            (approval_id,),
        ).fetchone()
        assert row is not None
        return {**dict(row), "idempotent": idempotent}


def _validate_action(
    conn: sqlite3.Connection, row: sqlite3.Row, target: dict[str, object], action_hash: str, now: float
) -> None:
    current = {
        "cluster_id": row["cluster_id"], "namespace": row["namespace"], "workload_kind": row["workload_kind"],
        "workload_name": row["workload_name"], "deployment_target_id": row["current_target_id"],
        "resource_binding_id": row["resource_binding_id"], "binding_revision": row["current_binding_revision"],
    }
    evidence = json.loads(str(row["evidence_step_ids_json"]))
    placeholders = ",".join("?" for _ in evidence)
    steps = [] if not evidence else conn.execute(
        f"SELECT * FROM evidence_steps WHERE investigation_id = ? AND id IN ({placeholders})", (row["investigation_id"], *evidence)
    ).fetchall()
    parameters = json.loads(str(row["parameters_json"]))
    rollback_plan = json.loads(str(row["rollback_plan_json"]))
    stale = (
        bool(mutation_action_reasons(str(row["action_type"]), parameters, rollback_plan))
        or bool(row["stale"]) or row["gate_status"] != "complete" or row["action_hash"] != action_hash
        or target != current or row["incident_status"] != "active" or len(steps) != len(evidence)
        or any(step["state"] != "succeeded" or float(step["expires_at"]) < now for step in steps)
    )
    if stale:
        raise ApprovalError("action_stale", "Recommended Action changed or its evidence is stale")
    if not bool(row["mutation_enabled"]):
        raise ApprovalError("mutation_disabled", "Cluster mutation policy is disabled")
    if row["runtime_status"] != "online" or now - float(row["last_heartbeat"]) > 120:
        raise ApprovalError("connector_unavailable", "Connector is not currently available")


def _matching_authority(conn: sqlite3.Connection, actor_id: str, context: dict[str, object]) -> sqlite3.Row | None:
    user = conn.execute("SELECT disabled FROM users WHERE id = ?", (actor_id,)).fetchone()
    if user is None or bool(user["disabled"]):
        return None
    environment = str(context.get("environment") or "")
    values = {
        "team": context.get("team_id"), "service": context.get("service_id"),
        "deployment_target": context.get("deployment_target_id"),
    }
    rows = conn.execute(
        "SELECT * FROM approval_authorities WHERE user_id = ? AND active = 1 AND environment IN ('*', ?) ORDER BY environment DESC, created_at",
        (actor_id, environment),
    )
    return next((row for row in rows if row["scope_type"] == "platform" or values.get(str(row["scope_type"])) == row["scope_id"]), None)


def _validate_scope_reference(conn: sqlite3.Connection, scope_type: str, scope_id: str | None) -> None:
    if scope_type == "platform":
        return
    table = {"team": "teams", "service": "services", "deployment_target": "deployment_targets"}[scope_type]
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (scope_id,)).fetchone() is None:
        raise ApprovalError("scope_not_found", "Approval Authority requires a real resource scope")


def _authority(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]), "user_id": str(row["user_id"]), "environment": str(row["environment"]),
        "scope_type": str(row["scope_type"]), "scope_id": row["scope_id"], "active": bool(row["active"]),
    }


def _environment(value: object) -> str:
    value = _text(value, "environment")
    if value not in {"*", "prod", "staging", "dev", "test"}:
        raise ApprovalError("invalid_approval_authority", "environment is invalid")
    return value


def _scope(scope_type: object, scope_id: object) -> tuple[str, str | None]:
    scope_type = _text(scope_type, "scope_type")
    normalized_id = scope_id.strip() if isinstance(scope_id, str) and scope_id.strip() else None
    if scope_type not in {"platform", "team", "service", "deployment_target"}:
        raise ApprovalError("invalid_approval_authority", "resource scope is invalid")
    if (scope_type == "platform") != (normalized_id is None):
        raise ApprovalError("invalid_approval_authority", "scope_id does not match resource scope")
    return scope_type, normalized_id


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 253:
        raise ApprovalError("invalid_request", f"{field} is required")
    return value.strip()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
