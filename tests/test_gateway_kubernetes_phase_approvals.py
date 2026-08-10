"""K03 exact Kubernetes Change Plan Phase Approval tests."""

from __future__ import annotations

import hashlib
import json
import base64
from pathlib import Path

import pytest

from aiops.domain.identity import SQLiteIdentityStore
from apps.aiops_k8s_gateway import main as _gateway_migrations  # noqa: F401
from apps.aiops_k8s_gateway.change_plan_phases import ChangePlanPhases, reconcile_change_notifications
from apps.aiops_k8s_gateway.change_requests import ChangeRequestError, ChangeRequests
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.connector_enrollments import ConnectorEnrollments
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.kubernetes_change_authorities import (
    KubernetesChangeAuthorities,
    KubernetesChangeAuthorityError,
    requires_cluster_change_authority,
)
from apps.aiops_k8s_gateway.kubernetes_change_validation import KubernetesChangeValidation
from apps.aiops_k8s_gateway.kubernetes_phase_approvals import (
    KubernetesPhaseApprovalError,
    KubernetesPhaseApprovals,
)
from apps.aiops_k8s_gateway.notification_requests import NotificationOutbox
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.secure_inputs import SecureInputs
from apps.aiops_k8s_gateway.identity_administration import IdentityAdministration


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _notification_events(store: GatewayDatabase) -> list[str]:
    reconcile_change_notifications(store)
    return [str(request["event_type"]) for request in NotificationOutbox(store).list_requests()]


def _store(tmp_path: Path) -> tuple[GatewayDatabase, str, str]:
    store = GatewayDatabase(tmp_path / "gateway.db")
    enrollments = ConnectorEnrollments(store, credential_factory=lambda: "connector-secret")
    SQLiteIdentityStore(store.db_path).close()
    _, approver = IdentityAdministration(store).mutate(
        collection="users", target_id=None,
        payload={"username": "approver", "display_name": "Approver", "password": "strong-password"},
        actor_id="admin", reason="test", action="users_create", request_id="req-user",
    )
    _, team = IdentityAdministration(store).mutate(
        collection="teams", target_id=None, payload={"name": "Payments", "description": ""},
        actor_id="admin", reason="test", action="teams_create", request_id="req-team",
    )
    IdentityAdministration(store).mutate(
        collection="team-memberships", target_id=None,
        payload={"user_id": approver["id"], "team_id": team["id"]},
        actor_id="admin", reason="test", action="team-memberships_create", request_id="req-membership",
    )
    _, credential = enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="req-enroll",
    )
    commands = ConnectorCommands(
        store, available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    enrollments.register(
        credential, "connector-prod", "cluster-prod", namespace_scope=["*"],
        capabilities=["validate", "execute"], commands=commands, request_id="req-register",
    )
    verification = commands.poll("connector-prod", "cluster-prod", 0)
    assert verification is not None
    commands.start(
        str(verification["id"]), "connector-prod", "cluster-prod", str(verification["lease_id"]),
    )
    commands.submit_result(
        str(verification["id"]), "connector-prod", "cluster-prod", str(verification["lease_id"]),
        {
            "status": "succeeded", "stdout": '{"apiVersion":"v1","kind":"PodList","items":[]}',
            "stderr": "", "exit_code": 0, "truncated": False, "error_code": None, "error_message": None,
        },
        request_id="req-verify", result_handler=enrollments.record_verification_result_in,
    )
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) "
            "VALUES ('incident-1', 'Checkout', 'critical', 'active', 1, 1)"
        )
    return store, str(approver["id"]), str(team["id"])


def _phase_approvals(
    store: GatewayDatabase, *, clock, secure_inputs: SecureInputs | None = None,
) -> tuple[KubernetesChangeAuthorities, KubernetesPhaseApprovals]:
    enrollments = ConnectorEnrollments(store)
    validation = KubernetesChangeValidation(commands=ConnectorCommands(store), enrollments=enrollments, secure_inputs=secure_inputs)
    authorities = KubernetesChangeAuthorities(
        store, user_active_in=IdentityAdministration.user_active_in,
        enrollments=enrollments, catalog=ResourceCatalog(store), clock=clock,
    )
    return authorities, KubernetesPhaseApprovals(
        store, enrollments=enrollments, authorities=authorities, phases=ChangePlanPhases(),
        validation=validation, clock=clock,
    )


def _draft(name: str = "checkout-api", replicas: int = 5) -> dict[str, object]:
    return {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment",
            "namespace": "payments", "name": name,
        },
        "operation": "patch",
        "payload": [{"op": "replace", "path": "/spec/replicas", "value": replicas}],
        "post_checks": [
            {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": replicas},
        ],
    }


