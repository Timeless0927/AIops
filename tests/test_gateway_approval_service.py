"""Gateway approval service API tests."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.cluster_connector import main as connector_main
from apps.cluster_connector.stream_client import ConnectorRegistration
from apps.aiops_k8s_gateway import approval_execution_service
from apps.aiops_k8s_gateway import approval_service
from apps.aiops_k8s_gateway import main as gateway_main
from toolsets.incident_store import IncidentStore


def _write_identity_config(path: Path) -> None:
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
  ldap:
    enabled: false
  users:
    - username: alice
      password: alice-pass
      display_name: Alice
      roles: [user]
      scope:
        services: ["checkout-api"]
        teams: ["payments"]
        namespaces: ["default", "staging"]
    - username: bob
      password: bob-pass
      display_name: Bob
      roles: [oncall_approver]
      scope:
        services: ["checkout-api"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: carol
      password: carol-pass
      display_name: Carol
      roles: [oncall_approver]
      scope:
        services: ["billing-api"]
        teams: ["finance"]
        namespaces: ["default"]
    - username: dave
      password: dave-pass
      display_name: Dave
      roles: [user]
      scope:
        services: ["billing-api"]
        teams: ["finance"]
        namespaces: ["default"]
    - username: admin
      password: admin-pass
      display_name: Admin
      roles: [admin]
      scope:
        services: ["*"]
        teams: ["*"]
        namespaces: ["*"]
""",
        encoding="utf-8",
    )


def _request_json(
    url: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    method: str = "POST",
) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _approval_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "incident_id": "inc-1",
        "session_id": "sess-1",
        "action_proposal_id": "act-1",
        "risk_level": "high",
        "requested_by": "diagnosis",
        "reason": "restart checkout-api to recover 5xx",
        "action_summary": "rollout restart deployment/checkout-api",
        "resource_scope": {
            "cluster_id": "cluster-local",
            "service_id": "checkout-api",
            "team_id": "payments",
            "namespace": "default",
        },
        "rollback_plan": "kubectl rollout undo deployment/checkout-api -n default",
        "evidence_refs": [{"type": "prometheus", "ref": "5xx_rate"}],
        "idempotency_key": "idem-act-1",
        "assigned_approvers": ["bob"],
    }
    override_scope = overrides.pop("resource_scope", None)
    if isinstance(override_scope, dict) and isinstance(payload["resource_scope"], dict):
        payload["resource_scope"] = {**payload["resource_scope"], **override_scope}
    payload.update(overrides)
    return payload


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIOPS_NOTIFICATION_DRY_RUN", "true")
    monkeypatch.setenv("AIOPS_CONSOLE_BASE_URL", "https://console.example.test")
    monkeypatch.setenv(
        "AIOPS_NOTIFICATION_CHANNELS_JSON",
        json.dumps(
            {
                "default_team_id": "payments",
                "teams": {"payments": {"feishu_chat_id": "oc_payments"}},
                "services": {"checkout-api": {"team_id": "payments"}},
            }
        ),
    )

    old_approval_db = approval_service._DB
    old_execution_db = approval_execution_service._DB
    old_notification_center = gateway_main.notification_center._CENTER
    old_audit_db = gateway_main.audit_log._DB
    old_incident_store = gateway_main.incident_store._STORE
    approval_service._DB = approval_service.ApprovalRequestDB(tmp_path / "approval_requests.db")
    approval_execution_service._DB = approval_execution_service.ApprovalExecutionDB(tmp_path / "approval_executions.db")
    gateway_main.notification_center._CENTER = None
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(tmp_path / "audit_log.db")
    gateway_main.incident_store._STORE = IncidentStore(tmp_path / "incidents.db")
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        approval_service._DB.close()
        approval_execution_service._DB.close()
        approval_service._DB = old_approval_db
        approval_execution_service._DB = old_execution_db
        gateway_main.notification_center._CENTER = old_notification_center
        gateway_main.audit_log._DB.close()
        gateway_main.audit_log._DB = old_audit_db
        gateway_main.incident_store._STORE.close()
        gateway_main.incident_store._STORE = old_incident_store


def _login(gateway_url: str, username: str, password: str) -> str:
    status, payload = _request_json(
        f"{gateway_url}/auth/login",
        body={"username": username, "password": password},
    )
    assert status == 200
    return str(payload["token"])


