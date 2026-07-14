"""Opt-in real Cluster checks for the Pilot observability owner paths."""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
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
FIXTURE_BASE = ROOT / "verification/base"
FIXTURE_RUN = ROOT / "verification/run"
NAMESPACE = "aiops-system"
VERIFICATION_NAMESPACE = "aiops-verification"
CLUSTER_ID = "pilot-cluster"


def _kubectl(*args: str) -> str:
    return subprocess.run(
        ["kubectl", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _apply_object(resource: dict[str, object]) -> None:
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        cwd=ROOT,
        check=True,
        input=json.dumps(resource),
        capture_output=True,
        text=True,
    )


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


def _mcp_call(service: str, path: str, payload: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(payload, separators=(",", ":"))
    script = (
        "token=$(cat /var/run/secrets/aiops-internal/token); "
        "curl -fsS -H \"Authorization: Bearer $token\" "
        "-H 'Content-Type: application/json' "
        f"-d {shlex.quote(encoded)} http://{service}{path}"
    )
    result = json.loads(
        _kubectl("-n", NAMESPACE, "exec", "deployment/aiops-diagnosis", "--", "sh", "-ec", script)
    )
    assert isinstance(result, dict)
    return result


def _mcp_query() -> dict[str, object]:
    return _mcp_call(
        "aiops-mcp-prometheus:8083",
        "/query_metrics",
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
    )


def _loki_query(opener: urllib.request.OpenerDirector, base_url: str, query: str) -> dict[str, object]:
    end = time.time_ns()
    parameters = urllib.parse.urlencode(
        {
            "query": query,
            "start": end - 15 * 60 * 1_000_000_000,
            "end": end,
            "limit": 20,
        }
    )
    return _request(opener, f"{base_url}/loki/api/v1/query_range?{parameters}")


def _loki_stream(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    query: str,
    *needles: str,
) -> dict[str, object] | None:
    result = _loki_query(opener, base_url, query)["data"]["result"]
    return next(
        (
            stream
            for stream in result
            if any(all(needle in value[1] for needle in needles) for value in stream["values"])
        ),
        None,
    )


def _mechanical_rollout_for_fixture_test(run_id: str) -> None:
    """Exercise the fixture restart seam; A02 owns live governed approval evidence."""
    patch = json.dumps(
        [
            {
                "op": "add",
                "path": "/spec/template/metadata/annotations/aiops.dev~1verification-run-id",
                "value": run_id,
            }
        ],
        separators=(",", ":"),
    )
    _kubectl(
        "-n",
        VERIFICATION_NAMESPACE,
        "patch",
        "deployment/verification-api",
        "--type=json",
        "-p",
        patch,
    )
    _kubectl(
        "-n",
        VERIFICATION_NAMESPACE,
        "rollout",
        "status",
        "deployment/verification-api",
        "--timeout=120s",
    )


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

    with (
        _port_forward("aiops-prometheus", 19091, 9090),
        _port_forward("aiops-gateway", 18080, 8080),
        _port_forward("aiops-loki", 13102, 3100),
    ):
        prometheus = "http://127.0.0.1:19091"
        gateway = "http://127.0.0.1:18080"
        loki = "http://127.0.0.1:13102"
        _request(
            opener,
            f"{gateway}/auth/login",
            method="POST",
            body={"username": "admin", "password": password, "session_mode": "cookie"},
        )
        csrf = _request(opener, f"{gateway}/auth/csrf")["csrf_token"]
        assert isinstance(csrf, str)
        _ensure_cluster(opener, gateway, csrf)

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

        def target_health() -> dict[str, str] | None:
            targets = _request(opener, f"{prometheus}/api/v1/targets")["data"]
            assert isinstance(targets, dict)
            active = targets["activeTargets"]
            assert isinstance(active, list)
            health = {target["labels"]["job"]: target["health"] for target in active}
            return health if expected_health.items() <= health.items() else None

        _wait_for(target_health)
        groups = _request(opener, f"{prometheus}/api/v1/rules")["data"]["groups"]
        rules = {rule["name"]: rule["health"] for group in groups for rule in group["rules"]}
        assert rules["AIOpsVerificationWorkloadUnavailable"] == "ok"

        envelope = _mcp_query()
        assert envelope["status"] == "succeeded"
        assert envelope["data"]["returned_series"] >= 1
        assert envelope["evidence_refs"][0]["cluster_id"] == CLUSTER_ID

        baseline: dict[str, dict[str, object]] = {}
        for row in _request(opener, f"{gateway}/api/v1/incidents")["incidents"]:
            if (
                row["alertname"] != "AIOpsVerificationWorkloadUnavailable"
                or row["namespace"] != VERIFICATION_NAMESPACE
            ):
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

        _kubectl("apply", "-k", str(FIXTURE_BASE))
        try:
            _kubectl(
                "-n",
                VERIFICATION_NAMESPACE,
                "wait",
                "--for=condition=Available",
                "deployment/verification-api",
                "--timeout=120s",
            )
            _kubectl("apply", "-k", str(FIXTURE_RUN))
            _kubectl(
                "-n",
                VERIFICATION_NAMESPACE,
                "wait",
                "--for=condition=complete",
                "job/verification-trigger",
                "--timeout=120s",
            )
            run_id = _kubectl(
                "-n",
                VERIFICATION_NAMESPACE,
                "get",
                "job/verification-trigger",
                "-o",
                "jsonpath={.metadata.uid}",
            )
            log_query = (
                f'{{cluster="{CLUSTER_ID}",namespace="{VERIFICATION_NAMESPACE}",'
                f'container="verification-api"}}'
            )
            _wait_for(
                lambda: _loki_stream(
                    opener, loki, log_query, run_id, "verification_fault_activated"
                )
            )

            def firing() -> dict[str, object] | None:
                alerts = _request(opener, f"{prometheus}/api/v1/alerts")["data"]["alerts"]
                return next(
                    (
                        alert
                        for alert in alerts
                        if alert["labels"].get("alertname") == "AIOpsVerificationWorkloadUnavailable"
                        and alert["labels"].get("namespace") == VERIFICATION_NAMESPACE
                        and alert["labels"].get("deployment") == "verification-api"
                        and alert["labels"].get("service") == "verification-api"
                        and alert["labels"].get("run_id") == run_id
                        and alert["state"] == "firing"
                    ),
                    None,
                )

            fired = _wait_for(firing)
            assert fired["labels"]["cluster"] == CLUSTER_ID

            def incident() -> dict[str, object] | None:
                listing = _request(opener, f"{gateway}/api/v1/incidents")["incidents"]
                return next(
                    (
                        row
                        for row in listing
                        if row["alertname"] == "AIOpsVerificationWorkloadUnavailable"
                        and row["namespace"] == VERIFICATION_NAMESPACE
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
            assert opened["namespace"] == VERIFICATION_NAMESPACE

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

            _mechanical_rollout_for_fixture_test(run_id)
            _wait_for(
                lambda: _loki_stream(
                    opener, loki, log_query, run_id, "verification_fault_recovered"
                )
            )

            def inactive() -> bool:
                alerts = _request(opener, f"{prometheus}/api/v1/alerts")["data"]["alerts"]
                return not any(
                    alert["labels"].get("alertname") == "AIOpsVerificationWorkloadUnavailable"
                    and alert["labels"].get("namespace") == VERIFICATION_NAMESPACE
                    and alert["labels"].get("run_id") == run_id
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

            _kubectl("delete", "-k", str(FIXTURE_RUN), "--ignore-not-found=true")
            _kubectl("apply", "-k", str(FIXTURE_RUN))
            _kubectl(
                "-n",
                VERIFICATION_NAMESPACE,
                "wait",
                "--for=condition=complete",
                "job/verification-trigger",
                "--timeout=120s",
            )
            rerun_id = _kubectl(
                "-n",
                VERIFICATION_NAMESPACE,
                "get",
                "job/verification-trigger",
                "-o",
                "jsonpath={.metadata.uid}",
            )
            assert rerun_id != run_id
            _mechanical_rollout_for_fixture_test(rerun_id)
            _wait_for(
                lambda: _loki_stream(
                    opener, loki, log_query, rerun_id, "verification_fault_recovered"
                )
            )
        finally:
            _kubectl("delete", "-k", str(FIXTURE_RUN), "--ignore-not-found=true")
            _kubectl("delete", "-k", str(FIXTURE_BASE), "--ignore-not-found=true")

        retained = _request(opener, f"{gateway}/api/v1/incidents/{opened['id']}/workbench")
        assert retained["incident"]["id"] == opened["id"]


def test_real_alloy_loki_mcp_and_owner_unavailable_path() -> None:
    suffix = f"{time.time_ns():x}"[-10:]
    pod_name = f"aiops-log-{suffix}"
    run_id = f"O02-{suffix}"
    query = (
        f'{{cluster="{CLUSTER_ID}",namespace="{NAMESPACE}",pod="{pod_name}",'
        f'container="verification"}} |= "{run_id}"'
    )
    pod: dict[str, object] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/name": "aiops-log-verification"},
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "runAsGroup": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "verification",
                    "image": (
                        "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway@"
        "sha256:8d587b3cdbc03059a2c18971075918f6c7995fe367539eae1214080f8a72fada"
                    ),
                    "command": ["/bin/sh", "-ec"],
                    "args": [f"echo {run_id}; sleep 300"],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "50m", "memory": "64Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
        },
    }
    opener = urllib.request.build_opener()
    _apply_object(pod)
    try:
        _kubectl(
            "-n",
            NAMESPACE,
            "wait",
            "--for=condition=Ready",
            f"pod/{pod_name}",
            "--timeout=60s",
        )

        def direct_log(base_url: str) -> dict[str, object] | None:
            return _loki_stream(opener, base_url, query, run_id)

        with _port_forward("aiops-loki", 13101, 3100):
            with opener.open("http://127.0.0.1:13101/ready", timeout=10) as response:
                assert response.status == 200
            first_stream = _wait_for(lambda: direct_log("http://127.0.0.1:13101"))
            expected_labels = {
                "cluster": CLUSTER_ID,
                "namespace": NAMESPACE,
                "pod": pod_name,
                "container": "verification",
                "app": "aiops-log-verification",
            }
            assert expected_labels.items() <= first_stream["stream"].items()

        envelope = _mcp_call(
            "aiops-mcp-loki:8084",
            "/query_logs",
            {
                "request_id": f"o02-real-mcp-{suffix}",
                "cluster_id": CLUSTER_ID,
                "namespace": NAMESPACE,
                "service": "aiops-log-verification",
                "reason": "O02 real guarded log query",
                "query": query,
                "time_range": {"type": "relative", "value": "15m"},
                "max_lines": 20,
                "sample_size": 5,
            },
        )
        assert envelope["status"] == "succeeded"
        assert envelope["data"]["returned_lines"] >= 1
        assert envelope["evidence_refs"][0]["source"] == "loki"

        _kubectl("-n", NAMESPACE, "scale", "deployment", "aiops-loki", "--replicas=0")
        _kubectl(
            "-n",
            NAMESPACE,
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app.kubernetes.io/name=aiops-loki",
            "--timeout=60s",
        )
        unavailable = _mcp_call(
            "aiops-mcp-loki:8084",
            "/query_logs",
            {
                "request_id": f"o02-unavailable-{suffix}",
                "cluster_id": CLUSTER_ID,
                "namespace": NAMESPACE,
                "service": "aiops-log-verification",
                "reason": "O02 owner unavailable check",
                "query": query,
                "time_range": {"type": "relative", "value": "15m"},
                "max_lines": 20,
            },
        )
        assert unavailable["status"] == "failed"
        assert unavailable["errors"][0]["code"] == "backend_unavailable"

        _kubectl("-n", NAMESPACE, "scale", "deployment", "aiops-loki", "--replicas=1")
        _kubectl(
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            "deployment/aiops-loki",
            "--timeout=60s",
        )
        with _port_forward("aiops-loki", 13101, 3100):
            assert _wait_for(lambda: direct_log("http://127.0.0.1:13101"))
    finally:
        try:
            _kubectl("-n", NAMESPACE, "scale", "deployment", "aiops-loki", "--replicas=1")
            _kubectl(
                "-n",
                NAMESPACE,
                "rollout",
                "status",
                "deployment/aiops-loki",
                "--timeout=60s",
            )
        finally:
            _kubectl("-n", NAMESPACE, "delete", "pod", pod_name, "--ignore-not-found=true")
