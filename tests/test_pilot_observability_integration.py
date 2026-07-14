"""Opt-in real Cluster check for the O01 metrics and alert path."""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("AIOPS_RUN_KUBERNETES_INTEGRATION") != "1",
    reason="requires the applied Pilot overlay and an explicit real-Cluster opt-in",
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "deploy/k8s/smoke/aiops-verification-unavailable.yaml"
NAMESPACE = "aiops-system"
CLUSTER_ID = "pilot-cluster"


def _kubectl(*args: str) -> str:
    return subprocess.run(
        ["kubectl", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@contextmanager
def _port_forward(service: str, local_port: int, remote_port: int) -> Iterator[None]:
    process = subprocess.Popen(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "port-forward",
            f"service/{service}",
            f"{local_port}:{remote_port}",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        assert "Forwarding from" in line, line
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _request(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with opener.open(request, timeout=10) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"{method} {url} failed: {exc.code} {exc.read().decode()}") from exc
    assert isinstance(result, dict)
    return result


def _wait_for(check: Callable[[], object], *, timeout: float = 120) -> object:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(3)
    raise AssertionError(f"condition did not become true in {timeout}s; last={last!r}")


def _ensure_cluster(opener: urllib.request.OpenerDirector, gateway: str, csrf: str) -> None:
    state = _request(opener, f"{gateway}/api/v1/admin/connector-enrollments")
    clusters = state.get("clusters")
    assert isinstance(clusters, list)
    if any(cluster.get("cluster_id") == CLUSTER_ID for cluster in clusters):
        return

    enrolled = _request(
        opener,
        f"{gateway}/api/v1/admin/connector-enrollments",
        method="POST",
        headers={"X-CSRF-Token": csrf},
        body={
            "connector_id": "connector-pilot",
            "cluster_id": CLUSTER_ID,
            "reason": "O01 real alert integration",
        },
    )
    credential = enrolled.get("credential")
    assert isinstance(credential, str) and credential
    _request(
        opener,
        f"{gateway}/api/v1/connectors/register",
        method="POST",
        headers={"Authorization": f"Bearer {credential}"},
        body={"connector_id": "connector-pilot", "cluster_id": CLUSTER_ID},
    )


def _mcp_query() -> dict[str, object]:
    payload = json.dumps(
        {
            "request_id": "o01-real-mcp",
            "cluster_id": CLUSTER_ID,
            "namespace": NAMESPACE,
            "service": "aiops-kube-state-metrics",
            "reason": "O01 real guarded query",
            "query": 'up{job="kube-state-metrics"}',
            "max_series": 5,
            "step": "15s",
        },
        separators=(",", ":"),
    )
    script = (
        "token=$(cat /var/run/secrets/aiops-internal/token); "
        "curl -fsS -H \"Authorization: Bearer $token\" "
        "-H 'Content-Type: application/json' "
        f"-d {shlex.quote(payload)} http://aiops-mcp-prometheus:8083/query_metrics"
    )
    result = json.loads(
        _kubectl("-n", NAMESPACE, "exec", "deployment/aiops-diagnosis", "--", "sh", "-ec", script)
    )
    assert isinstance(result, dict)
    return result


def test_real_prometheus_alertmanager_gateway_and_mcp_path() -> None:
    password = base64.b64decode(
        _kubectl(
            "-n",
            NAMESPACE,
            "get",
            "secret",
            "aiops-runtime-secret",
            "-o",
            "jsonpath={.data.AIOPS_BOOTSTRAP_ADMIN_PASSWORD}",
        )
    ).decode()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    with _port_forward("aiops-prometheus", 19091, 9090), _port_forward("aiops-gateway", 18080, 8080):
        prometheus = "http://127.0.0.1:19091"
        gateway = "http://127.0.0.1:18080"
        _request(
            opener,
            f"{gateway}/auth/login",
            method="POST",
            body={"username": "admin", "password": password, "session_mode": "cookie"},
        )
        csrf = _request(opener, f"{gateway}/auth/csrf")["csrf_token"]
        assert isinstance(csrf, str)
        _ensure_cluster(opener, gateway, csrf)

        targets = _request(opener, f"{prometheus}/api/v1/targets")["data"]
        assert isinstance(targets, dict)
        active = targets["activeTargets"]
        assert isinstance(active, list)
        health = {target["labels"]["job"]: target["health"] for target in active}
        expected_health = {
            "prometheus": "up",
            "alertmanager": "up",
            "kube-state-metrics": "up",
            "aiops-gateway": "up",
            "aiops-connector": "up",
            "aiops-diagnosis": "up",
            "aiops-notification": "up",
            "aiops-mcp-prometheus": "up",
            "aiops-mcp-loki": "up",
            "aiops-mcp-topology": "up",
        }
        assert expected_health.items() <= health.items()
        groups = _request(opener, f"{prometheus}/api/v1/rules")["data"]["groups"]
        rules = {rule["name"]: rule["health"] for group in groups for rule in group["rules"]}
        assert rules["AIOpsVerificationWorkloadUnavailable"] == "ok"

        envelope = _mcp_query()
        assert envelope["status"] == "succeeded"
        assert envelope["data"]["returned_series"] >= 1
        assert envelope["evidence_refs"][0]["cluster_id"] == CLUSTER_ID

        baseline: dict[str, dict[str, object]] = {}
        for row in _request(opener, f"{gateway}/api/v1/incidents")["incidents"]:
            if row["alertname"] != "AIOpsVerificationWorkloadUnavailable":
                continue
            workbench = _request(opener, f"{gateway}/api/v1/incidents/{row['id']}/workbench")
            signals = workbench["alert_signals"]
            recovery = workbench.get("recovery_observation")
            baseline[str(row["id"])] = {
                "incident_marker": (
                    row["updated_at"],
                    row["evidence_revision"],
                    row["signal_count"],
                ),
                "signal_updated_at": max(
                    (float(signal["updated_at"]) for signal in signals),
                    default=0.0,
                ),
                "recovery_id": recovery.get("id") if isinstance(recovery, dict) else None,
                "recovery_observed_at": (
                    float(recovery["observed_at"]) if isinstance(recovery, dict) else 0.0
                ),
            }

        _kubectl("apply", "-f", str(FIXTURE))
        try:
            def firing() -> dict[str, object] | None:
                alerts = _request(opener, f"{prometheus}/api/v1/alerts")["data"]["alerts"]
                return next(
                    (
                        alert
                        for alert in alerts
                        if alert["labels"].get("alertname") == "AIOpsVerificationWorkloadUnavailable"
                        and alert["state"] == "firing"
                    ),
                    None,
                )

            _wait_for(firing)

            def incident() -> dict[str, object] | None:
                listing = _request(opener, f"{gateway}/api/v1/incidents")["incidents"]
                return next(
                    (
                        row
                        for row in listing
                        if row["alertname"] == "AIOpsVerificationWorkloadUnavailable"
                        and (
                            str(row["id"]) not in baseline
                            or (
                                row["updated_at"],
                                row["evidence_revision"],
                                row["signal_count"],
                            )
                            != baseline[str(row["id"])]["incident_marker"]
                        )
                    ),
                    None,
                )

            opened = _wait_for(incident)
            assert isinstance(opened, dict)
            assert opened["cluster_id"] == CLUSTER_ID
            assert opened["namespace"] == NAMESPACE

            baseline_signal_updated_at = float(
                baseline.get(str(opened["id"]), {}).get("signal_updated_at", 0.0)
            )

            def delivered_firing() -> dict[str, object] | None:
                workbench = _request(
                    opener,
                    f"{gateway}/api/v1/incidents/{opened['id']}/workbench",
                )
                signal = next(
                    (
                        item
                        for item in workbench["alert_signals"]
                        if item["alertname"] == "AIOpsVerificationWorkloadUnavailable"
                        and item["status"] == "firing"
                        and float(item["updated_at"]) > baseline_signal_updated_at
                    ),
                    None,
                )
                return signal

            firing_signal = _wait_for(delivered_firing)
            assert isinstance(firing_signal, dict)
            firing_updated_at = float(firing_signal["updated_at"])

            _kubectl("-n", NAMESPACE, "scale", "deployment", "aiops-verification", "--replicas=0")

            def inactive() -> bool:
                alerts = _request(opener, f"{prometheus}/api/v1/alerts")["data"]["alerts"]
                return not any(
                    alert["labels"].get("alertname") == "AIOpsVerificationWorkloadUnavailable"
                    for alert in alerts
                )

            _wait_for(inactive)

            def recovered() -> dict[str, object] | None:
                workbench = _request(
                    opener,
                    f"{gateway}/api/v1/incidents/{opened['id']}/workbench",
                )
                observation = workbench.get("recovery_observation")
                delivered = any(
                    signal["alertname"] == "AIOpsVerificationWorkloadUnavailable"
                    and signal["status"] == "recovered"
                    and float(signal["updated_at"]) > firing_updated_at
                    for signal in workbench["alert_signals"]
                )
                previous = baseline.get(str(opened["id"]), {})
                is_new_observation = (
                    isinstance(observation, dict)
                    and observation["id"] != previous.get("recovery_id")
                    and float(observation["observed_at"])
                    > float(previous.get("recovery_observed_at", 0.0))
                )
                return observation if delivered and is_new_observation else None

            observation = _wait_for(recovered)
            assert observation["status"] in {"stabilizing", "resolved"}
        finally:
            _kubectl("delete", "-f", str(FIXTURE), "--ignore-not-found=true")
