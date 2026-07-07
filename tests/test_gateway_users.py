"""Gateway user-management route tests."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import main as gateway_main


def _write_identity_config(path: Path, *, ldap: bool = False) -> None:
    ldap_block = (
        """
  ldap:
    enabled: true
    url: "ldaps://ldap.example"
    bind_dn: "cn=svc,dc=example"
    bind_password: "secret"
    user_base_dn: "ou=users,dc=example"
  group_role_map:
    cn=ops,ou=groups,dc=example: oncall_approver
  group_scope_map:
    cn=ops,ou=groups,dc=example:
      clusters: ["prod-a"]
      services: ["checkout"]
      teams: ["payments"]
      namespaces: ["default"]
"""
        if ldap
        else """
  ldap:
    enabled: false
"""
    )
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
{ldap_block}
  users:
    - username: admin
      password: admin-pass
      display_name: Admin
      roles: [admin]
      scope:
        clusters: ["*"]
        services: ["*"]
        teams: ["*"]
        namespaces: ["*"]
    - username: auditor
      password: auditor-pass
      display_name: Auditor
      roles: [auditor]
      scope:
        clusters: ["*"]
        services: ["*"]
        teams: ["*"]
        namespaces: ["*"]
""",
        encoding="utf-8",
    )


def _request_json(url: str, *, body: dict | None = None, token: str | None = None, method: str = "POST") -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _start_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    gateway_main._SESSIONS.clear()
    gateway_main._ROUTES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password})
    assert status == 200
    return str(payload["token"])


def test_admin_manages_users_and_list_shows_recent_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    db = gateway_main.audit_log.AuditLogDB(tmp_path / "data" / "audit_log.db")
    old_db = gateway_main.audit_log._DB
    gateway_main.audit_log._DB = db
    server, thread, base_url = _start_gateway(monkeypatch, tmp_path)

    try:
        admin_token = _login(base_url, "admin", "admin-pass")
        missing_scope_status, missing_scope_payload = _request_json(
            f"{base_url}/api/users",
            token=admin_token,
            body={
                "username": "operator",
                "password": "operator-pass",
                "display_name": "Operator",
                "roles": ["operator"],
                "scope": {"services": ["checkout"], "teams": ["payments"], "namespaces": ["default"]},
            },
        )
        create_status, create_payload = _request_json(
            f"{base_url}/api/users",
            token=admin_token,
            body={
                "username": "operator",
                "password": "operator-pass",
                "display_name": "Operator",
                "roles": ["operator"],
                "scope": {
                    "clusters": ["prod-a"],
                    "services": ["checkout"],
                    "teams": ["payments"],
                    "namespaces": ["default"],
                },
            },
        )
        operator_token = _login(base_url, "operator", "operator-pass")
        operator_write_status, operator_write_payload = _request_json(
            f"{base_url}/api/users",
            token=operator_token,
            body={
                "username": "blocked",
                "password": "blocked-pass",
                "roles": ["viewer"],
                "scope": {"clusters": ["prod-a"], "services": ["checkout"], "teams": ["payments"], "namespaces": ["default"]},
            },
        )
        list_status, list_payload = _request_json(f"{base_url}/api/users", token=admin_token, method="GET")
        auditor_token = _login(base_url, "auditor", "auditor-pass")
        auditor_list_status, _ = _request_json(f"{base_url}/api/users", token=auditor_token, method="GET")
        auditor_write_status, auditor_write_payload = _request_json(
            f"{base_url}/api/users/operator/disable",
            token=auditor_token,
            body={},
        )

        assert missing_scope_status == 400
        assert missing_scope_payload["error"]["code"] == "scope_required"
        assert create_status == 201
        assert create_payload["user"]["roles"] == ["operator"]
        assert create_payload["user"]["scope"]["clusters"] == ["prod-a"]
        assert operator_write_status == 403
        assert operator_write_payload["error"]["code"] == "forbidden"
        assert list_status == 200
        operator = next(user for user in list_payload["users"] if user["username"] == "operator")
        assert operator["source"] == "local"
        assert operator["disabled"] is False
        assert operator["last_login_at"] is not None
        assert operator["recent_permission_audit"]["action"] == "user_create"
        assert auditor_list_status == 200
        assert auditor_write_status == 403
        assert auditor_write_payload["error"]["code"] == "forbidden"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_db
        db.close()


