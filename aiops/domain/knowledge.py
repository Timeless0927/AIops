"""Knowledge candidate rules built from incident facts."""

from __future__ import annotations

from typing import Any


JSON = dict[str, Any]

VALID_KB_SOURCES = {"resolved_incident", "published_report", "correct_feedback", "successful_action_post_check"}


def requested_sources(value: Any) -> set[str]:
    if value is None:
        return set(VALID_KB_SOURCES)
    raw = value if isinstance(value, list) else [value]
    return {source for source in (str(item).strip() for item in raw) if source in VALID_KB_SOURCES}


def candidate_drafts(
    incident: JSON,
    evidence: list[JSON],
    feedback: list[JSON],
    versions: list[JSON],
    approvals: list[JSON],
    executions: list[JSON | None],
    sources: set[str],
    *,
    actor_id: str,
) -> list[JSON]:
    candidates: list[JSON] = []
    base = _base_candidate(incident, evidence, actor_id=actor_id)
    published = next((item for item in versions if item.get("status") == "published"), None)
    correct_feedback = [item for item in feedback if item.get("rating") == "correct"]
    successful_executions = [item for item in executions if _successful_post_check(item)]
    if "resolved_incident" in sources and incident.get("status") == "resolved":
        candidates.append({**base, "source_type": "resolved_incident", "metadata": {"incident_status": incident.get("status")}})
    if "published_report" in sources and published:
        candidates.append({**base, "source_type": "published_report", "report_version_id": published["version_id"], "metadata": {"version": published["version_number"]}})
    for item in correct_feedback if "correct_feedback" in sources else []:
        candidates.append({
            **base,
            "source_type": "correct_feedback",
            "known_root_cause": item.get("comment") or base["known_root_cause"],
            "metadata": {"feedback_id": item["feedback_id"], "target_type": item["target_type"]},
        })
    for item in successful_executions if "successful_action_post_check" in sources else []:
        approval = next((approval for approval in approvals if approval.get("approval_id") == item.get("approval_id")), {})
        candidates.append({
            **base,
            "source_type": "successful_action_post_check",
            "recommended_actions": [approval.get("action_summary") or item.get("action", {}).get("reason") or "unknown"],
            "metadata": {"execution_id": item["execution_id"], "approval_id": item["approval_id"]},
        })
    return candidates


def _base_candidate(incident: JSON, evidence: list[JSON], *, actor_id: str) -> JSON:
    root_cause = _first_evidence_value(evidence, "root_cause") or "unknown"
    return {
        "incident_id": incident["id"],
        "scope": {
            "service": incident.get("service") or "unknown",
            "team": incident.get("team") or incident.get("owner_team") or "unknown",
            "cluster": incident.get("cluster") or "unknown",
            "namespace": incident.get("namespace") or "unknown",
        },
        "symptoms": [incident.get("summary") or incident.get("alert_name") or "unknown"],
        "known_root_cause": root_cause,
        "recommended_checks": [item.get("summary") or item.get("source_ref") or "unknown" for item in evidence] or ["unknown"],
        "recommended_actions": ["unknown"],
        "evidence_refs": evidence_refs(evidence),
        "owner": incident.get("owner_team") or incident.get("team"),
        "created_by": actor_id,
    }


def evidence_refs(evidence: list[JSON]) -> list[JSON]:
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


def _successful_post_check(execution: JSON | None) -> bool:
    if not execution or execution.get("status") != "succeeded":
        return False
    post_check = execution.get("post_check_result")
    return isinstance(post_check, dict) and post_check.get("status") == "succeeded"
