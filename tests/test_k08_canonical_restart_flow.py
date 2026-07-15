"""Canonical restart through Generic Change execution and reconciliation."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from aiops.contracts import CONTROLLED_RESTART_ANNOTATION_PATH
from apps.aiops_k8s_gateway.change_plan_phases import ChangePlanPhases
from apps.aiops_k8s_gateway.change_planning_boundary import validate_gateway_plan
from apps.aiops_k8s_gateway.change_requests import ChangeRequests
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.connector_validation_commands import ConnectorValidationCommands
from apps.aiops_k8s_gateway.kubernetes_change_executions import KubernetesChangeExecutions
from apps.aiops_k8s_gateway.kubernetes_change_validation import KubernetesChangeValidation
from apps.aiops_k8s_gateway.kubernetes_reconciliation import KubernetesReconciliations
from apps.aiops_k8s_gateway.notification_requests import NotificationOutbox
from apps.cluster_connector.command_worker import (
    ConnectorCommandJournal,
    execute_kubernetes_change,
    execute_kubernetes_reconciliation,
    execute_kubernetes_validation,
)
from apps.cluster_connector.kubernetes_change_adapter import (
    execute_change_command,
    execute_validation_command,
)
from apps.cluster_connector.kubernetes_reconciliation_adapter import (
    execute_reconciliation_command,
)
from test_gateway_kubernetes_phase_approvals import _phase_approvals, _store


class _Resources:
    def get(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            api_version="apps/v1", kind="Deployment", name="deployments",
            namespaced=True, verbs=("get", "patch"),
        )


class _Kubernetes:
    def __init__(self) -> None:
        self.resources = _Resources()
        self.live = _deployment(restarted=False)
        self.final = _deployment(restarted=True)
        self.patch_bodies: list[object] = []

    def get(self, _resource: object, **_kwargs: object) -> dict[str, object]:
        return deepcopy(self.live)

    def patch(self, _resource: object, **kwargs: object) -> dict[str, object]:
        self.patch_bodies.append(deepcopy(kwargs["body"]))
        if kwargs.get("dry_run") != "All":
            self.live = deepcopy(self.final)
        return deepcopy(self.final)


def _deployment(*, restarted: bool) -> dict[str, object]:
    metadata: dict[str, object] = {
        "name": "checkout-api", "namespace": "payments", "uid": "uid-1",
        "resourceVersion": "42" if restarted else "41",
        "generation": 8 if restarted else 7,
    }
    return {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
        "spec": {
            "replicas": 3,
            "template": {
                "metadata": ({
                    "annotations": {"aiops.dev/restart-request-id": "change-request-1"},
                } if restarted else {}),
            },
        },
        "status": {
            "observedGeneration": 8 if restarted else 7,
            "updatedReplicas": 3,
            "availableReplicas": 3,
        },
    }


def _plan(change_request_id: str) -> dict[str, object]:
    return {
        "status": "validating",
        "plan": {
            "summary": "Restart checkout-api through a controlled rollout",
            "changes": [{
                "target": {
                    "api_version": "apps/v1", "kind": "Deployment",
                    "namespace": "payments", "name": "checkout-api",
                },
                "operation": "patch",
                "payload": [{
                    "op": "add", "path": CONTROLLED_RESTART_ANNOTATION_PATH,
                    "value": change_request_id,
                }],
                "post_checks": [
                    {
                        "type": "json_pointer",
                        "path": CONTROLLED_RESTART_ANNOTATION_PATH,
                        "operator": "eq", "value": change_request_id,
                    },
                    {"type": "workload_rollout"},
                ],
                "rollback": {
                    "status": "unavailable",
                    "concrete_loss": "A rollout cannot restore the previous Pod identities.",
                },
            }],
        },
    }


def test_canonical_restart_uses_generic_execution_and_reconciliation(
    tmp_path: Path,
) -> None:
    store, approver_id, _team_id = _store(tmp_path)
    now = [1_000.0]
    validation = KubernetesChangeValidation(
        commands=ConnectorValidationCommands(),
        enrollments=store.connector_enrollments,
    )
    changes = ChangeRequests(
        store.database, validation=validation, clock=lambda: now[0],
        id_factory=lambda prefix: "change-request-1" if prefix == "change-request" else f"{prefix}-1",
    )
    created, change = changes.submit(
        incident_id="incident-1",
        facts={
            "resource": {"cluster_id": "cluster-prod"},
            "change_intent": "controlled_restart",
        },
        actor_id=approver_id,
        desired_outcome="Restart checkout-api through a controlled rollout",
        context="Restore readiness without changing replicas",
        idempotency_key="restart-1", request_id="req-restart",
        planner=lambda payload: validate_gateway_plan(
            payload, _plan(str(payload["change_request_id"])),
        ),
    )
    assert created is True
    phase_id = str(change["active_phase"]["id"])  # type: ignore[index]
    revision_id = str(change["active_revision"]["id"])  # type: ignore[index]

    api = _Kubernetes()
    commands = ConnectorCommands(store.database, clock=lambda: now[0])
    validation_command = commands.poll("connector-prod", "cluster-prod", 0)
    assert validation_command is not None
    commands.start(
        str(validation_command["id"]), "connector-prod", "cluster-prod",
        str(validation_command["lease_id"]),
    )
    validation_result = execute_kubernetes_validation(
        validation_command, cluster_id="cluster-prod", allowed_namespaces={"*"},
        executor=lambda command, **kwargs: execute_validation_command(
            command, **kwargs, client_factory=lambda: api,
        ),
    )
    commands.submit_result(
        str(validation_command["id"]), "connector-prod", "cluster-prod",
        str(validation_command["lease_id"]), validation_result,
        request_id="req-validation", result_handler=changes.record_validation_result_in,
    )
    canonical_validation = json.loads(str(validation_result["stdout"]))
    assert {
        "op": "add", "path": "/spec/template/metadata/annotations", "value": {},
    } in canonical_validation["canonical_change"]["payload"]

    now[0] = 1_001.0
    authorities, approvals = _phase_approvals(store, clock=lambda: now[0])
    authorities.create(
        user_id=approver_id, environment="prod", scope_type="namespace",
        scope={"cluster_id": "cluster-prod", "namespace": "payments"},
        actor_id="admin", reason="on-call authority", request_id="req-authority",
    )
    review = approvals.review("change-request-1", actor_id=approver_id)
    approval = approvals.approve(
        "change-request-1", actor_id=approver_id, revision_id=revision_id,
        dry_run_hashes=[str(review["changes"][0]["dry_run_hash"])],  # type: ignore[index]
        target_confirmations=[str(review["changes"][0]["target_confirmation"])],  # type: ignore[index]
        rollback_policy="stop_only", reason="controlled restart",
        idempotency_key="approval-1", request_id="req-approval",
    )
    assert approval["status"] == "approved"

    now[0] = 1_002.0
    executions = KubernetesChangeExecutions(
        store.database, approvals=approvals, enrollments=store.connector_enrollments,
        phases=ChangePlanPhases(), clock=lambda: now[0],
        id_factory=lambda prefix: f"{prefix}-1",
    )
    executions.start(
        "change-request-1", phase_id=phase_id, actor_id=approver_id,
        reason="execute controlled restart", idempotency_key="execution-1",
        request_id="req-execution", execution_timeout_seconds=300,
    )
    execution_command = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch",
    )
    assert execution_command is not None
    commands.start(
        str(execution_command["id"]), "connector-prod", "cluster-prod",
        str(execution_command["lease_id"]), start_handler=executions.record_started_in,
    )
    journal = ConnectorCommandJournal(tmp_path / "connector.db", clock=lambda: now[0])
    assert journal.accept(execution_command) == "accepted"
    journal.started(str(execution_command["id"]))
    worker_result = execute_kubernetes_change(
        execution_command, cluster_id="cluster-prod", allowed_namespaces={"*"},
        now=now[0], clock=lambda: now[0],
        executor=lambda command, **kwargs: execute_change_command(
            command, **kwargs, client_factory=lambda: api, sleeper=lambda _seconds: None,
        ),
    )
    journal.terminal(str(execution_command["id"]), worker_result)
    assert worker_result["status"] == "succeeded"
    assert all(
        check["status"] == "succeeded"
        for check in json.loads(str(worker_result["stdout"]))["post_checks"]
    )

    # The API mutation succeeded, but its terminal result was not delivered to Gateway.
    now[0] = 1_303.0
    ConnectorCommands(store.database, clock=lambda: now[0]).reconcile_unknown_outcomes()
    executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-timeout")
    reconciliation_command = commands.poll("connector-prod", "cluster-prod", 0)
    assert reconciliation_command is not None
    assert reconciliation_command["action"] == "reconcile_kubernetes_change"
    commands.start(
        str(reconciliation_command["id"]), "connector-prod", "cluster-prod",
        str(reconciliation_command["lease_id"]),
    )
    reconciliation_result = execute_kubernetes_reconciliation(
        reconciliation_command, cluster_id="cluster-prod", allowed_namespaces={"*"},
        observed_at=now[0],
        executor=lambda command, **kwargs: execute_reconciliation_command(
            command, **kwargs, client_factory=lambda: api,
        ),
    )
    commands.submit_result(
        str(reconciliation_command["id"]), "connector-prod", "cluster-prod",
        str(reconciliation_command["lease_id"]), reconciliation_result,
        request_id="req-reconciliation", result_handler=executions.record_result_in,
    )
    projected = executions.for_phase(phase_id)
    assert projected is not None
    assert projected["status"] == "effect_observed"
    evidence = projected["reconciliation"]
    assert evidence["classification"] == "effect_observed"  # type: ignore[index]

    reconciliations = KubernetesReconciliations(
        store.database, approvals=approvals, clock=lambda: now[0],
        id_factory=lambda prefix: f"{prefix}-accepted",
    )
    accepted = reconciliations.accept(
        "change-request-1", phase_id, actor_id=approver_id,
        evidence_sha256=str(evidence["evidence_sha256"]),  # type: ignore[index]
        reason="effect confirmed", idempotency_key="accept-1",
        request_id="req-accept",
    )
    assert accepted["state"] == "accepted"

    NotificationOutbox(store.database).reconcile_change_progress()
    events = [
        request["event_type"]
        for request in NotificationOutbox(store.database).list_requests()
    ]
    assert len(events) == 5
    assert set(events) == {
        "change.awaiting_approval", "change.approved", "change.outcome_unknown",
        "change.effect_observed", "change.reconciliation_accepted",
    }
    assert len(api.patch_bodies) == 2
    assert api.patch_bodies[-1][-1] == {  # type: ignore[index]
        "op": "add", "path": CONTROLLED_RESTART_ANNOTATION_PATH,
        "value": "change-request-1",
    }
