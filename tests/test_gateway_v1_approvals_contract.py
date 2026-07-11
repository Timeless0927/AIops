"""T13 explicit Approval and governed restart HTTP contract."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema
import pytest

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.approval import Approvals
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.evidence_decisions import record_diagnosis_facts
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.incident import AlertSignal, IncidentService
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog


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
        url, data=json.dumps(body).encode() if body is not None else None, headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _login(base_url: str, username: str, password: str) -> tuple[str, str]:
    status, _, set_cookie = _request(
        f"{base_url}/auth/login", body={"username": username, "password": password, "session_mode": "cookie"}
    )
    assert status == 200 and set_cookie
    cookie = set_cookie.split(";", 1)[0]
    csrf_status, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    assert csrf_status == 200
    return cookie, str(csrf["csrf_token"])


@pytest.mark.parametrize(
    ("action_type", "parameters", "rollback_plan"),
    [
        ("restart_deployment", {}, {"type": "none", "reason": "restart preserves revision"}),
        (
            "scale_deployment",
            {"current_replicas": 3, "target_replicas": 5},
            {
                "condition": "post_check_failed",
                "action_type": "scale_deployment",
                "parameters": {"current_replicas": 5, "target_replicas": 3},
                "target_assumptions": {"replicas": 5},
            },
        ),
        ("rollback_deployment", {"target_revision": 41}, None),
    ],
)
def test_explicit_approval_atomically_creates_one_typed_mutation_command(
    tmp_path: Path, monkeypatch, action_type: str, parameters: dict[str, object], rollback_plan: dict[str, object] | None
) -> None:
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
        admin_cookie, admin_csrf = _login(base_url, "admin", "correct-horse-battery-staple")
        store = gateway_main._SESSIONS
        _, user = store.mutate_admin(
            collection="users", target_id=None,
            payload={"username": "sre-approver", "display_name": "SRE Approver", "password": "strong-password"},
            actor_id="admin", reason="test", action="users_create", request_id="req-user",
        )
        _, team = store.mutate_admin(
            collection="teams", target_id=None, payload={"name": "Payments", "description": ""},
            actor_id="admin", reason="test", action="teams_create", request_id="req-team",
        )
        store.mutate_admin(
            collection="team-memberships", target_id=None,
            payload={"user_id": user["id"], "team_id": team["id"]}, actor_id="admin",
            reason="test", action="team-memberships_create", request_id="req-membership",
        )
        store.mutate_admin(
            collection="role-bindings", target_id=None,
            payload={"user_id": user["id"], "role": "sre", "scope_type": "team", "scope_id": team["id"]},
            actor_id="admin", reason="test", action="role-bindings_create", request_id="req-role",
        )
        _, credential = store.create_connector_enrollment(
            connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
            reason="test", request_id="req-enroll",
        )
        store.register_connector(credential, "connector-prod", "cluster-prod", request_id="req-register")
        store.record_connector_heartbeat(
            credential, "connector-prod", "cluster-prod", status="online", failure_summary="", request_id="req-heartbeat"
        )
        store.update_cluster(
            "cluster-prod", payload={"mutation_enabled": True}, actor_id="admin", reason="test", request_id="req-policy"
        )
        catalog = ResourceCatalog(store.database)
        [candidate] = catalog.refresh_discovery(
            "cluster-prod", [DiscoveryObservation(namespace="payments", workload_kind="Deployment", workload_name="checkout-api")]
        )
        service = catalog.create_service(
            team_id=str(team["id"]), name="Checkout", description="", actor_id="admin",
            reason="test", request_id="req-service",
        )
        binding = catalog.confirm_binding(
            candidate_id=str(candidate["id"]), service_id=str(service["id"]), actor_id="admin",
            reason="test", request_id="req-binding",
        )
        incidents = gateway_main._incident_service()
        created = incidents.ingest(AlertSignal(
            fingerprint="fp-t13", alertname="HighErrorRate", cluster_id="cluster-prod", namespace="payments",
            status="firing", severity="critical", summary="errors", workload_kind="Deployment",
            workload_name="checkout-api",
        ))
        incident_id = str(created["incident"]["id"])
        snapshot = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
        assert snapshot and snapshot["investigation"]
        investigation_id = str(snapshot["investigation"]["id"])
        now = time.time()
        with store.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record_diagnosis_facts(
                conn, request_id="diagnosis-t13", investigation_id=investigation_id, created_at=now,
                payload={
                    "diagnosis": {"summary": "mutation is recommended", "next_verification": [], "recommended_actions": [{
                        "id": "action-mutation", "action_type": action_type, "summary": "Mutate checkout-api",
                        "parameters": parameters, "evidence_step_ids": ["step-k8s"], "safeguards": ["one Deployment"],
                        "rollback_plan": rollback_plan,
                    }]},
                    "evidence_steps": [{
                        "id": "step-k8s", "purpose": "Confirm Deployment", "source": "k8s",
                        "scope": {"cluster_id": "cluster-prod", "namespace": "payments", "workload_kind": "Deployment", "workload_name": "checkout-api"},
                        "state": "succeeded", "result": "revision 42 is ready", "impact": "target confirmed",
                        "evidence_references": ["k8s:deployment/checkout-api@42", "k8s:deployment/checkout-api@41"], "observed_at": now, "expires_at": now + 300,
                    }], "missing_evidence": [],
                },
            )
            conn.commit()

        admin_view_status, admin_view, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/workbench", cookie=admin_cookie
        )
        action = admin_view["recommended_actions"][0]  # type: ignore[index]
        denied_status, denied, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/actions/action-mutation/approve-and-execute",
            body={"action_version": 1, "action_hash": action["hash"], "idempotency_key": "admin-denied"},
            cookie=admin_cookie, csrf=admin_csrf,
        )
        assert admin_view_status == 200 and action["can_approve"] is False
        assert denied_status == 403 and denied["error"]["code"] == "forbidden"  # type: ignore[index]

        authority_status, authority, _ = _request(
            f"{base_url}/api/v1/admin/approval-authorities",
            body={"user_id": user["id"], "environment": "prod", "scope_type": "deployment_target", "scope_id": binding["deployment_target_id"], "reason": "on-call authority"},
            cookie=admin_cookie, csrf=admin_csrf,
        )
        assert authority_status == 201 and authority["approval_authority"]["user_id"] == user["id"]  # type: ignore[index]
        user_cookie, user_csrf = _login(base_url, "sre-approver", "strong-password")
        view_status, view, _ = _request(f"{base_url}/api/v1/incidents/{incident_id}/workbench", cookie=user_cookie)
        action = view["recommended_actions"][0]  # type: ignore[index]
        assert view_status == 200 and action["can_approve"] is True
        assert action["rollback_plan"] == rollback_plan
        assert action["expires_at"] > now and action["evidence_step_ids"] == ["step-k8s"]

        stale_status, stale, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/actions/action-mutation/approve-and-execute",
            body={"action_version": 1, "action_hash": "0" * 64, "idempotency_key": "stale"},
            cookie=user_cookie, csrf=user_csrf,
        )
        assert stale_status == 409 and stale["error"]["code"] == "action_stale"  # type: ignore[index]
        with store.database.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM connector_commands").fetchone()[0] == 0

        payload = {"action_version": 1, "action_hash": action["hash"], "idempotency_key": "approve-once"}
        created_status, approved, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/actions/action-mutation/approve-and-execute",
            body=payload, cookie=user_cookie, csrf=user_csrf,
        )
        replay_status, replay, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/actions/action-mutation/approve-and-execute",
            body=payload, cookie=user_cookie, csrf=user_csrf,
        )
        assert created_status == 201 and replay_status == 200
        assert approved["execution"]["command_id"] == replay["execution"]["command_id"]  # type: ignore[index]
        assert replay["execution"]["idempotent"] is True  # type: ignore[index]
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ApproveAndExecuteResponse"],  # type: ignore[index]
            resolver=jsonschema.RefResolver.from_schema(spec),
        ).validate(approved)
        with store.database.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM execution_grants").fetchone()[0] == 1
            command = conn.execute("SELECT action, execution_grant_id FROM connector_commands").fetchone()
            assert tuple(command) == (action_type, approved["execution"]["execution_grant_id"])  # type: ignore[index]
        clock = [time.time()]
        commands = ConnectorCommands(store.database, clock=lambda: clock[0], lease_seconds=5)
        lifecycle = IncidentService(
            store.database, ResourceCatalog(store.database), ConnectorIdentity(store.database),
            clock=lambda: clock[0], stabilization_seconds=0,
        )
        lifecycle.ingest(AlertSignal(
            fingerprint="fp-t13", alertname="HighErrorRate", cluster_id="cluster-prod", namespace="payments",
            status="recovered", severity="critical", summary="recovered", workload_kind="Deployment",
            workload_name="checkout-api",
        ))
        assert lifecycle.workbench(incident_id, team_ids=None, actor_capabilities=[])["incident"]["status"] == "active"  # type: ignore[index]
        leased = commands.poll("connector-prod", "cluster-prod", 0)
        assert leased and leased["id"] == approved["execution"]["command_id"]  # type: ignore[index]
        commands.start(str(leased["id"]), "connector-prod", "cluster-prod", str(leased["lease_id"]))
        clock[0] += 6
        assert commands.reconcile_unknown_outcomes() == 1
        assert commands.get(str(leased["id"]))["status"] == "unknown_outcome"
        snapshot = lifecycle.workbench(incident_id, team_ids=None, actor_capabilities=[])
        assert snapshot is not None
        projected = Approvals(store.database).project_workbench(snapshot, str(user["id"]))
        assert projected["recommended_actions"][0]["execution"]["status"] == "unknown_outcome"  # type: ignore[index]
        assert commands.poll("connector-prod", "cluster-prod", 0) is None
        reconciled = commands.submit_result(
            str(leased["id"]), "connector-prod", "cluster-prod", str(leased["lease_id"]),
            {
                "status": "succeeded", "stdout": "{}", "stderr": "", "exit_code": 0,
                "truncated": False, "error_code": None, "error_message": None,
            },
            request_id="req-late-result",
        )
        assert reconciled["late"] is True
        assert commands.get(str(leased["id"]))["status"] == "succeeded"
        assert lifecycle.reconcile_due() == 1
        assert lifecycle.workbench(incident_id, team_ids=None, actor_capabilities=[])["incident"]["status"] == "resolved"  # type: ignore[index]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
