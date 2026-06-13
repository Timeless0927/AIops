"""Gateway approval service API tests."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import approval_service
from apps.aiops_k8s_gateway import main as gateway_main


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
        "requested_by": "hermes",
        "reason": "restart checkout-api to recover 5xx",
        "action_summary": "rollout restart deployment/checkout-api",
        "resource_scope": {
            "service_id": "checkout-api",
            "team_id": "payments",
            "namespace": "default",
        },
        "rollback_plan": "kubectl rollout undo deployment/checkout-api -n default",
        "evidence_refs": [{"type": "prometheus", "ref": "5xx_rate"}],
        "idempotency_key": "idem-act-1",
        "assigned_approvers": ["bob"],
    }
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
    old_notification_center = gateway_main.notification_center._CENTER
    old_audit_db = gateway_main.audit_log._DB
    approval_service._DB = approval_service.ApprovalRequestDB(tmp_path / "approval_requests.db")
    gateway_main.notification_center._CENTER = None
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(tmp_path / "audit_log.db")
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
        approval_service._DB = old_approval_db
        gateway_main.notification_center._CENTER = old_notification_center
        gateway_main.audit_log._DB.close()
        gateway_main.audit_log._DB = old_audit_db


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
    approve_status, approved = _request_json(
        f"{gateway}/api/approval-requests/{approval['approval_id']}/approve",
        body={},
        token=bob,
    )
    repeat_approve_status, repeat_approved = _request_json(
        f"{gateway}/api/approval-requests/{approval['approval_id']}/approve",
        body={},
        token=bob,
    )
    rows = asyncio.run(gateway_main.audit_log.query_audit(limit=20))

    assert create_status == 201
    assert repeat_status == 200
    assert repeated["idempotent"] is True
    assert repeated["approval_request"]["approval_id"] == approval["approval_id"]
    assert query_status == 200
    assert [item["approval_id"] for item in queried["approval_requests"]] == [approval["approval_id"]]
    assert detail_status == 200
    assert detail["approval_request"]["action_proposal_id"] == "act-1"
    assert detail["approval_request"]["notification_status"] == "sent"
    assert approve_status == 200
    assert approved["approval_request"]["status"] == "approved"
    assert approved["approval_request"]["approved_by"] == "bob"
    assert approved["approval_request"]["execution_grant"]["action_proposal_id"] == "act-1"
    assert repeat_approve_status == 200
    assert repeat_approved["idempotent"] is True
    audit = {(row["what"], row["approval_id"], row["action_proposal_id"], row["decision"]) for row in rows}
    assert ("approval_create", approval["approval_id"], "act-1", "allow") in audit
    assert ("approval_approve", approval["approval_id"], "act-1", "approved") in audit


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
        "requested_by": "hermes",
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
        body={},
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
    assert approval_audit["role"] == "oncall_approver"
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
        body={},
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
