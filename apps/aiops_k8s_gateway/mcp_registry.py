"""Gateway-owned registry for administrator-governed MCP Integrations."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

from aiops.contracts.governed_tools import approved_capability
from aiops.security.secure_input import (
    decrypt_secure_input_refs,
    encrypt_secure_input,
    read_change_encryption_key,
    secure_input_placeholder,
)
from apps.internal_auth import internal_auth_headers
from apps.service_http import read_bounded_json

from .gateway_db import GatewayDatabase, insert_admin_audit, register_migrations


JSON = dict[str, object]
Probe = Callable[[str, str | None, str], JSON]
ToolSender = Callable[[str, str, str | None, JSON, str], JSON]
_SCHEMA_VERSION = 49
_SCHEMA = """
CREATE TABLE mcp_integrations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    credential_nonce BLOB,
    credential_ciphertext BLOB,
    credential_sha256 TEXT,
    credential_key_fingerprint TEXT,
    capabilities_json TEXT NOT NULL CHECK (json_valid(capabilities_json)),
    allowed_scope_json TEXT NOT NULL CHECK (json_valid(allowed_scope_json)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    revision TEXT NOT NULL,
    verification_state TEXT NOT NULL CHECK (verification_state IN ('unverified', 'verified', 'failed')),
    verification_reason TEXT,
    verified_revision TEXT,
    capability_snapshot_json TEXT CHECK (capability_snapshot_json IS NULL OR json_valid(capability_snapshot_json)),
    verified_snapshot_json TEXT CHECK (verified_snapshot_json IS NULL OR json_valid(verified_snapshot_json)),
    health_status TEXT NOT NULL CHECK (health_status IN ('unknown', 'ok', 'unavailable')),
    health_error TEXT,
    health_checked_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK ((credential_nonce IS NULL) = (credential_ciphertext IS NULL)),
    CHECK ((credential_nonce IS NULL) = (credential_sha256 IS NULL)),
    CHECK ((credential_nonce IS NULL) = (credential_key_fingerprint IS NULL))
);
CREATE INDEX mcp_integrations_by_enabled ON mcp_integrations(enabled, verification_state);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


class MCPRegistryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MCPRegistry:
    """Owns exact MCP configuration, verification, scope and use decisions."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        key_path: Path | str | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        revision_id: Callable[[], str] | None = None,
        nonce_source: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._key_path = Path(key_path or os.getenv("AIOPS_MCP_ENCRYPTION_KEY_PATH", "/var/run/secrets/aiops-mcp/key"))
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"mcp-{uuid.uuid4().hex}")
        self._revision_id = revision_id or (lambda: f"mcp-revision:{uuid.uuid4().hex}")
        self._nonce_source = nonce_source

    @property
    def database(self) -> GatewayDatabase:
        return self._database

    def list(self) -> list[JSON]:
        with self._database.connect() as conn:
            rows = conn.execute("SELECT * FROM mcp_integrations ORDER BY created_at, id").fetchall()
        return [_project(row) for row in rows]

    def get(self, integration_id: str) -> JSON:
        with self._database.connect() as conn:
            return _project(self._get_in(conn, integration_id))

    def create(
        self,
        *,
        name: object,
        endpoint: object,
        credential: object,
        capabilities: object,
        allowed_scope: object,
        enabled: object,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> JSON:
        values = _configuration(name, endpoint, capabilities, allowed_scope, enabled)
        reason = _text(reason, "reason", 1_000)
        integration_id = self._id_factory()
        revision = self._revision_id()
        encrypted = self._encrypt(integration_id, credential)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO mcp_integrations (
                        id, name, endpoint, credential_nonce, credential_ciphertext,
                        credential_sha256, credential_key_fingerprint, capabilities_json,
                        allowed_scope_json, enabled, revision, verification_state,
                        verification_reason, health_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unverified',
                              'verification_required', 'unknown', ?, ?)
                    """,
                    (
                        integration_id, values["name"], values["endpoint"], *encrypted,
                        _json(values["capabilities"]), _json(values["allowed_scope"]),
                        int(values["enabled"]), revision, now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MCPRegistryError("integration_conflict", "MCP Integration already exists") from exc
            after = _project(self._get_in(conn, integration_id))
            self._audit(
                conn, actor_id=actor_id, integration_id=integration_id,
                action="mcp_integration_create", reason=reason, before=None,
                after=after, result="success", request_id=request_id,
            )
            conn.commit()
        return after

    def update(
        self,
        integration_id: str,
        *,
        name: object,
        endpoint: object,
        credential: object,
        capabilities: object,
        allowed_scope: object,
        enabled: object,
        expected_revision: object,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> JSON:
        values = _configuration(name, endpoint, capabilities, allowed_scope, enabled)
        reason = _text(reason, "reason", 1_000)
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_in(conn, integration_id)
            if expected_revision != row["revision"]:
                raise MCPRegistryError("revision_conflict", "MCP Integration revision changed")
            encrypted = (
                (row["credential_nonce"], row["credential_ciphertext"], row["credential_sha256"], row["credential_key_fingerprint"])
                if credential is None else self._encrypt(integration_id, credential)
            )
            before = _project(row)
            conn.execute(
                """
                UPDATE mcp_integrations
                SET name = ?, endpoint = ?, credential_nonce = ?, credential_ciphertext = ?,
                    credential_sha256 = ?, credential_key_fingerprint = ?, capabilities_json = ?,
                    allowed_scope_json = ?, enabled = ?, revision = ?, verification_state = 'unverified',
                    verification_reason = 'verification_required', verified_revision = NULL,
                    capability_snapshot_json = NULL, health_status = 'unknown', health_error = NULL,
                    health_checked_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    values["name"], values["endpoint"], *encrypted,
                    _json(values["capabilities"]), _json(values["allowed_scope"]),
                    int(values["enabled"]), self._revision_id(), self._clock(), integration_id,
                ),
            )
            after = _project(self._get_in(conn, integration_id))
            action = (
                f"mcp_integration_{'enable' if after['enabled'] else 'disable'}"
                if before["enabled"] != after["enabled"] else "mcp_integration_update"
            )
            self._audit(
                conn, actor_id=actor_id, integration_id=integration_id, action=action,
                reason=reason, before=before, after=after, result="success", request_id=request_id,
            )
            conn.commit()
        return after

    def verify(
        self,
        integration_id: str,
        *,
        actor_id: str,
        reason: str,
        request_id: str,
        probe: Probe | None = None,
    ) -> JSON:
        reason = _text(reason, "reason", 1_000)
        with self._database.connect() as conn:
            row = self._get_in(conn, integration_id)
            before = _project(row)
        health_status = "unavailable"
        health_error: str | None = "integration_unavailable"
        observed: list[JSON] | None = None
        verification_state = "failed"
        verification_reason = "health_unavailable"
        try:
            credential = self._decrypt(row)
            response = (probe or _probe)(str(row["endpoint"]), credential, request_id)
            health_status, health_error, observed = _health(response)
            if health_status == "ok":
                verification_reason = _snapshot_denial(json.loads(str(row["capabilities_json"])), observed)
                verification_state = "verified" if verification_reason is None else "failed"
        except Exception:
            # Never persist provider exceptions: their text may include credentials or response bodies.
            pass
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._get_in(conn, integration_id)
            if current["revision"] != row["revision"]:
                raise MCPRegistryError("revision_conflict", "MCP Integration revision changed during verification")
            conn.execute(
                """
                UPDATE mcp_integrations
                SET verification_state = ?, verification_reason = ?, verified_revision = ?,
                    capability_snapshot_json = ?,
                    verified_snapshot_json = CASE WHEN ? = 'verified' THEN ? ELSE verified_snapshot_json END,
                    health_status = ?, health_error = ?, health_checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    verification_state, verification_reason,
                    str(row["revision"]) if verification_state == "verified" else None,
                    _json(observed) if observed is not None else None,
                    verification_state, _json(observed) if observed is not None else None,
                    health_status, health_error, now, now, integration_id,
                ),
            )
            after = _project(self._get_in(conn, integration_id))
            self._audit(
                conn, actor_id=actor_id, integration_id=integration_id,
                action="mcp_integration_verify", reason=reason, before=before, after=after,
                result="success" if verification_state == "verified" else "rejected",
                request_id=request_id,
            )
            conn.commit()
        return after

    def authorized_snapshot(self, scope: object, *, actor_id: str, request_id: str) -> JSON:
        resources = _resources(scope)
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM mcp_integrations ORDER BY created_at, id").fetchall()
            candidates: dict[str, list[tuple[sqlite3.Row, JSON]]] = {}
            denied: list[tuple[sqlite3.Row, str]] = []
            for row in rows:
                reason = _availability_denial(row, resources)
                if reason is not None:
                    denied.append((row, reason))
                    continue
                for capability in json.loads(str(row["capability_snapshot_json"])):
                    candidates.setdefault(str(capability["name"]), []).append((row, capability))
            snapshot: JSON = {}
            for name, values in candidates.items():
                if len(values) != 1:
                    denied.extend((row, "ambiguous_capability_owner") for row, _capability in values)
                    continue
                row, capability = values[0]
                snapshot[name] = {
                    "name": name, "version": capability["version"], "enabled": True,
                    "read_only": True, "mutation": False, "integration_id": str(row["id"]),
                    "integration_revision": str(row["revision"]),
                }
                self._audit(
                    conn, actor_id=actor_id, integration_id=str(row["id"]),
                    action="mcp_integration_use", reason="authorized_snapshot",
                    before=None, after={"capability": name}, result="success", request_id=request_id,
                )
            for row, denial in denied:
                self._audit(
                    conn, actor_id=actor_id, integration_id=str(row["id"]),
                    action="mcp_integration_use_denied", reason=denial,
                    before=None, after=None, result="rejected", request_id=request_id,
                )
            conn.commit()
        return snapshot

    def invoke(
        self,
        tool: str,
        arguments: object,
        *,
        integration_id: str,
        integration_revision: str,
        actor_id: str,
        request_id: str,
        send: ToolSender | None = None,
    ) -> JSON:
        if not isinstance(arguments, dict):
            raise MCPRegistryError("invalid_request", "MCP tool arguments must be an object")
        resources = _resources({"resources": [arguments]})
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_in(conn, integration_id)
                denial = (
                    "integration_revision_changed"
                    if row["revision"] != integration_revision
                    else _availability_denial(row, resources)
                )
                snapshot = json.loads(str(row["capability_snapshot_json"])) if row["capability_snapshot_json"] else []
                capability = next((item for item in snapshot if item.get("name") == tool), None)
                if denial is None and capability is None:
                    denial = "capability_not_allowed"
            except MCPRegistryError:
                conn.rollback()
                raise
            if denial is not None:
                self._audit(
                    conn, actor_id=actor_id, integration_id=integration_id,
                    action="mcp_integration_invoke_denied", reason=denial,
                    before=None, after={"capability": tool}, result="rejected", request_id=request_id,
                )
                conn.commit()
                raise MCPRegistryError("capability_denied", "MCP capability is not authorized")
            conn.commit()
        try:
            result = (send or _send_tool)(
                str(row["endpoint"]), str(capability["path"]), self._decrypt(row),
                dict(arguments), request_id,
            )
            if not isinstance(result, dict):
                raise ValueError("invalid MCP response")
        except Exception as exc:
            self._record_invoke(
                row, tool, actor_id=actor_id, request_id=request_id, result="failed",
            )
            raise MCPRegistryError("integration_unavailable", "MCP Integration call failed") from exc
        self._record_invoke(row, tool, actor_id=actor_id, request_id=request_id, result="success")
        return result

    def _record_invoke(
        self,
        row: sqlite3.Row,
        tool: str,
        *,
        actor_id: str,
        request_id: str,
        result: str,
    ) -> None:
        with self._database.connect() as conn:
            self._audit(
                conn, actor_id=actor_id, integration_id=str(row["id"]),
                action="mcp_integration_invoke", reason="tool_invocation",
                before=None, after={"capability": tool}, result=result, request_id=request_id,
            )
            conn.commit()

    def _encrypt(self, integration_id: str, credential: object) -> tuple[bytes | None, bytes | None, str | None, str | None]:
        if credential is None:
            return None, None, None, None
        value = _text(credential, "credential", 8_000)
        key = read_change_encryption_key(self._key_path)
        nonce = self._nonce_source(12)
        if len(nonce) != 12:
            raise MCPRegistryError("encryption_failed", "MCP credential nonce is invalid")
        ciphertext, digest, fingerprint = encrypt_secure_input(
            input_id=integration_id, key_name="mcp_credential", value=value, key=key, nonce=nonce,
        )
        return nonce, ciphertext, digest, fingerprint

    def _decrypt(self, row: sqlite3.Row) -> str | None:
        if row["credential_ciphertext"] is None:
            return None
        placeholder = secure_input_placeholder(str(row["id"]))
        refs = [{
            "id": str(row["id"]), "key_name": "mcp_credential", "placeholder": placeholder,
            "sha256": str(row["credential_sha256"]),
            "key_fingerprint": str(row["credential_key_fingerprint"]),
            "nonce": base64.urlsafe_b64encode(bytes(row["credential_nonce"])).decode(),
            "ciphertext": base64.urlsafe_b64encode(bytes(row["credential_ciphertext"])).decode(),
        }]
        return decrypt_secure_input_refs(refs, key_path=self._key_path)[placeholder]

    @staticmethod
    def _get_in(conn: sqlite3.Connection, integration_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM mcp_integrations WHERE id = ?", (integration_id,)).fetchone()
        if row is None:
            raise MCPRegistryError("integration_not_found", "MCP Integration not found")
        return row

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        *,
        actor_id: str,
        integration_id: str,
        action: str,
        reason: str,
        before: JSON | None,
        after: JSON | None,
        result: str,
        request_id: str,
    ) -> None:
        insert_admin_audit(
            conn, actor_id=actor_id, target_type="mcp_integration", target_id=integration_id,
            action=action, reason=reason, before=before, after=after, result=result,
            request_id=request_id,
        )


def _configuration(
    name: object, endpoint: object, capabilities: object, allowed_scope: object, enabled: object,
) -> JSON:
    if not isinstance(enabled, bool):
        raise MCPRegistryError("invalid_request", "enabled must be boolean")
    normalized_endpoint = _endpoint(endpoint)
    normalized_capabilities = _policy(capabilities)
    normalized_scope = _allowed_scope(allowed_scope)
    return {
        "name": _text(name, "name", 200), "endpoint": normalized_endpoint,
        "capabilities": normalized_capabilities, "allowed_scope": normalized_scope,
        "enabled": enabled,
    }


def _endpoint(value: object) -> str:
    endpoint = _text(value, "endpoint", 2_000).rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MCPRegistryError("invalid_integration", "MCP endpoint is invalid")
    return endpoint


def _policy(value: object) -> list[JSON]:
    if not isinstance(value, list) or not value:
        raise MCPRegistryError("invalid_integration", "At least one capability policy is required")
    normalized: list[JSON] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "version", "read_only"}:
            raise MCPRegistryError("invalid_integration", "Capability policy fields are invalid")
        name = _text(item["name"], "capability name", 200)
        version = _text(item["version"], "capability version", 200)
        if name in names or not isinstance(item["read_only"], bool):
            raise MCPRegistryError("invalid_integration", "Capability policy is invalid")
        names.add(name)
        normalized.append({"name": name, "version": version, "read_only": item["read_only"]})
    return sorted(normalized, key=lambda item: str(item["name"]))


