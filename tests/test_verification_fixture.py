from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "verification/base"
RUN = ROOT / "verification/run"
IMAGE_PATTERN = re.compile(
    r"^registry\.cn-hangzhou\.aliyuncs\.com/timelessmao/aiops-verification@sha256:[0-9a-f]{64}$"
)
RESOURCES = {
    "requests": {"cpu": "50m", "memory": "64Mi"},
    "limits": {"cpu": "200m", "memory": "128Mi"},
}


def _render(path: Path) -> tuple[str, dict[tuple[str, str], dict]]:
    text = subprocess.run(
        ["kubectl", "kustomize", str(path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return text, {
        (document["kind"], document["metadata"]["name"]): document
        for document in yaml.safe_load_all(text)
        if document
    }


def test_optional_base_has_fixed_namespace_resources_and_restricted_workload() -> None:
    _text, resources = _render(BASE)

    assert set(resources) == {
        ("Namespace", "aiops-verification"),
        ("ResourceQuota", "verification-ceiling"),
        ("LimitRange", "verification-container-ceiling"),
        ("ServiceAccount", "verification-api"),
        ("Deployment", "verification-api"),
        ("Service", "verification-api"),
        ("NetworkPolicy", "verification-default-deny"),
        ("NetworkPolicy", "verification-api-ingress"),
        ("NetworkPolicy", "verification-trigger-egress"),
    }
    namespace = resources[("Namespace", "aiops-verification")]
    assert namespace["metadata"]["labels"] == {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "latest",
        "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/audit-version": "latest",
        "pod-security.kubernetes.io/warn": "restricted",
        "pod-security.kubernetes.io/warn-version": "latest",
    }
    assert all(
        resource["metadata"]["namespace"] == "aiops-verification"
        for (kind, _name), resource in resources.items()
        if kind != "Namespace"
    )

    quota = resources[("ResourceQuota", "verification-ceiling")]["spec"]["hard"]
    assert quota == {
        "pods": "5",
        "count/deployments.apps": "1",
        "count/jobs.batch": "1",
        "requests.cpu": "500m",
        "requests.memory": "512Mi",
        "limits.cpu": "1",
        "limits.memory": "1Gi",
    }
    ceiling = resources[("LimitRange", "verification-container-ceiling")]["spec"]["limits"]
    assert ceiling == [
        {
            "type": "Container",
            "defaultRequest": RESOURCES["requests"],
            "default": RESOURCES["limits"],
            "max": RESOURCES["limits"],
        }
    ]

    account = resources[("ServiceAccount", "verification-api")]
    assert account["automountServiceAccountToken"] is False
    deployment = resources[("Deployment", "verification-api")]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
    }
    assert deployment["spec"]["template"]["metadata"]["labels"][
        "aiops.dev/verification-deployment"
    ] == "verification-api"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "verification-api"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    container = pod["containers"][0]
    assert IMAGE_PATTERN.fullmatch(container["image"])
    assert container["resources"] == RESOURCES
    assert container["ports"] == [
        {"name": "live-ready", "containerPort": 8080},
        {"name": "trigger", "containerPort": 8081},
        {"name": "metrics", "containerPort": 9090},
    ]
    assert container["livenessProbe"]["httpGet"] == {"path": "/livez", "port": "live-ready"}
    assert container["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": "live-ready"}
    assert container["env"] == [
        {
            "name": "AIOPS_VERIFICATION_RUN_ID",
            "valueFrom": {
                "fieldRef": {
                    "fieldPath": "metadata.annotations['aiops.dev/verification-run-id']"
                }
            },
        }
    ]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert resources[("Service", "verification-api")]["spec"]["publishNotReadyAddresses"] is True
def test_network_policy_limits_trigger_metrics_dns_and_all_app_egress() -> None:
    _text, resources = _render(BASE)
    default_deny = resources[("NetworkPolicy", "verification-default-deny")]["spec"]
    assert default_deny == {
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
        "ingress": [],
        "egress": [],
    }
    ingress = resources[("NetworkPolicy", "verification-api-ingress")]["spec"]
    assert ingress["ingress"][0]["ports"] == [
        {"protocol": "TCP", "port": 8080},
        {"protocol": "TCP", "port": 8081},
    ]
    assert ingress["ingress"][0]["from"] == [
        {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "trigger"}}}
    ]
    assert ingress["ingress"][1]["ports"] == [{"protocol": "TCP", "port": 9090}]
    assert ingress["ingress"][1]["from"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "aiops-system"}
            },
            "podSelector": {
                "matchLabels": {"app.kubernetes.io/name": "aiops-prometheus"}
            },
        }
    ]
    egress = resources[("NetworkPolicy", "verification-trigger-egress")]["spec"]["egress"]
    assert len(egress) == 1
    assert egress[0]["ports"] == [
        {"protocol": "TCP", "port": 8081},
        {"protocol": "TCP", "port": 8080},
    ]


