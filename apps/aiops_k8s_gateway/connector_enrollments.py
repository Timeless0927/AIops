"""Gateway-owned Connector Enrollment and registered Cluster state."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

from aiops.domain.identity import IdentityError

from .connector_identity import ConnectorIdentity
from .gateway_db import GatewayDatabase, insert_admin_audit, register_migrations, token_hash

if TYPE_CHECKING:
    from .connector_commands import ConnectorCommands


_SCHEMA_VERSION = 3
_SCHEMA = """
CREATE TABLE connector_enrollments (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL UNIQUE CHECK (length(connector_id) > 0),
    cluster_id TEXT NOT NULL UNIQUE CHECK (length(cluster_id) > 0),
    credential_hash TEXT NOT NULL UNIQUE CHECK (length(credential_hash) = 64),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE clusters (
    cluster_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'prod' CHECK (environment IN ('prod', 'staging', 'dev', 'test')),
    governance_notes TEXT NOT NULL DEFAULT '',
    mutation_enabled INTEGER NOT NULL DEFAULT 0 CHECK (mutation_enabled IN (0, 1)),
    runtime_status TEXT NOT NULL DEFAULT 'online' CHECK (runtime_status IN ('online', 'offline', 'degraded')),
    failure_summary TEXT NOT NULL DEFAULT '',
    last_heartbeat REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (connector_id) REFERENCES connector_enrollments(connector_id)
);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))

_READ_VERIFICATION_SCHEMA_VERSION = 18
_READ_VERIFICATION_SCHEMA = """
ALTER TABLE connector_enrollments ADD COLUMN rotation_state TEXT NOT NULL DEFAULT 'current'
    CHECK (rotation_state IN ('current', 'pending'));
ALTER TABLE connector_enrollments ADD COLUMN candidate_credential_hash TEXT;
ALTER TABLE connector_enrollments ADD COLUMN candidate_expires_at REAL;
CREATE UNIQUE INDEX connector_candidate_credential
    ON connector_enrollments(candidate_credential_hash) WHERE candidate_credential_hash IS NOT NULL;

CREATE TABLE connector_read_verifications (
    cluster_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('verifying', 'verified', 'failed')),
    identity_json TEXT NOT NULL CHECK (json_valid(identity_json)),
    discovery_json TEXT CHECK (discovery_json IS NULL OR json_valid(discovery_json)),
    permission_summary_json TEXT CHECK (permission_summary_json IS NULL OR json_valid(permission_summary_json)),
    checked_at REAL,
    reason_code TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id) ON DELETE CASCADE
);
"""
register_migrations(((_READ_VERIFICATION_SCHEMA_VERSION, _READ_VERIFICATION_SCHEMA),))


