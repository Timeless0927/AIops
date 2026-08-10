"""K02 Gateway-owned Kubernetes Change validation state tests."""

from __future__ import annotations

import hashlib
import json
import base64
from pathlib import Path

import jsonschema
import pytest

from apps.aiops_k8s_gateway.change_requests import ChangeRequestError, ChangeRequests
from apps.aiops_k8s_gateway.change_plan_phases import ChangePlanPhases
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.connector_enrollments import ConnectorEnrollments
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.kubernetes_change_validation import KubernetesChangeValidation
from apps.aiops_k8s_gateway.secure_inputs import SecureInputs
from aiops.domain.identity import SQLiteIdentityStore


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _draft(*, extra_checks: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments", "name": "checkout-api",
        },
        "operation": "patch",
        "payload": [{"op": "replace", "path": "/spec/replicas", "value": 5}],
        "post_checks": [
            {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5},
            *(extra_checks or []),
        ],
    }


def _store(tmp_path: Path) -> tuple[GatewayDatabase, ConnectorEnrollments]:
    store = GatewayDatabase(tmp_path / "gateway.db")
    enrollments = ConnectorEnrollments(store, credential_factory=lambda: "connector-secret")
    _, credential = enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="enroll", request_id="req-enroll",
    )
    commands = ConnectorCommands(
        store,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    enrollments.register(
        credential, "connector-prod", "cluster-prod", namespace_scope=["*"],
        capabilities=["validate"], commands=commands, request_id="req-register",
    )
    enrollments.heartbeat(
        credential, "connector-prod", "cluster-prod", status="online",
        failure_summary="", request_id="req-heartbeat",
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
        request_id="req-verify",
        result_handler=enrollments.record_verification_result_in,
    )
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) VALUES ('incident-1', 'Checkout', 'critical', 'active', 1, 1)"
        )
    return store, enrollments


def test_degraded_connector_rejects_new_dry_run_validation(tmp_path: Path) -> None:
    store, enrollments = _store(tmp_path)
    enrollments.heartbeat(
        "connector-secret", "connector-prod", "cluster-prod", status="degraded",
        failure_summary="owner unavailable", request_id="req-degraded",
    )
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store), enrollments=enrollments,
    )
    with pytest.raises(ChangeRequestError) as unavailable:
        _submit(store, validation, _draft())
    assert unavailable.value.code == "cluster_not_ready"


def _submit(
    store: GatewayDatabase,
    validation: KubernetesChangeValidation,
    change: dict[str, object] | list[dict[str, object]],
) -> dict[str, object]:
    counts: dict[str, int] = {}

    def next_id(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{counts[prefix]}"

    _, item = ChangeRequests(store, validation=validation, id_factory=next_id).submit(
        incident_id="incident-1",
        facts={"resource": {"cluster_id": "cluster-prod"}},
        actor_id="operator",
        desired_outcome="scale checkout-api",
        context="load increased",
        idempotency_key="change-1",
        request_id="req-change-1",
        planner=lambda _payload: {
            "status": "validating", "plan": {
                "summary": "scale", "changes": change if isinstance(change, list) else [change],
            },
        },
    )
    return item


def test_change_after_api_surface_change_requires_a_new_phase(tmp_path: Path) -> None:
    store, enrollments = _store(tmp_path)
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store),
        enrollments=enrollments,
    )
    crd = _draft()
    crd["target"] = {
        "api_version": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
        "namespace": None, "name": "widgets.example.com",
    }

    state = _submit(store, validation, [crd, _draft()])

    assert state["active_revision"]["validation"]["changes"][1]["policy_error"]["code"] == (  # type: ignore[index]
        "api_surface_change_requires_new_phase"
    )
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM connector_commands WHERE action = 'validate_kubernetes_change'",
        ).fetchone()[0] == 1


