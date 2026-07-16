"""Gateway-owned durable delivery of Investigation work to Diagnosis."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from http import HTTPStatus
from pathlib import Path
from typing import Callable
from urllib import error, request

from apps.internal_auth import internal_auth_headers

from .evidence_decisions import EvidenceDecisionError, record_diagnosis_facts
from .gateway_db import GatewayDatabase, register_migrations
from .investigation_events import append_event, transition_investigation


JSON = dict[str, object]
Sender = Callable[[JSON], tuple[int, JSON]]

_SCHEMA_VERSION = 7
_SCHEMA = """
CREATE TABLE diagnosis_requests (
    id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected', 'expired', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    last_error TEXT,
    result_hash TEXT,
    result_json TEXT,
    outcome TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    accepted_at REAL,
    FOREIGN KEY (investigation_id) REFERENCES investigations(id)
);
CREATE INDEX diagnosis_requests_due ON diagnosis_requests(status, next_attempt_at, created_at);
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


class DiagnosisDeliveryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def persist_diagnosis_request(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    investigation_id: str,
    now: float,
    ttl_seconds: float,
) -> None:
    """Persist the request in the transaction that creates its Investigation."""
    conn.execute(
        """
        INSERT INTO diagnosis_requests (
            id, investigation_id, status, next_attempt_at, deadline_at, created_at, updated_at
        ) VALUES (?, ?, 'pending', ?, ?, ?, ?)
        """,
        (request_id, investigation_id, now, now + ttl_seconds, now, now),
    )


