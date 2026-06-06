"""Docker image workflow guardrails."""

from __future__ import annotations

from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docker-image.yml"


def test_image_smoke_runs_before_registry_login() -> None:
    """Local image smoke must leave evidence even when registry credentials fail."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build-aiops-image"]["steps"]
    step_names = [step["name"] for step in steps]

    smoke_build = step_names.index("Build local AIOps image for smoke")
    smoke_run = step_names.index("Run image import and facade smoke")
    registry_login = step_names.index("Log in to Aliyun Container Registry")
    publish_build = step_names.index("Build and push AIOps image")

    assert smoke_build < smoke_run < registry_login < publish_build
