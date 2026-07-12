"""T22 public observability seams for durable control-plane work."""

from __future__ import annotations

import io
import json
import sys
import threading
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.approval import Approvals  # noqa: F401 - registers Gateway owner migrations
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store
from apps.cluster_connector.command_worker import ConnectorCommandJournal
from apps.service_http import JsonHandler
from diagnosis_service.jobs import DiagnosisJobs
from notification_service.requests import NotificationStore


def test_http_surface_exposes_bounded_red_metrics_and_safe_json_log() -> None:
    class ProbeHandler(JsonHandler):
        service_name = "probe"

        def do_POST(self) -> None:  # noqa: N802
            self.read_json_body()
            self.write_json(HTTPStatus.CREATED, {"status": "accepted"})

        def do_GET(self) -> None:  # noqa: N802
            if self.is_metrics_request():
                self.write_metrics(self.service_name)
                return
            self.write_not_found()

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    output = io.StringIO()
    previous_stdout = sys.stdout
    sys.stdout = output
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/work?token=do-not-log",
            data=b'{"credential":"do-not-log"}',
            headers={
                "Authorization": "Bearer do-not-log",
                "Content-Type": "application/json",
                "X-Request-ID": "req-22",
                "X-Correlation-ID": "corr-22",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == HTTPStatus.CREATED
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/metrics", timeout=3
        ) as response:
            metrics = response.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        sys.stdout = previous_stdout

    assert 'aiops_http_requests_total{service="probe",method="POST",status="2xx"} 1' in metrics
    assert 'aiops_http_request_duration_seconds_count{service="probe",method="POST"} 1' in metrics
    assert 'aiops_storage_available_ratio{service="probe"}' in metrics
    assert 'aiops_sqlite_errors_total{service="probe"} 0' in metrics
    entry = next(json.loads(line) for line in output.getvalue().splitlines() if "req-22" in line)
    assert entry == {
        "service": "probe",
        "event": "http_request",
        "method": "POST",
        "status": 201,
        "request_id": "req-22",
        "correlation_id": "corr-22",
    }
    assert "do-not-log" not in output.getvalue()


def _gateway_store(tmp_path: Path, now: list[float]) -> GatewayV1Store:
    sequence = iter(f"id-{index}" for index in range(30))
    store = GatewayV1Store(
        tmp_path / "gateway.db",
        clock=lambda: now[0],
        credential_factory=lambda: "credential",
        id_factory=lambda _: next(sequence),
    )
    store.create_connector_enrollment(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin",
        reason="test",
        request_id="req-enroll",
    )
    store.register_connector("credential", "connector-prod", "cluster-prod", request_id="req-register")
    return store


def test_gateway_owner_metrics_cover_heartbeat_commands_and_unknown_outcome(tmp_path: Path) -> None:
    now = [100.0]
    store = _gateway_store(tmp_path, now)
    commands = ConnectorCommands(
        store.database,
        clock=lambda: now[0],
        id_factory=lambda prefix: f"{prefix}-1",
        lease_seconds=5,
    )
    commands.queue_read(
        cluster_id="cluster-prod",
        namespace="payments",
        action="get_resource",
        parameters={"resource_kind": "pods", "output": "json"},
        actor_id="admin",
        reason="test",
        request_id="req-command",
    )
    now[0] = 130.0

    metrics = ConnectorIdentity(store.database).metrics(now=now[0]) + commands.metrics()

    assert "aiops_gateway_connector_heartbeat_age_seconds 30.0" in metrics
    assert 'aiops_gateway_connector_commands{status="queued"} 1' in metrics
    assert "aiops_gateway_connector_command_oldest_age_seconds 30.0" in metrics
    assert 'aiops_gateway_connector_commands{status="unknown_outcome"} 0' in metrics
    assert "connector-prod" not in metrics
    assert "command-1" not in metrics


def test_diagnosis_and_connector_metrics_report_oldest_durable_work(tmp_path: Path) -> None:
    now = [1_000.0]
    jobs = DiagnosisJobs(tmp_path / "diagnosis.db", clock=lambda: now[0])
    jobs.accept(
        {
            "request_id": "diagnosis-1",
            "session_id": "diagnosis-1",
            "incident_id": "incident-1",
            "investigation_id": "investigation-1",
            "source": "gateway",
            "alert": {"alertname": "HighErrors", "status": "firing"},
        }
    )
    journal = ConnectorCommandJournal(tmp_path / "connector.db")
    journal.accept({"id": "command-1"})
    now[0] = 1_025.0

    diagnosis_metrics = jobs.metrics()
    connector_metrics = journal.metrics(now=now[0])

    assert 'aiops_diagnosis_jobs{status="queued"} 1' in diagnosis_metrics
    assert "aiops_diagnosis_job_oldest_age_seconds 25.0" in diagnosis_metrics
    assert 'aiops_connector_command_journal{state="accepted"} 1' in connector_metrics
    assert "aiops_connector_command_oldest_age_seconds" in connector_metrics
    for identity in ("diagnosis-1", "incident-1", "command-1"):
        assert identity not in diagnosis_metrics + connector_metrics


