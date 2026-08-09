"""C04 cross-Incident Change Center HTTP contract."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import change_request_http
from apps.aiops_k8s_gateway import main as gateway_main
from test_gateway_v1_change_requests_contract import (
    _PlannerHandler,
    _alert,
    _register_bound_target,
    _request,
)
from test_gateway_v1_kubernetes_phase_approvals_contract import _awaiting_change, _login


def _validate(spec: dict[str, object], schema_name: str, payload: dict[str, object]) -> None:
    resolver = jsonschema.RefResolver.from_schema(spec)
    schema = spec["components"]["schemas"][schema_name]  # type: ignore[index]
    jsonschema.Draft202012Validator(schema, resolver=resolver).validate(payload)


def test_change_center_lists_visible_attention_and_actor_scoped_detail(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("AIOPS_ALERTMANAGER_WEBHOOK_TOKEN", "alert-token")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    _PlannerHandler.requests = []
    _PlannerHandler.received_headers = []
    _PlannerHandler.responses = [{
        "service": "diagnosis", "status": "needs_input",
        "question": "Which stable revision should be restored?",
    }]
    planner_server = ThreadingHTTPServer(("127.0.0.1", 0), _PlannerHandler)
    planner_thread = threading.Thread(target=planner_server.serve_forever, daemon=True)
    planner_thread.start()
    monkeypatch.setenv(
        "AIOPS_DIAGNOSIS_URL", f"http://127.0.0.1:{planner_server.server_address[1]}",
    )
    monkeypatch.setattr(
        change_request_http, "internal_auth_headers",
        lambda: {"Authorization": "Bearer fake"},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())

    try:
        denied_status, denied, _ = _request(f"{base_url}/api/v1/changes")
        assert denied_status == 401
        assert denied["error"]["code"] == "unauthorized"  # type: ignore[index]

        _, _, set_cookie = _request(
            f"{base_url}/auth/login",
            body={
                "username": "admin", "password": "correct-horse-battery-staple",
                "session_mode": "cookie",
            },
        )
        cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
        _register_bound_target(tmp_path / "gateway.db")
        _, ingested, _ = _request(
            f"{base_url}/webhooks/alertmanager", body=_alert(),
            headers={"Authorization": "Bearer alert-token"},
        )
        incident_id = str(ingested["incidents"][0]["incident_id"])  # type: ignore[index]
        _, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
        created_status, created, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/change-requests",
            body={
                "desired_outcome": "Restore checkout availability",
                "context": "The last rollout may have regressed readiness",
                "idempotency_key": "change-center-1",
            },
            cookie=cookie,
            headers={"X-CSRF-Token": str(csrf["csrf_token"])},
        )
        assert created_status == 201
        change_request_id = str(created["change_request"]["id"])  # type: ignore[index]

        list_status, listing, _ = _request(f"{base_url}/api/v1/changes", cookie=cookie)
        assert list_status == 200
        assert listing["pending_count"] == 1
        assert listing["change_requests"] == [{
            "id": change_request_id,
            "incident": {
                "id": incident_id,
                "title": "HighErrorRate - Checkout",
                "severity": "critical",
                "lifecycle_state": "firing",
            },
            "desired_outcome": "Restore checkout availability",
            "status": "needs_input",
            "environment": "prod",
            "attention": "input",
            "updated_at": listing["change_requests"][0]["updated_at"],  # type: ignore[index]
        }]
        _validate(spec, "ChangeCenterListResponse", listing)

        detail_status, detail, _ = _request(
            f"{base_url}/api/v1/changes/{change_request_id}", cookie=cookie,
        )
        assert detail_status == 200
        assert detail["incident"]["id"] == incident_id
        assert detail["evidence_references"] == []
        assert detail["change_request"]["id"] == change_request_id
        assert detail["change_request"]["active_revision"]["question"] == (
            "Which stable revision should be restored?"
        )
        _validate(spec, "ChangeCenterDetailResponse", detail)

        missing_status, missing, _ = _request(
            f"{base_url}/api/v1/changes/missing", cookie=cookie,
        )
        assert missing_status == 404
        assert missing["error"]["code"] == "not_found"  # type: ignore[index]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        planner_server.shutdown()
        planner_server.server_close()
        planner_thread.join(timeout=2)


def test_change_center_fails_closed_for_incident_scope_and_change_authority(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())

    try:
        admin_cookie, admin_csrf = _login(
            base_url, "admin", "correct-horse-battery-staple",
        )
        _, approver = gateway_main._identity_administration().mutate(
            collection="users", target_id=None,
            payload={
                "username": "approver", "display_name": "Approver",
                "password": "strong-password",
            },
            actor_id="admin", reason="test", action="users_create", request_id="req-user",
        )
        gateway_main._identity_administration().mutate(
            collection="role-bindings", target_id=None,
            payload={
                "user_id": approver["id"], "role": "platform_administrator",
                "scope_type": "platform", "scope_id": None,
            },
            actor_id="admin", reason="test", action="role-bindings_create",
            request_id="req-role",
        )
        item = _awaiting_change()
        change_request_id = str(item["id"])
        _, owner_team = gateway_main._identity_administration().mutate(
            collection="teams", target_id=None,
            payload={"name": "Payments", "description": "Incident owner"},
            actor_id="admin", reason="test", action="teams_create",
            request_id="req-owner-team",
        )
        with gateway_main._GATEWAY.database.connect() as conn:
            conn.execute(
                "UPDATE incidents SET team_id = ? WHERE id = ?",
                (owner_team["id"], item["incident_id"]),
            )
            conn.commit()

        admin_status, admin_detail, _ = _request(
            f"{base_url}/api/v1/changes/{change_request_id}", cookie=admin_cookie,
        )
        assert admin_status == 200
        assert admin_detail["attention"] is None
        assert admin_detail["change_request"]["active_revision"]["plan"] is None  # type: ignore[index]
        [read_denial] = gateway_main._kubernetes_phase_approvals().audit_history(
            change_request_id,
        )
        admin_user = next(
            user for user in gateway_main._identity_administration().state()["users"] if user["username"] == "admin"
        )
        assert read_denial["actor_id"] == admin_user["id"]
        assert read_denial["request_id"] == admin_detail["request_id"]
        assert read_denial["result"] == "not_found"
        assert read_denial["reason"] == "exact_diff_access_denied"
        assert set(read_denial) == {
            "event_id", "phase_id", "actor_id", "result", "reason", "request_id", "created_at",
        }
        assert gateway_main._kubernetes_phase_approvals().access_for_projection(
            change_request_id, str(admin_user["id"]), "planning",
            request_id="req-denied-draft-read",
        ) == (False, None)
        draft_denial = gateway_main._kubernetes_phase_approvals().audit_history(change_request_id)[-1]
        assert draft_denial["reason"] == "exact_diff_access_denied"
        assert draft_denial["request_id"] == "req-denied-draft-read"

        _, outsider = gateway_main._identity_administration().mutate(
            collection="users", target_id=None,
            payload={
                "username": "outsider", "display_name": "Other SRE",
                "password": "safe-password",
            },
            actor_id="admin", reason="test", action="users_create",
            request_id="req-outsider",
        )
        _, other_team = gateway_main._identity_administration().mutate(
            collection="teams", target_id=None,
            payload={"name": "Other", "description": "Other team"},
            actor_id="admin", reason="test", action="teams_create",
            request_id="req-other-team",
        )
        gateway_main._identity_administration().mutate(
            collection="team-memberships", target_id=None,
            payload={"user_id": outsider["id"], "team_id": other_team["id"]},
            actor_id="admin", reason="test", action="team-memberships_create",
            request_id="req-membership",
        )
        gateway_main._identity_administration().mutate(
            collection="role-bindings", target_id=None,
            payload={
                "user_id": outsider["id"], "role": "sre",
                "scope_type": "team", "scope_id": other_team["id"],
            },
            actor_id="admin", reason="test", action="role-bindings_create",
            request_id="req-outsider-role",
        )
        outsider_cookie, _ = _login(base_url, "outsider", "safe-password")
        outsider_list_status, outsider_list, _ = _request(
            f"{base_url}/api/v1/changes", cookie=outsider_cookie,
        )
        assert outsider_list_status == 200
        assert outsider_list["pending_count"] == 0
        assert outsider_list["change_requests"] == []
        outsider_detail_status, outsider_detail, _ = _request(
            f"{base_url}/api/v1/changes/{change_request_id}", cookie=outsider_cookie,
        )
        assert outsider_detail_status == 404
        assert outsider_detail["error"]["code"] == "not_found"  # type: ignore[index]

        authority_status, _, _ = _request(
            f"{base_url}/api/v1/admin/kubernetes-change-authorities",
            body={
                "user_id": approver["id"], "environment": "prod",
                "scope_type": "namespace",
                "scope": {"cluster_id": "cluster-prod", "namespace": "payments"},
                "reason": "on-call authority",
            },
            cookie=admin_cookie,
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert authority_status == 201
        approver_cookie, _ = _login(base_url, "approver", "strong-password")
        list_status, listing, _ = _request(
            f"{base_url}/api/v1/changes", cookie=approver_cookie,
        )
        assert list_status == 200
        assert listing["pending_count"] == 1
        assert listing["change_requests"][0]["attention"] == "approval"  # type: ignore[index]
        detail_status, detail, _ = _request(
            f"{base_url}/api/v1/changes/{change_request_id}", cookie=approver_cookie,
        )
        assert detail_status == 200
        assert detail["change_request"]["phase_review"]["changes"][0]["diff"]  # type: ignore[index]
        _validate(spec, "ChangeCenterDetailResponse", detail)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
