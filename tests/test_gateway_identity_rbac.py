"""Gateway LDAP identity and RBAC authorization tests."""

from __future__ import annotations

import json
import subprocess
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
    resource_scope,
    role_permission_matrix,
)
from apps.aiops_k8s_gateway import main as gateway_main
from apps.cluster_connector import main as connector_main
from apps.cluster_connector.stream_client import ConnectorRegistration


def _write_identity_config(path: Path) -> None:
    path.write_text(
        """
identity:
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


def test_ldap_missing_config_returns_controlled_error() -> None:
    provider = IdentityProvider(IdentityConfig())

    with pytest.raises(IdentityError) as exc:
        provider.login("missing", "secret")

    assert exc.value.code == "ldap_not_configured"


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

        assert status == 200
        assert login_payload["actor"]["roles"] == ["user"]
        assert login_payload["role_permission_matrix"][ROLE_ADMIN]
        assert unauthorized_status == 401
        assert unauthorized_payload["error"]["code"] == "unauthorized"
        assert forbidden_status == 403
        assert forbidden_payload["error"]["code"] == "forbidden"
        assert sync_status == 403
        assert sync_payload["error"]["code"] == "forbidden"
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()


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