def _validation_result(name: str = "checkout-api", replicas: int = 5) -> dict[str, object]:
    canonical = {
        **_draft(name, replicas),
        "target": {
            **_draft(name, replicas)["target"],  # type: ignore[dict-item]
            "uid": "uid-1", "resource_version": "41",
        },
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "41"},
            {"op": "test", "path": "/spec/replicas", "value": 3},
            {"op": "replace", "path": "/spec/replicas", "value": replicas},
        ],
    }
    diff = [{"op": "replace", "path": "/spec/replicas", "before": 3, "after": replicas}]
    digest = hashlib.sha256(_json({"canonical_change": canonical, "diff": diff}).encode()).hexdigest()
    return {
        "discovery": {
            "api_version": "apps/v1", "kind": "Deployment", "resource": "deployments",
            "namespaced": True, "verbs": ["get", "patch"],
        },
        "live": {"exists": True, "uid": "uid-1", "resource_version": "41"},
        "canonical_change": canonical,
        "dry_run": {"diff": diff, "hash": digest},
    }


def _awaiting_approval(
    store: GatewayDatabase,
    *,
    now: float,
    actor_id: str = "admin",
    drafts: list[dict[str, object]] | None = None,
    secure_inputs: SecureInputs | None = None,
    validation_results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    enrollments = ConnectorEnrollments(store)
    plan_changes = drafts or [_draft()]
    validation = KubernetesChangeValidation(commands=ConnectorCommands(store), enrollments=enrollments, secure_inputs=secure_inputs)
    changes = ChangeRequests(store, validation=validation, clock=lambda: now)
    _, item = changes.submit(
        incident_id="incident-1", facts={"resource": {"cluster_id": "cluster-prod"}},
        actor_id=actor_id, desired_outcome="scale checkout-api", context="load increased",
        idempotency_key="change-1", request_id=f"req-change-{now}",
        planner=lambda _payload: {
            "status": "validating", "plan": {"summary": "scale", "changes": plan_changes},
        },
    )
    commands = ConnectorCommands(
        store, clock=lambda: now, available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    for index, draft in enumerate(plan_changes):
        command = commands.poll("connector-prod", "cluster-prod", 0)
        assert command is not None
        commands.start(
            str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        )
        target = draft["target"]
        payload = draft["payload"]
        assert isinstance(target, dict)
        if validation_results is None:
            assert isinstance(payload, list)
        commands.submit_result(
            str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
            {
                "status": "succeeded",
                "stdout": _json(
                    validation_results[index]
                    if validation_results is not None
                    else _validation_result(str(target["name"]), int(payload[-1]["value"]))  # type: ignore[index]
                ),
                "stderr": "", "exit_code": 0, "truncated": False,
                "error_code": None, "error_message": None,
            },
            request_id=f"req-validation-{index}", result_handler=changes.record_validation_result_in,
        )
    return changes.get(str(item["id"]))


def _bound_service(store: GatewayDatabase, team_id: str) -> str:
    catalog = ResourceCatalog(store)
    [candidate] = catalog.refresh_discovery(
        "cluster-prod",
        [DiscoveryObservation(
            namespace="payments", workload_kind="Deployment", workload_name="checkout-api",
        )],
    )
    service = catalog.create_service(
        team_id=team_id, name="Checkout", description="", actor_id="admin",
        reason="test", request_id="req-service",
    )
    catalog.confirm_binding(
        candidate_id=str(candidate["id"]), service_id=str(service["id"]), actor_id="admin",
        reason="test", request_id="req-binding",
    )
    return str(service["id"])


def test_exact_namespace_authority_is_required_to_read_phase_diff(tmp_path: Path) -> None:
    store, approver_id, _ = _store(tmp_path)
    item = _awaiting_approval(store, now=1_000.0)
    assert _notification_events(store) == ["change.awaiting_approval"]
    authorities, approvals = _phase_approvals(store, clock=lambda: 1_001.0)

    for actor_id in ("admin", approver_id):
        with pytest.raises(KubernetesPhaseApprovalError, match="not found") as denied:
            approvals.review(str(item["id"]), actor_id=actor_id)
        assert denied.value.code == "not_found"

    with pytest.raises(KubernetesPhaseApprovalError) as approval_denied:
        approvals.approve(
            str(item["id"]), actor_id=approver_id,
            revision_id=str(item["active_revision"]["id"]),  # type: ignore[index]
            dry_run_hashes=[str(_validation_result()["dry_run"]["hash"])],  # type: ignore[index]
            target_confirmations=["apps/v1:Deployment:payments/checkout-api"],
            rollback_policy="stop_only", reason="attempt without authority",
            idempotency_key="denied-approval", request_id="req-denied-approval",
        )
    assert approval_denied.value.code == "not_found"
    assert approvals.audit_history(str(item["id"]))[-1]["request_id"] == "req-denied-approval"

    authorities.create(
        user_id=approver_id,
        environment="staging",
        scope_type="namespace",
        scope={"cluster_id": "cluster-prod", "namespace": "payments"},
        actor_id="admin", reason="wrong environment", request_id="req-authority-staging",
    )
    with pytest.raises(KubernetesPhaseApprovalError) as wrong_environment:
        approvals.review(str(item["id"]), actor_id=approver_id)
    assert wrong_environment.value.code == "not_found"

    authority = authorities.create(
        user_id=approver_id,
        environment="prod",
        scope_type="namespace",
        scope={"cluster_id": "cluster-prod", "namespace": "payments"},
        actor_id="admin", reason="on-call namespace authority", request_id="req-authority-prod",
    )
    review = approvals.review(str(item["id"]), actor_id=approver_id)

    assert authority["scope"] == {"cluster_id": "cluster-prod", "namespace": "payments"}
    assert review["revision_id"] == item["active_revision"]["id"]  # type: ignore[index]
    assert review["status"] == "awaiting_approval"
    assert review["environment"] == "prod"
    assert review["changes"][0]["target_confirmation"] == "apps/v1:Deployment:payments/checkout-api"
    assert review["changes"][0]["dry_run_hash"] == _validation_result()["dry_run"]["hash"]  # type: ignore[index]
    assert review["changes"][0]["authority_id"] == authority["id"]
    assert review["dry_run_expires_at"] == 1_600.0


def test_approval_freezes_exact_revision_and_rechecks_authority_before_start(tmp_path: Path) -> None:
    store, approver_id, _ = _store(tmp_path)
    item = _awaiting_approval(store, now=2_000.0, actor_id=approver_id)
    authorities, approvals = _phase_approvals(store, clock=lambda: 2_001.0)
    authority = authorities.create(
        user_id=approver_id,
        environment="prod",
        scope_type="namespace",
        scope={"cluster_id": "cluster-prod", "namespace": "payments"},
        actor_id="admin", reason="on-call namespace authority", request_id="req-authority",
    )
    review = approvals.review(str(item["id"]), actor_id=approver_id)
    revision_id = str(review["revision_id"])
    hashes = [str(change["dry_run_hash"]) for change in review["changes"]]  # type: ignore[index]
    confirmations = [str(change["target_confirmation"]) for change in review["changes"]]  # type: ignore[index]

    with pytest.raises(KubernetesPhaseApprovalError) as stale:
        approvals.approve(
            str(item["id"]), actor_id=approver_id, revision_id="revision-stale",
            dry_run_hashes=hashes, target_confirmations=confirmations,
            rollback_policy="rollback_completed", reason="restore service capacity",
            idempotency_key="approve-stale", request_id="req-stale",
        )
    assert stale.value.code == "phase_stale"

    approved = approvals.approve(
        str(item["id"]), actor_id=approver_id, revision_id=revision_id,
        dry_run_hashes=hashes, target_confirmations=confirmations,
        rollback_policy="rollback_completed", reason="restore service capacity",
        idempotency_key="approve-once", request_id="req-approve",
    )
    replay = approvals.approve(
        str(item["id"]), actor_id=approver_id, revision_id=revision_id,
        dry_run_hashes=hashes, target_confirmations=confirmations,
        rollback_policy="rollback_completed", reason="restore service capacity",
        idempotency_key="approve-once", request_id="req-replay",
    )

    assert approved["status"] == "approved"
    assert approved["approval"]["idempotent"] is False  # type: ignore[index]
    assert approved["approval"]["rollback_policy"] == "rollback_completed"  # type: ignore[index]
    assert approved["approval"]["reason"] == "restore service capacity"  # type: ignore[index]
    assert approved["approval"]["request_id"] == "req-approve"  # type: ignore[index]
    assert item["submitted_by"] == approved["approval"]["approver_id"]  # type: ignore[index]
    assert approved["approval"]["authority_ids"] == [authority["id"]]  # type: ignore[index]
    assert approved["approval"]["start_expires_at"] == 2_901.0  # type: ignore[index]
    assert approved["approval"]["frozen_changes"][0]["dry_run_hash"] == hashes[0]  # type: ignore[index]
    assert replay["approval"]["id"] == approved["approval"]["id"]  # type: ignore[index]
    assert replay["approval"]["idempotent"] is True  # type: ignore[index]
    assert _notification_events(store) == ["change.awaiting_approval", "change.approved"]
    assert ChangeRequests(store).get(str(item["id"]))["events"][-1]["type"] == "change_request.phase_approved"  # type: ignore[index]

    with pytest.raises(KubernetesPhaseApprovalError) as conflict:
        approvals.approve(
            str(item["id"]), actor_id=approver_id, revision_id=revision_id,
            dry_run_hashes=hashes, target_confirmations=confirmations,
            rollback_policy="stop_only", reason="different request",
            idempotency_key="approve-once", request_id="req-conflict",
        )
    assert conflict.value.code == "idempotency_conflict"
    audit = approvals.audit_history(str(item["id"]))
    assert [event["result"] for event in audit] == ["phase_stale", "approved", "idempotency_conflict"]
    assert [event["request_id"] for event in audit] == ["req-stale", "req-approve", "req-conflict"]
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO change_plan_phases "
            "(id, change_request_id, sequence, status, created_at, updated_at) "
            "VALUES ('phase-later', ?, 2, 'planning', 2002, 2002)",
            (item["id"],),
        )
        conn.execute(
            "INSERT INTO change_plan_revisions "
            "(id, change_request_id, phase_id, revision, status, plan_json, created_at) "
            "VALUES ('revision-later', ?, 'phase-later', 2, 'validating', ?, 2002)",
            (item["id"], _json({"summary": "later", "changes": [_draft("later-object")]})),
        )
    start = approvals.authorize_start(
        str(review["phase_id"]), request_id="req-start-old-phase", stage="grant",
    )
    assert start["revision_id"] == revision_id
    start_audit = approvals.audit_history(str(item["id"]))[-1]
    assert {key: start_audit[key] for key in (
        "phase_id", "actor_id", "result", "reason", "request_id",
    )} == {
        "phase_id": review["phase_id"], "actor_id": approver_id,
        "result": "authorized", "reason": "grant_authorization",
        "request_id": "req-start-old-phase",
    }
    with store.connect() as conn:
        conn.execute(
            "UPDATE change_plan_phases SET execution_status = 'executing' WHERE id = ?",
            (review["phase_id"],),
        )
    assert approvals.authorize_start(
        str(review["phase_id"]), request_id="req-later-step", stage="grant",
    )["revision_id"] == revision_id
    with store.connect() as conn:
        conn.execute(
            "UPDATE change_plan_phases SET orchestration_status = 'rolling_back' WHERE id = ?",
            (review["phase_id"],),
        )
    assert approvals.authorize_start(
        str(review["phase_id"]), request_id="req-rollback-step", stage="dispatch",
    )["revision_id"] == revision_id

    authorities.update(
        str(authority["id"]), active=False, actor_id="admin",
        reason="on-call ended", request_id="req-revoke",
    )
    with pytest.raises(KubernetesPhaseApprovalError) as hidden:
        approvals.review(str(item["id"]), actor_id=approver_id)
    assert hidden.value.code == "not_found"
    with pytest.raises(KubernetesPhaseApprovalError) as revoked:
        approvals.authorize_start(
            str(review["phase_id"]), request_id="req-revoked-start", stage="grant",
        )
    assert revoked.value.code == "authority_revoked"
    assert approvals.audit_history(str(item["id"]))[-1]["request_id"] == "req-revoked-start"


@pytest.mark.parametrize("scope_type", ["object", "service", "cluster"])
def test_object_service_and_cluster_authorities_cover_only_real_exact_targets(
    tmp_path: Path, scope_type: str,
) -> None:
    store, approver_id, team_id = _store(tmp_path)
    item = _awaiting_approval(store, now=3_000.0)
    authorities, approvals = _phase_approvals(store, clock=lambda: 3_001.0)
    if scope_type == "object":
        scope = {
            "cluster_id": "cluster-prod", "api_version": "apps/v1", "kind": "Deployment",
            "namespace": "payments", "name": "checkout-api",
        }
    elif scope_type == "service":
        scope = {"service_id": _bound_service(store, team_id)}
    else:
        scope = {"cluster_id": "cluster-prod"}
    authorities.create(
        user_id=approver_id, environment="prod", scope_type=scope_type, scope=scope,
        actor_id="admin", reason="exact mutation authority", request_id=f"req-{scope_type}",
    )

    review = approvals.review(str(item["id"]), actor_id=approver_id)
    assert review["changes"][0]["target"]["name"] == "checkout-api"  # type: ignore[index]


def test_dry_run_and_approved_start_windows_expire_without_refresh(tmp_path: Path) -> None:
    store, approver_id, _ = _store(tmp_path)
    item = _awaiting_approval(store, now=4_000.0)
    on_time_authorities, on_time = _phase_approvals(store, clock=lambda: 4_001.0)
    on_time_authorities.create(
        user_id=approver_id, environment="prod", scope_type="cluster",
        scope={"cluster_id": "cluster-prod"}, actor_id="admin",
        reason="cluster authority", request_id="req-authority",
    )
    review = on_time.review(str(item["id"]), actor_id=approver_id)
    approved = on_time.approve(
        str(item["id"]), actor_id=approver_id, revision_id=str(review["revision_id"]),
        dry_run_hashes=[str(change["dry_run_hash"]) for change in review["changes"]],  # type: ignore[index]
        target_confirmations=[str(change["target_confirmation"]) for change in review["changes"]],  # type: ignore[index]
        rollback_policy="stop_only", reason="approved inside window",
        idempotency_key="approve", request_id="req-approve",
    )
    _, expired = _phase_approvals(store, clock=lambda: 4_902.0)

    expired_review = expired.review(str(item["id"]), actor_id=approver_id)
    assert expired_review["status"] == "expired"
    with pytest.raises(KubernetesPhaseApprovalError) as late_start:
        expired.authorize_start(
            str(approved["phase_id"]), request_id="req-expired-start", stage="dispatch",
        )
    assert late_start.value.code == "phase_expired"
    assert expired.authorize_cancel(
        str(approved["phase_id"]), actor_id=approver_id, request_id="req-cancel-expired",
    )["id"] == approved["approval"]["id"]  # type: ignore[index]
    events = ChangeRequests(store).get(str(item["id"]))["events"]
    assert [event["type"] for event in events].count("change_request.phase_expired") == 1  # type: ignore[index]

    other_store, other_approver, _ = _store(tmp_path / "other")
    other_item = _awaiting_approval(other_store, now=5_000.0)
    too_late_authorities, too_late = _phase_approvals(other_store, clock=lambda: 5_601.0)
    too_late_authorities.create(
        user_id=other_approver, environment="prod", scope_type="cluster",
        scope={"cluster_id": "cluster-prod"}, actor_id="admin",
        reason="cluster authority", request_id="req-other-authority",
    )
    projected = ChangeRequests(other_store).project_for_actor(
        ChangeRequests(other_store).get(str(other_item["id"])),
        actor_id=other_approver, phase_access=too_late.access_for_projection,
    )
    assert projected["status"] == "expired"
    assert projected["active_phase"]["status"] == "expired"  # type: ignore[index]


def test_expired_dry_run_can_retry_into_a_fresh_revision(tmp_path: Path) -> None:
    store, approver_id, _ = _store(tmp_path)
    item = _awaiting_approval(store, now=5_000.0, actor_id=approver_id)
    authorities, approvals = _phase_approvals(store, clock=lambda: 5_601.0)
    authorities.create(
        user_id=approver_id, environment="prod", scope_type="cluster", scope={"cluster_id": "cluster-prod"},
        actor_id="admin", reason="cluster authority", request_id="req-authority",
    )
    assert approvals.review(str(item["id"]), actor_id=approver_id)["status"] == "expired"
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store), enrollments=ConnectorEnrollments(store),
    )
    changes = ChangeRequests(store, validation=validation, clock=lambda: 5_602.0)
    retried = changes.retry(
        str(item["id"]), facts={"resource": {"cluster_id": "cluster-prod"}}, actor_id=approver_id,
        idempotency_key="retry-expired", request_id="req-retry",
        planner=lambda _payload: {"status": "validating", "plan": {"summary": "scale", "changes": [_draft()]}},
        expired_retry_eligible=approvals.expired_retry_eligible_in,
    )
    assert retried["status"] == "validating"
    assert retried["active_revision"]["number"] == 2  # type: ignore[index]


