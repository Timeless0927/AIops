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
    model_calls: list[list[dict[str, str]]] = []
    monkeypatch.setattr(
        chat_http,
        "send_knowledge_chat",
        lambda messages: model_calls.append(messages) or "Deployment 通过 ReplicaSet 滚动管理 Pod。",
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
        assert model_calls == [[{"role": "user", "content": "Deployment 如何管理 Pod？"}]]
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
