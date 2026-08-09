"""T05 private Chat Gateway HTTP/SSE contract."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import chat_http
from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.connector_enrollments import ConnectorEnrollments
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.mcp_registry import MCPRegistry
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.skill_registry import SkillRegistry
from apps.aiops_k8s_gateway.identity_administration import IdentityAdministration


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    authorization: str | None = None,
    csrf: str | None = None,
    method: str | None = None,
) -> tuple[int, dict[str, object], str | None]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if authorization:
        headers["Authorization"] = authorization
    if csrf:
        headers["X-CSRF-Token"] = csrf
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method or ("POST" if body is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _validate(spec: dict[str, object], schema_name: str, payload: dict[str, object]) -> None:
    resolver = jsonschema.RefResolver.from_schema(spec)
    schema = spec["components"]["schemas"][schema_name]  # type: ignore[index]
    jsonschema.Draft202012Validator(schema, resolver=resolver).validate(payload)


def test_private_chat_fake_model_replays_http_and_sse(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    database = GatewayDatabase(tmp_path / "gateway.db")
    enrollments = ConnectorEnrollments(database)
    monkeypatch.setattr(gateway_main, "_DATABASE", database)
    monkeypatch.setattr(gateway_main, "_CONNECTOR_ENROLLMENTS", enrollments)
    monkeypatch.setattr(
        gateway_main,
        "_CONNECTOR_COMMANDS",
        ConnectorCommands(
            database,
            available_connector_in=enrollments.require_available_connector_in,
            lease_identity_matches_in=enrollments.lease_identity_matches_in,
            verification_command_ids_in=enrollments.verification_command_ids_in,
        ),
    )
    model_calls: list[dict[str, object]] = []
    model = lambda request: model_calls.append(request) or {
            "mode": "knowledge", "answer": "Deployment 通过 ReplicaSet 滚动管理 Pod。", "scope": None,
            "tool_activity": [], "evidence_references": [], "uncertainty": None, "next_step": None,
            "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
        }
    monkeypatch.setattr(chat_http, "send_governed_chat", model)
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
    try:
        denied_status, denied, _ = _request(f"{base_url}/api/v1/chat/sessions")
        login_status, _, set_cookie = _request(
            f"{base_url}/auth/login",
            body={"username": "admin", "password": "correct-horse-battery-staple", "session_mode": "cookie"},
        )
        cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
        _, csrf_payload, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
        csrf = str(csrf_payload["csrf_token"])
        create_status, created, _ = _request(
            f"{base_url}/api/v1/chat/sessions",
            body={"idempotency_key": "create-1"},
            cookie=cookie,
            csrf=csrf,
        )
        session_id = str(created["chat_session"]["id"])  # type: ignore[index]
        send_status, sent, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages",
            body={"content": "Deployment 如何管理 Pod？", "idempotency_key": "message-1"},
            cookie=cookie,
            csrf=csrf,
        )
        duplicate_status, duplicate, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages",
            body={"content": "Deployment 如何管理 Pod？", "idempotency_key": "message-1"},
            cookie=cookie,
            csrf=csrf,
        )
        get_status, fetched, _ = _request(f"{base_url}/api/v1/chat/sessions/{session_id}", cookie=cookie)
        list_status, listing, _ = _request(f"{base_url}/api/v1/chat/sessions", cookie=cookie)
        events_status, replay, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/events?after=0&limit=200", cookie=cookie,
        )

        gateway_main._identity_administration().mutate(
            collection="users",
            target_id=None,
            payload={"username": "viewer", "display_name": "Viewer", "email": "viewer@example.com", "password": "viewer-password"},
            actor_id="admin",
            reason="Chat privacy contract",
            action="users_create",
            request_id="create-viewer",
        )
        _, _, viewer_set_cookie = _request(
            f"{base_url}/auth/login",
            body={"username": "viewer", "password": "viewer-password", "session_mode": "cookie"},
        )
        viewer_cookie = viewer_set_cookie.split(";", 1)[0] if viewer_set_cookie else ""
        hidden_status, hidden, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}", cookie=viewer_cookie,
        )
        hidden_stream_status, hidden_stream, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/events/stream", cookie=viewer_cookie,
        )

        assert denied_status == 401
        assert denied["error"]["code"] == "unauthorized"  # type: ignore[index]
        assert login_status == send_status == duplicate_status == get_status == list_status == events_status == 200
        assert create_status == 201
        assert sent["chat_session"] == duplicate["chat_session"] == fetched["chat_session"]
        assert model_calls[0]["messages"] == [{"role": "user", "content": "Deployment 如何管理 Pod？"}]
        assert listing["chat_sessions"][0]["id"] == session_id  # type: ignore[index]
        assert [message["status"] for message in fetched["chat_session"]["messages"]] == ["completed", "completed"]  # type: ignore[index]
        event_types = [event["type"] for event in replay["events"]]  # type: ignore[index]
        assert event_types[:3] == ["session.created", "message.created", "message.created"]
        assert event_types[-1] == "message.completed"
        assert "".join(
            event["payload"]["delta"] for event in replay["events"] if event["type"] == "message.delta"  # type: ignore[index]
        ) == "Deployment 通过 ReplicaSet 滚动管理 Pod。"
        assert hidden_status == 404
        assert hidden_stream_status == 404
        assert hidden["error"]["code"] == "chat_not_found"  # type: ignore[index]
        assert hidden_stream["error"]["code"] == "chat_not_found"  # type: ignore[index]
        assert not {"evidence", "approval", "connector_command"} & set(json.dumps(fetched).lower().split())
        _validate(spec, "ChatSessionResponse", fetched)
        _validate(spec, "ChatSessionListResponse", listing)
        _validate(spec, "ChatEventsResponse", replay)

        started = threading.Event()
        release = threading.Event()

        def delayed(_request: dict[str, object]) -> dict[str, object]:
            started.set()
            release.wait(2)
            return model(_request)

        monkeypatch.setattr(chat_http, "send_governed_chat", delayed)
        pending: list[tuple[int, dict[str, object], str | None]] = []
        pending_thread = threading.Thread(target=lambda: pending.append(_request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages",
            body={"content": "停止回答", "idempotency_key": "message-cancel"}, cookie=cookie, csrf=csrf,
        )))
        pending_thread.start()
        assert started.wait(2)
        cancel_status, cancelled, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages/cancel",
            body={"idempotency_key": "cancel-1"}, cookie=cookie, csrf=csrf,
        )
        release.set()
        pending_thread.join(2)
        monkeypatch.setattr(chat_http, "send_governed_chat", model)
        assert cancel_status == 200
        assert cancelled["chat_session"]["messages"][-1]["completion"] == {  # type: ignore[index]
            "status": "cancelled", "stopping_reason": "user_cancelled",
        }
        assert pending and pending[0][0] == 200
        _validate(spec, "ChatSessionResponse", cancelled)

        original_user_id = str(fetched["chat_session"]["messages"][0]["id"])  # type: ignore[index]
        original_assistant_id = str(fetched["chat_session"]["messages"][1]["id"])  # type: ignore[index]
        edit_status, edited, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages/{original_user_id}/edit",
            body={"content": "修改后的问题", "idempotency_key": "edit-1"}, cookie=cookie, csrf=csrf,
        )
        edited_messages = edited["chat_session"]["messages"]  # type: ignore[index]
        edited_assistant_id = str(edited_messages[-1]["id"])
        reload_status, reloaded, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages/{edited_assistant_id}/reload",
            body={"idempotency_key": "reload-1"}, cookie=cookie, csrf=csrf,
        )
        reload_head = str(reloaded["chat_session"]["current_branch_head_id"])  # type: ignore[index]
        switch_status, switched, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/branches",
            body={"message_id": original_assistant_id, "idempotency_key": "switch-1"}, cookie=cookie, csrf=csrf,
        )
        assert edit_status == reload_status == switch_status == 200
        assert edited_messages[0]["id"] == original_user_id  # type: ignore[index]
        assert edited_messages[-2]["content"] == "修改后的问题"  # type: ignore[index]
        assert edited_messages[0]["content"] == "Deployment 如何管理 Pod？"  # type: ignore[index]
        assert reload_head != edited_assistant_id
        assert switched["chat_session"]["current_branch_head_id"] == original_assistant_id  # type: ignore[index]
        _validate(spec, "ChatSessionResponse", edited)
        _validate(spec, "ChatSessionResponse", reloaded)
        _validate(spec, "ChatSessionResponse", switched)

        stream_request = urllib.request.Request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/events/stream",
            headers={"Cookie": cookie, "Last-Event-ID": "1", "Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            lines = [stream.readline().decode().strip() for _ in range(3)]
        assert lines[0] == "id: 2"
        assert lines[1] == "event: chat"
        assert json.loads(lines[2].removeprefix("data: "))["type"] == "message.created"

        missing_csrf_status, missing_csrf, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}",
            body={"title": "手动标题", "idempotency_key": "rename-1"}, cookie=cookie, method="PATCH",
        )
        update_status, updated, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}",
            body={"title": "手动标题", "pinned": True, "idempotency_key": "rename-1"},
            cookie=cookie, csrf=csrf, method="PATCH",
        )
        search_status, search, _ = _request(
            f"{base_url}/api/v1/chat/sessions?query={urllib.parse.quote('ReplicaSet')}&filter=pinned",
            cookie=cookie,
        )
        _, viewer_csrf_payload, _ = _request(f"{base_url}/auth/csrf", cookie=viewer_cookie)
        hidden_update_status, hidden_update, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}",
            body={"archived": True, "idempotency_key": "viewer-archive"},
            cookie=viewer_cookie, csrf=str(viewer_csrf_payload["csrf_token"]), method="PATCH",
        )
        delete_status, deleted, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}",
            body={"idempotency_key": "delete-1"}, cookie=cookie, csrf=csrf, method="DELETE",
        )
        replay_delete_status, replay_deleted, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}",
            body={"idempotency_key": "delete-1"}, cookie=cookie, csrf=csrf, method="DELETE",
        )
        deleted_get_status, deleted_get, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}", cookie=cookie,
        )
        assert missing_csrf_status == 403 and missing_csrf["error"]["code"] == "csrf_required"  # type: ignore[index]
        assert update_status == search_status == delete_status == replay_delete_status == 200
        assert updated["chat_session"]["title"] == "手动标题"  # type: ignore[index]
        assert updated["chat_session"]["pinned"] is True  # type: ignore[index]
        assert search["chat_sessions"][0]["id"] == session_id  # type: ignore[index]
        assert hidden_update_status == 404 and hidden_update["error"]["code"] == "chat_not_found"  # type: ignore[index]
        assert {key: value for key, value in deleted.items() if key != "request_id"} == {
            key: value for key, value in replay_deleted.items() if key != "request_id"
        }
        assert deleted_get_status == 404 and deleted_get["error"]["code"] == "chat_not_found"  # type: ignore[index]
        _validate(spec, "ChatSessionResponse", updated)
        _validate(spec, "ChatSessionDeleteResponse", deleted)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_environment_chat_freezes_catalog_scope_and_replays_cited_tool_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    database = GatewayDatabase(tmp_path / "gateway.db")
    enrollments = ConnectorEnrollments(database, credential_factory=lambda: "connector-secret")
    commands = ConnectorCommands(
        database,
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
    _, team = IdentityAdministration(database).mutate(
        collection="teams", target_id=None, payload={"name": "Payments", "description": ""},
        actor_id="admin", reason="test", action="teams_create", request_id="team-1",
    )
    catalog = ResourceCatalog(database)
    [candidate] = catalog.refresh_discovery("cluster-prod", [
        DiscoveryObservation(namespace="shop", workload_kind="Deployment", workload_name="checkout-api"),
    ])
    service = catalog.create_service(
        team_id=str(team["id"]), name="Checkout", description="", actor_id="admin",
        reason="test", request_id="service-1",
    )
    binding = catalog.confirm_binding(
        candidate_id=str(candidate["id"]), service_id=str(service["id"]), actor_id="admin",
        reason="test", request_id="binding-1",
    )
    target_id = str(binding["deployment_target_id"])
    registry = MCPRegistry(
        database, id_factory=lambda: "mcp-metrics",
        revision_id=lambda: "mcp-revision:metrics",
    )
    registry.create(
        name="Prometheus", endpoint="https://mcp.example.test", credential=None,
        capabilities=[{"name": "query_metrics", "version": "prometheus-query-v1", "read_only": True}],
        allowed_scope=[{"cluster_id": "cluster-prod", "namespace": "shop"}], enabled=True,
        actor_id="admin", reason="test", request_id="mcp-create",
    )
    registry.verify(
        "mcp-metrics", actor_id="admin", reason="test", request_id="mcp-verify",
        probe=lambda *_args: {
            "status": "ok", "capabilities": [{
                "name": "query_metrics", "version": "prometheus-query-v1",
                "read_only": True, "mutation": False, "path": "/query_metrics",
            }],
        },
    )
    skills = SkillRegistry(database, id_factory=lambda: "skill-payments")
    skills.create(
        name="Payments triage",
        instruction="Check error-rate Observation before concluding.",
        workflow=["Query metrics", "Cite accepted Evidence"],
        applicable_scope=[{"cluster_id": "cluster-prod", "namespace": "shop"}],
        required_mcp=[{
            "integration_id": "mcp-metrics", "integration_revision": "mcp-revision:metrics",
            "name": "query_metrics", "version": "prometheus-query-v1",
        }],
        actor_id="admin", reason="test", request_id="skill-create",
    )
    skills.set_enabled(
        "skill-payments", version=1, expected_active_version=None,
        mcp_integrations=registry.list(), actor_id="admin", reason="test",
        request_id="skill-enable",
    )
    monkeypatch.setattr(gateway_main, "_DATABASE", database)
    monkeypatch.setattr(gateway_main, "_CONNECTOR_ENROLLMENTS", enrollments)
    monkeypatch.setattr(gateway_main, "_CONNECTOR_COMMANDS", commands)
    calls: list[dict[str, object]] = []

    def fake_model_and_mcp(request: dict[str, object]) -> dict[str, object]:
        calls.append(request)
        scope = request["scope"]
        assert isinstance(scope, dict)
        assert request["capabilities"] == {
            "query_metrics": {
                "name": "query_metrics", "version": "prometheus-query-v1",
                "enabled": True, "read_only": True, "mutation": False,
                "integration_id": "mcp-metrics",
                "integration_revision": "mcp-revision:metrics",
            },
        }
        assert request["skills"] == [{
            "id": "skill-payments", "name": "Payments triage", "version": 1,
            "instruction": "Check error-rate Observation before concluding.",
            "workflow": ["Query metrics", "Cite accepted Evidence"],
            "required_mcp": [{
                "integration_id": "mcp-metrics", "integration_revision": "mcp-revision:metrics",
                "name": "query_metrics", "version": "prometheus-query-v1",
            }],
        }]
        skill_versions = [{"id": "skill-payments", "name": "Payments triage", "version": 1}]
        return {
            "mode": "environment", "answer": "checkout 当前错误率为 42%。", "scope": scope,
            "tool_activity": [{
                "tool": "query_metrics", "status": "succeeded", "summary": "error_rate=0.42",
                "authorized_scope": {"deployment_target_id": target_id},
                "skill_versions": skill_versions,
            }],
            "evidence_references": ["evidence:metrics:1"],
            "uncertainty": {"status": "accepted", "reasons": []},
            "next_step": "继续观察。",
            "completion": {"status": "accepted", "stopping_reason": "validated"},
            "skill_versions": skill_versions,
        }

    monkeypatch.setattr(chat_http, "send_governed_chat", fake_model_and_mcp)
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _, _, set_cookie = _request(
            f"{base_url}/auth/login",
            body={"username": "admin", "password": "correct-horse-battery-staple", "session_mode": "cookie"},
        )
        cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
        _, csrf_payload, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
        csrf = str(csrf_payload["csrf_token"])
        _, created, _ = _request(
            f"{base_url}/api/v1/chat/sessions", body={"idempotency_key": "create-env"},
            cookie=cookie, csrf=csrf,
        )
        session_id = str(created["chat_session"]["id"])  # type: ignore[index]
        status, sent, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages",
            body={
                "content": "checkout 现在错误率高吗？", "idempotency_key": "env-1",
                "scope": {"cluster_id": "cluster-prod", "deployment_target_id": target_id},
            },
            cookie=cookie, csrf=csrf,
        )
        hidden_status, hidden, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/messages",
            body={
                "content": "查询隐藏资源", "idempotency_key": "env-hidden",
                "scope": {"cluster_id": "cluster-secret"},
            },
            cookie=cookie, csrf=csrf,
        )
        _, replay, _ = _request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/events?after=0&limit=200", cookie=cookie,
        )

        chat_session = sent["chat_session"]
        assistant = chat_session["messages"][-1]  # type: ignore[index]
        assert status == 200
        assert hidden_status == 404 and hidden["error"]["code"] == "chat_scope_not_found"  # type: ignore[index]
        assert calls[0]["scope"]["resources"][0]["deployment_target_id"] == target_id  # type: ignore[index]
        assert assistant["evidence_references"] == ["evidence:metrics:1"]
        assert assistant["tool_activity"][0]["status"] == "succeeded"
        assert assistant["skill_versions"] == [{
            "id": "skill-payments", "name": "Payments triage", "version": 1,
        }]
        assert assistant["tool_activity"][0]["skill_versions"] == assistant["skill_versions"]
        assert chat_session["selected_scope"]["revision"] == calls[0]["scope"]["revision"]  # type: ignore[index]
        assert len(chat_session["messages"]) == 2  # unauthorized request was not accepted
        assert replay["events"][-1]["type"] == "message.completed"  # type: ignore[index]
        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        _validate(spec, "ChatSessionResponse", sent)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
