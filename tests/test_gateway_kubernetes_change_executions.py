"""K04 Gateway single Kubernetes Change execution tests."""

from __future__ import annotations

import hashlib
import json
import base64
from pathlib import Path

import pytest

from aiops.contracts.connector_journal import terminal_journal_evidence
from aiops.domain.identity import SQLiteIdentityStore
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommandError, ConnectorCommands
from apps.aiops_k8s_gateway.kubernetes_change_executions import (
    KubernetesChangeExecutionError,
    KubernetesChangeExecutions,
)
from apps.aiops_k8s_gateway.kubernetes_phase_approvals import KubernetesPhaseApprovalError
from apps.aiops_k8s_gateway.kubernetes_reconciliation import KubernetesReconciliations
from apps.aiops_k8s_gateway.identity_administration import IdentityAdministration
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store
from apps.aiops_k8s_gateway.secure_inputs import SecureInputs


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _change() -> dict[str, object]:
    return {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
            "name": "checkout-api", "uid": "uid-1", "resource_version": "41",
        },
        "operation": "patch",
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "41"},
            {"op": "test", "path": "/spec/replicas", "value": 3},
            {"op": "replace", "path": "/spec/replicas", "value": 5},
        ],
        "post_checks": [
            {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5},
        ],
    }


def _execution_result(
    change: dict[str, object] | None = None, *, status: str = "succeeded"
) -> dict[str, object]:
    frozen = change or _change()
    checks = frozen["post_checks"]
    assert isinstance(checks, list)
    return {
        "status": "succeeded" if status == "succeeded" else "failed",
        "stdout": "", "stderr": "", "exit_code": 0 if status == "succeeded" else None,
        "truncated": False,
        "error_code": None if status == "succeeded" else "post_check_failed",
        "error_message": None if status == "succeeded" else "failed",
        "execution": {
            "operation": frozen["operation"],
            "target": {"exists": frozen["operation"] != "delete", "uid": "uid-final", "resource_version": "42"},
            "post_checks": [
                {"type": item["type"], "status": "succeeded" if status == "succeeded" else "failed"}
                for item in checks
            ],
        },
    }


class ApprovalBoundary:
    def __init__(
        self, approver_id: str, *, deny_dispatch: bool = False,
        frozen_changes: list[dict[str, object]] | None = None,
    ) -> None:
        self.approver_id = approver_id
        self.deny_dispatch = deny_dispatch
        self.frozen_changes = frozen_changes
        self.calls: list[tuple[str, str]] = []

    def authorize_start(self, phase_id: str, *, request_id: str, stage: str) -> dict[str, object]:
        self.calls.append((stage, request_id))
        if stage == "dispatch" and self.deny_dispatch:
            raise KubernetesPhaseApprovalError("authority_revoked", "Authority revoked")
        change = _change()
        return {
            "id": "approval-1", "phase_id": phase_id, "revision_id": "revision-1",
            "approver_id": self.approver_id, "change_request_id": "change-1",
            "cluster_id": "cluster-prod", "environment": "prod",
            "frozen_changes": self.frozen_changes or [{
                "ordinal": 1, "canonical_change": change, "dry_run_hash": "d" * 64,
            }],
        }

    def authorize_reconciliation(
        self, phase_id: str, *, actor_id: str, request_id: str,
    ) -> dict[str, object]:
        assert actor_id == self.approver_id
        return self.authorize_start(
            phase_id, request_id=request_id, stage="reconciliation_check",
        )


