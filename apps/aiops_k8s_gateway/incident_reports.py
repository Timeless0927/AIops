"""Gateway-owned Incident Report drafts and immutable publications."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable

from .change_history import snapshot_change_history
from .gateway_db import GatewayDatabase, register_migrations


JSON = dict[str, object]
_TERMINAL_INVESTIGATIONS = {"completed", "failed", "terminated"}
_NARRATIVE_FIELDS = ("impact", "root_cause", "resolution_summary", "follow_up")
_SCHEMA_VERSION = 14
_SCHEMA = """
CREATE TABLE incident_report_drafts (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision > 0),
    source_resolved_at REAL NOT NULL,
    included_investigation_ids_json TEXT NOT NULL CHECK (json_valid(included_investigation_ids_json)),
    facts_json TEXT NOT NULL CHECK (json_valid(facts_json)),
    decision_action_history_json TEXT NOT NULL CHECK (json_valid(decision_action_history_json)),
    evidence_references_json TEXT NOT NULL CHECK (json_valid(evidence_references_json)),
    impact TEXT NOT NULL DEFAULT '',
    root_cause TEXT NOT NULL DEFAULT '',
    resolution_summary TEXT NOT NULL DEFAULT '',
    follow_up TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL,
    UNIQUE (incident_id, source_revision),
    FOREIGN KEY (incident_id) REFERENCES incidents(id),
    FOREIGN KEY (updated_by) REFERENCES users(id)
);
CREATE INDEX incident_report_drafts_by_incident
    ON incident_report_drafts(incident_id, source_revision DESC);

CREATE TABLE incident_report_publications (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    draft_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL CHECK (version > 0),
    report_json TEXT NOT NULL CHECK (json_valid(report_json)),
    published_by TEXT NOT NULL,
    published_at REAL NOT NULL,
    UNIQUE (incident_id, version),
    FOREIGN KEY (incident_id) REFERENCES incidents(id),
    FOREIGN KEY (draft_id) REFERENCES incident_report_drafts(id),
    FOREIGN KEY (published_by) REFERENCES users(id)
);
CREATE INDEX incident_report_publications_by_incident
    ON incident_report_publications(incident_id, version DESC);

CREATE TRIGGER incident_report_publications_no_update
BEFORE UPDATE ON incident_report_publications
BEGIN SELECT RAISE(ABORT, 'published Incident Reports are immutable'); END;

CREATE TRIGGER incident_report_publications_no_delete
BEFORE DELETE ON incident_report_publications
BEGIN SELECT RAISE(ABORT, 'published Incident Reports are immutable'); END;
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


class IncidentReportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class IncidentReports:
    def __init__(
        self,
        database: GatewayDatabase,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def list_for_actor(self, *, team_ids: set[str] | None) -> list[JSON]:
        with self._database.connect() as conn:
            summaries = []
            for incident in _visible_incidents(conn, team_ids):
                draft = conn.execute(
                    "SELECT * FROM incident_report_drafts "
                    "WHERE incident_id = ? AND source_revision = ?",
                    (incident["id"], incident["revision"]),
                ).fetchone()
                publications = conn.execute(
                    """
                    SELECT id, version, published_at,
                           json_extract(report_json, '$.source_revision') AS source_revision
                    FROM incident_report_publications
                    WHERE incident_id = ? ORDER BY version DESC
                    """,
                    (incident["id"],),
                ).fetchall()
                investigations = conn.execute(
                    "SELECT status FROM investigations WHERE incident_id = ?",
                    (incident["id"],),
                ).fetchall()
                eligible = _draft_eligible(
                    incident, investigations, _resolved_recovery(conn, incident),
                )
                if draft is None and not publications and not eligible:
                    continue
                summaries.append(_report_summary(incident, draft, publications, eligible=eligible))
        summaries.sort(key=lambda item: (
            {"draft": 0, "reopened": 1, "published": 2}[str(item["state"])],
            -float(item["relevant_at"]), str(item["incident"]["id"]),  # type: ignore[index]
        ))
        return summaries

    def get(self, incident_id: str, *, team_ids: set[str] | None, actor_id: str) -> JSON | None:
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = _visible_incident(conn, incident_id, team_ids)
            if incident is None:
                return None
            draft, reason = self._current_draft(conn, incident, actor_id)
            publications = [_publication(row) for row in conn.execute(
                "SELECT * FROM incident_report_publications WHERE incident_id = ? ORDER BY version DESC",
                (incident_id,),
            )]
            conn.commit()
        return {
            "availability": "ready" if draft is not None else "not_ready",
            "not_ready_reason": reason,
            "draft": _draft(draft) if draft is not None else None,
            "publications": publications,
        }

    def update(
        self,
        incident_id: str,
        narrative: JSON,
        *,
        team_ids: set[str] | None,
        actor_id: str,
    ) -> JSON:
        values = _narrative(narrative)
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = _visible_incident(conn, incident_id, team_ids)
            if incident is None:
                raise IncidentReportError("not_found", "Incident not found")
            draft, reason = self._current_draft(conn, incident, actor_id)
            if draft is None:
                raise IncidentReportError("report_not_ready", reason or "Incident Report is not ready")
            if draft["status"] != "draft":
                raise IncidentReportError("report_published", "Published Incident Report cannot be edited")
            conn.execute(
                """
                UPDATE incident_report_drafts
                SET impact = ?, root_cause = ?, resolution_summary = ?, follow_up = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
                """,
                (*[values[field] for field in _NARRATIVE_FIELDS], now, actor_id, draft["id"]),
            )
            updated = conn.execute("SELECT * FROM incident_report_drafts WHERE id = ?", (draft["id"],)).fetchone()
            conn.commit()
        return _draft(updated)

    def publish(self, incident_id: str, *, team_ids: set[str] | None, actor_id: str) -> JSON:
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = _visible_incident(conn, incident_id, team_ids)
            if incident is None:
                raise IncidentReportError("not_found", "Incident not found")
            draft, reason = self._current_draft(conn, incident, actor_id)
            if draft is None:
                raise IncidentReportError("report_not_ready", reason or "Incident Report is not ready")
            existing = conn.execute(
                "SELECT * FROM incident_report_publications WHERE draft_id = ?", (draft["id"],)
            ).fetchone()
            if existing is not None:
                conn.commit()
                return _publication(existing)
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM incident_report_publications WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()[0])
            report = _draft(draft)
            report["status"] = "published"
            report["version"] = version
            report["published_by"] = actor_id
            report["published_at"] = now
            publication_id = self._id_factory("report-version")
            conn.execute(
                """
                INSERT INTO incident_report_publications
                    (id, incident_id, draft_id, version, report_json, published_by, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (publication_id, incident_id, draft["id"], version, _json(report), actor_id, now),
            )
            conn.execute(
                "UPDATE incident_report_drafts SET status = 'published', updated_at = ?, updated_by = ? WHERE id = ?",
                (now, actor_id, draft["id"]),
            )
            publication = conn.execute(
                "SELECT * FROM incident_report_publications WHERE id = ?", (publication_id,)
            ).fetchone()
            conn.commit()
        return _publication(publication)

    def _current_draft(self, conn: object, incident: object, actor_id: str) -> tuple[object | None, str | None]:
        investigations = conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM investigations WHERE incident_id = ? ORDER BY sequence", (incident["id"],)  # type: ignore[index]
        ).fetchall()
        recovery = _resolved_recovery(conn, incident)
        if not _draft_eligible(incident, investigations, recovery):
            reason = (
                "Incident must be resolved"
                if incident["status"] != "resolved"  # type: ignore[index]
                else "All Investigations must be terminal"
                if not investigations or any(
                    row["status"] not in _TERMINAL_INVESTIGATIONS for row in investigations
                )
                else "Incident recovery must prove 300-second stabilization"
            )
            return None, reason
        draft = conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM incident_report_drafts WHERE incident_id = ? AND source_revision = ?",
            (incident["id"], incident["revision"]),  # type: ignore[index]
        ).fetchone()
        if draft is not None:
            return draft, None
        snapshot = _freeze(conn, incident, investigations)
        now = self._clock()
        draft_id = self._id_factory("report-draft")
        conn.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO incident_report_drafts (
                id, incident_id, source_revision, source_resolved_at, included_investigation_ids_json,
                facts_json, decision_action_history_json, evidence_references_json,
                created_at, updated_at, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id, incident["id"], incident["revision"], incident["resolved_at"],  # type: ignore[index]
                _json(snapshot["included_investigation_ids"]), _json(snapshot["facts"]),
                _json(snapshot["decision_action_history"]), _json(snapshot["evidence_references"]),
                now, now, actor_id,
            ),
        )
        return conn.execute("SELECT * FROM incident_report_drafts WHERE id = ?", (draft_id,)).fetchone(), None  # type: ignore[attr-defined]


def _freeze(conn: object, incident: object, investigations: list[object]) -> JSON:
    incident_id = str(incident["id"])  # type: ignore[index]
    investigation_ids = [str(row["id"]) for row in investigations]  # type: ignore[index]
    placeholders = ",".join("?" for _ in investigation_ids)
    steps = conn.execute(  # type: ignore[attr-defined]
        f"SELECT * FROM evidence_steps WHERE investigation_id IN ({placeholders}) ORDER BY investigation_id, sequence",
        investigation_ids,
    ).fetchall()
    judgments = conn.execute(  # type: ignore[attr-defined]
        f"SELECT * FROM investigation_judgments WHERE investigation_id IN ({placeholders}) ORDER BY investigation_id",
        investigation_ids,
    ).fetchall()
    actions = conn.execute(  # type: ignore[attr-defined]
        f"SELECT * FROM recommended_actions WHERE investigation_id IN ({placeholders}) ORDER BY investigation_id, version, id",
        investigation_ids,
    ).fetchall()
    facts = {
        "incident": _columns(incident, (
            "id", "title", "severity", "status", "lifecycle_state", "cluster_id", "namespace", "alertname",
            "binding_status", "deployment_target_id", "resource_binding_id", "binding_revision", "service_id",
            "team_id", "workload_kind", "workload_name", "created_at", "updated_at", "resolved_at",
        )),
        "alert_signals": [_columns(row, (
            "fingerprint", "alertname", "status", "severity", "summary", "workload_kind", "workload_name",
            "started_at", "firing_webhook_request_id", "recovered_webhook_request_id",
            "created_at", "updated_at",
        )) for row in conn.execute("SELECT * FROM alert_signals WHERE incident_id = ? ORDER BY created_at, id", (incident_id,))],  # type: ignore[attr-defined]
        "investigations": [_columns(row, ("id", "sequence", "status", "created_at", "updated_at")) for row in investigations],
        "recovery_observations": [_columns(row, (
            "id", "evidence_revision", "observed_at", "stabilizes_at", "cancelled_at", "resolved_at",
            "resolved_webhook_request_id",
        )) for row in conn.execute("SELECT * FROM recovery_observations WHERE incident_id = ? ORDER BY observed_at, id", (incident_id,))],  # type: ignore[attr-defined]
        "evidence_steps": [_columns(row, (
            "id", "investigation_id", "sequence", "purpose", "source", "scope_json", "state", "result",
            "impact", "observed_at", "expires_at",
        ), json_fields={"scope_json": "scope"}) for row in steps],
    }
    history: JSON = {
        "judgments": [_columns(row, (
            "investigation_id", "summary", "valid", "evidence_gate_status", "next_evidence_guidance_json",
        ), json_fields={"next_evidence_guidance_json": "next_evidence_guidance"}) for row in judgments],
        "recommended_actions": [_columns(row, (
            "id", "investigation_id", "version", "summary", "target_json",
            "evidence_step_ids_json", "safeguards_json", "gate_status", "gate_reasons_json",
            "action_hash", "stale", "created_at",
        ), json_fields={
            "target_json": "target", "evidence_step_ids_json": "evidence_step_ids",
            "safeguards_json": "safeguards", "gate_reasons_json": "gate_reasons",
        }) for row in actions],
        "change_requests": snapshot_change_history(conn, incident_id),
    }
    references = sorted({
        str(reference)
        for row in steps
        for reference in json.loads(str(row["evidence_references_json"]))  # type: ignore[index]
    })
    return {
        "included_investigation_ids": investigation_ids,
        "facts": facts,
        "decision_action_history": history,
        "evidence_references": references,
    }


def _visible_incident(conn: object, incident_id: str, team_ids: set[str] | None) -> object | None:
    rows = _visible_incidents(conn, team_ids, incident_id=incident_id)
    return rows[0] if rows else None


def _visible_incidents(
    conn: object, team_ids: set[str] | None, *, incident_id: str | None = None,
) -> list[object]:
    clauses = []
    params: list[object] = []
    if incident_id is not None:
        clauses.append("i.id = ?")
        params.append(incident_id)
    if team_ids is not None:
        if team_ids:
            placeholders = ",".join("?" for _ in team_ids)
            clauses.append(
                f"(COALESCE(rb.team_id, i.team_id) IS NULL OR "
                f"COALESCE(rb.team_id, i.team_id) IN ({placeholders}))"
            )
            params.extend(sorted(team_ids))
        else:
            clauses.append("COALESCE(rb.team_id, i.team_id) IS NULL")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return conn.execute(  # type: ignore[attr-defined]
        f"""
        SELECT i.*, COALESCE(rb.service_id, i.service_id) AS current_service_id,
               COALESCE(rb.team_id, i.team_id) AS current_team_id,
               s.name AS service_name
        FROM incidents i
        LEFT JOIN resource_bindings rb ON rb.id = i.resource_binding_id
        LEFT JOIN services s ON s.id = COALESCE(rb.service_id, i.service_id)
        {where}
        ORDER BY i.updated_at DESC, i.id
        """,
        params,
    ).fetchall()


def _draft_eligible(
    incident: object, investigations: list[object], recovery: object | None,
) -> bool:
    return incident["status"] == "resolved" and recovery is not None and bool(investigations) and all(  # type: ignore[index]
        row["status"] in _TERMINAL_INVESTIGATIONS for row in investigations
    )


def _resolved_recovery(conn: object, incident: object) -> object | None:
    resolved_at = incident["resolved_at"]  # type: ignore[index]
    if resolved_at is None:
        return None
    return conn.execute(  # type: ignore[attr-defined]
        """
        SELECT * FROM recovery_observations
        WHERE incident_id = ? AND cancelled_at IS NULL
          AND resolved_at = ? AND resolved_at = stabilizes_at
          AND stabilizes_at - observed_at >= 300
          AND resolved_webhook_request_id IS NOT NULL
        ORDER BY observed_at DESC, id DESC LIMIT 1
        """,
        (incident["id"], resolved_at),  # type: ignore[index]
    ).fetchone()


def _report_summary(
    incident: object,
    draft: object | None,
    publications: list[object],
    *,
    eligible: bool,
) -> JSON:
    latest = publications[0] if publications else None
    is_draft = eligible and (draft is None or draft["status"] == "draft")  # type: ignore[index]
    state = "draft" if is_draft else (
        "reopened" if incident["lifecycle_state"] == "reopened" else "published"  # type: ignore[index]
    )
    if is_draft:
        relevant_at = float(
            draft["updated_at"] if draft is not None else incident["resolved_at"]  # type: ignore[index]
        )
    elif state == "reopened":
        relevant_at = float(incident["updated_at"])  # type: ignore[index]
    else:
        relevant_at = float(latest["published_at"])  # type: ignore[index]
    service_id = incident["current_service_id"]  # type: ignore[index]
    return {
        "incident": {
            "id": str(incident["id"]), "title": str(incident["title"]),  # type: ignore[index]
            "severity": str(incident["severity"]),  # type: ignore[index]
            "lifecycle_state": str(incident["lifecycle_state"]),  # type: ignore[index]
        },
        "service": {
            "id": str(service_id), "name": str(incident["service_name"]),  # type: ignore[index]
        } if service_id is not None else None,
        "state": state,
        "draft": {
            "id": str(draft["id"]), "source_revision": int(draft["source_revision"]),  # type: ignore[index]
            "status": str(draft["status"]), "updated_at": float(draft["updated_at"]),  # type: ignore[index]
        } if is_draft and draft is not None else None,
        "latest_publication": {
            "id": str(latest["id"]), "version": int(latest["version"]),  # type: ignore[index]
            "source_revision": int(latest["source_revision"]),  # type: ignore[index]
            "published_at": float(latest["published_at"]),  # type: ignore[index]
        } if latest is not None else None,
        "publication_count": len(publications),
        "relevant_at": relevant_at,
    }


def _narrative(value: JSON) -> dict[str, str]:
    if set(value) != set(_NARRATIVE_FIELDS):
        raise IncidentReportError("invalid_report", "Only Incident Report narrative fields may be edited")
    result: dict[str, str] = {}
    for field in _NARRATIVE_FIELDS:
        text = value[field]
        if not isinstance(text, str) or len(text) > 20_000:
            raise IncidentReportError("invalid_report", f"{field} must be text up to 20000 characters")
        result[field] = text.strip()
    return result


def _draft(row: object) -> JSON:
    return {
        "id": str(row["id"]), "incident_id": str(row["incident_id"]),  # type: ignore[index]
        "source_revision": int(row["source_revision"]), "source_resolved_at": float(row["source_resolved_at"]),  # type: ignore[index]
        "included_investigation_ids": json.loads(str(row["included_investigation_ids_json"])),  # type: ignore[index]
        "facts": json.loads(str(row["facts_json"])),  # type: ignore[index]
        "decision_action_history": json.loads(str(row["decision_action_history_json"])),  # type: ignore[index]
        "evidence_references": json.loads(str(row["evidence_references_json"])),  # type: ignore[index]
        "narrative": {field: str(row[field]) for field in _NARRATIVE_FIELDS},  # type: ignore[index]
        "status": str(row["status"]), "created_at": float(row["created_at"]), "updated_at": float(row["updated_at"]),  # type: ignore[index]
    }


def _publication(row: object) -> JSON:
    report = json.loads(str(row["report_json"]))  # type: ignore[index]
    return {**report, "id": str(row["id"]), "draft_id": str(row["draft_id"])}  # type: ignore[index]


def _columns(row: object, names: tuple[str, ...], *, json_fields: dict[str, str] | None = None) -> JSON:
    json_fields = json_fields or {}
    result: JSON = {}
    for name in names:
        value = row[name]  # type: ignore[index]
        result[json_fields.get(name, name)] = json.loads(str(value)) if name in json_fields else value
    return result


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
