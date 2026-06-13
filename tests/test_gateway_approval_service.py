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
        namespaces: ["default"]
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

    assert status == 403
    assert payload["error"]["code"] == "forbidden"


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
