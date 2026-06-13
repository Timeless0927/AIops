"""Gateway-owned approval request service.

Feishu is intentionally notification-only here. Approval state transitions are
owned by Gateway/control-plane and guarded by RBAC at the HTTP boundary.
"""

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

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({APPROVED, REJECTED, EXPIRED, CANCELLED})
VALID_STATUSES = frozenset({PENDING, *TERMINAL_STATUSES})

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    action_proposal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at REAL NOT NULL,
    assigned_approvers_json TEXT NOT NULL,
    approver_policy_ref TEXT,
    approved_by TEXT,
    rejected_by TEXT,
    decided_at REAL,
    decision_reason TEXT,
    expires_at REAL,
    action_summary TEXT NOT NULL,
    resource_scope_json TEXT NOT NULL,
    rollback_plan TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    audit_refs_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    notification_status TEXT,
    notification_delivery_id TEXT,
    notification_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_requests_action_proposal
ON approval_requests(action_proposal_id);

CREATE INDEX IF NOT EXISTS idx_approval_requests_status
ON approval_requests(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_approval_requests_incident
ON approval_requests(incident_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_approval_requests_session
ON approval_requests(session_id, created_at DESC);
"""


class ApprovalServiceError(ValueError):
    """Controlled approval service failure."""

    def __init__(self, code: str, message: str, *, status: int = 400, approval: JSON | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.approval = approval


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "approval_requests.db"
    return _project_root() / "data" / "approval_requests.db"


class ApprovalRequestDB:
    """SQLite-backed approval request store."""

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

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(sql, params).fetchone()
        return _decode_row(dict(row)) if row is not None else None

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[JSON]:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode_row(dict(row)) for row in rows]

    def create_request(self, payload: JSON, *, actor_id: str, request_id: str) -> tuple[JSON, bool]:
        normalized = normalize_create_payload(payload)
        now = time.time()
        approval_id = f"ap-{uuid.uuid4().hex}"

        def _write(conn: sqlite3.Connection) -> tuple[str, bool]:
            existing = conn.execute(
                """
                SELECT approval_id FROM approval_requests
                WHERE idempotency_key = ? OR action_proposal_id = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (normalized["idempotency_key"], normalized["action_proposal_id"]),
            ).fetchone()
            if existing is not None:
                return str(existing["approval_id"]), True
            conn.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, incident_id, session_id, action_proposal_id,
                    status, risk_level, requested_by, requested_at,
                    assigned_approvers_json, approver_policy_ref, approved_by,
                    rejected_by, decided_at, decision_reason, expires_at,
                    action_summary, resource_scope_json, rollback_plan,
                    evidence_refs_json, audit_refs_json, idempotency_key,
                    notification_status, notification_delivery_id, notification_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, NULL, NULL, NULL,
                    NULL, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    approval_id,
                    normalized["incident_id"],
                    normalized["session_id"],
                    normalized["action_proposal_id"],
                    normalized["risk_level"],
                    normalized["requested_by"],
                    now,
                    json.dumps(normalized["assigned_approvers"], ensure_ascii=False, sort_keys=True),
                    normalized.get("approver_policy_ref"),
                    normalized.get("expires_at"),
                    normalized["action_summary"],
                    json.dumps(normalized["resource_scope"], ensure_ascii=False, sort_keys=True),
                    normalized["rollback_plan"],
                    json.dumps(normalized["evidence_refs"], ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        [
                            *normalized["audit_refs"],
                            {
                                "event": "approval_created",
                                "actor": actor_id,
                                "request_id": request_id,
                                "at": now,
                            },
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    normalized["idempotency_key"],
                    now,
                    now,
                ),
            )
            return approval_id, False

        row_id, idempotent = self._execute_write(_write)
        row = self.get_request(row_id)
        if row is None:
            raise ApprovalServiceError("not_found", "approval request not found after create", status=500)
        return row, idempotent

    def get_request(self, approval_id: str) -> JSON | None:
        return self._fetchone("SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,))

    def list_requests(
        self,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
        team_id: str | None = None,
        incident_id: str | None = None,
        session_id: str | None = None,
        action_proposal_id: str | None = None,
        risk_level: str | None = None,
        created_at_from: float | None = None,
        created_at_to: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JSON]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if incident_id:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if action_proposal_id:
            clauses.append("action_proposal_id = ?")
            params.append(action_proposal_id)
        if risk_level:
            clauses.append("risk_level = ?")
            params.append(risk_level)
        if created_at_from is not None:
            clauses.append("created_at >= ?")
            params.append(created_at_from)
        if created_at_to is not None:
            clauses.append("created_at <= ?")
            params.append(created_at_to)
        if assigned_to:
            clauses.append("assigned_approvers_json LIKE ?")
            params.append(f'%"{_escape_like(assigned_to)}"%')
        if team_id:
            clauses.append("resource_scope_json LIKE ?")
            params.append(f'%"team_id": "{_escape_like(team_id)}"%')
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        return self._fetchall(
            f"""
            SELECT * FROM approval_requests{where}
            ORDER BY created_at DESC, approval_id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        )

    def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        actor_id: str,
        reason: str | None,
        request_id: str,
    ) -> tuple[JSON, bool]:
        if decision not in {APPROVED, REJECTED, EXPIRED, CANCELLED}:
            raise ApprovalServiceError("invalid_decision", f"unsupported decision: {decision}", status=400)
        if decision == REJECTED and not str(reason or "").strip():
            raise ApprovalServiceError("invalid_request", "reject reason is required", status=400)
        now = time.time()

        def _write(conn: sqlite3.Connection) -> tuple[str, bool, str | None]:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalServiceError("not_found", "approval request not found", status=404)
            decoded = _decode_row(dict(row))
            if decoded["status"] in TERMINAL_STATUSES:
                if decoded["status"] == decision:
                    return approval_id, True, None
                raise ApprovalServiceError(
                    "invalid_state_transition",
                    f"approval is already {decoded['status']}",
                    status=409,
                    approval=decoded,
                )
            expires_at = decoded.get("expires_at")
            if decision in {APPROVED, REJECTED} and expires_at is not None and float(expires_at) <= now:
                self._expire_locked(conn, decoded, actor_id=actor_id, request_id=request_id, now=now)
                return approval_id, False, "approval_expired"
            audit_refs = list(decoded.get("audit_refs") or [])
            audit_refs.append(
                {
                    "event": f"approval_{decision}",
                    "actor": actor_id,
                    "request_id": request_id,
                    "at": now,
                    "reason": reason,
                }
            )
            approved_by = actor_id if decision == APPROVED else None
            rejected_by = actor_id if decision == REJECTED else None
            conn.execute(
                """
                UPDATE approval_requests
                SET status = ?,
                    approved_by = ?,
                    rejected_by = ?,
                    decided_at = ?,
                    decision_reason = ?,
                    audit_refs_json = ?,
                    updated_at = ?
                WHERE approval_id = ? AND status = 'pending'
                """,
                (
                    decision,
                    approved_by,
                    rejected_by,
                    now,
                    str(reason).strip() if reason is not None else None,
                    json.dumps(audit_refs, ensure_ascii=False, sort_keys=True),
                    now,
                    approval_id,
                ),
            )
            return approval_id, False, None

        row_id, idempotent, conflict_code = self._execute_write(_write)
        row = self.get_request(row_id)
        if row is None:
            raise ApprovalServiceError("not_found", "approval request not found", status=404)
        if conflict_code == "approval_expired":
            raise ApprovalServiceError(
                "approval_expired",
                "approval request is expired",
                status=409,
                approval=row,
            )
        return row, idempotent

    def mark_notification_result(self, approval_id: str, result: JSON) -> JSON | None:
        delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else {}
        status = delivery.get("delivery_status")
        delivery_id = delivery.get("id")
        error = delivery.get("last_delivery_error")

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE approval_requests
                SET notification_status = ?,
                    notification_delivery_id = ?,
                    notification_error = ?,
                    updated_at = ?
                WHERE approval_id = ?
                """,
                (
                    str(status or ("sent" if result.get("ok") else "failed")),
                    str(delivery_id) if delivery_id else None,
                    str(error) if error else None,
                    time.time(),
                    approval_id,
                ),
            )

        self._execute_write(_write)
        return self.get_request(approval_id)

    def _expire_locked(
        self,
        conn: sqlite3.Connection,
        decoded: JSON,
        *,
        actor_id: str,
        request_id: str,
        now: float,
    ) -> JSON:
        audit_refs = list(decoded.get("audit_refs") or [])
        audit_refs.append(
            {
                "event": "approval_expired",
                "actor": actor_id,
                "request_id": request_id,
                "at": now,
                "reason": "expires_at elapsed",
            }
        )
        conn.execute(
            """
            UPDATE approval_requests
            SET status = 'expired',
                decided_at = ?,
                decision_reason = ?,
                audit_refs_json = ?,
                updated_at = ?
            WHERE approval_id = ? AND status = 'pending'
            """,
            (
                now,
                "expires_at elapsed",
                json.dumps(audit_refs, ensure_ascii=False, sort_keys=True),
                now,
                decoded["approval_id"],
            ),
        )
        updated = dict(decoded)
        updated.update(
            {
                "status": EXPIRED,
                "decided_at": now,
                "decision_reason": "expires_at elapsed",
                "audit_refs": audit_refs,
                "updated_at": now,
            }
        )
        return updated


def normalize_create_payload(payload: JSON) -> JSON:
    required = (
        "incident_id",
        "session_id",
        "action_proposal_id",
        "risk_level",
        "requested_by",
        "reason",
        "action_summary",
        "resource_scope",
        "rollback_plan",
    )
    missing = [key for key in required if not _has_value(payload.get(key))]
    if missing:
        raise ApprovalServiceError("invalid_request", f"missing required fields: {', '.join(missing)}", status=400)
    resource_scope = payload.get("resource_scope")
    if not isinstance(resource_scope, dict):
        raise ApprovalServiceError("invalid_request", "resource_scope must be a JSON object", status=400)
    evidence_refs = payload.get("evidence_refs") or []
    audit_refs = payload.get("audit_refs") or []
    if not isinstance(evidence_refs, list):
        raise ApprovalServiceError("invalid_request", "evidence_refs must be a JSON array", status=400)
    if not isinstance(audit_refs, list):
        raise ApprovalServiceError("invalid_request", "audit_refs must be a JSON array", status=400)
    idempotency_key = _first_text(payload.get("idempotency_key"))
    if not idempotency_key:
        idempotency_key = f"action_proposal:{_text(payload['action_proposal_id'])}"
    return {
        "incident_id": _text(payload["incident_id"]),
        "session_id": _text(payload["session_id"]),
        "action_proposal_id": _text(payload["action_proposal_id"]),
        "risk_level": _text(payload["risk_level"]),
        "requested_by": _text(payload["requested_by"]),
        "reason": _text(payload["reason"]),
        "action_summary": _text(payload["action_summary"]),
        "resource_scope": dict(resource_scope),
        "rollback_plan": _text(payload["rollback_plan"]),
        "evidence_refs": list(evidence_refs),
        "audit_refs": list(audit_refs),
        "assigned_approvers": _as_string_list(payload.get("assigned_approvers")),
        "approver_policy_ref": _first_text(payload.get("approver_policy_ref")),
        "expires_at": _optional_float(payload.get("expires_at")),
        "idempotency_key": idempotency_key,
    }


def execution_grant_for(approval: JSON) -> JSON | None:
    """Return the execution grant basis only for locally approved approvals."""

    if approval.get("status") != APPROVED:
        return None
    return {
        "approval_id": approval["approval_id"],
        "incident_id": approval["incident_id"],
        "session_id": approval["session_id"],
        "action_proposal_id": approval["action_proposal_id"],
        "approved_by": approval.get("approved_by"),
        "decided_at": approval.get("decided_at"),
        "resource_scope": approval.get("resource_scope") or {},
    }


def get_db() -> ApprovalRequestDB:
    global _DB
    if _DB is None:
        _DB = ApprovalRequestDB()
    return _DB


def create_request(payload: JSON, *, actor_id: str, request_id: str) -> tuple[JSON, bool]:
    return get_db().create_request(payload, actor_id=actor_id, request_id=request_id)


def get_request(approval_id: str) -> JSON | None:
    return get_db().get_request(approval_id)


def list_requests(**filters: Any) -> list[JSON]:
    return get_db().list_requests(**filters)


def decide(
    approval_id: str,
    *,
    decision: str,
    actor_id: str,
    reason: str | None,
    request_id: str,
) -> tuple[JSON, bool]:
    return get_db().decide(
        approval_id,
        decision=decision,
        actor_id=actor_id,
        reason=reason,
        request_id=request_id,
    )


def mark_notification_result(approval_id: str, result: JSON) -> JSON | None:
    return get_db().mark_notification_result(approval_id, result)


def _decode_row(row: JSON) -> JSON:
    decoded = dict(row)
    decoded["assigned_approvers"] = _json_list(decoded.pop("assigned_approvers_json", "[]"))
    decoded["resource_scope"] = _json_obj(decoded.pop("resource_scope_json", "{}"))
    decoded["evidence_refs"] = _json_list(decoded.pop("evidence_refs_json", "[]"))
    decoded["audit_refs"] = _json_list(decoded.pop("audit_refs_json", "[]"))
    decoded["execution_grant"] = execution_grant_for(decoded)
    return decoded


def _json_obj(raw: Any) -> JSON:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _has_value(value: Any) -> bool:
    if isinstance(value, dict):
        return True
    return bool(str(value or "").strip())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ApprovalServiceError("invalid_request", "expires_at must be a unix timestamp", status=400) from exc


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_DB: ApprovalRequestDB | None = None
