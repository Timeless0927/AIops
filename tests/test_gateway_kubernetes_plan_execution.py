"""K05 Gateway multi-step Kubernetes Plan execution tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aiops.contracts import (
    CONTROLLED_RESTART_ANNOTATION_PATH,
    CONTROLLED_RESTART_ANNOTATIONS_PATH,
)
from aiops.contracts.connector_journal import terminal_journal_evidence
from aiops.domain.identity import SQLiteIdentityStore
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.kubernetes_change_executions import KubernetesChangeExecutions
from apps.aiops_k8s_gateway.kubernetes_inverse_changes import freeze_inverse_change
from apps.aiops_k8s_gateway.kubernetes_reconciliation import KubernetesReconciliations
from apps.aiops_k8s_gateway.notification_requests import NotificationOutbox
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _notification_events(store: GatewayV1Store) -> list[str]:
    NotificationOutbox(store.database).reconcile_change_progress()
    return [
        str(request["event_type"])
        for request in NotificationOutbox(store.database).list_requests()
    ]


def _change(ordinal: int) -> dict[str, object]:
    return {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
            "name": f"workload-{ordinal}", "uid": f"uid-{ordinal}",
            "resource_version": "41",
        },
        "operation": "patch",
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": f"uid-{ordinal}"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "41"},
            {"op": "test", "path": "/spec/replicas", "value": ordinal},
            {"op": "replace", "path": "/spec/replicas", "value": ordinal + 1},
        ],
        "post_checks": [
            {
                "type": "json_pointer", "path": "/spec/replicas", "operator": "eq",
                "value": ordinal + 1,
            },
        ],
    }


def _frozen_change(ordinal: int) -> dict[str, object]:
    canonical = _change(ordinal)
    reviewed = {
        "canonical_change": canonical,
        "diff": [{
            "op": "replace", "path": "/spec/replicas",
            "before": ordinal, "after": ordinal + 1,
        }],
    }
    return {
        "ordinal": ordinal, "canonical_change": canonical, "dry_run_hash": "d" * 64,
        "inverse_change": freeze_inverse_change(reviewed),
    }


def test_restart_parent_annotation_diff_has_narrow_frozen_inverse() -> None:
    annotation = {"aiops.dev/restart-request-id": "change-1"}
    change = {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
            "name": "checkout-api", "uid": "uid-1", "resource_version": "41",
        },
        "operation": "patch",
        "payload": [
            {"op": "add", "path": CONTROLLED_RESTART_ANNOTATIONS_PATH, "value": {}},
            {"op": "add", "path": CONTROLLED_RESTART_ANNOTATION_PATH, "value": "change-1"},
        ],
        "post_checks": [
            {
                "type": "json_pointer", "path": CONTROLLED_RESTART_ANNOTATION_PATH,
                "operator": "eq", "value": "change-1",
            },
            {"type": "workload_rollout"},
        ],
    }
    reviewed = {
        "canonical_change": change,
        "diff": [{
            "op": "add", "path": CONTROLLED_RESTART_ANNOTATIONS_PATH,
            "before": None, "after": annotation,
        }],
    }

    inverse = freeze_inverse_change(reviewed)

    assert inverse is not None
    assert inverse["payload"][-1] == {  # type: ignore[index]
        "op": "remove", "path": CONTROLLED_RESTART_ANNOTATIONS_PATH,
    }
    assert inverse["post_checks"] == [{"type": "workload_rollout"}]

    reviewed["diff"][0]["after"] = {  # type: ignore[index]
        **annotation, "owner": "platform",
    }
    assert freeze_inverse_change(reviewed) is None

    change["payload"] = [change["payload"][-1]]  # type: ignore[index]
    reviewed["diff"] = [{
        "op": "add", "path": CONTROLLED_RESTART_ANNOTATION_PATH,
        "before": None, "after": "change-1",
    }]
    assert freeze_inverse_change(reviewed) is not None
    reviewed["diff"][0]["op"] = "replace"  # type: ignore[index]
    assert freeze_inverse_change(reviewed) is None
    reviewed["diff"][0]["op"] = "add"  # type: ignore[index]
    reviewed["diff"][0]["after"] = "another-change"  # type: ignore[index]
    assert freeze_inverse_change(reviewed) is None


class ApprovalBoundary:
    def __init__(self, approver_id: str, *, count: int, rollback_policy: str) -> None:
        self.approver_id = approver_id
        self.frozen_changes = [_frozen_change(index) for index in range(1, count + 1)]
        self.rollback_policy = rollback_policy
        self.calls: list[tuple[str, str]] = []
        self.on_dispatch: object = None

    def authorize_start(self, phase_id: str, *, request_id: str, stage: str) -> dict[str, object]:
        self.calls.append((stage, request_id))
        if stage == "dispatch" and callable(self.on_dispatch):
            callback, self.on_dispatch = self.on_dispatch, None
            callback()
        return {
            "id": "approval-1", "phase_id": phase_id, "revision_id": "revision-1",
            "approver_id": self.approver_id, "change_request_id": "change-1",
            "cluster_id": "cluster-prod", "environment": "prod",
            "rollback_policy": self.rollback_policy, "frozen_changes": self.frozen_changes,
        }

    def authorize_cancel(
        self, phase_id: str, *, actor_id: str, request_id: str,
    ) -> dict[str, object]:
        self.calls.append(("cancel", request_id))
        assert actor_id == self.approver_id
        return self.authorize_start(phase_id, request_id=request_id, stage="cancel_check")

    def authorize_reconciliation(
        self, phase_id: str, *, actor_id: str, request_id: str,
    ) -> dict[str, object]:
        assert actor_id == self.approver_id
        return self.authorize_start(phase_id, request_id=request_id, stage="reconciliation")


def _store(tmp_path: Path, *, rollback_policy: str) -> tuple[GatewayV1Store, str]:
    store = GatewayV1Store(tmp_path / "gateway.db", credential_factory=lambda: "connector-secret")
    SQLiteIdentityStore(store.db_path).close()
    _, approver = store.mutate_admin(
        collection="users", target_id=None,
        payload={"username": "approver", "display_name": "Approver", "password": "strong-password"},
        actor_id="admin", reason="test", action="users_create", request_id="req-user",
    )
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="req-enroll",
    )
    commands = ConnectorCommands(store.database)
    store.connector_enrollments.register(
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
            "stderr": "", "exit_code": 0, "truncated": False,
            "error_code": None, "error_message": None,
        }, request_id="req-verify",
        result_handler=store.connector_enrollments.record_verification_result_in,
    )
    now = 1_000.0
    with store.database.connect() as conn:
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) "
            "VALUES ('incident-1', 'Checkout', 'critical', 'active', ?, ?)", (now, now),
        )
        conn.execute(
            "INSERT INTO change_requests "
            "(id, incident_id, actor_id, desired_outcome, context, idempotency_key, created_at, updated_at) "
            "VALUES ('change-1', 'incident-1', ?, 'scale', '', 'change-1', ?, ?)",
            (approver["id"], now, now),
        )
        conn.execute(
            "INSERT INTO change_plan_phases "
            "(id, change_request_id, sequence, status, approval_status, created_at, updated_at) "
            "VALUES ('phase-1', 'change-1', 1, 'awaiting_approval', 'approved', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO change_plan_revisions "
            "(id, change_request_id, phase_id, revision, status, plan_json, created_at) "
            "VALUES ('revision-1', 'change-1', 'phase-1', 1, 'validating', ?, ?)",
            (_json({"summary": "scale", "changes": []}), now),
        )
        conn.execute(
            """
            INSERT INTO kubernetes_phase_approvals (
                id, phase_id, revision_id, approver_id, authority_ids_json,
                idempotency_key, request_hash, reason, request_id, rollback_policy,
                target_confirmations_json, frozen_changes_json, dry_run_expires_at,
                approved_at, start_expires_at
            ) VALUES ('approval-1', 'phase-1', 'revision-1', ?, '[]', 'approval-1', ?,
                      'approved', 'req-approval', ?, '[]', '[]', 1500, 1000, 1900)
            """,
            (approver["id"], "a" * 64, rollback_policy),
        )
    return store, str(approver["id"])


def _system(
    tmp_path: Path, *, count: int = 3, rollback_policy: str = "stop_only",
) -> tuple[GatewayV1Store, KubernetesChangeExecutions, ApprovalBoundary, list[float], str]:
    store, actor_id = _store(tmp_path, rollback_policy=rollback_policy)
    boundary = ApprovalBoundary(actor_id, count=count, rollback_policy=rollback_policy)
    now = [1_001.0]
    counts: dict[str, int] = {}

    def next_id(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{counts[prefix]}"

    executions = KubernetesChangeExecutions(
        store.database, approvals=boundary, enrollments=store.connector_enrollments,
        clock=lambda: now[0], id_factory=next_id,
    )
    executions.start(
        "change-1", phase_id="phase-1", actor_id=actor_id, reason="execute plan",
        idempotency_key="start-1", request_id="req-start", execution_timeout_seconds=300,
    )
    return store, executions, boundary, now, actor_id


def _result(
    *, status: str = "succeeded", error_code: str | None = None,
    ordinal: int = 1,
) -> dict[str, object]:
    execution = {
        "operation": "patch",
        "target": {
            "exists": True, "uid": f"uid-{ordinal}",
            "resource_version": str(41 + ordinal),
        },
        "post_checks": [],
    }
    return {
        "status": status, "stdout": _json(execution), "stderr": "",
        "exit_code": 0 if status == "succeeded" else None, "truncated": False,
        "error_code": error_code, "error_message": error_code,
    }


def _run_next(
    store: GatewayV1Store,
    executions: KubernetesChangeExecutions,
    now: list[float],
    result: dict[str, object],
) -> dict[str, object]:
    command = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id=f"req-dispatch-{now[0]}",
    )
    assert command is not None
    now[0] += 1
    commands = ConnectorCommands(store.database, clock=lambda: now[0])
    commands.start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    now[0] += 1
    commands = ConnectorCommands(store.database, clock=lambda: now[0])
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        result,
        journal_evidence=terminal_journal_evidence(
            str(command["id"]), result, recorded_at=now[0],
        ),
        request_id=f"req-result-{now[0]}", result_handler=executions.record_result_in,
    )
    return command


def test_steps_receive_grants_strictly_after_prior_success(tmp_path: Path) -> None:
    store, executions, boundary, now, _ = _system(tmp_path)

    started = executions.for_phase("phase-1")
    assert started is not None
    assert [step["status"] for step in started["steps"]] == ["queued", "pending", "pending"]  # type: ignore[index]
    with store.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM kubernetes_execution_grants").fetchone()[0] == 1

    _run_next(store, executions, now, _result(ordinal=1))
    with store.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM kubernetes_execution_grants").fetchone()[0] == 1
    second = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-next")

    assert second is not None and second["parameters"]["change"]["target"]["name"] == "workload-2"  # type: ignore[index]
    with store.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM kubernetes_execution_grants").fetchone()[0] == 2
    assert [stage for stage, _ in boundary.calls] == ["grant", "dispatch", "grant", "dispatch"]


def test_successful_plan_enqueues_generic_change_notification(tmp_path: Path) -> None:
    store, executions, _, now, _ = _system(tmp_path, count=1)
    _run_next(store, executions, now, _result())

    assert executions.for_phase("phase-1")["status"] == "succeeded"  # type: ignore[index]
    assert _notification_events(store) == ["change.succeeded"]


def test_stop_only_failure_cancels_later_steps_without_rollback(tmp_path: Path) -> None:
    store, executions, _, now, _ = _system(tmp_path)
    _run_next(store, executions, now, _result(status="failed", error_code="kubernetes_api_rejected"))

    plan = executions.for_phase("phase-1")
    assert plan is not None and plan["status"] == "failed"
    assert [step["status"] for step in plan["steps"]] == ["failed", "cancelled", "cancelled"]  # type: ignore[index]
    assert _notification_events(store) == ["change.failed"]
    assert executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-none") is None
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM kubernetes_change_execution_steps WHERE direction = 'rollback'",
        ).fetchone()[0] == 0


def test_rollback_completed_runs_exact_inverses_in_reverse_order(tmp_path: Path) -> None:
    store, executions, _, now, _ = _system(tmp_path, rollback_policy="rollback_completed")
    _run_next(store, executions, now, _result(ordinal=1))
    _run_next(store, executions, now, _result(ordinal=2))
    _run_next(
        store, executions, now,
        _result(status="failed", error_code="kubernetes_api_rejected", ordinal=3),
    )

    rolling_back = executions.for_phase("phase-1")
    assert rolling_back is not None and rolling_back["status"] == "rolling_back"
    rollback_two = _run_next(store, executions, now, _result(ordinal=2))
    assert rollback_two["parameters"]["change"]["target"]["name"] == "workload-2"  # type: ignore[index]
    rollback_one = _run_next(store, executions, now, _result(ordinal=1))
    assert rollback_one["parameters"]["change"]["target"]["name"] == "workload-1"  # type: ignore[index]

    completed = executions.for_phase("phase-1")
    assert completed is not None and completed["status"] == "rolled_back"
    assert _notification_events(store) == ["change.rollback_started", "change.rolled_back"]
    forward = [step for step in completed["steps"] if step["direction"] == "forward"]  # type: ignore[index]
    assert [step["status"] for step in forward] == ["rolled_back", "rolled_back", "failed"]
    assert [step["source_ordinal"] for step in completed["steps"] if step["direction"] == "rollback"] == [2, 1]  # type: ignore[index]
    with store.database.connect() as conn:
        events = [row[0] for row in conn.execute(
            "SELECT type FROM change_request_events WHERE change_request_id = 'change-1' ORDER BY event_id",
        )]
    assert events == [
        "change_request.execution_queued",
        "change_request.execution_step_started", "change_request.execution_started",
        "change_request.execution_step_finished",
        "change_request.execution_step_granted", "change_request.execution_step_started",
        "change_request.execution_step_finished",
        "change_request.execution_step_granted", "change_request.execution_step_started",
        "change_request.execution_step_finished", "change_request.rollback_started",
        "change_request.execution_step_granted", "change_request.rollback_step_started",
        "change_request.rollback_step_finished",
        "change_request.execution_step_granted", "change_request.rollback_step_started",
        "change_request.rollback_step_finished", "change_request.rollback_finished",
    ]
    rollback_events = [event for event in events if event.startswith("change_request.rollback_")]
    assert rollback_events == [
        "change_request.rollback_started", "change_request.rollback_step_started",
        "change_request.rollback_step_finished", "change_request.rollback_step_started",
        "change_request.rollback_step_finished",
        "change_request.rollback_finished",
    ]


def test_post_check_failure_rolls_back_the_mutated_current_step(tmp_path: Path) -> None:
    store, executions, _, now, _ = _system(
        tmp_path, count=2, rollback_policy="rollback_completed",
    )
    _run_next(store, executions, now, _result(ordinal=1))
    _run_next(
        store, executions, now,
        _result(status="failed", error_code="post_check_failed", ordinal=2),
    )

    rollback = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-rollback")
    assert rollback is not None
    assert rollback["parameters"]["change"]["target"]["name"] == "workload-2"  # type: ignore[index]


def test_rollback_failure_is_terminal_and_does_not_continue(tmp_path: Path) -> None:
    store, executions, _, now, _ = _system(
        tmp_path, count=2, rollback_policy="rollback_completed",
    )
    _run_next(store, executions, now, _result(ordinal=1))
    _run_next(
        store, executions, now,
        _result(status="failed", error_code="kubernetes_api_rejected", ordinal=2),
    )
    _run_next(
        store, executions, now,
        _result(status="rejected", error_code="stale_change", ordinal=1),
    )

    failed = executions.for_phase("phase-1")
    assert failed is not None and failed["status"] == "rollback_failed"
    assert executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-none") is None


def test_unknown_outcome_never_advances_or_rolls_back(tmp_path: Path) -> None:
    store, executions, _, now, _ = _system(tmp_path, rollback_policy="rollback_completed")
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")
    assert command is not None
    now[0] += 1
    ConnectorCommands(store.database, clock=lambda: now[0]).start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    now[0] += 301
    ConnectorCommands(store.database, clock=lambda: now[0]).reconcile_unknown_outcomes()

    assert executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-reconcile") is None
    plan = executions.for_phase("phase-1")
    assert plan is not None and plan["status"] == "unknown_outcome"
    assert _notification_events(store) == ["change.outcome_unknown"]
    assert [step["direction"] for step in plan["steps"]] == ["forward", "forward", "forward"]  # type: ignore[index]
    observer = ConnectorCommands(store.database, clock=lambda: now[0])
    for _ in range(3):
        observation = observer.poll("connector-prod", "cluster-prod", 0)
        assert observation is not None and observation["action"] == "reconcile_kubernetes_change"
        observer.start(
            str(observation["id"]), "connector-prod", "cluster-prod",
            str(observation["lease_id"]),
        )
        now[0] += 31
    assert observer.poll("connector-prod", "cluster-prod", 0) is None
    assert executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-observation-failed",
    ) is None
    unavailable = executions.for_phase("phase-1")
    assert unavailable["reconciliation"]["state"] == "observed"  # type: ignore[index]
    assert unavailable["reconciliation"]["classification"] == "unknown_outcome"  # type: ignore[index]


def test_late_terminal_success_resumes_remaining_steps_without_false_plan_success(
    tmp_path: Path,
) -> None:
    store, executions, _, now, _ = _system(tmp_path, count=2)
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-first")
    assert command is not None
    now[0] += 1
    ConnectorCommands(store.database, clock=lambda: now[0]).start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    now[0] += 301
    ConnectorCommands(store.database, clock=lambda: now[0]).reconcile_unknown_outcomes()
    assert executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-timeout") is None
    now[0] += 1
    result = _result(ordinal=1)
    ConnectorCommands(store.database, clock=lambda: now[0]).submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        result,
        journal_evidence=terminal_journal_evidence(
            str(command["id"]), result, recorded_at=now[0] - 301,
        ),
        request_id="req-late", result_handler=executions.record_result_in,
    )

    resumed = executions.for_phase("phase-1")
    assert resumed is not None and resumed["status"] == "started"
    assert [step["status"] for step in resumed["steps"]] == ["succeeded", "pending"]  # type: ignore[index]
    assert executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-second",
    ) is not None


def test_accepted_reconciliation_keeps_late_terminal_from_resuming_old_plan(
    tmp_path: Path,
) -> None:
    store, executions, boundary, now, actor_id = _system(tmp_path, count=2)
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-first")
    assert command is not None
    now[0] += 1
    ConnectorCommands(store.database, clock=lambda: now[0]).start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    now[0] += 301
    ConnectorCommands(store.database, clock=lambda: now[0]).reconcile_unknown_outcomes()
    assert executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-timeout") is None
    observer = ConnectorCommands(store.database, clock=lambda: now[0])
    observation = observer.poll("connector-prod", "cluster-prod", 0)
    assert observation is not None and observation["action"] == "reconcile_kubernetes_change"
    observer.start(
        str(observation["id"]), "connector-prod", "cluster-prod", str(observation["lease_id"]),
    )
    evidence: dict[str, object] = {
        "classification": "effect_observed",
        "target": {"exists": True, "uid": "uid-1", "resource_version": "42"},
        "effect_matches": True,
        "post_checks": [{"type": "json_pointer", "status": "succeeded"}],
        "observed_at": now[0],
    }
    evidence["evidence_sha256"] = hashlib.sha256(_json(evidence).encode()).hexdigest()
    observed_result = {
        "status": "succeeded", "stdout": _json(evidence), "stderr": "", "exit_code": 0,
        "truncated": False, "error_code": None, "error_message": None,
    }
    observer.submit_result(
        str(observation["id"]), "connector-prod", "cluster-prod", str(observation["lease_id"]),
        observed_result, request_id="req-observed", result_handler=executions.record_result_in,
    )
    assert set(_notification_events(store)) == {"change.outcome_unknown", "change.effect_observed"}
    reconciliation = KubernetesReconciliations(
        store.database, approvals=boundary, clock=lambda: now[0],
    )
    projected = executions.for_phase("phase-1")
    reconciliation.accept(
        "change-1", "phase-1", actor_id=actor_id,
        evidence_sha256=str(projected["reconciliation"]["evidence_sha256"]),  # type: ignore[index]
        reason="accept observed state", idempotency_key="accept-1", request_id="req-accept",
    )
    assert set(_notification_events(store)) == {
        "change.outcome_unknown", "change.effect_observed", "change.reconciliation_accepted",
    }
    now[0] += 1
    terminal = _result(ordinal=1)
    ConnectorCommands(store.database, clock=lambda: now[0]).submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]), terminal,
        journal_evidence=terminal_journal_evidence(
            str(command["id"]), terminal, recorded_at=now[0],
        ),
        request_id="req-late", result_handler=executions.record_result_in,
    )

    old_plan = executions.for_phase("phase-1")
    assert old_plan is not None and old_plan["status"] == "unknown_outcome"
    assert [step["status"] for step in old_plan["steps"]] == ["succeeded", "cancelled"]  # type: ignore[index]
    assert old_plan["reconciliation"]["state"] == "resolved"  # type: ignore[index]
    assert old_plan["reconciliation"]["accepted_by"] == actor_id  # type: ignore[index]
    assert executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-none") is None


def test_cancel_before_start_revokes_grant_and_is_idempotent(tmp_path: Path) -> None:
    store, executions, _, _, actor_id = _system(tmp_path)
    cancelled = executions.cancel(
        "change-1", phase_id="phase-1", actor_id=actor_id, reason="maintenance window closed",
        idempotency_key="cancel-1", request_id="req-cancel",
    )
    replay = executions.cancel(
        "change-1", phase_id="phase-1", actor_id=actor_id, reason="maintenance window closed",
        idempotency_key="cancel-1", request_id="req-replay",
    )

    assert cancelled["status"] == "cancelled" and replay["idempotent"] is True
    assert all(step["status"] == "cancelled" for step in cancelled["steps"])  # type: ignore[index]
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT revoked_at FROM kubernetes_execution_grants",
        ).fetchone()[0] is not None


def test_cancel_after_dispatch_rejects_unstarted_command(tmp_path: Path) -> None:
    store, executions, _, _, actor_id = _system(tmp_path)
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")
    assert command is not None

    cancelled = executions.cancel(
        "change-1", phase_id="phase-1", actor_id=actor_id, reason="operator cancelled",
        idempotency_key="cancel-1", request_id="req-cancel",
    )

    assert cancelled["status"] == "cancelled"
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT status FROM connector_commands WHERE id = ?", (command["id"],),
        ).fetchone()[0] == "rejected"


def test_cancel_winning_the_dispatch_race_prevents_command_lease(tmp_path: Path) -> None:
    store, executions, boundary, _, actor_id = _system(tmp_path)
    boundary.on_dispatch = lambda: executions.cancel(
        "change-1", phase_id="phase-1", actor_id=actor_id, reason="operator cancelled",
        idempotency_key="cancel-race", request_id="req-cancel-race",
    )

    assert executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch-race",
    ) is None
    cancelled = executions.for_phase("phase-1")
    assert cancelled is not None and cancelled["status"] == "cancelled"
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM connector_commands WHERE action = 'execute_kubernetes_change'",
        ).fetchone()[0] == 0


def test_cancel_after_start_waits_for_current_outcome_then_stops(tmp_path: Path) -> None:
    store, executions, _, now, actor_id = _system(tmp_path)
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")
    assert command is not None
    now[0] += 1
    commands = ConnectorCommands(store.database, clock=lambda: now[0])
    commands.start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )

    requested = executions.cancel(
        "change-1", phase_id="phase-1", actor_id=actor_id, reason="operator cancelled",
        idempotency_key="cancel-1", request_id="req-cancel",
    )
    assert requested["status"] == "cancel_requested"
    now[0] += 1
    terminal_result = _result(ordinal=1)
    ConnectorCommands(store.database, clock=lambda: now[0]).submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        terminal_result,
        journal_evidence=terminal_journal_evidence(
            str(command["id"]), terminal_result, recorded_at=now[0],
        ),
        request_id="req-result", result_handler=executions.record_result_in,
    )

    completed = executions.for_phase("phase-1")
    assert completed is not None and completed["status"] == "cancelled"
    assert executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-none") is None


def test_cancel_requested_preserves_unknown_current_outcome(tmp_path: Path) -> None:
    store, executions, _, now, actor_id = _system(tmp_path)
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")
    assert command is not None
    now[0] += 1
    ConnectorCommands(store.database, clock=lambda: now[0]).start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    executions.cancel(
        "change-1", phase_id="phase-1", actor_id=actor_id, reason="operator cancelled",
        idempotency_key="cancel-1", request_id="req-cancel",
    )
    now[0] += 301
    ConnectorCommands(store.database, clock=lambda: now[0]).reconcile_unknown_outcomes()

    assert executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-reconcile",
    ) is None
    unknown = executions.for_phase("phase-1")
    assert unknown is not None and unknown["status"] == "unknown_outcome"