def test_create_query_approve_and_audit_contract(gateway: str) -> None:
    alice = _login(gateway, "alice", "alice-pass")
    bob = _login(gateway, "bob", "bob-pass")

    create_status, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(),
        token=alice,
    )
    approval = created["approval_request"]
    repeat_status, repeated = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(reason="duplicate should not create a new row"),
        token=alice,
    )
    query_status, queried = _request_json(
        f"{gateway}/api/approval-requests?incident_id=inc-1&session_id=sess-1&action_proposal_id=act-1",
        token=alice,
        method="GET",
    )
    detail_status, detail = _request_json(
        f"{gateway}/api/approval-requests/{approval['approval_id']}",
        token=alice,
        method="GET",
    )
    empty_approve_status, empty_approve = _request_json(
        f"{gateway}/api/approval-requests/{approval['approval_id']}/approve",
        body={},
        token=bob,
    )
    approve_status, approved = _request_json(
        f"{gateway}/api/approval-requests/{approval['approval_id']}/approve",
        body={"reason": "approved for remediation"},
        token=bob,
    )
    repeat_approve_status, repeat_approved = _request_json(
        f"{gateway}/api/approval-requests/{approval['approval_id']}/approve",
        body={"reason": "approved again"},
        token=bob,
    )
    rows = asyncio.run(gateway_main.audit_log.query_audit(limit=100))
    deliveries = gateway_main.notification_center.list_deliveries(limit=20)

    assert create_status == 201
    assert repeat_status == 200
    assert repeated["idempotent"] is True
    assert repeated["approval_request"]["approval_id"] == approval["approval_id"]
    assert query_status == 200
    assert [item["approval_id"] for item in queried["approval_requests"]] == [approval["approval_id"]]
    assert detail_status == 200
    assert detail["approval_request"]["action_proposal_id"] == "act-1"
    assert detail["approval_request"]["notification_status"] == "sent"
    assert any(item["notification_type"] == "approval_pending" for item in deliveries)
    assert any(item["notification_type"] == "approval_approved" for item in deliveries)
    assert empty_approve_status == 400
    assert empty_approve["error"]["code"] == "invalid_request"
    assert approve_status == 200
    assert approved["approval_request"]["status"] == "approved"
    assert approved["approval_request"]["approved_by"] == "bob"
    assert approved["approval_request"]["execution_grant"]["action_proposal_id"] == "act-1"
    assert repeat_approve_status == 200
    assert repeat_approved["idempotent"] is True
    audit = {(row["what"], row["approval_id"], row["action_proposal_id"], row["decision"]) for row in rows}
    assert ("approval_create", approval["approval_id"], "act-1", "allow") in audit
    assert ("approval_approve", approval["approval_id"], "act-1", "approved") in audit


def test_no_approver_creation_emits_blocked_notification(gateway: str) -> None:
    alice = _login(gateway, "alice", "alice-pass")

    status, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(action_proposal_id="act-no-approver", idempotency_key="idem-no-approver", assigned_approvers=[]),
        token=alice,
    )
    approval = created["approval_request"]
    deliveries = gateway_main.notification_center.list_deliveries(notification_type="blocked:no_approver")

    assert status == 201
    assert approval["notification_status"] == "sent"
    assert deliveries[0]["approval_id"] == approval["approval_id"]
    assert deliveries[0]["card"]["elements"][1]["actions"][0]["url"] == f"https://console.example.test/approvals/{approval['approval_id']}"


def test_same_idempotency_key_with_different_action_proposal_conflicts(gateway: str) -> None:
    alice = _login(gateway, "alice", "alice-pass")
    _, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(),
        token=alice,
    )

    status, conflict = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(action_proposal_id="act-2", idempotency_key="idem-act-1"),
        token=alice,
    )

    assert created["approval_request"]["approval_id"]
    assert status == 409
    assert conflict["error"]["code"] == "idempotency_conflict"
    assert "approval_request" not in conflict


def test_same_idempotency_key_with_different_resource_scope_conflicts(gateway: str) -> None:
    alice = _login(gateway, "alice", "alice-pass")
    _, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(),
        token=alice,
    )

    status, conflict = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(
            resource_scope={
                "service_id": "checkout-api",
                "team_id": "payments",
                "namespace": "staging",
            },
        ),
        token=alice,
    )

    assert created["approval_request"]["approval_id"]
    assert status == 409
    assert conflict["error"]["code"] == "idempotency_conflict"
    assert "approval_request" not in conflict


