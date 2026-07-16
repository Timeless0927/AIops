"""Pure acceptance decisions for the first Alert and Diagnosis gates."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


_ALERT_NAME = "AIOpsVerificationWorkloadUnavailable"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EVIDENCE_SOURCES = {"prometheus", "loki", "k8s"}


@dataclass(frozen=True)
class V03Decision:
    action: dict[str, object]
    signal: dict[str, object]
    model_revision: str


def valid_run_id(value: object) -> bool:
    return isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None


def is_verification_incident(incident: dict[str, object]) -> bool:
    return (
        incident.get("alertname") == _ALERT_NAME
        and incident.get("cluster_id") == "pilot-cluster"
        and incident.get("namespace") == "aiops-verification"
        and incident.get("workload_kind") == "Deployment"
        and incident.get("workload_name") == "verification-api"
    )


def signal_fingerprints(observed: dict[str, object]) -> set[str]:
    prometheus_items = _strict_dict_items(observed.get("prometheus_alerts"))
    alertmanager_items = _strict_dict_items(observed.get("alertmanager_alerts"))
    if prometheus_items is None or alertmanager_items is None:
        return set()
    prometheus_labels = {
        str(item.get("labels_sha256"))
        for item in prometheus_items
        if item.get("state") == "firing" and _sha256(item.get("labels_sha256"))
    }
    return {
        str(item["fingerprint"])
        for item in alertmanager_items
        if item.get("status") == "active"
        and str(item.get("labels_sha256")) in prometheus_labels
        and _REQUEST_ID.fullmatch(str(item.get("fingerprint") or ""))
    }


def v02_ready(
    observed: dict[str, object],
    *,
    run_id: str,
    trigger_started_at: float,
    telemetry_deadline_at: float,
) -> bool:
    observed_at = _timestamp(observed.get("observed_at"))
    try:
        counts_ready = (
            int(observed.get("fault_metric_series", 0)) >= 1
            and int(observed.get("deployment_unavailable_series", 0)) >= 1
            and int(observed.get("activation_log_lines", 0)) >= 1
        )
    except (TypeError, ValueError):
        return False
    return (
        observed.get("run_id") == run_id
        and observed_at is not None
        and trigger_started_at <= observed_at <= telemetry_deadline_at
        and counts_ready
        and bool(signal_fingerprints(observed))
    )


def v02_public_fact(
    workbench: dict[str, object],
    *,
    incident_id: str,
    fingerprints: set[str],
    trigger_started_at: float,
    public_deadline_at: float,
) -> dict[str, object] | None:
    incident = workbench.get("incident")
    investigation = workbench.get("investigation")
    if (
        not isinstance(incident, dict)
        or incident.get("id") != incident_id
        or not isinstance(investigation, dict)
        or not isinstance(investigation.get("id"), str)
        or not investigation["id"]
        or investigation.get("status") not in {
            "queued", "running", "completed", "paused", "human_led",
        }
        or not _within_deadline(
            incident.get("created_at"), trigger_started_at, public_deadline_at
        )
        or not _within_deadline(
            investigation.get("created_at"), trigger_started_at, public_deadline_at
        )
    ):
        return None
    signal_items = _strict_dict_items(workbench.get("alert_signals"))
    if signal_items is None:
        return None
    signals = [
        item
        for item in signal_items
        if item.get("fingerprint") in fingerprints
        and item.get("alertname") == _ALERT_NAME
        and item.get("status") == "firing"
        and item.get("workload_kind") == "Deployment"
        and item.get("workload_name") == "verification-api"
        and _REQUEST_ID.fullmatch(str(item.get("firing_webhook_request_id") or ""))
        and _within_deadline(
            item.get("started_at"), trigger_started_at, public_deadline_at
        )
        and _within_deadline(
            item.get("created_at"), trigger_started_at, public_deadline_at
        )
    ]
    if len(signals) != 1:
        return None
    return {
        "incident": incident,
        "investigation": investigation,
        "alert_signal": signals[0],
    }


def model_ready(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    revision = value.get("configuration_revision")
    verification = value.get("verification")
    availability = value.get("availability")
    return (
        value.get("readiness") == "ready"
        and isinstance(revision, str)
        and bool(revision)
        and isinstance(verification, dict)
        and verification.get("state") == "verified"
        and verification.get("revision") == revision
        and isinstance(availability, dict)
        and availability.get("state") == "available"
    )


def select_v03_action(
    workbench: dict[str, object],
    *,
    incident_id: str,
    investigation_id: str,
    alert_fingerprint: str,
    model: object,
    now: float,
) -> V03Decision | None:
    incident = workbench.get("incident")
    investigation = workbench.get("investigation")
    model_revision_value = (
        investigation.get("model_revision") if isinstance(investigation, dict) else None
    )
    model_revision = model_revision_value if isinstance(model_revision_value, str) else ""
    if (
        not isinstance(incident, dict)
        or incident.get("id") != incident_id
        or not isinstance(investigation, dict)
        or investigation.get("id") != investigation_id
        or investigation.get("status") != "completed"
        or not model_revision
        or not model_ready(model)
        or not isinstance(model, dict)
        or model.get("configuration_revision") != model_revision
    ):
        return None
    signal_items = _strict_dict_items(workbench.get("alert_signals"))
    if signal_items is None:
        return None
    signals = [
        item
        for item in signal_items
        if (
            item.get("fingerprint") == alert_fingerprint
            and item.get("alertname") == _ALERT_NAME
            and item.get("status") == "firing"
        )
    ]
    if len(signals) != 1:
        return None
    step_items = _strict_dict_items(workbench.get("evidence_steps"))
    action_items = _strict_dict_items(workbench.get("recommended_actions"))
    if step_items is None or action_items is None:
        return None
    step_ids = [item.get("id") for item in step_items]
    if any(not isinstance(step_id, str) or not step_id for step_id in step_ids):
        return None
    if len(set(step_ids)) != len(step_ids):
        return None
    steps = {str(item["id"]): item for item in step_items}
    judgment = workbench.get("judgment")
    if not isinstance(judgment, dict) or judgment.get("evidence_gate_status") != "complete":
        return None
    matches: list[dict[str, object]] = []
    for action in action_items:
        selected_ids = action.get("evidence_step_ids")
        if (
            not isinstance(selected_ids, list)
            or not selected_ids
            or any(not isinstance(step_id, str) or not step_id for step_id in selected_ids)
            or len(set(selected_ids)) != len(selected_ids)
        ):
            continue
        selected = [steps.get(step_id) for step_id in selected_ids]
        if any(item is None or not _fresh_evidence(item, now) for item in selected):
            continue
        sources = {str(item.get("source")) for item in selected if item is not None}
        if (
            action.get("change_intent") == "controlled_restart"
            and action.get("stale") is False
            and isinstance(action.get("gate"), dict)
            and action["gate"].get("status") == "complete"
            and _exact_verification_scope(action.get("target"))
            and sources == _EVIDENCE_SOURCES
            and isinstance(action.get("id"), str)
            and bool(action.get("id"))
            and _sha256(action.get("hash"))
            and isinstance(action.get("summary"), str)
            and bool(action.get("summary"))
        ):
            matches.append(action)
    if len(matches) != 1:
        return None
    return V03Decision(matches[0], signals[0], model_revision)


def _exact_verification_scope(value: object) -> bool:
    return isinstance(value, dict) and (
        value.get("cluster_id") == "pilot-cluster"
        and value.get("namespace") == "aiops-verification"
        and value.get("workload_kind") == "Deployment"
        and value.get("workload_name") == "verification-api"
    )


def _strict_dict_items(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return value


def _sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _timestamp(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _within_deadline(value: object, start: float, deadline: float) -> bool:
    timestamp = _timestamp(value)
    return timestamp is not None and start <= timestamp <= deadline


def _fresh_evidence(value: dict[str, object], now: float) -> bool:
    observed_at = _timestamp(value.get("observed_at"))
    expires_at = _timestamp(value.get("expires_at"))
    return (
        value.get("state") == "succeeded"
        and observed_at is not None
        and expires_at is not None
        and 0 <= now - observed_at <= 120
        and expires_at > now
        and _exact_verification_scope(value.get("scope"))
    )
