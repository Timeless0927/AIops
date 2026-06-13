"""Gateway LDAP identity and RBAC authorization tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from aiops.domain.identity import (
    IdentityConfig,
    IdentityError,
    IdentityProvider,
    PERMISSION_APPROVE_ACTION,
    PERMISSION_EXECUTE_MUTATION,
    PERMISSION_VIEW_INCIDENT,
    ROLE_ADMIN,
    ROLE_ONCALL_APPROVER,
    ROLE_USER,
    Scope,
    SQLiteIdentityStore,
    resource_scope,
    role_permission_matrix,
)
from apps.aiops_k8s_gateway import main as gateway_main
from apps.cluster_connector import main as connector_main
from apps.cluster_connector.stream_client import ConnectorRegistration


def _write_identity_config(path: Path) -> None:
    store_path = path.with_name("identity.db")
    path.write_text(
        f"""
identity:
  store_path: "{store_path}"
  ldap:
    enabled: false
  users:
    - username: admin
      password: admin-pass
      display_name: Admin
      roles: [admin]
      scope:
        services: ["*"]
        teams: ["*"]
        namespaces: ["*"]
    - username: alice
      password: alice-pass
      display_name: Alice
      roles: [user]
      scope:
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: bob
      password: bob-pass
      display_name: Bob
      roles: [oncall_approver]
      scope:
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default", "staging"]
    - username: disabled
      password: disabled-pass
      display_name: Disabled
      disabled: true
      roles: [user]
      scope:
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: noscope
      password: noscope-pass
      display_name: No Scope
      roles: [user]
    - username: oncall-noscope
      password: oncall-noscope-pass
      display_name: Oncall No Scope
      roles: [oncall_approver]
    - username: partial
      password: partial-pass
      display_name: Partial Scope
      roles: [oncall_approver]
      scope:
        services: ["checkout"]
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


def test_role_permission_matrix_covers_required_roles_and_permissions() -> None:
    matrix = role_permission_matrix()

    assert ROLE_ADMIN in matrix
    assert ROLE_USER in matrix
    assert ROLE_ONCALL_APPROVER in matrix
    assert PERMISSION_EXECUTE_MUTATION in matrix[ROLE_ADMIN]
    assert PERMISSION_APPROVE_ACTION in matrix[ROLE_ONCALL_APPROVER]
    assert PERMISSION_APPROVE_ACTION not in matrix[ROLE_USER]
    assert PERMISSION_VIEW_INCIDENT in matrix[ROLE_USER]


def test_actor_scope_limits_service_team_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    actor = IdentityProvider(IdentityConfig.load()).login("alice", "alice-pass")

    assert actor.can(PERMISSION_VIEW_INCIDENT, resource_scope(service="checkout", team="payments", namespace="default"))
    assert not actor.can(PERMISSION_VIEW_INCIDENT, resource_scope(service="checkout", team="payments", namespace="prod"))
    assert not actor.can(PERMISSION_APPROVE_ACTION, resource_scope(service="checkout", team="payments", namespace="default"))


def test_non_admin_without_explicit_scope_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    actor = IdentityProvider(IdentityConfig.load()).login("noscope", "noscope-pass")

    assert actor.scope == Scope()
    assert not actor.can(PERMISSION_VIEW_INCIDENT, resource_scope(service="checkout", team="payments", namespace="default"))


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("noscope", "noscope-pass"),
        ("oncall-noscope", "oncall-noscope-pass"),
        ("partial", "partial-pass"),
    ],
)
def test_non_admin_missing_scope_dimension_denies_k8s_incident_and_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    username: str,
    password: str,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    actor = IdentityProvider(IdentityConfig.load()).login(username, password)
    target = resource_scope(service="checkout", team="payments", namespace="default")

    assert not actor.can(PERMISSION_VIEW_INCIDENT, target)
    assert not actor.can("k8s_read", target)
    assert not actor.can(PERMISSION_APPROVE_ACTION, target)


def test_explicit_wildcard_scope_allows_non_admin_global_authorization(tmp_path: Path) -> None:
    actor = SQLiteIdentityStore(tmp_path / "identity.db").upsert_user(
        {
            "username": "wildcard",
            "password": "wildcard-pass",
            "display_name": "Wildcard",
            "roles": ["oncall_approver"],
            "scope": {"services": ["*"], "teams": ["*"], "namespaces": ["*"]},
        }
    )

    target = resource_scope(service="any-service", team="any-team", namespace="any-ns")

    assert actor.can(PERMISSION_VIEW_INCIDENT, target)
    assert actor.can("k8s_read", target)
    assert actor.can(PERMISSION_APPROVE_ACTION, target)


