"""Authenticated Alertmanager adapter for Gateway-owned Alert Signals."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from http import HTTPStatus
from typing import Any

from .incident import AlertSignal, IncidentError, IncidentService


JSON = dict[str, Any]
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _pick_first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_alert(alert: JSON) -> AlertSignal:
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    workload_kind, workload_name = _extract_workload(labels, annotations)
    status = str(alert.get("status") or "").strip().lower()
    return AlertSignal(
        fingerprint=str(alert.get("fingerprint") or "").strip(),
        alertname=str(labels.get("alertname") or "").strip(),
        status="recovered" if status == "resolved" else status,
        severity=_severity(str(labels.get("severity") or "info")),
        cluster_id=str(labels.get("cluster") or "").strip(),
        namespace=str(labels.get("namespace") or "").strip(),
        summary=str(annotations.get("description") or annotations.get("summary") or "").strip(),
        workload_kind=workload_kind,
        workload_name=workload_name,
        service_hint=_pick_first_text(
            labels.get("service"),
            labels.get("service_name"),
            labels.get("app.kubernetes.io/name"),
            labels.get("app"),
            annotations.get("service"),
        ),
        started_at=_pick_first_text(alert.get("startsAt")),
    )


def _extract_workload(labels: JSON, annotations: JSON) -> tuple[str | None, str | None]:
    pairs = (
        ("Deployment", _pick_first_text(labels.get("deployment"), labels.get("deployment_name"))),
        ("StatefulSet", _pick_first_text(labels.get("statefulset"), labels.get("statefulset_name"))),
        ("DaemonSet", _pick_first_text(labels.get("daemonset"), labels.get("daemonset_name"))),
        ("CronJob", _pick_first_text(labels.get("cronjob"), labels.get("cronjob_name"))),
        ("Job", _pick_first_text(labels.get("job_name"))),
    )
    for kind, name in pairs:
        if name:
            return kind, name
    name = _pick_first_text(
        annotations.get("workload_name"),
        labels.get("app.kubernetes.io/name"),
        labels.get("app"),
    )
    return (None, name)


def _severity(value: str) -> str:
    return {
        "critical": "critical",
        "error": "high",
        "high": "high",
        "warning": "medium",
        "medium": "medium",
    }.get(value.strip().lower(), "low")


def resolve_hmac_secret() -> str | None:
    return os.getenv("ALERTMANAGER_WEBHOOK_SECRET") or os.getenv("AIOPS_ALERTMANAGER_WEBHOOK_SECRET")


def resolve_bearer_token() -> str | None:
    token = os.getenv("AIOPS_ALERTMANAGER_WEBHOOK_TOKEN", "").strip()
    return token or None


def verify_bearer_token(configured: str, authorization: str | None) -> bool:
    if not authorization:
        return False
    scheme, _, token = authorization.strip().partition(" ")
    return scheme.lower() == "bearer" and bool(token) and hmac.compare_digest(token.strip(), configured)


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
    if not isinstance(alerts, list) or any(not isinstance(alert, dict) for alert in alerts):
        raise ValueError("payload.alerts must be a list of objects")
    return alerts


def _request_identity(value: str | None) -> str:
    candidate = value.strip() if isinstance(value, str) else ""
    if not candidate:
        return f"alertmanager-{uuid.uuid4().hex}"
    if not _REQUEST_ID.fullmatch(candidate):
        raise ValueError("X-Request-ID must be a bounded identifier")
    return candidate


def process_payload(payload: JSON, incidents: IncidentService, *, request_id: str) -> JSON:
    results: list[JSON] = []
    skipped = 0
    for raw_alert in validate_payload(payload):
        result = incidents.ingest(
            extract_alert(raw_alert), webhook_request_id=request_id
        )
        if not result.get("accepted"):
            skipped += 1
            continue
        incident = result["incident"]
        results.append(
            {
                "incident_id": incident["id"],
                "created": result["created"],
                "binding_status": incident["binding_status"],
            }
        )
    return {
        "ok": True,
        "request_id": request_id,
        "processed": len(results),
        "skipped": skipped,
        "incidents": results,
    }


def handle_http_request(
    body: bytes,
    headers: dict[str, str],
    incidents: IncidentService,
) -> tuple[HTTPStatus, JSON]:
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
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        request_id = _request_identity(normalized_headers.get("x-request-id"))
        return HTTPStatus.OK, process_payload(payload, incidents, request_id=request_id)
    except json.JSONDecodeError:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "message": "invalid JSON payload"}
    except IncidentError as exc:
        return HTTPStatus.UNPROCESSABLE_ENTITY, {
            "ok": False,
            "error": {"code": exc.code, "message": exc.message},
        }
    except ValueError as exc:
        return HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(exc)}