def test_multi_object_approval_preserves_validation_order(tmp_path: Path) -> None:
    store, approver_id, _ = _store(tmp_path)
    item = _awaiting_approval(
        store,
        now=6_000.0,
        actor_id=approver_id,
        drafts=[_draft("checkout-api", 5), _draft("checkout-worker", 2)],
    )
    authorities, approvals = _phase_approvals(store, clock=lambda: 6_001.0)
    authorities.create(
        user_id=approver_id, environment="prod", scope_type="namespace",
        scope={"cluster_id": "cluster-prod", "namespace": "payments"}, actor_id="admin",
        reason="namespace authority", request_id="req-authority",
    )
    review = approvals.review(str(item["id"]), actor_id=approver_id)
    confirmations = [str(change["target_confirmation"]) for change in review["changes"]]  # type: ignore[index]
    hashes = [str(change["dry_run_hash"]) for change in review["changes"]]  # type: ignore[index]
    approved = approvals.approve(
        str(item["id"]), actor_id=approver_id, revision_id=str(review["revision_id"]),
        dry_run_hashes=hashes, target_confirmations=confirmations,
        rollback_policy="rollback_completed", reason="scale both workloads",
        idempotency_key="approve-two", request_id="req-approve-two",
    )

    assert confirmations == [
        "apps/v1:Deployment:payments/checkout-api",
        "apps/v1:Deployment:payments/checkout-worker",
    ]
    assert [change["target_confirmation"] for change in approved["approval"]["frozen_changes"]] == confirmations  # type: ignore[index]
    assert all(
        change["inverse_change"] is not None
        for change in approved["approval"]["frozen_changes"]  # type: ignore[index]
    )


