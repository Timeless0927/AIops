"""Gateway-side diagnosis artifact writeback."""

from __future__ import annotations

import json
import os
from datetime import datetime
from http import HTTPStatus
from typing import Any

from aiops.contracts.writeback_auth import WRITEBACK_SECRET_ENV, WRITEBACK_SIGNATURE_HEADER, verify_writeback_signature
from toolsets import incident_store

from . import notification_center


JSON = dict[str, Any]


def authorize_writeback_request(
    *,
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
) -> tuple[HTTPStatus, JSON] | None:
    """Fail closed unless the request carries a valid Gateway writeback HMAC."""
    secret = os.getenv(WRITEBACK_SECRET_ENV, "").strip()
    if not secret:
        return HTTPStatus.UNAUTHORIZED, {
            "ok": False,
            "status": "unauthorized",
            "error": f"{WRITEBACK_SECRET_ENV} is required",
        }

    normalized_headers = {key.lower(): value for key, value in headers.items()}
    signature = normalized_headers.get(WRITEBACK_SIGNATURE_HEADER)
    if not verify_writeback_signature(secret, method=method, path=path, body=body, signature=signature):
        return HTTPStatus.UNAUTHORIZED, {
            "ok": False,
            "status": "unauthorized",
            "error": "invalid writeback signature",
        }
    return None


def validate_writeback_payload(payload: JSON) -> tuple[HTTPStatus, JSON] | None:
    """Validate diagnosis service writeback payload."""
    incident_id = str(payload.get("incident_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    diagnosis = payload.get("diagnosis")
    if not incident_id:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "status": "invalid", "error": "incident_id is required"}
    if not session_id:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "status": "invalid", "error": "session_id is required"}
    if not status:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "status": "invalid", "error": "status is required"}
    if not isinstance(diagnosis, dict):
        return HTTPStatus.BAD_REQUEST, {"ok": False, "status": "invalid", "error": "diagnosis must be an object"}
    if not isinstance(diagnosis.get("confidence"), dict):
        return HTTPStatus.BAD_REQUEST, {"ok": False, "status": "invalid", "error": "diagnosis.confidence must be an object"}
    if not isinstance(diagnosis.get("markdown"), str):
        return HTTPStatus.BAD_REQUEST, {"ok": False, "status": "invalid", "error": "diagnosis.markdown is required"}
    return None


async def apply_diagnosis_writeback(payload: JSON, *, store: Any = incident_store) -> tuple[HTTPStatus, JSON]:
    """Persist diagnosis artifacts into the Gateway incident store."""
    invalid = validate_writeback_payload(payload)
    if invalid is not None:
        return invalid

    incident_id = str(payload["incident_id"])
    session_id = str(payload["session_id"])
    diagnosis = payload["diagnosis"]
    timeline_refs = _timeline_refs(payload)
    try:
        await store.record_incident_diagnosis(incident_id, diagnosis)
        event_id = await store.add_event(
            incident_id,
            "investigate_end",
            "aiops_gateway",
            "Diagnosis service writeback",
            f"Diagnosis session completed with status {payload['status']}",
            {
                "session_id": session_id,
                "status": payload["status"],
                "diagnosis_summary": diagnosis.get("summary"),
                "writeback": {"source": "gateway_writeback_api", "status": "succeeded"},
                "timeline_refs": timeline_refs,
                "missing_evidence": payload.get("missing_evidence", []),
            },
        )
    except ValueError as exc:
        return HTTPStatus.NOT_FOUND, {"ok": False, "status": "not_found", "error": str(exc)}

    if str(payload.get("status") or "").strip() == "needs_human":
        await _notify_agent_waiting_for_human_input(store, incident_id, session_id, diagnosis)

    return HTTPStatus.OK, {
        "ok": True,
        "status": "persisted",
        "incident_id": incident_id,
        "session_id": session_id,
        "event_id": event_id,
        "timeline_refs": timeline_refs,
    }


async def _notify_agent_waiting_for_human_input(
    store: Any,
    incident_id: str,
    session_id: str,
    diagnosis: JSON,
) -> None:
    try:
        incident = await store.get_incident(incident_id)
        notification_center.send_notification(
            {
                "notification_type": "agent_waiting_for_human_input",
                "notification_id": f"agent_waiting_for_human_input-{incident_id}-{session_id}",
                "incident_id": incident_id,
                "run_id": session_id,
                "summary": diagnosis.get("summary") or "Agent waiting for human input",
                "dedupe_key": f"agent_waiting_for_human_input:{incident_id}:{session_id}",
                "context": {
                    "incident_id": incident_id,
                    "session_id": session_id,
                    "cluster": incident.get("cluster"),
                    "namespace": incident.get("namespace"),
                    "service": incident.get("service") or incident.get("service_id"),
                    "team": incident.get("team") or incident.get("owner_team"),
                    "status": "needs_human",
                },
            }
        )
    except Exception:
        pass


