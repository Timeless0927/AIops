from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

from bootstrap_service import BootstrapError, reconcile


class FakeCoreApi:
    def __init__(self, *, secrets: dict[str, dict] | None = None, marker: dict | None = None) -> None:
        self.secrets = secrets or {}
        self.marker = marker

    def get_secret(self, name: str) -> dict | None:
        return self.secrets.get(name)

    def create_secret(self, body: dict) -> bool:
        self.secrets[body["metadata"]["name"]] = body
        return True

    def get_marker(self) -> dict | None:
        return self.marker

    def create_marker(self, body: dict) -> bool:
        self.marker = body
        return True


class ConflictApi(FakeCoreApi):
    def create_secret(self, body: dict) -> bool:
        super().create_secret(body)
        return False

    def create_marker(self, body: dict) -> bool:
        super().create_marker(body)
        return False


def _encoded(value: bytes = b"x" * 32) -> str:
    return base64.b64encode(value).decode("ascii")


def test_first_run_creates_all_secrets_and_immutable_marker() -> None:
    api = FakeCoreApi()
    generated = iter(bytes([value]) * 32 for value in range(1, 6))

    reconcile(api, "aiops-system", random_bytes=lambda size: next(generated))

    assert set(api.secrets) == {
        "aiops-runtime-secret",
        "aiops-model-encryption",
        "aiops-notification-encryption",
        "aiops-change-encryption",
    }
    assert set(api.secrets["aiops-runtime-secret"]["data"]) == {
        "AIOPS_BOOTSTRAP_ADMIN_PASSWORD",
        "AIOPS_ALERTMANAGER_WEBHOOK_TOKEN",
    }
    for name in (
        "aiops-model-encryption",
        "aiops-notification-encryption",
        "aiops-change-encryption",
    ):
        assert len(base64.b64decode(api.secrets[name]["data"]["key"])) == 32
    assert api.marker["metadata"]["name"] == "aiops-bootstrap-state"
    assert api.marker["immutable"] is True


def test_reapply_keeps_existing_values() -> None:
    api = FakeCoreApi()
    reconcile(api, "aiops-system", random_bytes=lambda size: b"a" * size)
    original = {name: dict(secret["data"]) for name, secret in api.secrets.items()}

    reconcile(api, "aiops-system", random_bytes=lambda size: pytest.fail("must not rotate values"))

    assert {name: secret["data"] for name, secret in api.secrets.items()} == original


def test_rerun_completes_after_partial_failure_without_rotating_existing_secret() -> None:
    runtime = {
        "metadata": {"name": "aiops-runtime-secret"},
        "data": {
            "AIOPS_BOOTSTRAP_ADMIN_PASSWORD": _encoded(b"admin"),
            "AIOPS_ALERTMANAGER_WEBHOOK_TOKEN": _encoded(b"token"),
        },
    }
    api = FakeCoreApi(secrets={"aiops-runtime-secret": runtime})

    reconcile(api, "aiops-system", random_bytes=lambda size: b"b" * size)

    assert api.secrets["aiops-runtime-secret"] is runtime
    assert api.marker is not None


def test_existing_incomplete_secret_fails_without_filling_key() -> None:
    runtime = {
        "metadata": {"name": "aiops-runtime-secret"},
        "data": {"AIOPS_BOOTSTRAP_ADMIN_PASSWORD": _encoded()},
    }
    api = FakeCoreApi(secrets={"aiops-runtime-secret": runtime})

    with pytest.raises(BootstrapError, match="bootstrap_secret_incomplete"):
        reconcile(api, "aiops-system")

    assert set(runtime["data"]) == {"AIOPS_BOOTSTRAP_ADMIN_PASSWORD"}
    assert api.marker is None


def test_marker_makes_lost_secret_a_terminal_error() -> None:
    api = FakeCoreApi()
    reconcile(api, "aiops-system", random_bytes=lambda size: b"c" * size)
    del api.secrets["aiops-change-encryption"]

    with pytest.raises(BootstrapError, match="bootstrap_secret_lost"):
        reconcile(api, "aiops-system")

    assert "aiops-change-encryption" not in api.secrets


def test_concurrent_creator_is_accepted_after_conflict_reread() -> None:
    api = ConflictApi()

    reconcile(api, "aiops-system", random_bytes=lambda size: b"d" * size)

    assert len(api.secrets) == 4
    assert api.marker is not None


def test_malformed_completion_marker_is_rejected() -> None:
    api = FakeCoreApi(marker={"metadata": {"name": "aiops-bootstrap-state"}, "data": {"state": "complete"}})

    with pytest.raises(BootstrapError, match="bootstrap_marker_invalid"):
        reconcile(api, "aiops-system")


def test_bootstrap_job_has_minimal_namespaced_rbac() -> None:
    docs = [
        doc
        for doc in yaml.safe_load_all(Path("deploy/k8s/bootstrap/bootstrap.yaml").read_text(encoding="utf-8"))
        if doc
    ]
    resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}

    role = resources[("Role", "aiops-bootstrap")]
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": [
                "aiops-runtime-secret",
                "aiops-model-encryption",
                "aiops-notification-encryption",
                "aiops-change-encryption",
            ],
            "verbs": ["get"],
        },
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": ["aiops-bootstrap-state"],
            "verbs": ["get"],
        },
        {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["create"]},
    ]
    binding = resources[("RoleBinding", "aiops-bootstrap")]
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "aiops-bootstrap"}]

    job = resources[("Job", "aiops-bootstrap")]
    pod = job["spec"]["template"]["spec"]
    assert job["spec"]["backoffLimit"] == 0
    assert pod["restartPolicy"] == "Never"
    assert pod["serviceAccountName"] == "aiops-bootstrap"
    assert pod["containers"][0]["command"] == ["python", "-m", "bootstrap_service"]


def test_gateway_image_packages_bootstrap_runner_used_by_job() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text(encoding="utf-8")
    gateway_stage = dockerfile.split("FROM base AS diagnosis", 1)[0]

    assert "FROM base AS gateway" in gateway_stage
    assert "COPY bootstrap_service.py /app/bootstrap_service.py" in gateway_stage
