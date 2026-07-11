"""Channel-neutral Notification Request contract shared by Gateway and Engine."""

from __future__ import annotations

from datetime import UTC, datetime
import math
from typing import Any
from urllib.parse import urlsplit


EVENT_TYPES = (
    "incident.opened",
    "incident.severity_changed",
    "incident.reopened",
    "incident.resolved",
    "investigation.needs_input",
    "investigation.partial",
    "investigation.failed",
    "approval.required",
    "approval.approved",
    "approval.rejected",
    "approval.expired",
    "approval.blocked",
    "execution.succeeded",
    "execution.failed",
    "execution.rollback_required",
    "execution.outcome_unknown",
    "connector.offline",
    "connector.recovered",
)
SEVERITIES = frozenset({"info", "warning", "error", "critical"})
_FIELDS = frozenset(
    {"version", "event_id", "event_type", "occurred_at", "severity", "subject", "scope", "summary", "facts", "console_path"}
)
_SUBJECT_TYPES = {
    "incident": {"incident"},
    "investigation": {"investigation"},
    "approval": {"approval", "recommended_action"},
    "execution": {"execution", "connector_command"},
    "connector": {"connector"},
}
_SCOPE_FIELDS = frozenset(
    {"environment", "team_id", "service_id", "cluster_id", "namespace", "resource_type", "resource_id"}
)
_FACT_SCHEMAS = {
    "incident.opened": ({"incident_id", "status"}, {"incident_id", "status"}),
    "incident.severity_changed": (
        {"incident_id", "status", "previous_severity", "severity"},
        {"incident_id", "status", "previous_severity", "severity"},
    ),
    "incident.reopened": ({"incident_id", "status"}, {"incident_id", "status"}),
    "incident.resolved": ({"incident_id", "status"}, {"incident_id", "status"}),
    "investigation.needs_input": (
        {"incident_id", "investigation_id", "status", "reason"},
        {"incident_id", "investigation_id", "status", "reason"},
    ),
    "investigation.partial": (
        {"incident_id", "investigation_id", "status", "reason"},
        {"incident_id", "investigation_id", "status", "reason"},
    ),
    "investigation.failed": (
        {"incident_id", "investigation_id", "status", "reason"},
        {"incident_id", "investigation_id", "status", "reason"},
    ),
    "approval.required": (
        {"incident_id", "investigation_id", "action_id", "status"},
        {"incident_id", "investigation_id", "action_id", "status"},
    ),
    "approval.approved": (
        {"incident_id", "investigation_id", "action_id", "approval_id", "status"},
        {"incident_id", "investigation_id", "action_id", "approval_id", "status"},
    ),
    "approval.rejected": (
        {"incident_id", "investigation_id", "action_id", "approval_id", "status"},
        {"incident_id", "investigation_id", "action_id", "approval_id", "status", "reason"},
    ),
    "approval.expired": (
        {"incident_id", "investigation_id", "action_id", "approval_id", "status"},
        {"incident_id", "investigation_id", "action_id", "approval_id", "status", "reason"},
    ),
    "approval.blocked": (
        {"incident_id", "investigation_id", "action_id", "status", "reason"},
        {"incident_id", "investigation_id", "action_id", "approval_id", "status", "reason"},
    ),
    **{
        event_type: (
            {"incident_id", "command_id", "action", "status"},
            {"incident_id", "command_id", "action", "status", "error_code"},
        )
        for event_type in EVENT_TYPES
        if event_type.startswith("execution.")
    },
    "connector.offline": (
        {"connector_id", "cluster_id", "status"},
        {"connector_id", "cluster_id", "status", "reason"},
    ),
    "connector.recovered": (
        {"connector_id", "cluster_id", "status"},
        {"connector_id", "cluster_id", "status", "reason"},
    ),
}


class NotificationContractError(ValueError):
    pass


def notification_request(**payload: Any) -> dict[str, object]:
    """Validate and canonicalize one V1 Notification Request."""
    extras = set(payload) - _FIELDS
    if extras:
        raise NotificationContractError(f"unexpected Notification Request fields: {', '.join(sorted(extras))}")
    version = payload.get("version", 1)
    if version != 1:
        raise NotificationContractError("version must be 1")
    event_id = _text(payload.get("event_id"), "event_id", 300)
    event_type = _text(payload.get("event_type"), "event_type", 100)
    if event_type not in EVENT_TYPES:
        raise NotificationContractError("unsupported event_type")
    severity = _text(payload.get("severity"), "severity", 20)
    if severity not in SEVERITIES:
        raise NotificationContractError("severity must be info, warning, error, or critical")
    subject = _subject(payload.get("subject"), event_type)
    scope = _flat_object(payload.get("scope"), "scope", _SCOPE_FIELDS)
    required_facts, allowed_facts = _FACT_SCHEMAS[event_type]
    facts = _flat_object(payload.get("facts"), "facts", frozenset(allowed_facts))
    if not required_facts <= set(facts):
        raise NotificationContractError(
            "missing facts fields: " + ", ".join(sorted(required_facts - set(facts)))
        )
    if facts["status"] != event_type.rsplit(".", 1)[-1]:
        raise NotificationContractError("facts.status must match event_type")
    summary = _text(payload.get("summary"), "summary", 500)
    console_path = _console_path(payload.get("console_path"))
    return {
        "version": 1,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": _occurred_at(payload.get("occurred_at")),
        "severity": severity,
        "subject": subject,
        "scope": scope,
        "summary": summary,
        "facts": facts,
        "console_path": console_path,
    }


def _subject(value: Any, event_type: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"type", "id", "version"}:
        raise NotificationContractError("subject must contain only type, id, and version")
    subject_type = _text(value.get("type"), "subject.type", 50)
    if subject_type not in _SUBJECT_TYPES[event_type.split(".", 1)[0]]:
        raise NotificationContractError("subject.type does not match event_type")
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise NotificationContractError("subject.version must be a positive integer")
    return {"type": subject_type, "id": _text(value.get("id"), "subject.id", 200), "version": version}


def _flat_object(value: Any, field: str, allowed: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        raise NotificationContractError(f"{field} must be a non-empty object")
    extras = set(value) - allowed
    if extras:
        raise NotificationContractError(f"unexpected {field} fields: {', '.join(sorted(extras))}")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise NotificationContractError(f"{field}.{key} must be a scalar")
        if isinstance(item, (int, float)) and not math.isfinite(item):
            raise NotificationContractError(f"{field}.{key} must be finite")
        if isinstance(item, str):
            item = _text(item, f"{field}.{key}", 300)
        normalized[key] = item
    if not normalized:
        raise NotificationContractError(f"{field} must contain a value")
    return normalized


def _occurred_at(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise NotificationContractError("occurred_at must be finite")
        return datetime.fromtimestamp(float(value), UTC).isoformat().replace("+00:00", "Z")
    text = _text(value, "occurred_at", 40)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NotificationContractError("occurred_at must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise NotificationContractError("occurred_at must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _console_path(value: Any) -> str:
    path = _text(value, "console_path", 500)
    parsed = urlsplit(path)
    if not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.netloc:
        raise NotificationContractError("console_path must be an internal relative path")
    return path


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise NotificationContractError(f"{field} must be non-empty and at most {limit} characters")
    return value.strip()
