"""Split service packaging guardrails."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _wait_json(url: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                payload = response.read().decode("utf-8")
                data = json.loads(payload)
                assert isinstance(data, dict)
                return data
        except Exception as exc:  # pragma: no cover - diagnostic loop
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"{url} did not become ready: {last_error}")


def _start(module: str, *args: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.Popen(
        [sys.executable, "-m", module, *args],
        cwd=ROOT,
        env=process_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_dockerfile_declares_independent_service_targets() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text(encoding="utf-8")

    assert "FROM base AS gateway" in dockerfile
    assert "FROM hermes-runtime AS hermes" in dockerfile
    assert "FROM base AS connectors" in dockerfile
    assert "FROM base AS mcp-prometheus" in dockerfile
    assert "FROM base AS mcp-loki" in dockerfile
    assert "FROM base AS hermes-smoke" in dockerfile
    assert "FROM hermes-runtime AS aiops" in dockerfile
    assert "pip install --retries 5 --timeout 120 -r /app/requirements-runtime.txt" in dockerfile
    assert 'pip install "hermes-agent[messaging,feishu] @ file:///tmp/hermes-agent"' in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint-gateway.sh"]' in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint-hermes.sh"]' in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint-connector.sh"]' in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint-mcp-prometheus.sh"]' in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint-mcp-loki.sh"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_gateway_image_defaults_include_alertmanager_handoff_env() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text(encoding="utf-8")

    assert "AIOPS_CONNECTOR_URL=http://connector:8081" in dockerfile
    assert "AIOPS_HERMES_URL=http://hermes:8082" in dockerfile
    assert "AIOPS_HERMES_DIAGNOSIS_PATH=/diagnosis/sessions" in dockerfile


def test_dockerfile_does_not_copy_entire_repository_into_service_images() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text(encoding="utf-8")

    assert "COPY . /app" not in dockerfile
    assert "COPY requirements-runtime.txt /app/requirements-runtime.txt" in dockerfile
    assert "COPY tests " not in dockerfile
    assert "COPY docs " not in dockerfile
    assert "COPY .git " not in dockerfile

    service_copy_boundaries = {
        "gateway": (
            "COPY apps/aiops_k8s_gateway /app/apps/aiops_k8s_gateway",
            "COPY toolsets/__init__.py toolsets/incident_store.py /app/toolsets/",
            "COPY deploy/entrypoint-gateway.sh /app/deploy/entrypoint-gateway.sh",
        ),
        "connectors": (
            "COPY apps/cluster_connector /app/apps/cluster_connector",
            "COPY toolsets/__init__.py toolsets/k8s_redact.py /app/toolsets/",
            "COPY deploy/entrypoint-connector.sh /app/deploy/entrypoint-connector.sh",
        ),
        "mcp-prometheus": (
            "COPY apps/mcp_prometheus /app/apps/mcp_prometheus",
            "COPY toolsets/__init__.py toolsets/query_guard.py toolsets/audit_log.py toolsets/prometheus_query.py /app/toolsets/",
            "COPY deploy/entrypoint-mcp-prometheus.sh /app/deploy/entrypoint-mcp-prometheus.sh",
        ),
        "mcp-loki": (
            "COPY apps/mcp_loki /app/apps/mcp_loki",
            "COPY toolsets/__init__.py toolsets/query_guard.py toolsets/audit_log.py toolsets/loki_query.py /app/toolsets/",
            "COPY deploy/entrypoint-mcp-loki.sh /app/deploy/entrypoint-mcp-loki.sh",
        ),
        "hermes": (
            "COPY hermes /app/hermes",
            "COPY deploy/entrypoint-hermes.sh /app/deploy/entrypoint-hermes.sh",
        ),
    }
    for expected_lines in service_copy_boundaries.values():
        for expected_line in expected_lines:
            assert expected_line in dockerfile


def test_dockerignore_excludes_non_runtime_build_context() -> None:
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

    for ignored in (
        ".git",
        ".github",
        ".agents",
        ".pytest_cache",
        "__pycache__",
        "**/__pycache__",
        "*.pyc",
        "tests",
        "docs",
        "deploy/k8s",
        "docker-compose.services.yml",
    ):
        assert ignored in dockerignore


def test_k8s_readme_documents_dockerfile_targets_and_copy_boundaries() -> None:
    readme = Path("deploy/k8s/README.md").read_text(encoding="utf-8")

    assert "Dockerfile path `Dockerfile.aiops`" in readme
    for target in ("`gateway`", "`connectors`", "`hermes`", "`mcp-prometheus`", "`mcp-loki`", "`aiops`"):
        assert target in readme
    assert "The Dockerfile must not use `COPY . /app`" in readme
    assert "tests/`, `docs/`, `deploy/k8s/`" in readme


def test_k8s_config_wires_gateway_to_hermes_handoff() -> None:
    for configmap_path in ("deploy/k8s/configmap.yaml", "deploy/k8s/base/configmap.yaml"):
        configmap = yaml.safe_load(Path(configmap_path).read_text(encoding="utf-8"))
        data = configmap["data"]
        assert data["AIOPS_HERMES_URL"] == "http://aiops-hermes:8082"
        assert data["AIOPS_HERMES_DIAGNOSIS_PATH"] == "/diagnosis/sessions"


def test_compose_smoke_wires_gateway_hermes_and_connectors() -> None:
    compose = yaml.safe_load(Path("docker-compose.services.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["gateway"]["build"]["target"] == "gateway"
    assert services["connector"]["build"]["target"] == "connectors"
    assert services["hermes"]["build"]["target"] == "hermes-smoke"
    assert services["smoke"]["build"]["target"] == "hermes-smoke"
    assert services["smoke"]["command"] == ["python3", "-m", "runtime.service_mesh_smoke"]
    assert services["gateway"]["environment"]["AIOPS_CONNECTOR_URL"] == "http://connector:8081"
    assert services["gateway"]["environment"]["AIOPS_HERMES_URL"] == "http://hermes:8082"
    assert services["hermes"]["environment"]["AIOPS_GATEWAY_URL"] == "http://gateway:8080"


def test_ci_matrix_builds_observability_mcp_targets() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/docker-image.yml").read_text(encoding="utf-8"))
    services = workflow["jobs"]["build-service-images"]["strategy"]["matrix"]["service"]
    by_name = {service["name"]: service for service in services}

    assert by_name["mcp-prometheus"]["target"] == "mcp-prometheus"
    assert by_name["mcp-prometheus"]["image"] == "timelessmao/aiops-mcp-prometheus"
    assert by_name["mcp-prometheus"]["tag-prefix"] == ""
    assert by_name["mcp-loki"]["target"] == "mcp-loki"
    assert by_name["mcp-loki"]["image"] == "timelessmao/aiops-mcp-loki"
    assert by_name["mcp-loki"]["tag-prefix"] == ""
    assert all(service["image"] != "timelessmao/hub" for service in services)

    smoke_step = next(
        step
        for step in workflow["jobs"]["build-service-images"]["steps"]
        if step.get("name") == "Run split service import smoke"
    )
    assert 'SERVICE_NAME="${{ matrix.service.name }}"' in smoke_step["run"]
    assert "-m runtime.service_image_smoke" in smoke_step["run"]


def test_split_service_entrypoints_forward_explicit_commands() -> None:
    for script in (
        "deploy/entrypoint-gateway.sh",
        "deploy/entrypoint-hermes.sh",
        "deploy/entrypoint-connector.sh",
        "deploy/entrypoint-mcp-prometheus.sh",
        "deploy/entrypoint-mcp-loki.sh",
    ):
        result = subprocess.run(
            ["bash", script, "python3", "-c", "print('ok')"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.stdout.strip() == "ok"


def test_gateway_entrypoint_sets_alertmanager_handoff_defaults(tmp_path: Path) -> None:
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    log_path = tmp_path / "env.log"

    script = wrapper_dir / "python3"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$AIOPS_CONNECTOR_URL\" \"$AIOPS_HERMES_URL\" \"$AIOPS_HERMES_DIAGNOSIS_PATH\" > {log_path}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    env = os.environ.copy()
    for name in ("AIOPS_CONNECTOR_URL", "AIOPS_HERMES_URL", "AIOPS_HERMES_DIAGNOSIS_PATH"):
        env.pop(name, None)
    env["PATH"] = f"{wrapper_dir}:{env['PATH']}"

    subprocess.run(
        ["bash", "deploy/entrypoint-gateway.sh"],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=5,
    )

    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "http://connector:8081",
        "http://hermes:8082",
        "/diagnosis/sessions",
    ]


def test_gateway_and_connector_smoke_connectivity() -> None:
    gateway = _start(
        "apps.aiops_k8s_gateway.main",
        "--host",
        "127.0.0.1",
        "--port",
        "18080",
        env={"AIOPS_CONNECTOR_URL": "http://127.0.0.1:18081"},
    )
    connector = _start(
        "apps.cluster_connector.main",
        "--host",
        "127.0.0.1",
        "--port",
        "18081",
        env={
            "AIOPS_GATEWAY_URL": "http://127.0.0.1:18080",
            "AIOPS_CONNECTOR_ID": "connector-test",
            "AIOPS_CLUSTER_ID": "cluster-test",
            "AIOPS_NAMESPACE_SCOPE": "default,kube-system",
        },
    )
    try:
        assert _wait_json("http://127.0.0.1:18080/healthz")["status"] == "ok"
        connector_health = _wait_json("http://127.0.0.1:18081/healthz")
        assert connector_health["status"] == "ok"
        assert connector_health["registration"]["cluster_id"] == "cluster-test"
        assert _wait_json("http://127.0.0.1:18080/connectivity/connector")["status"] == "ok"
    finally:
        for process in (connector, gateway):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def test_hermes_smoke_connectivity_to_gateway() -> None:
    gateway = _start(
        "apps.aiops_k8s_gateway.main",
        "--host",
        "127.0.0.1",
        "--port",
        "18082",
    )
    hermes = _start(
        "hermes.service_main",
        "--host",
        "127.0.0.1",
        "--port",
        "18083",
        env={"AIOPS_GATEWAY_URL": "http://127.0.0.1:18082"},
    )
    try:
        assert _wait_json("http://127.0.0.1:18082/healthz")["status"] == "ok"
        assert _wait_json("http://127.0.0.1:18083/healthz")["status"] == "ok"
        assert _wait_json("http://127.0.0.1:18083/connectivity/gateway")["status"] == "ok"
    finally:
        for process in (hermes, gateway):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
