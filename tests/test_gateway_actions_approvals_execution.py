"""Console Next structured action, approval grant, execution, and lock tests."""

from __future__ import annotations

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
""",
        encoding="utf-8",
    )


def _request_json(url: str, *, body: dict | None = None, token: str | None = None, method: str = "POST") -> tuple[int, dict]:
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
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIOPS_NOTIFICATION_DRY_RUN", "true")

    old_action_db = action_control_service._DB
    old_run_db = agent_run_service._DB
    old_approval_db = approval_service._DB
    old_execution_db = approval_execution_service._DB
    old_audit_db = gateway_main.audit_log._DB
    old_incident_store = gateway_main.incident_store._STORE
    old_notification_center = gateway_main.notification_center._CENTER
    action_control_service._DB = action_control_service.ActionControlDB(tmp_path / "actions.db")
    agent_run_service._DB = agent_run_service.AgentRunDB(tmp_path / "agent_runs.db")
    approval_service._DB = approval_service.ApprovalRequestDB(tmp_path / "approval_requests.db")
    approval_execution_service._DB = approval_execution_service.ApprovalExecutionDB(tmp_path / "approval_executions.db")
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(tmp_path / "audit_log.db")
    gateway_main.incident_store._STORE = IncidentStore(tmp_path / "incidents.db")
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
        agent_run_service._DB.close()
        approval_service._DB.close()
        approval_execution_service._DB.close()
        gateway_main.audit_log._DB.close()
        gateway_main.incident_store._STORE.close()
        action_control_service._DB = old_action_db
        agent_run_service._DB = old_run_db
        approval_service._DB = old_approval_db
        approval_execution_service._DB = old_execution_db
        gateway_main.audit_log._DB = old_audit_db
        gateway_main.incident_store._STORE = old_incident_store
        gateway_main.notification_center._CENTER = old_notification_center


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password})
    assert status == 200
    return str(payload["token"])


def _action_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action_type": "restart_deployment",
        "cluster": "prod-a",
        "namespace": "default",
        "service": "checkout",
        "team": "payments",
        "deployment": "checkout",
        "incident_id": "inc-action",
        "session_id": "sess-action",
        "reason": "restart checkout after error spike",
        "assigned_approvers": ["approver"],
        "idempotency_key": "idem-action",
    }
    payload.update(overrides)
    return payload


def _fake_kubectl_popen(real_popen):  # noqa: ANN001, ANN202
    def _factory(argv, **kwargs):  # noqa: ANN001, ANN202
        script = "import sys; sys.stdout.write('ok\\n'); sys.exit(0)"
        return real_popen(["python3", "-c", script], stdout=kwargs["stdout"], stderr=kwargs["stderr"])  # noqa: S603

    return _factory


def test_action_proposal_approval_auto_executes_once(gateway: str, monkeypatch: pytest.MonkeyPatch) -> None:
    operator = _login(gateway, "operator", "operator-pass")
    approver = _login(gateway, "approver", "approver-pass")
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
        _request_json(
            f"{gateway}/connectors/register",
            body={
                "connector_id": "connector-prod",
                "cluster_id": "prod-a",
                "namespace_scope": ["default"],
                "capabilities": ["execute_read", "execute_mutation"],
            },
        )
        missing_status, missing = _request_json(
            f"{gateway}/api/actions/propose",
            body=_action_payload(deployment=""),
            token=operator,
        )
        propose_status, proposed = _request_json(f"{gateway}/api/actions/propose", body=_action_payload(), token=operator)
        approval_id = proposed["approval_request"]["approval_id"]
        action_id = proposed["action"]["action_id"]
        with patch("apps.cluster_connector.kubectl_executor.subprocess.Popen", side_effect=_fake_kubectl_popen(real_popen)) as popen:
            approve_status, approved = _request_json(
                f"{gateway}/api/approval-requests/{approval_id}/approve",
                body={"reason": "approved"},
                token=approver,
            )
            repeat_status, repeated = _request_json(
                f"{gateway}/api/approval-requests/{approval_id}/approve",
                body={"reason": "approved again"},
                token=approver,
            )
        detail_status, detail = _request_json(f"{gateway}/api/actions/{action_id}", token=operator, method="GET")

        assert missing_status == 400
        assert missing["error"]["code"] == "target_required"
        assert propose_status == 201
        assert proposed["policy"]["decision"] == "approval_required"
        assert proposed["action"]["action_hash"]
        assert proposed["approval_request"]["action_proposal_id"] == proposed["action"]["action_proposal_id"]
        assert approve_status == 200
        assert approved["approval_request"]["status"] == "approved"
        assert approved["execution"]["status"] == "succeeded"
        assert popen.call_count == 3
        assert repeat_status == 200
        assert repeated["idempotent"] is True
        assert repeated["execution"] is None
        assert detail_status == 200
        assert detail["execution"]["status"] == "succeeded"
    finally:
        connector_server.shutdown()
        connector_server.server_close()
        connector_thread.join(timeout=2)


def test_chat_action_request_clarifies_then_auto_executes_approved_action(gateway: str, monkeypatch: pytest.MonkeyPatch) -> None:
    operator = _login(gateway, "operator", "operator-pass")
    approver = _login(gateway, "approver", "approver-pass")
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
        _request_json(
            f"{gateway}/connectors/register",
            body={
                "connector_id": "connector-prod",
                "cluster_id": "prod-a",
                "namespace_scope": ["default"],
                "capabilities": ["execute_read", "execute_mutation"],
            },
        )
        run_status, run_payload = _request_json(
            f"{gateway}/api/agent-runs",
            body={
                "title": "Checkout chat action",
                "message": "investigate checkout",
                "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
            },
            token=operator,
        )
        run_id = run_payload["snapshot"]["run"]["run_id"]
        ambiguous_status, ambiguous = _request_json(
            f"{gateway}/api/agent-runs/{run_id}/messages",
            body={"message": "重启 checkout 服务或 billing 服务"},
            token=operator,
        )
        missing_type_status, missing_type = _request_json(
            f"{gateway}/api/agent-runs/{run_id}/messages",
            body={"message": "对 prod-a/default 的 checkout 服务执行操作"},
            token=operator,
        )
        complete_status, complete = _request_json(
            f"{gateway}/api/agent-runs/{run_id}/messages",
            body={"message": "重启 prod-a/default 的 checkout 服务"},
            token=operator,
        )
        action_result = complete["action_result"]
        approval_id = action_result["approval_request"]["approval_id"]
        with patch("apps.cluster_connector.kubectl_executor.subprocess.Popen", side_effect=_fake_kubectl_popen(real_popen)):
            approve_status, approved = _request_json(
                f"{gateway}/api/approval-requests/{approval_id}/approve",
                body={"reason": "approved from chat"},
                token=approver,
            )
        snapshot_status, snapshot = _request_json(f"{gateway}/api/agent-runs/{run_id}", token=operator, method="GET")
        event_types = {event["event_type"] for event in snapshot["snapshot"]["timeline"]}

        assert run_status == 201
        assert ambiguous_status == 200
        assert ambiguous["agent_event"]["event_type"] == "agent_clarification_requested"
        assert ambiguous["action_result"]["clarification_required"] is True
        assert missing_type_status == 200
        assert "action_type" in missing_type["action_result"]["missing_fields"]
        assert complete_status == 200
        assert action_result["policy"]["decision"] == "approval_required"
        assert action_result["action"]["requested_by"] == "operator"
        assert action_result["action"]["agent_id"] == "console-next-chat-agent"
        assert action_result["action"]["run_id"] == run_id
        assert action_result["approval_request"]["requested_by"] == "operator"
        assert approve_status == 200
        assert approved["approval_request"]["approved_by"] == "approver"
        assert approved["execution"]["executor_id"] == "gateway"
        assert approved["execution"]["status"] == "succeeded"
        assert snapshot_status == 200
        assert {
            "agent_clarification_requested",
            "risk_classified",
            "approval_requested",
            "execution_started",
            "preflight_finished",
            "mutation_finished",
            "post_check_finished",
        } <= event_types
    finally:
        connector_server.shutdown()
        connector_server.server_close()
        connector_thread.join(timeout=2)


def test_action_grant_and_target_lock_are_single_use(tmp_path: Path) -> None:
    db = action_control_service.ActionControlDB(tmp_path / "actions.db")
    old_db = action_control_service._DB
    action_control_service._DB = db
    try:
        action, _ = action_control_service.create_proposal(
            _action_payload(),
            actor_id="operator",
            request_id="req-test",
            policy={"decision": "approval_required", "reason": "prod_requires_approval"},
            policy_hit={"id": 1},
        )
        grant, _ = action_control_service.create_grant(action, grant_type="human_approval", source_id="ap-1", actor_id="approver")
        consumed = action_control_service.consume_grant(grant["grant_id"], action_hash=action["action_hash"], execution_id="exec-1")
        action_control_service.claim_lock("kubernetes:prod-a:default:deployment/checkout", action_id=action["action_id"], execution_id="exec-1", owner="approver")

        with pytest.raises(action_control_service.ActionControlError, match="grant has already been consumed"):
            action_control_service.consume_grant(grant["grant_id"], action_hash=action["action_hash"], execution_id="exec-2")
        with pytest.raises(action_control_service.ActionControlError, match="target is locked"):
            action_control_service.claim_lock("kubernetes:prod-a:default:deployment/checkout", action_id=action["action_id"], execution_id="exec-2", owner="approver")

        action_control_service.release_lock("kubernetes:prod-a:default:deployment/checkout", "exec-1")
        action_control_service.claim_lock("kubernetes:prod-a:default:deployment/checkout", action_id=action["action_id"], execution_id="exec-2", owner="approver")
        assert consumed["status"] == "consumed"
    finally:
        action_control_service._DB = old_db
        db.close()
