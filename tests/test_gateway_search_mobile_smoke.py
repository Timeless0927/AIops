"""Console Next global search, mobile approval detail, and final Gateway smoke."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.aiops_k8s_gateway import action_control_service
from apps.aiops_k8s_gateway import agent_run_service
from apps.aiops_k8s_gateway import approval_execution_service
from apps.aiops_k8s_gateway import approval_service
from apps.aiops_k8s_gateway import main as gateway_main
from apps.cluster_connector import main as connector_main
from apps.cluster_connector.stream_client import ConnectorRegistration
from toolsets.incident_store import IncidentStore


def _write_identity_config(path: Path) -> None:
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
  ldap:
    enabled: false
  users:
    - username: operator
      password: operator-pass
      display_name: Operator
      roles: [operator]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: approver
      password: approver-pass
      display_name: Approver
      roles: [approver]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: auditor
      password: auditor-pass
      display_name: Auditor
      roles: [auditor]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: outsider
      password: outsider-pass
      display_name: Outsider
      roles: [viewer]
      scope:
        clusters: ["prod-b"]
        services: ["billing"]
        teams: ["finance"]
        namespaces: ["default"]
""",
        encoding="utf-8",
    )


def _request_json(url: str, *, body: dict | None = None, token: str | None = None, method: str = "GET") -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "identity.yaml"
    data_dir = tmp_path / "data"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIOPS_NOTIFICATION_DRY_RUN", "true")

    old_action_db = action_control_service._DB
    old_approval_db = approval_service._DB
    old_execution_db = approval_execution_service._DB
    old_run_db = agent_run_service._DB
    old_audit_db = gateway_main.audit_log._DB
    old_incident_store = gateway_main.incident_store._STORE
    old_notification_center = gateway_main.notification_center._CENTER
    action_control_service._DB = action_control_service.ActionControlDB(data_dir / "actions.db")
    approval_service._DB = approval_service.ApprovalRequestDB(data_dir / "approval_requests.db")
    approval_execution_service._DB = approval_execution_service.ApprovalExecutionDB(data_dir / "approval_executions.db")
    agent_run_service._DB = agent_run_service.AgentRunDB(data_dir / "agent_runs.db")
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(data_dir / "audit_log.db")
    gateway_main.incident_store._STORE = IncidentStore(data_dir / "incidents.db")
    gateway_main.notification_center._CENTER = None
    gateway_main._SESSIONS.clear()
    gateway_main._ROUTES.clear()

    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main._ROUTES.clear()
        action_control_service._DB.close()
        approval_service._DB.close()
        approval_execution_service._DB.close()
        agent_run_service._DB.close()
        gateway_main.audit_log._DB.close()
        gateway_main.incident_store._STORE.close()
        action_control_service._DB = old_action_db
        approval_service._DB = old_approval_db
        approval_execution_service._DB = old_execution_db
        agent_run_service._DB = old_run_db
        gateway_main.audit_log._DB = old_audit_db
        gateway_main.incident_store._STORE = old_incident_store
        gateway_main.notification_center._CENTER = old_notification_center


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password}, method="POST")
    assert status == 200
    return str(payload["token"])


def _fake_kubectl_popen(real_popen):  # noqa: ANN001, ANN202
    def _factory(argv, **kwargs):  # noqa: ANN001, ANN202
        script = "import sys; sys.stdout.write('ok\\n'); sys.exit(0)"
        return real_popen(["python3", "-c", script], stdout=kwargs["stdout"], stderr=kwargs["stderr"])  # noqa: S603

    return _factory


