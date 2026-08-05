"""Diagnosis-owned durable Job execution and writeback."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from aiops.contracts.governed_skills import normalize_skill_bindings, skill_versions
from apps.service_http import record_sqlite_error
from diagnosis_service.database import connect, migrate


JSON = dict[str, object]
Runner = Callable[[JSON], JSON]
Sender = Callable[[JSON], tuple[int, JSON]]

_SCHEMA_VERSION = 1
_TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60
_CLEANUP_BATCH_SIZE = 1000
_SCHEMA = """
CREATE TABLE diagnosis_jobs (
    request_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at REAL NOT NULL,
    lease_until REAL,
    result_json TEXT,
    error TEXT,
    writeback_status TEXT NOT NULL DEFAULT 'none' CHECK (writeback_status IN ('none', 'pending', 'succeeded')),
    writeback_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (writeback_attempt_count >= 0),
    writeback_next_at REAL,
    writeback_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX diagnosis_jobs_execution_due ON diagnosis_jobs(status, next_attempt_at, created_at);
CREATE INDEX diagnosis_jobs_writeback_due ON diagnosis_jobs(writeback_status, writeback_next_at, created_at);
"""
_MIGRATIONS = (
    (_SCHEMA_VERSION, _SCHEMA),
    (2, "ALTER TABLE diagnosis_jobs ADD COLUMN finished_at REAL;"),
    (5, "ALTER TABLE diagnosis_jobs ADD COLUMN provider_revision TEXT;"),
    (7, "ALTER TABLE diagnosis_jobs ADD COLUMN loop_checkpoint_json TEXT;"),
)


class DiagnosisJobError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DiagnosisJobs:
    """Persists accepted Jobs and advances each retry boundary independently."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        clock: Callable[[], float] = time.time,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        execution_lease_seconds: float = 15 * 60,
        max_execution_attempts: int = 3,
    ) -> None:
        self.db_path = Path(db_path).expanduser() if db_path else self.default_path()
        self._clock = clock
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._retry_max_seconds = max(self._retry_base_seconds, retry_max_seconds)
        self._execution_lease_seconds = max(1.0, execution_lease_seconds)
        self._max_execution_attempts = max(1, max_execution_attempts)
        self._migrate()

    @staticmethod
    def default_path() -> Path:
        return Path(os.getenv("AIOPS_DATA_DIR", "data")).expanduser() / "diagnosis.db"

    def accept(self, payload: JSON, *, provider_revision: str | None = None) -> JSON:
        _validate_request(payload)
        canonical = _canonical(payload)
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        request_id = str(payload["request_id"])
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_hash, provider_revision FROM diagnosis_jobs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != request_hash or row["provider_revision"] != provider_revision:
                    raise DiagnosisJobError("request_conflict", "request_id is already used by another Diagnosis Job")
                return {"status": "accepted", "request_id": request_id, "duplicate": True}
            conn.execute(
                """
                INSERT INTO diagnosis_jobs (
                    request_id, request_hash, request_json, status,
                    next_attempt_at, created_at, updated_at, provider_revision
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (request_id, request_hash, canonical, now, now, now, provider_revision),
            )
            conn.commit()
        return {"status": "accepted", "request_id": request_id, "duplicate": False}

    def get(self, request_id: str) -> JSON | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM diagnosis_jobs WHERE request_id = ?", (request_id,)).fetchone()
        return _job_projection(row) if row is not None else None

    def list(self) -> list[JSON]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM diagnosis_jobs ORDER BY created_at, request_id").fetchall()
        return [_job_projection(row) for row in rows]

    def load_loop_checkpoint(self, request_id: str) -> JSON | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT loop_checkpoint_json FROM diagnosis_jobs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None or not row["loop_checkpoint_json"]:
            return None
        value = json.loads(str(row["loop_checkpoint_json"]))
        return value if isinstance(value, dict) else None

    def save_loop_checkpoint(self, request_id: str, checkpoint: JSON) -> None:
        encoded = _canonical(checkpoint)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("Diagnosis loop checkpoint exceeds 256 KiB")
        with self._connect() as conn:
            updated = conn.execute(
                """UPDATE diagnosis_jobs SET loop_checkpoint_json = ?, updated_at = ?
                   WHERE request_id = ? AND status IN ('queued', 'running')""",
                (encoded, self._clock(), request_id),
            ).rowcount
        if updated != 1:
            raise DiagnosisJobError("checkpoint_rejected", "Diagnosis Job cannot accept a loop checkpoint")

    def metrics(self) -> str:
        now = self._clock()
        with self._connect() as conn:
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM diagnosis_jobs GROUP BY status"))
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM diagnosis_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
            duration = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(finished_at - created_at), 0) FROM diagnosis_jobs WHERE finished_at IS NOT NULL"
            ).fetchone()
            writebacks = conn.execute(
                "SELECT COUNT(*), MIN(finished_at) FROM diagnosis_jobs WHERE writeback_status = 'pending'"
            ).fetchone()
            cleanup_eligible = conn.execute(
                """SELECT COUNT(*) FROM diagnosis_jobs
                   WHERE status IN ('completed', 'failed') AND writeback_status = 'succeeded'
                     AND finished_at <= ?""",
                (now - _TERMINAL_RETENTION_SECONDS,),
            ).fetchone()[0]
        lines = [
            "# HELP aiops_diagnosis_jobs Current Diagnosis Jobs by bounded outcome",
            "# TYPE aiops_diagnosis_jobs gauge",
        ]
        for status in ("queued", "running", "completed", "failed"):
            lines.append(f'aiops_diagnosis_jobs{{status="{status}"}} {int(counts.get(status, 0))}')
        lines.extend((
            "# HELP aiops_diagnosis_job_oldest_age_seconds Age of the oldest unfinished Diagnosis Job",
            "# TYPE aiops_diagnosis_job_oldest_age_seconds gauge",
            f"aiops_diagnosis_job_oldest_age_seconds {max(0.0, now - float(oldest)) if oldest else 0.0:.1f}",
            "# HELP aiops_diagnosis_duration_seconds Retained terminal Diagnosis Job duration",
            "# TYPE aiops_diagnosis_duration_seconds summary",
            f"aiops_diagnosis_duration_seconds_count {int(duration[0])}",
            f"aiops_diagnosis_duration_seconds_sum {float(duration[1]):.1f}",
            "# HELP aiops_diagnosis_writebacks Current pending Diagnosis writebacks",
            "# TYPE aiops_diagnosis_writebacks gauge",
            f"aiops_diagnosis_writebacks {int(writebacks[0])}",
            "# HELP aiops_diagnosis_writeback_oldest_age_seconds Age of the oldest pending Diagnosis writeback",
            "# TYPE aiops_diagnosis_writeback_oldest_age_seconds gauge",
            f"aiops_diagnosis_writeback_oldest_age_seconds {max(0.0, now - float(writebacks[1])) if writebacks[1] else 0.0:.1f}",
            "# HELP aiops_diagnosis_cleanup_eligible Terminal Diagnosis Jobs currently eligible for cleanup",
            "# TYPE aiops_diagnosis_cleanup_eligible gauge",
            f"aiops_diagnosis_cleanup_eligible {int(cleanup_eligible)}",
        ))
        return "\n".join(lines) + "\n"

    def cleanup_expired(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                """DELETE FROM diagnosis_jobs WHERE rowid IN (
                       SELECT rowid FROM diagnosis_jobs
                       WHERE status IN ('completed', 'failed') AND writeback_status = 'succeeded'
                         AND finished_at <= ? LIMIT ?
                   )""",
                (self._clock() - _TERMINAL_RETENTION_SECONDS, _CLEANUP_BATCH_SIZE),
            ).rowcount

    def export(self, request_id: str, *, artifact: str | None = None) -> JSON | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM diagnosis_jobs WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        request_payload = json.loads(str(row["request_json"]))
        result = json.loads(str(row["result_json"])) if row["result_json"] else None
        if artifact in (None, ""):
            return result or {
                "incident_id": request_payload["incident_id"],
                "session_id": request_payload["session_id"],
                "status": row["status"],
                "state_transitions": [row["status"]],
            }
        if result is None:
            return None
        if artifact == "diagnosis":
            diagnosis = result.get("diagnosis")
            return dict(diagnosis) if isinstance(diagnosis, dict) else None
        if artifact == "markdown":
            diagnosis = result.get("diagnosis")
            if not isinstance(diagnosis, dict):
                return None
            return {
                "session_id": result["session_id"],
                "incident_id": result["incident_id"],
                "markdown": diagnosis.get("markdown", ""),
            }
        if artifact == "timeline":
            return {
                "session_id": result["session_id"],
                "incident_id": result["incident_id"],
                "state_transitions": result.get("state_transitions", []),
                "steps": result.get("steps", []),
                "missing_evidence": result.get("missing_evidence", []),
                "writeback": {"status": row["writeback_status"], "error": row["writeback_error"]},
            }
        return None

    def run_execution_once(self, runner: Runner) -> bool:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM diagnosis_jobs
                WHERE (status = 'queued' AND next_attempt_at <= ?)
                   OR (status = 'running' AND lease_until <= ?)
                ORDER BY created_at, request_id LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return False
            request_id = str(row["request_id"])
            previous_attempts = int(row["attempt_count"])
            if row["status"] == "running" and previous_attempts >= self._max_execution_attempts:
                result = _failed_result(
                    str(row["request_json"]),
                    "Diagnosis Job execution lease expired",
                    provider_revision=row["provider_revision"],
                )
                conn.execute(
                    """
                    UPDATE diagnosis_jobs
                    SET status = 'failed', lease_until = NULL, result_json = ?, error = ?,
                        writeback_status = 'pending', writeback_next_at = ?, finished_at = ?, updated_at = ?
                    WHERE request_id = ? AND status = 'running'
                    """,
                    (result, "Diagnosis Job execution lease expired", now, now, now, request_id),
                )
                conn.commit()
                return True
            attempt = previous_attempts + 1
            conn.execute(
                """
                UPDATE diagnosis_jobs
                SET status = 'running', attempt_count = ?, lease_until = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (attempt, now + self._execution_lease_seconds, now, request_id),
            )
            conn.commit()
            payload = json.loads(str(row["request_json"]))
            if row["provider_revision"] is not None:
                payload["provider_revision"] = str(row["provider_revision"])
        try:
            result = runner(payload)
            if not isinstance(result, dict):
                raise TypeError("Diagnosis runner must return an object")
            _validate_result(result)
            if result["session_id"] != payload["session_id"] or result["incident_id"] != payload["incident_id"]:
                raise ValueError("Diagnosis result does not match its Job identity")
        except Exception as exc:
            self._record_execution_failure(request_id, attempt, exc)
            return True
        finished_at = self._clock()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE diagnosis_jobs
                SET status = 'completed', result_json = ?, error = NULL, lease_until = NULL,
                    writeback_status = 'pending', writeback_next_at = ?, finished_at = ?, updated_at = ?
                WHERE request_id = ? AND status = 'running'
                """,
                (_canonical(result), finished_at, finished_at, finished_at, request_id),
            )
        return True

    def run_writeback_once(self, sender: Sender) -> bool:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM diagnosis_jobs
                WHERE writeback_status = 'pending' AND writeback_next_at <= ?
                ORDER BY created_at, request_id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return False
            request_id = str(row["request_id"])
            attempt = int(row["writeback_attempt_count"]) + 1
            conn.execute(
                """
                UPDATE diagnosis_jobs
                SET writeback_attempt_count = ?, writeback_next_at = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (attempt, now + self._backoff(request_id, attempt), now, request_id),
            )
            conn.commit()
            request_payload = json.loads(str(row["request_json"]))
            result = json.loads(str(row["result_json"]))
        payload = {
            **result,
            "request_id": request_id,
            "incident_id": request_payload["incident_id"],
            "investigation_id": request_payload["investigation_id"],
        }
        try:
            status, response = sender(payload)
            accepted = 200 <= status < 300 and bool(response.get("ok"))
            error_message = None if accepted else str(response.get("error") or response.get("status") or f"HTTP {status}")
        except Exception as exc:
            accepted = False
            error_message = f"{type(exc).__name__}: {exc}"
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE diagnosis_jobs
                SET writeback_status = ?, writeback_error = ?, updated_at = ?
                WHERE request_id = ? AND writeback_status = 'pending'
                """,
                ("succeeded" if accepted else "pending", error_message, now, request_id),
            )
        return True

    def _record_execution_failure(self, request_id: str, attempt: int, exc: Exception) -> None:
        finished_at = self._clock()
        reason_code = str(getattr(exc, "code", "")) or None
        no_retry = bool(getattr(exc, "no_retry", False))
        message = (
            f"Model Provider 失败：{reason_code}"
            if no_retry and reason_code
            else f"{type(exc).__name__}: {exc}"[:1000]
        )
        terminal = no_retry or attempt >= self._max_execution_attempts
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_json, provider_revision FROM diagnosis_jobs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            result = (
                _failed_result(
                    str(row["request_json"]),
                    message,
                    reason_code=reason_code,
                    partial_result=getattr(exc, "partial_result", None),
                    provider_revision=row["provider_revision"],
                )
                if terminal
                else None
            )
            conn.execute(
                """
                UPDATE diagnosis_jobs
                SET status = ?, next_attempt_at = ?, lease_until = NULL, error = ?, result_json = ?,
                    writeback_status = ?, writeback_next_at = ?, finished_at = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (
                    "failed" if terminal else "queued",
                    finished_at + self._backoff(request_id, attempt),
                    message,
                    result,
                    "pending" if terminal else "none",
                    finished_at if terminal else None,
                    finished_at if terminal else None,
                    finished_at,
                    request_id,
                ),
            )

    def _backoff(self, request_id: str, attempt: int) -> float:
        delay = min(self._retry_max_seconds, self._retry_base_seconds * (2 ** max(0, attempt - 1)))
        jitter = 0.75 + int(hashlib.sha256(request_id.encode()).hexdigest()[:4], 16) / 65535 * 0.5
        return delay * jitter

    def _connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _migrate(self) -> None:
        migrate(self.db_path, _MIGRATIONS)