def test_same_action_proposal_with_different_key_or_payload_conflicts(gateway: str) -> None:
    alice = _login(gateway, "alice", "alice-pass")
    _, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(),
        token=alice,
    )

    key_status, key_conflict = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(idempotency_key="idem-different"),
        token=alice,
    )
    payload_status, payload_conflict = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(idempotency_key="idem-act-1", action_summary="different summary"),
        token=alice,
    )

    assert created["approval_request"]["approval_id"]
    assert key_status == 409
    assert key_conflict["error"]["code"] == "idempotency_conflict"
    assert "approval_request" not in key_conflict
    assert payload_status == 409
    assert payload_conflict["error"]["code"] == "idempotency_conflict"
    assert "approval_request" not in payload_conflict


def test_cross_scope_idempotent_replay_uses_stored_scope_and_does_not_leak_payload(gateway: str) -> None:
    dave = _login(gateway, "dave", "dave-pass")
    alice = _login(gateway, "alice", "alice-pass")
    billing_payload = _approval_payload(
        incident_id="inc-billing",
        session_id="sess-billing",
        action_proposal_id="act-billing",
        idempotency_key="idem-cross-scope",
        action_summary="restart billing-api",
        resource_scope={
            "service_id": "billing-api",
            "team_id": "finance",
            "namespace": "default",
        },
        rollback_plan="kubectl rollout undo deployment/billing-api -n default",
        assigned_approvers=["carol"],
    )
    _, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=billing_payload,
        token=dave,
    )

    status, replay = _request_json(
        f"{gateway}/api/approval-requests",
        body={
            **billing_payload,
                "resource_scope": {
                    "cluster_id": "cluster-local",
                    "service_id": "checkout-api",
                    "team_id": "payments",
                    "namespace": "default",
                },
        },
        token=alice,
    )

    assert created["approval_request"]["resource_scope"]["service_id"] == "billing-api"
    assert status == 409
    assert replay["error"]["code"] == "idempotency_conflict"
    assert "approval_request" not in replay
    assert "billing-api" not in json.dumps(replay, ensure_ascii=False)


def test_create_handler_final_authorization_uses_stored_approval_scope(
    gateway: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = _login(gateway, "alice", "alice-pass")
    hidden = {
        "approval_id": "ap-hidden",
        "incident_id": "inc-hidden",
        "session_id": "sess-hidden",
        "action_proposal_id": "act-hidden",
        "status": "pending",
        "risk_level": "high",
        "requested_by": "diagnosis",
        "requested_at": time.time(),
        "assigned_approvers": ["carol"],
        "approver_policy_ref": None,
        "approved_by": None,
        "rejected_by": None,
        "decided_at": None,
        "decision_reason": None,
        "expires_at": None,
        "action_summary": "restart billing-api",
        "resource_scope": {"service_id": "billing-api", "team_id": "finance", "namespace": "default"},
        "rollback_plan": "rollback hidden",
        "evidence_refs": [],
        "audit_refs": [],
        "idempotency_key": "idem-hidden",
        "notification_status": "sent",
        "notification_delivery_id": "delivery-hidden",
        "notification_error": None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "execution_grant": None,
    }

    def _fake_create_request(payload: dict, *, actor_id: str, request_id: str):  # noqa: ANN001
        assert payload["resource_scope"]["service_id"] == "checkout-api"
        assert actor_id == "alice"
        assert request_id.startswith("req-")
        return hidden, True

    monkeypatch.setattr(gateway_main.approval_service, "create_request", _fake_create_request)

    status, replay = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(
            action_proposal_id="act-hidden",
            idempotency_key="idem-hidden",
            resource_scope={"service_id": "checkout-api", "team_id": "payments", "namespace": "default"},
        ),
        token=alice,
    )

    assert status == 403
    assert replay["error"]["code"] == "forbidden"
    assert "approval_request" not in replay
    assert "billing-api" not in json.dumps(replay, ensure_ascii=False)


