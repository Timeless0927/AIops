"""Gateway evidence query route tests."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

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
    - username: auditor
      password: auditor-pass
      display_name: Auditor
      roles: [auditor]
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


def _start_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
    gateway_main._SESSIONS.clear()
    gateway_main._ROUTES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


class _OpenObserveHandler(BaseHTTPRequestHandler):
    seen_paths: list[str] = []
    seen_auth: list[str] = []
    seen_bodies: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        self.__class__.seen_paths.append(self.path)
        self.__class__.seen_auth.append(self.headers.get("Authorization") or "")
        self.__class__.seen_bodies.append(body)
        payload = {
            "hits": [
                {
                    "k8s_cluster_name": "prod-a",
                    "k8s_namespace_name": "default",
                    "service_name": "checkout",
                    "team": "payments",
                    "message": "authorization: Bearer raw-token token=raw-secret",
                    "password": "plain-password",
                    "kubernetes": {"kind": "Secret", "data": {"token": "k8s-secret"}, "metadata": {"name": "app-secret"}},
                },
                {"message": "unscoped row must fail closed"},
            ]
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:
        return


def _start_openobserve():
    _OpenObserveHandler.seen_paths.clear()
    _OpenObserveHandler.seen_auth.clear()
    _OpenObserveHandler.seen_bodies.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenObserveHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def test_evidence_query_scope_limits_redaction_audit_and_openobserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    openobserve, openobserve_thread, openobserve_url = _start_openobserve()
    monkeypatch.setenv("AIOPS_OPENOBSERVE_URL", openobserve_url)
    monkeypatch.setenv("AIOPS_OPENOBSERVE_TOKEN", "backend-token")
    monkeypatch.setenv("AIOPS_OPENOBSERVE_ORG", "default")
    db = gateway_main.audit_log.AuditLogDB(tmp_path / "data" / "audit_log.db")
    old_db = gateway_main.audit_log._DB
    gateway_main.audit_log._DB = db
    server, thread, base_url = _start_gateway(monkeypatch, tmp_path)

    try:
        operator_token = _login(base_url, "operator", "operator-pass")
        status, payload = _request_json(
            f"{base_url}/api/evidence/query",
            token=operator_token,
            body={
                "template": "service_overview",
                "query_type": "overview",
                "scope": {
                    "cluster": "prod-a",
                    "namespace": "default",
                    "service": "checkout",
                    "team": "payments",
                    "environment": "prod",
                },
                "limit": 999,
                "timeout_seconds": 99,
                "time_range": {"start_ts": 0, "end_ts": 9_999_999_999},
            },
        )
        audit_rows = asyncio.run(gateway_main.audit_log.query_audit(cluster="prod-a", namespace="default", limit=20))

        assert status == 200
        evidence = payload["evidence"]
        assert evidence["limits"]["limit"] == 100
        assert evidence["limits"]["timeout_seconds"] == 5.0
        assert evidence["limits"]["time_range_seconds"] <= 21_600
        assert {source["kind"] for source in evidence["sources"]} >= {"metrics", "logs", "traces", "kubernetes", "topology"}
        assert {source["status"] for source in evidence["sources"] if source["kind"] in {"metrics", "logs", "traces"}} == {"ok"}
        serialized = json.dumps(evidence, sort_keys=True)
        assert "raw-token" not in serialized
        assert "raw-secret" not in serialized
        assert "plain-password" not in serialized
        assert "k8s-secret" not in serialized
        assert "unscoped row" not in serialized
        assert '"data": "[redacted]"' in serialized
        assert evidence["nodes"]
        assert evidence["nodes"][0]["presentation"] == "process_node"
        assert evidence["nodes"][0]["scope"]["service"] == "checkout"
        assert evidence["nodes"][0]["snippet"]
        assert "why_it_mattered" in evidence["nodes"][0]
        assert _OpenObserveHandler.seen_paths == ["/api/default/_search"]
        assert _OpenObserveHandler.seen_auth == ["Bearer backend-token"]
        assert _OpenObserveHandler.seen_bodies[0]["query"]["size"] == 100
        assert _OpenObserveHandler.seen_bodies[0]["query"]["from"] > 0
        assert _OpenObserveHandler.seen_bodies[0]["query"]["to"] > 0
        assert any(
            row["what"] == "evidence_query"
            and row["permission"] == "view_evidence"
            and row["decision"] == "allow"
            and row["request_id"]
            for row in audit_rows
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        openobserve.shutdown()
        openobserve.server_close()
        openobserve_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_db
        db.close()


def test_evidence_missing_scope_advanced_query_and_agent_template_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.delenv("AIOPS_OPENOBSERVE_URL", raising=False)
    monkeypatch.delenv("AIOPS_OPENOBSERVE_TOKEN", raising=False)
    db = gateway_main.audit_log.AuditLogDB(tmp_path / "data" / "audit_log.db")
    old_db = gateway_main.audit_log._DB
    gateway_main.audit_log._DB = db
    server, thread, base_url = _start_gateway(monkeypatch, tmp_path)

    try:
        operator_token = _login(base_url, "operator", "operator-pass")
        auditor_token = _login(base_url, "auditor", "auditor-pass")
        missing_scope_status, missing_scope_payload = _request_json(
            f"{base_url}/api/evidence/query",
            token=operator_token,
            body={
                "template": "service_overview",
                "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout"},
            },
        )
        advanced_status, advanced_payload = _request_json(
            f"{base_url}/api/evidence/query",
            token=operator_token,
            body={
                "advanced_query": "select * from logs",
                "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
            },
        )
        auditor_status, auditor_payload = _request_json(
            f"{base_url}/api/evidence/query",
            token=auditor_token,
            body={
                "advanced_query": "select * from logs",
                "query_type": "logs",
                "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
            },
        )
        agent_status, agent_payload = _request_json(
            f"{base_url}/api/evidence/agent-query",
            token=operator_token,
            body={
                "template": "changes_recent",
                "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
            },
        )

        assert missing_scope_status == 403
        assert missing_scope_payload["error"]["code"] == "forbidden"
        assert advanced_status == 403
        assert advanced_payload["error"]["code"] == "advanced_query_forbidden"
        assert auditor_status == 200
        assert auditor_payload["evidence"]["query"]["advanced"] is True
        assert agent_status == 403
        assert agent_payload["error"]["code"] == "template_forbidden"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_db
        db.close()


def test_evidence_openobserve_unconfigured_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "identity.yaml"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.delenv("AIOPS_OPENOBSERVE_URL", raising=False)
    monkeypatch.delenv("AIOPS_OPENOBSERVE_TOKEN", raising=False)
    db = gateway_main.audit_log.AuditLogDB(tmp_path / "data" / "audit_log.db")
    old_db = gateway_main.audit_log._DB
    gateway_main.audit_log._DB = db
    server, thread, base_url = _start_gateway(monkeypatch, tmp_path)

    try:
        admin_token = _login(base_url, "admin", "admin-pass")
        status, payload = _request_json(
            f"{base_url}/api/evidence/query",
            token=admin_token,
            body={
                "template": "service_overview",
                "query_type": "overview",
                "scope": {"cluster": "prod-a", "namespace": "default", "service": "checkout", "team": "payments"},
            },
        )

        assert status == 200
        evidence = payload["evidence"]
        assert evidence["status"] == "partial"
        assert {source["status"] for source in evidence["sources"] if source["kind"] in {"metrics", "logs", "traces"}} == {"degraded"}
        assert {source["kind"] for source in evidence["sources"]} >= {"kubernetes", "topology", "changes", "tool_output"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main.audit_log._DB = old_db
        db.close()