def test_sqlite_identity_store_seeds_builtin_roles_users_and_scopes(tmp_path: Path) -> None:
    db_path = tmp_path / "identity.db"
    store = SQLiteIdentityStore(db_path)
    store.seed_users(
        (
            {
                "username": "seeded",
                "password": "seeded-pass",
                "display_name": "Seeded User",
                "roles": ["user"],
                "scope": {"services": ["checkout"], "teams": ["payments"], "namespaces": ["default"]},
            },
        )
    )

    actor = store.authenticate_seed_user("seeded", "seeded-pass")
    role_rows = sqlite3.connect(db_path).execute("SELECT name FROM roles ORDER BY name").fetchall()

    assert actor is not None
    assert actor.roles == ("user",)
    assert actor.scope == Scope(services=("checkout",), teams=("payments",), namespaces=("default",))
    assert {row[0] for row in role_rows} >= {ROLE_ADMIN, ROLE_USER, ROLE_ONCALL_APPROVER}
    assert store.authenticate_seed_user("missing", "bad") is None
    store.close()


def test_sqlite_identity_store_fails_closed_for_disabled_user(tmp_path: Path) -> None:
    store = SQLiteIdentityStore(tmp_path / "identity.db")
    store.seed_users(
        (
            {
                "username": "disabled",
                "password": "disabled-pass",
                "display_name": "Disabled",
                "disabled": True,
                "roles": ["user"],
                "scope": {"namespaces": ["default"]},
            },
        )
    )

    with pytest.raises(IdentityError) as exc:
        store.authenticate_seed_user("disabled", "disabled-pass")

    assert exc.value.code == "user_disabled"
    store.close()


def test_identity_provider_uses_sqlite_seed_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))

    provider = IdentityProvider(IdentityConfig.load())
    users = provider.sync_users()
    actor = provider.login("alice", "alice-pass")

    assert actor.auth_source == "sqlite"
    assert actor.can(PERMISSION_VIEW_INCIDENT, resource_scope(service="checkout", team="payments", namespace="default"))
    assert {user.username for user in users} >= {"admin", "alice", "bob"}
    with pytest.raises(IdentityError) as exc:
        provider.login("disabled", "disabled-pass")
    assert exc.value.code == "user_disabled"


def test_ldap_missing_config_returns_controlled_error() -> None:
    provider = IdentityProvider(IdentityConfig())

    with pytest.raises(IdentityError) as exc:
        provider.login("missing", "secret")

    assert exc.value.code == "ldap_not_configured"


def test_ldap_connection_failure_returns_controlled_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = IdentityConfig(
        ldap=IdentityConfig.load().ldap.__class__(
            enabled=True,
            url="ldaps://ldap.example",
            bind_dn="cn=svc,dc=example",
            bind_password="secret",
            user_base_dn="ou=users,dc=example",
        ),
        store_path=str(tmp_path / "identity.db"),
    )

    class FakeLdap:
        class Server:  # noqa: D401
            def __init__(self, *_: object, **__: object) -> None:
                raise OSError("ldap offline")

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "ldap3" else None)
    monkeypatch.setitem(sys.modules, "ldap3", FakeLdap)

    with pytest.raises(IdentityError) as exc:
        IdentityProvider(config).login("alice", "alice-pass")

    assert exc.value.code == "ldap_unavailable"


def test_ldap_search_failure_returns_controlled_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = IdentityConfig(
        ldap=IdentityConfig.load().ldap.__class__(
            enabled=True,
            url="ldaps://ldap.example",
            bind_dn="cn=svc,dc=example",
            bind_password="secret",
            user_base_dn="ou=users,dc=example",
        ),
        store_path=str(tmp_path / "identity.db"),
    )

    class FakeConnection:
        entries: list[object] = []

        def __init__(self, *_: object, **__: object) -> None:
            return None

        def search(self, *_: object, **__: object) -> bool:
            raise RuntimeError("search failed")

    class FakeLdap:
        class Server:
            def __init__(self, *_: object, **__: object) -> None:
                return None

        Connection = FakeConnection

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "ldap3" else None)
    monkeypatch.setitem(sys.modules, "ldap3", FakeLdap)

    with pytest.raises(IdentityError) as exc:
        IdentityProvider(config).login("alice", "alice-pass")

    assert exc.value.code == "ldap_unavailable"