def test_admin_update_disable_reset_and_last_admin_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    db = gateway_main.audit_log.AuditLogDB(tmp_path / "data" / "audit_log.db")
    old_db = gateway_main.audit_log._DB
    gateway_main.audit_log._DB = db
    server, thread, base_url = _start_gateway(monkeypatch, tmp_path)

    try:
        admin_token = _login(base_url, "admin", "admin-pass")
        self_disable_status, self_disable_payload = _request_json(
            f"{base_url}/api/users/admin/disable",
            token=admin_token,
            body={},
        )
        self_remove_status, self_remove_payload = _request_json(
            f"{base_url}/api/users/admin",
            token=admin_token,
            method="PATCH",
            body={
                "roles": ["viewer"],
                "scope": {"clusters": ["*"], "services": ["*"], "teams": ["*"], "namespaces": ["*"]},
            },
        )
        _request_json(
            f"{base_url}/api/users",
            token=admin_token,
            body={
                "username": "viewer",
                "password": "viewer-pass",
                "display_name": "Viewer",
                "roles": ["viewer"],
                "scope": {"clusters": ["prod-a"], "services": ["checkout"], "teams": ["payments"], "namespaces": ["default"]},
            },
        )
        update_status, update_payload = _request_json(
            f"{base_url}/api/users/viewer",
            token=admin_token,
            method="PATCH",
            body={
                "roles": ["approver", "operator"],
                "scope": {"clusters": ["prod-a"], "services": ["checkout"], "teams": ["payments"], "namespaces": ["staging"]},
            },
        )
        reset_status, _ = _request_json(
            f"{base_url}/api/users/viewer/reset-password",
            token=admin_token,
            body={"password": "new-viewer-pass"},
        )
        disable_status, disable_payload = _request_json(
            f"{base_url}/api/users/viewer/disable",
            token=admin_token,
            body={},
        )

        assert self_disable_status == 409
        assert self_disable_payload["error"]["code"] == "last_admin"
        assert self_remove_status == 409
        assert self_remove_payload["error"]["code"] == "last_admin"
        assert update_status == 200
        assert update_payload["user"]["roles"] == ["approver", "operator"]
        assert update_payload["user"]["scope"]["namespaces"] == ["staging"]
        assert reset_status == 200
        assert disable_status == 200
        assert disable_payload["user"]["disabled"] is True
        login_status, login_payload = _request_json(
            f"{base_url}/auth/login",
            body={"username": "viewer", "password": "new-viewer-pass"},
        )
        assert login_status == 401
        assert login_payload["error"]["code"] == "user_disabled"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_db
        db.close()


def test_ldap_user_lists_as_ldap_and_password_reset_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path, ldap=True)

    class FakeEntry:
        entry_dn = "uid=ldap-operator,ou=users,dc=example"
        cn = "LDAP Operator"
        mail = "ldap-operator@example.com"
        memberOf = ["cn=ops,ou=groups,dc=example"]
        department = "payments"

    class FakeConnection:
        def __init__(self, *_: object, auto_bind: bool = False, **__: object) -> None:
            self.entries = [FakeEntry()] if auto_bind else []
            self.auto_bind = auto_bind

        def search(self, *_: object, **__: object) -> bool:
            return True

        def bind(self) -> bool:
            return True

    class FakeLdap:
        class Server:
            def __init__(self, *_: object, **__: object) -> None:
                return None

        Connection = FakeConnection

    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "ldap3" else None)
    monkeypatch.setitem(sys.modules, "ldap3", FakeLdap)
    db = gateway_main.audit_log.AuditLogDB(tmp_path / "data" / "audit_log.db")
    old_db = gateway_main.audit_log._DB
    gateway_main.audit_log._DB = db
    server, thread, base_url = _start_gateway(monkeypatch, tmp_path)

    try:
        admin_token = _login(base_url, "admin", "admin-pass")
        ldap_token = _login(base_url, "ldap-operator", "ldap-pass")
        list_status, list_payload = _request_json(f"{base_url}/api/users", token=admin_token, method="GET")
        reset_status, reset_payload = _request_json(
            f"{base_url}/api/users/ldap-operator/reset-password",
            token=admin_token,
            body={"password": "new-pass"},
        )

        assert ldap_token
        assert list_status == 200
        ldap_user = next(user for user in list_payload["users"] if user["username"] == "ldap-operator")
        assert ldap_user["source"] == "ldap"
        assert ldap_user["roles"] == ["approver", "operator"]
        assert ldap_user["scope"]["clusters"] == ["prod-a"]
        assert reset_status == 403
        assert reset_payload["error"]["code"] == "ldap_password_reset_forbidden"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_db
        db.close()