async def read_incident_view(incident_id: str, *, store: Any = incident_store) -> tuple[HTTPStatus, JSON]:
    """Return the Gateway incident row and timeline for HTTP incident views."""
    try:
        incident = await store.get_incident(incident_id)
        timeline = await store.get_timeline(incident_id)
    except ValueError as exc:
        return HTTPStatus.NOT_FOUND, {"ok": False, "status": "not_found", "error": str(exc)}

    return HTTPStatus.OK, {
        "ok": True,
        "status": "ok",
        "incident": _decode_diagnosis_json(dict(incident)),
        "timeline": timeline,
    }


async def read_diagnosis_process_view(incident_id: str, *, store: Any = incident_store) -> tuple[HTTPStatus, JSON]:
    """Return the Console-facing diagnosis process payload for one incident."""
    try:
        incident = _decode_diagnosis_json(dict(await store.get_incident(incident_id)))
        timeline = await store.get_timeline(incident_id)
        evidence = await store.list_evidence(incident_id)
    except ValueError as exc:
        return HTTPStatus.NOT_FOUND, {"ok": False, "status": "not_found", "error": str(exc)}

    diagnosis = _normalize_diagnosis(incident)
    session_id = str((diagnosis or {}).get("session_id") or _latest_session_id(timeline) or "").strip()
    if diagnosis is not None and session_id and not diagnosis.get("session_id"):
        diagnosis["session_id"] = session_id
    latest_status = _latest_diagnosis_status(timeline)
    if diagnosis is not None and latest_status:
        diagnosis["status"] = _console_status(latest_status)
    trace = await _list_trace(store, session_id)
    missing_evidence = _missing_evidence(timeline)
    return HTTPStatus.OK, {
        "ok": True,
        "status": "ok",
        "process": {
            "incident": _normalize_incident(incident, session_id),
            "diagnosis": diagnosis,
            "timeline": _normalize_timeline(timeline, trace),
            "evidence": _normalize_evidence(evidence, trace, missing_evidence),
            "missing_evidence": missing_evidence,
            "actions": _normalize_actions(incident.get("diagnosis")),
            "audit": _normalize_audit(timeline, evidence, trace, session_id),
        },
    }


def _timeline_refs(payload: JSON) -> JSON:
    refs = payload.get("timeline_refs") if isinstance(payload.get("timeline_refs"), dict) else {}
    return {
        "session_id": payload.get("session_id"),
        "evidence_refs": list(refs.get("evidence_refs") or []),
        "state_transitions": list(refs.get("state_transitions") or []),
    }


def _decode_diagnosis_json(incident: JSON) -> JSON:
    raw = incident.get("diagnosis_json")
    if isinstance(raw, str) and raw:
        try:
            incident["diagnosis"] = json.loads(raw)
        except json.JSONDecodeError:
            incident["diagnosis"] = None
    else:
        incident["diagnosis"] = None
    return incident


def _normalize_incident(incident: JSON, session_id: str | None) -> JSON:
    service_name = str(incident.get("service") or incident.get("service_id") or "").strip()
    owner_team = str(incident.get("team") or incident.get("owner_team") or "").strip()
    namespace = str(incident.get("namespace") or "").strip()
    cluster = str(incident.get("cluster") or "").strip()
    incident_id = str(incident.get("id") or "").strip()
    return {
        "incident_id": incident_id,
        "title": incident.get("alert_name") or incident.get("summary") or incident_id,
        "status": incident.get("status") or "unknown",
        "severity": incident.get("severity") or "unknown",
        "source": {
            "kind": incident.get("platform") or "gateway",
            "alert_id": incident.get("alert_name") or incident_id,
            "labels": {"service": service_name, "namespace": namespace, "cluster": cluster},
        },
        "service": {
            "service_id": incident.get("service_id") or service_name,
            "service_name": service_name or "-",
            "owner_team_id": incident.get("owner_team") or owner_team,
            "owner_team_name": owner_team or "-",
            "ownership_status": incident.get("ownership_status") or "unknown",
        },
        "latest_session_id": session_id or None,
        "created_at": _epoch_to_iso(incident.get("created_at")),
        "updated_at": _epoch_to_iso(incident.get("diagnosed_at") or incident.get("created_at")),
        "permissions": {
            "can_view": True,
            "can_view_raw_evidence": False,
            "can_view_cost": False,
            "can_approve": False,
            "blocked_reason": "gateway_read_only",
        },
    }


