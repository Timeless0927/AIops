"""Transaction races at the Connector Command lifecycle projection boundary."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.connector_enrollments import ConnectorEnrollments
from apps.aiops_k8s_gateway.kubernetes_change_executions import KubernetesChangeExecutions
from apps.aiops_k8s_gateway.kubernetes_reconciliation import KubernetesReconciliations
from test_gateway_kubernetes_change_executions import ApprovalBoundary, _executions, _store


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_late_successful_observation_wins_before_failed_reconciliation(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    with store.connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    boundary = ApprovalBoundary(approver_id)
    executions = _executions(store, boundary)
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )
    mutation = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch",
    )
    assert mutation is not None
    ConnectorCommands(store, clock=lambda: 1_002.0).start(
        str(mutation["id"]), "connector-prod", "cluster-prod", str(mutation["lease_id"]),
        start_handler=executions.record_started_in,
    )
    ConnectorCommands(store, clock=lambda: 1_303.0).reconcile_unknown_outcomes()
    commands = ConnectorCommands(store, clock=lambda: 1_304.0)
    reconciliation = KubernetesReconciliations(
        store, approvals=boundary, commands=commands, clock=lambda: 1_304.0,
    )
    later = KubernetesChangeExecutions(
        store, approvals=boundary, enrollments=ConnectorEnrollments(store),
        commands=commands, reconciliations=reconciliation, clock=lambda: 1_304.0,
    )
    assert later.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-timeout",
    ) is None
    enrollments = ConnectorEnrollments(store, clock=lambda: 1_304.0)
    observer = ConnectorCommands(
        store, clock=lambda: 1_304.0,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    observation = observer.poll("connector-prod", "cluster-prod", 0)
    assert observation is not None
    observer.start(
        str(observation["id"]), "connector-prod", "cluster-prod",
        str(observation["lease_id"]),
    )
    failed = {
        "status": "failed", "stdout": "", "stderr": "", "exit_code": None,
        "truncated": False, "error_code": "reconciliation_unavailable",
        "error_message": "reconciliation_unavailable",
    }
    with store.connect() as conn:
        conn.execute(
            "UPDATE connector_commands SET status = 'failed', result_json = ?, "
            "result_received_at = 1304, updated_at = 1304 WHERE id = ?",
            (_json(failed), observation["id"]),
        )

    evidence: dict[str, object] = {
        "classification": "effect_observed",
        "target": {"exists": True, "uid": "uid-1", "resource_version": "42"},
        "effect_matches": True,
        "post_checks": [{"type": "json_pointer", "status": "succeeded"}],
        "observed_at": 1_305.0,
    }
    evidence["evidence_sha256"] = hashlib.sha256(_json(evidence).encode()).hexdigest()
    succeeded = {
        "status": "succeeded", "stdout": _json(evidence), "stderr": "", "exit_code": 0,
        "truncated": False, "error_code": None, "error_message": None,
    }
    ready = threading.Event()
    proceed = threading.Event()
    completed = threading.Event()
    counts: list[int] = []

    def reconcile_failed() -> None:
        with store.connect() as conn:
            ready.set()
            proceed.wait(timeout=2)
            counts.append(reconciliation.reconcile_failed_observations_in(
                conn, now=1_305.0,
            ))
        completed.set()

    worker = threading.Thread(target=reconcile_failed)
    worker.start()
    assert ready.wait(timeout=2)
    with store.connect() as success_conn:
        success_conn.execute("BEGIN IMMEDIATE")
        encoded = _json(succeeded)
        success_conn.execute(
            "UPDATE connector_commands SET status = 'succeeded', result_json = ?, "
            "result_hash = ?, result_received_at = 1305, updated_at = 1305 WHERE id = ?",
            (
                encoded, hashlib.sha256(encoded.encode()).hexdigest(), observation["id"],
            ),
        )
        later.record_result_in(success_conn, str(observation["id"]), succeeded, 1_305.0)
        proceed.set()
        assert completed.wait(0.1) is False
        success_conn.commit()
        worker.join(timeout=2)

    assert completed.is_set() and counts == [0]
    projected = later.for_phase("phase-1")
    assert projected is not None and projected["status"] == "effect_observed"
    assert projected["reconciliation"]["classification"] == "effect_observed"
