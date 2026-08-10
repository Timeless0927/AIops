"""Docker image workflow guardrails."""

from __future__ import annotations

from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docker-image.yml"


def test_image_smoke_runs_before_registry_login() -> None:
    """Local image smoke must leave evidence even when registry credentials fail."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build-service-images"]["steps"]
    step_names = [step["name"] for step in steps]

    smoke_build = step_names.index("Build local service image for smoke")
    smoke_run = step_names.index("Run split service import smoke")
    registry_login = step_names.index("Log in to Aliyun Container Registry")
    publish_build = step_names.index("Build and push service image")

    assert smoke_build < smoke_run < registry_login < publish_build


def test_split_service_targets_publish_digests() -> None:
    """CI must build and publish each independently deployable service target."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["build-service-images"]
    services = {
        item["name"]: item
        for item in job["strategy"]["matrix"]["service"]
    }

    assert services["gateway"]["target"] == "gateway"
    assert services["verification"]["target"] == "verification"
    assert services["diagnosis"]["target"] == "diagnosis"
    assert services["connectors"]["target"] == "connectors"
    assert services["gateway"]["image"] == "timelessmao/aiops-gateway"
    assert services["verification"]["image"] == "timelessmao/aiops-verification"
    assert services["diagnosis"]["image"] == "timelessmao/aiops-diagnosis"
    assert services["connectors"]["image"] == "timelessmao/aiops-connectors"
    assert services["mcp-prometheus"]["image"] == "timelessmao/aiops-mcp-prometheus"
    assert services["mcp-loki"]["image"] == "timelessmao/aiops-mcp-loki"
    assert services["mcp-topology"]["target"] == "mcp-topology"
    assert services["mcp-topology"]["image"] == "timelessmao/aiops-mcp-topology"
    assert all(service["image"] != "timelessmao/hub" for service in services.values())
    assert services["gateway"]["tag-prefix"] == ""
    assert services["diagnosis"]["tag-prefix"] == ""
    assert services["connectors"]["tag-prefix"] == ""

    metadata_step = next(step for step in job["steps"] if step["name"] == "Extract image metadata")
    tag_config = metadata_step["with"]["tags"]
    assert "value=latest" in tag_config
    assert "prefix=${{ matrix.service.tag-prefix }}candidate-" in tag_config
    assert "prefix=${{ matrix.service.tag-prefix }}" in tag_config

    summary_step = next(step for step in job["steps"] if step["name"] == "Summarize published service image")
    assert summary_step["env"]["DIGEST"] == "${{ steps.build.outputs.digest }}"

    identity_step = next(step for step in job["steps"] if step["name"] == "Write published image identity")
    assert identity_step["env"]["SOURCE_SHA"] == "${{ github.sha }}"
    assert '"source_sha"' in identity_step["run"]
    artifact_step = next(step for step in job["steps"] if step["name"] == "Upload published image identity")
    assert artifact_step["uses"] == "actions/upload-artifact@v4"
    assert artifact_step["with"]["name"] == "published-image-${{ matrix.service.name }}"
    assert artifact_step["with"]["if-no-files-found"] == "error"


def test_compose_smoke_job_runs_after_service_builds() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["smoke-service-compose"]

    assert job["needs"] == "build-service-images"
    command = "\n".join(
        step.get("run", "") for step in job["steps"] if step["name"] == "Run split service compose smoke"
    )
    assert "docker compose -f docker-compose.services.yml up" in command
    assert "--attach-dependencies" in command
    assert "--exit-code-from smoke" in command
