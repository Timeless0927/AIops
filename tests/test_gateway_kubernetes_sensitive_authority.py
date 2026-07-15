"""K06 classification of changes that require Cluster Change Authority."""

from __future__ import annotations

import pytest

from apps.aiops_k8s_gateway.kubernetes_change_authorities import (
    requires_cluster_change_authority,
)


@pytest.mark.parametrize(
    ("target", "rollback"),
    [
        ({"api_version": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "namespace": "payments", "name": "ops"}, None),
        ({"api_version": "v1", "kind": "ServiceAccount", "namespace": "payments", "name": "runner"}, None),
        ({"api_version": "apps/v1", "kind": "Deployment", "namespace": "kube-system", "name": "dns"}, None),
        ({"api_version": "apps/v1", "kind": "Deployment", "namespace": "payments", "name": "api"}, {"status": "unavailable", "concrete_loss": "runtime identity is lost"}),
    ],
)
def test_sensitive_and_irreversible_changes_require_cluster_authority(
    target: dict[str, object], rollback: dict[str, object] | None,
) -> None:
    change: dict[str, object] = {"target": target}
    if rollback is not None:
        change["rollback"] = rollback
    assert requires_cluster_change_authority([change]) is True


@pytest.mark.parametrize("payload", [
    [{
        "op": "replace", "path": "/spec/template/spec/serviceAccountName",
        "value": "privileged-runner",
    }],
    {
        "apiVersion": "v1", "kind": "Pod",
        "spec": {"volumes": [{"name": "host", "hostPath": {"path": "/etc/kubernetes"}}]},
    },
    [{
        "op": "add",
        "path": "/spec/template/spec/containers/0/securityContext/capabilities/add/-",
        "value": "SYS_ADMIN",
    }],
])
def test_privileged_workload_effect_requires_cluster_authority(payload: object) -> None:
    assert requires_cluster_change_authority([{
        "target": {
            "api_version": "apps/v1", "kind": "Deployment",
            "namespace": "payments", "name": "api",
        },
        "operation": "patch", "payload": payload,
    }]) is True


def test_controlled_restart_with_unavailable_pod_identity_uses_namespace_authority() -> None:
    assert requires_cluster_change_authority([{
        "target": {
            "api_version": "apps/v1", "kind": "Deployment",
            "namespace": "aiops-verification", "name": "verification-api",
        },
        "operation": "patch",
        "payload": [{
            "op": "add",
            "path": "/spec/template/metadata/annotations/aiops.dev~1verification-run-id",
            "value": "c917e13e-b4f0-4c49-81e8-388cf758571b",
        }],
        "rollback": {
            "status": "unavailable",
            "concrete_loss": "A rollout cannot restore the previous Pod identities.",
        },
    }]) is False
