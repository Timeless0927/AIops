"""Gateway-owned Resource Catalog module."""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .gateway_db import GatewayDatabase, insert_admin_audit, register_migrations


_SCHEMA_VERSION = 4
_SCHEMA = """
CREATE TABLE services (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    name TEXT NOT NULL CHECK (length(name) > 0),
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (team_id, name),
    FOREIGN KEY (team_id) REFERENCES teams(id)
);

CREATE TABLE discovery_candidates (
    id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL CHECK (length(namespace) > 0),
    workload_kind TEXT NOT NULL CHECK (length(workload_kind) > 0),
    workload_name TEXT NOT NULL CHECK (length(workload_name) > 0),
    service_name TEXT,
    service_hint TEXT,
    team_hint TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    UNIQUE (cluster_id, namespace, workload_kind, workload_name),
    FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id)
);

CREATE TABLE deployment_targets (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    workload_name TEXT NOT NULL,
    service_identity TEXT,
    created_at REAL NOT NULL,
    UNIQUE (cluster_id, namespace, workload_kind, workload_name),
    FOREIGN KEY (candidate_id) REFERENCES discovery_candidates(id),
    FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id)
);

CREATE TABLE resource_bindings (
    id TEXT PRIMARY KEY,
    deployment_target_id TEXT NOT NULL UNIQUE,
    service_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    confirmed_by TEXT NOT NULL,
    confirmed_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (deployment_target_id) REFERENCES deployment_targets(id),
    FOREIGN KEY (service_id) REFERENCES services(id),
    FOREIGN KEY (team_id) REFERENCES teams(id)
);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


@dataclass(frozen=True)
class DiscoveryObservation:
    namespace: str
    workload_kind: str
    workload_name: str
    service_name: str | None = None
    service_hint: str | None = None
    team_hint: str | None = None


class ResourceCatalogError(ValueError):
    def __init__(self, code: str, message: str, *, before: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.before = before


class ResourceCatalog:
    """Owns discovery candidates, Services, Deployment Targets and bindings."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def refresh_discovery(
        self,
        cluster_id: str,
        observations: list[DiscoveryObservation],
    ) -> list[dict[str, object]]:
        cluster_id = _required(cluster_id, "cluster_id")
        normalized = [_observation_values(observation) for observation in observations]
        now = self._clock()
        ids: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone() is None:
                raise ResourceCatalogError("cluster_not_registered", "Discovery requires a registered Cluster")
            for values in normalized:
                candidate_id = self._id_factory("candidate")
                conn.execute(
                    """
                    INSERT INTO discovery_candidates (
                        id, cluster_id, namespace, workload_kind, workload_name,
                        service_name, service_hint, team_hint, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cluster_id, namespace, workload_kind, workload_name) DO UPDATE SET
                        service_name = excluded.service_name,
                        service_hint = excluded.service_hint,
                        team_hint = excluded.team_hint,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (candidate_id, cluster_id, *values, now, now),
                )
                row = conn.execute(
                    """
                    SELECT id FROM discovery_candidates
                    WHERE cluster_id = ? AND namespace = ? AND workload_kind = ? AND workload_name = ?
                    """,
                    (cluster_id, *values[:3]),
                ).fetchone()
                ids.append(str(row["id"]))
            conn.commit()
        candidates = {str(row["id"]): row for row in self.list_state()["discovery_candidates"]}
        return [candidates[candidate_id] for candidate_id in ids]

    def create_service(
        self,
        *,
        team_id: str,
        name: str,
        description: str,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        team_id = _required(team_id, "team_id")
        name = _required(name, "name")
        now = self._clock()
        service = {
            "id": self._id_factory("service"),
            "team_id": team_id,
            "name": name,
            "description": description.strip(),
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM teams WHERE id = ? AND active = 1", (team_id,)).fetchone() is None:
                    raise ResourceCatalogError("team_not_found", "Service requires an active Team")
                conn.execute(
                    """
                    INSERT INTO services (id, team_id, name, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (service["id"], team_id, name, service["description"], now, now),
                )
                insert_admin_audit(
                    conn,
                    actor_id=actor_id,
                    target_type="services",
                    target_id=str(service["id"]),
                    action="services_create",
                    reason=_required(reason, "reason"),
                    before=None,
                    after=service,
                    result="success",
                    request_id=request_id,
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ResourceCatalogError("service_exists", "Service already exists for this Team") from exc
        return service

    def confirm_binding(
        self,
        *,
        candidate_id: str,
        service_id: str,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        candidate_id = _required(candidate_id, "candidate_id")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute("SELECT * FROM discovery_candidates WHERE id = ?", (candidate_id,)).fetchone()
            if candidate is None:
                raise ResourceCatalogError("candidate_not_found", "Discovery Candidate not found")
            service = _active_service(conn, service_id)
            now = self._clock()
            target_id = self._id_factory("target")
            binding_id = self._id_factory("binding")
            try:
                conn.execute(
                    """
                    INSERT INTO deployment_targets (
                        id, candidate_id, cluster_id, namespace, workload_kind,
                        workload_name, service_identity, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_id,
                        candidate_id,
                        candidate["cluster_id"],
                        candidate["namespace"],
                        candidate["workload_kind"],
                        candidate["workload_name"],
                        candidate["service_name"],
                        now,
                    ),
                )
                binding = _binding_record(
                    binding_id,
                    target_id,
                    service,
                    revision=1,
                    actor_id=actor_id,
                    now=now,
                )
                conn.execute(
                    """
                    INSERT INTO resource_bindings (
                        id, deployment_target_id, service_id, team_id, revision,
                        confirmed_by, confirmed_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (binding_id, target_id, service["id"], service["team_id"], actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ResourceCatalogError("binding_exists", "Discovery Candidate is already bound") from exc
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="resource-bindings",
                target_id=binding_id,
                action="resource-bindings_create",
                reason=_required(reason, "reason"),
                before=None,
                after=binding,
                result="success",
                request_id=request_id,
            )
            conn.commit()
        return binding

    def correct_binding(
        self,
        *,
        binding_id: str,
        service_id: str,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM resource_bindings WHERE id = ?", (binding_id,)).fetchone()
            if row is None:
                raise ResourceCatalogError("binding_not_found", "Resource Binding not found")
            before = _binding_from_row(row)
            try:
                service = _active_service(conn, service_id)
            except ResourceCatalogError as exc:
                raise ResourceCatalogError(exc.code, exc.message, before=before) from exc
            now = self._clock()
            revision = int(row["revision"]) + 1
            conn.execute(
                """
                UPDATE resource_bindings
                SET service_id = ?, team_id = ?, revision = ?, confirmed_by = ?, updated_at = ?
                WHERE id = ?
                """,
                (service["id"], service["team_id"], revision, actor_id, now, binding_id),
            )
            updated = conn.execute("SELECT * FROM resource_bindings WHERE id = ?", (binding_id,)).fetchone()
            after = _binding_from_row(updated)
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="resource-bindings",
                target_id=binding_id,
                action="resource-bindings_update",
                reason=_required(reason, "reason"),
                before=before,
                after=after,
                result="success",
                request_id=request_id,
            )
            conn.commit()
        return after

    def resolve_binding(
        self,
        cluster_id: str,
        namespace: str,
        workload_kind: str,
        workload_name: str,
    ) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT rb.* FROM resource_bindings rb
                JOIN deployment_targets dt ON dt.id = rb.deployment_target_id
                WHERE dt.cluster_id = ? AND dt.namespace = ?
                  AND dt.workload_kind = ? AND dt.workload_name = ?
                """,
                (cluster_id, namespace, workload_kind, workload_name),
            ).fetchone()
        return _binding_from_row(row) if row is not None else None

    def execution_target_error(self, target: dict[str, object]) -> ResourceCatalogError | None:
        cluster_id = str(target.get("cluster") or "").strip()
        namespace = str(target.get("namespace") or "").strip()
        deployment = str(target.get("deployment") or "").strip()
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone() is None:
                return None
            row = conn.execute(
                """
                SELECT rb.service_id, rb.team_id, s.name AS service_name, t.name AS team_name
                FROM resource_bindings rb
                JOIN deployment_targets dt ON dt.id = rb.deployment_target_id
                JOIN services s ON s.id = rb.service_id AND s.active = 1
                JOIN teams t ON t.id = rb.team_id AND t.active = 1
                WHERE dt.cluster_id = ? AND dt.namespace = ?
                  AND dt.workload_kind = 'Deployment' AND dt.workload_name = ?
                """,
                (cluster_id, namespace, deployment),
            ).fetchone()
        if row is None:
            return ResourceCatalogError("resource_unbound", "Deployment Target has no confirmed Resource Binding")
        if str(target.get("service") or "") not in {str(row["service_id"]), str(row["service_name"])} or str(
            target.get("team") or ""
        ) not in {str(row["team_id"]), str(row["team_name"])}:
            return ResourceCatalogError("resource_binding_mismatch", "action scope does not match the confirmed Resource Binding")
        return None

    def list_state(self) -> dict[str, list[dict[str, object]]]:
        with self._connect() as conn:
            candidates = conn.execute(
                """
                SELECT dc.*, dt.id AS deployment_target_id, rb.id AS resource_binding_id
                FROM discovery_candidates dc
                LEFT JOIN deployment_targets dt ON dt.candidate_id = dc.id
                LEFT JOIN resource_bindings rb ON rb.deployment_target_id = dt.id
                ORDER BY dc.cluster_id, dc.namespace, dc.workload_kind, dc.workload_name
                """
            ).fetchall()
            services = conn.execute("SELECT * FROM services ORDER BY name, id").fetchall()
            targets = conn.execute("SELECT * FROM deployment_targets ORDER BY cluster_id, namespace, workload_name").fetchall()
            bindings = conn.execute("SELECT * FROM resource_bindings ORDER BY confirmed_at, id").fetchall()
        return {
            "discovery_candidates": [
                {**dict(row), "binding_status": "bound" if row["resource_binding_id"] else "unbound"}
                for row in candidates
            ],
            "services": [{**dict(row), "active": bool(row["active"])} for row in services],
            "deployment_targets": [dict(row) for row in targets],
            "resource_bindings": [_binding_from_row(row) for row in bindings],
        }

    def _connect(self) -> sqlite3.Connection:
        return self._database.connect()


def _observation_values(observation: DiscoveryObservation) -> tuple[str, str, str, str | None, str | None, str | None]:
    return (
        _required(observation.namespace, "namespace"),
        _required(observation.workload_kind, "workload_kind"),
        _required(observation.workload_name, "workload_name"),
        _optional(observation.service_name),
        _optional(observation.service_hint),
        _optional(observation.team_hint),
    )


def _required(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResourceCatalogError("invalid_request", f"{field} is required")
    return value.strip()


def _optional(value: str | None) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _active_service(conn: sqlite3.Connection, service_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM services WHERE id = ? AND active = 1", (_required(service_id, "service_id"),)).fetchone()
    if row is None:
        raise ResourceCatalogError("service_not_found", "Resource Binding requires an active Service")
    return row


def _binding_record(
    binding_id: str,
    target_id: str,
    service: sqlite3.Row,
    *,
    revision: int,
    actor_id: str,
    now: float,
) -> dict[str, object]:
    return {
        "id": binding_id,
        "deployment_target_id": target_id,
        "service_id": str(service["id"]),
        "team_id": str(service["team_id"]),
        "revision": revision,
        "confirmed_by": actor_id,
        "confirmed_at": now,
        "updated_at": now,
    }


def _binding_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "deployment_target_id": str(row["deployment_target_id"]),
        "service_id": str(row["service_id"]),
        "team_id": str(row["team_id"]),
        "revision": int(row["revision"]),
        "confirmed_by": str(row["confirmed_by"]),
        "confirmed_at": float(row["confirmed_at"]),
        "updated_at": float(row["updated_at"]),
    }