@pytest.mark.parametrize("failure", ["service_connection", "user_connection", "user_bind_exception"])
def test_ldap_connection_and_bind_exceptions_return_controlled_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    config = IdentityConfig(
        ldap=IdentityConfig.load().ldap.__class__(
            enabled=True,
            url="ldaps://ldap.example",
            bind_dn="cn=svc,dc=example",
            bind_password="secret",
            user_base_dn="ou=users,dc=example",
        ),
        store_path=str(tmp_path / "identity.db"),
    )

    class FakeEntry:
        entry_dn = "uid=alice,ou=users,dc=example"
        cn = "Alice"
        mail = "alice@example.com"
        memberOf: list[str] = []
        department = "payments"

    class FakeConnection:
        def __init__(self, *_: object, auto_bind: bool = False, **__: object) -> None:
            if failure == "service_connection" and auto_bind:
                raise RuntimeError("service bind failed")
            if failure == "user_connection" and not auto_bind:
                raise RuntimeError("user connection failed")
            self.entries = [FakeEntry()] if auto_bind else []
            self.auto_bind = auto_bind

        def search(self, *_: object, **__: object) -> bool:
            return True

        def bind(self) -> bool:
            if failure == "user_bind_exception":
                raise RuntimeError("user bind exploded")
            return True

    class FakeLdap:
        class Server:
            def __init__(self, *_: object, **__: object) -> None:
                return None

        Connection = FakeConnection

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "ldap3" else None)
    monkeypatch.setitem(sys.modules, "ldap3", FakeLdap)

    with pytest.raises(IdentityError) as exc:
        IdentityProvider(config).login("alice", "alice-pass")

    assert exc.value.code == "ldap_unavailable"


def test_ldap_user_bind_false_remains_invalid_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = IdentityConfig(
        ldap=IdentityConfig.load().ldap.__class__(
            enabled=True,
            url="ldaps://ldap.example",
            bind_dn="cn=svc,dc=example",
            bind_password="secret",
            user_base_dn="ou=users,dc=example",
        ),
        store_path=str(tmp_path / "identity.db"),
    )

    class FakeEntry:
        entry_dn = "uid=alice,ou=users,dc=example"
        cn = "Alice"
        mail = "alice@example.com"
        memberOf: list[str] = []
        department = "payments"

    class FakeConnection:
        def __init__(self, *_: object, auto_bind: bool = False, **__: object) -> None:
            self.entries = [FakeEntry()] if auto_bind else []
            self.auto_bind = auto_bind

        def search(self, *_: object, **__: object) -> bool:
            return True

        def bind(self) -> bool:
            return False

    class FakeLdap:
        class Server:
            def __init__(self, *_: object, **__: object) -> None:
                return None

        Connection = FakeConnection

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "ldap3" else None)
    monkeypatch.setitem(sys.modules, "ldap3", FakeLdap)

    with pytest.raises(IdentityError) as exc:
        IdentityProvider(config).login("alice", "wrong-pass")

    assert exc.value.code == "invalid_credentials"


def test_gateway_login_ldap_unavailable_returns_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    config_path.write_text(
        f"""
identity:
  store_path: "{tmp_path / "identity.db"}"
  ldap:
    enabled: true
    url: "ldaps://ldap.example"
    bind_dn: "cn=svc,dc=example"
    bind_password: "secret"
    user_base_dn: "ou=users,dc=example"
""",
        encoding="utf-8",
    )

    class FakeLdap:
        class Server:
            def __init__(self, *_: object, **__: object) -> None:
                raise OSError("ldap offline")

    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "ldap3" else None)
    monkeypatch.setitem(sys.modules, "ldap3", FakeLdap)
    gateway_main._SESSIONS.clear()
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()
    gateway_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        status, payload = _request_json(
            f"{gateway_url}/auth/login",
            body={"username": "alice", "password": "alice-pass"},
        )

        assert status == 503
        assert payload["error"]["code"] == "ldap_unavailable"
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()


