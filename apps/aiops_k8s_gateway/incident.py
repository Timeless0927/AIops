"""Gateway-owned Alert Signal, Incident and initial Investigation state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .connector_identity import ConnectorIdentity
from .gateway_db import GatewayDatabase, register_migrations
from .resource_catalog import ResourceCatalog


_SCHEMA_VERSION = 5
_SCHEMA = """
ALTER TABLE incidents ADD COLUMN cluster_id TEXT REFERENCES clusters(cluster_id);
ALTER TABLE incidents ADD COLUMN namespace TEXT;
ALTER TABLE incidents ADD COLUMN alertname TEXT;
ALTER TABLE incidents ADD COLUMN binding_status TEXT CHECK (binding_status IN ('bound', 'unbound'));
ALTER TABLE incidents ADD COLUMN correlation_key TEXT;
ALTER TABLE incidents ADD COLUMN deployment_target_id TEXT REFERENCES deployment_targets(id);
ALTER TABLE incidents ADD COLUMN resource_binding_id TEXT REFERENCES resource_bindings(id);
ALTER TABLE incidents ADD COLUMN binding_revision INTEGER;
ALTER TABLE incidents ADD COLUMN service_id TEXT REFERENCES services(id);
ALTER TABLE incidents ADD COLUMN team_id TEXT REFERENCES teams(id);
ALTER TABLE incidents ADD COLUMN workload_kind TEXT;
ALTER TABLE incidents ADD COLUMN workload_name TEXT;
ALTER TABLE incidents ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0);

CREATE UNIQUE INDEX incidents_active_correlation
    ON incidents(correlation_key) WHERE status = 'active' AND correlation_key IS NOT NULL;
CREATE INDEX incidents_visible_by_team ON incidents(team_id, status, updated_at DESC);

CREATE TABLE alert_signals (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) > 0),
    alertname TEXT NOT NULL CHECK (length(alertname) > 0),
    status TEXT NOT NULL CHECK (status IN ('firing', 'recovered')),
    severity TEXT NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    summary TEXT NOT NULL DEFAULT '',
    workload_kind TEXT,
    workload_name TEXT,
    started_at TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (cluster_id, fingerprint),
    FOREIGN KEY (incident_id) REFERENCES incidents(id),
    FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id)
);
CREATE INDEX alert_signals_by_incident ON alert_signals(incident_id, created_at, id);

CREATE TABLE investigations (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'paused', 'human_led', 'completed', 'failed', 'terminated')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (incident_id, sequence),
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE UNIQUE INDEX investigations_one_active
    ON investigations(incident_id)
    WHERE status IN ('queued', 'running', 'paused', 'human_led');
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class AlertSignal:
    fingerprint: str
    alertname: str
    status: str
    severity: str
    cluster_id: str
    namespace: str
    summary: str = ""
    workload_kind: str | None = None
    workload_name: str | None = None
    service_hint: str | None = None
    started_at: str | None = None


class IncidentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class IncidentService:
    """Correlates Alert Signals and projects actor-scoped Incident views."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        catalog: ResourceCatalog,
        connector_identity: ConnectorIdentity,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._catalog = catalog
        self._connector_identity = connector_identity
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def ingest(self, signal: AlertSignal) -> dict[str, object]:
        signal = _validated(signal)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._connector_identity.is_cluster_registered_in(conn, signal.cluster_id):
                raise IncidentError("cluster_not_registered", "Alert Signal requires a registered Cluster")
            existing_signal = conn.execute(
                "SELECT * FROM alert_signals WHERE cluster_id = ? AND fingerprint = ?",
                (signal.cluster_id, signal.fingerprint),
            ).fetchone()
            if existing_signal is not None:
                result = self._update_signal(conn, existing_signal, signal, now)
                conn.commit()
                return result
            if signal.status == "recovered":
                conn.rollback()
                return {"accepted": False, "reason": "unknown_fingerprint"}

            resource = self._catalog.resolve_alert_resource_in(
                conn,
                cluster_id=signal.cluster_id,
                namespace=signal.namespace,
                workload_kind=signal.workload_kind,
                workload_name=signal.workload_name,
                service_hint=signal.service_hint,
            )
            correlation_key = _correlation_key(signal, resource)
            incident = conn.execute(
                "SELECT * FROM incidents WHERE correlation_key = ? AND status = 'active'",
                (correlation_key,),
            ).fetchone()
            created = incident is None
            if created:
                incident_id = self._id_factory("incident")
                binding_status = "bound" if resource else "unbound"
                conn.execute(
                    """
                    INSERT INTO incidents (
                        id, title, severity, status, created_at, updated_at,
                        cluster_id, namespace, alertname, binding_status, correlation_key,
                        deployment_target_id, resource_binding_id, binding_revision,
                        service_id, team_id, workload_kind, workload_name, revision
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        incident_id,
                        _title(signal, resource),
                        signal.severity,
                        now,
                        now,
                        signal.cluster_id,
                        signal.namespace,
                        signal.alertname,
                        binding_status,
                        correlation_key,
                        resource.get("deployment_target_id") if resource else None,
                        resource.get("resource_binding_id") if resource else None,
                        resource.get("binding_revision") if resource else None,
                        resource.get("service_id") if resource else None,
                        resource.get("team_id") if resource else None,
                        resource.get("workload_kind") if resource else signal.workload_kind,
                        resource.get("workload_name") if resource else signal.workload_name,
                    ),
                )
                conn.execute(
                    "INSERT INTO investigations (id, incident_id, sequence, status, created_at, updated_at) VALUES (?, ?, 1, 'queued', ?, ?)",
                    (self._id_factory("investigation"), incident_id, now, now),
                )
            else:
                incident_id = str(incident["id"])
                severity = _max_severity(str(incident["severity"]), signal.severity)
                conn.execute(
                    "UPDATE incidents SET severity = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
                    (severity, now, incident_id),
                )
            conn.execute(
                """
                INSERT INTO alert_signals (
                    id, incident_id, cluster_id, fingerprint, alertname, status,
                    severity, summary, workload_kind, workload_name, started_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._id_factory("signal"),
                    incident_id,
                    signal.cluster_id,
                    signal.fingerprint,
                    signal.alertname,
                    signal.status,
                    signal.severity,
                    signal.summary,
                    signal.workload_kind,
                    signal.workload_name,
                    signal.started_at,
                    now,
                    now,
                ),
            )
            conn.commit()
        return {"accepted": True, "created": created, "incident": self._incident(incident_id)}

    def list_incidents(self, *, team_ids: set[str] | None) -> list[dict[str, object]]:
        with self._database.connect() as conn:
            rows = self._visible_rows(conn, team_ids=team_ids)
        return [_incident_row(row) for row in rows]

    def workbench(
        self,
        incident_id: str,
        *,
        team_ids: set[str] | None,
        actor_capabilities: list[str],
    ) -> dict[str, object] | None:
        with self._database.connect() as conn:
            conn.execute("BEGIN")
            rows = self._visible_rows(conn, team_ids=team_ids, incident_id=incident_id)
            if not rows:
                return None
            row = rows[0]
            signals = conn.execute(
                """
                SELECT fingerprint, alertname, status, severity, summary,
                       workload_kind, workload_name, started_at, created_at, updated_at
                FROM alert_signals WHERE incident_id = ?
                ORDER BY created_at, id LIMIT 100
                """,
                (incident_id,),
            ).fetchall()
            investigation = conn.execute(
                """
                SELECT id, sequence, status, created_at, updated_at
                FROM investigations WHERE incident_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
        incident = _incident_row(row)
        snapshot: dict[str, object] = {
            "incident": incident,
            "resource_context": {
                "cluster_id": str(row["cluster_id"]),
                "cluster_name": str(row["cluster_name"]),
                "environment": str(row["environment"]),
                "runtime_status": str(row["runtime_status"]),
                "namespace": str(row["namespace"]),
                "workload_kind": row["workload_kind"],
                "workload_name": row["workload_name"],
                "deployment_target_id": row["deployment_target_id"],
                "service_id": row["current_service_id"],
                "service_name": row["service_name"],
                "resource_binding_id": row["resource_binding_id"],
                "binding_revision": row["current_binding_revision"],
            },
            "alert_signals": [dict(signal) for signal in signals],
            "investigation": dict(investigation) if investigation is not None else None,
            "evidence_steps": [],
            "judgment": None,
            "recommended_actions": [],
            "responsibility": {
                "status": "assigned" if row["current_team_id"] else "unassigned",
                "team_id": row["current_team_id"],
                "team_name": row["team_name"],
            },
            "actor_capabilities": sorted(set(actor_capabilities)),
            "event_cursor": 0,
        }
        snapshot["snapshot_revision"] = _snapshot_revision(snapshot)
        return snapshot

    def team_ids_for_actor(self, actor_id: str) -> set[str]:
        with self._database.connect() as conn:
            rows = conn.execute(
                "SELECT rb.scope_id FROM role_bindings rb JOIN team_memberships tm ON tm.user_id=rb.user_id AND tm.team_id=rb.scope_id AND tm.active=1 "
                "JOIN teams t ON t.id=tm.team_id AND t.active=1 WHERE rb.user_id=? AND rb.role='sre' AND rb.active=1",
                (actor_id,),
            )
            return {str(row["scope_id"]) for row in rows}

    def _update_signal(
        self,
        conn: sqlite3.Connection,
        existing: sqlite3.Row,
        signal: AlertSignal,
        now: float,
    ) -> dict[str, object]:
        if str(existing["alertname"]) != signal.alertname:
            raise IncidentError("fingerprint_conflict", "Alertmanager fingerprint is already used by another Alert Signal")
        incident_id = str(existing["incident_id"])
        conn.execute(
            """
            UPDATE alert_signals
            SET status = ?, severity = ?, summary = ?, workload_kind = ?, workload_name = ?, started_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                signal.status,
                signal.severity,
                signal.summary,
                signal.workload_kind,
                signal.workload_name,
                signal.started_at,
                now,
                existing["id"],
            ),
        )
        incident = conn.execute("SELECT severity FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        severity = _max_severity(str(incident["severity"]), signal.severity)
        conn.execute(
            "UPDATE incidents SET severity = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (severity, now, incident_id),
        )
        return {"accepted": True, "created": False, "incident": self._incident_in(conn, incident_id)}

    def _incident(self, incident_id: str) -> dict[str, object]:
        with self._database.connect() as conn:
            return self._incident_in(conn, incident_id)

    def _incident_in(self, conn: sqlite3.Connection, incident_id: str) -> dict[str, object]:
        row = self._visible_rows(conn, team_ids=None, incident_id=incident_id)[0]
        return _incident_row(row)

    def _visible_rows(
        self,
        conn: sqlite3.Connection,
        *,
        team_ids: set[str] | None,
        incident_id: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[object] = []
        if incident_id is not None:
            clauses.append("i.id = ?")
            params.append(incident_id)
        if team_ids is not None:
            if team_ids:
                placeholders = ",".join("?" for _ in team_ids)
                clauses.append(f"(COALESCE(rb.team_id, i.team_id) IS NULL OR COALESCE(rb.team_id, i.team_id) IN ({placeholders}))")
                params.extend(sorted(team_ids))
            else:
                clauses.append("COALESCE(rb.team_id, i.team_id) IS NULL")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        return conn.execute(
            f"""
            SELECT i.*, c.display_name AS cluster_name, c.environment, c.runtime_status,
                   COALESCE(rb.service_id, i.service_id) AS current_service_id,
                   COALESCE(rb.team_id, i.team_id) AS current_team_id,
                   COALESCE(rb.revision, i.binding_revision) AS current_binding_revision,
                   s.name AS service_name, t.name AS team_name,
                   (SELECT COUNT(*) FROM alert_signals a WHERE a.incident_id = i.id) AS signal_count
            FROM incidents i
            JOIN clusters c ON c.cluster_id = i.cluster_id
            LEFT JOIN resource_bindings rb ON rb.id = i.resource_binding_id
            LEFT JOIN services s ON s.id = COALESCE(rb.service_id, i.service_id)
            LEFT JOIN teams t ON t.id = COALESCE(rb.team_id, i.team_id)
            {where}
            ORDER BY CASE i.status WHEN 'active' THEN 0 ELSE 1 END, i.updated_at DESC, i.id
            """,
            params,
        ).fetchall()


def _validated(signal: AlertSignal) -> AlertSignal:
    values = {
        "fingerprint": _text(signal.fingerprint, "fingerprint", 256),
        "alertname": _text(signal.alertname, "alertname", 200),
        "cluster_id": _text(signal.cluster_id, "cluster_id", 200),
        "namespace": _text(signal.namespace, "namespace", 253),
        "summary": _optional(signal.summary, 2000) or "",
        "workload_kind": _optional(signal.workload_kind, 100),
        "workload_name": _optional(signal.workload_name, 253),
        "service_hint": _optional(signal.service_hint, 253),
        "started_at": _optional(signal.started_at, 100),
    }
    if signal.status not in {"firing", "recovered"}:
        raise IncidentError("invalid_alert_signal", "status must be firing or recovered")
    if signal.severity not in _SEVERITY_RANK:
        raise IncidentError("invalid_alert_signal", "severity is invalid")
    return AlertSignal(status=signal.status, severity=signal.severity, **values)


def _text(value: str, field: str, limit: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > limit:
        raise IncidentError("invalid_alert_signal", f"{field} is required and must not exceed {limit} characters")
    return normalized


def _optional(value: str | None, limit: int) -> str | None:
    normalized = value.strip() if isinstance(value, str) else ""
    if len(normalized) > limit:
        raise IncidentError("invalid_alert_signal", f"Alert Signal field must not exceed {limit} characters")
    return normalized or None


def _correlation_key(signal: AlertSignal, resource: dict[str, object] | None) -> str:
    if resource:
        identity = f"target:{resource['deployment_target_id']}"
    elif signal.workload_name:
        identity = f"workload:{signal.cluster_id}:{signal.namespace}:{signal.workload_kind or 'Workload'}:{signal.workload_name}"
    else:
        identity = f"fingerprint:{signal.cluster_id}:{signal.fingerprint}"
    return f"{identity}|alert:{signal.alertname}"


def _title(signal: AlertSignal, resource: dict[str, object] | None) -> str:
    target = resource.get("service_name") if resource else signal.workload_name
    return f"{signal.alertname} - {target}" if target else signal.alertname


def _max_severity(left: str, right: str) -> str:
    return left if _SEVERITY_RANK[left] >= _SEVERITY_RANK[right] else right


def _incident_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "severity": str(row["severity"]),
        "status": str(row["status"]),
        "binding_status": str(row["binding_status"]),
        "cluster_id": str(row["cluster_id"]),
        "cluster_name": str(row["cluster_name"]),
        "environment": str(row["environment"]),
        "namespace": str(row["namespace"]),
        "alertname": str(row["alertname"]),
        "workload_name": row["workload_name"],
        "service_name": row["service_name"],
        "team_name": row["team_name"],
        "signal_count": int(row["signal_count"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _snapshot_revision(snapshot: dict[str, object]) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]