def test_reject_requires_reason_and_terminal_states_do_not_grant_execution(gateway: str) -> None:
    alice = _login(gateway, "alice", "alice-pass")
    bob = _login(gateway, "bob", "bob-pass")
    _, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(action_proposal_id="act-reject", idempotency_key="idem-reject"),
        token=alice,
    )
    approval_id = created["approval_request"]["approval_id"]

    missing_reason_status, missing_reason = _request_json(
        f"{gateway}/api/approval-requests/{approval_id}/reject",
        body={},
        token=bob,
    )
    reject_status, rejected = _request_json(
        f"{gateway}/api/approval-requests/{approval_id}/reject",
        body={"reason": "risk is too high"},
        token=bob,
    )
    approve_after_reject_status, approve_after_reject = _request_json(
        f"{gateway}/api/approval-requests/{approval_id}/approve",
        body={"reason": "approve after reject should fail"},
        token=bob,
    )

    assert missing_reason_status == 400
    assert missing_reason["error"]["code"] == "invalid_request"
    assert reject_status == 200
    assert rejected["approval_request"]["status"] == "rejected"
    assert rejected["approval_request"]["execution_grant"] is None
    assert approve_after_reject_status == 409
    assert approve_after_reject["error"]["code"] == "invalid_state_transition"
    assert approve_after_reject["approval_request"]["status"] == "rejected"


def test_rbac_scope_denies_wrong_team_approver(gateway: str) -> None:
    alice = _login(gateway, "alice", "alice-pass")
    carol = _login(gateway, "carol", "carol-pass")
    _, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(action_proposal_id="act-rbac", idempotency_key="idem-rbac"),
        token=alice,
    )
    approval_id = created["approval_request"]["approval_id"]

    status, payload = _request_json(
        f"{gateway}/api/approval-requests/{approval_id}/approve",
        body={},
        token=carol,
    )
    rows = asyncio.run(gateway_main.audit_log.query_audit(limit=20))
    approval_audit = next(row for row in rows if row["what"] == "approval_authorize")

    assert status == 403
    assert payload["error"]["code"] == "forbidden"
    assert approval_audit["approval_id"] == approval_id
    assert approval_audit["incident_id"] == "inc-1"
    assert approval_audit["action_proposal_id"] == "act-rbac"
    assert approval_audit["actor"] == "carol"
    assert approval_audit["role"] == "approver,operator"
    assert approval_audit["request_id"] == payload["request_id"]
    assert approval_audit["permission"] == "approve_action"
    assert approval_audit["decision"] == "deny"
    assert approval_audit["result"] == "forbidden"
    assert "checkout-api" in str(approval_audit["resource_scope"])


def test_expired_approval_cannot_be_approved_and_feishu_cannot_mutate_state(gateway: str) -> None:
    alice = _login(gateway, "alice", "alice-pass")
    bob = _login(gateway, "bob", "bob-pass")
    _, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(
            action_proposal_id="act-expired",
            idempotency_key="idem-expired",
            expires_at=time.time() - 1,
        ),
        token=alice,
    )
    approval_id = created["approval_request"]["approval_id"]

    approve_status, approved = _request_json(
        f"{gateway}/api/approval-requests/{approval_id}/approve",
        body={"reason": "expired approval should fail"},
        token=bob,
    )
    feishu_status, _ = _request_json(
        f"{gateway}/webhooks/feishu/approval",
        body={"approval_id": approval_id, "decision": "approved"},
        token=bob,
    )
    _, detail = _request_json(
        f"{gateway}/api/approval-requests/{approval_id}",
        token=alice,
        method="GET",
    )

    assert approve_status == 409
    assert approved["error"]["code"] == "approval_expired"
    assert approved["approval_request"]["status"] == "expired"
    assert feishu_status == 404
    assert detail["approval_request"]["status"] == "expired"
    assert detail["approval_request"]["execution_grant"] is None


def _execution_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "cluster_id": "cluster-local",
        "namespace": "default",
        "idempotency_key": "exec-act-1",
        "argv": ["kubectl", "rollout", "restart", "deployment/checkout-api", "-n", "default"],
        "preflight_argv": ["kubectl", "get", "deployment/checkout-api", "-n", "default"],
        "post_check_argv": ["kubectl", "rollout", "status", "deployment/checkout-api", "-n", "default"],
        "reason": "approved controlled mutation test",
        "task_id": "task-exec",
        "command_id": "cmd-exec",
    }
    payload.update(overrides)
    return payload


def _fake_kubectl_popen(real_popen, *, fail_post_check: bool = False):  # noqa: ANN001, ANN202
    def _factory(argv, **kwargs):  # noqa: ANN001, ANN202
        is_post_check = "rollout" in argv and "status" in argv
        code = 1 if fail_post_check and is_post_check else 0
        stdout = "ok\\n" if code == 0 else ""
        stderr = "" if code == 0 else "deployment unavailable\\n"
        script = f"import sys; sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); sys.exit({code})"
        return real_popen(  # noqa: S603
            ["python3", "-c", script],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )

    return _factory


