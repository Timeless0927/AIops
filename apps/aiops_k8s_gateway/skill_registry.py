"""Gateway-owned registry for versioned, non-executable Skills."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .gateway_audit import insert_admin_audit
from .gateway_db import GatewayDatabase, register_migrations


JSON = dict[str, object]
_SCHEMA_VERSION = 50
_SCHEMA = """
CREATE TABLE skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active_version INTEGER CHECK (active_version IS NULL OR active_version > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE skill_versions (
    skill_id TEXT NOT NULL REFERENCES skills(id),
    version INTEGER NOT NULL CHECK (version > 0),
    instruction TEXT NOT NULL,
    workflow_json TEXT NOT NULL CHECK (json_valid(workflow_json)),
    applicable_scope_json TEXT NOT NULL CHECK (json_valid(applicable_scope_json)),
    required_mcp_json TEXT NOT NULL CHECK (json_valid(required_mcp_json)),
    created_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (skill_id, version)
);
CREATE INDEX skills_by_active_version ON skills(active_version, created_at);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))
_DEPENDENCY_ERRORS = {
    "mcp_snapshot_unavailable": "MCP Integration snapshot is unavailable",
    "mcp_integration_missing": "Required MCP Integration is missing",
    "mcp_integration_changed": "Required MCP Integration is unavailable or changed",
    "mcp_capability_changed": "Required MCP capability is unavailable or changed",
    "mcp_scope_not_covered": "Required MCP Integration does not cover the Skill scope",
}


class SkillRegistryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SkillRegistry:
    """Owns immutable Skill versions and the exact enabled-version pointer."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"skill-{uuid.uuid4().hex}")

    @property
    def database(self) -> GatewayDatabase:
        return self._database

    def list(self, mcp_integrations: object = None) -> list[JSON]:
        with self._database.connect() as conn:
            rows = conn.execute("SELECT * FROM skills ORDER BY created_at, id").fetchall()
            return [_project(conn, row, mcp_integrations=mcp_integrations) for row in rows]

    def get(self, skill_id: str, mcp_integrations: object = None) -> JSON:
        with self._database.connect() as conn:
            return _project(conn, _skill(conn, skill_id), mcp_integrations=mcp_integrations)

    def create(
        self,
        *,
        name: object,
        instruction: object,
        workflow: object,
        applicable_scope: object,
        required_mcp: object,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> JSON:
        name_value = _text(name, "name", 200)
        content = _content(instruction, workflow, applicable_scope, required_mcp)
        actor = _text(actor_id, "actor_id", 500)
        reason_value = _text(reason, "reason", 1_000)
        request = _text(request_id, "request_id", 500)
        skill_id = self._id_factory()
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO skills (id, name, active_version, created_at, updated_at) "
                    "VALUES (?, ?, NULL, ?, ?)",
                    (skill_id, name_value, now, now),
                )
                _insert_version(conn, skill_id, 1, content, actor, reason_value, now)
            except sqlite3.IntegrityError as exc:
                raise SkillRegistryError("skill_conflict", "Skill already exists") from exc
            result = _project(conn, _skill(conn, skill_id))
            _audit(
                conn, actor_id=actor, skill_id=skill_id, action="skill_create",
                reason=reason_value, before=None, after={"name": name_value, "version": 1},
                result="success", request_id=request,
            )
            conn.commit()
        return result

    def create_version(
        self,
        skill_id: str,
        *,
        instruction: object,
        workflow: object,
        applicable_scope: object,
        required_mcp: object,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> JSON:
        content = _content(instruction, workflow, applicable_scope, required_mcp)
        actor = _text(actor_id, "actor_id", 500)
        reason_value = _text(reason, "reason", 1_000)
        request = _text(request_id, "request_id", 500)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            skill = _skill(conn, skill_id)
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM skill_versions WHERE skill_id = ?",
                (skill_id,),
            ).fetchone()[0])
            _insert_version(conn, skill_id, version, content, actor, reason_value, now)
            conn.execute("UPDATE skills SET updated_at = ? WHERE id = ?", (now, skill_id))
            result = _project(conn, skill)
            _audit(
                conn, actor_id=actor, skill_id=skill_id, action="skill_version_create",
                reason=reason_value, before=None, after={"version": version},
                result="success", request_id=request,
            )
            conn.commit()
        return result

    def set_enabled(
        self,
        skill_id: str,
        *,
        version: object,
        expected_active_version: object,
        mcp_integrations: object,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> JSON:
        if version is not None and (not isinstance(version, int) or isinstance(version, bool) or version < 1):
            raise SkillRegistryError("invalid_skill", "Skill version is invalid")
        if expected_active_version is not None and (
            not isinstance(expected_active_version, int)
            or isinstance(expected_active_version, bool)
            or expected_active_version < 1
        ):
            raise SkillRegistryError("invalid_skill", "Expected active Skill version is invalid")
        actor = _text(actor_id, "actor_id", 500)
        reason_value = _text(reason, "reason", 1_000)
        request = _text(request_id, "request_id", 500)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _skill(conn, skill_id)
            active = int(row["active_version"]) if row["active_version"] is not None else None
            if active != expected_active_version:
                raise SkillRegistryError("version_conflict", "Active Skill version changed")
            if version is not None:
                selected = conn.execute(
                    "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?",
                    (skill_id, version),
                ).fetchone()
                if selected is None:
                    raise SkillRegistryError("version_not_found", "Skill version not found")
                denial = _dependency_denial(selected, mcp_integrations)
                if denial is not None:
                    _audit(
                        conn, actor_id=actor, skill_id=skill_id,
                        action="skill_enable_denied", reason=reason_value,
                        before={"active_version": active},
                        after={"version": version, "denial": denial},
                        result="rejected", request_id=request,
                    )
                    conn.commit()
                    raise SkillRegistryError("skill_dependency_unavailable", _DEPENDENCY_ERRORS[denial])
            conn.execute(
                "UPDATE skills SET active_version = ?, updated_at = ? WHERE id = ?",
                (version, now, skill_id),
            )
            result = _project(conn, _skill(conn, skill_id))
            _audit(
                conn, actor_id=actor, skill_id=skill_id,
                action="skill_enable" if version is not None else "skill_disable",
                reason=reason_value, before={"active_version": active},
                after={"active_version": version}, result="success", request_id=request,
            )
            conn.commit()
        return result

    def authorized_bindings(
        self,
        scope: object,
        capability_snapshot: object,
        *,
        actor_id: str,
        request_id: str,
    ) -> list[JSON]:
        resources = _runtime_resources(scope)
        actor = _text(actor_id, "actor_id", 500)
        request = _text(request_id, "request_id", 500)
        if resources is None:
            return []
        bindings: list[JSON] = []
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT skills.id, skills.name, skill_versions.*
                FROM skills
                JOIN skill_versions
                  ON skill_versions.skill_id = skills.id
                 AND skill_versions.version = skills.active_version
                ORDER BY skills.created_at, skills.id
                """
            ).fetchall()
            for row in rows:
                applicable = json.loads(str(row["applicable_scope_json"]))
                if not _scope_covers(applicable, resources):
                    continue
                requirements = json.loads(str(row["required_mcp_json"]))
                denial = _runtime_dependency_denial(requirements, capability_snapshot)
                if denial is not None:
                    _audit(
                        conn, actor_id=actor, skill_id=str(row["id"]),
                        action="skill_use_denied", reason=denial, before=None,
                        after={"version": int(row["version"])}, result="rejected",
                        request_id=request,
                    )
                    continue
                bindings.append({
                    "id": str(row["id"]),
                    "name": str(row["name"]),
                    "version": int(row["version"]),
                    "instruction": str(row["instruction"]),
                    "workflow": json.loads(str(row["workflow_json"])),
                    "required_mcp": requirements,
                })
                _audit(
                    conn, actor_id=actor, skill_id=str(row["id"]),
                    action="skill_use", reason="authorized_binding", before=None,
                    after={"version": int(row["version"])}, result="success",
                    request_id=request,
                )
            conn.commit()
        return bindings


