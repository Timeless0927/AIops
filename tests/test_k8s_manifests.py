from pathlib import Path

import yaml


def _docs(path: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(Path(path).read_text(encoding="utf-8")) if doc]


def test_deployment_manifest_references_split_service_images_and_health() -> None:
    deployments = {doc["metadata"]["name"]: doc for doc in _docs("deploy/k8s/deployment.yaml")}

    expected = {
        "aiops-gateway": ("gateway", "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:gateway-latest", 8080),
        "aiops-connector": ("connector", "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:connectors-latest", 8081),
        "aiops-hermes": ("hermes", "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:hermes-latest", 8082),
        "aiops-mcp-prometheus": (
            "mcp-prometheus",
            "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-prometheus-latest",
            8083,
        ),
        "aiops-mcp-loki": ("mcp-loki", "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-latest", 8084),
    }

    assert set(expected) <= set(deployments)
    for name, (container_name, image, port) in expected.items():
        pod_spec = deployments[name]["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        assert container["name"] == container_name
        assert container["image"] == image
        assert container["ports"][0]["containerPort"] == port
        assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
        assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
        assert {"configMapRef": {"name": "aiops-runtime-config"}} in container["envFrom"]

    assert deployments["aiops-connector"]["spec"]["template"]["spec"]["serviceAccountName"] == "aiops-connector"
    hermes_volume = deployments["aiops-hermes"]["spec"]["template"]["spec"]["volumes"][0]
    assert hermes_volume["persistentVolumeClaim"]["claimName"] == "aiops-hermes-data"


def test_configmap_contains_runtime_authorization_and_service_routing() -> None:
    configmap = yaml.safe_load(Path("deploy/k8s/configmap.yaml").read_text(encoding="utf-8"))
    data = configmap["data"]

    for key in (
        "AIOPS_SRE_ADMIN_NAME",
        "AIOPS_SRE_ADMIN_OPEN_ID",
        "AIOPS_SRE_OPERATOR_NAME",
        "AIOPS_SRE_OPERATOR_OPEN_ID",
        "AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK",
        "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC",
        "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS",
        "FEISHU_GROUP_POLICY",
        "FEISHU_ALLOWED_USERS",
        "HERMES_HOME",
        "HERMES_CONFIG",
        "AIOPS_DATA_DIR",
        "AIOPS_CONNECTOR_URL",
        "AIOPS_GATEWAY_URL",
        "PROMETHEUS_URL",
        "LOKI_URL",
    ):
        assert key in data

    assert data["HERMES_HOME"] == "/data/hermes"
    assert data["HERMES_CONFIG"] == "/data/hermes/config.yaml"
    assert data["AIOPS_DATA_DIR"] == "/data/aiops"


def test_service_manifest_exposes_split_service_ports() -> None:
    services = {doc["metadata"]["name"]: doc for doc in _docs("deploy/k8s/service.yaml")}

    assert services["aiops-gateway"]["spec"]["ports"][0]["port"] == 8080
    assert services["aiops-connector"]["spec"]["ports"][0]["port"] == 8081
    assert services["aiops-hermes"]["spec"]["ports"][0]["port"] == 8082
    assert services["aiops-mcp-prometheus"]["spec"]["ports"][0]["port"] == 8083
    assert services["aiops-mcp-loki"]["spec"]["ports"][0]["port"] == 8084


def test_kustomize_overlays_define_observability_profiles_and_images() -> None:
    bundled = yaml.safe_load(Path("deploy/k8s/overlays/dev-bundled/kustomization.yaml").read_text(encoding="utf-8"))
    external = yaml.safe_load(Path("deploy/k8s/overlays/dev-external/kustomization.yaml").read_text(encoding="utf-8"))
    disabled = yaml.safe_load(Path("deploy/k8s/overlays/dev-disabled/kustomization.yaml").read_text(encoding="utf-8"))

    assert bundled["namespace"] == "aiops-dev"
    assert "../../base" in bundled["resources"]
    assert "../../bundled" in bundled["resources"]
    assert "http://aiops-dev-prometheus:9090" in bundled["patches"][0]["patch"]
    assert "http://aiops-dev-loki:3100" in bundled["patches"][0]["patch"]
    assert "http://prometheus.monitoring.svc.cluster.local:9090" in external["patches"][0]["patch"]
    assert "http://loki.monitoring.svc.cluster.local:3100" in external["patches"][0]["patch"]
    assert 'path: /data/PROMETHEUS_URL\n  value: ""' in disabled["patches"][0]["patch"]
    assert 'path: /data/LOKI_URL\n  value: ""' in disabled["patches"][0]["patch"]


def test_base_kustomize_files_match_root_auditable_yaml() -> None:
    for name in ("configmap.yaml", "serviceaccount.yaml", "rbac.yaml", "pvc.yaml", "deployment.yaml", "service.yaml"):
        assert Path(f"deploy/k8s/base/{name}").read_text(encoding="utf-8") == Path(f"deploy/k8s/{name}").read_text(
            encoding="utf-8"
        )

    assert Path("deploy/k8s/bundled/observability-bundled.yaml").read_text(encoding="utf-8") == Path(
        "deploy/k8s/observability-bundled.yaml"
    ).read_text(encoding="utf-8")


def test_bundled_observability_manifest_contains_prometheus_loki_and_payment_api() -> None:
    docs = _docs("deploy/k8s/observability-bundled.yaml")
    by_name = {(doc["kind"], doc["metadata"]["name"]) for doc in docs}

    assert ("Deployment", "aiops-dev-prometheus") in by_name
    assert ("Service", "aiops-dev-prometheus") in by_name
    assert ("Deployment", "aiops-dev-loki") in by_name
    assert ("Service", "aiops-dev-loki") in by_name
    assert ("Deployment", "payment-api") in by_name
    assert ("Job", "aiops-loki-synthetic-log") in by_name
    bundled = Path("deploy/k8s/observability-bundled.yaml").read_text(encoding="utf-8")
    assert "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-prometheus-latest" in bundled
    assert "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-latest" in bundled
    assert "payment-api synthetic checkout error" in Path("deploy/k8s/observability-bundled.yaml").read_text(
        encoding="utf-8"
    )


def test_k8s_readme_mentions_profiles_image_digest_validation_and_retention() -> None:
    readme = Path("deploy/k8s/README.md").read_text(encoding="utf-8")
    assert "kubectl apply -k deploy/k8s/overlays/dev-bundled" in readme
    assert "kubectl apply -k deploy/k8s/overlays/dev-external" in readme
    assert "kubectl apply -k deploy/k8s/overlays/dev-disabled" in readme
    assert "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:<gateway-digest>" in readme
    assert "backend_unavailable" in readme
    assert "do not clean up the namespace after smoke" in readme
