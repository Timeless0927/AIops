"""Alertmanager ingress for the split Gateway boundary."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from http import HTTPStatus
from typing import Any
from urllib import error, request

from toolsets import incident_store


JSON = dict[str, Any]


def _pick_first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_alert(alert: JSON) -> JSON:
    """Extract the stable alert fields shared with the legacy webhook path."""
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    target_fields = _extract_target_fields(labels, annotations)
    return {
        "alertname": str(labels.get("alertname", "")).strip(),
        "severity": str(labels.get("severity", "info")).strip().lower() or "info",
        "namespace": str(labels.get("namespace", "default")).strip() or "default",
        "cluster": str(labels.get("cluster", "default")).strip() or "default",
        "service": _extract_service(labels, annotations),
        "team": _extract_team(labels, annotations),
        "description": str(annotations.get("description") or annotations.get("summary") or "").strip(),
        "status": str(alert.get("status", "")).strip().lower(),
        **target_fields,
    }


def _extract_service(labels: JSON, annotations: JSON) -> str | None:
    return _pick_first_text(
        labels.get("service"),
        labels.get("service_name"),
        labels.get("app.kubernetes.io/name"),
        labels.get("app"),
        annotations.get("service"),
    )


def _extract_team(labels: JSON, annotations: JSON) -> str | None:
    return _pick_first_text(
        labels.get("team"),
        labels.get("owner_team"),
        labels.get("sre_team"),
        labels.get("owner"),
        annotations.get("team"),
    )


def _extract_target_fields(labels: JSON, annotations: JSON) -> JSON:
    pod_name = _pick_first_text(labels.get("pod"), labels.get("pod_name"), annotations.get("pod"))
    container_name = _pick_first_text(
        labels.get("container"),
        labels.get("container_name"),
        annotations.get("container"),
    )
    workload_pairs = (
        ("Deployment", _pick_first_text(labels.get("deployment"), labels.get("deployment_name"))),
        ("StatefulSet", _pick_first_text(labels.get("statefulset"), labels.get("statefulset_name"))),
        ("DaemonSet", _pick_first_text(labels.get("daemonset"), labels.get("daemonset_name"))),
        ("CronJob", _pick_first_text(labels.get("cronjob"), labels.get("cronjob_name"))),
        ("Job", _pick_first_text(labels.get("job_name"))),
    )
    for workload_kind, workload_name in workload_pairs:
        if workload_name:
            return {
                "pod_name": pod_name,
                "container_name": container_name,
                "workload_kind": workload_kind,
                "workload_name": workload_name,
            }
    return {
        "pod_name": pod_name,
        "container_name": container_name,
        "workload_kind": None,
        "workload_name": _pick_first_text(
            annotations.get("workload_name"),
            labels.get("app.kubernetes.io/name"),
            labels.get("app"),
        ),
    }


def build_dedup_key(alert: JSON) -> str:
    return "|".join([alert["alertname"], alert["namespace"], alert["cluster"]])


def dedup_key_version() -> str:
    return os.getenv("AIOPS_DEDUP_KEY_VERSION", "v1")


def resolve_hmac_secret() -> str | None:
    return os.getenv("ALERTMANAGER_WEBHOOK_SECRET") or os.getenv("AIOPS_ALERTMANAGER_WEBHOOK_SECRET")


def resolve_bearer_token() -> str | None:
    token = os.getenv("AIOPS_ALERTMANAGER_WEBHOOK_TOKEN", "").strip()
    return token or None


def verify_bearer_token(configured: str, authorization: str | None) -> bool:
    if not authorization:
        return False
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token.strip(), configured)


def verify_hmac_signature(body: bytes, secret: str, signature: str | None) -> bool:
    if not signature:
        return False
    received = signature.strip()
    if received.startswith("sha256="):
        received = received.split("=", 1)[1]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def validate_payload(payload: JSON) -> list[JSON]:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise ValueError("payload.alerts must be a list")
    return [alert for alert in alerts if isinstance(alert, dict)]


async def process_payload(payload: JSON, *, headers: dict[str, str] | None = None) -> JSON:
    """Persist alert ingress state and trigger Hermes without doing diagnosis."""
    del headers
    alerts = validate_payload(payload)
    processed = 0
    skipped = 0
    incidents: list[JSON] = []

    for raw_alert in alerts:
        alert = extract_alert(raw_alert)
        if not alert["alertname"]:
            skipped += 1
            continue

        dedup_key = build_dedup_key(alert)
        version = dedup_key_version()
        if alert["status"] == "resolved":
            resolved = await _handle_resolved_alert(alert, dedup_key, version)
            if resolved is None:
                skipped += 1
            else:
                processed += 1
                incidents.append(resolved)
            continue

        incident = await _create_or_reuse_incident(alert, dedup_key, version)
        incident_id = str(incident["incident_id"])
        session_id = f"diagnosis-{uuid.uuid4().hex}"
        await incident_store.add_event(
            incident_id,
            "alert_fired",
            "aiops_gateway",
            alert["alertname"],
            alert["description"] or "Alertmanager firing",
            {
                "alert": alert,
                "dedup_key": dedup_key,
                "dedup_key_version": version,
                "session_id": session_id,
                "ingress": "split_gateway",
            },
        )
        handoff = await trigger_hermes_diagnosis_session(
            incident_id=incident_id,
            session_id=session_id,
            alert=alert,
            dedup_key=dedup_key,
            dedup_key_version=version,
        )
        await _record_handoff_event(incident_id, session_id, alert, handoff)
        processed += 1
        incidents.append(
            {
                "incident_id": incident_id,
                "event_type": "alert_fired",
                "dedup_key": dedup_key,
                "dedup_key_version": version,
                "session_id": session_id,
                "reused": incident["reused"],
                "reopened": incident["reopened"],
                "hermes_handoff": handoff,
            }
        )

    return {"ok": True, "processed": processed, "skipped": skipped, "incidents": incidents}


async def _create_or_reuse_incident(alert: JSON, dedup_key: str, version: str) -> JSON:
    existing = await incident_store.find_reusable_incident(dedup_key, version)
    if existing is not None:
        incident_id = str(existing["id"])
        if str(existing.get("status") or "").strip().lower() == "resolved":
            await incident_store.reopen_incident(incident_id, "Alertmanager firing again")
            return {"incident_id": incident_id, "reused": True, "reopened": True}
        return {"incident_id": incident_id, "reused": True, "reopened": False}
    incident_id = await incident_store.create_incident(
        alert["alertname"],
        alert["namespace"],
        alert["cluster"],
        alert["description"],
        service=alert.get("service"),
        team=alert.get("team"),
        platform="gateway",
        dedup_key=dedup_key,
        dedup_key_version=version,
    )
    return {"incident_id": incident_id, "reused": False, "reopened": False}


async def _handle_resolved_alert(alert: JSON, dedup_key: str, version: str) -> JSON | None:
    existing = await incident_store.find_reusable_incident(dedup_key, version)
    if existing is None:
        return None
    incident_id = str(existing["id"])
    await incident_store.add_event(
        incident_id,
        "resolved",
        "aiops_gateway",
        alert["alertname"],
        alert["description"] or "Alertmanager resolved",
        {
            "alert": alert,
            "dedup_key": dedup_key,
            "dedup_key_version": version,
            "ingress": "split_gateway",
        },
    )
    if str(existing.get("status", "")).lower() != "resolved":
        await incident_store.update_status(incident_id, "resolved")
    return {
        "incident_id": incident_id,
        "event_type": "resolved",
        "dedup_key": dedup_key,
        "dedup_key_version": version,
    }


async def trigger_hermes_diagnosis_session(
    *,
    incident_id: str,
    session_id: str,
    alert: JSON,
    dedup_key: str,
    dedup_key_version: str,
) -> JSON:
    hermes_url = os.getenv("AIOPS_HERMES_URL", "").strip()
    if not hermes_url:
        return {"status": "skipped", "reason": "AIOPS_HERMES_URL is not set"}

    path = os.getenv("AIOPS_HERMES_DIAGNOSIS_PATH", "/diagnosis/sessions").strip() or "/diagnosis/sessions"
    target = f"{hermes_url.rstrip('/')}/{path.lstrip('/')}"
    payload = {
        "incident_id": incident_id,
        "session_id": session_id,
        "source": "alertmanager",
        "alert": alert,
        "dedup_key": dedup_key,
        "dedup_key_version": dedup_key_version,
    }
    timeout = _handoff_timeout()
    return await asyncio.to_thread(_post_json, target, payload, timeout)


def _handoff_timeout() -> float:
    try:
        return max(0.1, float(os.getenv("AIOPS_HERMES_HANDOFF_TIMEOUT_SECONDS", "2")))
    except ValueError:
        return 2.0


def _post_json(target: str, payload: JSON, timeout: float) -> JSON:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    req = request.Request(
        target,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
            data = json.loads(raw_body or "{}")
            if not isinstance(data, dict):
                data = {"raw": data}
            return {"status": "requested", "target": target, "response": data}
    except (OSError, TimeoutError, error.URLError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "failed", "target": target, "error": str(exc)}


async def _record_handoff_event(incident_id: str, session_id: str, alert: JSON, handoff: JSON) -> None:
    status = str(handoff.get("status") or "")
    if status == "requested":
        event_type = "hermes_handoff_requested"
        output_summary = f"Hermes diagnosis session requested: {session_id}"
    elif status == "skipped":
        event_type = "hermes_handoff_skipped"
        output_summary = str(handoff.get("reason") or "Hermes handoff skipped")
    else:
        event_type = "hermes_handoff_failed"
        output_summary = str(handoff.get("error") or "Hermes handoff failed")

    await incident_store.add_event(
        incident_id,
        event_type,
        "aiops_gateway",
        alert["alertname"],
        output_summary,
        {"session_id": session_id, "handoff": handoff},
    )


def handle_http_request(body: bytes, headers: dict[str, str]) -> tuple[HTTPStatus, JSON]:
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    bearer_token = resolve_bearer_token()
    if bearer_token and not verify_bearer_token(bearer_token, normalized_headers.get("authorization")):
        return HTTPStatus.UNAUTHORIZED, {"ok": False, "message": "alertmanager bearer token verification failed"}

    secret = resolve_hmac_secret()
    if secret and not bearer_token:
        signature = normalized_headers.get("x-signature") or normalized_headers.get("x-hub-signature-256")
        if not verify_hmac_signature(body, secret, signature):
            return HTTPStatus.UNAUTHORIZED, {"ok": False, "message": "signature verification failed"}

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "message": "invalid JSON payload"}
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, {"ok": False, "message": "request body must be a JSON object"}

    try:
        result = asyncio.run(process_payload(payload, headers=headers))
    except ValueError as exc:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(exc)}
    return HTTPStatus.OK, result