def _content(
    instruction: object,
    workflow: object,
    applicable_scope: object,
    required_mcp: object,
) -> JSON:
    steps = _workflow(workflow)
    instruction_value = _text(instruction, "instruction", 8_000, allow_empty=True)
    if not instruction_value and not steps:
        raise SkillRegistryError("invalid_skill", "Skill requires instruction or workflow content")
    return {
        "instruction": instruction_value,
        "workflow": steps,
        "applicable_scope": _scope(applicable_scope),
        "required_mcp": _requirements(required_mcp),
    }


def _workflow(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise SkillRegistryError("invalid_skill", "Skill workflow must be a bounded list")
    return [_text(item, "workflow step", 1_000) for item in value]


def _scope(value: object) -> list[JSON]:
    if not isinstance(value, list) or not value:
        raise SkillRegistryError("invalid_skill", "Skill applicable scope is required")
    normalized: dict[str, JSON] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"cluster_id", "namespace"}:
            raise SkillRegistryError("invalid_skill", "Skill applicable scope fields are invalid")
        namespace = item["namespace"]
        if namespace is not None:
            namespace = _text(namespace, "namespace", 253)
        entry = {"cluster_id": _text(item["cluster_id"], "cluster_id", 253), "namespace": namespace}
        normalized[_json(entry)] = entry
    return [normalized[key] for key in sorted(normalized)]


def _requirements(value: object) -> list[JSON]:
    if not isinstance(value, list) or len(value) > 32:
        raise SkillRegistryError("invalid_skill", "Skill required MCP references must be a bounded list")
    normalized: dict[str, JSON] = {}
    fields = {"integration_id", "integration_revision", "name", "version"}
    for item in value:
        if not isinstance(item, dict) or set(item) != fields:
            raise SkillRegistryError("invalid_skill", "Skill required MCP reference fields are invalid")
        entry = {field: _text(item[field], field, 500 if field.startswith("integration_") else 200) for field in fields}
        key = f"{entry['integration_id']}\0{entry['name']}"
        if key in normalized:
            raise SkillRegistryError("invalid_skill", "Skill required MCP references must be unique")
        normalized[key] = entry
    return [normalized[key] for key in sorted(normalized)]


