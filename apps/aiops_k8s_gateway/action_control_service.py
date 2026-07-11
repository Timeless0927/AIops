"""Gateway-owned structured actions, grants, and mutation target locks."""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, TypeVar


JSON = dict[str, Any]
T = TypeVar("T")

SUPPORTED_ACTIONS = {"restart_deployment", "scale_deployment", "rollback_deployment"}
TERMINAL_GRANT_STATUSES = {"consumed", "cancelled"}
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS action_proposals (
    action_id TEXT PRIMARY KEY,
    action_proposal_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL DEFAULT 'gateway-action-parser',
    action_type TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    request_id TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT,
    risk_level TEXT NOT NULL,
    policy_decision TEXT NOT NULL,
    policy_reason TEXT NOT NULL,
    policy_hit_id TEXT,
    approval_id TEXT,
    target_json TEXT NOT NULL,
    action_json TEXT NOT NULL,
    execution_payload_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_grants (
    grant_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    grant_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    granted_to TEXT NOT NULL,
    status TEXT NOT NULL,
    execution_id TEXT,
    created_at REAL NOT NULL,
    consumed_at REAL,
    FOREIGN KEY (action_id) REFERENCES action_proposals(action_id)
);

CREATE TABLE IF NOT EXISTS target_locks (
    lock_key TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_proposals_created ON action_proposals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_grants_action ON execution_grants(action_id, status);
"""


class ActionControlError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400, action: JSON | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.action = action


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "actions.db"
    return _project_root() / "data" / "actions.db"


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ActionControlDB:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._migrate_schema()

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
            self._conn.close()
            self._conn = None

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        last_err: Exception | None = None
        for attempt in range(_WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    if self._conn is None:
                        raise sqlite3.ProgrammingError("database connection is closed")
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                self._write_count += 1
                if self._write_count % _CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if ("locked" in message or "busy" in message) and attempt < _WRITE_MAX_RETRIES - 1:
                    last_err = exc
                    time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))
                    continue
                raise
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    def _try_wal_checkpoint(self) -> None:
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def _migrate_schema(self) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(action_proposals)").fetchall()}
        if "agent_id" not in columns:
            self._conn.execute("ALTER TABLE action_proposals ADD COLUMN agent_id TEXT NOT NULL DEFAULT 'gateway-action-parser'")

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("database connection is closed")
            row = self._conn.execute(sql, params).fetchone()
        return _decode_action(dict(row)) if row is not None else None

    def create_proposal(self, payload: JSON, *, actor_id: str, request_id: str, policy: JSON, policy_hit: JSON | None) -> tuple[JSON, bool]:
        normalized = normalize_action_payload(payload)
        normalized["policy_decision"] = str(policy.get("decision") or "approval_required")
        normalized["policy_reason"] = str(policy.get("reason") or "")
        normalized["policy_hit_id"] = str((policy_hit or {}).get("id") or "")
        normalized["requested_by"] = actor_id
        normalized["request_id"] = request_id
        now = time.time()

        def _write(conn: sqlite3.Connection) -> tuple[str, bool]:
            existing = conn.execute(
                "SELECT * FROM action_proposals WHERE idempotency_key = ? OR action_proposal_id = ?",
                (normalized["idempotency_key"], normalized["action_proposal_id"]),
            ).fetchone()
            if existing is not None:
                decoded = _decode_action(dict(existing))
                if (
                    decoded["idempotency_key"] == normalized["idempotency_key"]
                    and decoded["action_proposal_id"] == normalized["action_proposal_id"]
                    and decoded["action_hash"] == normalized["action_hash"]
                ):
                    return str(decoded["action_id"]), True
                raise ActionControlError("idempotency_conflict", "action idempotency key conflicts with another action", status=HTTPStatus.CONFLICT)
            action_id = f"act-{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO action_proposals (
                    action_id, action_proposal_id, idempotency_key, action_type,
                    agent_id, action_hash, status, requested_by, request_id, incident_id,
                    session_id, run_id, risk_level, policy_decision, policy_reason,
                    policy_hit_id, approval_id, target_json, action_json,
                    execution_payload_json, evidence_refs_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    normalized["action_proposal_id"],
                    normalized["idempotency_key"],
                    normalized["action_type"],
                    normalized["agent_id"],
                    normalized["action_hash"],
                    normalized["requested_by"],
                    normalized["request_id"],
                    normalized["incident_id"],
                    normalized["session_id"],
                    normalized["run_id"],
                    normalized["risk_level"],
                    normalized["policy_decision"],
                    normalized["policy_reason"],
                    normalized["policy_hit_id"],
                    stable_json(normalized["target"]),
                    stable_json(normalized["action"]),
                    stable_json(normalized["execution_payload"]),
                    stable_json(normalized["evidence_refs"]),
                    now,
                    now,
                ),
            )
            return action_id, False

        action_id, idempotent = self._execute_write(_write)
        action = self.get_action(action_id)
        if action is None:
            raise ActionControlError("not_found", "action proposal not found after create", status=500)
        return action, idempotent

    def get_action(self, action_id: str) -> JSON | None:
        return self._fetchone("SELECT * FROM action_proposals WHERE action_id = ?", (action_id,))

    def get_by_proposal_id(self, action_proposal_id: str) -> JSON | None:
        return self._fetchone("SELECT * FROM action_proposals WHERE action_proposal_id = ?", (action_proposal_id,))

    def attach_approval(self, action_id: str, approval_id: str) -> JSON:
        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE action_proposals SET approval_id = ?, status = 'pending_approval', updated_at = ? WHERE action_id = ?",
                (approval_id, time.time(), action_id),
            )

        self._execute_write(_write)
        action = self.get_action(action_id)
        if action is None:
            raise ActionControlError("not_found", "action proposal not found", status=HTTPStatus.NOT_FOUND)
        return action

    def create_grant(self, action: JSON, *, grant_type: str, source_id: str, actor_id: str) -> tuple[JSON, bool]:
        now = time.time()
        grant_id = f"grant-{uuid.uuid4().hex}"

        def _write(conn: sqlite3.Connection) -> tuple[str, bool]:
            existing = conn.execute(
                "SELECT * FROM execution_grants WHERE action_id = ? AND grant_type = ? AND source_id = ?",
                (action["action_id"], grant_type, source_id),
            ).fetchone()
            if existing is not None:
                return str(existing["grant_id"]), True
            conn.execute(
                """
                INSERT INTO execution_grants (
                    grant_id, action_id, action_hash, grant_type, source_id,
                    granted_to, status, execution_id, created_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?, NULL)
                """,
                (grant_id, action["action_id"], action["action_hash"], grant_type, source_id, actor_id, now),
            )
            return grant_id, False

        row_id, idempotent = self._execute_write(_write)
        grant = self.get_grant(row_id)
        if grant is None:
            raise ActionControlError("not_found", "grant not found after create", status=500)
        return grant, idempotent

    def get_grant(self, grant_id: str) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("database connection is closed")
            row = self._conn.execute("SELECT * FROM execution_grants WHERE grant_id = ?", (grant_id,)).fetchone()
        return _decode_grant(dict(row)) if row is not None else None

    def consume_grant(self, grant_id: str, *, action_hash: str, execution_id: str) -> JSON:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> JSON:
            row = conn.execute("SELECT * FROM execution_grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if row is None:
                raise ActionControlError("grant_not_found", "execution grant not found", status=HTTPStatus.FORBIDDEN)
            grant = _decode_grant(dict(row))
            if grant["action_hash"] != action_hash:
                raise ActionControlError("grant_hash_mismatch", "execution grant does not match action hash", status=HTTPStatus.FORBIDDEN)
            if grant["status"] in TERMINAL_GRANT_STATUSES:
                if grant.get("execution_id") == execution_id:
                    return grant
                raise ActionControlError("grant_consumed", "execution grant has already been consumed", status=HTTPStatus.CONFLICT)
            conn.execute(
                "UPDATE execution_grants SET status = 'consumed', execution_id = ?, consumed_at = ? WHERE grant_id = ?",
                (execution_id, now, grant_id),
            )
            updated = conn.execute("SELECT * FROM execution_grants WHERE grant_id = ?", (grant_id,)).fetchone()
            return _decode_grant(dict(updated))

        return self._execute_write(_write)

    def claim_lock(self, lock_key: str, *, action_id: str, execution_id: str, owner: str, ttl_seconds: int = 600) -> JSON:
        now = time.time()
        expires_at = now + ttl_seconds

        def _write(conn: sqlite3.Connection) -> JSON:
            row = conn.execute("SELECT * FROM target_locks WHERE lock_key = ?", (lock_key,)).fetchone()
            if row is not None and float(row["expires_at"]) > now and row["execution_id"] != execution_id:
                raise ActionControlError("target_locked", "mutation target is locked by another execution", status=HTTPStatus.CONFLICT)
            conn.execute(
                """
                INSERT INTO target_locks (lock_key, action_id, execution_id, owner, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(lock_key) DO UPDATE SET
                    action_id=excluded.action_id,
                    execution_id=excluded.execution_id,
                    owner=excluded.owner,
                    expires_at=excluded.expires_at
                """,
                (lock_key, action_id, execution_id, owner, expires_at, now),
            )
            updated = conn.execute("SELECT * FROM target_locks WHERE lock_key = ?", (lock_key,)).fetchone()
            return dict(updated)

        return self._execute_write(_write)

    def release_lock(self, lock_key: str, execution_id: str) -> None:
        def _write(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM target_locks WHERE lock_key = ? AND execution_id = ?", (lock_key, execution_id))

        self._execute_write(_write)


def normalize_action_payload(payload: JSON) -> JSON:
    action_type = str(payload.get("action_type") or payload.get("action") or "").strip()
    if action_type not in SUPPORTED_ACTIONS:
        raise ActionControlError("action_unknown", "unsupported action_type", status=HTTPStatus.BAD_REQUEST)
    target = _target(payload, require_replicas=action_type == "scale_deployment")
    evidence_refs = payload.get("evidence_refs") or []
    if not isinstance(evidence_refs, list):
        raise ActionControlError("invalid_request", "evidence_refs must be an array", status=HTTPStatus.BAD_REQUEST)
    action = {
        "action_type": action_type,
        "target": target,
        "params": {"replicas": target.get("replicas")} if action_type == "scale_deployment" else {},
        "reason": _text(payload.get("reason")) or f"{action_type} requested",
    }
    execution_payload = _execution_payload(action_type, target, action["reason"], payload)
    frozen = {
        "action": action,
        "execution_payload": execution_payload,
        "evidence_refs": evidence_refs,
        "incident_id": _text(payload.get("incident_id")) or "manual",
        "run_id": _text(payload.get("run_id")),
        "session_id": _text(payload.get("session_id")) or _text(payload.get("run_id")) or "manual",
    }
    action_hash = hashlib.sha256(stable_json(frozen).encode("utf-8")).hexdigest()
    action_proposal_id = _text(payload.get("action_proposal_id")) or f"proposal-{action_hash[:16]}"
    idempotency_key = _text(payload.get("idempotency_key")) or f"action:{action_hash}"
    return {
        "action_proposal_id": action_proposal_id,
        "idempotency_key": idempotency_key,
        "agent_id": _text(payload.get("agent_id") or payload.get("agent")) or "gateway-action-parser",
        "action_type": action_type,
        "action_hash": action_hash,
        "incident_id": frozen["incident_id"],
        "session_id": frozen["session_id"],
        "run_id": frozen["run_id"] or None,
        "risk_level": _text(payload.get("risk_level")) or _risk_for(action_type),
        "target": target,
        "action": action,
        "execution_payload": execution_payload,
        "evidence_refs": evidence_refs,
    }


def approval_payload(action: JSON, *, assigned_approvers: list[str] | None = None, expires_at: float | None = None) -> JSON:
    target = action["target"]
    return {
        "incident_id": action["incident_id"],
        "session_id": action["session_id"],
        "action_proposal_id": action["action_proposal_id"],
        "risk_level": action["risk_level"],
        "requested_by": action["requested_by"],
        "reason": action["action"]["reason"],
        "action_summary": _summary(action),
        "resource_scope": _resource_scope(target),
        "rollback_plan": action["execution_payload"]["rollback_plan"],
        "evidence_refs": action["evidence_refs"],
        "audit_refs": [{"event": "action_frozen", "action_id": action["action_id"], "action_hash": action["action_hash"]}],
        "idempotency_key": f"approval:{action['action_hash']}",
        "assigned_approvers": assigned_approvers or [],
        "expires_at": expires_at,
    }


def execution_payload_for(action: JSON, *, grant_id: str, idempotency_key: str | None = None) -> JSON:
    payload = dict(action["execution_payload"])
    payload.update(
        {
            "grant_id": grant_id,
            "idempotency_key": idempotency_key or f"exec:{action['action_hash']}",
            "action_id": action["action_id"],
            "action_hash": action["action_hash"],
            "lock_key": lock_key(action),
        }
    )
    return payload


def lock_key(action: JSON) -> str:
    target = action["target"]
    return f"kubernetes:{target['cluster']}:{target['namespace']}:deployment/{target['deployment']}"


def _target(payload: JSON, *, require_replicas: bool) -> JSON:
    deployment = payload.get("deployment") or payload.get("target")
    if isinstance(deployment, list):
        values = [str(item).strip() for item in deployment if str(item).strip()]
        if len(values) != 1:
            raise ActionControlError("ambiguous_target", "exactly one deployment target is required", status=HTTPStatus.BAD_REQUEST)
        deployment = values[0]
    deployment_text = _text(deployment)
    missing = [
        key
        for key, value in {
            "cluster": payload.get("cluster") or payload.get("cluster_id"),
            "namespace": payload.get("namespace"),
            "service": payload.get("service") or payload.get("service_id"),
            "team": payload.get("team") or payload.get("team_id"),
            "deployment": deployment_text,
        }.items()
        if not _text(value)
    ]
    if missing:
        raise ActionControlError("target_required", f"missing target fields: {', '.join(missing)}", status=HTTPStatus.BAD_REQUEST)
    target: JSON = {
        "cluster": _text(payload.get("cluster") or payload.get("cluster_id")),
        "namespace": _text(payload.get("namespace")),
        "service": _text(payload.get("service") or payload.get("service_id")),
        "team": _text(payload.get("team") or payload.get("team_id")),
        "deployment": deployment_text,
    }
    if require_replicas:
        try:
            replicas = int(payload.get("replicas"))
        except (TypeError, ValueError) as exc:
            raise ActionControlError("target_required", "replicas is required for scale_deployment", status=HTTPStatus.BAD_REQUEST) from exc
        if replicas < 0 or replicas > 20:
            raise ActionControlError("invalid_request", "replicas must be between 0 and 20", status=HTTPStatus.BAD_REQUEST)
        target["replicas"] = replicas
    return target


def _execution_payload(action_type: str, target: JSON, reason: str, payload: JSON) -> JSON:
    name = f"deployment/{target['deployment']}"
    namespace = target["namespace"]
    base = {
        "cluster_id": target["cluster"],
        "namespace": namespace,
        "reason": reason,
        "risk_level": _text(payload.get("risk_level")) or _risk_for(action_type),
        "task_id": _text(payload.get("task_id")),
        "command_id": _text(payload.get("command_id")),
        "preflight_argv": ["kubectl", "get", name, "-n", namespace],
        "post_check_argv": ["kubectl", "rollout", "status", name, "-n", namespace],
    }
    if action_type == "restart_deployment":
        argv = ["kubectl", "rollout", "restart", name, "-n", namespace]
        rollback = f"kubectl rollout undo {name} -n {namespace}"
    elif action_type == "scale_deployment":
        argv = ["kubectl", "scale", name, f"--replicas={target['replicas']}", "-n", namespace]
        rollback = f"restore previous replica count for {name} in {namespace}"
    else:
        argv = ["kubectl", "rollout", "undo", name, "-n", namespace]
        rollback = f"kubectl rollout restart {name} -n {namespace}"
    return {**base, "argv": argv, "rollback_plan": _text(payload.get("rollback_plan")) or rollback}


def _resource_scope(target: JSON) -> JSON:
    return {
        "cluster_id": target["cluster"],
        "cluster": target["cluster"],
        "namespace": target["namespace"],
        "service_id": target["service"],
        "service": target["service"],
        "team_id": target["team"],
        "team": target["team"],
    }


def _summary(action: JSON) -> str:
    target = action["target"]
    return f"{action['action_type']} deployment/{target['deployment']} in {target['namespace']}"


def _risk_for(action_type: str) -> str:
    return "medium" if action_type == "scale_deployment" else "low"


def _decode_action(row: JSON) -> JSON:
    decoded = dict(row)
    decoded["target"] = json.loads(decoded.pop("target_json") or "{}")
    decoded["action"] = json.loads(decoded.pop("action_json") or "{}")
    decoded["execution_payload"] = json.loads(decoded.pop("execution_payload_json") or "{}")
    decoded["evidence_refs"] = json.loads(decoded.pop("evidence_refs_json") or "[]")
    return decoded


def _decode_grant(row: JSON) -> JSON:
    return dict(row)


def _text(value: Any) -> str:
    return str(value or "").strip()


_DB = ActionControlDB()


def create_proposal(payload: JSON, *, actor_id: str, request_id: str, policy: JSON, policy_hit: JSON | None) -> tuple[JSON, bool]:
    return _DB.create_proposal(payload, actor_id=actor_id, request_id=request_id, policy=policy, policy_hit=policy_hit)


def get_action(action_id: str) -> JSON | None:
    return _DB.get_action(action_id)


def get_by_proposal_id(action_proposal_id: str) -> JSON | None:
    return _DB.get_by_proposal_id(action_proposal_id)


def attach_approval(action_id: str, approval_id: str) -> JSON:
    return _DB.attach_approval(action_id, approval_id)


def create_grant(action: JSON, *, grant_type: str, source_id: str, actor_id: str) -> tuple[JSON, bool]:
    return _DB.create_grant(action, grant_type=grant_type, source_id=source_id, actor_id=actor_id)


def consume_grant(grant_id: str, *, action_hash: str, execution_id: str) -> JSON:
    return _DB.consume_grant(grant_id, action_hash=action_hash, execution_id=execution_id)


def claim_lock(lock_key: str, *, action_id: str, execution_id: str, owner: str, ttl_seconds: int = 600) -> JSON:
    return _DB.claim_lock(lock_key, action_id=action_id, execution_id=execution_id, owner=owner, ttl_seconds=ttl_seconds)


def release_lock(lock_key: str, execution_id: str) -> None:
    _DB.release_lock(lock_key, execution_id)