def _normalize_diagnosis(incident: JSON) -> JSON | None:
    diagnosis = incident.get("diagnosis")
    if not isinstance(diagnosis, dict):
        return None
    candidate = _first_candidate(diagnosis)
    confidence = diagnosis.get("confidence") if isinstance(diagnosis.get("confidence"), dict) else {}
    score = candidate.get("confidence") if isinstance(candidate, dict) and candidate.get("confidence") is not None else confidence.get("score")
    session_id = str(diagnosis.get("session_id") or "").strip() or _session_from_markdown(diagnosis)
    return {
        "session_id": session_id or None,
        "status": _diagnosis_status(incident, diagnosis),
        "summary": diagnosis.get("summary") or incident.get("diagnosis_summary") or "No summary available.",
        "root_cause": {
            "category": candidate.get("category") or "unknown",
            "statement": candidate.get("cause") or candidate.get("statement") or "No root cause conclusion is available.",
            "confidence": _float_or_none(score),
        },
        "diagnosed_at": _epoch_to_iso(incident.get("diagnosed_at")),
        "missing_evidence": _missing_sources_from_diagnosis(diagnosis),
        "markdown": diagnosis.get("markdown") or incident.get("diagnosis_markdown") or "",
        "redactions": {"chain_of_thought_hidden": True, "raw_evidence_restricted": True},
    }


