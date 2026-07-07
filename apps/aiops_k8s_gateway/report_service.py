"""Gateway-owned incident report versions and human feedback."""

from __future__ import annotations

import asyncio
import html
import json
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, TypeVar

from toolsets import incident_store

from . import approval_execution_service
from . import approval_service


JSON = dict[str, Any]
T = TypeVar("T")

_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15
_CHECKPOINT_EVERY_N_WRITES = 50

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS incident_reports (
    version_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    html TEXT NOT NULL,
    sections_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    unknowns_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    published_by TEXT,
    published_at REAL,
    source_version_id TEXT,
    UNIQUE(incident_id, version_number)
);

CREATE TABLE IF NOT EXISTS human_feedback (
    feedback_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    incident_id TEXT,
    run_id TEXT,
    rating TEXT NOT NULL,
    comment TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incident_reports_incident
ON incident_reports(incident_id, version_number DESC);

CREATE INDEX IF NOT EXISTS idx_human_feedback_run
ON human_feedback(run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_human_feedback_incident
ON human_feedback(incident_id, created_at DESC);
"""

VALID_TARGET_TYPES = {"diagnosis", "evidence", "action_proposal", "report"}
VALID_RATINGS = {"positive", "negative", "neutral", "unknown"}


class ReportServiceError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "reports_feedback.db"
    return _project_root() / "data" / "reports_feedback.db"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ReportDB:
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

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[JSON]:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("database connection is closed")
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode_report(dict(row)) for row in rows]

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> JSON | None:
        rows = self._fetchall(sql, params)
        return rows[0] if rows else None

    def list_versions(self, incident_id: str) -> list[JSON]:
        return self._fetchall(
            "SELECT * FROM incident_reports WHERE incident_id = ? ORDER BY version_number DESC",
            (incident_id,),
        )

    def latest_report(self, incident_id: str) -> JSON | None:
        return self._fetchone(
            "SELECT * FROM incident_reports WHERE incident_id = ? ORDER BY version_number DESC LIMIT 1",
            (incident_id,),
        )

    def create_draft(self, report: JSON) -> JSON:
        version_id = f"rep-{uuid.uuid4().hex}"
        now = time.time()

        def _write(conn: sqlite3.Connection) -> str:
            row = conn.execute(
                "SELECT version_number, version_id FROM incident_reports WHERE incident_id = ? ORDER BY version_number DESC LIMIT 1",
                (report["incident_id"],),
            ).fetchone()
            version_number = int(row["version_number"]) + 1 if row is not None else 1
            source_version_id = str(row["version_id"]) if row is not None else None
            conn.execute(
                """
                INSERT INTO incident_reports (
                    version_id, incident_id, version_number, status, html, sections_json,
                    evidence_refs_json, unknowns_json, created_by, created_at,
                    published_by, published_at, source_version_id
                ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    version_id,
                    report["incident_id"],
                    version_number,
                    _sanitize_html(report["html"]),
                    _json_dumps(report["sections"]),
                    _json_dumps(report["evidence_refs"]),
                    _json_dumps(report["unknowns"]),
                    report["created_by"],
                    now,
                    source_version_id,
                ),
            )
            return version_id

        row_id = self._execute_write(_write)
        row = self.get_report(row_id)
        if row is None:
            raise ReportServiceError("not_found", "report version not found after create", status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return row

    def get_report(self, version_id: str) -> JSON | None:
        return self._fetchone("SELECT * FROM incident_reports WHERE version_id = ?", (version_id,))

    def publish(self, incident_id: str, *, version_id: str | None, actor_id: str) -> JSON:
        now = time.time()

        def _write(conn: sqlite3.Connection) -> str:
            if version_id:
                row = conn.execute("SELECT * FROM incident_reports WHERE incident_id = ? AND version_id = ?", (incident_id, version_id)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM incident_reports WHERE incident_id = ? ORDER BY version_number DESC LIMIT 1",
                    (incident_id,),
                ).fetchone()
            if row is None:
                raise ReportServiceError("not_found", "report draft not found", status=HTTPStatus.NOT_FOUND)
            if row["status"] != "draft":
                raise ReportServiceError("immutable_report", "published report versions are immutable", status=HTTPStatus.CONFLICT)
            conn.execute(
                "UPDATE incident_reports SET status = 'published', published_by = ?, published_at = ? WHERE version_id = ?",
                (actor_id, now, row["version_id"]),
            )
            return str(row["version_id"])

        row_id = self._execute_write(_write)
        row = self.get_report(row_id)
        if row is None:
            raise ReportServiceError("not_found", "report version not found after publish", status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return row

    def add_feedback(self, payload: JSON, *, actor_id: str) -> JSON:
        normalized = _normalize_feedback(payload)
        feedback_id = f"fb-{uuid.uuid4().hex}"
        now = time.time()

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO human_feedback (
                    feedback_id, target_type, target_id, incident_id, run_id,
                    rating, comment, actor_id, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    normalized["target_type"],
                    normalized["target_id"],
                    normalized.get("incident_id"),
                    normalized.get("run_id"),
                    normalized["rating"],
                    normalized["comment"],
                    actor_id,
                    _json_dumps(normalized["metadata"]),
                    now,
                ),
            )

        self._execute_write(_write)
        return self.get_feedback(feedback_id) or {}

    def get_feedback(self, feedback_id: str) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("database connection is closed")
            row = self._conn.execute("SELECT * FROM human_feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
        return _decode_feedback(dict(row)) if row is not None else None

    def list_feedback(self, *, incident_id: str | None = None, run_id: str | None = None) -> list[JSON]:
        clauses: list[str] = []
        params: list[Any] = []
        if incident_id:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("database connection is closed")
            rows = self._conn.execute(f"SELECT * FROM human_feedback{where} ORDER BY created_at DESC", tuple(params)).fetchall()
        return [_decode_feedback(dict(row)) for row in rows]


async def report_snapshot(incident_id: str) -> JSON:
    _ = await incident_store.get_incident(incident_id)
    return {
        "latest_report": _DB.latest_report(incident_id),
        "versions": _DB.list_versions(incident_id),
        "feedback": _DB.list_feedback(incident_id=incident_id),
    }


async def create_draft(incident_id: str, payload: JSON, *, actor_id: str) -> JSON:
    incident = await incident_store.get_incident(incident_id)
    timeline = await incident_store.get_timeline(incident_id)
    evidence = await incident_store.list_evidence(incident_id)
    approvals = approval_service.list_requests(incident_id=incident_id, limit=100)
    executions = [
        approval_execution_service.get_execution(str(approval["approval_id"]))
        for approval in approvals
        if approval_execution_service.get_execution(str(approval["approval_id"])) is not None
    ]
    html_text = str(payload.get("html") or "").strip()
    sections = _sections(incident, timeline, evidence, approvals, executions)
    unknowns = _unknowns(incident, evidence, timeline)
    report = {
        "incident_id": incident_id,
        "html": html_text or _render_html(incident, sections, unknowns),
        "sections": sections,
        "evidence_refs": _evidence_refs(evidence),
        "unknowns": unknowns,
        "created_by": actor_id,
    }
    return await asyncio.to_thread(_DB.create_draft, report)


async def publish(incident_id: str, payload: JSON, *, actor_id: str) -> JSON:
    version_id = str(payload.get("version_id") or "").strip() or None
    return await asyncio.to_thread(_DB.publish, incident_id, version_id=version_id, actor_id=actor_id)


async def add_feedback(payload: JSON, *, actor_id: str) -> JSON:
    return await asyncio.to_thread(_DB.add_feedback, payload, actor_id=actor_id)


async def list_run_feedback(run_id: str) -> list[JSON]:
    return await asyncio.to_thread(_DB.list_feedback, run_id=run_id)


def _sections(
    incident: JSON,
    timeline: list[JSON],
    evidence: list[JSON],
    approvals: list[JSON],
    executions: list[JSON],
) -> JSON:
    return {
        "summary": incident.get("summary") or "unknown",
        "timeline": timeline,
        "impact": incident.get("impact") or "unknown",
        "root_cause": _first_evidence_value(evidence, "root_cause") or "unknown",
        "trigger": incident.get("alert_name") or "unknown",
        "remediation_process": [item for item in timeline if str(item.get("event_type", "")).startswith("remediate")],
        "agent_actions": approvals,
        "human_approvals": approvals,
        "evidence_references": _evidence_refs(evidence),
        "recovery_validation": executions,
        "follow_up_items": [],
        "responsibility_chain": [{"approval_id": item.get("approval_id"), "status": item.get("status")} for item in approvals],
    }


def _unknowns(incident: JSON, evidence: list[JSON], timeline: list[JSON]) -> list[str]:
    unknowns: list[str] = []
    if not evidence:
        unknowns.append("evidence")
    if not _first_evidence_value(evidence, "root_cause"):
        unknowns.append("root_cause")
    if not incident.get("impact"):
        unknowns.append("impact")
    if not timeline:
        unknowns.append("timeline")
    return unknowns


def _render_html(incident: JSON, sections: JSON, unknowns: list[str]) -> str:
    title = html.escape(str(incident.get("alert_name") or incident.get("id") or "Incident report"))
    parts = [
        "<article class=\"incident-report\">",
        f"<h1>{title}</h1>",
        f"<section><h2>Summary</h2><p>{html.escape(str(sections['summary']))}</p></section>",
        f"<section><h2>Impact</h2><p>{html.escape(str(sections['impact']))}</p></section>",
        f"<section><h2>Root cause</h2><p>{html.escape(str(sections['root_cause']))}</p></section>",
        f"<section><h2>Trigger</h2><p>{html.escape(str(sections['trigger']))}</p></section>",
        _list_section("Timeline", [f"{item.get('event_type')}: {item.get('output_summary')}" for item in sections["timeline"]]),
        _list_section("Evidence references", [str(item.get("ref_id") or item.get("id")) for item in sections["evidence_references"]]),
        _list_section("Unknowns", unknowns or ["none"]),
        "</article>",
    ]
    return "".join(parts)


def _list_section(title: str, items: list[str]) -> str:
    escaped = "".join(f"<li>{html.escape(item)}</li>" for item in (items or ["unknown"]))
    return f"<section><h2>{html.escape(title)}</h2><ul>{escaped}</ul></section>"


def _evidence_refs(evidence: list[JSON]) -> list[JSON]:
    return [
        {
            "id": item.get("id"),
            "ref_id": item.get("source_ref") or f"evidence-{item.get('id')}",
            "source": item.get("source_type"),
            "summary": item.get("summary"),
        }
        for item in evidence
    ]


def _first_evidence_value(evidence: list[JSON], key: str) -> str | None:
    for item in evidence:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _normalize_feedback(payload: JSON) -> JSON:
    target_type = str(payload.get("target_type") or "").strip()
    if target_type not in VALID_TARGET_TYPES:
        raise ReportServiceError("invalid_feedback_target", "unsupported feedback target_type", status=HTTPStatus.BAD_REQUEST)
    target_id = str(payload.get("target_id") or "").strip()
    if not target_id:
        raise ReportServiceError("invalid_request", "target_id is required", status=HTTPStatus.BAD_REQUEST)
    rating = str(payload.get("rating") or "unknown").strip()
    if rating not in VALID_RATINGS:
        raise ReportServiceError("invalid_rating", "unsupported feedback rating", status=HTTPStatus.BAD_REQUEST)
    return {
        "target_type": target_type,
        "target_id": target_id,
        "incident_id": _optional_text(payload.get("incident_id")),
        "run_id": _optional_text(payload.get("run_id")),
        "rating": rating,
        "comment": str(payload.get("comment") or "").strip(),
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }


def _sanitize_html(value: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", "", value, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\son[a-z]+\s*=\s*(['\"]).*?\1", "", without_scripts, flags=re.IGNORECASE)


def _decode_report(row: JSON) -> JSON:
    decoded = dict(row)
    decoded["sections"] = json.loads(decoded.pop("sections_json") or "{}")
    decoded["evidence_refs"] = json.loads(decoded.pop("evidence_refs_json") or "[]")
    decoded["unknowns"] = json.loads(decoded.pop("unknowns_json") or "[]")
    return decoded


def _decode_feedback(row: JSON) -> JSON:
    decoded = dict(row)
    decoded["metadata"] = json.loads(decoded.pop("metadata_json") or "{}")
    return decoded


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


_DB = ReportDB()
