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
from .diagnosis_delivery import persist_diagnosis_request
from .evidence_decisions import project as project_evidence_decisions, stale_incident_actions
from .gateway_db import GatewayDatabase, register_migrations
from .investigation_events import append_event
from .notification_requests import enqueue_incident_event
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

_LIFECYCLE_SCHEMA_VERSION = 6
_LIFECYCLE_SCHEMA = """
ALTER TABLE incidents ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'firing'
    CHECK (lifecycle_state IN ('firing', 'stabilizing', 'resolved', 'reopened'));
ALTER TABLE incidents ADD COLUMN resolved_at REAL;
ALTER TABLE incidents ADD COLUMN reopened_at REAL;
ALTER TABLE incidents ADD COLUMN evidence_revision INTEGER NOT NULL DEFAULT 0 CHECK (evidence_revision >= 0);

ALTER TABLE alert_signals RENAME TO alert_signals_v5;
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
    FOREIGN KEY (incident_id) REFERENCES incidents(id),
    FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id)
);
INSERT INTO alert_signals SELECT * FROM alert_signals_v5;
DROP TABLE alert_signals_v5;
CREATE INDEX alert_signals_by_incident ON alert_signals(incident_id, created_at, id);
CREATE INDEX alert_signals_latest_fingerprint ON alert_signals(cluster_id, fingerprint, created_at DESC);

CREATE TABLE recovery_observations (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    evidence_revision INTEGER NOT NULL CHECK (evidence_revision > 0),
    observed_at REAL NOT NULL,
    stabilizes_at REAL NOT NULL CHECK (stabilizes_at >= observed_at),
    cancelled_at REAL,
    resolved_at REAL,
    CHECK (cancelled_at IS NULL OR resolved_at IS NULL),
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE UNIQUE INDEX recovery_observations_one_active
    ON recovery_observations(incident_id) WHERE cancelled_at IS NULL AND resolved_at IS NULL;
CREATE INDEX recovery_observations_latest ON recovery_observations(incident_id, observed_at DESC, id DESC);
"""
register_migrations(((_LIFECYCLE_SCHEMA_VERSION, _LIFECYCLE_SCHEMA),))
register_migrations(((43, "ALTER TABLE alert_signals ADD COLUMN firing_webhook_request_id TEXT; ALTER TABLE alert_signals ADD COLUMN recovered_webhook_request_id TEXT;"),))

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
        stabilization_seconds: float = 5 * 60,
        reopen_seconds: float = 24 * 60 * 60,
        diagnosis_request_ttl_seconds: float = 15 * 60,
    ) -> None:
        if stabilization_seconds < 0 or reopen_seconds < 0 or diagnosis_request_ttl_seconds <= 0:
            raise ValueError("Incident lifecycle windows must be non-negative and Diagnosis Request TTL must be positive")
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._catalog = catalog
        self._connector_identity = connector_identity
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")
        self._stabilization_seconds = stabilization_seconds
        self._reopen_seconds = reopen_seconds
        self._diagnosis_request_ttl_seconds = diagnosis_request_ttl_seconds

    def ingest(self, signal: AlertSignal, *, webhook_request_id: str | None = None) -> dict[str, object]:
        signal = _validated(signal)
        webhook_request_id = _optional(webhook_request_id, 128)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._resolve_due_recoveries(conn, now)
            if not self._connector_identity.is_cluster_registered_in(conn, signal.cluster_id):
                raise IncidentError("cluster_not_registered", "Alert Signal requires a registered Cluster")
            existing_signal = conn.execute(
                """
                SELECT a.*, i.status AS incident_status, i.resolved_at
                FROM alert_signals a JOIN incidents i ON i.id = a.incident_id
                WHERE a.cluster_id = ? AND a.fingerprint = ?
                ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1
                """,
                (signal.cluster_id, signal.fingerprint),
            ).fetchone()
            if existing_signal is not None:
                if str(existing_signal["alertname"]) != signal.alertname:
                    raise IncidentError("fingerprint_conflict", "Alertmanager fingerprint is already used by another Alert Signal")
                if existing_signal["incident_status"] == "active" or signal.status == "recovered":
                    result = self._update_signal(
                        conn, existing_signal, signal, now, webhook_request_id
                    )
                    conn.commit()
                    return result
                resolved_at = float(existing_signal["resolved_at"])
                if now <= resolved_at + self._reopen_seconds:
                    self._reopen_incident(conn, str(existing_signal["incident_id"]), now)
                    result = self._update_signal(
                        conn, existing_signal, signal, now, webhook_request_id
                    )
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
                """
                SELECT * FROM incidents
                WHERE correlation_key = ?
                  AND (status = 'active' OR (status = 'resolved' AND resolved_at >= ?))
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, resolved_at DESC
                LIMIT 1
                """,
                (correlation_key, now - self._reopen_seconds),
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
                investigation_id = self._id_factory("investigation")
                conn.execute(
                    "INSERT INTO investigations (id, incident_id, sequence, status, created_at, updated_at) VALUES (?, ?, 1, 'queued', ?, ?)",
                    (investigation_id, incident_id, now, now),
                )
                append_event(
                    conn,
                    investigation_id=investigation_id,
                    event_type="investigation.lifecycle",
                    idempotency_key="lifecycle:queued",
                    payload={"from": None, "to": "queued", "reason": "alert_signal"},
                    created_at=now,
                )
                persist_diagnosis_request(
                    conn,
                    request_id=self._id_factory("diagnosis-request"),
                    investigation_id=investigation_id,
                    now=now,
                    ttl_seconds=self._diagnosis_request_ttl_seconds,
                )
            else:
                incident_id = str(incident["id"])
                if incident["status"] == "resolved":
                    self._reopen_incident(conn, incident_id, now)
                self._update_incident_severity(conn, incident_id, str(incident["severity"]), signal.severity, now)
            conn.execute(
                """
                INSERT INTO alert_signals (
                    id, incident_id, cluster_id, fingerprint, alertname, status,
                    severity, summary, workload_kind, workload_name, started_at, created_at,
                    updated_at, firing_webhook_request_id, recovered_webhook_request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    webhook_request_id,
                    None,
                ),
            )
            if not created:
                self._cancel_recovery(conn, incident_id, now)
            if created:
                enqueue_incident_event(conn, event_type="incident.opened", incident_id=incident_id, now=now)
            conn.commit()
        return {"accepted": True, "created": created, "incident": self._incident(incident_id)}

    def list_incidents(self, *, team_ids: set[str] | None) -> list[dict[str, object]]:
        with self._database.connect() as conn:
            self._resolve_due_recoveries(conn, self._clock())
            rows = self._visible_rows(conn, team_ids=team_ids)
        return [_incident_row(row) for row in rows]

    def reinvestigate(self, incident_id: str, *, idempotency_key: str | None = None) -> dict[str, object]:
        """Explicitly create the next Investigation after a terminal round."""
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute("SELECT id FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if incident is None:
                raise IncidentError("incident_not_found", "Incident was not found")
            if idempotency_key:
                replay = conn.execute(
                    """
                    SELECT i.id, i.incident_id, i.sequence, i.status
                    FROM investigation_events e JOIN investigations i ON i.id = e.investigation_id
                    WHERE i.incident_id = ? AND e.idempotency_key = ?
                    """,
                    (incident_id, f"reinvestigate:{idempotency_key}"),
                ).fetchone()
                if replay is not None:
                    return dict(replay)
            latest = conn.execute(
                "SELECT sequence, status FROM investigations WHERE incident_id = ? ORDER BY sequence DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
            if latest is not None and latest["status"] in {"queued", "running", "paused", "human_led"}:
                raise IncidentError("investigation_active", "Incident already has an active Investigation")
            sequence = int(latest["sequence"]) + 1 if latest is not None else 1
            investigation_id = self._id_factory("investigation")
            conn.execute(
                "INSERT INTO investigations (id, incident_id, sequence, status, created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?)",
                (investigation_id, incident_id, sequence, now, now),
            )
            append_event(
                conn,
                investigation_id=investigation_id,
                event_type="investigation.lifecycle",
                idempotency_key=f"reinvestigate:{idempotency_key or investigation_id}",
                payload={"from": None, "to": "queued", "reason": "reinvestigate"},
                created_at=now,
            )
            persist_diagnosis_request(
                conn,
                request_id=self._id_factory("diagnosis-request"),
                investigation_id=investigation_id,
                now=now,
                ttl_seconds=self._diagnosis_request_ttl_seconds,
            )
            conn.commit()
        return {"id": investigation_id, "incident_id": incident_id, "sequence": sequence, "status": "queued"}

    def workbench(
        self,
        incident_id: str,
        *,
        team_ids: set[str] | None,
        actor_capabilities: list[str],
    ) -> dict[str, object] | None:
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self._clock()
            self._resolve_due_recoveries(conn, now)
            rows = self._visible_rows(conn, team_ids=team_ids, incident_id=incident_id)
            if not rows:
                return None
            row = rows[0]
            signals = conn.execute(
                """
                SELECT fingerprint, alertname, status, severity, summary,
                       workload_kind, workload_name, started_at, created_at, updated_at,
                       firing_webhook_request_id, recovered_webhook_request_id
                FROM alert_signals WHERE incident_id = ?
                ORDER BY created_at, id LIMIT 100
                """,
                (incident_id,),
            ).fetchall()
            investigation = conn.execute(
                """
                SELECT i.id, i.sequence, i.status, i.created_at, i.updated_at,
                       json_extract(request.result_json, '$.provider_revision') AS model_revision
                FROM investigations i
                LEFT JOIN diagnosis_requests request
                  ON request.investigation_id = i.id
                 AND request.status = 'accepted' AND request.result_json IS NOT NULL
                WHERE i.incident_id = ? ORDER BY i.sequence DESC LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
            recovery = conn.execute(
                "SELECT * FROM recovery_observations WHERE incident_id = ? ORDER BY observed_at DESC, rowid DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
            event_cursor = int(
                conn.execute(
                    "SELECT COALESCE(MAX(event_id), 0) FROM investigation_events WHERE investigation_id = ?",
                    (investigation["id"],) if investigation is not None else ("",),
                ).fetchone()[0]
            )
            decisions = project_evidence_decisions(
                conn,
                str(investigation["id"]) if investigation is not None else None,
                now=now,
            )
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
                "team_id": row["current_team_id"],
                "resource_binding_id": row["resource_binding_id"],
                "binding_revision": row["current_binding_revision"],
            },
            "alert_signals": [dict(signal) for signal in signals],
            "investigation": dict(investigation) if investigation is not None else None,
            **decisions,
            "recovery_observation": _recovery_row(recovery) if recovery is not None else None,
            "responsibility": {
                "status": "assigned" if row["current_team_id"] else "unassigned",
                "team_id": row["current_team_id"],
                "team_name": row["team_name"],
            },
            "actor_capabilities": sorted(set(actor_capabilities)),
            "event_cursor": event_cursor,
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

    def planning_facts(
        self, incident_id: str, *, team_ids: set[str] | None,
        desired_outcome: object = None,
    ) -> dict[str, object] | None:
        snapshot = self.workbench(incident_id, team_ids=team_ids, actor_capabilities=[])
        if snapshot is None:
            return None
        incident = snapshot["incident"]
        resource = snapshot["resource_context"]
        assert isinstance(incident, dict) and isinstance(resource, dict)
        incident_keys = ("id", "title", "severity", "status", "lifecycle_state", "binding_status", "evidence_revision")
        resource_keys = (
            "cluster_id", "environment", "runtime_status", "namespace", "workload_kind", "workload_name",
            "deployment_target_id", "service_id", "team_id", "resource_binding_id", "binding_revision",
        )
        evidence = [
            {key: step.get(key) for key in ("id", "purpose", "source", "scope", "state", "evidence_references")}
            for step in snapshot.get("evidence_steps", [])
            if isinstance(step, dict)
        ]
        matched_intents = {
            action.get("change_intent")
            for action in snapshot.get("recommended_actions", [])
            if isinstance(action, dict)
            and isinstance(desired_outcome, str)
            and action.get("summary") == desired_outcome
            and action.get("change_intent") in {"generic", "controlled_restart"}
        }
        return {
            "incident": {key: incident.get(key) for key in incident_keys},
            "resource": {key: resource.get(key) for key in resource_keys},
            "evidence_steps": evidence,
            "change_intent": matched_intents.pop() if len(matched_intents) == 1 else "generic",
        }

    def _update_signal(
        self, conn: sqlite3.Connection, existing: sqlite3.Row, signal: AlertSignal,
        now: float, webhook_request_id: str | None,
    ) -> dict[str, object]:
        incident_id = str(existing["incident_id"])
        previous_status = str(existing["status"])
        firing_request_id = webhook_request_id if signal.status == "firing" else existing["firing_webhook_request_id"]
        recovered_request_id = webhook_request_id if signal.status == "recovered" else existing["recovered_webhook_request_id"]
        conn.execute(
            """
            UPDATE alert_signals
            SET status = ?, severity = ?, summary = ?, workload_kind = ?, workload_name = ?,
                started_at = ?, updated_at = ?, firing_webhook_request_id = ?,
                recovered_webhook_request_id = ?
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
                firing_request_id,
                recovered_request_id,
                existing["id"],
            ),
        )
        incident = conn.execute("SELECT severity FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        previous_severity = str(incident["severity"])
        self._update_incident_severity(conn, incident_id, previous_severity, signal.severity, now)
        if previous_status == "recovered" and signal.status == "firing":
            self._cancel_recovery(conn, incident_id, now)
        elif previous_status == "firing" and signal.status == "recovered":
            self._start_recovery_if_ready(conn, incident_id, now)
        return {"accepted": True, "created": False, "incident": self._incident_in(conn, incident_id)}

    def _update_incident_severity(
        self, conn: sqlite3.Connection, incident_id: str, previous_severity: str, signal_severity: str, now: float
    ) -> None:
        severity = _max_severity(previous_severity, signal_severity)
        conn.execute(
            "UPDATE incidents SET severity = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (severity, now, incident_id),
        )
        if severity != previous_severity:
            enqueue_incident_event(
                conn, event_type="incident.severity_changed", incident_id=incident_id,
                now=now, previous_severity=previous_severity,
            )

    def _start_recovery_if_ready(self, conn: sqlite3.Connection, incident_id: str, now: float) -> None:
        firing = conn.execute(
            "SELECT 1 FROM alert_signals WHERE incident_id = ? AND status = 'firing' LIMIT 1",
            (incident_id,),
        ).fetchone()
        if firing is not None:
            return
        revision = int(conn.execute("SELECT evidence_revision FROM incidents WHERE id = ?", (incident_id,)).fetchone()[0]) + 1
        conn.execute(
            "INSERT INTO recovery_observations (id, incident_id, evidence_revision, observed_at, stabilizes_at) VALUES (?, ?, ?, ?, ?)",
            (self._id_factory("recovery"), incident_id, revision, now, now + self._stabilization_seconds),
        )
        conn.execute(
            "UPDATE incidents SET lifecycle_state = 'stabilizing', evidence_revision = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (revision, now, incident_id),
        )
        stale_incident_actions(conn, incident_id)
        self._resolve_due_recoveries(conn, now)

    def _cancel_recovery(self, conn: sqlite3.Connection, incident_id: str, now: float) -> None:
        changed = conn.execute(
            "UPDATE recovery_observations SET cancelled_at = ? WHERE incident_id = ? AND cancelled_at IS NULL AND resolved_at IS NULL",
            (now, incident_id),
        ).rowcount
        if changed:
            incident = conn.execute("SELECT reopened_at FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            state = "reopened" if incident["reopened_at"] is not None else "firing"
            conn.execute(
                "UPDATE incidents SET lifecycle_state = ?, evidence_revision = evidence_revision + 1, updated_at = ?, revision = revision + 1 WHERE id = ?",
                (state, now, incident_id),
            )
            stale_incident_actions(conn, incident_id)

    def reconcile_due(self) -> int:
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._resolve_due_recoveries(conn, self._clock())

    def _resolve_due_recoveries(self, conn: sqlite3.Connection, now: float) -> int:
        due = conn.execute(
            """
            SELECT ro.id, ro.incident_id, ro.stabilizes_at
            FROM recovery_observations ro JOIN incidents i ON i.id = ro.incident_id
            WHERE i.status = 'active' AND ro.cancelled_at IS NULL AND ro.resolved_at IS NULL
              AND ro.stabilizes_at <= ?
              AND NOT EXISTS (SELECT 1 FROM alert_signals a WHERE a.incident_id = ro.incident_id AND a.status = 'firing')
            """,
            (now,),
        ).fetchall()
        resolved = 0
        for observation in due:
            if _incident_has_blocking_change(conn, str(observation["incident_id"])):
                continue
            resolved_at = float(observation["stabilizes_at"])
            conn.execute("UPDATE recovery_observations SET resolved_at = ? WHERE id = ?", (resolved_at, observation["id"]))
            conn.execute(
                "UPDATE incidents SET status = 'resolved', lifecycle_state = 'resolved', resolved_at = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
                (resolved_at, resolved_at, observation["incident_id"]),
            )
            enqueue_incident_event(
                conn,
                event_type="incident.resolved",
                incident_id=str(observation["incident_id"]),
                now=resolved_at,
            )
            resolved += 1
        return resolved

    def _reopen_incident(self, conn: sqlite3.Connection, incident_id: str, now: float) -> None:
        conn.execute(
            "UPDATE incidents SET status = 'active', lifecycle_state = 'reopened', resolved_at = NULL, reopened_at = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (now, now, incident_id),
        )
        enqueue_incident_event(conn, event_type="incident.reopened", incident_id=incident_id, now=now)

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
    if len(normalized) > limit or (normalized and not normalized.isprintable()):
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


def _incident_has_blocking_change(conn: sqlite3.Connection, incident_id: str) -> bool:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kubernetes_change_executions'"
    ).fetchone() is None:
        return False
    return conn.execute(
        """
        SELECT 1
        FROM kubernetes_change_executions execution
        JOIN change_requests request ON request.id = execution.change_request_id
        WHERE request.incident_id = ?
          AND execution.status IN (
              'queued', 'dispatched', 'started', 'unknown_outcome',
              'cancel_requested', 'rolling_back'
          )
        LIMIT 1
        """,
        (incident_id,),
    ).fetchone() is not None


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
        "lifecycle_state": str(row["lifecycle_state"]),
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
        "evidence_revision": int(row["evidence_revision"]),
        "resolved_at": float(row["resolved_at"]) if row["resolved_at"] is not None else None,
        "reopened_at": float(row["reopened_at"]) if row["reopened_at"] is not None else None,
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _recovery_row(row: sqlite3.Row) -> dict[str, object]:
    status = "cancelled" if row["cancelled_at"] is not None else "resolved" if row["resolved_at"] is not None else "stabilizing"
    return {
        "id": str(row["id"]),
        "evidence_revision": int(row["evidence_revision"]),
        "status": status,
        "observed_at": float(row["observed_at"]),
        "stabilizes_at": float(row["stabilizes_at"]),
        "cancelled_at": float(row["cancelled_at"]) if row["cancelled_at"] is not None else None,
        "resolved_at": float(row["resolved_at"]) if row["resolved_at"] is not None else None,
    }


def _snapshot_revision(snapshot: dict[str, object]) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]