def test_gateway_login_and_authorization_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        status, login_payload = _request_json(
            f"{gateway_url}/auth/login",
            body={"username": "alice", "password": "alice-pass"},
        )
        token = login_payload["token"]

        unauthorized_status, unauthorized_payload = _request_json(
            f"{gateway_url}/k8s/read",
            body={"cluster_id": "cluster-local", "namespace": "default"},
        )
        forbidden_status, forbidden_payload = _request_json(
            f"{gateway_url}/k8s/read",
            body={"cluster_id": "cluster-local", "namespace": "prod"},
            token=token,
        )
        sync_status, sync_payload = _request_json(f"{gateway_url}/auth/sync", body={}, token=token)
        noscope_login_status, noscope_login_payload = _request_json(
            f"{gateway_url}/auth/login",
            body={"username": "noscope", "password": "noscope-pass"},
        )
        noscope_status, noscope_payload = _request_json(
            f"{gateway_url}/k8s/read",
            body={"cluster_id": "cluster-local", "namespace": "default"},
            token=noscope_login_payload["token"],
        )

        assert status == 200
        assert noscope_login_status == 200
        assert login_payload["actor"]["roles"] == ["user"]
        assert login_payload["role_permission_matrix"][ROLE_ADMIN]
        assert unauthorized_status == 401
        assert unauthorized_payload["error"]["code"] == "unauthorized"
        assert forbidden_status == 403
        assert forbidden_payload["error"]["code"] == "forbidden"
        assert noscope_status == 403
        assert noscope_payload["error"]["code"] == "forbidden"
        assert sync_status == 403
        assert sync_payload["error"]["code"] == "forbidden"
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()


def test_gateway_auth_rejections_are_audited_with_permission_and_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    db = gateway_main.audit_log.AuditLogDB(tmp_path / "data" / "audit_log.db")
    old_db = gateway_main.audit_log._DB
    gateway_main.audit_log._DB = db
    gateway_main._SESSIONS.clear()
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()
    gateway_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        _, login_payload = _request_json(
            f"{gateway_url}/auth/login",
            body={"username": "alice", "password": "alice-pass"},
        )
        token = login_payload["token"]
        unauthorized_status, unauthorized_payload = _request_json(
            f"{gateway_url}/k8s/read",
            body={"cluster_id": "cluster-local", "namespace": "default"},
        )
        forbidden_status, forbidden_payload = _request_json(
            f"{gateway_url}/k8s/read",
            body={"cluster_id": "cluster-local", "namespace": "prod"},
            token=token,
        )
        rows = asyncio_run(gateway_main.audit_log.query_audit(limit=20))

        assert unauthorized_status == 401
        assert forbidden_status == 403
        decisions = {(row["permission"], row["decision"], row["request_id"]) for row in rows}
        assert ("k8s_read", "deny", unauthorized_payload["request_id"]) in decisions
        assert ("k8s_read", "deny", forbidden_payload["request_id"]) in decisions
        forbidden_row = next(row for row in rows if row["request_id"] == forbidden_payload["request_id"])
        assert forbidden_row["actor"] == "alice"
        assert forbidden_row["role"] == "user"
        assert "prod" in str(forbidden_row["resource_scope"])
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_db
        db.close()


def asyncio_run(coro):  # noqa: ANN001, ANN201
    import asyncio

    return asyncio.run(coro)


def test_gateway_incident_query_filters_service_team_and_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setattr(
        gateway_main.incident_store,
        "list_active",
        lambda: _async_value(
            [
                {"id": "allowed", "service": "checkout", "team": "payments", "namespace": "default"},
                {"id": "wrong-service", "service": "billing", "team": "payments", "namespace": "default"},
                {"id": "wrong-team", "service": "checkout", "team": "platform", "namespace": "default"},
                {"id": "wrong-namespace", "service": "checkout", "team": "payments", "namespace": "prod"},
            ]
        ),
    )
    gateway_main._SESSIONS.clear()
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()
    gateway_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        _, login_payload = _request_json(
            f"{gateway_url}/auth/login",
            body={"username": "alice", "password": "alice-pass"},
        )
        status, payload = _request_json(f"{gateway_url}/incidents/query", body={}, token=login_payload["token"])

        assert status == 200
        assert [incident["id"] for incident in payload["incidents"]] == ["allowed"]
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()


