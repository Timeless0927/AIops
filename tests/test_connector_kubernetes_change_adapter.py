"""K02 Connector Kubernetes SDK Adapter tests."""

from __future__ import annotations

from copy import deepcopy
import base64
import json
import os
from types import SimpleNamespace
import uuid

import pytest

from apps.cluster_connector import command_worker
from apps.cluster_connector.kubernetes_change_adapter import execute_validation_command
from apps.cluster_connector.command_worker import ConnectorCommandJournal, execute_kubernetes_validation
from aiops.security import encrypt_secure_input, key_fingerprint, secure_input_placeholder, value_hash


class NotFound(Exception):
    status = 404


class Unprocessable(Exception):
    status = 422

    def __str__(self) -> str:
        return 'raw response contains token="must-not-persist"'


class FakeResources:
    def __init__(self, resource: SimpleNamespace) -> None:
        self.resource = resource
        self.requested: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> SimpleNamespace:
        self.requested.append(kwargs)
        return self.resource


class FakeDynamicClient:
    def __init__(self, live: dict[str, object] | None, final: dict[str, object] | None) -> None:
        self.resources = FakeResources(SimpleNamespace(
            api_version="apps/v1", kind="Deployment", name="deployments", namespaced=True,
            verbs=("get", "create", "patch", "delete"),
        ))
        self.live = deepcopy(live)
        self.final = deepcopy(final)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, resource: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("get", kwargs))
        if self.live is None:
            raise NotFound()
        return deepcopy(self.live)

    def create(self, resource: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("create", kwargs))
        return deepcopy(self.final or {})

    def patch(self, resource: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("patch", kwargs))
        return deepcopy(self.final or {})

    def delete(self, resource: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("delete", kwargs))
        return {"kind": "Status", "status": "Success"}


def _command(operation: str, payload: object, *, kind: str = "Deployment") -> dict[str, object]:
    return {
        "id": "command-validate",
        "cluster_id": "cluster-prod",
        "namespace": "payments",
        "action": "validate_kubernetes_change",
        "parameters": {
            "change": {
                "target": {
                    "api_version": "apps/v1", "kind": kind, "namespace": "payments", "name": "checkout-api",
                },
                "operation": operation,
                "payload": payload,
                "post_checks": [{"type": "exists" if operation != "delete" else "absent"}],
            },
        },
    }


def _live() -> dict[str, object]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "checkout-api", "namespace": "payments", "uid": "uid-1", "resourceVersion": "41",
        },
        "spec": {"replicas": 3},
    }


def test_patch_freezes_identity_version_and_relevant_old_values_before_server_dry_run() -> None:
    final = _live()
    final["metadata"]["resourceVersion"] = "42"  # type: ignore[index]
    final["spec"]["replicas"] = 5  # type: ignore[index]
    client = FakeDynamicClient(_live(), final)

    result = execute_validation_command(
        _command("patch", [{"op": "replace", "path": "/spec/replicas", "value": 5}]),
        connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"},
        client_factory=lambda: client,
    )

    assert result["status"] == "succeeded"
    payload = result["validation"]["canonical_change"]["payload"]  # type: ignore[index]
    assert {tuple(item.items()) for item in payload[:2]} == {
        (("op", "test"), ("path", "/metadata/uid"), ("value", "uid-1")),
        (("op", "test"), ("path", "/metadata/resourceVersion"), ("value", "41")),
    }
    assert {"op": "test", "path": "/spec/replicas", "value": 3} in payload
    assert client.calls[-1][0] == "patch"
    assert client.calls[-1][1]["dry_run"] == "All"
    assert client.calls[-1][1]["content_type"] == "application/json-patch+json"
    assert result["validation"]["dry_run"]["diff"] == [  # type: ignore[index]
        {"op": "replace", "path": "/spec/replicas", "before": 3, "after": 5},
    ]


