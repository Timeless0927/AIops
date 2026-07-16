"""Pure acceptance decisions for the first Alert and Diagnosis gates."""

from __future__ import annotations

from collections.abc import Callable


def is_verification_incident(incident: dict[str, object]) -> bool:
    return (
        incident.get("alertname") == "AIOpsVerificationWorkloadUnavailable"
        and incident.get("cluster_id") == "pilot-cluster"
        and incident.get("namespace") == "aiops-verification"
        and incident.get("workload_name") == "verification-api"
    )


def signal_fingerprints(observed: dict[str, object]) -> set[str]:
    prometheus = {
        str(item.get("fingerprint"))
        for item in observed.get("prometheus_alerts", [])
        if isinstance(item, dict) and item.get("state") == "firing"
    }
    alertmanager = {
        str(item.get("fingerprint"))
        for item in observed.get("alertmanager_alerts", [])
        if isinstance(item, dict) and item.get("status") == "active"
    }
    return prometheus & alertmanager


def v02_ready(observed: dict[str, object]) -> bool:
    return (
        int(observed.get("fault_metric_series", 0)) >= 1
        and int(observed.get("deployment_unavailable_series", 0)) >= 1
        and int(observed.get("activation_log_lines", 0)) >= 1
        and bool(signal_fingerprints(observed))
    )


def model_ready(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    revision = value.get("configuration_revision")
    return (
        value.get("readiness") == "ready"
        and isinstance(revision, str)
        and bool(revision)
        and value.get("verification", {}).get("state") == "verified"
        and value.get("verification", {}).get("revision") == revision
        and value.get("availability", {}).get("state") == "available"
    )


def select_v03_action(
    workbench: dict[str, object],
    *,
    investigation_id: str,
    alert_fingerprint: str,
    now: Callable[[], float],
) -> dict[str, object] | None:
    investigation = workbench.get("investigation")
    if (
        not isinstance(investigation, dict)
        or investigation.get("id") != investigation_id
        or investigation.get("status") != "completed"
        or not any(
            item.get("fingerprint") == alert_fingerprint
            and item.get("status") == "firing"
            for item in workbench.get("alert_signals", [])
        )
    ):
        return None
    steps = {
        str(item.get("id")): item
        for item in workbench.get("evidence_steps", [])
        if isinstance(item, dict)
        and item.get("state") == "succeeded"
        and float(item.get("expires_at", 0)) > now()
        and 0 <= now() - float(item.get("observed_at", 0)) <= 120
        and _exact_verification_scope(item.get("scope"))
    }
    judgment = workbench.get("judgment")
    if not isinstance(judgment, dict) or judgment.get("evidence_gate_status") != "complete":
        return None
    for action in workbench.get("recommended_actions", []):
        if not isinstance(action, dict):
            continue
        selected = [steps.get(str(step_id)) for step_id in action.get("evidence_step_ids", [])]
        sources = {str(item.get("source")) for item in selected if isinstance(item, dict)}
        if (
            action.get("change_intent") == "controlled_restart"
            and action.get("stale") is False
            and action.get("gate", {}).get("status") == "complete"
            and _exact_verification_scope(action.get("target"))
            and {"prometheus", "loki", "k8s"} <= sources
        ):
            return action
    return None


def _exact_verification_scope(value: object) -> bool:
    return isinstance(value, dict) and (
        value.get("cluster_id") == "pilot-cluster"
        and value.get("namespace") == "aiops-verification"
        and value.get("workload_kind") == "Deployment"
        and value.get("workload_name") == "verification-api"
    )