def _allowed_scope(value: object) -> list[JSON]:
    if not isinstance(value, list) or not value:
        raise MCPRegistryError("invalid_integration", "At least one allowed scope is required")
    normalized: list[JSON] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"cluster_id", "namespace"}:
            raise MCPRegistryError("invalid_integration", "Allowed scope fields are invalid")
        namespace = item["namespace"]
        if namespace is not None:
            namespace = _text(namespace, "namespace", 253)
        normalized.append({"cluster_id": _text(item["cluster_id"], "cluster_id", 253), "namespace": namespace})
    unique = {_json(item): item for item in normalized}
    return [unique[key] for key in sorted(unique)]


def _health(value: object) -> tuple[str, str | None, list[JSON] | None]:
    if not isinstance(value, dict) or value.get("status") != "ok":
        return "unavailable", "health_unavailable", None
    raw = value.get("capabilities")
    if not isinstance(raw, list) and value.get("tool_name") is not None:
        raw = [{
            "name": value.get("tool_name"), "version": value.get("version"),
            "read_only": value.get("read_only"), "mutation": value.get("mutation"),
            "path": value.get("query_path"),
        }]
    if not isinstance(raw, list) or not raw:
        return "ok", None, []
    snapshot: list[JSON] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "version", "read_only", "mutation", "path"}:
            return "ok", None, []
        try:
            snapshot.append({
                "name": _text(item["name"], "capability name", 200),
                "version": _text(item["version"], "capability version", 200),
                "read_only": item["read_only"], "mutation": item["mutation"],
                "path": _path(item["path"]),
            })
        except MCPRegistryError:
            return "ok", None, []
    return "ok", None, sorted(snapshot, key=lambda item: str(item["name"]))


