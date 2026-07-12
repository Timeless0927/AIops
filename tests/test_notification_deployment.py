from __future__ import annotations

from pathlib import Path

import yaml


def _documents(path: str) -> list[dict[str, object]]:
    return [document for document in yaml.safe_load_all(Path(path).read_text()) if isinstance(document, dict)]


def test_notification_engine_has_independent_image_and_release_target() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text()
    workflow = Path(".github/workflows/docker-image.yml").read_text()

    assert "FROM base AS notification" in dockerfile
    assert "COPY notification_service /app/notification_service" in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint-notification.sh"]' in dockerfile
    assert "target: notification" in workflow
    assert "image: timelessmao/aiops-notification" in workflow


def test_notification_engine_owns_only_notification_pvc_and_gateway_only_ingress() -> None:
    deployments = {doc["metadata"]["name"]: doc for doc in _documents("deploy/k8s/base/deployment.yaml")}
    services = {doc["metadata"]["name"]: doc for doc in _documents("deploy/k8s/base/service.yaml")}
    accounts = {doc["metadata"]["name"]: doc for doc in _documents("deploy/k8s/base/serviceaccount.yaml")}
    claims = {doc["metadata"]["name"]: doc for doc in _documents("deploy/k8s/base/pvc.yaml")}
    policies = {doc["metadata"]["name"]: doc for doc in _documents("deploy/k8s/base/networkpolicy.yaml")}

    deployment = deployments["aiops-notification"]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "aiops-notification"
    assert services["aiops-notification"]["spec"]["ports"][0]["port"] == 8086
    assert "aiops-notification" in accounts
    assert "aiops-notification-data" in claims
    assert {volume["persistentVolumeClaim"]["claimName"] for volume in deployment["spec"]["template"]["spec"]["volumes"] if "persistentVolumeClaim" in volume} == {"aiops-notification-data"}
    key_volume = next(volume for volume in deployment["spec"]["template"]["spec"]["volumes"] if volume["name"] == "destination-encryption-key")
    assert key_volume["secret"] == {"secretName": "aiops-notification-encryption", "defaultMode": 0o400, "items": [{"key": "key", "path": "key"}]}
    key_mount = next(mount for mount in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] if mount["name"] == "destination-encryption-key")
    assert key_mount == {"name": "destination-encryption-key", "mountPath": "/var/run/secrets/aiops-notification", "readOnly": True}
    assert all("ENCRYPTION" not in item.get("name", "") for item in deployment["spec"]["template"]["spec"]["containers"][0].get("env", []))

    ingress = policies["aiops-notification-internal"]["spec"]["ingress"]
    assert ingress[0]["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "aiops-gateway"
    assert ingress[0]["ports"][0]["port"] == 8086


def test_gateway_is_configured_to_handoff_to_notification_engine() -> None:
    config = _documents("deploy/k8s/base/configmap.yaml")[0]["data"]
    assert config["AIOPS_NOTIFICATION_ENGINE_URL"] == "http://aiops-notification:8086"
    assert config["AIOPS_NOTIFICATION_HOST"] == "0.0.0.0"
