"""K03 HTTP contract for exact Kubernetes Phase Approval."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.change_requests import ChangeRequests
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.gateway_db import token_hash
from apps.aiops_k8s_gateway.incident import AlertSignal


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
) -> tuple[int, dict[str, object], str | None]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _login(base_url: str, username: str, password: str) -> tuple[str, str]:
    status, _, header = _request(
        f"{base_url}/auth/login",
        body={"username": username, "password": password, "session_mode": "cookie"},
    )
    assert status == 200 and header
    cookie = header.split(";", 1)[0]
    csrf_status, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    assert csrf_status == 200
    return cookie, str(csrf["csrf_token"])


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _draft() -> dict[str, object]:
    return {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment",
            "namespace": "payments", "name": "checkout-api",
        },
        "operation": "patch",
        "payload": [{"op": "replace", "path": "/spec/replicas", "value": 5}],
        "post_checks": [{"type": "workload_rollout"}],
    }


def _result() -> dict[str, object]:
    canonical = {
        **_draft(),
        "target": {**_draft()["target"], "uid": "uid-1", "resource_version": "41"},  # type: ignore[dict-item]
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "41"},
            {"op": "test", "path": "/spec/replicas", "value": 3},
            {"op": "replace", "path": "/spec/replicas", "value": 5},
        ],
    }
    diff = [{"op": "replace", "path": "/spec/replicas", "before": 3, "after": 5}]
    digest = hashlib.sha256(_json({"canonical_change": canonical, "diff": diff}).encode()).hexdigest()
    return {
        "discovery": {
            "api_version": "apps/v1", "kind": "Deployment", "resource": "deployments",
            "namespaced": True, "verbs": ["get", "patch"],
        },
        "live": {"exists": True, "uid": "uid-1", "resource_version": "41"},
        "canonical_change": canonical,
        "dry_run": {"diff": diff, "hash": digest},
    }


def _awaiting_change() -> dict[str, object]:
    store = gateway_main._SESSIONS
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="req-enroll",
    )
    commands = ConnectorCommands(store.database)
    store.connector_enrollments.register(
        credential, "connector-prod", "cluster-prod", namespace_scope=["*"],
        capabilities=["validate", "execute"], commands=commands, request_id="req-register",
    )
    verification = commands.poll("connector-prod", "cluster-prod", 0)
    assert verification is not None
    commands.start(
        str(verification["id"]), "connector-prod", "cluster-prod", str(verification["lease_id"]),
    )
    commands.submit_result(
        str(verification["id"]), "connector-prod", "cluster-prod", str(verification["lease_id"]),
        {
            "status": "succeeded", "stdout": '{"apiVersion":"v1","kind":"PodList","items":[]}',
            "stderr": "", "exit_code": 0, "truncated": False, "error_code": None, "error_message": None,
        },
        request_id="req-verify", result_handler=store.connector_enrollments.record_verification_result_in,
    )
    created = gateway_main._incident_service().ingest(AlertSignal(
        fingerprint="fp-k03", alertname="HighErrorRate", cluster_id="cluster-prod",
        namespace="payments", status="firing", severity="critical", summary="errors",
        workload_kind="Deployment", workload_name="checkout-api",
    ))
    incident_id = str(created["incident"]["id"])
    changes = gateway_main._change_requests()
    _, item = changes.submit(
        incident_id=incident_id, facts={"resource": {"cluster_id": "cluster-prod"}},
        actor_id="admin", desired_outcome="scale checkout-api", context="load increased",
        idempotency_key="change-k03", request_id="req-change-k03",
        planner=lambda _payload: {
            "status": "validating", "plan": {"summary": "scale", "changes": [_draft()]},
        },
    )
    command = commands.poll("connector-prod", "cluster-prod", 0)
    assert command is not None
    commands.start(str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]))
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        {
            "status": "succeeded", "stdout": _json(_result()), "stderr": "", "exit_code": 0,
            "truncated": False, "error_code": None, "error_message": None,
        },
        request_id="req-validation", result_handler=changes.record_validation_result_in,
    )
    return ChangeRequests(store.database).get(str(item["id"]))


def test_http_requires_exact_authority_fresh_auth_and_contract_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
    try:
        fabricated_status, _, _ = _request(
            f"{base_url}/api/v1/change-requests/fabricated/phase-approval",
        )
        assert fabricated_status == 401
        assert gateway_main._kubernetes_phase_approvals().audit_history("fabricated") == []
        admin_cookie, admin_csrf = _login(base_url, "admin", "correct-horse-battery-staple")
        _, approver = gateway_main._SESSIONS.mutate_admin(
            collection="users", target_id=None,
            payload={"username": "approver", "display_name": "Approver", "password": "strong-password"},
            actor_id="admin", reason="test", action="users_create", request_id="req-user",
        )
        gateway_main._SESSIONS.mutate_admin(
            collection="role-bindings", target_id=None,
            payload={
                "user_id": approver["id"], "role": "platform_administrator",
                "scope_type": "platform", "scope_id": None,
            },
            actor_id="admin", reason="test", action="role-bindings_create", request_id="req-role",
        )
        item = _awaiting_change()
        change_request_id = str(item["id"])

        detail_status, detail, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}", cookie=admin_cookie,
        )
        assert detail_status == 200
        assert detail["change_request"]["active_revision"]["plan"] is None  # type: ignore[index]
        assert detail["change_request"]["active_revision"]["validation"] is None  # type: ignore[index]
        assert detail["change_request"]["phase_review"] is None  # type: ignore[index]
        workbench_status, workbench, _ = _request(
            f"{base_url}/api/v1/incidents/{item['incident_id']}/workbench", cookie=admin_cookie,
        )
        assert workbench_status == 200
        assert workbench["change_requests"][0]["active_revision"]["plan"] is None  # type: ignore[index]
        assert workbench["change_requests"][0]["phase_review"] is None  # type: ignore[index]

        hidden_status, hidden, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-approval",
            cookie=admin_cookie,
        )
        assert hidden_status == 404 and hidden["error"]["code"] == "not_found"  # type: ignore[index]

        authority_status, authority, _ = _request(
            f"{base_url}/api/v1/admin/kubernetes-change-authorities",
            body={
                "user_id": approver["id"], "environment": "prod", "scope_type": "namespace",
                "scope": {"cluster_id": "cluster-prod", "namespace": "payments"},
                "reason": "on-call authority",
            },
            cookie=admin_cookie, csrf=admin_csrf,
        )
        assert authority_status == 201
        approver_cookie, approver_csrf = _login(base_url, "approver", "strong-password")
        review_status, reviewed, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-approval",
            cookie=approver_cookie,
        )
        assert review_status == 200
        review = reviewed["phase_review"]
        authorized_workbench_status, authorized_workbench, _ = _request(
            f"{base_url}/api/v1/incidents/{item['incident_id']}/workbench", cookie=approver_cookie,
        )
        assert authorized_workbench_status == 200
        assert authorized_workbench["change_requests"][0]["phase_review"]["revision_id"] == review["revision_id"]  # type: ignore[index]
        payload = {
            "revision_id": review["revision_id"],
            "dry_run_hashes": [change["dry_run_hash"] for change in review["changes"]],
            "target_confirmations": [change["target_confirmation"] for change in review["changes"]],
            "rollback_policy": "rollback_completed",
            "reason": "restore service capacity",
            "idempotency_key": "approve-once",
        }
        csrf_status, csrf_denied, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-approval/approve",
            body=payload, cookie=approver_cookie,
        )
        assert csrf_status == 403 and csrf_denied["error"]["code"] == "csrf_required"  # type: ignore[index]
        raw_token = approver_cookie.split("=", 1)[1]
        with gateway_main._SESSIONS.database.connect() as conn:
            conn.execute("UPDATE sessions SET fresh_at = 0 WHERE token_hash = ?", (token_hash(raw_token),))
        stale_auth_status, stale_auth, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-approval/approve",
            body=payload, cookie=approver_cookie, csrf=approver_csrf,
        )
        assert stale_auth_status == 403 and stale_auth["error"]["code"] == "fresh_auth_required"  # type: ignore[index]
        reauth_status, _, _ = _request(
            f"{base_url}/auth/reauth", body={"password": "strong-password"},
            cookie=approver_cookie, csrf=approver_csrf,
        )
        assert reauth_status == 200

        wrong = {**payload, "dry_run_hashes": ["0" * 64], "idempotency_key": "wrong-hash"}
        wrong_status, wrong_response, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-approval/approve",
            body=wrong, cookie=approver_cookie, csrf=approver_csrf,
        )
        assert wrong_status == 409 and wrong_response["error"]["code"] == "phase_stale"  # type: ignore[index]

        approved_status, approved, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-approval/approve",
            body=payload, cookie=approver_cookie, csrf=approver_csrf,
        )
        replay_status, replay, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-approval/approve",
            body=payload, cookie=approver_cookie, csrf=approver_csrf,
        )
        assert approved_status == 201 and replay_status == 200
        assert replay["phase_review"]["approval"]["idempotent"] is True  # type: ignore[index]
        assert approved["phase_review"]["approval"]["authority_ids"] == [  # type: ignore[index]
            authority["kubernetes_change_authority"]["id"]  # type: ignore[index]
        ]
        audit = gateway_main._kubernetes_phase_approvals().audit_history(change_request_id)
        assert [event["result"] for event in audit] == [
            "not_found", "csrf_required", "fresh_auth_required", "phase_stale", "approved",
        ]
        assert [event["request_id"] for event in audit] == [
            hidden["request_id"], csrf_denied["request_id"], stale_auth["request_id"],
            wrong_response["request_id"], approved["request_id"],
        ]
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["KubernetesPhaseReviewResponse"],
            resolver=jsonschema.RefResolver.from_schema(spec),
        ).validate(approved)
        empty_status, empty, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-execution",
            cookie=approver_cookie,
        )
        assert empty_status == 200 and empty["phase_execution"] is None
        execution_payload = {
            "phase_id": review["phase_id"],
            "reason": "execute approved exact change",
            "idempotency_key": "execute-once",
            "execution_timeout_seconds": 300,
        }
        start_status, started, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-execution/start",
            body=execution_payload, cookie=approver_cookie, csrf=approver_csrf,
        )
        replay_status, replayed, _ = _request(
            f"{base_url}/api/v1/change-requests/{change_request_id}/phase-execution/start",
            body=execution_payload, cookie=approver_cookie, csrf=approver_csrf,
        )
        assert start_status == 201 and replay_status == 200
        assert started["phase_execution"]["grant"]["expires_at"] > started["phase_execution"]["grant"]["issued_at"]
        assert replayed["phase_execution"]["idempotent"] is True
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["KubernetesPhaseExecutionResponse"],
            resolver=jsonschema.RefResolver.from_schema(spec),
        ).validate(started)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
