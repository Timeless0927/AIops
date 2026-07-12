"""Exact Notification Request scope matching shared by Routes and Silences."""

from __future__ import annotations

from aiops.contracts.notification import EVENT_TYPES, SEVERITIES


JSON = dict[str, object]
MATCH_FIELDS = {"event", "severity", "environment", "team", "service"}


def validate_match(value: object, *, owner: str) -> JSON:
    if not isinstance(value, dict) or set(value) - MATCH_FIELDS:
        raise ValueError(f"{owner} match supports only event, severity, environment, team, and service")
    result: JSON = {}
    for key, raw in value.items():
        values = raw if isinstance(raw, list) else [raw]
        if not values or not all(isinstance(item, str) and item.strip() for item in values):
            raise ValueError(f"{owner} match values must be non-empty strings")
        normalized = list(dict.fromkeys(item.strip() for item in values))
        if key == "event" and any(item not in EVENT_TYPES for item in normalized):
            raise ValueError(f"{owner} event is unsupported")
        if key == "severity" and any(item not in SEVERITIES for item in normalized):
            raise ValueError(f"{owner} severity is unsupported")
        result[key] = normalized
    return result


def matches(match: object, request: JSON) -> bool:
    assert isinstance(match, dict)
    scope = request["scope"]
    assert isinstance(scope, dict)
    actual = {
        "event": request["event_type"],
        "severity": request["severity"],
        "environment": scope.get("environment"),
        "team": scope.get("team_id"),
        "service": scope.get("service_id"),
    }
    return all(actual[key] in values for key, values in match.items())
