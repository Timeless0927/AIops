"""Gateway-owned Change Request and immutable Plan Revision state."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

from .gateway_db import GatewayDatabase, register_migrations


_SCHEMA_VERSION = 16
_SCHEMA = """
CREATE TABLE change_requests (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    actor_id TEXT NOT NULL,
    desired_outcome TEXT NOT NULL,
    context TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(actor_id, idempotency_key)
);
CREATE INDEX change_requests_by_incident ON change_requests(incident_id, created_at, id);

CREATE TABLE change_plan_phases (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL CHECK (status IN ('planning', 'needs_input', 'validating')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(change_request_id, sequence)
);

CREATE TABLE change_plan_revisions (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    phase_id TEXT NOT NULL REFERENCES change_plan_phases(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision > 0),
    status TEXT NOT NULL CHECK (status IN ('needs_input', 'validating', 'superseded')),
    question TEXT,
    plan_json TEXT,
    created_at REAL NOT NULL,
    superseded_at REAL,
    UNIQUE(change_request_id, revision)
);

CREATE TABLE change_request_inputs (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    question TEXT NOT NULL,
    content TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(change_request_id, idempotency_key)
);

CREATE TABLE change_request_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    actor_id TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX change_request_events_by_request ON change_request_events(change_request_id, event_id);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))

