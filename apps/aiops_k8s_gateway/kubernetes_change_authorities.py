"""Explicit Kubernetes mutation Authority and scope matching."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .connector_enrollments import ConnectorEnrollments
from .gateway_db import GatewayDatabase, insert_admin_audit, register_migrations
from .resource_catalog import ResourceCatalog
from .v1_store import GatewayV1Store


_SCHEMA_VERSION = 23
_SCHEMA = """
CREATE TABLE kubernetes_change_authorities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    environment TEXT NOT NULL CHECK (environment IN ('prod', 'staging', 'dev', 'test')),
    scope_type TEXT NOT NULL CHECK (scope_type IN ('object', 'namespace', 'service', 'cluster')),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(user_id, environment, scope_type, scope_json)
);
CREATE INDEX kubernetes_change_authorities_by_user
    ON kubernetes_change_authorities(user_id, active, environment);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


class KubernetesChangeAuthorityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KubernetesChangeAuthorities:
    """Owns Authority grants and exact Object/Namespace/Service/Cluster scope checks."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        users: GatewayV1Store,
        enrollments: ConnectorEnrollments,
        catalog: ResourceCatalog,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._users = users
        self._enrollments = enrollments
        self._catalog = catalog
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def list(self) -> list[dict[str, object]]:
        with self._database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM kubernetes_change_authorities ORDER BY created_at, id"
            ).fetchall()
        return [_authority(row) for row in rows]

    def create(
        self,
        *,
        user_id: str,
        environment: str,
        scope_type: str,
        scope: dict[str, object],
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        user_id = _text(user_id, "user_id")
        environment = _environment(environment)
        scope_type, scope = _scope(scope_type, scope)
        reason = _text(reason, "reason")
        now = self._clock()
        authority_id = self._id_factory("kubernetes-authority")
        encoded_scope = _json(scope)
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._users.user_active_in(conn, user_id):
                raise KubernetesChangeAuthorityError("user_not_found", "Authority requires an active User")
            self._validate_scope_reference_in(conn, scope_type, scope)
            try:
                conn.execute(
                    """
                    INSERT INTO kubernetes_change_authorities (
                        id, user_id, environment, scope_type, scope_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (authority_id, user_id, environment, scope_type, encoded_scope, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise KubernetesChangeAuthorityError(
                    "authority_exists", "Kubernetes Change Authority already exists"
                ) from exc
            authority = self._get_in(conn, authority_id)
            insert_admin_audit(
                conn, actor_id=actor_id, target_type="kubernetes-change-authorities",
                target_id=authority_id, action="kubernetes-change-authorities_create",
                reason=reason, before=None, after=authority, result="success",
                request_id=request_id,
            )
            conn.commit()
        return authority

    def update(
        self,
        authority_id: str,
        *,
        active: bool,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> dict[str, object]:
        if not isinstance(active, bool):
            raise KubernetesChangeAuthorityError("invalid_request", "active must be boolean")
        reason = _text(reason, "reason")
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before = self._get_in(conn, authority_id)
            conn.execute(
                "UPDATE kubernetes_change_authorities SET active = ?, updated_at = ? WHERE id = ?",
                (int(active), self._clock(), authority_id),
            )
            after = self._get_in(conn, authority_id)
            insert_admin_audit(
                conn, actor_id=actor_id, target_type="kubernetes-change-authorities",
                target_id=authority_id, action="kubernetes-change-authorities_update",
                reason=reason, before=before, after=after, result="success",
                request_id=request_id,
            )
            conn.commit()
        return after

    def authorize_proposal(self, facts: dict[str, object], *, actor_id: str) -> None:
        resource = facts.get("resource")
        if not isinstance(resource, dict):
            raise KubernetesChangeAuthorityError(
                "proposal_forbidden", "Change Authority does not cover this proposal"
            )
        cluster_id = str(resource.get("cluster_id") or "")
        with self._database.connect() as conn:
            environment = self._enrollments.cluster_environment_in(conn, cluster_id)
            if (
                environment is None
                or environment != resource.get("environment")
                or not any(
                    self._intersects_cluster_in(conn, row, cluster_id)
                    for row in self._active_in(conn, actor_id=actor_id, environment=environment)
                )
            ):
                raise KubernetesChangeAuthorityError(
                    "proposal_forbidden", "Change Authority does not cover this proposal"
                )

    def authorize_draft_plan(
        self,
        facts: dict[str, object],
        *,
        actor_id: str,
        plan: dict[str, object],
    ) -> bool:
        resource = facts.get("resource")
        changes = plan.get("changes")
        if not isinstance(resource, dict) or not isinstance(changes, list):
            return False
        targets = [change.get("target") if isinstance(change, dict) else None for change in changes]
        if not targets or not all(isinstance(target, dict) for target in targets):
            return False
        cluster_id = str(resource.get("cluster_id") or "")
        with self._database.connect() as conn:
            environment = self._enrollments.cluster_environment_in(conn, cluster_id)
            return bool(environment) and environment == resource.get("environment") and self.matching_ids_in(
                conn, actor_id=actor_id, environment=str(environment), cluster_id=cluster_id,
                targets=targets,  # type: ignore[arg-type]
            ) is not None

    def matching_ids_in(
        self,
        conn: sqlite3.Connection,
        *,
        actor_id: str,
        environment: str,
        cluster_id: str,
        targets: list[dict[str, object]],
    ) -> list[str] | None:
        rows = self._active_in(conn, actor_id=actor_id, environment=environment)
        matched: list[str] = []
        for target in targets:
            match = next(
                (row for row in rows if self._covers_in(conn, row, cluster_id=cluster_id, target=target)),
                None,
            )
            if match is None:
                return None
            matched.append(str(match["id"]))
        return matched

    def targets_authorized_in(
        self,
        conn: sqlite3.Connection,
        *,
        actor_id: str,
        environment: str,
        cluster_id: str,
        targets: list[dict[str, object]],
    ) -> bool:
        return self.matching_ids_in(
            conn, actor_id=actor_id, environment=environment,
            cluster_id=cluster_id, targets=targets,
        ) is not None

    def _active_in(
        self, conn: sqlite3.Connection, *, actor_id: str, environment: str,
    ) -> list[sqlite3.Row]:
        if not self._users.user_active_in(conn, actor_id):
            return []
        return conn.execute(
            """
            SELECT * FROM kubernetes_change_authorities
            WHERE user_id = ? AND environment = ? AND active = 1 ORDER BY created_at, id
            """,
            (actor_id, environment),
        ).fetchall()

    def _intersects_cluster_in(
        self, conn: sqlite3.Connection, row: sqlite3.Row, cluster_id: str,
    ) -> bool:
        scope = json.loads(str(row["scope_json"]))
        if row["scope_type"] == "service":
            return self._catalog.service_bound_to_cluster_in(
                conn, service_id=str(scope["service_id"]), cluster_id=cluster_id,
            )
        return scope["cluster_id"] == cluster_id

    def _covers_in(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        cluster_id: str,
        target: dict[str, object],
    ) -> bool:
        scope = json.loads(str(row["scope_json"]))
        scope_type = str(row["scope_type"])
        if scope_type == "cluster":
            return scope["cluster_id"] == cluster_id
        if scope_type == "namespace":
            return scope["cluster_id"] == cluster_id and scope["namespace"] == target.get("namespace")
        if scope_type == "object":
            return scope == {
                "cluster_id": cluster_id, "api_version": target.get("api_version"),
                "kind": target.get("kind"), "namespace": target.get("namespace"),
                "name": target.get("name"),
            }
        return self._catalog.service_covers_target_in(
            conn, service_id=str(scope["service_id"]), cluster_id=cluster_id,
            namespace=target.get("namespace") if isinstance(target.get("namespace"), str) else None,
            workload_kind=str(target.get("kind") or ""), workload_name=str(target.get("name") or ""),
        )

    def _validate_scope_reference_in(
        self, conn: sqlite3.Connection, scope_type: str, scope: dict[str, object],
    ) -> None:
        exists = (
            self._catalog.service_active_in(conn, str(scope["service_id"]))
            if scope_type == "service"
            else self._enrollments.cluster_environment_in(conn, str(scope["cluster_id"])) is not None
        )
        if not exists:
            code = "service_not_found" if scope_type == "service" else "cluster_not_found"
            raise KubernetesChangeAuthorityError(code, "Authority scope reference not found")

    def _get_in(self, conn: sqlite3.Connection, authority_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT * FROM kubernetes_change_authorities WHERE id = ?", (authority_id,),
        ).fetchone()
        if row is None:
            raise KubernetesChangeAuthorityError(
                "authority_not_found", "Kubernetes Change Authority not found"
            )
        return _authority(row)


def _scope(scope_type: object, raw_scope: object) -> tuple[str, dict[str, object]]:
    if scope_type not in {"object", "namespace", "service", "cluster"} or not isinstance(raw_scope, dict):
        raise KubernetesChangeAuthorityError("invalid_authority", "Authority scope is invalid")
    required = {
        "object": {"cluster_id", "api_version", "kind", "namespace", "name"},
        "namespace": {"cluster_id", "namespace"},
        "service": {"service_id"},
        "cluster": {"cluster_id"},
    }[scope_type]
    if set(raw_scope) != required:
        raise KubernetesChangeAuthorityError("invalid_authority", "Authority scope fields are invalid")
    scope: dict[str, object] = {}
    for field in required:
        value = raw_scope[field]
        if field == "namespace" and scope_type == "object" and value is None:
            scope[field] = None
        else:
            scope[field] = _text(value, field)
    return str(scope_type), scope


def _authority(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]), "user_id": str(row["user_id"]),
        "environment": str(row["environment"]), "scope_type": str(row["scope_type"]),
        "scope": json.loads(str(row["scope_json"])), "active": bool(row["active"]),
        "created_at": float(row["created_at"]), "updated_at": float(row["updated_at"]),
    }


def _environment(value: object) -> str:
    normalized = _text(value, "environment")
    if normalized not in {"prod", "staging", "dev", "test"}:
        raise KubernetesChangeAuthorityError("invalid_authority", "Authority Environment is invalid")
    return normalized


def _text(value: object, field: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > 500:
        raise KubernetesChangeAuthorityError("invalid_request", f"{field} is required")
    return normalized


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
