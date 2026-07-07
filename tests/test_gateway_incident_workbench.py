"""Console Next incident workbench route tests."""

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


def _request_json(url: str, *, body: dict | None = None, token: str | None = None, method: str = "POST") -> tuple[int, dict]:
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


def _request_text(url: str, *, token: str, headers: dict[str, str] | None = None) -> tuple[int, str, str]:
    headers = {"Accept": "text/event-stream", **(headers or {}), "Authorization": f"Bearer {token}"}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    old_store = gateway_main.incident_store._STORE
    old_run_db = agent_run_service._DB
    old_audit_db = gateway_main.audit_log._DB
    gateway_main.incident_store._STORE = IncidentStore(tmp_path / "incidents.db")
    agent_run_service._DB = agent_run_service.AgentRunDB(tmp_path / "agent_runs.db")
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(tmp_path / "audit_log.db")
    gateway_main._SESSIONS.clear()
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
        gateway_main.incident_store._STORE.close()
        agent_run_service._DB.close()
        gateway_main.audit_log._DB.close()
        gateway_main.incident_store._STORE = old_store
        agent_run_service._DB = old_run_db
        gateway_main.audit_log._DB = old_audit_db


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password})
    assert status == 200
    return str(payload["token"])


def _create_incident() -> str:
    return asyncio.run(
        gateway_main.incident_store.create_incident(
            "CheckoutLatencyHigh",
            "default",
            "prod-a",
            "checkout latency rose",
            service="checkout",
            team="payments",
        )
    )


def test_incident_workbench_controls_runs_and_audit(gateway: str) -> None:
    token = _login(gateway, "operator", "operator-pass")
    incident_id = _create_incident()
    asyncio.run(gateway_main.incident_store.add_evidence(incident_id, "metrics", "m1", "latency high", payload={"p95": 3.2}))

    get_status, workbench = _request_json(f"{gateway}/api/incidents/{incident_id}/workbench", token=token, method="GET")
    takeover_status, takeover = _request_json(
        f"{gateway}/api/incidents/{incident_id}/controls",
        token=token,
        body={"action": "manual_takeover", "note": "taking over"},
    )
    note_status, _ = _request_json(
        f"{gateway}/api/incidents/{incident_id}/controls",
        token=token,
        body={"action": "human_note", "note": "checked checkout pods"},
    )
    run_status, run = _request_json(
        f"{gateway}/api/incidents/{incident_id}/controls",
        token=token,
        body={"action": "restart_run", "mode": "start_new"},
    )
    current_status, current = _request_json(
        f"{gateway}/api/incidents/{incident_id}/controls",
        token=token,
        body={"action": "restart_run"},
    )
    resolve_status, resolved = _request_json(
        f"{gateway}/api/incidents/{incident_id}/controls",
        token=token,
        body={"action": "resolve", "note": "healthy"},
    )
    reopen_status, reopened = _request_json(
        f"{gateway}/api/incidents/{incident_id}/controls",
        token=token,
        body={"action": "reopen", "note": "alert fired again"},
    )
    timeline = asyncio.run(gateway_main.incident_store.get_timeline(incident_id))
    audit_rows = asyncio.run(gateway_main.audit_log.query_audit(limit=50))

    assert get_status == 200
    assert workbench["workbench"]["panels"]["evidence"]["status"] == "ok"
    assert takeover_status == 200
    assert takeover["result"]["incident"]["status"] == "new"
    assert note_status == 200
    assert run_status == 200
    assert run["result"]["snapshot"]["run"]["incident_id"] == incident_id
    assert current_status == 200
    assert current["result"]["current_run"]["run_id"] == run["result"]["snapshot"]["run"]["run_id"]
    assert resolve_status == 200
    assert resolved["result"]["incident"]["status"] == "resolved"
    assert reopen_status == 200
    assert reopened["result"]["incident"]["status"] == "triaging"
    assert {"investigate_progress", "investigate_start", "resolved", "reopened"} <= {event["event_type"] for event in timeline}
    assert {"incident_workbench_get", "incident_control_manual_takeover", "incident_control_resolve"} <= {row["what"] for row in audit_rows}


def test_incident_workbench_scope_denies_outsider(gateway: str) -> None:
    operator = _login(gateway, "operator", "operator-pass")
    outsider = _login(gateway, "outsider", "outsider-pass")
    incident_id = _create_incident()

    allowed_status, _ = _request_json(f"{gateway}/api/incidents/{incident_id}/workbench", token=operator, method="GET")
    forbidden_status, forbidden = _request_json(f"{gateway}/api/incidents/{incident_id}/workbench", token=outsider, method="GET")
    forbidden_control_status, forbidden_control = _request_json(
        f"{gateway}/api/incidents/{incident_id}/controls",
        token=outsider,
        body={"action": "resolve", "note": "nope"},
    )

    assert allowed_status == 200
    assert forbidden_status == 403
    assert forbidden["error"]["code"] == "forbidden"
    assert forbidden_control_status == 403
    assert forbidden_control["error"]["code"] == "forbidden"


def test_incident_diagnosis_process_stream_replays_human_lines(gateway: str) -> None:
    token = _login(gateway, "operator", "operator-pass")
    incident_id = _create_incident()
    asyncio.run(
        gateway_main.incident_store.record_incident_diagnosis(
            incident_id,
            {
                "session_id": "diagnosis-stream-session",
                "summary": "Checkout latency is tied to upstream payment timeouts.",
                "root_cause_candidates": [
                    {"cause": "payment-api upstream timeout", "category": "upstream_dependency_down", "confidence": 0.91}
                ],
            },
        )
    )
    asyncio.run(
        gateway_main.incident_store.add_diagnosis_trace(
            session_id="diagnosis-stream-session",
            step_index=1,
            tool_name="query_metrics",
            tool_args={"query": "up"},
            observation_ref="ev-metrics-1",
        )
    )
    asyncio.run(
        gateway_main.incident_store.add_event(
            incident_id,
            "investigate_end",
            "aiops_gateway",
            "Diagnosis service writeback",
            "Diagnosis session completed with status partial",
            {"session_id": "diagnosis-stream-session", "status": "partial"},
        )
    )

    status, content_type, body = _request_text(f"{gateway}/api/incidents/{incident_id}/diagnosis-process/stream", token=token)

    assert status == 200
    assert content_type.startswith("text/event-stream")
    assert "Investigate End" in body
    assert "Diagnosis session completed with status partial" in body
    assert "Tool call: query_metrics" in body


def test_incident_diagnosis_process_stream_honors_last_event_id(gateway: str) -> None:
    token = _login(gateway, "operator", "operator-pass")
    incident_id = _create_incident()
    for index in range(3):
        asyncio.run(
            gateway_main.incident_store.add_event(
                incident_id,
                "investigate_progress",
                "aiops_gateway",
                f"step {index}",
                f"diagnosis step {index}",
                {"session_id": "diagnosis-stream-session"},
            )
        )
    asyncio.run(
        gateway_main.incident_store.add_event(
            incident_id,
            "investigate_end",
            "aiops_gateway",
            "done",
            "diagnosis completed",
            {"session_id": "diagnosis-stream-session", "status": "partial"},
        )
    )

    status, _, body = _request_text(
        f"{gateway}/api/incidents/{incident_id}/diagnosis-process/stream",
        token=token,
        headers={"Last-Event-ID": "2"},
    )

    assert status == 200
    assert "id: 1\n" not in body
    assert "id: 2\n" not in body
    assert "id: 3\n" in body
