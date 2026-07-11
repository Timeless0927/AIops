"""Console Next cluster registry route tests."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import main as gateway_main
from aiops.domain import cluster_registry


def _write_identity_config(path: Path) -> None:
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
  ldap:
    enabled: false
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


def _request_json(
    url: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    method: str = "POST",
) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password})
    assert status == 200
    return str(payload["token"])


def test_cluster_registry_health_admin_write_and_offline_mutation_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    data_dir = tmp_path / "data"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(data_dir))
    gateway_main._SESSIONS.clear()
    gateway_main._ROUTES.clear()

    old_audit_db = gateway_main.audit_log._DB
    old_cluster_db = cluster_registry._DB
    audit_db = gateway_main.audit_log.AuditLogDB(data_dir / "audit_log.db")
    clusters_db = cluster_registry.ClusterDB(data_dir / "clusters.db")
    gateway_main.audit_log._DB = audit_db
    cluster_registry._DB = clusters_db

    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        admin_token = _login(base_url, "admin", "admin-pass")
        auditor_token = _login(base_url, "auditor", "auditor-pass")

        auditor_write_status, auditor_write = _request_json(
            f"{base_url}/api/clusters",
            token=auditor_token,
            body={"cluster_id": "prod-a", "environment": "prod"},
        )
        secret_status, secret_payload = _request_json(
            f"{base_url}/api/clusters",
            token=admin_token,
            body={"cluster_id": "prod-a", "openobserve_token": "hidden"},
        )
        url_ref_status, url_ref_payload = _request_json(
            f"{base_url}/api/clusters",
            token=admin_token,
            body={"cluster_id": "prod-a", "openobserve_config_ref": "http://openobserve.internal"},
        )
        invalid_env_status, invalid_env_payload = _request_json(
            f"{base_url}/api/clusters",
            token=admin_token,
            body={"cluster_id": "prod-a", "environment": "qa"},
        )
        save_status, save_payload = _request_json(
            f"{base_url}/api/clusters",
            token=admin_token,
            body={
                "cluster_id": "prod-a",
                "display_name": "Production A",
                "environment": "staging",
                "default_namespace_scope": "default,payments",
                "owner_team": "platform",
                "automatic_actions_enabled": True,
                "openobserve_config_ref": "oo-prod-a",
            },
        )
        unconfigured_status, _ = _request_json(
            f"{base_url}/api/clusters/runtime",
            body={
                "cluster_id": "shadow-a",
                "connector_id": "conn-shadow",
                "connector_status": "online",
                "openobserve_status": "ok",
                "scope_field_mapping_health": "healthy",
                "recent_query_health": "ok",
                "failure_summary": "token should be redacted",
            },
        )
        list_status, list_payload = _request_json(f"{base_url}/api/clusters", token=auditor_token, method="GET")
        action_status, action_payload = _request_json(
            f"{base_url}/api/actions/propose",
            token=admin_token,
            body={
                "action_type": "restart_deployment",
                "cluster": "prod-a",
                "namespace": "default",
                "service": "checkout",
                "team": "payments",
                "deployment": "checkout",
                "risk_level": "low",
                "idempotency_key": "cluster-offline",
            },
        )
        runtime_status, runtime_payload = _request_json(
            f"{base_url}/connectors/register",
            body={
                "cluster_id": "prod-a",
                "connector_id": "conn-prod",
                "openobserve_status": "ok",
                "scope_field_mapping_health": "healthy",
                "recent_query_health": "ok",
                "failure_summary": "",
            },
        )
        refreshed_status, refreshed_payload = _request_json(f"{base_url}/api/clusters", token=auditor_token, method="GET")
        audit_rows = asyncio.run(gateway_main.audit_log.query_audit(cluster="prod-a", limit=20))

        assert auditor_write_status == 403
        assert auditor_write["error"]["code"] == "forbidden"
        assert secret_status == 400
        assert secret_payload["error"]["code"] == "secret_field_forbidden"
        assert url_ref_status == 400
        assert url_ref_payload["error"]["code"] == "secret_field_forbidden"
        assert invalid_env_status == 400
        assert invalid_env_payload["error"]["code"] == "invalid_environment"
        assert save_status == 200
        assert save_payload["cluster"]["cluster_id"] == "prod-a"
        assert save_payload["cluster"]["environment"] == "staging"
        assert save_payload["cluster"]["runtime_state"]["connector_status"] == "offline"
        assert save_payload["cluster"]["mutation_enabled"] is False
        assert unconfigured_status == 200
        assert list_status == 200
        clusters = {item["cluster_id"]: item for item in list_payload["clusters"]}
        assert clusters["prod-a"]["openobserve_config_ref"] == "oo-prod-a"
        assert clusters["shadow-a"]["configuration_status"] == "unconfigured"
        assert clusters["shadow-a"]["effective_environment"] == "prod"
        assert clusters["shadow-a"]["automatic_actions_enabled"] is False
        assert clusters["shadow-a"]["runtime_state"]["failure_summary"] == "[redacted]"
        assert action_status == 409
        assert action_payload["error"]["code"] == "connector_offline"
        assert runtime_status == 401
        assert runtime_payload["error"]["code"] == "invalid_connector_credential"
        assert refreshed_status == 200
        prod = {item["cluster_id"]: item for item in refreshed_payload["clusters"]}["prod-a"]
        assert prod["runtime_state"]["connector_status"] == "offline"
        assert prod["mutation_enabled"] is False
        serialized = json.dumps(refreshed_payload, sort_keys=True).lower()
        for forbidden in ("token", "secret", "internal_url", "database_path"):
            assert forbidden not in serialized
        assert any(row["what"] == "cluster_config_save" and row["result"] == "success" for row in audit_rows)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main._ROUTES.clear()
        gateway_main.audit_log._DB = old_audit_db
        cluster_registry._DB = old_cluster_db
        audit_db.close()
        clusters_db.close()