def _insert_version(
    conn: sqlite3.Connection,
    skill_id: str,
    version: int,
    content: JSON,
    actor_id: str,
    reason: str,
    created_at: float,
) -> None:
    conn.execute(
        """
        INSERT INTO skill_versions (
            skill_id, version, instruction, workflow_json, applicable_scope_json,
            required_mcp_json, created_by, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            skill_id, version, content["instruction"], _json(content["workflow"]),
            _json(content["applicable_scope"]), _json(content["required_mcp"]),
            actor_id, reason, created_at,
        ),
    )


def _dependency_denial(version: sqlite3.Row, integrations: object) -> str | None:
    return _dependency_denial_for(
        json.loads(str(version["applicable_scope_json"])),
        json.loads(str(version["required_mcp_json"])),
        integrations,
    )


def _dependency_denial_for(scope: list[JSON], requirements: list[JSON], integrations: object) -> str | None:
    if not isinstance(integrations, list):
        return "mcp_snapshot_unavailable"
    by_id = {
        item.get("id"): item
        for item in integrations
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for requirement in requirements:
        integration = by_id.get(requirement["integration_id"])
        if not isinstance(integration, dict):
            return "mcp_integration_missing"
        verification = integration.get("verification")
        health = integration.get("health")
        if (
            integration.get("enabled") is not True
            or integration.get("revision") != requirement["integration_revision"]
            or not isinstance(verification, dict)
            or verification.get("state") != "verified"
            or verification.get("verified_revision") != integration.get("revision")
            or not isinstance(health, dict)
            or health.get("status") != "ok"
        ):
            return "mcp_integration_changed"
        capabilities = integration.get("capability_snapshot")
        capability = next((
            item for item in capabilities
            if isinstance(item, dict) and item.get("name") == requirement["name"]
        ), None) if isinstance(capabilities, list) else None
        if (
            not isinstance(capability, dict)
            or capability.get("version") != requirement["version"]
            or capability.get("read_only") is not True
            or capability.get("mutation") is not False
        ):
            return "mcp_capability_changed"
        allowed_scope = integration.get("allowed_scope")
        if not isinstance(allowed_scope, list) or not _scope_covers(allowed_scope, scope):
            return "mcp_scope_not_covered"
    return None


def _scope_covers(allowed: list[object], requested: list[object]) -> bool:
    return all(
        isinstance(resource, dict)
        and any(
            isinstance(entry, dict)
            and entry.get("cluster_id") == resource.get("cluster_id")
            and (
                entry.get("namespace") is None
                or entry.get("namespace") == resource.get("namespace")
            )
            for entry in allowed
        )
        for resource in requested
    )


def _runtime_resources(scope: object) -> list[JSON] | None:
    if not isinstance(scope, dict) or not isinstance(scope.get("resources"), list) or not scope["resources"]:
        return None
    resources: list[JSON] = []
    for item in scope["resources"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("cluster_id"), str)
            or not isinstance(item.get("namespace"), str)
        ):
            return None
        resources.append({"cluster_id": item["cluster_id"], "namespace": item["namespace"]})
    return resources


def _runtime_dependency_denial(requirements: list[JSON], snapshot: object) -> str | None:
    if not isinstance(snapshot, dict):
        return "required_mcp_unavailable"
    for requirement in requirements:
        capability = snapshot.get(requirement["name"])
        if not isinstance(capability, dict) or any(
            capability.get(field) != requirement[expected]
            for field, expected in (
                ("integration_id", "integration_id"),
                ("integration_revision", "integration_revision"),
                ("name", "name"),
                ("version", "version"),
            )
        ) or capability.get("enabled") is not True or capability.get("read_only") is not True or capability.get("mutation") is not False:
            return "required_mcp_unavailable"
    return None


def _skill(conn: sqlite3.Connection, skill_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if row is None:
        raise SkillRegistryError("skill_not_found", "Skill not found")
    return row


def _project(conn: sqlite3.Connection, row: sqlite3.Row, *, mcp_integrations: object = None) -> JSON:
    versions = [
        {
            "version": int(item["version"]),
            "instruction": str(item["instruction"]),
            "workflow": json.loads(str(item["workflow_json"])),
            "applicable_scope": json.loads(str(item["applicable_scope_json"])),
            "required_mcp": json.loads(str(item["required_mcp_json"])),
            "created_by": str(item["created_by"]),
            "reason": str(item["reason"]),
            "created_at": float(item["created_at"]),
        }
        for item in conn.execute(
            "SELECT * FROM skill_versions WHERE skill_id = ? ORDER BY version",
            (row["id"],),
        )
    ]
    active = int(row["active_version"]) if row["active_version"] is not None else None
    result: JSON = {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "enabled": active is not None,
        "active_version": active,
        "latest_version": int(versions[-1]["version"]),
        "versions": versions,
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }
    if mcp_integrations is not None:
        for version in versions:
            denial = _dependency_denial_for(
                version["applicable_scope"], version["required_mcp"], mcp_integrations,
            )
            version["dependency"] = {
                "state": "ready" if denial is None else "unavailable",
                "reason_code": denial,
            }
        result["availability"] = (
            next(version["dependency"] for version in versions if version["version"] == active)
            if active is not None
            else {"state": "disabled", "reason_code": "skill_disabled"}
        )
    return result


def _audit(
    conn: sqlite3.Connection,
    *,
    actor_id: str,
    skill_id: str,
    action: str,
    reason: str,
    before: JSON | None,
    after: JSON | None,
    result: str,
    request_id: str,
) -> None:
    insert_admin_audit(
        conn, actor_id=actor_id, target_type="skill", target_id=skill_id,
        action=action, reason=reason, before=before, after=after,
        result=result, request_id=request_id,
    )


def _text(value: object, field: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SkillRegistryError("invalid_skill", f"{field} must be text")
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > limit or "\x00" in normalized:
        raise SkillRegistryError("invalid_skill", f"{field} is invalid")
    return normalized


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