def test_irreversible_change_exposes_concrete_loss_and_requires_stop_only(tmp_path: Path) -> None:
    store, approver_id, _ = _store(tmp_path)
    draft = _draft()
    draft["rollback"] = {
        "status": "unavailable",
        "concrete_loss": "The current unmanaged runtime state cannot be reconstructed.",
    }
    item = _awaiting_approval(
        store, now=7_000.0, actor_id=approver_id, drafts=[draft],
    )
    authorities, approvals = _phase_approvals(store, clock=lambda: 7_001.0)
    authorities.create(
        user_id=approver_id, environment="prod", scope_type="namespace",
        scope={"cluster_id": "cluster-prod", "namespace": "payments"},
        actor_id="admin", reason="namespace authority", request_id="req-authority",
    )
    with pytest.raises(KubernetesPhaseApprovalError) as hidden:
        approvals.review(str(item["id"]), actor_id=approver_id)
    assert hidden.value.code == "not_found"
    authorities.create(
        user_id=approver_id, environment="prod", scope_type="cluster",
        scope={"cluster_id": "cluster-prod"}, actor_id="admin",
        reason="irreversible change authority", request_id="req-cluster-authority",
    )
    review = approvals.review(str(item["id"]), actor_id=approver_id)
    change = review["changes"][0]
    assert change["rollback"] == draft["rollback"]
    assert change["inverse_change"] is None

    with pytest.raises(KubernetesPhaseApprovalError) as unsafe:
        approvals.approve(
            str(item["id"]), actor_id=approver_id, revision_id=str(review["revision_id"]),
            dry_run_hashes=[str(change["dry_run_hash"])],
            target_confirmations=[str(change["target_confirmation"])],
            rollback_policy="rollback_completed", reason="unsafe rollback request",
            idempotency_key="approval-unsafe", request_id="req-approval-unsafe",
        )
    assert unsafe.value.code == "rollback_unavailable"

    approved = approvals.approve(
        str(item["id"]), actor_id=approver_id, revision_id=str(review["revision_id"]),
        dry_run_hashes=[str(change["dry_run_hash"])],
        target_confirmations=[str(change["target_confirmation"])],
        rollback_policy="stop_only", reason="accept concrete permanent loss",
        idempotency_key="approval-safe", request_id="req-approval-safe",
    )
    assert approved["approval"]["rollback_policy"] == "stop_only"  # type: ignore[index]