def _snapshot_denial(policy: list[JSON], observed: list[JSON] | None) -> str | None:
    if observed is None or len(policy) != len(observed):
        return "capability_snapshot_changed"
    for configured, actual in zip(policy, observed, strict=True):
        approved = approved_capability(str(configured["name"]))
        if (
            configured.get("read_only") is not True
            or approved is None
            or configured.get("version") != approved["version"]
            or actual != approved
        ):
            return "capability_snapshot_changed"
    return None


def _availability_denial(row: sqlite3.Row, resources: list[JSON] | None) -> str | None:
    if not row["enabled"]:
        return "integration_disabled"
    if row["verification_state"] != "verified" or row["verified_revision"] != row["revision"]:
        return "integration_unverified"
    if row["health_status"] != "ok" or row["capability_snapshot_json"] is None:
        return "integration_unavailable"
    snapshot = json.loads(str(row["capability_snapshot_json"]))
    policy = json.loads(str(row["capabilities_json"]))
    if _snapshot_denial(policy, snapshot) is not None:
        return "capability_snapshot_changed"
    if resources is None or not _scope_covers(json.loads(str(row["allowed_scope_json"])), resources):
        return "scope_not_allowed"
    return None


def _resources(scope: object) -> list[JSON] | None:
    if not isinstance(scope, dict) or not isinstance(scope.get("resources"), list) or not scope["resources"]:
        return None
    resources: list[JSON] = []
    for item in scope["resources"]:
        if not isinstance(item, dict) or not isinstance(item.get("cluster_id"), str) or not isinstance(item.get("namespace"), str):
            return None
        resources.append({"cluster_id": item["cluster_id"], "namespace": item["namespace"]})
    return resources


