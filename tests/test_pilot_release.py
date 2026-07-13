from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

import yaml


PILOT = Path("deploy/k8s/pilot")
STATEFUL_CLAIMS = {
    "aiops-gateway": ("aiops-gateway-data", "5Gi"),
    "aiops-diagnosis": ("aiops-diagnosis-data", "5Gi"),
    "aiops-connector": ("aiops-connector-data", "1Gi"),
    "aiops-notification": ("aiops-notification-data", "1Gi"),
}


@lru_cache
def _rendered_text() -> str:
    return subprocess.run(
        ["kubectl", "kustomize", str(PILOT)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _resources() -> dict[tuple[str, str], dict]:
    documents = [doc for doc in yaml.safe_load_all(_rendered_text()) if doc]
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in documents}


def test_pilot_is_one_local_aiops_system_entrypoint() -> None:
    kustomization = yaml.safe_load((PILOT / "kustomization.yaml").read_text(encoding="utf-8"))
    assert kustomization["namespace"] == "aiops-system"
    assert all("://" not in resource for resource in kustomization["resources"])

    resources = _resources()
    assert resources[("Namespace", "aiops-system")]["metadata"]["name"] == "aiops-system"
    assert not any(kind in {"Ingress", "ServiceMonitor", "PrometheusRule"} for kind, _ in resources)
    for (kind, _name), resource in resources.items():
        if kind not in {"Namespace", "ClusterRole", "ClusterRoleBinding"}:
            assert resource["metadata"]["namespace"] == "aiops-system"


def test_every_workload_uses_an_immutable_non_placeholder_image() -> None:
    resources = _resources()
    workload_names = {
        name
        for (kind, name), resource in resources.items()
        if kind in {"Deployment", "DaemonSet", "StatefulSet", "Job"}
        for container in resource["spec"]["template"]["spec"].get("containers", [])
    }
    assert workload_names == {
        "aiops-bootstrap",
        "aiops-console",
        "aiops-gateway",
        "aiops-connector",
        "aiops-diagnosis",
        "aiops-notification",
        "aiops-mcp-prometheus",
        "aiops-mcp-loki",
        "aiops-mcp-topology",
    }
    for (kind, _name), resource in resources.items():
        if kind not in {"Deployment", "DaemonSet", "StatefulSet", "Job"}:
            continue
        for container in resource["spec"]["template"]["spec"].get("containers", []):
            image = container["image"]
            assert re.search(r"@sha256:[0-9a-f]{64}$", image)
            assert not image.endswith("sha256:" + "0" * 64)


def test_stateful_processes_have_one_replica_and_owned_rwo_claim() -> None:
    resources = _resources()
    assert {
        name: claim["spec"]["resources"]["requests"]["storage"]
        for (kind, name), claim in resources.items()
        if kind == "PersistentVolumeClaim"
    } == {claim: size for claim, size in STATEFUL_CLAIMS.values()}

    for deployment_name, (claim_name, _size) in STATEFUL_CLAIMS.items():
        deployment = resources[("Deployment", deployment_name)]
        assert deployment["spec"]["replicas"] == 1
        claims = [
            volume["persistentVolumeClaim"]["claimName"]
            for volume in deployment["spec"]["template"]["spec"]["volumes"]
            if "persistentVolumeClaim" in volume
        ]
        assert claims == [claim_name]
        claim = resources[("PersistentVolumeClaim", claim_name)]
        assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
        assert "storageClassName" not in claim["spec"]


def test_console_is_the_only_nodeport_and_proxies_same_origin_routes() -> None:
    resources = _resources()
    services = [resource for (kind, _), resource in resources.items() if kind == "Service"]
    node_ports = [service for service in services if service["spec"].get("type") == "NodePort"]
    assert [service["metadata"]["name"] for service in node_ports] == ["aiops-console"]
    assert node_ports[0]["spec"]["ports"] == [
        {"name": "http", "port": 80, "targetPort": "http", "nodePort": 30088}
    ]

    nginx = resources[("ConfigMap", "aiops-console-edge")]["data"]["default.conf"]
    assert "location ^~ /api/v1" in nginx
    assert "location ^~ /auth" in nginx
    assert "proxy_pass http://aiops-gateway:8080" in nginx
    assert "proxy_buffering off" in nginx
    console = resources[("Deployment", "aiops-console")]
    mounts = console["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert {
        "name": "edge-config",
        "mountPath": "/etc/nginx/conf.d/default.conf",
        "subPath": "default.conf",
        "readOnly": True,
    } in mounts


def test_bootstrap_secrets_are_required_by_their_consumers() -> None:
    resources = _resources()
    expected = {
        "aiops-gateway": {
            "aiops-runtime-secret": {
                "AIOPS_BOOTSTRAP_ADMIN_PASSWORD",
                "AIOPS_ALERTMANAGER_WEBHOOK_TOKEN",
            },
            "aiops-change-encryption": {"key"},
        },
        "aiops-connector": {"aiops-change-encryption": {"key"}},
        "aiops-diagnosis": {"aiops-model-encryption": {"key"}},
        "aiops-notification": {"aiops-notification-encryption": {"key"}},
    }
    for name, required_secrets in expected.items():
        pod = resources[("Deployment", name)]["spec"]["template"]["spec"]
        mounted = {
            volume["secret"]["secretName"]: {
                item["key"] for item in volume["secret"].get("items", [])
            }
            for volume in pod["volumes"]
            if "secret" in volume
        }
        assert required_secrets.items() <= mounted.items()
        assert all(not volume["secret"].get("optional", False) for volume in pod["volumes"] if "secret" in volume)
        readiness = " ".join(pod["containers"][0]["readinessProbe"]["exec"]["command"])
        for secret_name, keys in required_secrets.items():
            assert secret_name in readiness
            assert all(f".data.{key}" in readiness for key in keys)
        assert "/readyz" in readiness

    assert not any(kind == "Secret" for kind, _ in resources)
    assert ("Job", "aiops-bootstrap") in resources

    for name in {
        "aiops-connector",
        "aiops-diagnosis",
        "aiops-mcp-prometheus",
        "aiops-mcp-loki",
        "aiops-mcp-topology",
    }:
        env_from = resources[("Deployment", name)]["spec"]["template"]["spec"]["containers"][0]["envFrom"]
        assert {"secretRef": {"name": "aiops-runtime-secret", "optional": True}} not in env_from

    readiness_roles = {
        "aiops-gateway-secret-readiness": {"aiops-runtime-secret", "aiops-change-encryption"},
        "aiops-connector-secret-readiness": {"aiops-change-encryption"},
        "aiops-diagnosis-secret-readiness": {"aiops-model-encryption"},
        "aiops-notification-secret-readiness": {"aiops-notification-encryption"},
    }
    for role_name, secret_names in readiness_roles.items():
        role = resources[("Role", role_name)]
        assert role["rules"] == [
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "resourceNames": sorted(secret_names),
                "verbs": ["get"],
            }
        ]
        binding = resources[("RoleBinding", role_name)]
        assert binding["subjects"] == [
            {
                "kind": "ServiceAccount",
                "name": role_name.removesuffix("-secret-readiness"),
                "namespace": "aiops-system",
            }
        ]