def _validation_result() -> dict[str, object]:
    canonical = {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments", "name": "checkout-api",
            "uid": "uid-1", "resource_version": "41",
        },
        "operation": "patch",
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "41"},
            {"op": "test", "path": "/spec/replicas", "value": 3},
            {"op": "replace", "path": "/spec/replicas", "value": 5},
        ],
        "post_checks": [{"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5}],
    }
    diff = [{"op": "replace", "path": "/spec/replicas", "before": 3, "after": 5}]
    digest = hashlib.sha256(_json({"canonical_change": canonical, "diff": diff}).encode()).hexdigest()
    return {
        "discovery": {
            "api_version": "apps/v1", "kind": "Deployment", "resource": "deployments",
            "namespaced": True, "verbs": ["create", "delete", "get", "patch"],
        },
        "live": {"exists": True, "uid": "uid-1", "resource_version": "41"},
        "canonical_change": canonical,
        "dry_run": {"diff": diff, "hash": digest},
    }


def test_validation_owner_queues_typed_connector_command_and_projects_trusted_result(tmp_path: Path) -> None:
    store, enrollments = _store(tmp_path)
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store), enrollments=enrollments,
    )
    item = _submit(store, validation, _draft())

    assert item["active_revision"]["validation"]["status"] == "pending"  # type: ignore[index]
    commands = ConnectorCommands(
        store, clock=lambda: 10.0,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    command = commands.poll("connector-prod", "cluster-prod", 0)
    assert command is not None
    assert command["action"] == "validate_kubernetes_change"
    assert command["parameters"] == {"change": _draft()}

    result = _validation_result()
    change_requests = ChangeRequests(store, validation=validation)
    normalized = {
        "status": "succeeded", "stdout": _json(result), "stderr": "", "exit_code": 0,
        "truncated": False, "error_code": None, "error_message": None,
    }
    commands.start(str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]))
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]), normalized,
        request_id="req-validation-result", result_handler=change_requests.record_validation_result_in,
    )
    projected = change_requests.get("change-request-1")

    state = projected["active_revision"]["validation"]  # type: ignore[index]
    assert state["status"] == "succeeded"
    assert projected["status"] == "awaiting_approval"
    assert projected["active_phase"]["status"] == "awaiting_approval"  # type: ignore[index]
    assert state["changes"][0]["result"]["canonical_change"]["target"]["uid"] == "uid-1"
    assert state["changes"][0]["result"]["dry_run"]["diff"] == [
        {"op": "replace", "path": "/spec/replicas", "before": 3, "after": 5},
    ]
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
    jsonschema.Draft202012Validator(
        spec["components"]["schemas"]["ChangeRequest"],
        resolver=jsonschema.RefResolver.from_schema(spec),
    ).validate(projected)


def test_query_guard_failure_is_public_policy_error_and_queues_no_command(tmp_path: Path) -> None:
    store, enrollments = _store(tmp_path)
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store), enrollments=enrollments,
    )
    item = _submit(store, validation, _draft(extra_checks=[{
        "type": "loki",
        "query": '{job=~".*"}',
        "start": "2026-07-13T00:00:00Z",
        "end": "2026-07-13T01:00:00Z",
        "operator": "eq",
        "value": 0,
        "limit": 100,
    }]))

    state = item["active_revision"]["validation"]  # type: ignore[index]
    assert state["status"] == "failed"
    assert item["status"] == "planning"
    assert state["changes"][0]["policy_error"]["code"] == "post_check_query_rejected"
    assert ConnectorCommands(
        store,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    ).poll("connector-prod", "cluster-prod", 0) is None


