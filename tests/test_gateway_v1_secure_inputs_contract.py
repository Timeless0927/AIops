"""K06 actor-scoped Secure Input HTTP contract tests."""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], str | None]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if cookie:
        request_headers["Cookie"] = cookie
    outgoing = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=request_headers, method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(outgoing, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def test_secure_input_http_never_returns_or_persists_plaintext(tmp_path: Path, monkeypatch) -> None:
    key_path = tmp_path / "change.key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_CHANGE_ENCRYPTION_KEY_PATH", str(key_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())

    try:
        unauthenticated_status, unauthenticated, _ = _request(
            f"{base_url}/api/v1/secure-inputs",
            body={
                "key_name": "database.password", "value": "must-never-persist",
                "idempotency_key": "unauthenticated",
            },
        )
        _, _, set_cookie = _request(
            f"{base_url}/auth/login",
            body={
                "username": "admin", "password": "correct-horse-battery-staple",
                "session_mode": "cookie",
            },
        )
        cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
        _, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
        write_headers = {"X-CSRF-Token": str(csrf["csrf_token"])}
        csrf_status, csrf_denied, _ = _request(
            f"{base_url}/api/v1/secure-inputs",
            body={
                "key_name": "database.password", "value": "must-never-persist",
                "idempotency_key": "missing-csrf",
            },
            cookie=cookie,
        )
        viewer_create_status, _, _ = _request(
            f"{base_url}/api/v1/admin/users",
            body={
                "username": "viewer", "display_name": "Viewer", "password": "viewer-pass",
                "reason": "Secure Input capability boundary test",
            },
            cookie=cookie, headers=write_headers,
        )
        _, _, viewer_set_cookie = _request(
            f"{base_url}/auth/login",
            body={"username": "viewer", "password": "viewer-pass", "session_mode": "cookie"},
        )
        viewer_cookie = viewer_set_cookie.split(";", 1)[0] if viewer_set_cookie else ""
        _, viewer_csrf, _ = _request(f"{base_url}/auth/csrf", cookie=viewer_cookie)
        capability_status, capability_denied, _ = _request(
            f"{base_url}/api/v1/secure-inputs",
            body={
                "key_name": "database.password", "value": "must-never-persist",
                "idempotency_key": "viewer-denied",
            },
            cookie=viewer_cookie,
            headers={"X-CSRF-Token": str(viewer_csrf["csrf_token"])},
        )
        status, supplied, _ = _request(
            f"{base_url}/api/v1/secure-inputs",
            body={
                "key_name": "database.password", "value": "must-never-persist",
                "idempotency_key": "secure-1",
            },
            cookie=cookie, headers=write_headers,
        )
        generated_status, generated, _ = _request(
            f"{base_url}/api/v1/secure-inputs",
            body={
                "key_name": "api.token", "generated_bytes": 32,
                "idempotency_key": "secure-2",
            },
            cookie=cookie, headers=write_headers,
        )
        input_id = str(supplied["secure_input"]["id"])  # type: ignore[index]
        read_status, projected, _ = _request(
            f"{base_url}/api/v1/secure-inputs/{input_id}", cookie=cookie,
        )

        assert unauthenticated_status == 401
        assert unauthenticated["error"]["code"] == "unauthorized"  # type: ignore[index]
        assert csrf_status == 403 and csrf_denied["error"]["code"] == "csrf_required"  # type: ignore[index]
        assert viewer_create_status == 201
        assert capability_status == 403
        assert capability_denied["error"]["code"] == "forbidden"  # type: ignore[index]
        assert (status, generated_status, read_status) == (201, 201, 200)
        assert supplied["secure_input"]["placeholder"] == f"{{{{secure-input:{input_id}}}}}"  # type: ignore[index]
        assert "value" not in supplied["secure_input"] and "value" not in generated["secure_input"]  # type: ignore[operator]
        assert projected["secure_input"]["sha256"] == supplied["secure_input"]["sha256"]  # type: ignore[index]
        assert "must-never-persist" not in json.dumps(supplied)
        assert b"must-never-persist" not in (tmp_path / "gateway.db").read_bytes()
        resolver = jsonschema.RefResolver.from_schema(spec)
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["SecureInputResponse"],  # type: ignore[index]
            resolver=resolver,
        ).validate(supplied)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    with sqlite3.connect(tmp_path / "gateway.db") as conn:
        audits = conn.execute(
            "SELECT actor_id, after_json, request_id FROM admin_audit "
            "WHERE action = 'secure_input_create' ORDER BY id",
        ).fetchall()
    assert len(audits) == 2
    assert [json.loads(str(row[1]))["key_name"] for row in audits] == [
        "database.password", "api.token",
    ]
    assert all("must-never-persist" not in str(row) for row in audits)
