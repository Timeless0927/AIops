"""T07 explicit Chat Handoff Gateway HTTP/SSE contract."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import chat_http
from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.connector_enrollments import ConnectorEnrollments
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.incident import AlertSignal, IncidentService
from apps.aiops_k8s_gateway.investigation_events import InvestigationEvents
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.identity_administration import IdentityAdministration
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase


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
    outbound = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(outbound, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _validate(schema_name: str, payload: dict[str, object]) -> None:
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
    resolver = jsonschema.RefResolver.from_schema(spec)
    schema = spec["components"]["schemas"][schema_name]
    jsonschema.Draft202012Validator(schema, resolver=resolver).validate(payload)


def _login(base_url: str) -> tuple[str, str]:
    _, _, set_cookie = _request(
        f"{base_url}/auth/login",
        body={"username": "admin", "password": "correct-horse-battery-staple", "session_mode": "cookie"},
    )
    cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
    _, payload, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    return cookie, str(payload["csrf_token"])


def test_existing_incident_handoff_is_explicit_idempotent_and_replayed_over_sse(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    store = GatewayDatabase(tmp_path / "gateway.db")
    enrollments = ConnectorEnrollments(store, credential_factory=lambda: "connector-secret")
    commands = ConnectorCommands(
        store,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
        verification_command_ids_in=enrollments.verification_command_ids_in,
    )
    _, credential = enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="enroll-1",
    )
    enrollments.register(
        credential, "connector-prod", "cluster-prod",
        commands=commands, request_id="register-1",
    )
    incidents = IncidentService(
        store, ResourceCatalog(store), ConnectorIdentity(store),
    )
    incident = incidents.ingest(AlertSignal(
        fingerprint="fp-1", alertname="HighErrorRate", status="firing", severity="critical",
        cluster_id="cluster-prod", namespace="shop",
    ))["incident"]
    incident_id = str(incident["id"])
    investigation_id = str(incidents.workbench(
        incident_id, team_ids=None, actor_capabilities=["manage_investigation"],
    )["investigation"]["id"])
    monkeypatch.setattr(gateway_main, "_DATABASE", store)
    monkeypatch.setattr(gateway_main, "_CONNECTOR_ENROLLMENTS", enrollments)
    monkeypatch.setattr(gateway_main, "_CONNECTOR_COMMANDS", commands)
    monkeypatch.setattr(chat_http, "send_governed_chat", lambda _request: {
        "mode": "knowledge", "answer": "待核实。", "scope": None, "tool_activity": [],
        "evidence_references": [], "uncertainty": None, "next_step": None,
        "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        cookie, csrf = _login(base_url)
        _, created, _ = _request(
            f"{base_url}/api/v1/chat/sessions", body={"idempotency_key": "chat-1"},
            cookie=cookie, csrf=csrf,
        )
        session_id = str(created["chat_session"]["id"])
        _, sent, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages",
            body={"content": "发布后错误率升高", "idempotency_key": "message-1"},
            cookie=cookie, csrf=csrf,
        )
        selected = [str(sent["chat_session"]["messages"][0]["id"])]
        request_body = {
            "message_ids": selected, "idempotency_key": "handoff-1",
            "target": {"type": "existing_incident", "incident_id": incident_id},
        }

        status, created_handoff, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/handoffs",
            body=request_body, cookie=cookie, csrf=csrf,
        )
        replay_status, replayed_handoff, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/handoffs",
            body=request_body, cookie=cookie, csrf=csrf,
        )
        _, events, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/events?after=0&limit=200",
            cookie=cookie,
        )

        assert status == 201
        assert replay_status == 200
        assert created_handoff["handoff"] == {
            **replayed_handoff["handoff"], "idempotent": False,
        }
        assert created_handoff["handoff"]["incident_id"] == incident_id
        assert created_handoff["handoff"]["investigation_id"] == investigation_id
        assert created_handoff["handoff"]["selected_message_ids"] == selected
        assert events["events"][-1]["type"] == "handoff.completed"
        assert [event["type"] for event in InvestigationEvents(store).list(investigation_id)["events"]][-2:] == [
            "handoff.created", "human_input.assertion",
        ]
        _validate("ChatHandoffResponse", created_handoff)

        handoff_event_id = int(events["events"][-1]["id"])
        stream = urllib.request.Request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/events/stream",
            headers={"Cookie": cookie, "Last-Event-ID": str(handoff_event_id - 1), "Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(stream, timeout=3) as response:
            lines = [response.readline().decode().strip() for _ in range(3)]
        assert lines[0] == f"id: {handoff_event_id}"
        assert json.loads(lines[2].removeprefix("data: "))["type"] == "handoff.completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_user_created_incident_handoff_requires_bound_scope_and_manage_permission(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    store = GatewayDatabase(tmp_path / "gateway.db")
    enrollments = ConnectorEnrollments(store, credential_factory=lambda: "connector-secret")
    commands = ConnectorCommands(
        store,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
        verification_command_ids_in=enrollments.verification_command_ids_in,
    )
    _, credential = enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="enroll-1",
    )
    enrollments.register(
        credential, "connector-prod", "cluster-prod",
        commands=commands, request_id="register-1",
    )
    _, team = IdentityAdministration(store).mutate(
        collection="teams", target_id=None, payload={"name": "Payments", "description": ""},
        actor_id="admin", reason="test", action="teams_create", request_id="team-1",
    )
    catalog = ResourceCatalog(store)
    bound, unbound = catalog.refresh_discovery("cluster-prod", [
        DiscoveryObservation(namespace="shop", workload_kind="Deployment", workload_name="checkout-api"),
        DiscoveryObservation(namespace="shop", workload_kind="Deployment", workload_name="catalog-api"),
    ])
    service = catalog.create_service(
        team_id=str(team["id"]), name="Checkout", description="", actor_id="admin",
        reason="test", request_id="service-1",
    )
    binding = catalog.confirm_binding(
        candidate_id=str(bound["id"]), service_id=str(service["id"]), actor_id="admin",
        reason="test", request_id="binding-1",
    )
    target_id = str(binding["deployment_target_id"])
    unbound_target_id = str(unbound["id"])
    monkeypatch.setattr(gateway_main, "_DATABASE", store)
    monkeypatch.setattr(gateway_main, "_CONNECTOR_ENROLLMENTS", enrollments)
    monkeypatch.setattr(gateway_main, "_CONNECTOR_COMMANDS", commands)
    monkeypatch.setattr(chat_http, "send_governed_chat", lambda _request: {
        "mode": "knowledge", "answer": "待核实。", "scope": None, "tool_activity": [],
        "evidence_references": [], "uncertainty": None, "next_step": None,
        "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        cookie, csrf = _login(base_url)
        IdentityAdministration(store).mutate(
            collection="users", target_id=None,
            payload={"username": "viewer", "display_name": "Viewer", "email": "viewer@example.com", "password": "viewer-password"},
            actor_id="admin", reason="test", action="users_create", request_id="viewer-1",
        )
        _, created, _ = _request(
            f"{base_url}/api/v1/chat/sessions", body={"idempotency_key": "chat-new"},
            cookie=cookie, csrf=csrf,
        )
        session_id = str(created["chat_session"]["id"])
        _, sent, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages",
            body={"content": "checkout 发布后报错", "idempotency_key": "message-new"},
            cookie=cookie, csrf=csrf,
        )
        selected = [str(sent["chat_session"]["messages"][0]["id"])]
        before = {}
        with store.connect() as conn:
            for table in ("kubernetes_phase_approvals", "kubernetes_execution_grants", "connector_commands"):
                before[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        status, payload, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/handoffs",
            body={
                "message_ids": selected, "idempotency_key": "handoff-new",
                "target": {
                    "type": "user_created_incident", "problem_summary": "checkout 错误率持续升高",
                    "scope": {"cluster_id": "cluster-prod", "deployment_target_id": target_id},
                },
            },
            cookie=cookie, csrf=csrf,
        )
        unbound_status, unbound_error, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/handoffs",
            body={
                "message_ids": selected, "idempotency_key": "handoff-unbound",
                "target": {
                    "type": "user_created_incident", "problem_summary": "catalog 异常",
                    "scope": {"cluster_id": "cluster-prod", "deployment_target_id": unbound_target_id},
                },
            },
            cookie=cookie, csrf=csrf,
        )

        _, _, viewer_set_cookie = _request(
            f"{base_url}/auth/login",
            body={"username": "viewer", "password": "viewer-password", "session_mode": "cookie"},
        )
        viewer_cookie = viewer_set_cookie.split(";", 1)[0] if viewer_set_cookie else ""
        _, viewer_csrf_payload, _ = _request(f"{base_url}/auth/csrf", cookie=viewer_cookie)
        viewer_csrf = str(viewer_csrf_payload["csrf_token"])
        _, viewer_chat, _ = _request(
            f"{base_url}/api/v1/chat/sessions", body={"idempotency_key": "viewer-chat"},
            cookie=viewer_cookie, csrf=viewer_csrf,
        )
        viewer_session_id = str(viewer_chat["chat_session"]["id"])
        _, viewer_sent, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{viewer_session_id}/messages",
            body={"content": "转交", "idempotency_key": "viewer-message"},
            cookie=viewer_cookie, csrf=viewer_csrf,
        )
        forbidden_status, forbidden_error, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{viewer_session_id}/handoffs",
            body={
                "message_ids": [str(viewer_sent["chat_session"]["messages"][0]["id"])],
                "idempotency_key": "viewer-handoff",
                "target": {"type": "existing_incident", "incident_id": str(payload["handoff"]["incident_id"])},
            },
            cookie=viewer_cookie, csrf=viewer_csrf,
        )

        assert status == 201
        assert payload["handoff"]["target_type"] == "user_created_incident"
        incident_id = str(payload["handoff"]["incident_id"])
        incidents = IncidentService(store, catalog, ConnectorIdentity(store))
        incident = next(item for item in incidents.list_incidents(team_ids=None) if item["id"] == incident_id)
        assert incident["origin"] == "user" and incident["signal_count"] == 0
        assert incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])["resource_context"]["deployment_target_id"] == target_id
        assert unbound_status == 409 and unbound_error["error"]["code"] == "resource_not_bound"
        assert forbidden_status == 403 and forbidden_error["error"]["code"] == "forbidden"
        with store.connect() as conn:
            assert {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            } == before
        _validate("ChatHandoffResponse", payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
