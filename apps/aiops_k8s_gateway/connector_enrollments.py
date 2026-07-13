"""Gateway-owned Connector Enrollment and registered Cluster state."""

from __future__ import annotations

import secrets
import sqlite3
import time
import uuid
from typing import Any, Callable

from aiops.domain.identity import IdentityError

from .connector_identity import ConnectorIdentity
from .gateway_db import GatewayDatabase, insert_admin_audit, register_migrations, token_hash


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
            enrollments = conn.execute(
                """
                SELECT e.id, e.connector_id, e.cluster_id, e.active,
                       EXISTS(SELECT 1 FROM clusters c WHERE c.cluster_id = e.cluster_id) AS registered
                FROM connector_enrollments e ORDER BY e.created_at
                """
            ).fetchall()
            clusters = conn.execute("SELECT * FROM clusters ORDER BY cluster_id").fetchall()
        return {
            "connector_enrollments": [
                {
                    "id": str(row["id"]),
                    "connector_id": str(row["connector_id"]),
                    "cluster_id": str(row["cluster_id"]),
                    "active": bool(row["active"]),
                    "registered": bool(row["registered"]),
                }
                for row in enrollments
            ],
            "clusters": [_cluster_record(row, now=self._clock()) for row in clusters],
        }

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
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> tuple[dict[str, object], str | None]:
        credential = self._credential_factory() if rotate_credential else None
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM connector_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
            if row is None:
                raise IdentityError("not_found", "Connector Enrollment not found")
            before = _enrollment_record(conn, row)
            next_active = bool(row["active"]) if active is None else active
            conn.execute(
                """
                UPDATE connector_enrollments
                SET credential_hash = ?, active = ?, updated_at = ? WHERE id = ?
                """,
                (token_hash(credential) if credential else row["credential_hash"], int(next_active), now, enrollment_id),
            )
            if not next_active:
                conn.execute(
                    "UPDATE clusters SET runtime_status = 'offline', failure_summary = 'Connector credential revoked', updated_at = ? WHERE connector_id = ?",
                    (now, row["connector_id"]),
                )
            updated = conn.execute("SELECT * FROM connector_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
            after = _enrollment_record(conn, updated)
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
        request_id: str,
    ) -> tuple[dict[str, object], bool]:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._identity.authenticate_in(conn, credential, connector_id, cluster_id)
            enrollment = conn.execute("SELECT * FROM connector_enrollments WHERE connector_id = ?", (connector_id,)).fetchone()
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

    def _connect(self) -> sqlite3.Connection:
        return self._database.connect()


def _enrollment_record(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    registered = conn.execute("SELECT 1 FROM clusters WHERE cluster_id = ?", (row["cluster_id"],)).fetchone()
    return {
        "id": str(row["id"]),
        "connector_id": str(row["connector_id"]),
        "cluster_id": str(row["cluster_id"]),
        "active": bool(row["active"]),
        "registered": registered is not None,
    }


def _cluster_record(row: sqlite3.Row, *, now: float) -> dict[str, object]:
    runtime_status = str(row["runtime_status"])
    failure_summary = str(row["failure_summary"])
    if runtime_status != "offline" and now - float(row["last_heartbeat"]) > 120:
        runtime_status = "offline"
        failure_summary = failure_summary or "Connector heartbeat is stale"
    return {
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