def test_create_requires_absence_and_sends_complete_object_to_server_dry_run() -> None:
    body = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "checkout-api", "namespace": "payments"},
        "spec": {"replicas": 1},
    }
    final = deepcopy(body)
    final["spec"]["strategy"] = {"type": "RollingUpdate"}  # type: ignore[index]
    client = FakeDynamicClient(None, final)

    result = execute_validation_command(
        _command("create", body),
        connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, client_factory=lambda: client,
    )

    assert result["status"] == "succeeded"
    assert client.calls == [
        ("get", {"name": "checkout-api", "namespace": "payments"}),
        ("create", {"body": body, "namespace": "payments", "dry_run": "All"}),
    ]
    assert result["validation"]["live"] == {"exists": False, "uid": None, "resource_version": None}  # type: ignore[index]


def test_secure_input_is_decrypted_only_for_dry_run_and_result_is_redacted(tmp_path: Path) -> None:
    key = b"k" * 32
    key_path = tmp_path / "change.key"
    key_path.write_bytes(base64.urlsafe_b64encode(key))
    placeholder = secure_input_placeholder("opaque-1")
    ciphertext, digest, fingerprint = encrypt_secure_input(
        input_id="opaque-1", key_name="api.token", value="must-never-persist",
        key=key, nonce=b"n" * 12,
    )
    ref = {
        "id": "opaque-1", "key_name": "api.token", "placeholder": placeholder,
        "sha256": digest, "key_fingerprint": fingerprint,
        "nonce": base64.urlsafe_b64encode(b"n" * 12).decode(),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode(),
    }
    body = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "api-key", "namespace": "payments"},
        "stringData": {"token": placeholder},
    }
    client = FakeDynamicClient(None, {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "api-key", "namespace": "payments"},
        "data": {"token": base64.b64encode(b"must-never-persist").decode("ascii")},
    })
    client.resources.resource.api_version = "v1"
    client.resources.resource.kind = "Secret"
    client.resources.resource.name = "secrets"
    command = {
        "id": "command-sensitive", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "validate_kubernetes_change",
        "parameters": {
            "change": {
                "target": {"api_version": "v1", "kind": "Secret", "namespace": "payments", "name": "api-key"},
                "operation": "create", "payload": body, "post_checks": [{"type": "exists"}],
                "rollback": {"status": "available"},
            },
            "secure_inputs": [ref],
        },
    }

    result = execute_validation_command(
        command, connector_cluster_id="cluster-prod", allowed_namespaces={"*"},
        client_factory=lambda: client, secure_input_key_path=key_path,
    )

    assert result["status"] == "succeeded"
    assert client.calls[-1][1]["body"]["stringData"]["token"] == "must-never-persist"
    assert result["validation"]["canonical_change"]["payload"] == body  # type: ignore[index]
    assert "must-never-persist" not in json.dumps(result)
    diff = result["validation"]["dry_run"]["diff"]  # type: ignore[index]
    assert diff[0]["after"]["data"]["token"] == {  # type: ignore[index]
        "secure_input": {"key_name": "api.token", "sha256": value_hash("must-never-persist")},
    }
    assert key_fingerprint(key) == ref["key_fingerprint"]

    key_path.write_bytes(base64.urlsafe_b64encode(b"r" * 32))
    untouched = FakeDynamicClient(None, None)
    untouched.resources.resource.api_version = "v1"
    untouched.resources.resource.kind = "Secret"
    lost = execute_validation_command(
        command, connector_cluster_id="cluster-prod", allowed_namespaces={"*"},
        client_factory=lambda: untouched, secure_input_key_path=key_path,
    )
    assert (lost["status"], lost["error_code"]) == ("rejected", "secure_input_unavailable")
    assert untouched.calls == []