def _create_approved_execution_grant(gateway: str) -> tuple[str, str]:
    incident_id = asyncio.run(
        gateway_main.incident_store.create_incident(
            "CheckoutUnavailable",
            "default",
            "cluster-local",
            "checkout unavailable",
            service="checkout-api",
            team="payments",
        )
    )
    alice = _login(gateway, "alice", "alice-pass")
    bob = _login(gateway, "bob", "bob-pass")
    _, created = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(
            incident_id=incident_id,
            risk_level="low",
            action_proposal_id="act-exec",
            idempotency_key="idem-exec",
        ),
        token=alice,
    )
    approval_id = created["approval_request"]["approval_id"]
    approve_status, _ = _request_json(
        f"{gateway}/api/approval-requests/{approval_id}/approve",
        body={"reason": "approved for execution"},
        token=bob,
    )
    assert approve_status == 200
    return incident_id, approval_id


def test_approved_mutation_execution_runs_preflight_postcheck_and_is_idempotent(
    gateway: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _login(gateway, "admin", "admin-pass")
    incident_id, approval_id = _create_approved_execution_grant(gateway)
    connector_main.ConnectorHandler.registration = ConnectorRegistration(
        connector_id="connector-local",
        cluster_id="cluster-local",
        namespace_scope=("default",),
        capabilities=("execute_read", "execute_mutation"),
    )
    connector_main.ConnectorHandler.gateway_url = ""
    connector_main.ConnectorHandler.registered_with_gateway = False
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
                "connector_id": "connector-local",
                "cluster_id": "cluster-local",
                "namespace_scope": ["default"],
                "capabilities": ["execute_read", "execute_mutation"],
            },
        )
        unauthorized_status, unauthorized_execution = _request_json(
            f"{gateway}/api/approval-requests/{approval_id}/execution",
            method="GET",
        )
        before_status, before_execution = _request_json(
            f"{gateway}/api/approval-requests/{approval_id}/execution",
            token=admin,
            method="GET",
        )
        with patch(
            "apps.cluster_connector.kubectl_executor.subprocess.Popen",
            side_effect=_fake_kubectl_popen(real_popen),
        ) as popen:
            execute_status, executed = _request_json(
                f"{gateway}/api/approval-requests/{approval_id}/execute",
                body=_execution_payload(),
                token=admin,
            )
            replay_status, replayed = _request_json(
                f"{gateway}/api/approval-requests/{approval_id}/execute",
                body=_execution_payload(),
                token=admin,
            )

        rows = asyncio.run(gateway_main.audit_log.query_audit(limit=50))
        timeline = asyncio.run(gateway_main.incident_store.get_timeline(incident_id))

        assert execute_status == 200
        assert unauthorized_status == 401
        assert unauthorized_execution["error"]["code"] == "unauthorized"
        assert before_status == 200
        assert before_execution["execution"] is None
        assert before_execution["execution_grant"]["approval_id"] == approval_id
        assert executed["execution"]["status"] == "succeeded"
        assert replay_status == 200
        assert replayed["idempotent"] is True
        assert popen.call_count == 3
        read_status, read_execution = _request_json(
            f"{gateway}/api/approval-requests/{approval_id}/execution",
            token=admin,
            method="GET",
        )
        assert read_status == 200
        assert read_execution["execution"]["status"] == "succeeded"
        assert read_execution["execution"]["preflight_result"]["status"] == "succeeded"
        assert read_execution["execution"]["execution_result"]["status"] == "succeeded"
        assert read_execution["execution"]["post_check_result"]["status"] == "succeeded"
        assert {"approval_execute_preflight", "approval_execute_mutation", "approval_execute_post_check"} <= {
            row["what"] for row in rows
        }
        assert {"approval_requested", "approval_approved", "remediate_executed", "remediate_verified"} <= {
            event["event_type"] for event in timeline
        }
    finally:
        connector_server.shutdown()
        connector_server.server_close()
        connector_thread.join(timeout=2)
        gateway_main._ROUTES.clear()