def test_privileged_workload_effect_rejects_namespace_authority(tmp_path: Path) -> None:
    store, approver_id, _ = _store(tmp_path)
    authorities, _ = _phase_approvals(store, clock=lambda: 7_001.0)
    authorities.create(
        user_id=approver_id, environment="prod", scope_type="namespace",
        scope={"cluster_id": "cluster-prod", "namespace": "payments"}, actor_id="admin",
        reason="namespace authority", request_id="req-namespace-authority",
    )
    facts = {"resource": {
        "cluster_id": "cluster-prod", "environment": "prod", "namespace": "payments",
        "workload_kind": "Deployment", "workload_name": "checkout-api",
    }}
    plan = {"summary": "grant host access", "changes": [{
        **_draft(),
        "payload": [{
            "op": "add", "path": "/spec/template/spec/volumes/-",
            "value": {"name": "host", "hostPath": {"path": "/etc"}},
        }],
    }]}

    assert not authorities.authorize_draft_plan(facts, actor_id=approver_id, plan=plan)
    authorities.create(
        user_id=approver_id, environment="prod", scope_type="cluster",
        scope={"cluster_id": "cluster-prod"}, actor_id="admin",
        reason="sensitive workload authority", request_id="req-cluster-authority",
    )
    assert authorities.authorize_draft_plan(facts, actor_id=approver_id, plan=plan)


