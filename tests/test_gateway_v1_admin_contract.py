"""T04 administration and live authorization contract."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    method: str = "GET",
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
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _login(base_url: str, username: str, password: str) -> tuple[str, str]:
    status, _, set_cookie = _request(
        f"{base_url}/auth/login",
        method="POST",
        body={"username": username, "password": password, "session_mode": "cookie"},
    )
    assert status == 200 and set_cookie
    cookie = set_cookie.split(";", 1)[0]
    csrf_status, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    assert csrf_status == 200
    return cookie, csrf["csrf_token"]


def test_platform_admin_manages_identity_with_fresh_auth_and_live_sessions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        admin_cookie, admin_csrf = _login(base_url, "admin", "admin-pass")
        actor_status, actor, _ = _request(f"{base_url}/api/v1/actor", cookie=admin_cookie)
        users_status, users, _ = _request(f"{base_url}/api/v1/admin/users", cookie=admin_cookie)
        admin_id = users["users"][0]["id"]
        admin_binding = users["role_bindings"][0]

        assert actor_status == users_status == 200
        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        jsonschema.Draft202012Validator(spec["components"]["schemas"]["AdminStateResponse"], resolver=resolver).validate(users)
        assert actor["actor"]["is_platform_administrator"] is True
        assert "approve_action" not in actor["actor"]["capabilities"]
        assert "execute_mutation" not in actor["actor"]["capabilities"]
        session_actor = GatewayV1Store(tmp_path / "gateway.db").get(admin_cookie.partition("=")[2]).actor
        assert "approve_action" not in session_actor.permissions()
        assert "execute_mutation" not in session_actor.permissions()
        bearer_status, bearer, _ = _request(
            f"{base_url}/auth/login",
            method="POST",
            body={"username": "admin", "password": "admin-pass"},
        )
        bearer_actor = GatewayV1Store(tmp_path / "gateway.db").get(bearer["token"]).actor
        assert bearer_status == 200
        assert bearer["actor"]["roles"] == ["platform_administrator"]
        assert "approve_action" not in bearer_actor.permissions()
        malformed_status, _, _ = _request(f"{base_url}/api/v1/admin/users/extra/path", cookie=admin_cookie)
        assert malformed_status == 404

        with sqlite3.connect(tmp_path / "gateway.db") as conn:
            conn.execute("UPDATE sessions SET fresh_at = 0")
            conn.commit()
        stale_status, stale, _ = _request(
            f"{base_url}/api/v1/admin/teams",
            method="POST",
            body={"name": "Payments", "reason": "建立责任团队"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        assert stale_status == 403
        assert stale["error"]["code"] == "fresh_auth_required"

        reauth_status, _, _ = _request(
            f"{base_url}/auth/reauth",
            method="POST",
            body={"password": "admin-pass"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        assert reauth_status == 200

        team_status, team_payload, _ = _request(
            f"{base_url}/api/v1/admin/teams",
            method="POST",
            body={"name": "Payments", "description": "支付服务责任团队", "reason": "建立责任团队"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        user_status, user_payload, _ = _request(
            f"{base_url}/api/v1/admin/users",
            method="POST",
            body={
                "username": "sre",
                "display_name": "值班 SRE",
                "password": "sre-pass",
                "reason": "创建值班用户",
            },
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        team_id = team_payload["team"]["id"]
        user_id = user_payload["user"]["id"]
        membership_status, membership_payload, _ = _request(
            f"{base_url}/api/v1/admin/team-memberships",
            method="POST",
            body={"user_id": user_id, "team_id": team_id, "reason": "加入支付团队"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        membership_id = membership_payload["team_membership"]["id"]
        user_update_status, user_update, _ = _request(
            f"{base_url}/api/v1/admin/users/{user_id}",
            method="PATCH",
            body={"display_name": "支付值班 SRE", "reason": "校正显示名称"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        team_update_status, team_update, _ = _request(
            f"{base_url}/api/v1/admin/teams/{team_id}",
            method="PATCH",
            body={"description": "支付服务与结算责任团队", "reason": "补充团队说明"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        membership_off_status, _, _ = _request(
            f"{base_url}/api/v1/admin/team-memberships/{membership_id}",
            method="PATCH",
            body={"active": False, "reason": "验证成员关系更新"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        membership_on_status, _, _ = _request(
            f"{base_url}/api/v1/admin/team-memberships/{membership_id}",
            method="PATCH",
            body={"active": True, "reason": "恢复成员关系"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        binding_status, binding_payload, _ = _request(
            f"{base_url}/api/v1/admin/role-bindings",
            method="POST",
            body={
                "user_id": user_id,
                "role": "sre",
                "scope_type": "team",
                "scope_id": team_id,
                "reason": "授予团队事件访问权",
            },
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        assert team_status == user_status == membership_status == binding_status == 201
        assert user_update_status == team_update_status == membership_off_status == membership_on_status == 200
        assert user_update["user"]["display_name"] == "支付值班 SRE"
        assert team_update["team"]["description"] == "支付服务与结算责任团队"

        sre_cookie, _ = _login(base_url, "sre", "sre-pass")
        denied_status, denied, _ = _request(f"{base_url}/api/v1/admin/users", cookie=sre_cookie)
        assert denied_status == 403
        assert denied["error"]["code"] == "forbidden"

        remove_status, _, _ = _request(
            f"{base_url}/api/v1/admin/role-bindings/{binding_payload['role_binding']['id']}",
            method="PATCH",
            body={"active": False, "reason": "撤销团队访问权"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        revoked_status, revoked, _ = _request(f"{base_url}/api/v1/actor", cookie=sre_cookie)
        assert remove_status == 200
        assert revoked_status == 401
        assert revoked["error"]["code"] == "unauthorized"

        password_cookie, _ = _login(base_url, "sre", "sre-pass")
        password_status, _, _ = _request(
            f"{base_url}/api/v1/admin/users/{user_id}",
            method="PATCH",
            body={"password": "new-sre-pass", "reason": "轮换本地密码"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        password_revoked_status, _, _ = _request(f"{base_url}/api/v1/actor", cookie=password_cookie)
        assert password_status == 200
        assert password_revoked_status == 401

        disabled_cookie, _ = _login(base_url, "sre", "new-sre-pass")
        disable_status, _, _ = _request(
            f"{base_url}/api/v1/admin/users/{user_id}",
            method="PATCH",
            body={"active": False, "reason": "停用离岗用户"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        disabled_status, _, _ = _request(f"{base_url}/api/v1/actor", cookie=disabled_cookie)
        assert disable_status == 200
        assert disabled_status == 401

        last_admin_status, last_admin, _ = _request(
            f"{base_url}/api/v1/admin/role-bindings/{admin_binding['id']}",
            method="PATCH",
            body={"active": False, "reason": "错误地撤销最后管理员"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        assert last_admin_status == 409
        assert last_admin["error"]["code"] == "last_admin"

        audit_status, audit, _ = _request(f"{base_url}/api/v1/admin/audit", cookie=admin_cookie)
        assert audit_status == 200
        assert any(row["target_id"] == user_id and row["result"] == "success" for row in audit["audit"])
        fresh_denial = next(row for row in audit["audit"] if row["result"] == "fresh_auth_required")
        assert fresh_denial["reason"] == "建立责任团队"
        assert "sre-pass" not in json.dumps(audit, ensure_ascii=False)
        assert all(row["actor_id"] == admin_id for row in audit["audit"] if row["actor_id"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()


def test_concurrent_role_removal_keeps_one_active_platform_administrator(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        admin_cookie, admin_csrf = _login(base_url, "admin", "admin-pass")
        _, initial, _ = _request(f"{base_url}/api/v1/admin/users", cookie=admin_cookie)
        admin_binding_id = initial["role_bindings"][0]["id"]
        create_status, created, _ = _request(
            f"{base_url}/api/v1/admin/users",
            method="POST",
            body={"username": "second-admin", "display_name": "第二管理员", "password": "second-pass", "reason": "建立备用管理员"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        second_id = created["user"]["id"]
        binding_status, binding, _ = _request(
            f"{base_url}/api/v1/admin/role-bindings",
            method="POST",
            body={"user_id": second_id, "role": "platform_administrator", "scope_type": "platform", "reason": "授予平台管理职责"},
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        second_binding_id = binding["role_binding"]["id"]
        second_cookie, second_csrf = _login(base_url, "second-admin", "second-pass")
        assert create_status == binding_status == 201

        def remove(binding_id: str, cookie: str, csrf: str) -> tuple[int, dict, str | None]:
            return _request(
                f"{base_url}/api/v1/admin/role-bindings/{binding_id}",
                method="PATCH",
                body={"active": False, "reason": "并发撤销验证"},
                cookie=cookie,
                csrf=csrf,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda args: remove(*args),
                    [
                        (admin_binding_id, admin_cookie, admin_csrf),
                        (second_binding_id, second_cookie, second_csrf),
                    ],
                )
            )

        assert sorted(status for status, _, _ in results) == [200, 409]
        assert next(payload for status, payload, _ in results if status == 409)["error"]["code"] == "last_admin"
        states = [_request(f"{base_url}/api/v1/admin/users", cookie=cookie) for cookie in (admin_cookie, second_cookie)]
        state = next(payload for status, payload, _ in states if status == 200)
        assert sum(binding["active"] and binding["role"] == "platform_administrator" for binding in state["role_bindings"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()


def test_admin_mutation_rolls_back_when_audit_cannot_commit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        cookie, csrf = _login(base_url, "admin", "admin-pass")
        with sqlite3.connect(tmp_path / "gateway.db") as conn:
            conn.execute(
                """
                CREATE TRIGGER reject_admin_audit BEFORE INSERT ON admin_audit
                BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END
                """
            )
        status, payload, _ = _request(
            f"{base_url}/api/v1/admin/teams",
            method="POST",
            body={"name": "Must Roll Back", "reason": "验证原子审计"},
            cookie=cookie,
            csrf=csrf,
        )
        with sqlite3.connect(tmp_path / "gateway.db") as conn:
            conn.execute("DROP TRIGGER reject_admin_audit")
        state_status, state, _ = _request(f"{base_url}/api/v1/admin/users", cookie=cookie)

        assert status == 500
        assert payload["error"]["code"] == "administration_write_failed"
        assert state_status == 200
        assert state["teams"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