def test_run_is_only_a_bounded_trigger_job_using_the_controller_uid() -> None:
    _base_text, base = _render(BASE)
    _run_text, resources = _render(RUN)

    assert set(resources) == {("Job", "verification-trigger")}
    job = resources[("Job", "verification-trigger")]
    assert job["metadata"]["namespace"] == "aiops-verification"
    assert job["spec"]["activeDeadlineSeconds"] == 120
    assert job["spec"]["backoffLimit"] == 3
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "OnFailure"
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is True
    assert pod["serviceAccountName"] == "verification-api"
    container = pod["containers"][0]
    base_image = base[("Deployment", "verification-api")]["spec"]["template"]["spec"][
        "containers"
    ][0]["image"]
    assert container["image"] == base_image
    assert IMAGE_PATTERN.fullmatch(container["image"])
    assert container["args"] == ["trigger"]
    assert container["resources"] == RESOURCES
    env = {item["name"]: item for item in container["env"]}
    assert env["AIOPS_VERIFICATION_RUN_ID"]["valueFrom"]["fieldRef"]["fieldPath"] == (
        "metadata.labels['batch.kubernetes.io/controller-uid']"
    )
    assert env["AIOPS_VERIFICATION_RUN_ID_LEGACY"]["valueFrom"]["fieldRef"]["fieldPath"] == (
        "metadata.labels['controller-uid']"
    )
    assert set(env) == {"AIOPS_VERIFICATION_RUN_ID", "AIOPS_VERIFICATION_RUN_ID_LEGACY"}


def test_overlays_render_for_client_apply_and_stay_out_of_default_install() -> None:
    for path in (BASE, RUN):
        text, _resources = _render(path)
        result = subprocess.run(
            ["kubectl", "apply", "--dry-run=client", "--validate=false", "-f", "-"],
            cwd=ROOT,
            input=text,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        kustomization = yaml.safe_load((path / "kustomization.yaml").read_text(encoding="utf-8"))
        assert all("://" not in resource for resource in kustomization["resources"])

    pilot = yaml.safe_load((ROOT / "deploy/k8s/pilot/kustomization.yaml").read_text())
    assert not any("verification" in resource for resource in pilot["resources"])
    instructions = (ROOT / "verification/README.md").read_text(encoding="utf-8")
    for command in (
        "kubectl apply -k verification/base",
        "kubectl apply -k verification/run",
        "kubectl delete -k verification/run --ignore-not-found",
        "kubectl delete -k verification/base --ignore-not-found",
    ):
        assert command in instructions
    assert "aiops.dev/verification-run-id" in instructions


def test_verification_image_stage_contains_only_the_fixture_module() -> None:
    dockerfile = (ROOT / "Dockerfile.aiops").read_text(encoding="utf-8")
    stage = dockerfile.split("FROM python:3.11-slim AS verification", 1)[1].split(
        "FROM base AS diagnosis", 1
    )[0]

    assert "COPY verification_service /app/verification_service" in stage
    assert "USER 65532:65532" in stage
    assert 'ENTRYPOINT ["python3", "-m", "verification_service"]' in stage
    assert "COPY apps" not in stage
    assert "kubectl" not in stage