def test_sensitive_phase_requires_cluster_authority_and_projects_only_key_hash(tmp_path: Path) -> None:
    store, approver_id, _ = _store(tmp_path)
    key_path = tmp_path / "change.key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    secure_inputs = SecureInputs(
        store, key_path=key_path, clock=lambda: 8_000.0,
        id_factory=lambda: "opaque-1",
    )
    secure = secure_inputs.create(
        actor_id=approver_id, key_name="api.token", value="must-never-persist",
        generated_bytes=None, idempotency_key="secure-1", request_id="req-secure-1",
    )
    draft = {
        "target": {"api_version": "v1", "kind": "Secret", "namespace": "payments", "name": "api-key"},
        "operation": "create",
        "payload": {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "api-key", "namespace": "payments"},
            "stringData": {"token": secure["placeholder"]},
        },
        "post_checks": [{"type": "exists"}],
        "rollback": {"status": "available"},
    }
    canonical = {
        **draft,
        "target": {**draft["target"], "uid": None, "resource_version": None},
    }
    canonical.pop("rollback")
    diff = [{
        "op": "add", "path": "", "before": None,
        "after": {
            **draft["payload"],
            "stringData": {"token": {"secure_input": {
                "key_name": "api.token", "sha256": secure["sha256"],
            }}},
        },
    }]
    validation_result = {
        "discovery": {
            "api_version": "v1", "kind": "Secret", "resource": "secrets",
            "namespaced": True, "verbs": ["create", "get"],
        },
        "live": {"exists": False, "uid": None, "resource_version": None},
        "canonical_change": canonical,
        "dry_run": {
            "diff": diff,
            "hash": hashlib.sha256(_json({"canonical_change": canonical, "diff": diff}).encode()).hexdigest(),
        },
    }
    item = _awaiting_approval(
        store, now=8_000.0, actor_id=approver_id, drafts=[draft],
        secure_inputs=secure_inputs, validation_results=[validation_result],
    )
    authorities, approvals = _phase_approvals(
        store, clock=lambda: 8_001.0, secure_inputs=secure_inputs,
    )
    authorities.create(
        user_id=approver_id, environment="prod", scope_type="namespace",
        scope={"cluster_id": "cluster-prod", "namespace": "payments"},
        actor_id="admin", reason="namespace authority", request_id="req-namespace",
    )
    with pytest.raises(KubernetesPhaseApprovalError) as hidden:
        approvals.review(str(item["id"]), actor_id=approver_id)
    assert hidden.value.code == "not_found"
    authorities.create(
        user_id=approver_id, environment="prod", scope_type="cluster",
        scope={"cluster_id": "cluster-prod"}, actor_id="admin",
        reason="sensitive change authority", request_id="req-cluster",
    )

    review = approvals.review(str(item["id"]), actor_id=approver_id)
    assert review["changes"][0]["secure_inputs"] == [{
        "key_name": "api.token", "sha256": secure["sha256"],
    }]
    assert "must-never-persist" not in json.dumps(review)
    assert b"must-never-persist" not in store.db_path.read_bytes()
    change = review["changes"][0]
    approved = approvals.approve(
        str(item["id"]), actor_id=approver_id, revision_id=str(review["revision_id"]),
        dry_run_hashes=[str(change["dry_run_hash"])],
        target_confirmations=[str(change["target_confirmation"])],
        rollback_policy="stop_only", reason="approve sensitive input by hash",
        idempotency_key="approval-sensitive", request_id="req-approval-sensitive",
    )
    public_ref = approved["approval"]["frozen_changes"][0]["secure_inputs"][0]  # type: ignore[index]
    assert public_ref == {"key_name": "api.token", "sha256": secure["sha256"]}
    private_ref = approvals.authorize_start(
        str(review["phase_id"]), request_id="req-authorize", stage="grant",
    )["frozen_changes"][0]["secure_inputs"][0]
    assert private_ref["id"] == "opaque-1" and private_ref["placeholder"] == secure["placeholder"]