def test_sensitive_old_value_is_rejected_instead_of_returned_from_validation(tmp_path: Path) -> None:
    key = b"k" * 32
    key_path = tmp_path / "change.key"
    key_path.write_bytes(base64.urlsafe_b64encode(key))
    placeholder = secure_input_placeholder("opaque-1")
    ciphertext, digest, fingerprint = encrypt_secure_input(
        input_id="opaque-1", key_name="api.token", value="new-secret",
        key=key, nonce=b"n" * 12,
    )
    live = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {
            "name": "api-key", "namespace": "payments", "uid": "uid-1", "resourceVersion": "1",
        },
        "stringData": {"token": "old-secret"},
    }
    client = FakeDynamicClient(live, live)
    client.resources.resource.api_version = "v1"
    client.resources.resource.kind = "Secret"
    client.resources.resource.name = "secrets"
    command = {
        "id": "command-sensitive", "cluster_id": "cluster-prod", "namespace": "payments",
        "action": "validate_kubernetes_change",
        "parameters": {
            "change": {
                "target": {"api_version": "v1", "kind": "Secret", "namespace": "payments", "name": "api-key"},
                "operation": "patch",
                "payload": [{"op": "replace", "path": "/stringData/token", "value": placeholder}],
                "post_checks": [{"type": "exists"}],
                "rollback": {"status": "unavailable", "concrete_loss": "The previous token cannot be retained."},
            },
            "secure_inputs": [{
                "id": "opaque-1", "key_name": "api.token", "placeholder": placeholder,
                "sha256": digest, "key_fingerprint": fingerprint,
                "nonce": base64.urlsafe_b64encode(b"n" * 12).decode(),
                "ciphertext": base64.urlsafe_b64encode(ciphertext).decode(),
            }],
        },
    }

    result = execute_validation_command(
        command, connector_cluster_id="cluster-prod", allowed_namespaces={"*"},
        client_factory=lambda: client, secure_input_key_path=key_path,
    )

    assert (result["status"], result["error_code"]) == (
        "rejected", "secure_input_precondition_unavailable",
    )
    assert "old-secret" not in json.dumps(result)
    assert [call[0] for call in client.calls] == ["get"]


def test_delete_builds_delete_options_with_uid_and_resource_version_preconditions() -> None:
    client = FakeDynamicClient(_live(), None)

    result = execute_validation_command(
        _command("delete", {"propagation_policy": "Foreground"}),
        connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, client_factory=lambda: client,
    )

    assert result["status"] == "succeeded"
    body = client.calls[-1][1]["body"]
    assert body == {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "propagationPolicy": "Foreground",
        "preconditions": {"uid": "uid-1", "resourceVersion": "41"},
        "dryRun": ["All"],
    }
    assert result["validation"]["canonical_change"]["payload"] == {  # type: ignore[index]
        "apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground",
        "preconditions": {"uid": "uid-1", "resourceVersion": "41"},
    }
    assert client.calls[-1][1]["dry_run"] == "All"
    assert result["validation"]["dry_run"]["diff"] == [  # type: ignore[index]
        {"op": "remove", "path": "", "before": _live(), "after": None},
    ]


def test_adapter_rejects_cluster_namespace_mismatch_and_subresource_discovery() -> None:
    client = FakeDynamicClient(_live(), _live())
    client.resources.resource.name = "deployments/status"

    rejected = execute_validation_command(
        _command("patch", [{"op": "replace", "path": "/spec/replicas", "value": 5}]),
        connector_cluster_id="another-cluster",
        allowed_namespaces={"payments"}, client_factory=lambda: client,
    )
    subresource = execute_validation_command(
        _command("patch", [{"op": "replace", "path": "/spec/replicas", "value": 5}]),
        connector_cluster_id="cluster-prod",
        allowed_namespaces={"payments"}, client_factory=lambda: client,
    )

    assert (rejected["status"], rejected["error_code"]) == ("rejected", "identity_mismatch")
    assert (subresource["status"], subresource["error_code"]) == ("rejected", "subresource_forbidden")


