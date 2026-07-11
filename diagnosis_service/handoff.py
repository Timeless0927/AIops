"""Translate a Gateway Diagnosis Request into diagnosis incident context."""

from __future__ import annotations

from typing import Any


def incident_from_handoff(payload: dict[str, Any]) -> dict[str, Any]:
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    description = str(alert.get("description") or alert.get("summary") or "")
    service = str(
        alert.get("service")
        or alert.get("workload_name")
        or alert.get("deployment")
        or alert.get("app")
        or ""
    )
    return {
        "incident_id": str(payload["incident_id"]),
        "session_id": str(payload["session_id"]),
        "source": str(payload.get("source") or "gateway"),
        "alert_name": str(alert.get("alertname") or payload.get("dedup_key") or "alertmanager alert"),
        "summary": description or str(alert.get("alertname") or "Alertmanager firing"),
        "namespace": str(alert.get("namespace") or "default"),
        "cluster": str(alert.get("cluster") or "default"),
        "service": service,
        "app": service,
        "pod_name": str(alert.get("pod_name") or alert.get("pod") or ""),
        "container_name": str(alert.get("container_name") or alert.get("container") or ""),
        "workload_kind": str(alert.get("workload_kind") or ""),
        "workload_name": str(alert.get("workload_name") or alert.get("deployment") or ""),
        "severity": str(alert.get("severity") or "info"),
        "dedup_key": payload.get("dedup_key"),
        "dedup_key_version": payload.get("dedup_key_version"),
    }