def _scope_covers(allowed: list[JSON], resources: list[JSON]) -> bool:
    return all(any(
        entry["cluster_id"] == resource["cluster_id"]
        and (entry["namespace"] is None or entry["namespace"] == resource["namespace"])
        for entry in allowed
    ) for resource in resources)


def _project(row: sqlite3.Row) -> JSON:
    snapshot = json.loads(str(row["capability_snapshot_json"])) if row["capability_snapshot_json"] else None
    verified = json.loads(str(row["verified_snapshot_json"])) if row["verified_snapshot_json"] else None
    return {
        "id": str(row["id"]), "name": str(row["name"]), "endpoint": str(row["endpoint"]),
        "credential_configured": row["credential_ciphertext"] is not None,
        "capabilities": json.loads(str(row["capabilities_json"])),
        "allowed_scope": json.loads(str(row["allowed_scope_json"])),
        "enabled": bool(row["enabled"]), "revision": str(row["revision"]),
        "verification": {
            "state": str(row["verification_state"]), "reason_code": row["verification_reason"],
            "verified_revision": row["verified_revision"],
        },
        "health": {
            "status": str(row["health_status"]), "checked_at": row["health_checked_at"],
            "error": row["health_error"],
        },
        "capability_snapshot": snapshot, "verified_capability_snapshot": verified,
        "capability_changed": snapshot is not None and verified is not None and snapshot != verified,
        "created_at": float(row["created_at"]), "updated_at": float(row["updated_at"]),
    }


def _probe(endpoint: str, credential: str | None, request_id: str) -> JSON:
    headers = {"Accept": "application/json", "X-Request-ID": request_id}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    outbound = request.Request(f"{endpoint}/healthz", headers=headers, method="GET")
    try:
        with request.urlopen(outbound, timeout=3) as response:
            payload = read_bounded_json(response)
    except (OSError, TimeoutError, error.URLError, ValueError) as exc:
        raise MCPRegistryError("integration_unavailable", "MCP Integration is unavailable") from exc
    if not isinstance(payload, dict):
        raise MCPRegistryError("integration_unavailable", "MCP Integration health response is invalid")
    return payload


def _send_tool(
    endpoint: str, path: str, credential: str | None, arguments: JSON, request_id: str,
) -> JSON:
    body = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    headers = {
        "Accept": "application/json", "Content-Type": "application/json",
        "X-Request-ID": request_id, "X-Correlation-ID": request_id,
    }
    headers.update(
        {"Authorization": f"Bearer {credential}"}
        if credential is not None else internal_auth_headers()
    )
    outbound = request.Request(f"{endpoint}{path}", data=body, headers=headers, method="POST")
    try:
        with request.urlopen(outbound, timeout=5) as response:
            payload = read_bounded_json(response)
    except (OSError, TimeoutError, error.URLError, ValueError) as exc:
        raise MCPRegistryError("integration_unavailable", "MCP Integration call failed") from exc
    if not isinstance(payload, dict):
        raise MCPRegistryError("integration_unavailable", "MCP Integration response is invalid")
    return payload


def _path(value: object) -> str:
    path = _text(value, "capability path", 500)
    if not path.startswith("/") or "?" in path or "#" in path:
        raise MCPRegistryError("invalid_integration", "Capability path is invalid")
    return path


def _text(value: object, field: str, maximum: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > maximum:
        raise MCPRegistryError("invalid_request", f"{field} is required")
    return normalized


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