def test_internal_service_identity_network_and_change_executor_stay_bounded() -> None:
    resources = _resources()
    internal = {
        "aiops-gateway",
        "aiops-diagnosis",
        "aiops-mcp-prometheus",
        "aiops-mcp-loki",
        "aiops-mcp-topology",
    }
    for name in internal:
        pod = resources[("Deployment", name)]["spec"]["template"]["spec"]
        projection = next(
            source["serviceAccountToken"]
            for volume in pod["volumes"]
            if volume["name"] == "internal-identity"
            for source in volume["projected"]["sources"]
        )
        assert projection["audience"] == "aiops-internal"

    assert {
        name for kind, name in resources if kind == "NetworkPolicy"
    } >= {
        "aiops-connector-internal",
        "aiops-diagnosis-internal",
        "aiops-notification-internal",
        "aiops-mcp-prometheus-internal",
        "aiops-mcp-loki-internal",
        "aiops-mcp-topology-internal",
    }
    connector_policy = resources[("NetworkPolicy", "aiops-connector-internal")]
    assert connector_policy["spec"]["ingress"] == []
    role = resources[("ClusterRole", "aiops-change-executor")]
    assert role["rules"] == [
        {
            "apiGroups": ["*"],
            "resources": ["*"],
            "verbs": ["get", "list", "watch", "create", "patch", "delete"],
        },
        {
            "nonResourceURLs": ["/api", "/api/*", "/apis", "/apis/*", "/openapi", "/openapi/*", "/version"],
            "verbs": ["get"],
        },
    ]
    binding = resources[("ClusterRoleBinding", "aiops-change-executor")]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "aiops-connector", "namespace": "aiops-system"}
    ]


def test_installation_readiness_does_not_create_integration_state() -> None:
    resources = _resources()
    config = resources[("ConfigMap", "aiops-runtime-config")]["data"]
    assert config["PROMETHEUS_URL"] == ""
    assert config["LOKI_URL"] == ""
    assert not any(key.startswith("FEISHU_") for key in config)
    assert {
        "AIOPS_MODEL_PROVIDER",
        "AIOPS_MODEL_NAME",
        "AIOPS_MODEL_BASE_URL",
        "AIOPS_NOTIFICATION_CHANNELS_JSON",
        "AIOPS_SRE_ADMIN_NAME",
        "AIOPS_SRE_ADMIN_OPEN_ID",
        "AIOPS_SRE_OPERATOR_NAME",
        "AIOPS_SRE_OPERATOR_OPEN_ID",
    }.isdisjoint(config)
    assert not any("replace_me" in value or "model-service" in value for value in config.values())
    assert not any("credential" in name.lower() for _kind, name in resources)
    for name in {
        "aiops-console",
        "aiops-gateway",
        "aiops-connector",
        "aiops-diagnosis",
        "aiops-notification",
        "aiops-mcp-prometheus",
        "aiops-mcp-loki",
        "aiops-mcp-topology",
    }:
        deployment = resources[("Deployment", name)]
        assert deployment["spec"]["template"]["spec"]["containers"][0].get("readinessProbe")


def test_render_is_stable_for_same_version_reapply_and_client_parses() -> None:
    first = _rendered_text()
    second = subprocess.run(
        ["kubectl", "kustomize", str(PILOT)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert second == first
    result = subprocess.run(
        ["kubectl", "apply", "--dry-run=client", "--validate=false", "-f", "-"],
        input=first,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_operator_instructions_keep_one_install_and_explicit_bootstrap_recheck() -> None:
    instructions = (PILOT / "README.md").read_text(encoding="utf-8")
    assert "kubectl apply -k deploy/k8s/pilot" in instructions
    assert "kubectl wait -n aiops-system --for=condition=complete job/aiops-bootstrap" in instructions
    assert "kubectl wait -n aiops-system --for=condition=Available deployment --all" in instructions
    assert "kubectl delete job -n aiops-system aiops-bootstrap" in instructions
    assert "AIOPS_BOOTSTRAP_ADMIN_PASSWORD" in instructions