class DiagnosisDelivery:
    """Retries durable Diagnosis Requests and accepts idempotent results."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        send: Sender | None = None,
        clock: Callable[[], float] = time.time,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._send = send or send_diagnosis_request
        self._clock = clock
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._retry_max_seconds = max(self._retry_base_seconds, retry_max_seconds)

    def reconcile_due(self, *, limit: int = 20) -> int:
        now = self._clock()
        with self._database.connect() as conn:
            due = conn.execute(
                """
                SELECT id FROM diagnosis_requests
                WHERE status = 'pending' AND (next_attempt_at <= ? OR deadline_at <= ?)
                ORDER BY created_at, id LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
        for row in due:
            self._deliver(str(row["id"]), now)
        return len(due)

    def metrics(self) -> str:
        now = self._clock()
        with self._database.connect() as conn:
            pending, oldest = conn.execute(
                "SELECT COUNT(*), MIN(created_at) FROM diagnosis_requests WHERE status = 'pending'"
            ).fetchone()
        return (
            "# HELP aiops_gateway_diagnosis_requests Current pending Diagnosis Requests\n"
            "# TYPE aiops_gateway_diagnosis_requests gauge\n"
            f"aiops_gateway_diagnosis_requests {int(pending)}\n"
            "# HELP aiops_gateway_diagnosis_request_oldest_age_seconds Age of the oldest pending Diagnosis Request\n"
            "# TYPE aiops_gateway_diagnosis_request_oldest_age_seconds gauge\n"
            f"aiops_gateway_diagnosis_request_oldest_age_seconds {max(0.0, now - float(oldest)) if oldest else 0.0:.1f}\n"
        )

    def accept_writeback(self, payload: JSON) -> JSON:
        request_id = _required_text(payload, "request_id")
        incident_id = _required_text(payload, "incident_id")
        investigation_id = _required_text(payload, "investigation_id")
        provider_revision = _required_text(payload, "provider_revision")
        if len(provider_revision) > 256:
            raise DiagnosisDeliveryError("invalid_result", "provider_revision is too long")
        outcome = _required_text(payload, "status")
        if outcome not in {"diagnosed", "partial", "needs_human", "completed", "failed"}:
            raise DiagnosisDeliveryError("invalid_result", "unsupported diagnosis result status")
        if not isinstance(payload.get("diagnosis"), dict):
            raise DiagnosisDeliveryError("invalid_result", "diagnosis must be an object")
        diagnosis = payload["diagnosis"]
        dependencies = diagnosis.get("human_input_event_ids", [])  # type: ignore[union-attr]
        action_ids = diagnosis.get("recommended_action_ids", [])  # type: ignore[union-attr]
        if not isinstance(dependencies, list) or any(not isinstance(item, int) or item < 1 for item in dependencies):
            raise DiagnosisDeliveryError("invalid_result", "human_input_event_ids must contain positive integers")
        if not isinstance(action_ids, list) or any(not isinstance(item, str) or not item for item in action_ids):
            raise DiagnosisDeliveryError("invalid_result", "recommended_action_ids must contain non-empty strings")
        if not isinstance(payload.get("missing_evidence", []), list):
            raise DiagnosisDeliveryError("invalid_result", "missing_evidence must be a list")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT dr.*, i.incident_id
                FROM diagnosis_requests dr
                JOIN investigations i ON i.id = dr.investigation_id
                WHERE dr.id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                raise DiagnosisDeliveryError("request_not_found", "Diagnosis Request was not found")
            if str(row["investigation_id"]) != investigation_id or str(row["incident_id"]) != incident_id:
                raise DiagnosisDeliveryError("request_mismatch", "Diagnosis result does not match its Request")
            if dependencies:
                found = conn.execute(
                    f"SELECT COUNT(*) FROM investigation_events WHERE investigation_id = ? AND event_type LIKE 'human_input.%' AND event_id IN ({','.join('?' for _ in dependencies)})",
                    (investigation_id, *dependencies),
                ).fetchone()[0]
                if found != len(set(dependencies)):
                    raise DiagnosisDeliveryError("invalid_result", "diagnosis references unknown Human Input")
            if row["result_hash"] is not None:
                if str(row["result_hash"]) != result_hash:
                    raise DiagnosisDeliveryError("result_conflict", "Diagnosis Request already has a different result")
                return {"ok": True, "duplicate": True}
            if row["status"] in {"rejected", "expired", "cancelled"}:
                raise DiagnosisDeliveryError("request_terminal", "Diagnosis Request is already terminal")
            try:
                decisions = record_diagnosis_facts(
                    conn,
                    request_id=request_id,
                    investigation_id=investigation_id,
                    payload=payload,
                    created_at=now,
                )
            except EvidenceDecisionError as exc:
                raise DiagnosisDeliveryError(exc.code, exc.message) from exc
            conn.execute(
                """
                UPDATE diagnosis_requests
                SET status = 'accepted', accepted_at = COALESCE(accepted_at, ?),
                    result_hash = ?, result_json = ?, outcome = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, result_hash, canonical, outcome, now, request_id),
            )
            diagnosis_payload = dict(diagnosis)
            action_ids = [str(action["id"]) for action in decisions["recommended_actions"]]  # type: ignore[index]
            if action_ids:
                diagnosis_payload["recommended_action_ids"] = action_ids
            append_event(
                conn,
                investigation_id=investigation_id,
                event_type="diagnosis.output",
                idempotency_key=f"diagnosis-result:{request_id}",
                payload={
                    "status": outcome,
                    "diagnosis": diagnosis_payload,
                    "missing_evidence": payload.get("missing_evidence", []),
                },
                created_at=now,
            )
            for event_type, field in (("tool.activity", "tool_activity"),):
                items = payload.get(field, [])
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise DiagnosisDeliveryError("invalid_result", f"{field} must be a list of objects")
                for index, item in enumerate(items):
                    append_event(
                        conn,
                        investigation_id=investigation_id,
                        event_type=event_type,
                        idempotency_key=f"{field}:{request_id}:{index}",
                        payload=item,
                        created_at=now,
                    )
            for index, item in enumerate(decisions["evidence_steps"]):  # type: ignore[union-attr]
                append_event(
                    conn,
                    investigation_id=investigation_id,
                    event_type="evidence_step.changed",
                    idempotency_key=f"evidence_steps:{request_id}:{index}",
                    payload=item,
                    created_at=now,
                )
            lifecycle = "failed" if outcome == "failed" else "completed"
            transition_investigation(
                conn,
                investigation_id=investigation_id,
                to_status=lifecycle,
                allowed_from={"queued", "running", "paused"},
                reason="diagnosis_result",
                idempotency_key=f"diagnosis-lifecycle:{request_id}",
                created_at=now,
            )
            conn.commit()
        return {"ok": True, "duplicate": False}

    def cancel(self, investigation_id: str) -> bool:
        """Cancel an unaccepted Request and terminate its queued Investigation."""
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """
                UPDATE diagnosis_requests
                SET status = 'cancelled', updated_at = ?
                WHERE investigation_id = ? AND status = 'pending'
                """,
                (now, investigation_id),
            ).rowcount
            if changed:
                transition_investigation(
                    conn,
                    investigation_id=investigation_id,
                    to_status="terminated",
                    allowed_from={"queued"},
                    reason="diagnosis_cancelled",
                    idempotency_key="diagnosis-cancelled",
                    created_at=now,
                )
            conn.commit()
        return bool(changed)

    def _deliver(self, request_id: str, now: float) -> None:
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT dr.*, i.status AS investigation_status
                FROM diagnosis_requests dr JOIN investigations i ON i.id = dr.investigation_id
                WHERE dr.id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return
            if row["investigation_status"] in {"human_led", "terminated"}:
                conn.execute(
                    "UPDATE diagnosis_requests SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (now, request_id),
                )
                conn.commit()
                return
            if float(row["deadline_at"]) <= now:
                self._finish_request(conn, request_id, "expired", "Diagnosis acceptance deadline expired", now)
                conn.commit()
                return
            payload = self._payload(conn, row)
            attempt = int(row["attempt_count"]) + 1
            conn.execute(
                "UPDATE diagnosis_requests SET attempt_count = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (attempt, now + self._backoff(request_id, attempt), now, request_id),
            )
            conn.commit()

        try:
            status, response = self._send(payload)
        except Exception as exc:
            self._record_retry_error(request_id, f"{type(exc).__name__}: {exc}", now)
            return
        accepted = response.get("status") == "accepted" and response.get("request_id") == request_id
        if status == HTTPStatus.ACCEPTED and accepted:
            with self._database.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE diagnosis_requests SET status = 'accepted', accepted_at = ?, last_error = NULL, updated_at = ? WHERE id = ? AND status = 'pending'",
                    (now, now, request_id),
                )
                transition_investigation(
                    conn,
                    investigation_id=str(row["investigation_id"]),
                    to_status="running",
                    allowed_from={"queued"},
                    reason="diagnosis_accepted",
                    idempotency_key=f"diagnosis-accepted:{request_id}",
                    created_at=now,
                )
                conn.commit()
            return
        if status == HTTPStatus.ACCEPTED:
            self._record_retry_error(request_id, "Diagnosis returned an invalid acceptance", now)
            return
        message = str(response.get("error") or response.get("status") or f"HTTP {status}")
        if 400 <= status < 500 and status not in {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}:
            with self._database.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._finish_request(conn, request_id, "rejected", message, now)
                conn.commit()
            return
        self._record_retry_error(request_id, message, now)

    def _payload(self, conn: sqlite3.Connection, request_row: sqlite3.Row) -> JSON:
        row = conn.execute(
            """
            SELECT i.id AS investigation_id, i.incident_id, inc.cluster_id, inc.namespace,
                   inc.alertname, inc.severity, inc.workload_kind, inc.workload_name,
                   s.summary, s.status AS alert_status
            FROM investigations i
            JOIN incidents inc ON inc.id = i.incident_id
            LEFT JOIN alert_signals s ON s.id = (
                SELECT id FROM alert_signals WHERE incident_id = inc.id ORDER BY created_at DESC, id DESC LIMIT 1
            )
            WHERE i.id = ?
            """,
            (request_row["investigation_id"],),
        ).fetchone()
        if row is None:
            raise DiagnosisDeliveryError("investigation_not_found", "Diagnosis Request lost its Investigation")
        request_id = str(request_row["id"])
        human_inputs = []
        for event in conn.execute(
            """
            SELECT event_id, event_type, actor_id, payload_json, created_at
            FROM investigation_events
            WHERE investigation_id = ? AND event_type LIKE 'human_input.%'
            ORDER BY event_id
            """,
            (row["investigation_id"],),
        ):
            human_inputs.append(
                {
                    "event_id": int(event["event_id"]),
                    "kind": str(event["event_type"]).removeprefix("human_input."),
                    "actor_id": event["actor_id"],
                    "payload": json.loads(str(event["payload_json"])),
                    "created_at": float(event["created_at"]),
                }
            )
        return {
            "request_id": request_id,
            "session_id": request_id,
            "incident_id": str(row["incident_id"]),
            "investigation_id": str(row["investigation_id"]),
            "source": "gateway",
            "human_inputs": human_inputs,
            "alert": {
                "alertname": str(row["alertname"]),
                "severity": str(row["severity"]),
                "cluster": str(row["cluster_id"]),
                "namespace": str(row["namespace"]),
                "workload_kind": row["workload_kind"],
                "workload_name": row["workload_name"],
                "description": str(row["summary"] or ""),
                "status": str(row["alert_status"] or "firing"),
            },
        }

    def _finish_request(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        status: str,
        message: str,
        now: float,
    ) -> None:
        conn.execute(
            "UPDATE diagnosis_requests SET status = ?, last_error = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
            (status, message, now, request_id),
        )
        row = conn.execute("SELECT investigation_id FROM diagnosis_requests WHERE id = ?", (request_id,)).fetchone()
        if row is not None:
            transition_investigation(
                conn,
                investigation_id=str(row["investigation_id"]),
                to_status="failed",
                allowed_from={"queued"},
                reason=f"diagnosis_{status}",
                idempotency_key=f"diagnosis-{status}:{request_id}",
                created_at=now,
            )

    def _record_retry_error(self, request_id: str, message: str, now: float) -> None:
        with self._database.connect() as conn:
            conn.execute(
                "UPDATE diagnosis_requests SET last_error = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                (message[:1000], now, request_id),
            )

    def _backoff(self, request_id: str, attempt: int) -> float:
        delay = min(self._retry_max_seconds, self._retry_base_seconds * (2 ** max(0, attempt - 1)))
        jitter = 0.75 + int(hashlib.sha256(request_id.encode()).hexdigest()[:4], 16) / 65535 * 0.5
        return delay * jitter

def send_diagnosis_request(payload: JSON) -> tuple[int, JSON]:
    """Send one durable Gateway request across the Diagnosis HTTP boundary."""
    base_url = os.getenv("AIOPS_DIAGNOSIS_URL", "").strip()
    if not base_url:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"status": "diagnosis_unconfigured"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Request-ID": str(payload["request_id"]),
        "X-Correlation-ID": str(payload["incident_id"]),
    }
    headers.update(internal_auth_headers())
    req = request.Request(f"{base_url.rstrip('/')}/diagnosis/sessions", data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=2.0) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
            return response.status, data if isinstance(data, dict) else {"status": "invalid_response"}
    except error.HTTPError as exc:
        data = json.loads(exc.read().decode("utf-8") or "{}")
        return exc.code, data if isinstance(data, dict) else {"status": "invalid_response"}


def _required_text(payload: JSON, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DiagnosisDeliveryError("invalid_result", f"{field} is required")
    return value.strip()