def test_gateway_rejects_connector_result_whose_diff_hash_does_not_match(tmp_path: Path) -> None:
    store, enrollments = _store(tmp_path)
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store), enrollments=enrollments,
    )
    _submit(store, validation, _draft())
    commands = ConnectorCommands(
        store,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    command = commands.poll("connector-prod", "cluster-prod", 0)
    assert command is not None
    commands.start(str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]))
    result = _validation_result()
    result["dry_run"]["hash"] = "0" * 64  # type: ignore[index]
    change_requests = ChangeRequests(store, validation=validation)
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        {
            "status": "succeeded", "stdout": _json(result), "stderr": "", "exit_code": 0,
            "truncated": False, "error_code": None, "error_message": None,
        },
        request_id="req-invalid-result", result_handler=change_requests.record_validation_result_in,
    )

    projected = change_requests.get("change-request-1")
    state = projected["active_revision"]["validation"]  # type: ignore[index]
    assert state["status"] == "failed"
    assert projected["status"] == "planning"
    assert state["changes"][0]["policy_error"]["code"] == "invalid_validation_result"


def test_openapi_draft_contract_rejects_connector_owned_precondition_tests() -> None:
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
    schema = spec["components"]["schemas"]["DraftKubernetesChange"]
    draft = _draft()
    draft["payload"] = [{"op": "test", "path": "/metadata/uid", "value": "model-guess"}]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(
            schema, resolver=jsonschema.RefResolver.from_schema(spec),
        ).validate(draft)


def test_sensitive_validation_queues_only_ciphertext_and_projects_key_hash(tmp_path: Path) -> None:
    store, enrollments = _store(tmp_path)
    identity = SQLiteIdentityStore(store.db_path)
    identity.upsert_user({
        "id": "operator", "username": "operator", "display_name": "Operator",
        "password": "strong-password", "roles": ["viewer"],
    })
    identity.close()
    key_path = tmp_path / "change.key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    secure_inputs = SecureInputs(
        store, key_path=key_path, id_factory=lambda: "opaque-1",
    )
    secure = secure_inputs.create(
        actor_id="operator", key_name="api.token", value="must-never-persist",
        generated_bytes=None, idempotency_key="secure-1", request_id="req-secure-1",
    )
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store), enrollments=enrollments,
        secure_inputs=secure_inputs,
        availability_recorder=ChangePlanPhases().record_secure_input_unavailable_in,
    )
    draft = {
        "target": {"api_version": "v1", "kind": "Secret", "namespace": "payments", "name": "api-key"},
        "operation": "create",
        "payload": {
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "api-key", "namespace": "payments"},
            "stringData": {"token": secure["placeholder"]},
        },
        "post_checks": [{"type": "exists"}],
        "rollback": {"status": "available"},
    }

    item = _submit(store, validation, draft)
    command = ConnectorCommands(
        store,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    ).poll("connector-prod", "cluster-prod", 0)

    assert command is not None
    assert command["parameters"]["change"] == draft
    refs = command["parameters"]["secure_inputs"]
    assert [{key: ref[key] for key in ("id", "key_name", "sha256", "placeholder")} for ref in refs] == [{
        "id": "opaque-1", "key_name": "api.token", "sha256": secure["sha256"],
        "placeholder": secure["placeholder"],
    }]
    assert "must-never-persist" not in str(command)
    assert b"must-never-persist" not in store.db_path.read_bytes()
    validation_id = item["active_revision"]["validation"]["changes"][0]["command_id"]  # type: ignore[index]
    assert validation_id is not None
    commands = ConnectorCommands(
        store, clock=lambda: 10.0,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    commands.start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
    )
    change_requests = ChangeRequests(store, validation=validation)
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        {
            "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
            "truncated": False, "error_code": "secure_input_unavailable",
            "error_message": "Secure Input key is unavailable",
        },
        request_id="req-validation-key-lost",
        result_handler=change_requests.record_validation_result_in,
    )
    unavailable = change_requests.get(str(item["id"]))
    assert unavailable["status"] == "secure_input_unavailable"
    with store.connect() as conn:
        persisted_parameters = json.loads(str(conn.execute(
            "SELECT parameters_json FROM connector_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()[0]))
    assert persisted_parameters["secure_inputs"] == [{
        "key_name": "api.token", "sha256": secure["sha256"],
    }]
    transported_ciphertext = str(refs[0]["ciphertext"])
    assert transported_ciphertext.encode() not in store.db_path.read_bytes()


def test_gateway_key_loss_marks_validation_plan_unavailable(tmp_path: Path) -> None:
    store, enrollments = _store(tmp_path)
    identity = SQLiteIdentityStore(store.db_path)
    identity.upsert_user({
        "id": "operator", "username": "operator", "display_name": "Operator",
        "password": "strong-password", "roles": ["viewer"],
    })
    identity.close()
    key_path = tmp_path / "change.key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    secure_inputs = SecureInputs(
        store, key_path=key_path, id_factory=lambda: "opaque-1",
    )
    secure = secure_inputs.create(
        actor_id="operator", key_name="api.token", value="must-never-persist",
        generated_bytes=None, idempotency_key="secure-1", request_id="req-secure-1",
    )
    key_path.write_bytes(base64.urlsafe_b64encode(b"r" * 32))
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store), enrollments=enrollments,
        secure_inputs=secure_inputs,
        availability_recorder=ChangePlanPhases().record_secure_input_unavailable_in,
    )
    draft = {
        "target": {
            "api_version": "v1", "kind": "Secret",
            "namespace": "payments", "name": "api-key",
        },
        "operation": "create",
        "payload": {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "api-key", "namespace": "payments"},
            "stringData": {"token": secure["placeholder"]},
        },
        "post_checks": [{"type": "exists"}],
        "rollback": {"status": "available"},
    }

    unavailable = _submit(store, validation, draft)

    assert unavailable["status"] == "secure_input_unavailable"
    policy = unavailable["active_revision"]["validation"]["changes"][0]["policy_error"]  # type: ignore[index]
    assert policy["code"] == "secure_input_unavailable"
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM connector_commands WHERE action = 'validate_kubernetes_change'",
        ).fetchone()[0] == 0