def _first_candidate(diagnosis: JSON) -> JSON:
    root = diagnosis.get("root_cause") if isinstance(diagnosis.get("root_cause"), dict) else None
    if root:
        return root
    candidates = diagnosis.get("root_cause_candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates[0]
    return {}


def _diagnosis_status(incident: JSON, diagnosis: JSON) -> str:
    status = str(diagnosis.get("status") or "").strip()
    if status:
        return status
    markdown = str(diagnosis.get("markdown") or incident.get("diagnosis_markdown") or "").lower()
    if "needs_human" in markdown or "needs human" in markdown:
        return "partial"
    return "succeeded"


def _latest_diagnosis_status(timeline: list[JSON]) -> str | None:
    for event in reversed(timeline):
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        status = metadata.get("status") or _nested(metadata, "writeback", "status")
        if status:
            return str(status)
    return None


def _console_status(status: str) -> str:
    return {"diagnosed": "succeeded", "needs_human": "partial"}.get(status, status)


def _missing_sources_from_diagnosis(diagnosis: JSON) -> list[str]:
    values = diagnosis.get("missing_evidence")
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            result.append(str(item.get("source_type") or item.get("tool") or item.get("reason") or "unknown"))
    return result


def _normalize_timeline(timeline: list[JSON], trace: list[JSON]) -> list[JSON]:
    items: list[JSON] = []
    for event in timeline:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        items.append(
            {
                "event_id": str(event.get("id") or ""),
                "occurred_at": _epoch_to_iso(event.get("timestamp")),
                "type": event.get("event_type") or "event",
                "status": _event_status(event, metadata),
                "title": _event_title(event),
                "summary": event.get("output_summary") or event.get("input_summary") or "",
                "refs": _event_refs(event, metadata),
            }
        )
    for row in trace:
        items.append(
            {
                "event_id": f"trace-{row.get('step_index')}",
                "occurred_at": _epoch_to_iso(row.get("trace_collected_at")),
                "type": "tool",
                "status": "succeeded" if row.get("observation_ref") else "partial",
                "title": f"Tool call: {row.get('tool_name') or 'unknown'}",
                "summary": _trace_summary(row),
                "refs": {
                    "session_id": row.get("session_id"),
                    "evidence_id": row.get("observation_ref"),
                    "duration_ms": row.get("duration_ms"),
                },
            }
        )
    return sorted(items, key=lambda item: str(item.get("occurred_at") or ""))


def _event_status(event: JSON, metadata: JSON) -> str:
    status = str(metadata.get("status") or "").strip()
    if status:
        return status
    writeback = metadata.get("writeback") if isinstance(metadata.get("writeback"), dict) else {}
    return str(writeback.get("status") or "succeeded")


def _event_title(event: JSON) -> str:
    event_type = str(event.get("event_type") or "event")
    return event_type.replace("_", " ").title()


def _event_refs(event: JSON, metadata: JSON) -> JSON:
    refs = {
        "incident_id": event.get("incident_id"),
        "event_id": event.get("id"),
    }
    session_id = metadata.get("session_id") or _nested(metadata, "timeline_refs", "session_id")
    if session_id:
        refs["session_id"] = session_id
    evidence_refs = _nested(metadata, "timeline_refs", "evidence_refs")
    if evidence_refs:
        refs["evidence_id"] = evidence_refs
    return refs


def _normalize_evidence(evidence: list[JSON], trace: list[JSON], missing: list[JSON]) -> list[JSON]:
    items: list[JSON] = []
    for row in evidence:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        items.append(
            {
                "evidence_id": row.get("source_ref") or f"evidence-{row.get('id')}",
                "kind": _evidence_kind(row.get("source_type")),
                "status": "partial" if _float_or_none(row.get("confidence")) is not None and float(row["confidence"]) < 0.6 else "succeeded",
                "summary": row.get("summary") or "No summary available.",
                "collected_at": _epoch_to_iso(row.get("collected_at")),
                "query": {"display": _query_display(payload), "time_range": _time_range(row)},
                "result_ref": row.get("source_ref"),
                "raw_available": False,
                "failure": None,
            }
        )
    seen = {_evidence_kind(item.get("kind")) for item in items}
    for gap in missing:
        kind = _evidence_kind(gap.get("source_type") or gap.get("tool"))
        if kind in seen:
            continue
        items.append(
            {
                "evidence_id": f"missing-{kind}",
                "kind": kind,
                "status": "failed" if _nested(gap, "audit", "error_code") else "empty",
                "summary": str(gap.get("reason") or "Evidence was not collected."),
                "collected_at": None,
                "query": {"display": str(gap.get("tool") or kind), "time_range": {}},
                "result_ref": None,
                "raw_available": False,
                "failure": {
                    "code": str(_nested(gap, "audit", "error_code") or "missing_evidence"),
                    "message": str(gap.get("reason") or "Evidence missing."),
                    "retryable": True,
                },
            }
        )
    return items


def _evidence_kind(source_type: Any) -> str:
    value = str(source_type or "").strip()
    return {
        "metrics": "prometheus",
        "logs": "loki",
        "k8s_read": "k8s",
        "query_metrics": "prometheus",
        "query_logs": "loki",
        "get_service_topology": "topology",
        "run_k8s_read": "k8s",
    }.get(value, value or "evidence")


def _query_display(payload: JSON) -> str:
    for key in ("query", "command", "selector"):
        if payload.get(key):
            return str(payload[key])
    return "Gateway evidence artifact"


def _time_range(row: JSON) -> JSON:
    return {"from": _epoch_to_iso(row.get("window_start_ts")), "to": _epoch_to_iso(row.get("window_end_ts"))}


def _normalize_actions(diagnosis: Any) -> list[JSON]:
    if not isinstance(diagnosis, dict):
        return []
    actions = diagnosis.get("recommended_actions")
    if not isinstance(actions, list):
        return []
    result: list[JSON] = []
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            continue
        result.append(
            {
                "action_proposal_id": action.get("action_proposal_id") or f"diag-action-{index}",
                "summary": action.get("summary") or action.get("description") or "Action proposal",
                "risk_level": action.get("risk_level") or ("high" if action.get("approval_required") else "low"),
                "approval_required": bool(action.get("approval_required")),
                "approval_id": action.get("approval_id"),
                "execution_enabled": False,
            }
        )
    return result


def _normalize_audit(timeline: list[JSON], evidence: list[JSON], trace: list[JSON], session_id: str | None) -> JSON:
    refs = [str(event.get("id")) for event in timeline if event.get("id")]
    if session_id:
        refs.append(session_id)
    return {
        "status": "available" if timeline else "empty",
        "summary": f"{len(timeline)} timeline events, {len(evidence)} evidence artifacts, {len(trace)} tool trace rows, writeback durable state via Gateway.",
        "refs": refs,
    }


def _missing_evidence(timeline: list[JSON]) -> list[JSON]:
    result: list[JSON] = []
    for event in timeline:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        values = metadata.get("missing_evidence")
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict):
                result.append(item)
            else:
                result.append({"source_type": str(item), "reason": "missing_evidence"})
    return result


async def _list_trace(store: Any, session_id: str) -> list[JSON]:
    if not session_id:
        return []
    reader = getattr(store, "list_diagnosis_trace", None)
    if reader is None:
        return []
    return await reader(session_id)


def _latest_session_id(timeline: list[JSON]) -> str | None:
    for event in reversed(timeline):
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        session_id = metadata.get("session_id") or _nested(metadata, "timeline_refs", "session_id")
        if session_id:
            return str(session_id)
    return None


def _session_from_markdown(diagnosis: JSON) -> str | None:
    return str(diagnosis.get("session_id") or "").strip() or None


def _trace_summary(row: JSON) -> str:
    args = row.get("tool_args") if isinstance(row.get("tool_args"), dict) else {}
    pieces = [f"input={_compact_json(args)}"]
    if row.get("observation_ref"):
        pieces.append(f"observation_ref={row.get('observation_ref')}")
    if row.get("model"):
        pieces.append(f"model={row.get('model')}")
    return "; ".join(pieces)


def _compact_json(value: Any) -> str:
    text = json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 180 else f"{text[:177]}..."


def _nested(value: JSON, *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_to_iso(value: Any) -> str | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value) if value else None
    return f"{datetime.utcfromtimestamp(numeric).isoformat()}Z"
