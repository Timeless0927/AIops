"""Smoke tests for observability MCP HTTP runtimes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


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


def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
        assert isinstance(data, dict)
        return data


def _start(module: str, port: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {
            "PROMETHEUS_URL": "",
            "LOKI_URL": "",
        }
    )
    return subprocess.Popen(
        [sys.executable, "-m", module, "--host", "127.0.0.1", "--port", port],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_prometheus_mcp_runtime_health_and_query_degradation() -> None:
    process = _start("apps.mcp_prometheus.main", "18084")
    try:
        health = _wait_json("http://127.0.0.1:18084/healthz")
        assert health["service"] == "mcp-prometheus"
        assert health["tool_name"] == "query_metrics"

        envelope = _post_json(
            "http://127.0.0.1:18084/query_metrics",
            {
                "request_id": "runtime-prom-1",
                "cluster_id": "cluster-a",
                "reason": "runtime smoke",
                "query": "up",
            },
        )
        assert envelope["tool_name"] == "query_metrics"
        assert envelope["status"] == "failed"
        assert envelope["errors"][0]["code"] == "backend_unavailable"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def test_loki_mcp_runtime_health_and_query_degradation() -> None:
    process = _start("apps.mcp_loki.main", "18085")
    try:
        health = _wait_json("http://127.0.0.1:18085/healthz")
        assert health["service"] == "mcp-loki"
        assert health["tool_name"] == "query_logs"

        envelope = _post_json(
            "http://127.0.0.1:18085/query_logs",
            {
                "request_id": "runtime-loki-1",
                "cluster_id": "cluster-a",
                "reason": "runtime smoke",
                "query": '{app="payment-api"}',
                "time_range": {"type": "relative", "value": "15m"},
                "max_lines": 20,
            },
        )
        assert envelope["tool_name"] == "query_logs"
        assert envelope["status"] == "failed"
        assert envelope["errors"][0]["code"] == "backend_unavailable"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