def test_failed_sensitive_validation_releases_ciphertext_hold(tmp_path: Path) -> None:
    store, enrollments = _store(tmp_path)
    identity = SQLiteIdentityStore(store.db_path)
    identity.upsert_user({
        "id": "operator", "username": "operator", "display_name": "Operator",
        "password": "strong-password", "roles": ["viewer"],
    })
    identity.close()
    key_path = tmp_path / "change.key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    secure_inputs = SecureInputs(
        store, key_path=key_path, clock=lambda: 1.0,
        id_factory=lambda: "opaque-1",
    )
    secure = secure_inputs.create(
        actor_id="operator", key_name="api.token", value="must-never-persist",
        generated_bytes=None, idempotency_key="secure-1", request_id="req-secure-1",
    )
    validation = KubernetesChangeValidation(
        commands=ConnectorCommands(store), enrollments=enrollments,
        secure_inputs=secure_inputs,
        availability_recorder=ChangePlanPhases().record_secure_input_unavailable_in,
    )
    draft = {
        "target": {
            "api_version": "v1", "kind": "Secret",
            "namespace": "payments", "name": "api-key",
        },
        "operation": "create",
        "payload": {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "api-key", "namespace": "payments"},
            "stringData": {"token": secure["placeholder"]},
        },
        "post_checks": [{"type": "exists"}],
        "rollback": {"status": "available"},
    }
    item = _submit(store, validation, draft)
    commands = ConnectorCommands(
        store, clock=lambda: 10.0,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    command = commands.poll("connector-prod", "cluster-prod", 0)
    assert command is not None
    commands.start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
    )
    change_requests = ChangeRequests(store, validation=validation)
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        {
            "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
            "truncated": False, "error_code": "kubernetes_validation_failed",
            "error_message": "API server rejected dry-run",
        },
        request_id="req-validation-failed",
        result_handler=change_requests.record_validation_result_in,
    )

    assert change_requests.get(str(item["id"]))["status"] == "planning"
    assert secure_inputs.cleanup_expired(now=10.0) == 1
