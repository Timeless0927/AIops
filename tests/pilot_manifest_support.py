"""Shared canonical Pilot render boundary for manifest tests."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

import yaml


PILOT = Path("deploy/k8s/pilot")


@lru_cache
def rendered_text() -> str:
    return subprocess.run(
        ["kubectl", "kustomize", str(PILOT)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@lru_cache
def resources() -> dict[tuple[str, str], dict]:
    return {
        (document["kind"], document["metadata"]["name"]): document
        for document in yaml.safe_load_all(rendered_text())
        if document
    }
