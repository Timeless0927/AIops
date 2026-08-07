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
from apps.aiops_k8s_gateway.chat_attachments import ChatAttachments
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    raw: bytes | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    idempotency_key: str | None = None,
    method: str | None = None,
) -> tuple[int, bytes, object]:
    headers = {"Accept": "application/json"}
    data = raw
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def _json(result: tuple[int, bytes, object]) -> tuple[int, dict[str, object], object]:
    return result[0], json.loads(result[1]), result[2]


def test_gateway_attachment_upload_download_binding_and_privacy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    monkeypatch.setattr(gateway_main, "_SESSIONS", GatewayV1Store(tmp_path / "gateway.db"))
    monkeypatch.setattr(gateway_main, "ChatAttachments", lambda database: ChatAttachments(database, scanner=lambda _: True))
    monkeypatch.setattr(chat_http, "send_governed_chat", lambda _: {
        "mode": "knowledge", "answer": "已读取附件引用。", "scope": None, "tool_activity": [],
        "evidence_references": [], "uncertainty": None, "next_step": None,
        "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _, login, login_headers = _json(_request(f"{base}/auth/login", body={"username": "admin", "password": "correct-horse-battery-staple", "session_mode": "cookie"}))
        cookie = str(login_headers.get("Set-Cookie")).split(";", 1)[0]
        _, csrf_body, _ = _json(_request(f"{base}/auth/csrf", cookie=cookie))
        csrf = str(csrf_body["csrf_token"])
        _, created, _ = _json(_request(f"{base}/api/v1/chat/sessions", body={"idempotency_key": "session"}, cookie=cookie, csrf=csrf))
        session_id = str(created["chat_session"]["id"])  # type: ignore[index]
        reserve_status, reserved, _ = _json(_request(
            f"{base}/api/v1/chat/sessions/{session_id}/attachments",
            body={"filename": "../incident.log", "content_type": "text/plain", "size": 3, "idempotency_key": "reserve"},
            cookie=cookie, csrf=csrf,
        ))
        attachment_id = str(reserved["attachment"]["id"])  # type: ignore[index]
        denied_status, denied, _ = _json(_request(
            f"{base}/api/v1/chat/sessions/{session_id}/attachments/{attachment_id}/content",
            raw=b"ok\n", cookie=cookie, idempotency_key="upload", method="PUT",
        ))
        upload_status, uploaded, _ = _json(_request(
            f"{base}/api/v1/chat/sessions/{session_id}/attachments/{attachment_id}/content",
            raw=b"ok\n", cookie=cookie, csrf=csrf, idempotency_key="upload", method="PUT",
        ))
        replay_status, replayed, _ = _json(_request(
            f"{base}/api/v1/chat/sessions/{session_id}/attachments/{attachment_id}/content",
            raw=b"ok\n", cookie=cookie, csrf=csrf, idempotency_key="upload", method="PUT",
        ))
        conflict_status, conflict, _ = _json(_request(
            f"{base}/api/v1/chat/sessions/{session_id}/attachments/{attachment_id}/content",
            raw=b"no\n", cookie=cookie, csrf=csrf, idempotency_key="upload", method="PUT",
        ))
        list_status, listing, _ = _json(_request(f"{base}/api/v1/chat/sessions/{session_id}/attachments", cookie=cookie))
        search_status, search, _ = _json(_request(f"{base}/api/v1/chat/sessions?query=incident.log", cookie=cookie))
        download = _request(f"{base}/api/v1/chat/sessions/{session_id}/attachments/{attachment_id}/download", cookie=cookie)
        send_status, sent, _ = _json(_request(
            f"{base}/api/v1/chat/sessions/{session_id}/messages",
            body={"content": "分析附件", "attachment_ids": [attachment_id], "idempotency_key": "message"},
            cookie=cookie, csrf=csrf,
        ))
        _, bound, _ = _json(_request(f"{base}/api/v1/chat/sessions/{session_id}/attachments/{attachment_id}", cookie=cookie))

        gateway_main._SESSIONS.mutate_admin(
            collection="users", target_id=None,
            payload={"username": "viewer", "display_name": "Viewer", "email": "viewer@example.com", "password": "viewer-password"},
            actor_id="admin", reason="Attachment privacy", action="users_create", request_id="viewer",
        )
        _, _, viewer_headers = _json(_request(f"{base}/auth/login", body={"username": "viewer", "password": "viewer-password", "session_mode": "cookie"}))
        viewer_cookie = str(viewer_headers.get("Set-Cookie")).split(";", 1)[0]
        hidden_status, hidden, _ = _json(_request(f"{base}/api/v1/chat/sessions/{session_id}/attachments/{attachment_id}", cookie=viewer_cookie))

        assert login["status"] == "ok"
        assert reserve_status == 201
        assert denied_status == 403 and denied["error"]["code"] == "csrf_required"  # type: ignore[index]
        assert upload_status == replay_status == list_status == search_status == send_status == download[0] == 200
        assert uploaded["attachment"] == replayed["attachment"]
        assert uploaded["attachment"]["status"] == "ready"  # type: ignore[index]
        assert uploaded["attachment"]["filename"] == "incident.log"  # type: ignore[index]
        assert listing["attachments"] == [uploaded["attachment"]]
        assert search["chat_sessions"][0]["id"] == session_id  # type: ignore[index]
        assert download[1] == b"ok\n"
        assert conflict_status == 409 and conflict["error"]["code"] == "idempotency_conflict"  # type: ignore[index]
        assert bound["attachment"]["message_id"] == sent["chat_session"]["messages"][0]["id"]  # type: ignore[index]
        assert hidden_status == 404 and hidden["error"]["code"] == "attachment_not_found"  # type: ignore[index]

        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        jsonschema.Draft202012Validator(spec["components"]["schemas"]["ChatAttachmentResponse"], resolver=resolver).validate(uploaded)
        jsonschema.Draft202012Validator(spec["components"]["schemas"]["ChatAttachmentListResponse"], resolver=resolver).validate(listing)
    finally:
        server.shutdown()
        server.server_close()
