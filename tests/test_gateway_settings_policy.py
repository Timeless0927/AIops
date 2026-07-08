"""Gateway settings and policy route tests."""

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
from apps.aiops_k8s_gateway import settings_service


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


def test_settings_versions_admin_auditor_policy_hits_and_secret_guard(
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
    old_settings_db = settings_service._DB
    audit_db = gateway_main.audit_log.AuditLogDB(data_dir / "audit_log.db")
    settings_db = settings_service.SettingsDB(data_dir / "settings.db")
    gateway_main.audit_log._DB = audit_db
    settings_service._DB = settings_db

    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        admin_token = _login(base_url, "admin", "admin-pass")
        auditor_token = _login(base_url, "auditor", "auditor-pass")

        admin_get_status, admin_get = _request_json(f"{base_url}/api/settings", token=admin_token, method="GET")
        auditor_get_status, auditor_get = _request_json(f"{base_url}/api/settings", token=auditor_token, method="GET")
        auditor_policies_status, _ = _request_json(f"{base_url}/api/policies", token=auditor_token, method="GET")
        auditor_write_status, auditor_write = _request_json(
            f"{base_url}/api/settings/preview",
            token=auditor_token,
            body={"settings": {"clusters": [{"cluster": "dev-a", "environment": "dev"}]}},
        )
        secret_status, secret_payload = _request_json(
            f"{base_url}/api/settings/preview",
            token=admin_token,
            body={"settings": {"ldap_bind_password": "hidden"}},
        )
        next_settings = {
            "clusters": [
                {"cluster": "dev-a", "environment": "dev"},
                {"cluster": "stage-a", "environment": "staging"},
            ],
            "approval_policy": {
                "rules": [
                    {
                        "environment": "prod",
                        "cluster": None,
                        "namespace": None,
                        "action_type": "*",
                        "risk_level": "low",
                        "approval_required": True,
                        "auto_execution": True,
                        "self_approval": False,
                        "eligible_approver_roles": ["approver", "admin"],
                    },
                    {
                        "environment": "dev",
                        "cluster": "dev-a",
                        "namespace": "default",
                        "action_type": "restart_deployment",
                        "risk_level": "low",
                        "approval_required": False,
                        "auto_execution": True,
                        "self_approval": True,
                        "eligible_approver_roles": ["approver", "admin"],
                    },
                ],
                "allow_self_approval_low_risk": False,
                "dev_low_risk_auto_execute": True,
                "test_low_risk_auto_execute": False,
            },
            "action_allowlist": [
                {
                    "action_type": "restart_deployment",
                    "backend": "k8s",
                    "template": "kubectl rollout restart deployment/{service}",
                    "allowed_scopes": ["prod", "dev"],
                    "default_risk": "low",
                    "preflight": True,
                    "post_check": True,
                    "rollback_required": True,
                    "enabled": True,
                },
                {
                    "action_type": "scale_deployment",
                    "backend": "k8s",
                    "template": "kubectl scale deployment/{service} --replicas={replicas}",
                    "allowed_scopes": ["dev"],
                    "default_risk": "medium",
                    "preflight": True,
                    "post_check": True,
                    "rollback_required": True,
                    "enabled": False,
                },
            ],
        }
        preview_status, preview = _request_json(
            f"{base_url}/api/settings/preview",
            token=admin_token,
            body={"settings": next_settings},
        )
        denied_save_status, denied_save = _request_json(
            f"{base_url}/api/settings",
            token=admin_token,
            body={"settings": next_settings},
        )
        save_status, save_payload = _request_json(
            f"{base_url}/api/settings",
            token=admin_token,
            body={
                "settings": next_settings,
                "confirmation": settings_service.CONFIRM_TEXT,
                "change_summary": "enable dev grants",
            },
        )
        rollback_status, rollback_payload = _request_json(
            f"{base_url}/api/settings/rollback",
            token=admin_token,
            body={},
        )
        noop_save_status, noop_save = _request_json(
            f"{base_url}/api/settings",
            token=admin_token,
            body={
                "settings": rollback_payload.get("settings_version", {}).get("settings", {}),
                "change_summary": "no-op audit save",
            },
        )
        policy_test_status, policy_test = _request_json(
            f"{base_url}/api/policies/test",
            token=auditor_token,
            body={"action_type": "restart_deployment", "cluster": "unknown-a", "namespace": "default", "risk_level": "low"},
        )
        prod_risk_decisions = [
            _request_json(
                f"{base_url}/api/policies/test",
                token=auditor_token,
                body={"action_type": "restart_deployment", "cluster": "unknown-a", "namespace": "default", "risk_level": risk},
            )[1]["result"]["decision"]
            for risk in ("low", "medium", "high")
        ]
        policies_status, policies = _request_json(f"{base_url}/api/policies", token=auditor_token, method="GET")
        audit_rows = asyncio.run(gateway_main.audit_log.query_audit(cluster="settings", limit=20))

        assert admin_get_status == 200
        assert admin_get["settings_version"]["version_number"] == 1
        assert auditor_get_status == 200
        assert auditor_get["settings_version"]["settings"] == admin_get["settings_version"]["settings"]
        assert auditor_policies_status == 200
        assert auditor_write_status == 403
        assert auditor_write["error"]["code"] == "forbidden"
        assert secret_status == 400
        assert secret_payload["error"]["code"] == "secret_field_forbidden"
        serialized_settings = json.dumps(admin_get, sort_keys=True).lower()
        for forbidden in ("secret", "token", "password", "bind_password", "internal_url", "database_path"):
            assert forbidden not in serialized_settings
        assert preview_status == 200
        assert preview["preview"]["critical"] is True
        assert preview["preview"]["confirmation_text"] == settings_service.CONFIRM_TEXT
        assert {item["path"] for item in preview["preview"]["diff"]} >= {"clusters", "approval_policy", "action_allowlist"}
        assert denied_save_status == 409
        assert denied_save["error"]["code"] == "confirmation_required"
        assert save_status == 200
        assert save_payload["settings_version"]["version_number"] == 2
        assert save_payload["settings_version"]["critical_confirmed"] is True
        assert rollback_status == 200
        assert rollback_payload["settings_version"]["version_number"] == 3
        assert rollback_payload["settings_version"]["settings"] == admin_get["settings_version"]["settings"]
        assert noop_save_status == 200
        assert noop_save["settings_version"]["version_number"] == 4
        assert noop_save["settings_version"]["diff"] == []
        assert policy_test_status == 200
        assert policy_test["result"]["environment"] == "prod"
        assert policy_test["result"]["decision"] == "approval_required"
        assert policy_test["policy_hit"]["settings_version"] == 4
        assert prod_risk_decisions == ["approval_required", "approval_required", "approval_required"]
        assert policies_status == 200
        assert policies["policy_state"]["default_environment"] == "prod"
        rules = policies["policy_state"]["policy"]["rules"]
        assert {
            "environment",
            "cluster",
            "namespace",
            "action_type",
            "risk_level",
            "approval_required",
            "auto_execution",
            "self_approval",
            "eligible_approver_roles",
        } <= set(rules[0])
        allowlist = policies["policy_state"]["action_allowlist"]
        assert {
            "action_type",
            "backend",
            "template",
            "allowed_scopes",
            "default_risk",
            "preflight",
            "post_check",
            "rollback_required",
            "enabled",
        } <= set(allowlist[0])
        assert any(item["action_type"] == "restart_deployment" for item in allowlist)
        assert policies["policy_state"]["recent_policy_hits"][0]["cluster"] == "unknown-a"
        assert {row["what"] for row in audit_rows} >= {"settings_save", "settings_rollback"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_audit_db
        settings_service._DB = old_settings_db
        audit_db.close()
        settings_db.close()


def test_settings_service_redacts_legacy_secret_keys_and_matches_scoped_allowlist(tmp_path: Path) -> None:
    settings_db = settings_service.SettingsDB(tmp_path / "settings.db")
    try:
        settings_db._insert_version(
            {
                "database_path": "/tmp/secret.db",
                "internal_service_url": "http://internal",
                "action_allowlist": [
                    {
                        "action_type": "restart_deployment",
                        "backend": "k8s",
                        "template": "kubectl rollout restart deployment/{service}",
                        "allowed_scopes": ["cluster:dev-a"],
                        "default_risk": "low",
                        "preflight": True,
                        "post_check": True,
                        "rollback_required": True,
                        "enabled": True,
                    }
                ],
                "approval_policy": {
                    "rules": [
                        {
                            "environment": "prod",
                            "action_type": "*",
                            "risk_level": "low",
                            "approval_required": True,
                            "auto_execution": True,
                            "self_approval": False,
                            "eligible_approver_roles": ["approver", "admin"],
                        }
                    ]
                },
            },
            [],
            actor_id="test",
            summary="legacy secret fixture",
            critical_confirmed=True,
            reload_required=False,
        )
        current = settings_db.current()
        serialized = json.dumps(current["settings"], sort_keys=True).lower()
        assert "database_path" not in serialized
        assert "internal_service_url" not in serialized
        assert settings_service.scope_allowed(["cluster:dev-a"], environment="prod", payload={"cluster": "dev-a"})
        assert not settings_service.scope_allowed(["cluster:dev-a"], environment="prod", payload={"cluster": "prod-a"})
    finally:
        settings_db.close()
