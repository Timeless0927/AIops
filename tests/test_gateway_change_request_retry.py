"""Change Request retry state transition tests."""

from pathlib import Path

import pytest

from aiops.domain.identity import SQLiteIdentityStore
from apps.aiops_k8s_gateway import main as _gateway_migrations  # noqa: F401
from apps.aiops_k8s_gateway.change_requests import ChangeRequestError, ChangeRequests
from apps.aiops_k8s_gateway.notification_requests import (
    NotificationOutbox,
    change_notification_request,
    persist_notification_request_in,
)
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _plan() -> dict[str, object]:
    return {
        "status": "validating",
        "plan": {
            "summary": "scale",
            "changes": [{
                "target": {
                    "api_version": "apps/v1", "kind": "Deployment",
                    "namespace": "payments", "name": "checkout-api",
                },
                "operation": "patch",
                "payload": [{"op": "replace", "path": "/spec/replicas", "value": 5}],
                "post_checks": [{"type": "workload_rollout"}],
            }],
        },
    }


def test_awaiting_approval_notification_identity_includes_revision(tmp_path: Path) -> None:
    store = GatewayV1Store(tmp_path / "gateway.db")
    SQLiteIdentityStore(store.db_path).close()
    with store.database.connect() as conn:
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) "
            "VALUES ('incident-1', 'Checkout', 'critical', 'active', 1, 1)",
        )
    changes = ChangeRequests(store.database, clock=lambda: 5_000.0)
    _, item = changes.submit(
        incident_id="incident-1", facts={}, actor_id="sre-1",
        desired_outcome="scale checkout-api", context="load increased",
        idempotency_key="change-1", request_id="req-change",
        planner=lambda _payload: _plan(),
    )
    phase_id = str(item["active_phase"]["id"])  # type: ignore[index]
    with store.database.connect() as conn:
        for revision_id in ("revision-1", "revision-2"):
            assert persist_notification_request_in(
                conn,
                change_notification_request(
                    conn, event_type="change.awaiting_approval",
                    change_request_id=str(item["id"]), phase_id=phase_id,
                    revision_id=revision_id, now=5_001.0,
                ),
                now=5_001.0,
            )
    requests = NotificationOutbox(store.database).list_requests()
    event_ids = [item["event_id"] for item in requests]
    assert event_ids == [
        f"change.awaiting_approval:{phase_id}:revision-1",
        f"change.awaiting_approval:{phase_id}:revision-2",
    ]
    assert [item["facts"]["revision_id"] for item in requests] == ["revision-1", "revision-2"]


@pytest.mark.parametrize(
    ("status", "approval_status", "eligible"),
    [
        ("awaiting_approval", "approved", True),
        ("awaiting_approval", "expired", False),
    ],
)
def test_approved_terminal_or_approval_owned_phase_cannot_retry(
    tmp_path: Path, status: str, approval_status: str, eligible: bool
) -> None:
    store = GatewayV1Store(tmp_path / "gateway.db")
    with store.database.connect() as conn:
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) "
            "VALUES ('incident-1', 'Checkout', 'critical', 'active', 1, 1)",
        )
        conn.execute(
            "INSERT INTO change_requests (id, incident_id, actor_id, desired_outcome, context, idempotency_key, created_at, updated_at) "
            "VALUES ('change-1', 'incident-1', 'sre-1', 'scale', '', 'change-1', 1, 1)",
        )
        conn.execute(
            "INSERT INTO change_plan_phases (id, change_request_id, sequence, status, approval_status, created_at, updated_at) "
            "VALUES ('phase-1', 'change-1', 1, ?, ?, 1, 1)",
            (status, approval_status),
        )
    with pytest.raises(ChangeRequestError, match="not waiting for planning retry"):
        ChangeRequests(store.database).retry(
            "change-1", facts={}, actor_id="sre-1", idempotency_key="retry-1",
            request_id="req-retry", planner=lambda _: pytest.fail("planner must not run"),
            expired_retry_eligible=lambda _conn, _phase_id: eligible,
        )
