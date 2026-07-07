"""Gateway Console Next agent-run and SSE route tests."""

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
      roles: [viewer]
      scope:
        clusters: ["prod-b"]
        services: ["billing"]
        teams: ["finance"]
        namespaces: ["default"]
""",
        encoding="utf-8",
    )


def _request_json(
    url: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    method: str = "POST",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _request_text(
    url: str,
    *,
    token: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    request_headers = {"Accept": "text/event-stream", **(headers or {}), "Authorization": f"Bearer {token}"}
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password})
    assert status == 200
    return str(payload["token"])


def _start_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    gateway_main._SESSIONS.clear()
    gateway_main._ROUTES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _parse_sse(body: str) -> list[dict]:
    events: list[dict] = []
    for block in body.strip().split("\n\n"):
        data = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), "")
        if data:
            events.append(json.loads(data))
    return events


def test_agent_run_lifecycle_sse_replay_redaction_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    old_audit_db = gateway_main.audit_log._DB
    old_run_db = agent_run_service._DB
    audit_db = gateway_main.audit_log.AuditLogDB(tmp_path / "data" / "audit_log.db")
    run_db = agent_run_service.AgentRunDB(tmp_path / "data" / "agent_runs.db")
    gateway_main.audit_log._DB = audit_db
    agent_run_service._DB = run_db
    server, thread, base_url = _start_gateway(monkeypatch, tmp_path)

    try:
        operator_token = _login(base_url, "operator", "operator-pass")
        create_status, create_payload = _request_json(
            f"{base_url}/api/agent-runs",
            token=operator_token,
            body={
                "title": "Checkout run",
                "message": "Investigate checkout token=secret",
                "tags": ["checkout"],
                "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
            },
        )
        run_id = create_payload["snapshot"]["run"]["run_id"]
        side_status, side_payload = _request_json(
            f"{base_url}/api/agent-runs/{run_id}/messages",
            token=operator_token,
            body={"message": "/btw compare authorization: Bearer raw-token"},
        )
        promote_status, promote_payload = _request_json(
            f"{base_url}/api/agent-runs/{run_id}/promote",
            token=operator_token,
            body={"event_id": side_payload["result"]["id"]},
        )
        update_status, update_payload = _request_json(
            f"{base_url}/api/agent-runs/{run_id}/conversation",
            token=operator_token,
            method="POST",
            body={"title": "Checkout updated", "tags": ["checkout", "side"]},
        )
        snapshot_status, snapshot_payload = _request_json(f"{base_url}/api/agent-runs/{run_id}", token=operator_token, method="GET")
        list_status, list_payload = _request_json(f"{base_url}/api/agent-runs", token=operator_token, method="GET")
        sse_status, content_type, sse_body = _request_text(f"{base_url}/api/agent-runs/{run_id}/stream", token=operator_token, headers={"Last-Event-ID": "1"})
        archive_status, archive_payload = _request_json(f"{base_url}/api/agent-runs/{run_id}/archive", token=operator_token, body={})
        delete_status, delete_payload = _request_json(f"{base_url}/api/agent-runs/{run_id}/delete", token=operator_token, body={})
        audit_rows = asyncio.run(gateway_main.audit_log.query_audit(cluster="prod-a", namespace="default", limit=50))

        assert create_status == 201
        assert create_payload["snapshot"]["timeline"][0]["event_type"] == "run_created"
        assert side_status == 200
        assert side_payload["result"]["thread_type"] == "side"
        assert promote_status == 200
        assert promote_payload["result"]["event_type"] == "btw_promoted"
        assert update_status == 200
        assert update_payload["result"]["conversation"]["title"] == "Checkout updated"
        assert snapshot_status == 200
        assert len(snapshot_payload["snapshot"]["timeline"]) >= 4
        serialized = json.dumps(snapshot_payload, sort_keys=True)
        assert "raw-token" not in serialized
        assert "token=secret" not in serialized
        assert list_status == 200
        assert list_payload["agent_runs"][0]["run_id"] == run_id
        assert sse_status == 200
        assert content_type.startswith("text/event-stream")
        replayed = _parse_sse(sse_body)
        assert replayed
        assert all(event["id"] > 1 for event in replayed)
        assert archive_status == 200
        assert archive_payload["snapshot"]["conversation"]["status"] == "archived"
        assert delete_status == 200
        assert delete_payload["snapshot"]["conversation"]["status"] == "deleted"
        assert {row["what"] for row in audit_rows} >= {"agent_run_create", "agent_run_message", "agent_run_promote", "agent_run_stream"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_audit_db
        agent_run_service._DB = old_run_db
        audit_db.close()
        run_db.close()


def test_agent_run_scope_fail_closed_and_hidden_events_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    old_run_db = agent_run_service._DB
    run_db = agent_run_service.AgentRunDB(tmp_path / "data" / "agent_runs.db")
    agent_run_service._DB = run_db
    server, thread, base_url = _start_gateway(monkeypatch, tmp_path)

    try:
        operator_token = _login(base_url, "operator", "operator-pass")
        outsider_token = _login(base_url, "outsider", "outsider-pass")
        missing_scope_status, missing_scope_payload = _request_json(
            f"{base_url}/api/agent-runs",
            token=operator_token,
            body={
                "title": "Missing scope",
                "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout"},
            },
        )
        create_status, create_payload = _request_json(
            f"{base_url}/api/agent-runs",
            token=operator_token,
            body={
                "title": "Checkout run",
                "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
            },
        )
        run_id = create_payload["snapshot"]["run"]["run_id"]
        outsider_snapshot_status, outsider_snapshot = _request_json(f"{base_url}/api/agent-runs/{run_id}", token=outsider_token, method="GET")
        outsider_stream_status, _, outsider_stream = _request_text(f"{base_url}/api/agent-runs/{run_id}/stream", token=outsider_token)

        assert missing_scope_status == 403
        assert missing_scope_payload["error"]["code"] == "forbidden"
        assert create_status == 201
        assert outsider_snapshot_status == 403
        assert outsider_snapshot["error"]["code"] == "forbidden"
        assert outsider_stream_status == 403
        assert "run_created" not in outsider_stream
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        agent_run_service._DB = old_run_db
        run_db.close()
