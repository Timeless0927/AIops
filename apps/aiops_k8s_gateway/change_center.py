"""Cross-Incident Change Request read model."""

from __future__ import annotations

from collections.abc import Callable

from .change_requests import ChangeRequestError, ChangeRequests, PhaseAccess


IncidentSnapshot = Callable[[str], dict[str, object] | None]


class ChangeCenterError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ChangeCenter:
    """Projects visible Change Requests without owning their workflow state."""

    def __init__(self, changes: ChangeRequests) -> None:
        self._changes = changes

    def list_for_actor(
        self,
        incidents: list[dict[str, object]],
        *,
        actor_id: str,
        can_manage: bool,
        phase_access: PhaseAccess,
    ) -> dict[str, object]:
        source_by_id = _source_incidents(incidents)
        projected = []
        for incident_id, source in source_by_id.items():
            for item in self._changes.list_for_incident(incident_id):
                view, visible, review = self._project(
                    item, actor_id=actor_id, phase_access=phase_access,
                )
                projected.append(_summary(
                    view, source,
                    attention=_attention(
                        view, actor_id=actor_id, can_manage=can_manage,
                        visible=visible, review=review,
                    ),
                ))
        projected.sort(key=lambda item: (
            item["attention"] is None, -float(item["updated_at"]), str(item["id"]),
        ))
        return {
            "pending_count": sum(item["attention"] is not None for item in projected),
            "change_requests": projected,
        }

    def detail_for_actor(
        self,
        change_request_id: str,
        incidents: list[dict[str, object]],
        *,
        actor_id: str,
        can_manage: bool,
        phase_access: PhaseAccess,
        incident_snapshot: IncidentSnapshot | None = None,
    ) -> dict[str, object]:
        try:
            item = self._changes.get(change_request_id)
        except ChangeRequestError as exc:
            if exc.code == "not_found":
                raise ChangeCenterError("not_found", "Change Request not found") from exc
            raise
        source = _source_incidents(incidents).get(str(item["incident_id"]))
        if source is None:
            raise ChangeCenterError("not_found", "Change Request not found")
        view, visible, review = self._project(
            item, actor_id=actor_id, phase_access=phase_access,
        )
        return {
            "incident": {key: source[key] for key in (
                "id", "title", "severity", "lifecycle_state",
            )},
            "environment": source["environment"],
            "attention": _attention(
                view, actor_id=actor_id, can_manage=can_manage,
                visible=visible, review=review,
            ),
            "can_manage": can_manage,
            "evidence_references": _evidence_references(
                incident_snapshot(str(source["id"])) if incident_snapshot else None,
            ),
            "change_request": view,
        }

    def _project(
        self,
        item: dict[str, object],
        *,
        actor_id: str,
        phase_access: PhaseAccess,
    ) -> tuple[dict[str, object], bool, dict[str, object] | None]:
        access = phase_access(str(item["id"]), actor_id, str(item["status"]))
        projected = self._changes.project_for_actor(
            item, actor_id=actor_id,
            phase_access=lambda _request_id, _actor_id, _status: access,
        )
        return projected, access[0], access[1]


def _source_incidents(
    incidents: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    result = {}
    for incident in incidents:
        incident_id = incident.get("id")
        if not isinstance(incident_id, str):
            continue
        result[incident_id] = {
            "id": incident_id,
            "title": str(incident.get("title", "")),
            "severity": str(incident.get("severity", "")),
            "lifecycle_state": str(incident.get("lifecycle_state", "")),
            "environment": str(incident.get("environment", "")),
        }
    return result


def _summary(
    change_request: dict[str, object],
    incident: dict[str, object],
    *,
    attention: str | None,
) -> dict[str, object]:
    return {
        "id": str(change_request["id"]),
        "incident": {key: incident[key] for key in (
            "id", "title", "severity", "lifecycle_state",
        )},
        "desired_outcome": str(change_request["desired_outcome"]),
        "status": str(change_request["status"]),
        "environment": str(incident["environment"]),
        "attention": attention,
        "updated_at": float(change_request["updated_at"]),
    }


def _attention(
    change_request: dict[str, object],
    *,
    actor_id: str,
    can_manage: bool,
    visible: bool,
    review: dict[str, object] | None,
) -> str | None:
    status = str(change_request["status"])
    if can_manage and status == "needs_input":
        return "input"
    if can_manage and status == "planning":
        return "retry"
    if not can_manage or not visible or review is None:
        return None
    if status == "awaiting_approval":
        return "approval"
    approval = review.get("approval")
    approver_id = approval.get("approver_id") if isinstance(approval, dict) else None
    if approver_id != actor_id:
        return None
    if status in {"approved", "executing"}:
        return "execution"
    if status == "effect_observed":
        return "reconciliation"
    return None


def _evidence_references(snapshot: dict[str, object] | None) -> list[str]:
    if not isinstance(snapshot, dict):
        return []
    references: list[str] = []
    for step in snapshot.get("evidence_steps", []):
        if not isinstance(step, dict):
            continue
        for reference in step.get("evidence_references", []):
            if isinstance(reference, str) and reference and reference not in references:
                references.append(reference)
    return references
