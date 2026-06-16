"""Gateway-side diagnosis artifact writeback."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from typing import Any

from aiops.contracts.writeback_auth import WRITEBACK_SECRET_ENV, WRITEBACK_SIGNATURE_HEADER, verify_writeback_signature
from toolsets import incident_store


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
    """Validate Hermes diagnosis writeback payload."""
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
    """Persist Hermes diagnosis artifacts into the Gateway incident store."""
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
            "Hermes diagnosis writeback",
            f"Hermes diagnosis session completed with status {payload['status']}",
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

    return HTTPStatus.OK, {
        "ok": True,
        "status": "persisted",
        "incident_id": incident_id,
        "session_id": session_id,
        "event_id": event_id,
        "timeline_refs": timeline_refs,
    }


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
