"""Console Next incident report and feedback route tests."""

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
from apps.aiops_k8s_gateway import report_service
from toolsets.incident_store import IncidentStore


def _write_identity_config(path: Path) -> None:
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
  ldap:
    enabled: false
  users:
    - username: operator
      password: operator-pass
      display_name: Operator
      roles: [operator]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: outsider
      password: outsider-pass
      display_name: Outsider
      roles: [operator]
      scope:
        clusters: ["prod-b"]
        services: ["billing"]
        teams: ["finance"]
        namespaces: ["default"]
""",
        encoding="utf-8",
    )


def _request_json(url: str, *, body: dict | None = None, token: str | None = None, method: str = "GET") -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _request_text(url: str, *, token: str | None = None) -> tuple[int, str, str]:
    headers = {"Accept": "text/html"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password}, method="POST")
    assert status == 200
    return str(payload["token"])


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "identity.yaml"
    data_dir = tmp_path / "data"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(data_dir))

    old_report_db = report_service._DB
    old_run_db = agent_run_service._DB
    old_audit_db = gateway_main.audit_log._DB
    old_incident_store = gateway_main.incident_store._STORE
    report_service._DB = report_service.ReportDB(data_dir / "reports_feedback.db")
    agent_run_service._DB = agent_run_service.AgentRunDB(data_dir / "agent_runs.db")
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(data_dir / "audit_log.db")
    gateway_main.incident_store._STORE = IncidentStore(data_dir / "incidents.db")
    gateway_main._SESSIONS.clear()
    gateway_main._ROUTES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main._ROUTES.clear()
        report_service._DB.close()
        agent_run_service._DB.close()
        gateway_main.audit_log._DB.close()
        gateway_main.incident_store._STORE.close()
        report_service._DB = old_report_db
        agent_run_service._DB = old_run_db
        gateway_main.audit_log._DB = old_audit_db
        gateway_main.incident_store._STORE = old_incident_store


def test_report_draft_publish_version_export_feedback_and_auth(gateway: str) -> None:
    incident_id = asyncio.run(
        gateway_main.incident_store.create_incident(
            "CheckoutErrors",
            "default",
            "prod-a",
            "checkout error rate high",
            service="checkout",
            team="payments",
        )
    )
    run = asyncio.run(
        agent_run_service.create_run(
            {
                "title": "checkout run",
                "message": "investigate checkout",
                "incident_id": incident_id,
                "scope": {
                    "cluster": "prod-a",
                    "namespace": "default",
                    "service": "checkout",
                    "team": "payments",
                },
            },
            actor_id="operator",
        )
    )
    run_id = str(run["run"]["run_id"])
    operator = _login(gateway, "operator", "operator-pass")
    outsider = _login(gateway, "outsider", "outsider-pass")

    public_status, public_payload = _request_json(f"{gateway}/api/incidents/{incident_id}/report")
    hidden_status, hidden_payload = _request_json(f"{gateway}/api/incidents/{incident_id}/report", token=outsider)
    draft_status, draft = _request_json(f"{gateway}/api/incidents/{incident_id}/report/draft", body={}, token=operator, method="POST")
    version_id = draft["report_version"]["version_id"]
    get_status, report = _request_json(f"{gateway}/api/incidents/{incident_id}/report", token=operator)
    html_status, content_type, html = _request_text(f"{gateway}/api/incidents/{incident_id}/report?format=html", token=operator)
    publish_status, published = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/publish",
        body={"version_id": version_id},
        token=operator,
        method="POST",
    )
    republish_status, republish = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/publish",
        body={"version_id": version_id},
        token=operator,
        method="POST",
    )
    edit_status, edited = _request_json(
        f"{gateway}/api/incidents/{incident_id}/report/draft",
        body={"html": "<article><h1>edited</h1><script>alert(1)</script></article>"},
        token=operator,
        method="POST",
    )
    feedback_payloads = [
        {"target_type": "diagnosis", "target_id": incident_id, "incident_id": incident_id, "rating": "neutral", "comment": "root cause unknown"},
        {"target_type": "evidence", "target_id": "ev-1", "incident_id": incident_id, "rating": "negative", "comment": "missing logs"},
        {"target_type": "action_proposal", "target_id": "proposal-1", "incident_id": incident_id, "rating": "positive", "comment": "safe action"},
        {"target_type": "report", "target_id": version_id, "incident_id": incident_id, "rating": "positive", "comment": "ready"},
        {"target_type": "report", "target_id": version_id, "run_id": run_id, "rating": "positive", "comment": "visible on run"},
    ]
    feedback_results = [
        _request_json(f"{gateway}/api/feedback", body=payload, token=operator, method="POST")
        for payload in feedback_payloads
    ]
    final_status, final_report = _request_json(f"{gateway}/api/incidents/{incident_id}/report", token=operator)
    run_feedback_status, run_feedback = _request_json(f"{gateway}/api/agent-runs/{run_id}/feedback", token=operator)

    assert public_status == 401
    assert public_payload["error"]["code"] == "unauthorized"
    assert hidden_status == 403
    assert hidden_payload["error"]["code"] == "forbidden"
    assert draft_status == 201
    assert draft["report_version"]["status"] == "draft"
    assert "root_cause" in draft["report_version"]["unknowns"]
    assert "unknown" in draft["report_version"]["html"]
    assert get_status == 200
    assert report["report"]["latest_report"]["version_id"] == version_id
    assert html_status == 200
    assert "text/html" in content_type
    assert "CheckoutErrors" in html
    assert publish_status == 200
    assert published["report_version"]["status"] == "published"
    assert republish_status == 409
    assert republish["error"]["code"] == "immutable_report"
    assert edit_status == 201
    assert edited["report_version"]["version_number"] == 2
    assert "<script>" not in edited["report_version"]["html"]
    assert all(status == 201 for status, _ in feedback_results)
    assert final_status == 200
    assert {item["target_type"] for item in final_report["report"]["feedback"]} >= {
        "diagnosis",
        "evidence",
        "action_proposal",
        "report",
    }
    assert run_feedback_status == 200
    assert run_feedback["feedback"][0]["run_id"] == run_id