def test_global_search_filters_results_and_final_gateway_smoke(gateway: str, monkeypatch: pytest.MonkeyPatch) -> None:
    operator = _login(gateway, "operator", "operator-pass")
    approver = _login(gateway, "approver", "approver-pass")
    auditor = _login(gateway, "auditor", "auditor-pass")
    outsider = _login(gateway, "outsider", "outsider-pass")
    incident_id = asyncio.run(
        gateway_main.incident_store.create_incident(
            "CheckoutLatencyHigh",
            "default",
            "prod-a",
            "checkout latency rose",
            service="checkout",
            team="payments",
        )
    )
    asyncio.run(
        gateway_main.incident_store.create_incident(
            "BillingLatencyHigh",
            "default",
            "prod-b",
            "billing latency rose",
            service="billing",
            team="finance",
        )
    )
    asyncio.run(gateway_main.incident_store.add_evidence(incident_id, "logs", "ev-1", "checkout errors", payload={"count": 3}))

    workbench_status, workbench = _request_json(f"{gateway}/api/incidents/{incident_id}/workbench", token=operator)
    run_status, run_payload = _request_json(
        f"{gateway}/api/agent-runs",
        token=operator,
        method="POST",
        body={
            "title": "checkout restart investigation",
            "message": "Investigate checkout before restart",
            "incident_id": incident_id,
            "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
        },
    )
    run_id = run_payload["snapshot"]["run"]["run_id"]
    events_status, events = _request_json(f"{gateway}/api/agent-runs/{run_id}/events", token=operator)

    connector_main.ConnectorHandler.registration = ConnectorRegistration(
        connector_id="connector-prod",
        cluster_id="prod-a",
        namespace_scope=("default",),
        capabilities=("execute_read", "execute_mutation"),
    )
    connector_server = ThreadingHTTPServer(("127.0.0.1", 0), connector_main.ConnectorHandler)
    connector_thread = threading.Thread(target=connector_server.serve_forever, daemon=True)
    connector_thread.start()
    monkeypatch.setenv("AIOPS_CONNECTOR_URL", f"http://127.0.0.1:{connector_server.server_address[1]}")
    monkeypatch.setenv("AIOPS_CONNECTOR_ENABLE_MUTATION_EXECUTION", "true")
    real_popen = subprocess.Popen

    try:
        register_status, _ = _request_json(
            f"{gateway}/connectors/register",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "prod-a"},
        )
        propose_status, proposed = _request_json(
            f"{gateway}/api/actions/propose",
            token=operator,
            method="POST",
            body={
                "action_type": "restart_deployment",
                "cluster": "prod-a",
                "namespace": "default",
                "service": "checkout",
                "team": "payments",
                "deployment": "checkout",
                "incident_id": incident_id,
                "run_id": run_id,
                "session_id": run_id,
                "reason": "request service restart",
                "evidence_refs": [{"ref_id": "ev-1", "source": "logs"}],
                "assigned_approvers": ["approver"],
                "idempotency_key": "final-smoke",
            },
        )
        approval_id = proposed["approval_request"]["approval_id"]
        detail_status, approval_detail = _request_json(f"{gateway}/api/approval-requests/{approval_id}", token=approver)
        with patch("apps.cluster_connector.kubectl_executor.subprocess.Popen", side_effect=_fake_kubectl_popen(real_popen)):
            approve_status, approved = _request_json(
                f"{gateway}/api/approval-requests/{approval_id}/approve",
                token=approver,
                method="POST",
                body={"reason": "mobile approval remark"},
            )
        execution_status, execution = _request_json(f"{gateway}/api/approval-requests/{approval_id}/execution", token=approver)
        notifications_status, notifications = _request_json(f"{gateway}/api/notifications", token=approver)
        audit_status, audit = _request_json(f"{gateway}/api/audit/chains", token=auditor)
        audit_detail_status, audit_detail = _request_json(f"{gateway}/api/audit/chains/chain-{approval_id}", token=auditor)
        all_search_status, all_search = _request_json(f"{gateway}/api/search", token=operator)
        search_status, search = _request_json(f"{gateway}/api/search?q=checkout", token=operator)
        outsider_status, outsider_search = _request_json(f"{gateway}/api/search?q=checkout", token=outsider)
    finally:
        connector_server.shutdown()
        connector_server.server_close()
        connector_thread.join(timeout=2)

    assert workbench_status == 200
    assert workbench["workbench"]["incident"]["incident_id"] == incident_id
    assert run_status == 201
    assert events_status == 200
    assert any(event["event_type"] == "run_created" for event in events["events"])
    assert register_status == 201
    assert propose_status == 201
    assert detail_status == 200
    assert approval_detail["approval_request"]["evidence_refs"] == [{"ref_id": "ev-1", "source": "logs"}]
    assert approve_status == 200
    assert approved["execution"]["status"] == "succeeded"
    assert execution_status == 200
    assert execution["execution"]["status"] == "succeeded"
    assert notifications_status == 200
    assert any(item["approval_id"] == approval_id for item in notifications["notifications"])
    assert audit_status == 200
    assert any(chain["approval_id"] == approval_id for chain in audit["chains"])
    assert audit_detail_status == 200
    assert any(item["approval_id"] == approval_id for item in audit_detail["chain"]["notifications"])
    assert all_search_status == 200
    result_types = {result["type"] for result in all_search["results"]}
    assert {"incident", "agent_run", "conversation", "cluster", "namespace", "service", "team"} <= result_types
    assert all(result["route"].startswith(("/incidents/", "/agent-runs/", "/search?")) for result in all_search["results"])
    assert search_status == 200
    assert any(result["type"] == "incident" and result["id"] == incident_id for result in search["results"])
    assert outsider_status == 200
    assert not outsider_search["results"]
