"""T05 private Chat Gateway HTTP/SSE contract."""

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
from apps.aiops_k8s_gateway.mcp_registry import MCPRegistry
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    authorization: str | None = None,
    csrf: str | None = None,
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
        method="POST" if body is not None else "GET",
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
    monkeypatch.setattr(gateway_main, "_SESSIONS", GatewayV1Store(tmp_path / "gateway.db"))
    model_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        chat_http,
        "send_governed_chat",
        lambda request: model_calls.append(request) or {
            "mode": "knowledge", "answer": "Deployment 通过 ReplicaSet 滚动管理 Pod。", "scope": None,
            "tool_activity": [], "evidence_references": [], "uncertainty": None, "next_step": None,
            "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
        },
    )
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

        gateway_main._SESSIONS.mutate_admin(
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
        assert [event["type"] for event in replay["events"]] == [  # type: ignore[index]
            "session.created", "message.created", "message.created", "message.completed",
        ]
        assert hidden_status == 404
        assert hidden_stream_status == 404
        assert hidden["error"]["code"] == "chat_not_found"  # type: ignore[index]
        assert hidden_stream["error"]["code"] == "chat_not_found"  # type: ignore[index]
        assert not {"evidence", "approval", "connector_command"} & set(json.dumps(fetched).lower().split())
        _validate(spec, "ChatSessionResponse", fetched)
        _validate(spec, "ChatSessionListResponse", listing)
        _validate(spec, "ChatEventsResponse", replay)

        stream_request = urllib.request.Request(
            f"{base_url}/api/v1/chat/sessions/{session_id}/events/stream",
            headers={"Cookie": cookie, "Last-Event-ID": "1", "Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            lines = [stream.readline().decode().strip() for _ in range(3)]
        assert lines[0] == "id: 2"
        assert lines[1] == "event: chat"
        assert json.loads(lines[2].removeprefix("data: "))["type"] == "message.created"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_environment_chat_freezes_catalog_scope_and_replays_cited_tool_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    store = GatewayV1Store(tmp_path / "gateway.db", credential_factory=lambda: "connector-secret")
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="enroll-1",
    )
    store.connector_enrollments.register(
        credential, "connector-prod", "cluster-prod", request_id="register-1",
    )
    _, team = store.mutate_admin(
        collection="teams", target_id=None, payload={"name": "Payments", "description": ""},
        actor_id="admin", reason="test", action="teams_create", request_id="team-1",
    )
    catalog = ResourceCatalog(store.database)
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
        store.database, id_factory=lambda: "mcp-metrics",
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
    monkeypatch.setattr(gateway_main, "_SESSIONS", store)
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
        return {
            "mode": "environment", "answer": "checkout 当前错误率为 42%。", "scope": scope,
            "tool_activity": [{
                "tool": "query_metrics", "status": "succeeded", "summary": "error_rate=0.42",
                "authorized_scope": {"deployment_target_id": target_id},
            }],
            "evidence_references": ["evidence:metrics:1"],
            "uncertainty": {"status": "accepted", "reasons": []},
            "next_step": "继续观察。",
            "completion": {"status": "accepted", "stopping_reason": "validated"},
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
        assert chat_session["selected_scope"]["revision"] == calls[0]["scope"]["revision"]  # type: ignore[index]
        assert len(chat_session["messages"]) == 2  # unauthorized request was not accepted
        assert replay["events"][-1]["type"] == "message.completed"  # type: ignore[index]
        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        _validate(spec, "ChatSessionResponse", sent)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