def test_diagnosis_duration_stops_when_execution_finishes(tmp_path: Path) -> None:
    now = [1_000.0]
    jobs = DiagnosisJobs(tmp_path / "diagnosis.db", clock=lambda: now[0])
    request = {
        "request_id": "diagnosis-1",
        "session_id": "diagnosis-1",
        "incident_id": "incident-1",
        "investigation_id": "investigation-1",
        "source": "gateway",
        "alert": {"alertname": "HighErrors", "status": "firing"},
    }
    jobs.accept(request)
    now[0] = 1_010.0
    jobs.run_execution_once(
        lambda payload: {
            "session_id": payload["session_id"],
            "incident_id": payload["incident_id"],
            "status": "completed",
            "diagnosis": {"summary": "done"},
        }
    )
    now[0] = 1_050.0
    jobs.run_writeback_once(lambda _payload: (200, {"ok": True}))

    metrics = jobs.metrics()

    assert "aiops_diagnosis_duration_seconds_count 1" in metrics
    assert "aiops_diagnosis_duration_seconds_sum 10.0" in metrics


def test_notification_metrics_include_oldest_delivery_without_identity_labels(tmp_path: Path) -> None:
    now = [2_000.0]
    store = NotificationStore(tmp_path / "notification.db", clock=lambda: now[0])
    store.accept(
        {
            "event_id": "incident.opened:incident-1:1",
            "event_type": "incident.opened",
            "occurred_at": now[0],
            "severity": "critical",
            "subject": {"type": "incident", "id": "incident-1", "version": 1},
            "scope": {"environment": "prod"},
            "summary": "Incident opened",
            "facts": {"incident_id": "incident-1", "status": "opened"},
            "console_path": "/incidents/incident-1",
        }
    )
    now[0] = 2_040.0

    metrics = store.metrics()

    assert "aiops_notification_delivery_oldest_age_seconds 40.0" in metrics
    assert "incident.opened:incident-1:1" not in metrics


def test_control_plane_rules_cover_unavailable_stalled_unknown_and_storage() -> None:
    rule = Path("deploy/k8s/base/control-plane-prometheusrule.yaml").read_text()
    kustomization = Path("deploy/k8s/base/kustomization.yaml").read_text()

    for alert in (
        "AIOpsControlPlaneUnavailable",
        "AIOpsDurableWorkStalled",
        "AIOpsUnknownOutcome",
        "AIOpsStoragePressure",
    ):
        assert f"alert: {alert}" in rule
    assert "control-plane-prometheusrule.yaml" in kustomization
    for forbidden_label in ("incident_id", "user_id", "command_id", "delivery_id"):
        assert forbidden_label not in rule


def test_gateway_metrics_include_sse_connection_gauge(tmp_path: Path) -> None:
    from apps.aiops_k8s_gateway.observability import metrics_body as gateway_metrics_body

    now = [100.0]
    store = _gateway_store(tmp_path, now)

    metrics = gateway_metrics_body(store.database, handler_type=None).decode()

    assert "aiops_gateway_sse_connections 0" in metrics
    assert "aiops_gateway_diagnosis_requests 0" in metrics
    assert "aiops_gateway_command_leases 0" in metrics


def test_internal_http_adapters_propagate_request_and_correlation_ids(
    tmp_path: Path, monkeypatch
) -> None:
    from apps.aiops_k8s_gateway.diagnosis_delivery import DiagnosisDelivery
    from apps.aiops_k8s_gateway.notification_handoff_http import send_notification_request
    from apps.cluster_connector import command_worker
    from diagnosis_service import service_main

    token = tmp_path / "token"
    token.write_text("service-account-token")
    monkeypatch.setenv("AIOPS_INTERNAL_TOKEN_FILE", str(token))
    monkeypatch.setenv("AIOPS_DIAGNOSIS_URL", "http://diagnosis.test")
    monkeypatch.setenv("AIOPS_NOTIFICATION_ENGINE_URL", "http://notification.test")
    captured: list[urllib.request.Request] = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b'{"status":"accepted","request_id":"diagnosis-1"}'

    def open_request(request, **_kwargs):
        captured.append(request)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)

    DiagnosisDelivery._send_http(
        {"request_id": "diagnosis-1", "incident_id": "incident-1", "investigation_id": "inv-1"}
    )
    service_main._post_json(
        "http://mcp.test/query",
        {"request_id": "tool-1", "correlation_id": "incident-1"},
        1,
    )
    command_worker._post_json(
        "https://gateway.test",
        "/api/v1/connectors/commands/poll",
        {"connector_id": "connector-1", "cluster_id": "cluster-1"},
        "credential",
    )
    send_notification_request({"event_id": "incident.opened:incident-1:1"})

    assert [(request.get_header("X-request-id"), request.get_header("X-correlation-id")) for request in captured] == [
        ("diagnosis-1", "incident-1"),
        ("tool-1", "incident-1"),
        (captured[2].get_header("X-request-id"), "cluster-1"),
        ("notification-incident.opened:incident-1:1", "incident.opened:incident-1:1"),
    ]
    assert captured[2].get_header("X-request-id").startswith("req-")
