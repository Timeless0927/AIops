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
    assert "FROM hermes-runtime AS aiops" in dockerfile
    assert 'pip install "hermes-agent[messaging,feishu] @ file:///tmp/hermes-agent"' in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint-gateway.sh"]' in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint-hermes.sh"]' in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint-connector.sh"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_compose_smoke_wires_gateway_hermes_and_connectors() -> None:
    compose = yaml.safe_load(Path("docker-compose.services.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["gateway"]["build"]["target"] == "gateway"
    assert services["connector"]["build"]["target"] == "connectors"
    assert services["hermes"]["build"]["target"] == "hermes"
    assert services["smoke"]["command"] == ["python3", "-m", "runtime.service_mesh_smoke"]
    assert services["gateway"]["environment"]["AIOPS_CONNECTOR_URL"] == "http://connector:8081"
    assert services["hermes"]["environment"]["AIOPS_GATEWAY_URL"] == "http://gateway:8080"


def test_split_service_entrypoints_forward_explicit_commands() -> None:
    for script in (
        "deploy/entrypoint-gateway.sh",
        "deploy/entrypoint-hermes.sh",
        "deploy/entrypoint-connector.sh",
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