def test_server_policy_error_does_not_transport_raw_api_response() -> None:
    client = FakeDynamicClient(_live(), _live())

    def reject(*_args, **_kwargs):
        raise Unprocessable()

    client.patch = reject  # type: ignore[method-assign]
    result = execute_validation_command(
        _command("patch", [{"op": "replace", "path": "/spec/replicas", "value": 5}]),
        connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, client_factory=lambda: client,
    )

    assert result == {
        "status": "rejected",
        "error_code": "server_dry_run_rejected",
        "error_message": "API Server rejected the dry-run request",
    }


def test_secret_delete_diff_redacts_every_data_value_regardless_of_key_name() -> None:
    live = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "checkout-api", "namespace": "payments", "uid": "uid-1", "resourceVersion": "41"},
        "data": {"opaque": "dG9wLXNlY3JldA=="},
    }
    client = FakeDynamicClient(live, None)
    client.resources.resource.api_version = "v1"
    client.resources.resource.kind = "Secret"
    result = execute_validation_command(
        {
            **_command("delete", {"propagation_policy": "Foreground"}, kind="Secret"),
            "parameters": {"change": {
                "target": {"api_version": "v1", "kind": "Secret", "namespace": "payments", "name": "checkout-api"},
                "operation": "delete", "payload": {"propagation_policy": "Foreground"},
                "post_checks": [{"type": "absent"}],
            }},
        },
        connector_cluster_id="cluster-prod",
        allowed_namespaces={"*"}, client_factory=lambda: client,
    )

    encoded = json.dumps(result)
    assert result["status"] == "succeeded"
    assert "dG9wLXNlY3JldA==" not in encoded
    assert '"redacted": true' in encoded


def test_live_workload_diff_redacts_sensitive_named_environment_values() -> None:
    live = _live()
    live["spec"] = {
        "template": {"spec": {"containers": [{
            "name": "api", "env": [{"name": "DATABASE_PASSWORD", "value": "hunter2"}],
        }]}}
    }
    client = FakeDynamicClient(live, None)
    result = execute_validation_command(
        _command("delete", {"propagation_policy": "Foreground"}),
        connector_cluster_id="cluster-prod", allowed_namespaces={"*"}, client_factory=lambda: client,
    )

    encoded = json.dumps(result)
    assert result["status"] == "succeeded"
    assert "hunter2" not in encoded
    assert '"redacted": true' in encoded


def test_worker_transports_only_bounded_redacted_validation_result() -> None:
    result = execute_kubernetes_validation(
        _command("patch", [{"op": "replace", "path": "/spec/replicas", "value": 5}]),
        cluster_id="cluster-prod",
        allowed_namespaces={"*"},
        executor=lambda *_args, **_kwargs: {
            "status": "succeeded",
            "validation": {"dry_run": {"diff": [], "hash": "a" * 64}},
        },
    )

    assert result == {
        "status": "succeeded",
        "stdout": '{"dry_run":{"diff":[],"hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}',
        "stderr": "",
        "exit_code": 0,
        "truncated": False,
        "error_code": None,
        "error_message": None,
    }


def test_durable_worker_dispatches_validation_without_mutation_executor(tmp_path, monkeypatch) -> None:
    command = {
        **_command("patch", [{"op": "replace", "path": "/spec/replicas", "value": 5}]),
        "status": "leased", "attempt_count": 0, "lease_id": "lease-1",
        "lease_expires_at": 200.0, "created_at": 100.0, "result": None,
        "frozen_action": None, "scale_replica_bounds": None, "rollback_plan": None,
        "execution_grant_id": None, "execution_grant_expires_at": None, "action_hash": None,
    }
    paths: list[str] = []

    def post(_url, path, _payload, _credential, **_kwargs):
        paths.append(path)
        return (200, {"command": command}) if path.endswith("/poll") else (200, {})

    monkeypatch.setattr(command_worker, "_post_json", post)
    adapter_calls: list[str] = []
    journal = ConnectorCommandJournal(tmp_path / "connector.db")

    assert command_worker.run_command_cycle(
        "https://gateway.example",
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        credential="credential",
        allowed_namespaces={"*"},
        journal=journal,
        wait_seconds=0,
        validation_executor=lambda *_args, **_kwargs: adapter_calls.append("validate") or {
            "status": "succeeded", "validation": {"dry_run": {"diff": [], "hash": "a" * 64}},
        },
        mutation_executor=lambda *_args, **_kwargs: pytest.fail("mutation executor must not run"),
    )

    assert adapter_calls == ["validate"]
    assert paths == [
        "/api/v1/connectors/commands/poll",
        "/api/v1/connectors/commands/command-validate/start",
        "/api/v1/connectors/commands/command-validate/result",
    ]
    assert journal.unreported_results() == []


