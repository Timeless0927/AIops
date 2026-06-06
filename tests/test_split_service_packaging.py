"""Split service packaging guardrails."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


class _EvidenceConnectorHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._write(200, {"status": "ok"})
            return
        self._write(404, {"status": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/diagnostics/read":
            self._write(404, {"status": "not_found"})
            return
        self._write(
            200,
            {
                "service": "cluster-connector",
                "request_id": "req-evidence",
                "tool_name": "query_metrics",
                "status": "succeeded",
                "summary": "fake connector evidence",
                "data": {"ref": "ev_prom_1234"},
                "evidence_refs": [
                    {
                        "ref_id": "ev_prom_1234",
                        "source": "prometheus",
                        "cluster_id": "cluster-diagnostic",
                        "namespace": "default",
                        "service": "payment-api",
                        "query_digest": "abcd",
                    }
                ],
                "audit": {"status": "succeeded"},
                "errors": [],
            },
        )

    def _write(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
            assert isinstance(data, dict)
            return response.status, data
    except urllib.error.HTTPError as exc:
        data = json.loads(exc.read().decode("utf-8"))
        assert isinstance(data, dict)
        return exc.code, data


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


def _start_fake_connector(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _EvidenceConnectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_dockerfile_declares_independent_service_targets() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text(encoding="utf-8")

    assert "FROM base AS gateway" in dockerfile
    assert "FROM hermes-runtime AS hermes" in dockerfile
    assert "FROM base AS connectors" in dockerfile
    assert "FROM base AS hermes-smoke" in dockerfile
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
    assert services["hermes"]["build"]["target"] == "hermes-smoke"
    assert services["smoke"]["build"]["target"] == "hermes-smoke"
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


def test_gateway_connector_and_hermes_read_only_diagnostic_contract() -> None:
    gateway = _start(
        "apps.aiops_k8s_gateway.main",
        "--host",
        "127.0.0.1",
        "--port",
        "18084",
        env={"AIOPS_CONNECTOR_URL": "http://127.0.0.1:18085"},
    )
    connector = _start(
        "apps.cluster_connector.main",
        "--host",
        "127.0.0.1",
        "--port",
        "18085",
        env={
            "AIOPS_GATEWAY_URL": "http://127.0.0.1:18084",
            "AIOPS_CONNECTOR_ID": "connector-diagnostic",
            "AIOPS_CLUSTER_ID": "cluster-diagnostic",
            "AIOPS_NAMESPACE_SCOPE": "default",
        },
    )
    hermes = _start(
        "hermes.service_main",
        "--host",
        "127.0.0.1",
        "--port",
        "18086",
        env={"AIOPS_GATEWAY_URL": "http://127.0.0.1:18084"},
    )
    try:
        assert _wait_json("http://127.0.0.1:18084/connectivity/connector")["status"] == "ok"
        assert _wait_json("http://127.0.0.1:18086/connectivity/gateway")["status"] == "ok"

        payload = {
            "tool": "query_metrics",
            "args": {
                "request_id": "req-http-1",
                "cluster_id": "cluster-diagnostic",
                "namespace": "default",
                "service": "payment-api",
                "query": "up",
                "start": "2026-06-04T00:00:00Z",
                "end": "2026-06-04T00:10:00Z",
                "reason": "contract test",
            },
        }
        status, gateway_result = _post_json("http://127.0.0.1:18084/diagnostics/read", payload)
        assert status == 200
        assert gateway_result["service"] == "aiops-k8s-gateway"
        assert gateway_result["status"] == "failed"
        assert gateway_result["tool_name"] == "query_metrics"
        assert gateway_result["connector"]["connector_id"] == "connector-diagnostic"
        assert gateway_result["errors"][0]["code"] == "backend_unavailable"
        assert gateway_result["evidence_refs"] == []

        command_status, command_result = _post_json(
            "http://127.0.0.1:18084/diagnostics/read",
            {
                "tool": "run_k8s_read",
                "args": {
                    "request_id": "req-http-command",
                    "cluster_id": "cluster-diagnostic",
                    "namespace": "default",
                    "argv": ["kubectl", "delete", "pod", "payment-api", "-n", "default"],
                    "reason": "contract test rejects mutation",
                },
            },
        )
        assert command_status == 200
        assert command_result["tool_name"] == "run_k8s_read"
        assert command_result["status"] == "failed"
        assert command_result["errors"][0]["code"] == "command_rejected"
        assert command_result["evidence_refs"] == []

        escalation_status, escalation_result = _post_json(
            "http://127.0.0.1:18084/diagnostics/read",
            {
                "tool": "run_k8s_read",
                "args": {
                    "request_id": "req-http-scope-escalation",
                    "cluster_id": "caller-cluster",
                    "namespace": "kube-system",
                    "namespace_scope": ["*"],
                    "argv": ["kubectl", "get", "pods", "-n", "kube-system"],
                    "reason": "caller-supplied scope must not expand connector scope",
                },
            },
        )
        assert escalation_status == 200
        assert escalation_result["tool_name"] == "run_k8s_read"
        assert escalation_result["connector"]["cluster_id"] == "cluster-diagnostic"
        assert escalation_result["connector"]["namespace_scope"] == ["default"]
        assert escalation_result["status"] == "failed"
        assert escalation_result["errors"][0]["code"] == "namespace_out_of_scope"

        _, hermes_result = _post_json("http://127.0.0.1:18086/diagnostics/gateway", payload)
        assert hermes_result["service"] == "hermes"
        assert hermes_result["gateway_status"] == "failed"
        assert hermes_result["diagnostic"]["tool_name"] == "query_metrics"
        assert hermes_result["diagnostic"]["errors"][0]["code"] == "backend_unavailable"
        assert hermes_result["diagnostic"]["evidence_refs"] == []
    finally:
        for process in (hermes, connector, gateway):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def test_gateway_diagnostic_entry_maps_connector_unavailable_to_controlled_error() -> None:
    gateway = _start(
        "apps.aiops_k8s_gateway.main",
        "--host",
        "127.0.0.1",
        "--port",
        "18087",
        env={"AIOPS_CONNECTOR_URL": "http://127.0.0.1:65530"},
    )
    try:
        assert _wait_json("http://127.0.0.1:18087/healthz")["status"] == "ok"
        status, result = _post_json(
            "http://127.0.0.1:18087/diagnostics/read",
            {"tool": "query_logs", "args": {"request_id": "req-offline"}},
        )

        assert status == 503
        assert result["service"] == "aiops-k8s-gateway"
        assert result["status"] == "failed"
        assert result["tool_name"] == "query_logs"
        assert result["errors"][0]["code"] == "connector_offline"
        assert result["evidence_refs"] == []
    finally:
        gateway.terminate()
        try:
            gateway.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gateway.kill()


def test_gateway_and_hermes_preserve_connector_evidence_refs() -> None:
    fake_connector = _start_fake_connector(18088)
    gateway = _start(
        "apps.aiops_k8s_gateway.main",
        "--host",
        "127.0.0.1",
        "--port",
        "18089",
        env={"AIOPS_CONNECTOR_URL": "http://127.0.0.1:18088"},
    )
    hermes = _start(
        "hermes.service_main",
        "--host",
        "127.0.0.1",
        "--port",
        "18090",
        env={"AIOPS_GATEWAY_URL": "http://127.0.0.1:18089"},
    )
    try:
        assert _wait_json("http://127.0.0.1:18089/connectivity/connector")["status"] == "ok"
        payload = {"tool": "query_metrics", "args": {"request_id": "req-evidence"}}

        _, gateway_result = _post_json("http://127.0.0.1:18089/diagnostics/read", payload)
        assert gateway_result["status"] == "succeeded"
        assert gateway_result["evidence_refs"][0]["ref_id"] == "ev_prom_1234"
        assert gateway_result["evidence_refs"][0]["source"] == "prometheus"

        _, hermes_result = _post_json("http://127.0.0.1:18090/diagnostics/gateway", payload)
        diagnostic = hermes_result["diagnostic"]
        assert diagnostic["status"] == "succeeded"
        assert diagnostic["evidence_refs"][0]["ref_id"] == "ev_prom_1234"
        assert diagnostic["evidence_refs"][0]["service"] == "payment-api"
    finally:
        for process in (hermes, gateway):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        fake_connector.shutdown()
        fake_connector.server_close()
