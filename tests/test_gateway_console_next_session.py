"""Gateway Console Next session, CSRF, and asset fallback tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import main as gateway_main


def _write_identity_config(path: Path) -> None:
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
  ldap:
    enabled: false
  users:
    - username: alice
      password: alice-pass
      display_name: Alice
      roles: [user]
      scope:
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
""",
        encoding="utf-8",
    )


def _request(
    url: str,
    *,
    body: dict | None = None,
    method: str = "GET",
    token: str | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
) -> tuple[int, dict, dict[str, str]]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}"), dict(exc.headers)


def _request_text(url: str) -> tuple[int, str, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.read().decode("utf-8"), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers.get("Content-Type", "")


def test_gateway_cookie_session_csrf_and_bearer_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    gateway_main._SESSIONS.clear()
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()
    gateway_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        status, login, headers = _request(
            f"{gateway_url}/auth/login",
            method="POST",
            body={"username": "alice", "password": "alice-pass"},
        )
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        cookie_me_status, cookie_me, _ = _request(f"{gateway_url}/auth/me", cookie=cookie)
        bearer_me_status, bearer_me, _ = _request(f"{gateway_url}/auth/me", token=login["token"])
        csrf_status, csrf_payload, _ = _request(f"{gateway_url}/auth/csrf", cookie=cookie)
        denied_status, denied_payload, _ = _request(f"{gateway_url}/auth/logout", method="POST", body={}, cookie=cookie)
        logout_status, _, logout_headers = _request(
            f"{gateway_url}/auth/logout",
            method="POST",
            body={},
            cookie=cookie,
            csrf=csrf_payload["csrf_token"],
        )

        assert status == 200
        assert "HttpOnly" in headers["Set-Cookie"]
        assert "SameSite=Lax" in headers["Set-Cookie"]
        assert login["token"]
        assert cookie_me_status == 200
        assert cookie_me["actor"]["username"] == "alice"
        assert bearer_me_status == 200
        assert bearer_me["actor"]["username"] == "alice"
        assert csrf_status == 200
        assert denied_status == 403
        assert denied_payload["error"]["code"] == "csrf_required"
        assert logout_status == 200
        assert "Max-Age=0" in logout_headers["Set-Cookie"]
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()


def test_gateway_serves_console_assets_with_explicit_app_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<div id="root"></div><script src="/assets/app-a1b2.js"></script>', encoding="utf-8")
    (dist / "assets" / "app-a1b2.js").write_text("console.log('hashed asset')", encoding="utf-8")
    monkeypatch.setenv("AIOPS_CONSOLE_DIST_DIR", str(dist))
    gateway_main._SESSIONS.clear()
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()
    gateway_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        app_status, app_body, app_type = _request_text(f"{gateway_url}/incidents/demo-checkout")
        asset_status, asset_body, asset_type = _request_text(f"{gateway_url}/assets/app-a1b2.js")
        api_status, api_body, api_type = _request_text(f"{gateway_url}/api/does-not-exist")
        unknown_status, unknown_body, _ = _request_text(f"{gateway_url}/not-a-console-route")

        assert app_status == 200
        assert "root" in app_body
        assert app_type.startswith("text/html")
        assert asset_status == 200
        assert "hashed asset" in asset_body
        assert "javascript" in asset_type
        assert api_status == 404
        assert '"status":"not_found"' in api_body
        assert api_type.startswith("application/json")
        assert unknown_status == 404
        assert '"status":"not_found"' in unknown_body
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