@pytest.mark.skipif(
    os.getenv("AIOPS_RUN_KUBERNETES_INTEGRATION") != "1",
    reason="requires an explicit real Kubernetes API Server",
)
def test_real_api_server_create_dry_run_does_not_persist_object() -> None:
    name = f"aiops-k02-dryrun-{uuid.uuid4().hex[:12]}"
    command = {
        "id": "command-real-dry-run",
        "cluster_id": "cluster-real",
        "namespace": "default",
        "action": "validate_kubernetes_change",
        "parameters": {
            "change": {
                "target": {"api_version": "v1", "kind": "ConfigMap", "namespace": "default", "name": name},
                "operation": "create",
                "payload": {
                    "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "default"},
                    "data": {"proof": "server-side-dry-run"},
                },
                "post_checks": [{"type": "exists"}],
            },
        },
    }

    result = execute_validation_command(
        command,
        connector_cluster_id="cluster-real",
        allowed_namespaces={"*"},
    )

    assert result["status"] == "succeeded", result
    assert result["validation"]["dry_run"]["diff"][0]["op"] == "add"  # type: ignore[index]
    repeated = execute_validation_command(
        command,
        connector_cluster_id="cluster-real",
        allowed_namespaces={"*"},
    )
    assert repeated["status"] == "succeeded", repeated


@pytest.mark.skipif(
    os.getenv("AIOPS_RUN_KUBERNETES_INTEGRATION") != "1",
    reason="requires an explicit real Kubernetes API Server",
)
def test_real_api_server_patch_and_delete_dry_runs_leave_existing_object_unchanged() -> None:
    annotation = f"aiops.io/dry-run-{uuid.uuid4().hex[:12]}"
    target = {"api_version": "v1", "kind": "ConfigMap", "namespace": "default", "name": "kube-root-ca.crt"}

    def validate(operation: str, payload: object, post_check: dict[str, object]) -> dict[str, object]:
        return execute_validation_command(
            {
                "id": f"command-real-{operation}", "cluster_id": "cluster-real", "namespace": "default",
                "action": "validate_kubernetes_change",
                "parameters": {"change": {
                    "target": target, "operation": operation, "payload": payload, "post_checks": [post_check],
                }},
            },
            connector_cluster_id="cluster-real", allowed_namespaces={"*"},
        )

    patch = validate(
        "patch",
        [{"op": "add", "path": f"/metadata/annotations/{annotation.replace('/', '~1')}", "value": "proof"}],
        {"type": "exists"},
    )
    repeated_patch = validate(
        "patch",
        [{"op": "add", "path": f"/metadata/annotations/{annotation.replace('/', '~1')}", "value": "proof"}],
        {"type": "exists"},
    )
    deleted = validate("delete", {"propagation_policy": "Foreground"}, {"type": "absent"})
    repeated_delete = validate("delete", {"propagation_policy": "Foreground"}, {"type": "absent"})

    assert patch["status"] == "succeeded", patch
    assert repeated_patch["status"] == "succeeded", repeated_patch
    assert deleted["status"] == "succeeded", deleted
    assert repeated_delete["status"] == "succeeded", repeated_delete
    live_identities = [
        item["validation"]["live"]  # type: ignore[index]
        for item in (patch, repeated_patch, deleted, repeated_delete)
    ]
    assert live_identities == [live_identities[0]] * 4
