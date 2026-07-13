"""Console artifact build, routing, and promotion guardrails."""

import json
from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
BUILD_WORKFLOW = ROOT / ".github/workflows/console-image.yml"
PROMOTE_WORKFLOW = ROOT / ".github/workflows/promote-console.yml"
CONSOLE = ROOT / "apps/aiops_console_web"


def _workflow(path: Path) -> dict:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    workflow["on"] = workflow.pop(True, workflow.get("on"))
    return workflow


def test_console_ci_is_path_aware_and_publishes_commit_image() -> None:
    workflow = _workflow(BUILD_WORKFLOW)
    triggers = workflow["on"]
    console_paths = triggers["pull_request"]["paths"]
    assert "apps/aiops_console_web/**" in console_paths
    assert triggers["push"]["branches"] == ["main"]
    assert "apps/aiops_console_web/**" in triggers["push"]["paths"]

    steps = workflow["jobs"]["build-console"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "npm ci" in commands
    assert "npm test" in commands
    assert "npm run build" in commands
    workflow_text = BUILD_WORKFLOW.read_text(encoding="utf-8")
    assert "${{ github.sha }}" in workflow_text
    assert "push-by-digest=true" in workflow_text
    assert "tags:" not in workflow_text
    assert "build-args: VCS_REF=${{ github.sha }}" in workflow_text


def test_console_candidate_can_be_published_by_manual_dispatch() -> None:
    workflow = _workflow(BUILD_WORKFLOW)
    assert "workflow_dispatch" in workflow["on"]

    steps = workflow["jobs"]["build-console"]["steps"]
    login = next(step for step in steps if step["name"] == "Log in to Aliyun Container Registry")
    image = next(step for step in steps if step["name"] == "Build Console image")
    candidate = next(step for step in steps if step["name"] == "Register immutable candidate tag")
    summary = next(step for step in steps if step["name"] == "Summarize immutable image")
    assert login["if"] == "github.event_name != 'pull_request'"
    assert "push=${{ github.event_name != 'pull_request' }}" in image["with"]["outputs"]
    assert 'imagetools create --tag "${IMAGE}:candidate-${GITHUB_SHA}"' in candidate["run"]
    assert '"${IMAGE}@${DIGEST}"' in candidate["run"]
    assert summary["if"] == "github.event_name != 'pull_request'"


def test_console_image_serves_spa() -> None:
    dockerfile = (CONSOLE / "Dockerfile").read_text(encoding="utf-8")
    nginx = (CONSOLE / "nginx.conf").read_text(encoding="utf-8")

    assert "npm ci" in dockerfile
    assert "npm run build" in dockerfile
    assert "ARG VCS_REF" in dockerfile
    assert "LABEL org.opencontainers.image.revision=$VCS_REF" in dockerfile
    assert "COPY --from=build /app/dist" in dockerfile
    assert "try_files $uri $uri/ /index.html" in nginx


def test_release_identity_rejects_mismatch_and_invalid_digest() -> None:
    script = CONSOLE / "verify-release-identity.sh"
    source_sha = "a" * 40
    digest = "sha256:" + "b" * 64

    def verify(revision: str, candidate_digest: str = digest) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", script, source_sha, candidate_digest],
            input=json.dumps({"org.opencontainers.image.revision": revision}),
            text=True,
            capture_output=True,
            check=False,
        )

    assert verify(source_sha).returncode == 0
    assert verify("c" * 40).returncode != 0
    assert verify(source_sha, "latest").returncode != 0


def test_console_release_is_digest_pinned_and_only_manually_promoted() -> None:
    kustomization = yaml.safe_load(
        (ROOT / "deploy/k8s/console/kustomization.yaml").read_text(encoding="utf-8")
    )
    image = kustomization["images"][0]
    assert image["name"] == "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-console"
    assert image["digest"].startswith("sha256:")
    assert "newTag" not in image

    workflow = _workflow(PROMOTE_WORKFLOW)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    job = workflow["jobs"]["promote-console"]
    assert job["environment"] == "production"
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ inputs.source_sha }}"
    assert checkout["with"]["fetch-depth"] == 0
    validation = job["steps"][1]
    assert 'git merge-base --is-ancestor "${SOURCE_SHA}" origin/main' in validation["run"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "imagetools inspect" in commands
    assert "@${DIGEST}" in commands
    assert "verify-release-identity.sh" in commands
    assert "kustomize edit set image" in commands
    assert "kubectl apply -k" in commands


def test_console_manifest_stays_independent_from_gateway() -> None:
    deployment = yaml.safe_load(
        (ROOT / "deploy/k8s/console/deployment.yaml").read_text(encoding="utf-8")
    )
    service = yaml.safe_load(
        (ROOT / "deploy/k8s/console/service.yaml").read_text(encoding="utf-8")
    )
    ingresses = list(
        yaml.safe_load_all((ROOT / "deploy/k8s/console/ingress.yaml").read_text(encoding="utf-8"))
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert deployment["metadata"]["name"] == "aiops-console"
    assert container["name"] == "console"
    assert container["ports"][0]["containerPort"] == 8080
    assert service["spec"]["selector"] == {"app.kubernetes.io/name": "aiops-console"}

    routes = {
        path["path"]: path["backend"]["service"]["name"]
        for ingress in ingresses
        for path in ingress["spec"]["rules"][0]["http"]["paths"]
    }
    assert routes == {"/": "aiops-console", "/api/v1": "aiops-gateway", "/auth": "aiops-gateway"}
    gateway_annotations = ingresses[1]["metadata"]["annotations"]
    assert gateway_annotations["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
    assert gateway_annotations["nginx.ingress.kubernetes.io/proxy-read-timeout"] == "3600"
    assert all(ingress["spec"]["tls"][0]["secretName"] == "aiops-console-tls" for ingress in ingresses)