def _store(tmp_path: Path, *, verify_connector: bool = True) -> tuple[GatewayV1Store, str]:
    store = GatewayV1Store(tmp_path / "gateway.db", credential_factory=lambda: "connector-secret")
    SQLiteIdentityStore(store.db_path).close()
    _, approver = IdentityAdministration(store.database).mutate(
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
    store.connector_enrollments.heartbeat(
        credential, "connector-prod", "cluster-prod", status="online",
        failure_summary="", request_id="req-heartbeat",
    )
    if verify_connector:
        verification = commands.poll("connector-prod", "cluster-prod", 0)
        assert verification is not None
        commands.start(
            str(verification["id"]), "connector-prod", "cluster-prod",
            str(verification["lease_id"]),
        )
        commands.submit_result(
            str(verification["id"]), "connector-prod", "cluster-prod",
            str(verification["lease_id"]),
            {
                "status": "succeeded",
                "stdout": '{"apiVersion":"v1","kind":"PodList","items":[]}',
                "stderr": "", "exit_code": 0, "truncated": False,
                "error_code": None, "error_message": None,
            },
            request_id="req-verify",
            result_handler=store.connector_enrollments.record_verification_result_in,
        )
    now = 1_000.0
    frozen = [{
        "ordinal": 1, "canonical_change": _change(), "dry_run_hash": "d" * 64,
    }]
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
                      'approved', 'req-approval', 'stop_only', '[]', ?, 1500, 1000, 1900)
            """,
            (approver["id"], "a" * 64, _json(frozen)),
        )
    return store, str(approver["id"])


def _executions(
    store: GatewayV1Store,
    boundary: ApprovalBoundary,
    *,
    now: float = 1_001.0,
    secure_inputs: SecureInputs | None = None,
) -> KubernetesChangeExecutions:
    counts: dict[str, int] = {}

    def next_id(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{counts[prefix]}"

    return KubernetesChangeExecutions(
        store.database, approvals=boundary, enrollments=store.connector_enrollments,
        secure_inputs=secure_inputs, clock=lambda: now, id_factory=next_id,
    )


def test_start_issues_one_60_second_grant_and_is_idempotent(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id)
    executions = _executions(store, boundary)

    started = executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start approved change", idempotency_key="start-1",
        request_id="req-start", execution_timeout_seconds=300,
    )
    replay = executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start approved change", idempotency_key="start-1",
        request_id="req-replay", execution_timeout_seconds=300,
    )

    assert started["status"] == "queued"
    assert started["grant"]["expires_at"] == 1_061.0  # type: ignore[index]
    assert started["grant"]["consumed_at"] is None  # type: ignore[index]
    assert replay["id"] == started["id"] and replay["idempotent"] is True
    assert boundary.calls == [("grant", "req-start")]
    with pytest.raises(KubernetesChangeExecutionError) as second:
        executions.start(
            "change-1", phase_id="phase-1", actor_id=approver_id,
            reason="second start", idempotency_key="start-2",
            request_id="req-second", execution_timeout_seconds=300,
        )
    assert second.value.code == "execution_exists"


def test_sensitive_step_dispatches_ciphertext_and_key_rotation_fails_before_mutation(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    key_path = tmp_path / "change.key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    secure_inputs = SecureInputs(
        store.database, key_path=key_path, clock=lambda: 1_001.0,
        id_factory=lambda: "opaque-1",
    )
    secure = secure_inputs.create(
        actor_id=approver_id, key_name="api.token", value="must-never-persist",
        generated_bytes=None, idempotency_key="secure-1", request_id="req-secure-1",
    )
    change = {
        "target": {
            "api_version": "v1", "kind": "Secret", "namespace": "payments",
            "name": "api-key", "uid": None, "resource_version": None,
        },
        "operation": "create",
        "payload": {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "api-key", "namespace": "payments"},
            "stringData": {"token": secure["placeholder"]},
        },
        "post_checks": [{"type": "exists"}],
    }
    frozen = [{
        "ordinal": 1, "canonical_change": change, "dry_run_hash": "d" * 64,
        "secure_inputs": [{
            key: secure[key] for key in ("id", "key_name", "placeholder", "sha256")
        }],
    }]
    boundary = ApprovalBoundary(approver_id, frozen_changes=frozen)
    executions = _executions(store, boundary, secure_inputs=secure_inputs)
    started = executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start sensitive change", idempotency_key="start-sensitive",
        request_id="req-start-sensitive", execution_timeout_seconds=300,
    )
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")

    assert command is not None
    assert command["parameters"]["change"] == change
    assert command["parameters"]["secure_inputs"][0]["id"] == "opaque-1"
    assert "must-never-persist" not in str(command)
    assert b"must-never-persist" not in store.db_path.read_bytes()
    commands = ConnectorCommands(store.database, clock=lambda: 1_002.0)
    commands.start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    terminal_result = _execution_result(change)
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        terminal_result,
        journal_evidence=terminal_journal_evidence(
            str(command["id"]), terminal_result, recorded_at=1_002.0,
        ),
        request_id="req-sensitive-result", result_handler=executions.record_result_in,
    )
    with store.database.connect() as conn:
        persisted_parameters = json.loads(str(conn.execute(
            "SELECT parameters_json FROM connector_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()[0]))
    assert persisted_parameters["secure_inputs"] == [{
        "key_name": "api.token", "sha256": secure["sha256"],
    }]
    assert "ciphertext" not in json.dumps(persisted_parameters)
    transported_ciphertext = str(command["parameters"]["secure_inputs"][0]["ciphertext"])
    assert transported_ciphertext.encode() not in store.db_path.read_bytes()
    assert secure_inputs.cleanup_expired(now=1_301.0) == 0
    assert secure_inputs.cleanup_expired(now=1_302.0) == 1
    assert secure_inputs.get("opaque-1", actor_id=approver_id)["available"] is False

    store2, approver2 = _store(tmp_path / "rotated")
    rotated_key_path = tmp_path / "rotated.key"
    rotated_key_path.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    rotated_inputs = SecureInputs(
        store2.database, key_path=rotated_key_path, clock=lambda: 1_001.0,
        id_factory=lambda: "opaque-2",
    )
    rotated = rotated_inputs.create(
        actor_id=approver2, key_name="api.token", value="another-secret",
        generated_bytes=None, idempotency_key="secure-2", request_id="req-secure-2",
    )
    rotated_change = json.loads(json.dumps(change))
    rotated_change["payload"]["stringData"]["token"] = rotated["placeholder"]
    rotated_boundary = ApprovalBoundary(approver2, frozen_changes=[{
        "ordinal": 1, "canonical_change": rotated_change, "dry_run_hash": "e" * 64,
        "secure_inputs": [{
            key: rotated[key] for key in ("id", "key_name", "placeholder", "sha256")
        }],
    }])
    rotated_executions = _executions(store2, rotated_boundary, secure_inputs=rotated_inputs)
    rotated_started = rotated_executions.start(
        "change-1", phase_id="phase-1", actor_id=approver2,
        reason="start sensitive change", idempotency_key="start-rotated",
        request_id="req-start-rotated", execution_timeout_seconds=300,
    )
    rotated_key_path.write_bytes(base64.urlsafe_b64encode(b"r" * 32))

    assert rotated_executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-key-lost",
    ) is None
    unavailable = rotated_executions.for_phase("phase-1")
    assert unavailable is not None and unavailable["status"] == "secure_input_unavailable"
    assert unavailable["result"]["error_code"] == "secure_input_unavailable"
    with store2.database.connect() as conn:
        phase = conn.execute(
            "SELECT availability_status FROM change_plan_phases WHERE id = 'phase-1'",
        ).fetchone()
    assert phase is not None and phase["availability_status"] == "secure_input_unavailable"
    assert started["status"] == "queued" and rotated_started["status"] == "queued"

    store3, approver3 = _store(tmp_path / "connector-lost")
    connector_key_path = tmp_path / "connector-lost.key"
    connector_key_path.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    connector_inputs = SecureInputs(
        store3.database, key_path=connector_key_path, clock=lambda: 1_001.0,
        id_factory=lambda: "opaque-3",
    )
    connector_secure = connector_inputs.create(
        actor_id=approver3, key_name="api.token", value="connector-secret",
        generated_bytes=None, idempotency_key="secure-3", request_id="req-secure-3",
    )
    connector_change = json.loads(json.dumps(change))
    connector_change["payload"]["stringData"]["token"] = connector_secure["placeholder"]
    connector_boundary = ApprovalBoundary(approver3, frozen_changes=[{
        "ordinal": 1, "canonical_change": connector_change, "dry_run_hash": "f" * 64,
        "secure_inputs": [{
            key: connector_secure[key] for key in ("id", "key_name", "placeholder", "sha256")
        }],
    }])
    connector_executions = _executions(
        store3, connector_boundary, secure_inputs=connector_inputs,
    )
    connector_executions.start(
        "change-1", phase_id="phase-1", actor_id=approver3,
        reason="start connector key loss", idempotency_key="start-connector-lost",
        request_id="req-start-connector-lost", execution_timeout_seconds=300,
    )
    connector_command = connector_executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-connector-dispatch",
    )
    assert connector_command is not None
    connector_commands = ConnectorCommands(store3.database, clock=lambda: 1_002.0)
    connector_commands.start(
        str(connector_command["id"]), "connector-prod", "cluster-prod",
        str(connector_command["lease_id"]), start_handler=connector_executions.record_started_in,
    )
    terminal_result = {
        "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
        "truncated": False, "error_code": "secure_input_unavailable",
        "error_message": "Secure Input key is unavailable",
    }
    connector_commands.submit_result(
        str(connector_command["id"]), "connector-prod", "cluster-prod",
        str(connector_command["lease_id"]),
        terminal_result,
        journal_evidence=terminal_journal_evidence(
            str(connector_command["id"]), terminal_result, recorded_at=1_002.0,
        ),
        request_id="req-connector-key-lost",
        result_handler=connector_executions.record_result_in,
    )
    connector_lost = connector_executions.for_phase("phase-1")
    assert connector_lost is not None
    assert connector_lost["status"] == "secure_input_unavailable"
    assert all(step["direction"] == "forward" for step in connector_lost["steps"])


def test_dispatch_rechecks_authority_and_consumes_grant_once(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id)
    executions = _executions(store, boundary)
    started = executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )

    command = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch",
    )
    repeated = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch-again",
    )

    assert command is not None and command["action"] == "execute_kubernetes_change"
    assert command["execution_grant_id"] == started["grant"]["id"]  # type: ignore[index]
    assert repeated is None
    assert boundary.calls == [("grant", "req-start"), ("dispatch", "req-dispatch")]
    projected = executions.for_phase("phase-1")
    assert projected is not None and projected["grant"]["consumed_at"] == 1_001.0  # type: ignore[index]


def test_revoked_dispatch_never_delivers_command(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id, deny_dispatch=True)
    executions = _executions(store, boundary)
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )

    assert executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch",
    ) is None
    projected = executions.for_phase("phase-1")
    assert projected is not None and projected["status"] == "failed"
    assert projected["result"]["error_code"] == "authority_revoked"  # type: ignore[index]
    assert projected["grant"]["consumed_at"] is None  # type: ignore[index]


@pytest.mark.parametrize("timeout", [299, 1801, True])
def test_execution_timeout_is_bounded(tmp_path: Path, timeout: object) -> None:
    store, approver_id = _store(tmp_path)
    executions = _executions(store, ApprovalBoundary(approver_id))
    with pytest.raises(KubernetesChangeExecutionError) as invalid:
        executions.start(
            "change-1", phase_id="phase-1", actor_id=approver_id,
            reason="start", idempotency_key="start", request_id="req-start",
            execution_timeout_seconds=timeout,
        )
    assert invalid.value.code == "invalid_execution_timeout"


def test_start_requires_execute_capability(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    with store.database.connect() as conn:
        row = conn.execute(
            "SELECT identity_json FROM connector_read_verifications WHERE cluster_id = 'cluster-prod'",
        ).fetchone()
        identity = json.loads(str(row["identity_json"]))
        identity["capabilities"] = ["validate"]
        conn.execute(
            "UPDATE connector_read_verifications SET identity_json = ? WHERE cluster_id = 'cluster-prod'",
            (_json(identity),),
        )
    executions = _executions(store, ApprovalBoundary(approver_id))

    with pytest.raises(KubernetesChangeExecutionError) as unavailable:
        executions.start(
            "change-1", phase_id="phase-1", actor_id=approver_id,
            reason="start", idempotency_key="start", request_id="req-start",
            execution_timeout_seconds=300,
        )
    assert unavailable.value.code == "cluster_not_ready"


def test_degraded_connector_rejects_execution_grant(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    store.connector_enrollments.heartbeat(
        "connector-secret", "connector-prod", "cluster-prod", status="degraded",
        failure_summary="owner unavailable", request_id="req-degraded",
    )
    executions = _executions(store, ApprovalBoundary(approver_id))
    with pytest.raises(KubernetesChangeExecutionError) as unavailable:
        executions.start(
            "change-1", phase_id="phase-1", actor_id=approver_id,
            reason="start", idempotency_key="start", request_id="req-start",
            execution_timeout_seconds=300,
        )
    assert unavailable.value.code == "cluster_not_ready"


def test_degraded_connector_does_not_dispatch_an_existing_grant(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    executions = _executions(store, ApprovalBoundary(approver_id))
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )
    store.connector_enrollments.heartbeat(
        "connector-secret", "connector-prod", "cluster-prod", status="degraded",
        failure_summary="owner unavailable", request_id="req-degraded",
    )
    with pytest.raises(KubernetesChangeExecutionError) as unavailable:
        executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")
    assert unavailable.value.code == "cluster_not_ready"
    projected = executions.for_phase("phase-1")
    assert projected is not None and projected["grant"]["consumed_at"] is None  # type: ignore[index]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_execution_result(), "succeeded"),
        ({"status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
          "truncated": False, "error_code": "stale_change", "error_message": "stale"}, "stale"),
        (_execution_result(status="failed"), "post_check_failed"),
        ({"status": "failed", "stdout": "", "stderr": "", "exit_code": None,
          "truncated": False, "error_code": "kubernetes_api_rejected", "error_message": "rejected"}, "failed"),
    ],
)
def test_started_and_terminal_results_update_execution_and_phase(
    tmp_path: Path, result: dict[str, object], expected: str,
) -> None:
    store, approver_id = _store(tmp_path)
    executions = _executions(store, ApprovalBoundary(approver_id))
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")
    assert command is not None
    commands = ConnectorCommands(store.database, clock=lambda: 1_002.0)
    commands.start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        result,
        journal_evidence=terminal_journal_evidence(
            str(command["id"]), result, recorded_at=1_002.0,
        ),
        request_id="req-result", result_handler=executions.record_result_in,
    )

    projected = executions.for_phase("phase-1")
    assert projected is not None and projected["status"] == expected
    assert projected["started_at"] == 1_002.0
    with store.database.connect() as conn:
        phase_status = conn.execute(
            "SELECT execution_status FROM change_plan_phases WHERE id = 'phase-1'",
        ).fetchone()[0]
        execution_expires_at = conn.execute(
            "SELECT execution_expires_at FROM connector_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()[0]
    assert phase_status == ("succeeded" if expected == "succeeded" else "failed")
    assert execution_expires_at == 1_302.0


def test_stale_change_is_terminal_without_replacement_grant_retry_or_rollback(
    tmp_path: Path,
) -> None:
    store, approver_id = _store(tmp_path)
    executions = _executions(store, ApprovalBoundary(approver_id))
    started = executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start stale probe", idempotency_key="start-stale",
        request_id="req-start-stale", execution_timeout_seconds=300,
    )
    assert (started["grant_count"], started["command_count"]) == (1, 0)
    command = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch-stale",
    )
    assert command is not None
    commands = ConnectorCommands(store.database, clock=lambda: 1_002.0)
    commands.start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    stale = {
        "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
        "truncated": False, "error_code": "stale_change",
        "error_message": "frozen target identity or resourceVersion changed",
    }
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        stale,
        journal_evidence=terminal_journal_evidence(
            str(command["id"]), stale, recorded_at=1_002.0,
        ),
        request_id="req-result-stale", result_handler=executions.record_result_in,
    )

    terminal = executions.for_phase("phase-1")
    assert terminal is not None and terminal["status"] == "stale"
    assert terminal["id"] == started["id"]
    assert terminal["grant"]["id"] == started["grant"]["id"]  # type: ignore[index]
    assert terminal["grant"]["consumed_at"] is not None  # type: ignore[index]
    assert (terminal["grant_count"], terminal["command_count"]) == (1, 1)
    assert [(step["direction"], step["status"]) for step in terminal["steps"]] == [  # type: ignore[union-attr]
        ("forward", "stale"),
    ]
    assert executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-no-retry",
    ) is None
    with pytest.raises(KubernetesChangeExecutionError, match="already has an execution"):
        executions.start(
            "change-1", phase_id="phase-1", actor_id=approver_id,
            reason="replacement", idempotency_key="replacement",
            request_id="req-replacement", execution_timeout_seconds=300,
        )
    unchanged = executions.for_phase("phase-1")
    assert unchanged == terminal
    assert (unchanged["grant_count"], unchanged["command_count"]) == (1, 1)  # type: ignore[index]


def test_started_timeout_remains_unknown_outcome_until_reconciled(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id)
    executions = _executions(store, boundary)
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )
    command = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch",
    )
    assert command is not None
    ConnectorCommands(store.database, clock=lambda: 1_002.0).start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    ConnectorCommands(store.database, clock=lambda: 1_303.0).reconcile_unknown_outcomes()
    later = _executions(store, boundary, now=1_303.0)

    assert later.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-reconcile",
    ) is None
    projected = later.for_phase("phase-1")
    assert projected is not None and projected["status"] == "unknown_outcome"
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT execution_status FROM change_plan_phases WHERE id = 'phase-1'",
        ).fetchone()[0] == "unknown_outcome"
        assert conn.execute(
            "SELECT type FROM change_request_events WHERE change_request_id = 'change-1' "
            "ORDER BY event_id DESC LIMIT 1",
        ).fetchone()[0] == "change_request.execution_outcome_unknown"


def test_unknown_outcome_observes_effect_and_requires_user_acceptance(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id)
    executions = _executions(store, boundary)
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")
    assert command is not None
    ConnectorCommands(store.database, clock=lambda: 1_002.0).start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    ConnectorCommands(store.database, clock=lambda: 1_303.0).reconcile_unknown_outcomes()
    reconciliation = KubernetesReconciliations(
        store.database, approvals=boundary, clock=lambda: 1_303.0,
        id_factory=lambda prefix: f"{prefix}-accepted")
    later = KubernetesChangeExecutions(
        store.database, approvals=boundary, enrollments=store.connector_enrollments,
        reconciliations=reconciliation, clock=lambda: 1_303.0)
    assert later.dispatch_next("connector-prod", "cluster-prod", request_id="req-timeout") is None
    observer = ConnectorCommands(store.database, clock=lambda: 1_304.0)
    observation_command = observer.poll("connector-prod", "cluster-prod", 0)
    assert observation_command is not None
    assert observation_command["action"] == "reconcile_kubernetes_change"
    observer.start(
        str(observation_command["id"]), "connector-prod", "cluster-prod",
        str(observation_command["lease_id"]))
    evidence: dict[str, object] = {
        "classification": "effect_observed",
        "target": {"exists": True, "uid": "uid-1", "resource_version": "42"},
        "effect_matches": True,
        "post_checks": [{"type": "json_pointer", "status": "succeeded"}],
        "observed_at": 1_304.0,
    }
    evidence["evidence_sha256"] = hashlib.sha256(
        _json(evidence).encode()).hexdigest()
    result = {
        "status": "succeeded", "stdout": _json(evidence), "stderr": "", "exit_code": 0,
        "truncated": False, "error_code": None, "error_message": None,
    }
    observer.submit_result(
        str(observation_command["id"]), "connector-prod", "cluster-prod",
        str(observation_command["lease_id"]), result, request_id="req-observation",
        result_handler=later.record_result_in,
    )
    projected = later.for_phase("phase-1")
    assert projected is not None and projected["status"] == "effect_observed"
    assert projected["current_step"]["status"] == "effect_observed"
    observed = projected["reconciliation"]
    assert observed["state"] == "observed"
    accepted = reconciliation.accept(
        "change-1", "phase-1", actor_id=approver_id,
        evidence_sha256=str(observed["evidence_sha256"]),
        reason="Accept exact live observation", idempotency_key="accept-1",
        request_id="req-accept",
    )
    assert accepted["state"] == "accepted" and accepted["idempotent"] is False
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT status FROM change_plan_phases WHERE id = ?",
            (accepted["replanning_phase_id"],),
        ).fetchone()[0] == "planning"
        assert conn.execute(
            "SELECT action FROM admin_audit WHERE target_id = ? ORDER BY id DESC LIMIT 1",
            (accepted["id"],),
        ).fetchone()[0] == "kubernetes_reconciliation_accept"

def test_late_journal_terminal_result_supersedes_pending_observation(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id)
    executions = _executions(store, boundary)
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")
    assert command is not None
    ConnectorCommands(store.database, clock=lambda: 1_002.0).start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    ConnectorCommands(store.database, clock=lambda: 1_303.0).reconcile_unknown_outcomes()
    later = _executions(store, boundary, now=1_303.0)
    assert later.dispatch_next("connector-prod", "cluster-prod", request_id="req-timeout") is None
    result = _execution_result()
    commands = ConnectorCommands(store.database, clock=lambda: 1_304.0)
    with pytest.raises(ConnectorCommandError, match="durable Connector journal evidence") as denied:
        commands.submit_result(
            str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
            result, request_id="req-untrusted", result_handler=later.record_result_in,
        )
    assert denied.value.code == "untrusted_terminal_result"
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        result,
        journal_evidence=terminal_journal_evidence(
            str(command["id"]), result, recorded_at=1_003.0,
        ),
        request_id="req-late", result_handler=later.record_result_in,
    )
    projected = later.for_phase("phase-1")
    assert projected is not None and projected["status"] == "succeeded"
    assert projected["reconciliation"]["state"] == "resolved"
    assert commands.poll("connector-prod", "cluster-prod", 0) is None