class ConnectorEnrollments:
    def __init__(
        self,
        database: GatewayDatabase,
        *,
        clock: Callable[[], float] = time.time,
        credential_factory: Callable[[], str] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._credential_factory = credential_factory or (lambda: secrets.token_urlsafe(32))
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")
        self._identity = ConnectorIdentity(database)

    def admin_state(self) -> dict[str, list[dict[str, object]]]:
        with self._connect() as conn:
            self._expire_candidates_in(conn, self._clock())
            enrollments = conn.execute(
                """
                SELECT e.id, e.connector_id, e.cluster_id, e.active, e.rotation_state,
                       e.candidate_expires_at, c.runtime_status, c.last_heartbeat,
                       c.cluster_id IS NOT NULL AS registered,
                       COALESCE(v.status, 'unverified') AS verification_status
                FROM connector_enrollments e
                LEFT JOIN clusters c ON c.cluster_id = e.cluster_id
                LEFT JOIN connector_read_verifications v ON v.cluster_id = e.cluster_id
                ORDER BY e.created_at
                """
            ).fetchall()
            clusters = conn.execute(
                """SELECT c.*, v.status AS verification_status, v.checked_at,
                          v.reason_code, v.identity_json, v.discovery_json, v.permission_summary_json
                   FROM clusters c
                   LEFT JOIN connector_read_verifications v ON v.cluster_id = c.cluster_id
                   ORDER BY c.cluster_id"""
            ).fetchall()
        return {
            "connector_enrollments": [
                {
                    "id": str(row["id"]),
                    "connector_id": str(row["connector_id"]),
                    "cluster_id": str(row["cluster_id"]),
                    "active": bool(row["active"]),
                    "registered": bool(row["registered"]),
                    "state": _enrollment_state(row, now=self._clock()),
                    "read_verification": str(row["verification_status"]),
                    "rotation_expires_at": float(row["candidate_expires_at"])
                    if row["candidate_expires_at"] is not None
                    else None,
                }
                for row in enrollments
            ],
            "clusters": [_cluster_record(row, now=self._clock(), include_verification=True) for row in clusters],
        }

    def public_status(self) -> list[dict[str, object]]:
        state = self.admin_state()
        return [
            {
                "connector_id": enrollment["connector_id"],
                "cluster_id": enrollment["cluster_id"],
                "state": enrollment["state"],
                "read_verification": enrollment["read_verification"],
            }
            for enrollment in state["connector_enrollments"]
        ]

    def create(
        self,
        *,
        connector_id: str,
        cluster_id: str,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, object], str]:
        connector_id = connector_id.strip()
        cluster_id = cluster_id.strip()
        if not connector_id or not cluster_id:
            raise IdentityError("invalid_enrollment", "connector_id and cluster_id are required")
        now = self._clock()
        enrollment_id = self._id_factory("enr")
        credential = self._credential_factory()
        after = {
            "id": enrollment_id,
            "connector_id": connector_id,
            "cluster_id": cluster_id,
            "active": True,
            "registered": False,
            "state": "pending_registration",
            "read_verification": "unverified",
            "rotation_expires_at": None,
        }
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO connector_enrollments (
                        id, connector_id, cluster_id, credential_hash, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (enrollment_id, connector_id, cluster_id, token_hash(credential), now, now),
                )
                insert_admin_audit(
                    conn,
                    actor_id=actor_id,
                    target_type="connector-enrollments",
                    target_id=enrollment_id,
                    action="connector-enrollments_create",
                    reason=reason,
                    before=None,
                    after=after,
                    result="success",
                    request_id=request_id,
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise IdentityError("enrollment_exists", "Connector or Cluster is already enrolled") from exc
        return after, credential

    def update(
        self,
        enrollment_id: str,
        *,
        active: bool | None,
        rotate_credential: bool,
        retry_read_verification: bool = False,
        commands: ConnectorCommands | None = None,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, object], str | None]:
        if sum((active is not None, rotate_credential, retry_read_verification)) > 1:
            raise IdentityError("invalid_enrollment", "change one Enrollment lifecycle field at a time")
        credential = self._credential_factory() if rotate_credential else None
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_candidates_in(conn, now)
            row = conn.execute("SELECT * FROM connector_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
            if row is None:
                raise IdentityError("not_found", "Connector Enrollment not found")
            before = _enrollment_record(conn, row, now=now)
            next_active = bool(row["active"]) if active is None else active
            if rotate_credential:
                if row["rotation_state"] == "pending":
                    raise IdentityError("rotation_pending", "Connector credential rotation is already pending")
                if self._rotation_blocked_in(conn, str(row["cluster_id"])):
                    raise IdentityError("rotation_blocked", "unfinished Connector work blocks credential rotation")
            conn.execute(
                """
                UPDATE connector_enrollments
                SET active = ?, rotation_state = ?, candidate_credential_hash = ?,
                    candidate_expires_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    int(next_active),
                    "pending" if credential else row["rotation_state"],
                    token_hash(credential) if credential else row["candidate_credential_hash"],
                    now + 15 * 60 if credential else row["candidate_expires_at"],
                    now,
                    enrollment_id,
                ),
            )
            if not next_active:
                conn.execute(
                    """UPDATE connector_enrollments
                       SET rotation_state = 'current', candidate_credential_hash = NULL,
                           candidate_expires_at = NULL WHERE id = ?""",
                    (enrollment_id,),
                )
                conn.execute(
                    "UPDATE clusters SET runtime_status = 'offline', failure_summary = 'Connector credential revoked', updated_at = ? WHERE connector_id = ?",
                    (now, row["connector_id"]),
                )
            if retry_read_verification:
                if commands is None:
                    raise IdentityError("verification_unavailable", "Connector Command owner is unavailable")
                self._retry_verification_in(conn, row, commands, now)
            updated = conn.execute("SELECT * FROM connector_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
            after = _enrollment_record(conn, updated, now=now)
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="connector-enrollments",
                target_id=enrollment_id,
                action="connector-enrollments_update",
                reason=reason,
                before=before,
                after=after,
                result="success",
                request_id=request_id,
            )
            conn.commit()
        return after, credential

    def register(
        self,
        credential: str,
        connector_id: str,
        cluster_id: str,
        *,
        namespace_scope: list[str] | None = None,
        capabilities: list[str] | None = None,
        commands: ConnectorCommands | None = None,
        request_id: str,
    ) -> tuple[dict[str, object], bool]:
        now = self._clock()
        if commands is None:
            from .connector_commands import ConnectorCommands

            commands = ConnectorCommands(self._database, clock=self._clock)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_candidates_in(conn, now)
            candidate = self._authenticate_registration_in(
                conn, credential, connector_id, cluster_id, now=now
            )
            enrollment = conn.execute("SELECT * FROM connector_enrollments WHERE connector_id = ?", (connector_id,)).fetchone()
            if candidate:
                conn.execute(
                    """UPDATE connector_enrollments
                       SET credential_hash = candidate_credential_hash, rotation_state = 'current',
                           candidate_credential_hash = NULL, candidate_expires_at = NULL, updated_at = ?
                       WHERE id = ?""",
                    (now, enrollment["id"]),
                )
            previous = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            exists = previous is not None
            conn.execute(
                """
                INSERT INTO clusters (
                    cluster_id, connector_id, display_name, runtime_status, last_heartbeat, created_at, updated_at
                ) VALUES (?, ?, ?, 'offline', ?, ?, ?)
                ON CONFLICT(cluster_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (enrollment["cluster_id"], enrollment["connector_id"], enrollment["cluster_id"], now, now, now),
            )
            row = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            cluster = _cluster_record(row, now=now)
            self._ensure_verification_in(
                conn,
                connector_id=connector_id,
                cluster_id=cluster_id,
                namespace_scope=namespace_scope or ["default"],
                capabilities=capabilities or [],
                commands=commands,
                now=now,
                force=candidate,
            )
            if not exists:
                insert_admin_audit(
                    conn,
                    actor_id=None,
                    target_type="connectors",
                    target_id=connector_id,
                    action="connector_register",
                    reason="authenticated Connector registration",
                    before=None,
                    after=cluster,
                    result="success",
                    request_id=request_id,
                )
            conn.commit()
        return cluster, not exists

    def heartbeat(
        self,
        credential: str,
        connector_id: str,
        cluster_id: str,
        *,
        status: str,
        failure_summary: str,
        request_id: str,
    ) -> dict[str, object]:
        if status not in {"online", "degraded"}:
            raise IdentityError("invalid_status", "heartbeat status must be online or degraded")
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._identity.authenticate_in(conn, credential, connector_id, cluster_id)
            row = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            if row is None:
                raise IdentityError("not_registered", "Connector must register before heartbeat")
            conn.execute(
                "UPDATE clusters SET runtime_status = ?, failure_summary = ?, last_heartbeat = ?, updated_at = ? WHERE cluster_id = ?",
                (status, failure_summary.strip(), now, now, cluster_id),
            )
            updated = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            cluster = _cluster_record(updated, now=now)
            before = _cluster_record(row, now=now)
            if (before["runtime_status"], before["failure_summary"]) != (
                cluster["runtime_status"],
                cluster["failure_summary"],
            ):
                insert_admin_audit(
                    conn,
                    actor_id=None,
                    target_type="connectors",
                    target_id=connector_id,
                    action="connector_heartbeat",
                    reason="Connector runtime state transition",
                    before=before,
                    after=cluster,
                    result="success",
                    request_id=request_id,
                )
            conn.commit()
        return cluster

    def update_cluster(
        self,
        cluster_id: str,
        *,
        payload: dict[str, Any],
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            if row is None:
                raise IdentityError("not_found", "Cluster not found")
            before = _cluster_record(row, now=now)
            display_name = str(payload.get("display_name", row["display_name"])).strip()
            environment = str(payload.get("environment", row["environment"])).strip()
            if not display_name or environment not in {"prod", "staging", "dev", "test"}:
                raise IdentityError("invalid_cluster", "display_name or environment is invalid")
            conn.execute(
                """
                UPDATE clusters SET display_name = ?, environment = ?, governance_notes = ?,
                    mutation_enabled = ?, updated_at = ? WHERE cluster_id = ?
                """,
                (
                    display_name,
                    environment,
                    str(payload.get("governance_notes", row["governance_notes"])).strip(),
                    int(bool(payload.get("mutation_enabled", row["mutation_enabled"]))),
                    now,
                    cluster_id,
                ),
            )
            updated = conn.execute("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone()
            after = _cluster_record(updated, now=now)
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="clusters",
                target_id=cluster_id,
                action="clusters_update",
                reason=reason,
                before=before,
                after=after,
                result="success",
                request_id=request_id,
            )
            conn.commit()
        return after

    def record_verification_result_in(
        self,
        conn: sqlite3.Connection,
        command_id: str,
        result: dict[str, object],
        now: float,
    ) -> None:
        row = conn.execute(
            "SELECT cluster_id FROM connector_read_verifications WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return
        status = "failed"
        reason_code = str(result.get("error_code") or "read_verification_failed")
        discovery = permission_summary = None
        if result.get("status") == "succeeded":
            try:
                payload = json.loads(str(result.get("stdout") or ""))
                items = payload.get("items") if isinstance(payload, dict) else None
                kind = payload.get("kind") if isinstance(payload, dict) else None
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("apiVersion"), str)
                    or not isinstance(items, list)
                    or kind not in {"PodList", "List"}
                    or not all(
                        isinstance(item, dict)
                        and item.get("apiVersion") == payload.get("apiVersion")
                        and item.get("kind") == "Pod"
                        for item in items
                    )
                ):
                    raise ValueError("invalid PodList")
                identity = json.loads(
                    conn.execute(
                        "SELECT identity_json FROM connector_read_verifications WHERE command_id = ?",
                        (command_id,),
                    ).fetchone()[0]
                )
                namespace = identity["verification_namespace"]
                discovery = {"api_version": payload["apiVersion"], "kind": "PodList"}
                permission_summary = [
                    {"verb": "list", "resource": "pods", "namespace": namespace, "allowed": True}
                ]
                status = "verified"
                reason_code = ""
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                reason_code = "invalid_verification_response"
        conn.execute(
            """UPDATE connector_read_verifications
               SET status = ?, discovery_json = ?, permission_summary_json = ?,
                   checked_at = ?, reason_code = ?, updated_at = ? WHERE command_id = ?""",
            (
                status,
                _json(discovery) if discovery else None,
                _json(permission_summary) if permission_summary else None,
                now,
                reason_code,
                now,
                command_id,
            ),
        )

    def validation_connector_in(self, conn: sqlite3.Connection, cluster_id: str) -> str:
        return require_available_connector_in(
            conn, cluster_id, now=self._clock(), capability="validate",
        )

    def execution_connector_in(self, conn: sqlite3.Connection, cluster_id: str) -> str:
        return require_available_connector_in(
            conn, cluster_id, now=self._clock(), capability="execute",
        )

    @staticmethod
    def cluster_environment_in(conn: sqlite3.Connection, cluster_id: str) -> str | None:
        row = conn.execute(
            "SELECT environment FROM clusters WHERE cluster_id = ?", (cluster_id,),
        ).fetchone()
        return str(row["environment"]) if row is not None else None

    def _ensure_verification_in(
        self,
        conn: sqlite3.Connection,
        *,
        connector_id: str,
        cluster_id: str,
        namespace_scope: list[str],
        capabilities: list[str],
        commands: ConnectorCommands,
        now: float,
        force: bool,
    ) -> None:
        scope = sorted({item.strip() for item in namespace_scope if item.strip()})
        caps = sorted({item.strip() for item in capabilities if item.strip()})
        namespace = next((item for item in scope if item != "*"), "default")
        identity = {
            "connector_id": connector_id,
            "cluster_id": cluster_id,
            "namespace_scope": scope,
            "capabilities": caps,
            "verification_namespace": namespace,
        }
        encoded = _json(identity)
        existing = conn.execute(
            "SELECT status, identity_json FROM connector_read_verifications WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()
        if existing is not None and not force and str(existing["identity_json"]) == encoded:
            return
        command_id = commands.queue_verification_in(
            conn,
            connector_id=connector_id,
            cluster_id=cluster_id,
            namespace=namespace,
            now=now,
        )
        conn.execute(
            """INSERT INTO connector_read_verifications (
                   cluster_id, command_id, status, identity_json, created_at, updated_at
               ) VALUES (?, ?, 'verifying', ?, ?, ?)
               ON CONFLICT(cluster_id) DO UPDATE SET
                   command_id = excluded.command_id, status = 'verifying',
                   identity_json = excluded.identity_json, discovery_json = NULL,
                   permission_summary_json = NULL, checked_at = NULL,
                   reason_code = '', updated_at = excluded.updated_at""",
            (cluster_id, command_id, encoded, now, now),
        )

    def _retry_verification_in(
        self,
        conn: sqlite3.Connection,
        enrollment: sqlite3.Row,
        commands: ConnectorCommands,
        now: float,
    ) -> None:
        verification = conn.execute(
            "SELECT status, identity_json FROM connector_read_verifications WHERE cluster_id = ?",
            (enrollment["cluster_id"],),
        ).fetchone()
        if verification is None or verification["status"] != "failed":
            raise IdentityError("verification_not_retryable", "only failed read verification can be retried")
        identity = json.loads(str(verification["identity_json"]))
        self._ensure_verification_in(
            conn,
            connector_id=str(enrollment["connector_id"]),
            cluster_id=str(enrollment["cluster_id"]),
            namespace_scope=list(identity["namespace_scope"]),
            capabilities=list(identity["capabilities"]),
            commands=commands,
            now=now,
            force=True,
        )

    @staticmethod
    def _rotation_blocked_in(conn: sqlite3.Connection, cluster_id: str) -> bool:
        return conn.execute(
            """SELECT 1 FROM connector_commands
               WHERE cluster_id = ? AND (
                   status IN ('queued', 'leased', 'started', 'unknown_outcome')
                   OR json_extract(result_json, '$.error_code') = 'rollback_required'
               ) LIMIT 1""",
            (cluster_id,),
        ).fetchone() is not None

    @staticmethod
    def _expire_candidates_in(conn: sqlite3.Connection, now: float) -> None:
        conn.execute(
            """UPDATE connector_enrollments
               SET rotation_state = 'current', candidate_credential_hash = NULL,
                   candidate_expires_at = NULL, updated_at = ?
               WHERE rotation_state = 'pending' AND candidate_expires_at <= ?""",
            (now, now),
        )

    @staticmethod
    def _authenticate_registration_in(
        conn: sqlite3.Connection,
        credential: str,
        connector_id: str,
        cluster_id: str,
        *,
        now: float,
    ) -> bool:
        row = conn.execute(
            """SELECT connector_id, cluster_id, active, credential_hash,
                      candidate_credential_hash, candidate_expires_at
               FROM connector_enrollments
               WHERE credential_hash = ? OR candidate_credential_hash = ?""",
            (token_hash(credential), token_hash(credential)),
        ).fetchone()
        if row is None or not bool(row["active"]):
            raise IdentityError("invalid_connector_credential", "Connector credential is invalid or revoked")
        if row["connector_id"] != connector_id or row["cluster_id"] != cluster_id:
            raise IdentityError("identity_mismatch", "Connector identity does not match its Enrollment")
        candidate = row["candidate_credential_hash"] == token_hash(credential)
        if candidate and (row["candidate_expires_at"] is None or float(row["candidate_expires_at"]) < now):
            raise IdentityError("invalid_connector_credential", "Connector candidate credential expired")
        return candidate

    def _connect(self) -> sqlite3.Connection:
        return self._database.connect()


def _enrollment_record(
    conn: sqlite3.Connection, row: sqlite3.Row, *, now: float
) -> dict[str, object]:
    cluster = conn.execute(
        "SELECT runtime_status, last_heartbeat FROM clusters WHERE cluster_id = ?",
        (row["cluster_id"],),
    ).fetchone()
    verification = conn.execute(
        "SELECT status FROM connector_read_verifications WHERE cluster_id = ?",
        (row["cluster_id"],),
    ).fetchone()
    return {
        "id": str(row["id"]),
        "connector_id": str(row["connector_id"]),
        "cluster_id": str(row["cluster_id"]),
        "active": bool(row["active"]),
        "registered": cluster is not None,
        "state": _enrollment_state_values(
            active=bool(row["active"]),
            rotation_state=str(row["rotation_state"]),
            registered=cluster is not None,
            runtime_status=str(cluster["runtime_status"]) if cluster else None,
            last_heartbeat=float(cluster["last_heartbeat"]) if cluster else None,
            now=now,
        ),
        "read_verification": str(verification["status"]) if verification else "unverified",
        "rotation_expires_at": float(row["candidate_expires_at"])
        if row["candidate_expires_at"] is not None
        else None,
    }


def require_available_connector_in(
    conn: sqlite3.Connection,
    cluster_id: str,
    *,
    now: float,
    connector_id: str | None = None,
    capability: str | None = None,
    require_verified: bool = True,
) -> str:
    row = conn.execute(
        """SELECT e.connector_id, c.runtime_status, c.last_heartbeat,
                  v.status AS verification_status, v.identity_json
           FROM connector_enrollments e
           JOIN clusters c ON c.cluster_id = e.cluster_id
           LEFT JOIN connector_read_verifications v ON v.cluster_id = e.cluster_id
           WHERE e.cluster_id = ? AND e.active = 1 AND e.rotation_state = 'current'""",
        (cluster_id,),
    ).fetchone()
    if (
        row is None
        or (connector_id is not None and row["connector_id"] != connector_id)
        or row["runtime_status"] != "online"
        or now - float(row["last_heartbeat"]) > 120
        or (require_verified and row["verification_status"] != "verified")
    ):
        raise IdentityError("cluster_not_ready", "Connector is unavailable or not read-verified")
    identity = json.loads(str(row["identity_json"])) if row["identity_json"] else {}
    if capability is not None and capability not in identity.get("capabilities", []):
        raise IdentityError("cluster_not_ready", f"Connector does not advertise {capability}")
    return str(row["connector_id"])


def _cluster_record(
    row: sqlite3.Row,
    *,
    now: float,
    include_verification: bool = False,
) -> dict[str, object]:
    runtime_status = str(row["runtime_status"])
    failure_summary = str(row["failure_summary"])
    if runtime_status != "offline" and now - float(row["last_heartbeat"]) > 120:
        runtime_status = "offline"
        failure_summary = failure_summary or "Connector heartbeat is stale"
    record: dict[str, object] = {
        "cluster_id": str(row["cluster_id"]),
        "connector_id": str(row["connector_id"]),
        "display_name": str(row["display_name"]),
        "environment": str(row["environment"]),
        "governance_notes": str(row["governance_notes"]),
        "mutation_enabled": bool(row["mutation_enabled"]),
        "runtime_status": runtime_status,
        "failure_summary": failure_summary,
        "last_heartbeat": float(row["last_heartbeat"]),
    }
    if include_verification:
        identity = json.loads(str(row["identity_json"])) if row["identity_json"] else None
        record["read_verification"] = {
            "status": str(row["verification_status"] or "unverified"),
            "checked_at": float(row["checked_at"]) if row["checked_at"] is not None else None,
            "reason_code": str(row["reason_code"] or ""),
            "cluster_identity": {
                "connector_id": identity["connector_id"],
                "cluster_id": identity["cluster_id"],
            } if identity else None,
            "discovery": json.loads(str(row["discovery_json"])) if row["discovery_json"] else None,
            "permission_summary": json.loads(str(row["permission_summary_json"]))
            if row["permission_summary_json"]
            else [],
        }
    return record


def _enrollment_state(row: sqlite3.Row, *, now: float) -> str:
    return _enrollment_state_values(
        active=bool(row["active"]),
        rotation_state=str(row["rotation_state"]),
        registered=bool(row["registered"]),
        runtime_status=str(row["runtime_status"]) if row["runtime_status"] is not None else None,
        last_heartbeat=float(row["last_heartbeat"]) if row["last_heartbeat"] is not None else None,
        now=now,
    )


def _enrollment_state_values(
    *, active: bool, rotation_state: str, registered: bool,
    runtime_status: str | None, last_heartbeat: float | None, now: float,
) -> str:
    if not active:
        return "disabled"
    if rotation_state == "pending":
        return "rotation_pending"
    if not registered:
        return "pending_registration"
    if runtime_status == "offline" or last_heartbeat is None or now - last_heartbeat > 120:
        return "offline"
    return "online"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
