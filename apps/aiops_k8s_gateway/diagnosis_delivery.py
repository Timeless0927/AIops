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

from .gateway_db import GatewayDatabase, register_migrations


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
        self._send = send or self._send_http
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

    def accept_writeback(self, payload: JSON) -> JSON:
        request_id = _required_text(payload, "request_id")
        incident_id = _required_text(payload, "incident_id")
        investigation_id = _required_text(payload, "investigation_id")
        outcome = _required_text(payload, "status")
        if outcome not in {"diagnosed", "partial", "needs_human", "completed", "failed"}:
            raise DiagnosisDeliveryError("invalid_result", "unsupported diagnosis result status")
        if not isinstance(payload.get("diagnosis"), dict):
            raise DiagnosisDeliveryError("invalid_result", "diagnosis must be an object")
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
            if row["result_hash"] is not None:
                if str(row["result_hash"]) != result_hash:
                    raise DiagnosisDeliveryError("result_conflict", "Diagnosis Request already has a different result")
                return {"ok": True, "duplicate": True}
            if row["status"] in {"rejected", "expired", "cancelled"}:
                raise DiagnosisDeliveryError("request_terminal", "Diagnosis Request is already terminal")
            conn.execute(
                """
                UPDATE diagnosis_requests
                SET status = 'accepted', accepted_at = COALESCE(accepted_at, ?),
                    result_hash = ?, result_json = ?, outcome = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, result_hash, canonical, outcome, now, request_id),
            )
            lifecycle = "failed" if outcome == "failed" else "completed"
            conn.execute(
                "UPDATE investigations SET status = ?, updated_at = ? WHERE id = ? AND status IN ('queued', 'running', 'paused', 'human_led')",
                (lifecycle, now, investigation_id),
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
                conn.execute(
                    "UPDATE investigations SET status = 'terminated', updated_at = ? WHERE id = ? AND status = 'queued'",
                    (now, investigation_id),
                )
            conn.commit()
        return bool(changed)

    def _deliver(self, request_id: str, now: float) -> None:
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM diagnosis_requests WHERE id = ?", (request_id,)).fetchone()
            if row is None or row["status"] != "pending":
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
                conn.execute(
                    """
                    UPDATE investigations SET status = 'running', updated_at = ?
                    WHERE id = (SELECT investigation_id FROM diagnosis_requests WHERE id = ?) AND status = 'queued'
                    """,
                    (now, request_id),
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
        return {
            "request_id": request_id,
            "session_id": request_id,
            "incident_id": str(row["incident_id"]),
            "investigation_id": str(row["investigation_id"]),
            "source": "gateway",
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
        conn.execute(
            """
            UPDATE investigations SET status = 'failed', updated_at = ?
            WHERE id = (SELECT investigation_id FROM diagnosis_requests WHERE id = ?) AND status = 'queued'
            """,
            (now, request_id),
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

    @staticmethod
    def _send_http(payload: JSON) -> tuple[int, JSON]:
        base_url = os.getenv("AIOPS_DIAGNOSIS_URL", "").strip()
        if not base_url:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"status": "diagnosis_unconfigured"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
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