def start_workers(
    jobs: DiagnosisJobs,
    *,
    runner: Runner,
    sender: Sender,
    interval_seconds: float = 1.0,
    stop_event: threading.Event | None = None,
) -> tuple[threading.Thread, threading.Thread]:
    stop = stop_event or threading.Event()

    def work(step: Callable[[], bool], cleanup: bool) -> None:
        next_cleanup_at = 0.0
        while not stop.is_set():
            try:
                monotonic_now = time.monotonic()
                if cleanup and monotonic_now >= next_cleanup_at:
                    jobs.cleanup_expired()
                    next_cleanup_at = monotonic_now + 60 * 60
                worked = step()
            except sqlite3.Error:
                record_sqlite_error("diagnosis")
                worked = False
            if not worked:
                stop.wait(interval_seconds)

    execution = threading.Thread(
        target=work,
        args=(lambda: jobs.run_execution_once(runner), True),
        name="diagnosis-job-worker",
        daemon=True,
    )
    writeback = threading.Thread(
        target=work,
        args=(lambda: jobs.run_writeback_once(sender), False),
        name="diagnosis-writeback-worker",
        daemon=True,
    )
    execution.start()
    writeback.start()
    return execution, writeback


def _validate_request(payload: JSON) -> None:
    for field in ("request_id", "session_id", "incident_id", "investigation_id"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise DiagnosisJobError("invalid_request", f"{field} is required")
    if payload["session_id"] != payload["request_id"]:
        raise DiagnosisJobError("invalid_request", "session_id must equal request_id")
    if not isinstance(payload.get("alert"), dict):
        raise DiagnosisJobError("invalid_request", "alert must be an object")
    try:
        normalize_skill_bindings(payload.get("skills"))
    except ValueError as exc:
        raise DiagnosisJobError("invalid_request", str(exc)) from exc


def _validate_result(result: JSON) -> None:
    for field in ("session_id", "incident_id", "status"):
        if not isinstance(result.get(field), str) or not str(result[field]).strip():
            raise ValueError(f"Diagnosis result {field} is required")
    if not isinstance(result.get("diagnosis"), dict):
        raise ValueError("Diagnosis result diagnosis must be an object")


def _canonical(payload: JSON) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _failed_result(
    request_json: str,
    message: str,
    *,
    reason_code: str | None = None,
    partial_result: object = None,
    provider_revision: object = None,
) -> str:
    request_payload = json.loads(request_json)
    result: JSON = {
        "session_id": request_payload["session_id"],
        "incident_id": request_payload["incident_id"],
        "status": "failed",
        "diagnosis": {"summary": message, **({"reason_code": reason_code} if reason_code else {})},
        "steps": [],
        "tool_activity": [],
        "missing_evidence": [],
        "state_transitions": ["running", "failed"],
        "skill_versions": skill_versions(request_payload.get("skills")),
        **({"provider_revision": provider_revision} if provider_revision else {}),
    }
    if isinstance(partial_result, dict):
        for field in ("steps", "tool_activity", "missing_evidence"):
            value = partial_result.get(field)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                result[field] = value
    return _canonical(result)


def _job_projection(row: sqlite3.Row) -> JSON:
    checkpoint = json.loads(str(row["loop_checkpoint_json"])) if row["loop_checkpoint_json"] else None
    return {
        "request_id": str(row["request_id"]),
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "writeback_status": str(row["writeback_status"]),
        "writeback_attempt_count": int(row["writeback_attempt_count"]),
        "error": row["error"],
        "writeback_error": row["writeback_error"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "provider_revision": row["provider_revision"],
        "loop_checkpoint": checkpoint,
    }