_CREDENTIAL = re.compile(
    r"(?is)(?:\b(?:password|passwd|token|api[_ -]?key|secret|credential)\b\s*[:=]\s*\S+|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_KUBERNETES_MANIFEST = re.compile(r"(?im)^\s*apiVersion\s*:\s*\S+.*^\s*kind\s*:\s*\S+")
_KUBECTL = re.compile(r"(?i)(?:^|[;&|`]\s*)kubectl\s+|```(?:yaml|json|sh|bash)\b")

Planner = Callable[[dict[str, object]], dict[str, object]]


class ChangeRequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ChangeRequests:
    """Persists User intent and model-produced planning revisions."""

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

    def submit(
        self,
        *,
        incident_snapshot: dict[str, object],
        actor_id: str,
        desired_outcome: str,
        context: str,
        idempotency_key: str,
        planner: Planner,
    ) -> tuple[bool, dict[str, object]]:
        desired_outcome = _text(desired_outcome, "desired_outcome", 2000)
        context = _optional_text(context, "context", 4000)
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        _reject_credentials(desired_outcome, context)
        _reject_executable_proposals(desired_outcome, context)
        incident = incident_snapshot.get("incident")
        if not isinstance(incident, dict) or not isinstance(incident.get("id"), str):
            raise ChangeRequestError("incident_not_found", "Incident not found")
        now = self._clock()
        request_id = self._id_factory("change-request")
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id, incident_id, desired_outcome, context FROM change_requests WHERE actor_id = ? AND idempotency_key = ?",
                (actor_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                if (
                    str(existing["incident_id"]) != incident["id"]
                    or str(existing["desired_outcome"]) != desired_outcome
                    or str(existing["context"]) != context
                ):
                    raise ChangeRequestError("idempotency_conflict", "Idempotency key is already used by another request")
                return False, self.get(str(existing["id"]))
            conn.execute(
                """
                INSERT INTO change_requests (
                    id, incident_id, actor_id, desired_outcome, context,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, incident["id"], actor_id, desired_outcome, context, idempotency_key, now, now),
            )
            phase_id = self._id_factory("plan-phase")
            conn.execute(
                "INSERT INTO change_plan_phases (id, change_request_id, sequence, status, created_at, updated_at) VALUES (?, ?, 1, 'planning', ?, ?)",
                (phase_id, request_id, now, now),
            )
            _append_event(
                conn, request_id, "change_request.created", actor_id,
                {"phase_id": phase_id, "phase_sequence": 1, "status": "planning"}, now,
            )
            conn.commit()
        result = planner(
            {
                "change_request_id": request_id,
                "incident_id": incident["id"],
                "desired_outcome": desired_outcome,
                "context": context,
                "facts": _sanitized_facts(incident_snapshot),
                "inputs": [],
            }
        )
        self._finalize(request_id, actor_id, result)
        return True, self.get(request_id)

    def add_input(
        self,
        change_request_id: str,
        *,
        incident_snapshot: dict[str, object],
        actor_id: str,
        content: str,
        idempotency_key: str,
        planner: Planner,
    ) -> dict[str, object]:
        content = _text(content, "content", 4000)
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        _reject_credentials(content)
        _reject_executable_proposals(content)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT cr.*, phase.id AS phase_id, phase.status AS phase_status
                FROM change_requests cr JOIN change_plan_phases phase ON phase.change_request_id = cr.id
                WHERE cr.id = ? ORDER BY phase.sequence DESC LIMIT 1
                """,
                (change_request_id,),
            ).fetchone()
            if row is None:
                raise ChangeRequestError("not_found", "Change Request not found")
            duplicate = conn.execute(
                "SELECT 1 FROM change_request_inputs WHERE change_request_id = ? AND idempotency_key = ?",
                (change_request_id, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                conn.rollback()
                return self.get(change_request_id)
            if str(row["phase_status"]) != "needs_input":
                raise ChangeRequestError("input_not_expected", "Change Request is not waiting for input")
            revision = conn.execute(
                "SELECT question FROM change_plan_revisions WHERE phase_id = ? AND status = 'needs_input' ORDER BY revision DESC LIMIT 1",
                (row["phase_id"],),
            ).fetchone()
            if revision is None or not revision["question"]:
                raise ChangeRequestError("input_not_expected", "Change Request has no blocking question")
            conn.execute(
                "INSERT INTO change_request_inputs (id, change_request_id, actor_id, question, content, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._id_factory("change-input"), change_request_id, actor_id, revision["question"], content, idempotency_key, now),
            )
            conn.execute(
                "UPDATE change_plan_phases SET status = 'planning', updated_at = ? WHERE id = ?",
                (now, row["phase_id"]),
            )
            conn.execute("UPDATE change_requests SET updated_at = ? WHERE id = ?", (now, change_request_id))
            _append_event(conn, change_request_id, "change_request.input_received", actor_id, {}, now)
            conn.commit()
        current = self.get(change_request_id)
        result = planner(
            {
                "change_request_id": change_request_id,
                "incident_id": current["incident_id"],
                "desired_outcome": current["desired_outcome"],
                "context": current["context"],
                "facts": _sanitized_facts(incident_snapshot),
                "inputs": self._inputs(change_request_id),
            }
        )
        self._finalize(change_request_id, actor_id, result)
        return self.get(change_request_id)

    def get(self, change_request_id: str) -> dict[str, object]:
        with self._database.connect() as conn:
            row = conn.execute("SELECT * FROM change_requests WHERE id = ?", (change_request_id,)).fetchone()
            if row is None:
                raise ChangeRequestError("not_found", "Change Request not found")
            return _project(conn, row)

    def list_for_incident(self, incident_id: str) -> list[dict[str, object]]:
        with self._database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM change_requests WHERE incident_id = ? ORDER BY created_at, id",
                (incident_id,),
            ).fetchall()
            return [_project(conn, row) for row in rows]

    def _inputs(self, change_request_id: str) -> list[dict[str, str]]:
        with self._database.connect() as conn:
            rows = conn.execute(
                "SELECT question, content FROM change_request_inputs WHERE change_request_id = ? ORDER BY created_at, rowid",
                (change_request_id,),
            ).fetchall()
            return [{"question": str(row["question"]), "content": str(row["content"])} for row in rows]

    def _finalize(self, change_request_id: str, actor_id: str, raw_result: dict[str, object]) -> None:
        result = _planning_result(raw_result)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, status FROM change_plan_phases WHERE change_request_id = ? ORDER BY sequence DESC LIMIT 1",
                (change_request_id,),
            ).fetchone()
            if row is None:
                raise ChangeRequestError("not_found", "Change Request not found")
            if str(row["status"]) != "planning":
                raise ChangeRequestError("planning_conflict", "Change Request is no longer planning")
            previous = conn.execute(
                "SELECT id, revision FROM change_plan_revisions WHERE change_request_id = ? AND status != 'superseded' ORDER BY revision DESC LIMIT 1",
                (change_request_id,),
            ).fetchone()
            revision = int(previous["revision"]) + 1 if previous is not None else 1
            if previous is not None:
                conn.execute(
                    "UPDATE change_plan_revisions SET status = 'superseded', superseded_at = ? WHERE id = ?",
                    (now, previous["id"]),
                )
                _append_event(
                    conn,
                    change_request_id,
                    "change_request.revision_superseded",
                    actor_id,
                    {"revision": int(previous["revision"])},
                    now,
                )
            revision_id = self._id_factory("plan-revision")
            conn.execute(
                """
                INSERT INTO change_plan_revisions (
                    id, change_request_id, phase_id, revision, status, question, plan_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    change_request_id,
                    row["id"],
                    revision,
                    result["status"],
                    result.get("question"),
                    json.dumps(result["plan"], ensure_ascii=False, sort_keys=True) if result.get("plan") else None,
                    now,
                ),
            )
            conn.execute(
                "UPDATE change_plan_phases SET status = ?, updated_at = ? WHERE id = ?",
                (result["status"], now, row["id"]),
            )
            conn.execute("UPDATE change_requests SET updated_at = ? WHERE id = ?", (now, change_request_id))
            _append_event(
                conn,
                change_request_id,
                f"change_request.{result['status']}",
                None,
                {"revision": revision, "revision_id": revision_id},
                now,
            )
            conn.commit()


def _sanitized_facts(snapshot: dict[str, object]) -> dict[str, object]:
    incident = snapshot["incident"]
    resource = snapshot["resource_context"]
    assert isinstance(incident, dict) and isinstance(resource, dict)
    incident_keys = ("id", "title", "severity", "status", "lifecycle_state", "binding_status", "evidence_revision")
    resource_keys = (
        "cluster_id", "environment", "runtime_status", "namespace", "workload_kind", "workload_name",
        "deployment_target_id", "service_id", "team_id", "resource_binding_id", "binding_revision",
    )
    evidence = []
    for step in snapshot.get("evidence_steps", []):
        if not isinstance(step, dict):
            continue
        evidence.append(
            {key: step.get(key) for key in ("id", "purpose", "source", "scope", "state", "evidence_references")}
        )
    return {
        "incident": {key: incident.get(key) for key in incident_keys},
        "resource": {key: resource.get(key) for key in resource_keys},
        "evidence_steps": evidence,
    }


def _planning_result(raw: dict[str, object]) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ChangeRequestError("invalid_plan", "Diagnosis planning response must be an object")
    status = raw.get("status")
    if status == "needs_input":
        if set(raw) - {"status", "question"}:
            raise ChangeRequestError("invalid_plan", "needs_input response contains unsupported fields")
        return {"status": status, "question": _text(raw.get("question"), "question", 2000)}
    if status != "validating" or set(raw) - {"status", "plan"}:
        raise ChangeRequestError("invalid_plan", "planning response must be needs_input or validating")
    plan = raw.get("plan")
    if not isinstance(plan, dict) or set(plan) != {"summary", "changes"}:
        raise ChangeRequestError("invalid_plan", "validating response requires a structured plan")
    summary = _text(plan.get("summary"), "plan.summary", 2000)
    changes = plan.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
        raise ChangeRequestError("invalid_plan", "plan.changes must contain between 1 and 100 changes")
    normalized = []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"target", "desired_state", "post_check"}:
            raise ChangeRequestError("invalid_plan", "each draft change requires target, desired_state, and post_check")
        target = change.get("target")
        if not isinstance(target, dict) or set(target) != {"api_version", "kind", "namespace", "name"}:
            raise ChangeRequestError("invalid_plan", "each draft target must be exact")
        namespace = target.get("namespace")
        if namespace is not None and not isinstance(namespace, str):
            raise ChangeRequestError("invalid_plan", "target.namespace must be a string or null")
        normalized.append(
            {
                "target": {
                    "api_version": _text(target.get("api_version"), "target.api_version", 200),
                    "kind": _text(target.get("kind"), "target.kind", 200),
                    "namespace": _optional_text(namespace, "target.namespace", 253) or None,
                    "name": _text(target.get("name"), "target.name", 253),
                },
                "desired_state": _text(change.get("desired_state"), "desired_state", 4000),
                "post_check": _text(change.get("post_check"), "post_check", 2000),
            }
        )
    return {"status": status, "plan": {"summary": summary, "changes": normalized}}


def _project(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    phase = conn.execute(
        "SELECT * FROM change_plan_phases WHERE change_request_id = ? ORDER BY sequence DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    if phase is None:
        raise ChangeRequestError("invalid_state", "Change Request has no active Phase")
    revisions = conn.execute(
        "SELECT * FROM change_plan_revisions WHERE change_request_id = ? ORDER BY revision",
        (row["id"],),
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM change_request_events WHERE change_request_id = ? ORDER BY event_id",
        (row["id"],),
    ).fetchall()
    projected_revisions = [_revision(item) for item in revisions]
    active = next((item for item in reversed(projected_revisions) if item["status"] != "superseded"), None)
    return {
        "id": str(row["id"]),
        "incident_id": str(row["incident_id"]),
        "submitted_by": str(row["actor_id"]),
        "desired_outcome": str(row["desired_outcome"]),
        "context": str(row["context"]),
        "status": str(phase["status"]),
        "active_phase": {
            "id": str(phase["id"]),
            "sequence": int(phase["sequence"]),
            "status": str(phase["status"]),
            "created_at": float(phase["created_at"]),
            "updated_at": float(phase["updated_at"]),
        },
        "active_revision": active,
        "revisions": projected_revisions,
        "events": [
            {
                "id": int(event["event_id"]),
                "type": str(event["type"]),
                "actor_id": event["actor_id"],
                "payload": json.loads(str(event["payload_json"])),
                "created_at": float(event["created_at"]),
            }
            for event in events
        ],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _revision(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "number": int(row["revision"]),
        "status": str(row["status"]),
        "question": row["question"],
        "plan": json.loads(str(row["plan_json"])) if row["plan_json"] is not None else None,
        "created_at": float(row["created_at"]),
        "superseded_at": float(row["superseded_at"]) if row["superseded_at"] is not None else None,
    }


def _append_event(
    conn: sqlite3.Connection,
    change_request_id: str,
    event_type: str,
    actor_id: str | None,
    payload: dict[str, object],
    now: float,
) -> None:
    conn.execute(
        "INSERT INTO change_request_events (change_request_id, type, actor_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (change_request_id, event_type, actor_id, json.dumps(payload, sort_keys=True), now),
    )


def _reject_credentials(*values: str) -> None:
    if any(_CREDENTIAL.search(value) for value in values):
        raise ChangeRequestError("secure_input_required", "Sensitive values must be supplied through Secure Input")


def _reject_executable_proposals(*values: str) -> None:
    for value in values:
        if _KUBERNETES_MANIFEST.search(value) or _KUBECTL.search(value):
            raise ChangeRequestError("executable_proposal_forbidden", "Submit the desired outcome, not an executable proposal")
        try:
            structured = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(structured, dict) and {"apiVersion", "kind"} <= set(structured):
            raise ChangeRequestError("executable_proposal_forbidden", "Submit the desired outcome, not a Kubernetes object")
        if isinstance(structured, list) and structured and all(
            isinstance(item, dict) and {"op", "path"} <= set(item) for item in structured
        ):
            raise ChangeRequestError("executable_proposal_forbidden", "Submit the desired outcome, not JSON Patch")


def _text(value: object, field: str, limit: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > limit:
        raise ChangeRequestError("invalid_request", f"{field} is required and must not exceed {limit} characters")
    return normalized


def _optional_text(value: object, field: str, limit: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if len(normalized) > limit:
        raise ChangeRequestError("invalid_request", f"{field} must not exceed {limit} characters")
    return normalized
