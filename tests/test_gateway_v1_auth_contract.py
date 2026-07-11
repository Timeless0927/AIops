"""T03 local authentication and first V1 contract slice."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _request(
    url: str,
    *,
    body: dict | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
) -> tuple[int, dict, str | None]:
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


def _validate(spec: dict, schema_name: str, payload: dict) -> None:
    resolver = jsonschema.RefResolver.from_schema(spec)
    jsonschema.Draft202012Validator(spec["components"]["schemas"][schema_name], resolver=resolver).validate(payload)


def test_bootstrap_cookie_session_and_empty_incident_contract(tmp_path: Path, monkeypatch) -> None:
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
        denied_status, denied, _ = _request(f"{base_url}/api/v1/incidents")
        login_status, login, set_cookie = _request(
            f"{base_url}/auth/login",
            body={
                "username": "admin",
                "password": "correct-horse-battery-staple",
                "session_mode": "cookie",
            },
        )
        cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
        token = cookie.partition("=")[2]
        actor_status, actor, _ = _request(f"{base_url}/api/v1/actor", cookie=cookie)
        incidents_status, incidents, _ = _request(f"{base_url}/api/v1/incidents", cookie=cookie)
        csrf_status, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
        logout_denied_status, logout_denied, _ = _request(f"{base_url}/auth/logout", body={}, cookie=cookie)

        assert denied_status == 401
        assert denied["error"]["code"] == "unauthorized"
        assert login_status == actor_status == incidents_status == csrf_status == 200
        assert "token" not in login
        assert login["actor"]["roles"] == ["platform_administrator"]
        assert "approve_action" not in login["actor"]["capabilities"]
        assert set_cookie and "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie
        assert incidents["incidents"] == []
        assert logout_denied_status == 403
        assert logout_denied["error"]["code"] == "csrf_required"
        assert GatewayV1Store(tmp_path / "gateway.db").get(token).actor.username == "admin"
        _validate(spec, "LoginResponse", login)
        _validate(spec, "ActorResponse", actor)
        _validate(spec, "IncidentListResponse", incidents)
        _validate(spec, "ErrorResponse", denied)

        logout_status, _, _ = _request(
            f"{base_url}/auth/logout",
            body={},
            cookie=cookie,
            csrf=csrf["csrf_token"],
        )
        assert logout_status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    with sqlite3.connect(tmp_path / "gateway.db") as conn:
        password_hash = conn.execute("SELECT password FROM users WHERE username = 'admin'").fetchone()[0]
        migrations = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert password_hash.startswith("$argon2id$")
    assert migrations == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]
