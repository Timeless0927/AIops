"""K02 canonical Kubernetes Change contract tests."""

from __future__ import annotations

import pytest

from aiops.contracts.kubernetes_change import (
    KubernetesChangeContractError,
    validate_draft_kubernetes_change,
)


def _target() -> dict[str, object]:
    return {
        "api_version": "apps/v1",
        "kind": "Deployment",
        "namespace": "payments",
        "name": "checkout-api",
    }


def test_patch_contract_accepts_only_rfc6902_and_structured_post_checks() -> None:
    change = validate_draft_kubernetes_change({
        "target": _target(),
        "operation": "patch",
        "payload": [
            {"op": "replace", "path": "/spec/replicas", "value": 5},
        ],
        "post_checks": [
            {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5},
            {"type": "condition", "condition_type": "Available", "status": "True"},
        ],
    })

    assert change["operation"] == "patch"
    assert change["payload"] == [{"op": "replace", "path": "/spec/replicas", "value": 5}]

    with pytest.raises(KubernetesChangeContractError, match="RFC 6902"):
        validate_draft_kubernetes_change({
            "target": _target(),
            "operation": "patch",
            "payload": {"spec": {"replicas": 5}},
            "post_checks": [{"type": "exists"}],
        })
    with pytest.raises(KubernetesChangeContractError, match="post-check"):
        validate_draft_kubernetes_change({
            "target": _target(),
            "operation": "patch",
            "payload": [{"op": "replace", "path": "/spec/replicas", "value": 5}],
            "post_checks": ["kubectl rollout status"],
        })


@pytest.mark.parametrize(
    "change",
    [
        {
            "target": {**_target(), "kind": "Deployment/status"},
            "operation": "delete",
            "payload": {"propagation_policy": "Foreground"},
            "post_checks": [{"type": "absent"}],
        },
        {
            "target": _target(),
            "operation": "shell",
            "payload": {"command": "kubectl delete deployment checkout-api"},
            "post_checks": [{"type": "absent"}],
        },
        {
            "target": _target(),
            "operation": "patch",
            "payload": [{"op": "move", "from": "/spec/a", "path": "/spec/b"}],
            "post_checks": [{"type": "exists"}],
        },
    ],
)
def test_contract_rejects_subresources_shell_and_non_deterministic_patch(change: dict[str, object]) -> None:
    with pytest.raises(KubernetesChangeContractError):
        validate_draft_kubernetes_change(change)


def test_create_and_delete_payloads_are_exactly_typed() -> None:
    created = validate_draft_kubernetes_change({
        "target": {
            "api_version": "batch/v1", "kind": "Job", "namespace": "payments", "name": "reindex",
        },
        "operation": "create",
        "payload": {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "reindex", "namespace": "payments"},
            "spec": {"template": {"spec": {"restartPolicy": "Never", "containers": []}}},
        },
        "post_checks": [{"type": "job_terminal", "outcome": "complete"}],
    })
    deleted = validate_draft_kubernetes_change({
        "target": _target(),
        "operation": "delete",
        "payload": {"propagation_policy": "Foreground"},
        "post_checks": [{"type": "absent"}],
    })

    assert created["payload"]["metadata"]["name"] == "reindex"  # type: ignore[index]
    assert deleted["payload"] == {"propagation_policy": "Foreground"}


def test_secret_plaintext_is_redirected_to_secure_input_before_connector_transport() -> None:
    with pytest.raises(KubernetesChangeContractError, match="Secure Input"):
        validate_draft_kubernetes_change({
            "target": {"api_version": "v1", "kind": "Secret", "namespace": "payments", "name": "api-key"},
            "operation": "create",
            "payload": {
                "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "api-key", "namespace": "payments"},
                "stringData": {"token": "plaintext"},
            },
            "post_checks": [{"type": "exists"}],
        })
