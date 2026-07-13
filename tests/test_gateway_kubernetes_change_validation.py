"""K02 Gateway-owned Kubernetes Change validation state tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from apps.aiops_k8s_gateway.change_requests import ChangeRequests
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.connector_validation_commands import ConnectorValidationCommands
from apps.aiops_k8s_gateway.kubernetes_change_validation import KubernetesChangeValidation
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


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


def _store(tmp_path: Path) -> GatewayV1Store:
    store = GatewayV1Store(tmp_path / "gateway.db", credential_factory=lambda: "connector-secret")
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="enroll", request_id="req-enroll",
    )
    commands = ConnectorCommands(store.database)
    store.connector_enrollments.register(
        credential, "connector-prod", "cluster-prod", namespace_scope=["*"],
        capabilities=["validate"], commands=commands, request_id="req-register",
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
        result_handler=store.connector_enrollments.record_verification_result_in,
    )
    with store.database.connect() as conn:
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) VALUES ('incident-1', 'Checkout', 'critical', 'active', 1, 1)"
        )
    return store


def _submit(store: GatewayV1Store, validation: KubernetesChangeValidation, change: dict[str, object]) -> dict[str, object]:
    counts: dict[str, int] = {}

    def next_id(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{counts[prefix]}"

    _, item = ChangeRequests(store.database, validation=validation, id_factory=next_id).submit(
        incident_id="incident-1",
        facts={"resource": {"cluster_id": "cluster-prod"}},
        actor_id="operator",
        desired_outcome="scale checkout-api",
        context="load increased",
        idempotency_key="change-1",
        request_id="req-change-1",
        planner=lambda _payload: {
            "status": "validating", "plan": {"summary": "scale", "changes": [change]},
        },
    )
    return item


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
    store = _store(tmp_path)
    validation = KubernetesChangeValidation(
        commands=ConnectorValidationCommands(), enrollments=store.connector_enrollments,
    )
    item = _submit(store, validation, _draft())

    assert item["active_revision"]["validation"]["status"] == "pending"  # type: ignore[index]
    commands = ConnectorCommands(store.database, clock=lambda: 10.0)
    command = commands.poll("connector-prod", "cluster-prod", 0)
    assert command is not None
    assert command["action"] == "validate_kubernetes_change"
    assert command["parameters"] == {"change": _draft()}

    result = _validation_result()
    change_requests = ChangeRequests(store.database, validation=validation)
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
    store = _store(tmp_path)
    validation = KubernetesChangeValidation(
        commands=ConnectorValidationCommands(), enrollments=store.connector_enrollments,
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
    assert ConnectorCommands(store.database).poll("connector-prod", "cluster-prod", 0) is None


def test_gateway_rejects_connector_result_whose_diff_hash_does_not_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    validation = KubernetesChangeValidation(
        commands=ConnectorValidationCommands(), enrollments=store.connector_enrollments,
    )
    _submit(store, validation, _draft())
    commands = ConnectorCommands(store.database)
    command = commands.poll("connector-prod", "cluster-prod", 0)
    assert command is not None
    commands.start(str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]))
    result = _validation_result()
    result["dry_run"]["hash"] = "0" * 64  # type: ignore[index]
    change_requests = ChangeRequests(store.database, validation=validation)
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
