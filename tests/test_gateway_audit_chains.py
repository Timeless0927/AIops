"""Console Next responsibility-chain audit route tests."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import action_control_service
from apps.aiops_k8s_gateway import agent_run_service
from apps.aiops_k8s_gateway import approval_execution_service
from apps.aiops_k8s_gateway import approval_service
from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import notification_center


def _write_identity_config(path: Path) -> None:
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
  ldap:
    enabled: false
  users:
    - username: auditor
      password: auditor-pass
      display_name: Auditor
      roles: [auditor]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: other-auditor
      password: other-pass
      display_name: Other Auditor
      roles: [auditor]
      scope:
        clusters: ["prod-b"]
        services: ["billing"]
        teams: ["finance"]
        namespaces: ["default"]
    - username: operator
      password: operator-pass
      display_name: Operator
      roles: [operator]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: viewer
      password: viewer-pass
      display_name: Viewer
      roles: [viewer]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
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


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password}, method="POST")
    assert status == 200
    return str(payload["token"])


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
    old_notification_center = notification_center._CENTER
    action_control_service._DB = action_control_service.ActionControlDB(data_dir / "actions.db")
    approval_service._DB = approval_service.ApprovalRequestDB(data_dir / "approval_requests.db")
    approval_execution_service._DB = approval_execution_service.ApprovalExecutionDB(data_dir / "approval_executions.db")
    agent_run_service._DB = agent_run_service.AgentRunDB(data_dir / "agent_runs.db")
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(data_dir / "audit_log.db")
    notification_center._CENTER = notification_center.NotificationCenter(
        db=notification_center.NotificationDeliveryDB(data_dir / "notification_deliveries.db"),
        settings=notification_center.NotificationSettings(
            console_base_url="http://console.test",
            max_attempts=1,
            retry_delay_seconds=0,
            channel_config={},
            dry_run=True,
        ),
    )
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
        notification_center._CENTER.db.close()
        action_control_service._DB = old_action_db
        approval_service._DB = old_approval_db
        approval_execution_service._DB = old_execution_db
        agent_run_service._DB = old_run_db
        gateway_main.audit_log._DB = old_audit_db
        notification_center._CENTER = old_notification_center


def _seed_chain() -> tuple[str, str]:
    run = asyncio.run(
        agent_run_service.create_run(
            {
                "title": "checkout run",
                "message": "Investigate checkout",
                "incident_id": "inc-audit",
                "scope": {
                    "cluster": "prod-a",
                    "namespace": "default",
                    "service": "checkout",
                    "team": "payments",
                },
            },
            actor_id="operator",
        )
    )
    run_id = str(run["run"]["run_id"])
    action, _ = action_control_service.create_proposal(
        {
            "action_type": "restart_deployment",
            "cluster": "prod-a",
            "namespace": "default",
            "service": "checkout",
            "team": "payments",
            "deployment": "checkout",
            "incident_id": "inc-audit",
            "session_id": run_id,
            "run_id": run_id,
            "reason": "restart after error spike",
            "evidence_refs": [{"ref_id": "ev-1", "source": "logs"}],
            "idempotency_key": "idem-audit-action",
        },
        actor_id="operator",
        request_id="req-action",
        policy={"decision": "approval_required", "reason": "prod requires approval"},
        policy_hit={"id": 7},
    )
    approval, _ = approval_service.create_request(
        {
            "incident_id": "inc-audit",
            "session_id": run_id,
            "action_proposal_id": action["action_proposal_id"],
            "risk_level": "low",
            "requested_by": "operator",
            "reason": "restart after error spike",
            "action_summary": "restart checkout deployment",
            "resource_scope": {
                "cluster": "prod-a",
                "namespace": "default",
                "service": "checkout",
                "team": "payments",
            },
            "rollback_plan": "rollout undo deployment/checkout",
            "evidence_refs": [{"ref_id": "ev-1", "source": "logs"}],
            "audit_refs": [],
            "assigned_approvers": ["approver"],
        },
        actor_id="operator",
        request_id="req-approval",
    )
    approval, _ = approval_service.decide(
        str(approval["approval_id"]),
        decision=approval_service.APPROVED,
        actor_id="approver",
        reason="looks safe",
        request_id="req-approve",
    )
    execution, _ = approval_execution_service.create_or_replay(
        approval,
        {
            "idempotency_key": "idem-exec",
            "cluster_id": "prod-a",
            "namespace": "default",
            "argv": ["kubectl", "rollout", "restart", "deployment/checkout"],
            "preflight_argv": ["kubectl", "get", "deployment/checkout"],
            "post_check_argv": ["kubectl", "rollout", "status", "deployment/checkout"],
        },
        actor_id="approver",
    )
    approval_execution_service.update_execution(
        str(approval["approval_id"]),
        "succeeded",
        preflight_result={"status": "succeeded"},
        execution_result={"status": "succeeded"},
        post_check_result={"status": "succeeded"},
    )
    notification_center.send_notification(
        {
            "notification_type": "execution_result",
            "notification_id": "ntf-audit",
            "incident_id": "inc-audit",
            "approval_id": approval["approval_id"],
            "summary": "execution succeeded",
            "dedupe_key": "audit-chain-notification",
        }
    )
    asyncio.run(
        gateway_main.audit_log.record_audit(
            "operator",
            "approval_create",
            cluster="prod-a",
            namespace="default",
            trigger="gateway",
            tool_level="control-plane",
            tool_name="gateway",
            result="success",
            incident_id="inc-audit",
            approval_id=approval["approval_id"],
            action_proposal_id=action["action_proposal_id"],
        )
    )
    return str(approval["approval_id"]), run_id


def test_audit_chains_detail_raw_tombstones_and_scope_filtering(gateway: str) -> None:
    approval_id, run_id = _seed_chain()
    auditor = _login(gateway, "auditor", "auditor-pass")
    other = _login(gateway, "other-auditor", "other-pass")
    operator = _login(gateway, "operator", "operator-pass")
    viewer = _login(gateway, "viewer", "viewer-pass")

    delete_status, delete_payload = _request_json(
        f"{gateway}/api/agent-runs/{run_id}/delete",
        body={"reason": "retention cleanup"},
        token=operator,
        method="POST",
    )
    list_status, listed = _request_json(f"{gateway}/api/audit/chains", token=auditor)
    detail_status, detail = _request_json(f"{gateway}/api/audit/chains/chain-{approval_id}", token=auditor)
    raw_status, raw = _request_json(f"{gateway}/api/audit/raw?cluster=prod-a&namespace=default", token=auditor)
    tombstone_status, tombstone_payload = _request_json(f"{gateway}/api/audit/tombstones", token=auditor)
    hidden_list_status, hidden_list = _request_json(f"{gateway}/api/audit/chains", token=other)
    hidden_detail_status, hidden_detail = _request_json(f"{gateway}/api/audit/chains/chain-{approval_id}", token=other)
    forbidden_status, forbidden = _request_json(f"{gateway}/api/audit/chains", token=viewer)

    assert delete_status == 200
    assert delete_payload["snapshot"]["conversation"]["status"] == "deleted"
    assert list_status == 200
    assert [chain["chain_id"] for chain in listed["chains"]] == [f"chain-{approval_id}"]
    assert detail_status == 200
    chain = detail["chain"]
    assert chain["approval_id"] == approval_id
    assert chain["agent_request"]["reason"] == "restart after error spike"
    assert chain["frozen_action"]["action_hash"]
    assert chain["approver_snapshot"]["approved_by"] == "approver"
    assert chain["execution"]["status"] == "succeeded"
    assert chain["immutable_records"]["action_request"]["reason"] == "restart after error spike"
    assert chain["immutable_records"]["risk_classification"]["risk_level"] == "low"
    assert chain["immutable_records"]["evidence_refs"] == [{"ref_id": "ev-1", "source": "logs"}]
    assert chain["immutable_records"]["approval_request"]["approval_id"] == approval_id
    assert chain["immutable_records"]["approval_decision"]["approved_by"] == "approver"
    assert chain["immutable_records"]["approver_identity_snapshot"]["approved_by"] == "approver"
    assert chain["immutable_records"]["frozen_action_hash"] == chain["frozen_action"]["action_hash"]
    assert chain["immutable_records"]["execution_record"]["status"] == "succeeded"
    assert chain["immutable_records"]["preflight_result"] == {"status": "succeeded"}
    assert chain["immutable_records"]["mutation_result"] == {"status": "succeeded"}
    assert chain["immutable_records"]["post_check_result"] == {"status": "succeeded"}
    assert chain["notifications"][0]["notification_type"] == "execution_result"
    assert chain["delete_tombstones"][0]["reason"] == "retention cleanup"
    assert chain["delete_tombstones"][0]["linked_approval_ids"] == [approval_id]
    assert any(row["what"] == "approval_create" for row in chain["raw_audit_refs"])
    assert raw_status == 200
    assert any(row["what"] == "approval_create" for row in raw["rows"])
    assert tombstone_status == 200
    assert tombstone_payload["tombstones"][0]["linked_execution_ids"]
    assert hidden_list_status == 200
    assert hidden_list["chains"] == []
    assert hidden_detail_status == 404
    assert hidden_detail["error"]["code"] == "not_found"
    assert forbidden_status == 403
    assert forbidden["error"]["code"] == "forbidden"