def test_proposal_generation_and_projection_require_current_authority(tmp_path: Path) -> None:
    store, approver_id, _ = _store(tmp_path)
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store), enrollments=ConnectorEnrollments(store),
    )
    changes = ChangeRequests(store, validation=validation, clock=lambda: 7_000.0)
    authorities, approvals = _phase_approvals(store, clock=lambda: 7_001.0)
    facts = {"resource": {
        "cluster_id": "cluster-prod", "environment": "prod", "namespace": "payments",
        "workload_kind": "Deployment", "workload_name": "checkout-api",
    }}
    with pytest.raises(KubernetesChangeAuthorityError) as forbidden:
        authorities.authorize_proposal(facts, actor_id=approver_id)
    assert forbidden.value.code == "proposal_forbidden"
    object_authority = authorities.create(
        user_id=approver_id, environment="prod", scope_type="object",
        scope={
            "cluster_id": "cluster-prod", "api_version": "v1", "kind": "Service",
            "namespace": "payments", "name": "checkout-api",
        },
        actor_id="admin", reason="service object authority", request_id="req-object-authority",
    )
    authorities.authorize_proposal(facts, actor_id=approver_id)
    assert authorities.authorize_draft_plan(
        facts, actor_id=approver_id,
        plan={"summary": "node port", "changes": [{
            **_draft(),
            "target": {"api_version": "v1", "kind": "Service", "namespace": "payments", "name": "checkout-api"},
        }]},
    )
    authorities.update(
        str(object_authority["id"]), active=False, actor_id="admin",
        reason="replace test authority", request_id="req-disable-object",
    )
    authority = authorities.create(
        user_id=approver_id, environment="prod", scope_type="namespace",
        scope={"cluster_id": "cluster-prod", "namespace": "payments"}, actor_id="admin",
        reason="namespace authority", request_id="req-pending-authority",
    )
    authorities.authorize_proposal(facts, actor_id=approver_id)
    assert not authorities.authorize_draft_plan(
        facts, actor_id=approver_id,
        plan={"summary": "expand", "changes": [_draft("checkout-api") | {"target": {**_draft()["target"], "namespace": "other"}}]},
    )
    with pytest.raises(ChangeRequestError) as rejected:
        changes.submit(
            incident_id="incident-1", facts=facts, actor_id=approver_id,
            desired_outcome="change another namespace", context="scope expansion",
            idempotency_key="rejected-change", request_id="req-rejected-change",
            planner=lambda _payload: {
                "status": "validating",
                "plan": {"summary": "expand", "changes": [
                    _draft() | {"target": {**_draft()["target"], "namespace": "other"}},
                ]},
            },
            plan_authorizer=lambda plan: authorities.authorize_draft_plan(
                facts, actor_id=approver_id, plan=plan,
            ),
        )
    assert rejected.value.code == "proposal_forbidden"
    with store.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM change_requests WHERE idempotency_key = 'rejected-change'"
        ).fetchone() is None
        rejected_audit = conn.execute(
            "SELECT actor_id, desired_outcome, idempotency_key, request_id, result, reason "
            "FROM rejected_change_request_proposals WHERE idempotency_key = 'rejected-change'"
        ).fetchone()
    assert dict(rejected_audit) == {
        "actor_id": approver_id,
        "desired_outcome": "change another namespace",
        "idempotency_key": "rejected-change",
        "request_id": "req-rejected-change",
        "result": "proposal_forbidden",
        "reason": "authority_scope",
    }
    _, item = changes.submit(
        incident_id="incident-1", facts=facts,
        actor_id=approver_id, desired_outcome="scale checkout-api", context="load increased",
        idempotency_key="pending-change", request_id="req-pending-change",
        planner=lambda _payload: {
            "status": "validating", "plan": {"summary": "scale", "changes": [_draft()]},
        },
        plan_authorizer=lambda plan: authorities.authorize_draft_plan(
            facts, actor_id=approver_id, plan=plan,
        ),
    )
    visible = changes.project_for_actor(
        item, actor_id=approver_id, phase_access=approvals.access_for_projection,
    )
    assert visible["active_revision"]["plan"]["changes"][0]["target"]["name"] == "checkout-api"  # type: ignore[index]
    assert "phase_review" not in visible
    authorities.update(
        str(authority["id"]), active=False, actor_id="admin",
        reason="authority revoked", request_id="req-revoke-pending",
    )
    item["status"] = "planning"
    item["active_phase"]["status"] = "planning"  # type: ignore[index]
    hidden = changes.project_for_actor(
        item, actor_id=approver_id, phase_access=approvals.access_for_projection,
    )
    assert hidden["active_revision"]["plan"] is None  # type: ignore[index]
    assert hidden["active_revision"]["validation"] is None  # type: ignore[index]
