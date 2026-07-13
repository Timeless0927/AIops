"""Gateway-owned Change Request and immutable Plan Revision state."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

from aiops.contracts import ChangePlanningContractError, validate_change_planning_result

from .gateway_db import GatewayDatabase, register_migrations
from .change_request_projection import ChangeRequestProjectionError, project_change_request_in
from . import kubernetes_change_execution_schema as _execution_schema  # noqa: F401
from .kubernetes_change_validation import (
    KubernetesChangeValidation,
    KubernetesChangeValidationError,
)


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

_RETRY_SCHEMA_VERSION = 17
_RETRY_SCHEMA = """
CREATE TABLE change_planning_retries (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(change_request_id, idempotency_key)
);
"""
register_migrations(((_RETRY_SCHEMA_VERSION, _RETRY_SCHEMA),))

_VALIDATION_PHASE_SCHEMA_VERSION = 20
_VALIDATION_PHASE_SCHEMA = """
ALTER TABLE change_plan_revisions RENAME TO change_plan_revisions_v17;
ALTER TABLE change_plan_phases RENAME TO change_plan_phases_v16;

CREATE TABLE change_plan_phases (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL CHECK (status IN ('planning', 'needs_input', 'validating', 'awaiting_approval')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(change_request_id, sequence)
);
INSERT INTO change_plan_phases SELECT * FROM change_plan_phases_v16;

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
INSERT INTO change_plan_revisions SELECT * FROM change_plan_revisions_v17;
DROP TABLE change_plan_revisions_v17;
DROP TABLE change_plan_phases_v16;
"""
register_migrations(((_VALIDATION_PHASE_SCHEMA_VERSION, _VALIDATION_PHASE_SCHEMA),))

_APPROVAL_PHASE_SCHEMA_VERSION = 22
_APPROVAL_PHASE_SCHEMA = """
ALTER TABLE change_plan_phases ADD COLUMN approval_status TEXT
    CHECK (approval_status IS NULL OR approval_status IN ('approved', 'expired'));
"""
register_migrations(((_APPROVAL_PHASE_SCHEMA_VERSION, _APPROVAL_PHASE_SCHEMA),))

_PROPOSAL_AUDIT_SCHEMA_VERSION = 25
_PROPOSAL_AUDIT_SCHEMA = """
CREATE TABLE rejected_change_request_proposals (
    change_request_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, actor_id TEXT NOT NULL,
    desired_outcome TEXT NOT NULL,
    context TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    result TEXT NOT NULL, reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""
register_migrations(((_PROPOSAL_AUDIT_SCHEMA_VERSION, _PROPOSAL_AUDIT_SCHEMA),))

_CREDENTIAL = re.compile(
    r"(?is)(?:\b(?:password|passwd|token|api[_ -]?key|secret|credential)\b\s*(?::|=|\bis\b|是)\s*\S+|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_YAML_API_VERSION = re.compile(r"(?im)^\s*apiVersion\s*:\s*\S+")
_YAML_KIND = re.compile(r"(?im)^\s*kind\s*:\s*\S+")
_EXECUTABLE_TEXT = re.compile(
    r"(?i)(?:\b(?:kubectl|helm)\s+\S+|\b(?:bash|sh|zsh)\s+-c\b|"
    r"\b(?:curl|wget)\s+\S+[^\n]*(?:\|\s*(?:bash|sh|zsh)\b)|```(?:sh|bash)\b)"
)
_COMMAND_LINE = re.compile(
    r"(?m)^\s*(?:\$\s+|sudo\s+|env\s+)?(?:kubectl|oc|helm|docker|podman|crictl|"
    r"bash|sh|zsh|python\d*|node|ruby|perl|curl|wget|rm|cp|mv|sed|awk|jq|yq|"
    r"systemctl|service|make|ansible|terraform)\b"
)
_SHELL_SYNTAX = re.compile(r"(?m)^\s*(?:\./|/)[^\s]+|&&|\|\||\$\(|(?:^|\s)(?:\d+[<>]|>>|<<)\s*\S+")
_JSON_FENCE = re.compile(r"(?is)^\s*```json\s*(.*?)\s*```\s*$")

Planner = Callable[[dict[str, object]], dict[str, object]]
PlanAuthorizer = Callable[[dict[str, object]], bool]
PhaseAccess = Callable[[str, str, str], tuple[bool, dict[str, object] | None]]


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
        validation: KubernetesChangeValidation | None = None,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")
        self._validation = validation

    def submit(
        self,
        *,
        incident_id: str,
        facts: dict[str, object],
        actor_id: str,
        desired_outcome: str,
        context: str,
        idempotency_key: str,
        request_id: str,
        planner: Planner,
        plan_authorizer: PlanAuthorizer | None = None,
    ) -> tuple[bool, dict[str, object]]:
        desired_outcome = _text(desired_outcome, "desired_outcome", 2000)
        context = _optional_text(context, "context", 4000)
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        request_id = _text(request_id, "request_id", 200)
        _reject_credentials(desired_outcome, context)
        _reject_executable_proposals(desired_outcome, context)
        incident_id = _text(incident_id, "incident_id", 200)
        now = self._clock()
        change_request_id = self._id_factory("change-request")
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id, incident_id, desired_outcome, context FROM change_requests WHERE actor_id = ? AND idempotency_key = ?",
                (actor_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                if (
                    str(existing["incident_id"]) != incident_id
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
                (change_request_id, incident_id, actor_id, desired_outcome, context, idempotency_key, now, now),
            )
            phase_id = self._id_factory("plan-phase")
            conn.execute(
                "INSERT INTO change_plan_phases (id, change_request_id, sequence, status, created_at, updated_at) VALUES (?, ?, 1, 'planning', ?, ?)",
                (phase_id, change_request_id, now, now),
            )
            _append_event(
                conn, change_request_id, "change_request.created", actor_id,
                {"phase_id": phase_id, "phase_sequence": 1, "status": "planning"}, now,
            )
            conn.commit()
        result = self._call_planner(change_request_id, actor_id, planner,
            {
                "change_request_id": change_request_id,
                "incident_id": incident_id,
                "desired_outcome": desired_outcome,
                "context": context,
                "facts": facts,
                "inputs": [],
            },
        )
        try:
            self._finalize(
                change_request_id, actor_id, result, cluster_id=_planning_cluster_id(facts),
                plan_authorizer=plan_authorizer, request_id=request_id,
            )
        except ChangeRequestError as exc:
            if exc.code == "proposal_forbidden":
                with self._database.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        INSERT INTO rejected_change_request_proposals (
                            change_request_id, incident_id, actor_id, desired_outcome,
                            context, idempotency_key, request_id, result, reason, created_at
                        )
                        SELECT id, incident_id, actor_id, desired_outcome, context,
                               idempotency_key, ?, 'proposal_forbidden', 'authority_scope', ?
                        FROM change_requests WHERE id = ?
                        """,
                        (request_id, self._clock(), change_request_id),
                    )
                    conn.execute(
                        "DELETE FROM change_requests WHERE id = ? AND NOT EXISTS "
                        "(SELECT 1 FROM change_plan_revisions WHERE change_request_id = ?)",
                        (change_request_id, change_request_id),
                    )
                    conn.commit()
            raise
        return True, self.get(change_request_id)

    def add_input(
        self,
        change_request_id: str,
        *,
        facts: dict[str, object],
        actor_id: str,
        content: str,
        idempotency_key: str,
        request_id: str,
        planner: Planner,
        plan_authorizer: PlanAuthorizer | None = None,
    ) -> dict[str, object]:
        content = _text(content, "content", 4000)
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        request_id = _text(request_id, "request_id", 200)
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
                "SELECT actor_id, content FROM change_request_inputs WHERE change_request_id = ? AND idempotency_key = ?",
                (change_request_id, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                conn.rollback()
                if str(duplicate["actor_id"]) != actor_id or str(duplicate["content"]) != content:
                    raise ChangeRequestError("idempotency_conflict", "Idempotency key is already used by another input")
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
        result = self._call_planner(change_request_id, actor_id, planner,
            {
                "change_request_id": change_request_id,
                "incident_id": current["incident_id"],
                "desired_outcome": current["desired_outcome"],
                "context": current["context"],
                "facts": facts,
                "inputs": self._inputs(change_request_id),
            },
        )
        self._finalize(
            change_request_id, actor_id, result, cluster_id=_planning_cluster_id(facts),
            plan_authorizer=plan_authorizer, request_id=request_id,
        )
        return self.get(change_request_id)

    def retry(
        self,
        change_request_id: str,
        *,
        facts: dict[str, object],
        actor_id: str,
        idempotency_key: str,
        request_id: str,
        planner: Planner,
        plan_authorizer: PlanAuthorizer | None = None,
    ) -> dict[str, object]:
        idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        request_id = _text(request_id, "request_id", 200)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_phase = conn.execute(
                "SELECT status FROM change_plan_phases WHERE change_request_id = ? ORDER BY sequence DESC LIMIT 1",
                (change_request_id,),
            ).fetchone()
            if current_phase is None:
                raise ChangeRequestError("not_found", "Change Request not found")
            duplicate = conn.execute(
                "SELECT actor_id FROM change_planning_retries WHERE change_request_id = ? AND idempotency_key = ?",
                (change_request_id, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                conn.rollback()
                if str(duplicate["actor_id"]) != actor_id:
                    raise ChangeRequestError("idempotency_conflict", "Idempotency key is already used by another retry")
                return self.get(change_request_id)
            if str(current_phase["status"]) != "planning":
                raise ChangeRequestError("planning_not_retryable", "Change Request is not waiting for planning retry")
            conn.execute(
                "INSERT INTO change_planning_retries (id, change_request_id, actor_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?)",
                (self._id_factory("planning-retry"), change_request_id, actor_id, idempotency_key, now),
            )
            _append_event(conn, change_request_id, "change_request.planning_retried", actor_id, {}, now)
            conn.commit()
        current = self.get(change_request_id)
        result = self._call_planner(
            change_request_id,
            actor_id,
            planner,
            {
                "change_request_id": change_request_id,
                "incident_id": current["incident_id"],
                "desired_outcome": current["desired_outcome"],
                "context": current["context"],
                "facts": facts,
                "inputs": self._inputs(change_request_id),
            },
        )
        self._finalize(
            change_request_id, actor_id, result, cluster_id=_planning_cluster_id(facts),
            plan_authorizer=plan_authorizer, request_id=request_id,
        )
        return self.get(change_request_id)

    def get(self, change_request_id: str) -> dict[str, object]:
        with self._database.connect() as conn:
            row = conn.execute("SELECT * FROM change_requests WHERE id = ?", (change_request_id,)).fetchone()
            if row is None:
                raise ChangeRequestError("not_found", "Change Request not found")
            try:
                return project_change_request_in(conn, row)
            except ChangeRequestProjectionError as exc:
                raise ChangeRequestError("invalid_state", str(exc)) from exc

    def list_for_incident(self, incident_id: str) -> list[dict[str, object]]:
        with self._database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM change_requests WHERE incident_id = ? ORDER BY created_at, id",
                (incident_id,),
            ).fetchall()
            try:
                return [project_change_request_in(conn, row) for row in rows]
            except ChangeRequestProjectionError as exc:
                raise ChangeRequestError("invalid_state", str(exc)) from exc

    def project_for_actor(
        self,
        item: dict[str, object],
        *,
        actor_id: str,
        phase_access: PhaseAccess,
    ) -> dict[str, object]:
        projected = copy.deepcopy(item)
        status = str(projected["status"])
        visible, review = phase_access(str(projected["id"]), actor_id, status)
        if not visible:
            _redact_plan(projected)
        if status in {"awaiting_approval", "approved", "expired"}:
            projected["phase_review"] = review if visible else None
            if visible and review is not None:
                projected["status"] = review["status"]
                active_phase = projected.get("active_phase")
                if isinstance(active_phase, dict):
                    active_phase["status"] = review["status"]
        return projected

    def list_for_incident_for_actor(
        self, incident_id: str, *, actor_id: str, phase_access: PhaseAccess,
    ) -> list[dict[str, object]]:
        return [
            self.project_for_actor(item, actor_id=actor_id, phase_access=phase_access)
            for item in self.list_for_incident(incident_id)
        ]

    def record_validation_result_in(
        self,
        conn: sqlite3.Connection,
        command_id: str,
        result: dict[str, object],
        now: float,
    ) -> None:
        if self._validation is None:
            return
        event = self._validation.record_result_in(conn, command_id, result, now)
        if event is not None:
            change_request_id, event_type, revision_id = event
            phase_status = "awaiting_approval" if event_type == "change_request.validation_succeeded" else "planning"
            conn.execute(
                """UPDATE change_plan_phases SET status = ?, updated_at = ?
                   WHERE id = (SELECT phase_id FROM change_plan_revisions WHERE id = ?)""",
                (phase_status, now, revision_id),
            )
            conn.execute("UPDATE change_requests SET updated_at = ? WHERE id = ?", (now, change_request_id))
            _append_event(conn, change_request_id, event_type, None, {"revision_id": revision_id}, now)

    def _inputs(self, change_request_id: str) -> list[dict[str, str]]:
        with self._database.connect() as conn:
            rows = conn.execute(
                "SELECT question, content FROM change_request_inputs WHERE change_request_id = ? ORDER BY created_at, rowid",
                (change_request_id,),
            ).fetchall()
            return [{"question": str(row["question"]), "content": str(row["content"])} for row in rows]

    def _finalize(
        self,
        change_request_id: str,
        actor_id: str,
        raw_result: dict[str, object],
        *,
        cluster_id: str | None,
        plan_authorizer: PlanAuthorizer | None,
        request_id: str,
    ) -> None:
        result = _planning_result(raw_result)
        if (
            result["status"] == "validating"
            and plan_authorizer is not None
            and not plan_authorizer(result["plan"])
        ):
            now = self._clock()
            with self._database.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                _append_event(
                    conn, change_request_id, "change_request.proposal_rejected", actor_id,
                    {"reason": "authority_scope", "request_id": request_id}, now,
                )
                conn.execute(
                    "UPDATE change_requests SET updated_at = ? WHERE id = ?",
                    (now, change_request_id),
                )
                conn.commit()
            raise ChangeRequestError("proposal_forbidden", "Change Authority does not cover the proposed targets")
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
                if self._validation is not None:
                    self._validation.supersede_revision_in(conn, str(previous["id"]), now)
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
            if result["status"] == "validating" and self._validation is not None:
                if cluster_id is None:
                    raise ChangeRequestError("invalid_plan", "Planning facts do not identify an exact Cluster")
                try:
                    validation_status = self._validation.begin_in(
                        conn,
                        change_request_id=change_request_id,
                        phase_id=str(row["id"]),
                        revision_id=revision_id,
                        cluster_id=cluster_id,
                        plan=result["plan"],
                        now=now,
                    )
                except KubernetesChangeValidationError as exc:
                    raise ChangeRequestError(exc.code, str(exc)) from exc
                _append_event(
                    conn,
                    change_request_id,
                    "change_request.validation_started",
                    None,
                    {"revision_id": revision_id, "change_count": len(result["plan"]["changes"]), "cluster_id": cluster_id},
                    now,
                )
                if validation_status == "failed":
                    conn.execute(
                        "UPDATE change_plan_phases SET status = 'planning', updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                    _append_event(
                        conn,
                        change_request_id,
                        "change_request.validation_failed",
                        None,
                        {"revision_id": revision_id},
                        now,
                    )
            conn.commit()

    def _call_planner(
        self,
        change_request_id: str,
        actor_id: str,
        planner: Planner,
        payload: dict[str, object],
    ) -> dict[str, object]:
        try:
            return planner(payload)
        except ChangeRequestError as exc:
            now = self._clock()
            with self._database.connect() as conn:
                _append_event(
                    conn,
                    change_request_id,
                    "change_request.planning_failed",
                    actor_id,
                    {"reason": exc.code},
                    now,
                )
                conn.commit()
            raise


def _planning_result(raw: dict[str, object]) -> dict[str, object]:
    try:
        return validate_change_planning_result(raw)
    except ChangePlanningContractError as exc:
        raise ChangeRequestError("invalid_plan", f"Diagnosis planning response is invalid: {exc}") from exc


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
        if (
            (_YAML_API_VERSION.search(value) and _YAML_KIND.search(value))
            or _EXECUTABLE_TEXT.search(value)
            or _COMMAND_LINE.search(value)
            or _SHELL_SYNTAX.search(value)
        ):
            raise ChangeRequestError("executable_proposal_forbidden", "Submit the desired outcome, not an executable proposal")
        try:
            fenced = _JSON_FENCE.fullmatch(value)
            structured = json.loads(fenced.group(1) if fenced else value)
        except json.JSONDecodeError:
            continue
        if _contains_executable_proposal(structured):
            raise ChangeRequestError("executable_proposal_forbidden", "Submit the desired outcome, not executable data")


def _contains_executable_proposal(value: object) -> bool:
    if isinstance(value, dict):
        if {"apiVersion", "kind"} <= set(value) or {"op", "path"} <= set(value):
            return True
        return any(_contains_executable_proposal(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_executable_proposal(item) for item in value)
    return False


def _redact_plan(projected: dict[str, object]) -> None:
    for revision in projected.get("revisions", []):
        if isinstance(revision, dict):
            revision["plan"] = None
            revision["validation"] = None
    active = projected.get("active_revision")
    if isinstance(active, dict):
        active["plan"] = None
        active["validation"] = None


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


def _planning_cluster_id(facts: dict[str, object]) -> str | None:
    resource = facts.get("resource")
    value = resource.get("cluster_id") if isinstance(resource, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None