def test_mutation_execution_fail_closed_cases(gateway: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOPS_CONNECTOR_URL", raising=False)
    admin = _login(gateway, "admin", "admin-pass")
    alice = _login(gateway, "alice", "alice-pass")
    bob = _login(gateway, "bob", "bob-pass")
    _, pending = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(action_proposal_id="act-pending", idempotency_key="idem-pending", risk_level="low"),
        token=alice,
    )
    pending_id = pending["approval_request"]["approval_id"]
    _, expired = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(
            action_proposal_id="act-expire-exec",
            idempotency_key="idem-expire-exec",
            risk_level="low",
            expires_at=time.time() - 1,
        ),
        token=alice,
    )
    expired_id = expired["approval_request"]["approval_id"]
    _, rejected = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(action_proposal_id="act-rejected", idempotency_key="idem-rejected", risk_level="low"),
        token=alice,
    )
    rejected_id = rejected["approval_request"]["approval_id"]
    _request_json(f"{gateway}/api/approval-requests/{rejected_id}/reject", body={"reason": "no"}, token=bob)
    _, approved = _request_json(
        f"{gateway}/api/approval-requests",
        body=_approval_payload(action_proposal_id="act-dup", idempotency_key="idem-dup", risk_level="low"),
        token=alice,
    )
    approved_id = approved["approval_request"]["approval_id"]
    _request_json(f"{gateway}/api/approval-requests/{approved_id}/approve", body={"reason": "approved for duplicate execution test"}, token=bob)

    pending_status, pending_payload = _request_json(
        f"{gateway}/api/approval-requests/{pending_id}/execute",
        body=_execution_payload(idempotency_key="exec-pending"),
        token=admin,
    )
    rejected_status, rejected_payload = _request_json(
        f"{gateway}/api/approval-requests/{rejected_id}/execute",
        body=_execution_payload(idempotency_key="exec-rejected"),
        token=admin,
    )
    expired_status, expired_payload = _request_json(
        f"{gateway}/api/approval-requests/{expired_id}/execute",
        body=_execution_payload(idempotency_key="exec-expired"),
        token=admin,
    )
    out_status, out_payload = _request_json(
        f"{gateway}/api/approval-requests/{approved_id}/execute",
        body=_execution_payload(idempotency_key="exec-out", namespace="staging"),
        token=admin,
    )
    first_status, _ = _request_json(
        f"{gateway}/api/approval-requests/{approved_id}/execute",
        body=_execution_payload(idempotency_key="exec-dup"),
        token=admin,
    )
    duplicate_status, duplicate_payload = _request_json(
        f"{gateway}/api/approval-requests/{approved_id}/execute",
        body=_execution_payload(idempotency_key="exec-other"),
        token=admin,
    )

    assert pending_status == 409
    assert pending_payload["error"]["code"] == "approval_not_approved"
    assert rejected_status == 409
    assert rejected_payload["error"]["code"] == "approval_not_approved"
    assert expired_status == 409
    assert expired_payload["error"]["code"] == "approval_not_approved"
    assert out_status == 403
    assert out_payload["error"]["code"] == "out_of_scope"
    assert first_status == 409
    assert duplicate_status == 409
    assert duplicate_payload["error"]["code"] == "duplicate_execution"


def test_failed_post_check_marks_rollback_required_and_notifies(
    gateway: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _login(gateway, "admin", "admin-pass")
    incident_id, approval_id = _create_approved_execution_grant(gateway)
    connector_main.ConnectorHandler.registration = ConnectorRegistration(
        connector_id="connector-local",
        cluster_id="cluster-local",
        namespace_scope=("default",),
        capabilities=("execute_read", "execute_mutation"),
    )
    connector_main.ConnectorHandler.gateway_url = ""
    connector_main.ConnectorHandler.registered_with_gateway = False
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
                "connector_id": "connector-local",
                "cluster_id": "cluster-local",
                "namespace_scope": ["default"],
                "capabilities": ["execute_read", "execute_mutation"],
            },
        )
        with patch(
            "apps.cluster_connector.kubectl_executor.subprocess.Popen",
            side_effect=_fake_kubectl_popen(real_popen, fail_post_check=True),
        ):
            status, payload = _request_json(
                f"{gateway}/api/approval-requests/{approval_id}/execute",
                body=_execution_payload(idempotency_key="exec-rollback"),
                token=admin,
            )

        incident = asyncio.run(gateway_main.incident_store.get_incident(incident_id))
        deliveries = gateway_main.notification_center.list_deliveries(notification_type="execution_rollback_required")

        assert status == 409
        assert payload["execution"]["status"] == "rollback_required"
        assert incident["status"] == "rollback_required"
        assert deliveries[0]["notification_type"] == "execution_rollback_required"
        assert deliveries[0]["delivery_status"] == "sent"
    finally:
        connector_server.shutdown()
        connector_server.server_close()
        connector_thread.join(timeout=2)
        gateway_main._ROUTES.clear()
