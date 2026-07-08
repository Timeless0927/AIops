"""Console Next runbook management route tests."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import agent_run_service
from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import runbook_service


def _write_identity_config(path: Path) -> None:
    users = [
        ("admin", "admin-pass", "Admin", "admin", "payments"),
        ("operator", "operator-pass", "Operator", "operator", "payments"),
        ("viewer", "viewer-pass", "Viewer", "viewer", "payments"),
        ("auditor", "auditor-pass", "Auditor", "auditor", "payments"),
        ("outsider", "outsider-pass", "Outsider", "viewer", "other"),
    ]
    path.write_text(
        "\n".join(
            [
                "identity:",
                f'  store_path: "{path.with_name("identity.db")}"',
                "  ldap:",
                "    enabled: false",
                "  users:",
                *[
                    f"""    - username: {username}
      password: {password}
      display_name: {display}
      roles: [{role}]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["{team}"]
        namespaces: ["default"]"""
                    for username, password, display, role, team in users
                ],
            ]
        ),
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


def test_runbooks_list_toggle_scope_readonly_and_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "identity.yaml"
    data_dir = tmp_path / "data"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(data_dir))
    gateway_main._SESSIONS.clear()

    old_audit_db = gateway_main.audit_log._DB
    old_agent_db = agent_run_service._DB
    old_runbook_db = runbook_service._DB
    audit_db = gateway_main.audit_log.AuditLogDB(data_dir / "audit_log.db")
    runs_db = agent_run_service.AgentRunDB(data_dir / "agent_runs.db")
    runbooks_db = runbook_service.RunbookDB(data_dir / "runbooks.db")
    gateway_main.audit_log._DB = audit_db
    agent_run_service._DB = runs_db
    runbook_service._DB = runbooks_db

    asyncio.run(
        agent_run_service.create_run(
            {
                "title": "Checkout health",
                "message": "check checkout",
                "runbook_skeleton": "service_health",
                "cluster": "prod-a",
                "namespace": "default",
                "service": "checkout",
                "team": "payments",
            },
            actor_id="operator",
        )
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        admin = _login(base_url, "admin", "admin-pass")
        operator = _login(base_url, "operator", "operator-pass")
        viewer = _login(base_url, "viewer", "viewer-pass")
        auditor = _login(base_url, "auditor", "auditor-pass")
        outsider = _login(base_url, "outsider", "outsider-pass")

        list_status, listed = _request_json(f"{base_url}/api/runbooks", token=operator, method="GET")
        outsider_status, outsider_list = _request_json(f"{base_url}/api/runbooks", token=outsider, method="GET")
        auditor_status, auditor_list = _request_json(f"{base_url}/api/runbooks", token=auditor, method="GET")
        viewer_toggle_status, viewer_toggle = _request_json(
            f"{base_url}/api/runbooks/service_health/toggle",
            token=viewer,
            body={"enabled": False},
        )
        invalid_toggle_status, invalid_toggle = _request_json(
            f"{base_url}/api/runbooks/service_health/toggle",
            token=admin,
            body={"enabled": "false"},
        )
        toggle_status, toggled = _request_json(
            f"{base_url}/api/runbooks/service_health/toggle",
            token=admin,
            body={"enabled": False},
        )
        refreshed_status, refreshed = _request_json(f"{base_url}/api/runbooks", token=viewer, method="GET")
        audit_rows = asyncio.run(gateway_main.audit_log.query_audit(cluster="runbooks", limit=20))

        assert list_status == 200
        assert [item["id"] for item in listed["runbooks"]] == ["service_health", "k8s_workload", "dependency"]
        service_health = listed["runbooks"][0]
        assert service_health["enabled"] is True
        assert service_health["scope"] == {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"}
        assert service_health["owner"] == "sre-platform"
        assert service_health["updated_at"]
        assert service_health["last_run_summary"]["run_id"].startswith("run-")
        assert service_health["last_run_summary"]["status"] in {"finished", "stuck"}
        assert outsider_status == 200
        assert outsider_list["runbooks"] == []
        assert auditor_status == 200
        assert len(auditor_list["runbooks"]) == 3
        assert viewer_toggle_status == 403
        assert viewer_toggle["error"]["code"] == "forbidden"
        assert invalid_toggle_status == 400
        assert invalid_toggle["error"]["code"] == "invalid_request"
        assert toggle_status == 200
        assert toggled["runbook"]["enabled"] is False
        assert refreshed_status == 200
        assert refreshed["runbooks"][0]["enabled"] is False
        assert any(row["what"] == "runbook_toggle" and row["result"] == "success" for row in audit_rows)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_audit_db
        agent_run_service._DB = old_agent_db
        runbook_service._DB = old_runbook_db
        audit_db.close()
        runs_db.close()
        runbooks_db.close()
