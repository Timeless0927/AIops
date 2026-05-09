from pathlib import Path


def test_deployment_manifest_references_required_runtime_components() -> None:
    deployment = Path("deploy/k8s/deployment.yaml").read_text(encoding="utf-8")
    assert "serviceAccountName: aiops-agent" in deployment
    assert "claimName: aiops-agent-data" in deployment
    assert "name: FEISHU_MAIN_CHAT_ID" in deployment
    assert "image: aiops-agent:latest" in deployment


def test_configmap_contains_runtime_authorization_config() -> None:
    configmap = Path("deploy/k8s/configmap.yaml").read_text(encoding="utf-8")
    assert "AIOPS_SRE_ADMIN_NAME" in configmap
    assert "AIOPS_SRE_ADMIN_OPEN_ID" in configmap
    assert "AIOPS_SRE_OPERATOR_NAME" in configmap
    assert "AIOPS_SRE_OPERATOR_OPEN_ID" in configmap
    assert "AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK" in configmap
    assert "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC" in configmap
    assert "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS" in configmap
    assert "FEISHU_GROUP_POLICY" in configmap
    assert "FEISHU_ALLOWED_USERS" in configmap


def test_service_manifest_exposes_webhook_port() -> None:
    service = Path("deploy/k8s/service.yaml").read_text(encoding="utf-8")
    assert "port: 8765" in service
    assert "targetPort: 8765" in service


def test_k8s_readme_mentions_build_apply_and_alertmanager_url() -> None:
    readme = Path("deploy/k8s/README.md").read_text(encoding="utf-8")
    assert "docker build -f Dockerfile.aiops" in readme
    assert "kubectl apply -f deploy/k8s" in readme
    assert "/webhooks/alertmanager" in readme
