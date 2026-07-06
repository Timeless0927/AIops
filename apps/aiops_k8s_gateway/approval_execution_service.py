"""Gateway-owned approval execution records."""

from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, TypeVar


JSON = dict[str, Any]
T = TypeVar("T")

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "preflight_failed", "rollback_required"})
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approval_executions (
    execution_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE,
    incident_id TEXT NOT NULL,
    action_proposal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    action_json TEXT NOT NULL,
    preflight_json TEXT NOT NULL,
    post_check_json TEXT NOT NULL,
    preflight_result_json TEXT,
    execution_result_json TEXT,
    post_check_result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_approval_executions_status
ON approval_executions(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_approval_executions_incident
ON approval_executions(incident_id, created_at DESC);
"""


class ApprovalExecutionError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400, execution: JSON | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.execution = execution


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "approval_executions.db"
    return _project_root() / "data" / "approval_executions.db"


class ApprovalExecutionDB:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQL)

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
                        raise sqlite3.ProgrammingError("数据库连接已关闭")
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

    def get_execution(self, approval_id: str) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return _decode_row(dict(row)) if row is not None else None

    def create_or_replay(self, approval: JSON, payload: JSON, *, actor_id: str) -> tuple[JSON, bool]:
        normalized = normalize_execution_payload(approval, payload)
        now = time.time()
        execution_id = f"exec-{uuid.uuid4().hex}"

        def _write(conn: sqlite3.Connection) -> tuple[str, bool]:
            existing = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ? OR idempotency_key = ?",
                (approval["approval_id"], normalized["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                decoded = _decode_row(dict(existing))
                if (
                    decoded["approval_id"] == approval["approval_id"]
                    and decoded["idempotency_key"] == normalized["idempotency_key"]
                    and _execution_fingerprint(decoded) == _normalized_fingerprint(normalized)
                ):
                    return str(decoded["execution_id"]), True
                raise ApprovalExecutionError(
                    "duplicate_execution",
                    "approval grant already has a different execution record",
                    status=409,
                    execution=decoded,
                )
            conn.execute(
                """
                INSERT INTO approval_executions (
                    execution_id, approval_id, incident_id, action_proposal_id,
                    idempotency_key, status, cluster_id, namespace, requested_by,
                    action_json, preflight_json, post_check_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    approval["approval_id"],
                    approval["incident_id"],
                    approval["action_proposal_id"],
                    normalized["idempotency_key"],
                    normalized["cluster_id"],
                    normalized["namespace"],
                    actor_id,
                    stable_json(normalized["action"]),
                    stable_json(normalized["preflight"]),
                    stable_json(normalized["post_check"]),
                    now,
                    now,
                ),
            )
            return execution_id, False

        row_id, idempotent = self._execute_write(_write)
        row = self._get_by_execution_id(row_id)
        if row is None:
            raise ApprovalExecutionError("not_found", "execution record not found after create", status=500)
        return row, idempotent

    def claim(self, approval_id: str) -> tuple[bool, JSON]:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> tuple[bool, JSON]:
            row = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalExecutionError("not_found", "execution record not found", status=404)
            decoded = _decode_row(dict(row))
            if decoded["status"] != "queued":
                return False, decoded
            conn.execute(
                "UPDATE approval_executions SET status = 'preflight_running', updated_at = ? WHERE approval_id = ?",
                (now, approval_id),
            )
            updated = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return True, _decode_row(dict(updated))

        return self._execute_write(_write)

    def update_execution(
        self,
        approval_id: str,
        status: str,
        *,
        preflight_result: JSON | None = None,
        execution_result: JSON | None = None,
        post_check_result: JSON | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> JSON:
        now = time.time()
        completed_at = now if status in TERMINAL_STATUSES else None

        def _write(conn: sqlite3.Connection) -> JSON:
            row = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalExecutionError("not_found", "execution record not found", status=404)
            current = dict(row)
            conn.execute(
                """
                UPDATE approval_executions
                SET status = ?, updated_at = ?, completed_at = ?,
                    preflight_result_json = ?, execution_result_json = ?,
                    post_check_result_json = ?, error_code = ?, error_message = ?
                WHERE approval_id = ?
                """,
                (
                    status,
                    now,
                    completed_at,
                    _json_or_existing(preflight_result, current.get("preflight_result_json")),
                    _json_or_existing(execution_result, current.get("execution_result_json")),
                    _json_or_existing(post_check_result, current.get("post_check_result_json")),
                    error_code if error_code is not None else current.get("error_code"),
                    error_message if error_message is not None else current.get("error_message"),
                    approval_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return _decode_row(dict(updated))

        return self._execute_write(_write)

    def _get_by_execution_id(self, execution_id: str) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(
                "SELECT * FROM approval_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return _decode_row(dict(row)) if row is not None else None


def normalize_execution_payload(approval: JSON, payload: JSON) -> JSON:
    if approval.get("status") != "approved":
        raise ApprovalExecutionError("approval_not_approved", "approval request is not approved", status=409)
    expires_at = approval.get("expires_at")
    if expires_at is not None and float(expires_at) <= time.time():
        raise ApprovalExecutionError("approval_expired", "approval request is expired", status=409)
    if not str(approval.get("rollback_plan") or "").strip():
        raise ApprovalExecutionError("rollback_plan_required", "approved mutation requires a rollback plan", status=400)

    idempotency_key = _text(payload.get("idempotency_key"))
    if not idempotency_key:
        raise ApprovalExecutionError("idempotency_required", "idempotency_key is required", status=400)
    cluster_id = _text(payload.get("cluster_id"))
    namespace = _text(payload.get("namespace"))
    if not cluster_id or not namespace:
        raise ApprovalExecutionError("invalid_request", "cluster_id and namespace are required", status=400)
    scope = approval.get("resource_scope") if isinstance(approval.get("resource_scope"), dict) else {}
    approval_namespace = _text(scope.get("namespace"))
    if approval_namespace and namespace != approval_namespace:
        raise ApprovalExecutionError("out_of_scope", "execution namespace differs from approved scope", status=403)

    action_argv = _argv(payload.get("argv"), "argv")
    preflight_argv = _argv(payload.get("preflight_argv"), "preflight_argv")
    post_check_argv = _argv(payload.get("post_check_argv"), "post_check_argv")
    return {
        "idempotency_key": idempotency_key,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "action": _command_payload(payload, action_argv, "mutation"),
        "preflight": _command_payload(payload, preflight_argv, "read"),
        "post_check": _command_payload(payload, post_check_argv, "read"),
    }


def get_db() -> ApprovalExecutionDB:
    global _DB
    if _DB is None:
        _DB = ApprovalExecutionDB()
    return _DB


def create_or_replay(approval: JSON, payload: JSON, *, actor_id: str) -> tuple[JSON, bool]:
    return get_db().create_or_replay(approval, payload, actor_id=actor_id)


def claim(approval_id: str) -> tuple[bool, JSON]:
    return get_db().claim(approval_id)


def get_execution(approval_id: str) -> JSON | None:
    return get_db().get_execution(approval_id)


def update_execution(approval_id: str, status: str, **kwargs: Any) -> JSON:
    return get_db().update_execution(approval_id, status, **kwargs)


def stable_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _command_payload(payload: JSON, argv: list[str], action_type: str) -> JSON:
    return {
        "cluster_id": _text(payload.get("cluster_id")),
        "namespace": _text(payload.get("namespace")),
        "argv": argv,
        "reason": _text(payload.get("reason")) or "approved mutation execution",
        "task_id": _text(payload.get("task_id")) or "",
        "command_id": _text(payload.get("command_id")) or "",
        "timeout_seconds": int(payload.get("timeout_seconds") or 30),
        "output_limit_bytes": int(payload.get("output_limit_bytes") or 262144),
        "risk_level": _text(payload.get("risk_level")) or "low",
        "grant_id": _text(payload.get("grant_id")) or "",
        "action_type": action_type,
    }


def _decode_row(row: JSON) -> JSON:
    decoded = dict(row)
    decoded["action"] = _json_obj(decoded.pop("action_json", "{}"))
    decoded["preflight"] = _json_obj(decoded.pop("preflight_json", "{}"))
    decoded["post_check"] = _json_obj(decoded.pop("post_check_json", "{}"))
    decoded["preflight_result"] = _json_obj(decoded.pop("preflight_result_json", None))
    decoded["execution_result"] = _json_obj(decoded.pop("execution_result_json", None))
    decoded["post_check_result"] = _json_obj(decoded.pop("post_check_result_json", None))
    return decoded


def _json_obj(raw: Any) -> JSON | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _json_or_existing(value: JSON | None, existing: str | None) -> str | None:
    if value is None:
        return existing
    return stable_json(value)


def _argv(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        raise ApprovalExecutionError("invalid_request", f"{field} must be an array, not a shell string", status=400)
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise ApprovalExecutionError("invalid_request", f"{field} must be a non-empty string array", status=400)
    return list(value)


def _execution_fingerprint(execution: JSON) -> str:
    return stable_json(
        {
            "cluster_id": execution.get("cluster_id"),
            "namespace": execution.get("namespace"),
            "action": execution.get("action") or {},
            "preflight": execution.get("preflight") or {},
            "post_check": execution.get("post_check") or {},
        }
    )


def _normalized_fingerprint(normalized: JSON) -> str:
    return stable_json(
        {
            "cluster_id": normalized.get("cluster_id"),
            "namespace": normalized.get("namespace"),
            "action": normalized.get("action") or {},
            "preflight": normalized.get("preflight") or {},
            "post_check": normalized.get("post_check") or {},
        }
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


_DB: ApprovalExecutionDB | None = None
