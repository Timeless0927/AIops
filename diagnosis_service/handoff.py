"""Translate a Gateway Diagnosis Request into diagnosis incident context."""

from __future__ import annotations

from typing import Any


def incident_from_handoff(payload: dict[str, Any]) -> dict[str, Any]:
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    description = str(alert.get("description") or alert.get("summary") or "")
    human_inputs = []
    for item in payload.get("human_inputs", []):
        if not isinstance(item, dict) or not isinstance(item.get("event_id"), int) or not isinstance(item.get("payload"), dict):
            continue
        content = item["payload"].get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        target = item["payload"].get("target_event_id")
        reference = f" -> #{target}" if isinstance(target, int) else ""
        human_inputs.append(
            {
                "event_id": item["event_id"],
                "kind": str(item.get("kind") or "assertion"),
                "content": content.strip(),
                "reference": reference,
            }
        )
    if human_inputs:
        context = "\n".join(
            f"- [#{item['event_id']} {item['kind']}{item['reference']}] {item['content']}" for item in human_inputs
        )
        description = f"{description}\n\nUnverified Human Input (not Evidence):\n{context}"
    service = str(
        alert.get("service")
        or alert.get("workload_name")
        or alert.get("deployment")
        or alert.get("app")
        or ""
    )
    window = alert.get("observation_window") if isinstance(alert.get("observation_window"), dict) else {}
    start = str(window.get("start") or "").strip()
    end = str(window.get("end") or "").strip()
    incident = {
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
        "human_input_event_ids": [item["event_id"] for item in human_inputs],
    }
    if start and end:
        incident["start"] = start
        incident["end"] = end
        incident["time_range"] = {"type": "absolute", "value": f"{start}/{end}"}
    fingerprint = str(alert.get("fingerprint") or "").strip()
    if fingerprint:
        incident["fingerprint"] = fingerprint
    started_at = str(alert.get("started_at") or "").strip()
    if started_at:
        incident["started_at"] = started_at
    return incident