def test_gateway_incident_query_uses_persisted_scope_and_fails_closed_for_missing_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    store = gateway_main.incident_store.IncidentStore(tmp_path / "data" / "incidents.db")
    old_store = gateway_main.incident_store._STORE
    gateway_main.incident_store._STORE = store
    allowed_id = asyncio_run(
        gateway_main.incident_store.create_incident(
            "AllowedCheckout",
            "default",
            "prod-a",
            "checkout degraded",
            service="checkout",
            team="payments",
        )
    )
    asyncio_run(
        gateway_main.incident_store.create_incident(
            "WrongService",
            "default",
            "prod-a",
            "billing degraded",
            service="billing",
            team="payments",
        )
    )
    asyncio_run(
        gateway_main.incident_store.create_incident(
            "WrongTeam",
            "default",
            "prod-a",
            "platform owned checkout alert",
            service="checkout",
            team="platform",
        )
    )
    asyncio_run(
        gateway_main.incident_store.create_incident(
            "LegacyMissingScope",
            "default",
            "prod-a",
            "legacy row without service/team must not be visible",
        )
    )
    gateway_main._SESSIONS.clear()
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()
    gateway_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        _, login_payload = _request_json(
            f"{gateway_url}/auth/login",
            body={"username": "alice", "password": "alice-pass"},
        )
        status, payload = _request_json(f"{gateway_url}/incidents/query", body={}, token=login_payload["token"])

        assert status == 200
        assert [incident["id"] for incident in payload["incidents"]] == [allowed_id]
        assert payload["incidents"][0]["service"] == "checkout"
        assert payload["incidents"][0]["team"] == "payments"
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.incident_store._STORE = old_store
        store.close()


async def _async_value(value):  # noqa: ANN001, ANN201
    return value


def test_gateway_k8s_read_requires_server_auth_and_returns_audit_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    gateway_main._SESSIONS.clear()
    gateway_main._ROUTES.clear()
    connector_main.ConnectorHandler.registration = ConnectorRegistration(
        connector_id="connector-local",
        cluster_id="cluster-local",
        namespace_scope=("default",),
        capabilities=("execute_read",),
    )
    connector_main.ConnectorHandler.gateway_url = ""
    connector_main.ConnectorHandler.registered_with_gateway = False

    connector_server = ThreadingHTTPServer(("127.0.0.1", 0), connector_main.ConnectorHandler)
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    connector_thread = threading.Thread(target=connector_server.serve_forever, daemon=True)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    connector_thread.start()
    gateway_thread.start()
    connector_url = f"http://127.0.0.1:{connector_server.server_address[1]}"
    gateway_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"
    monkeypatch.setenv("AIOPS_CONNECTOR_URL", connector_url)
    real_popen = subprocess.Popen

    try:
        with patch(
            "apps.cluster_connector.kubectl_executor.subprocess.Popen",
            side_effect=lambda argv, **kwargs: real_popen(  # noqa: S603, ARG005
                ["python3", "-c", "import sys; sys.stdout.write('NAME READY\\napi 1/1\\n')"],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
            ),
        ):
            _, login_payload = _request_json(
                f"{gateway_url}/auth/login",
                body={"username": "bob", "password": "bob-pass"},
            )
            token = login_payload["token"]
            register_status, _ = _request_json(
                f"{gateway_url}/connectors/register",
                body={
                    "connector_id": "connector-local",
                    "cluster_id": "cluster-local",
                    "namespace_scope": ["default"],
                    "capabilities": ["execute_read"],
                },
            )
            read_status, read_payload = _request_json(
                f"{gateway_url}/k8s/read",
                body={
                    "cluster_id": "cluster-local",
                    "namespace": "default",
                    "service": "checkout",
                    "team": "payments",
                    "argv": ["kubectl", "get", "pods", "-n", "default"],
                    "reason": "authorized gateway read",
                    "task_id": "task-rbac",
                    "command_id": "cmd-rbac",
                },
                token=token,
            )

        assert register_status == 201
        assert read_status == 200
        assert read_payload["status"] == "succeeded"
        assert read_payload["audit"]["actor"] == "bob"
        assert read_payload["audit"]["roles"] == ["oncall_approver"]
        assert read_payload["audit"]["request_id"].startswith("req-")
        assert read_payload["audit"]["scope"] == {
            "services": ["checkout"],
            "teams": ["payments"],
            "namespaces": ["default", "staging"],
        }
    finally:
        connector_server.shutdown()
        gateway_server.shutdown()
        connector_server.server_close()
        gateway_server.server_close()
        connector_thread.join(timeout=2)
        gateway_thread.join(timeout=2)
        gateway_main._ROUTES.clear()
        gateway_main._SESSIONS.clear()
