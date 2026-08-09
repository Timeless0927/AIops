"""C04 actor-scoped Change Center read model tests."""

from __future__ import annotations

from pathlib import Path

from aiops.domain.identity import SQLiteIdentityStore
from apps.aiops_k8s_gateway import main as _gateway_migrations  # noqa: F401
from apps.aiops_k8s_gateway.change_center import ChangeCenter, ChangeCenterError
from apps.aiops_k8s_gateway.change_requests import ChangeRequests
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase


def _system(
    tmp_path: Path,
) -> tuple[ChangeCenter, ChangeRequests, dict[str, object], dict[str, object]]:
    store = GatewayDatabase(tmp_path / "gateway.db")
    SQLiteIdentityStore(store.db_path).close()
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) "
            "VALUES ('incident-1', 'Checkout latency', 'critical', 'active', 1000, 1000)",
        )
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) "
            "VALUES ('incident-2', 'Worker backlog', 'high', 'active', 1000, 1000)",
        )
    ids = iter(("change-input", "phase-input", "revision-input", "change-plan", "phase-plan", "revision-plan"))
    changes = ChangeRequests(store, clock=lambda: 1_001.0, id_factory=lambda _prefix: next(ids))
    _, input_request = changes.submit(
        incident_id="incident-1", facts={}, actor_id="user-1",
        desired_outcome="Clarify the safe checkout target", context="",
        idempotency_key="input-1", request_id="req-input",
        planner=lambda _payload: {"status": "needs_input", "question": "Which revision?"},
    )
    _, plan_request = changes.submit(
        incident_id="incident-2", facts={}, actor_id="user-1",
        desired_outcome="Restore worker capacity", context="Queue depth is increasing",
        idempotency_key="plan-1", request_id="req-plan",
        planner=lambda _payload: {
            "status": "validating",
            "plan": {
                "summary": "Scale worker",
                "changes": [{
                    "target": {
                        "api_version": "apps/v1", "kind": "Deployment",
                        "namespace": "jobs", "name": "worker",
                    },
                    "operation": "patch",
                    "payload": [{"op": "replace", "path": "/spec/replicas", "value": 4}],
                    "post_checks": [{"type": "workload_rollout"}],
                }],
            },
        },
    )
    return ChangeCenter(changes), changes, input_request, plan_request


def _incidents() -> list[dict[str, object]]:
    return [
        {
            "id": "incident-1", "title": "Checkout latency", "severity": "critical",
            "lifecycle_state": "firing", "environment": "prod",
        },
        {
            "id": "incident-2", "title": "Worker backlog", "severity": "high",
            "lifecycle_state": "firing", "environment": "staging",
        },
    ]


def test_list_prioritizes_only_actor_actionable_visible_changes(tmp_path: Path) -> None:
    center, _changes, input_request, plan_request = _system(tmp_path)

    result = center.list_for_actor(
        _incidents(), actor_id="user-1", can_manage=True,
        phase_access=lambda request_id, _actor_id, _status: (
            request_id == input_request["id"], None,
        ),
    )

    assert result["pending_count"] == 1
    assert [item["id"] for item in result["change_requests"]] == [
        input_request["id"], plan_request["id"],
    ]
    assert result["change_requests"][0] == {
        "id": input_request["id"],
        "incident": {
            "id": "incident-1", "title": "Checkout latency", "severity": "critical",
            "lifecycle_state": "firing",
        },
        "desired_outcome": "Clarify the safe checkout target",
        "status": "needs_input", "environment": "prod", "attention": "input",
        "updated_at": 1001.0,
    }


def test_detail_keeps_source_but_redacts_plan_without_change_authority(tmp_path: Path) -> None:
    center, _changes, _input_request, plan_request = _system(tmp_path)

    detail = center.detail_for_actor(
        str(plan_request["id"]), _incidents(), actor_id="user-1", can_manage=True,
        phase_access=lambda _request_id, _actor_id, _status: (False, None),
        incident_snapshot=lambda _incident_id: {
            "evidence_steps": [
                {"evidence_references": ["prometheus://query/1", "loki://stream/2"]},
                {"evidence_references": ["prometheus://query/1", None]},
            ],
        },
    )

    assert detail["incident"] == {
        "id": "incident-2", "title": "Worker backlog", "severity": "high",
        "lifecycle_state": "firing",
    }
    assert detail["environment"] == "staging"
    assert detail["attention"] is None
    assert detail["can_manage"] is True
    assert detail["evidence_references"] == ["prometheus://query/1", "loki://stream/2"]
    assert detail["change_request"]["active_revision"]["plan"] is None  # type: ignore[index]


def test_detail_does_not_disclose_changes_outside_incident_scope(tmp_path: Path) -> None:
    center, _changes, _input_request, plan_request = _system(tmp_path)

    try:
        center.detail_for_actor(
            str(plan_request["id"]), _incidents()[:1], actor_id="user-1", can_manage=True,
            phase_access=lambda _request_id, _actor_id, _status: (True, None),
        )
    except ChangeCenterError as exc:
        assert exc.code == "not_found"
    else:
        raise AssertionError("out-of-scope Change Request was disclosed")


class _AttentionChanges:
    def list_for_incident(self, incident_id: str) -> list[dict[str, object]]:
        if incident_id != "incident-1":
            return []
        return [
            {
                "id": f"change-{status}", "incident_id": incident_id,
                "desired_outcome": status, "status": status, "updated_at": 1.0,
            }
            for status in (
                "awaiting_approval", "approved", "executing", "effect_observed",
                "unknown_outcome", "cancel_requested", "rolling_back",
            )
        ]

    @staticmethod
    def project_for_actor(
        item: dict[str, object], *, actor_id: str, phase_access,
    ) -> dict[str, object]:
        phase_access(str(item["id"]), actor_id, str(item["status"]))
        return dict(item)


def test_pending_count_excludes_paused_states_without_actor_command() -> None:
    center = ChangeCenter(_AttentionChanges())  # type: ignore[arg-type]
    result = center.list_for_actor(
        _incidents()[:1], actor_id="user-1", can_manage=True,
        phase_access=lambda _request_id, _actor_id, _status: (
            True, {"approval": {"approver_id": "user-1"}},
        ),
    )

    attention = {
        item["status"]: item["attention"] for item in result["change_requests"]
    }
    assert result["pending_count"] == 4
    assert attention == {
        "awaiting_approval": "approval",
        "approved": "execution",
        "executing": "execution",
        "effect_observed": "reconciliation",
        "unknown_outcome": None,
        "cancel_requested": None,
        "rolling_back": None,
    }


def test_terminal_detail_keeps_authorized_frozen_phase_review(tmp_path: Path) -> None:
    _center, changes, _input_request, plan_request = _system(tmp_path)
    review = {
        "status": "succeeded",
        "approval": {
            "approver_id": "user-1",
            "frozen_changes": [{"target_confirmation": "apps/v1:Deployment:jobs/worker"}],
        },
    }
    projected = changes.project_for_actor(
        {**plan_request, "status": "succeeded"}, actor_id="user-1",
        phase_access=lambda _request_id, _actor_id, _status: (True, review),
    )
    hidden = changes.project_for_actor(
        {**plan_request, "status": "succeeded"}, actor_id="user-2",
        phase_access=lambda _request_id, _actor_id, _status: (False, None),
    )

    assert projected["phase_review"] == review
    assert hidden["active_revision"]["plan"] is None  # type: ignore[index]
    assert "phase_review" not in hidden
