from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import mcp_registry_http


class FakeMCP(BaseHTTPRequestHandler):
    authorization: str | None = None
    arguments: dict | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        type(self).authorization = self.headers.get("Authorization")
        payload = json.dumps({
            "status": "ok",
            "capabilities": [{
                "name": "query_metrics", "version": "prometheus-query-v1",
                "read_only": True, "mutation": False, "path": "/query_metrics",
            }],
        }).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        type(self).authorization = self.headers.get("Authorization")
        type(self).arguments = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        payload = json.dumps({
            "request_id": self.arguments["request_id"], "tool_name": "query_metrics",
            "status": "succeeded", "summary": "up=1", "data": {"value": 1},
            "evidence_refs": [], "audit": {"status": "succeeded"},
        }).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        pass


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    request_id: str | None = None,
) -> tuple[int, dict, str | None]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if request_id:
        headers["X-Request-ID"] = request_id
    outbound = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(outbound, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _login(base_url: str) -> tuple[str, str]:
    status, _, set_cookie = _request(
        f"{base_url}/auth/login", method="POST",
        body={"username": "admin", "password": "admin-pass", "session_mode": "cookie"},
    )
    assert status == 200 and set_cookie
    cookie = set_cookie.split(";", 1)[0]
    status, payload, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    assert status == 200
    return cookie, payload["csrf_token"]


def test_mcp_registry_admin_contract_is_fresh_authenticated_masked_and_audited(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    key = tmp_path / "mcp-key"
    key.write_bytes(b"k" * 32)
    monkeypatch.setenv("AIOPS_MCP_ENCRYPTION_KEY_PATH", str(key))
    FakeMCP.authorization = None
    FakeMCP.arguments = None
    monkeypatch.setattr(
        mcp_registry_http, "enforce_internal_auth",
        lambda *_args, **_kwargs: "system:serviceaccount:aiops:aiops-diagnosis",
    )
    mcp_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMCP)
    mcp_thread = threading.Thread(target=mcp_server.serve_forever, daemon=True)
    mcp_thread.start()
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()
    base_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"
    endpoint = f"http://127.0.0.1:{mcp_server.server_address[1]}"
    policy = [{"name": "query_metrics", "version": "prometheus-query-v1", "read_only": True}]
    allowed_scope = [{"cluster_id": "cluster-a", "namespace": "payments"}]
    create_body = {
        "name": "Prometheus production", "endpoint": endpoint,
        "credential": "mcp-contract-secret", "capabilities": policy,
        "allowed_scope": allowed_scope, "enabled": True,
        "reason": "register approved production metrics",
    }

    try:
        unauthorized_status, _, _ = _request(f"{base_url}/api/v1/admin/mcp-integrations")
        cookie, csrf = _login(base_url)
        with gateway_main._GATEWAY.database.connect() as conn:
            conn.execute("UPDATE sessions SET fresh_at = 0")
        stale_status, stale, _ = _request(
            f"{base_url}/api/v1/admin/mcp-integrations", method="POST", body=create_body,
            cookie=cookie, csrf=csrf, request_id="mcp-stale-create",
        )
        reauth_status, _, _ = _request(
            f"{base_url}/auth/reauth", method="POST", body={"password": "admin-pass"},
            cookie=cookie, csrf=csrf,
        )
        create_status, created, _ = _request(
            f"{base_url}/api/v1/admin/mcp-integrations", method="POST", body=create_body,
            cookie=cookie, csrf=csrf, request_id="mcp-create-contract",
        )
        integration = created["mcp_integration"]
        integration_id = integration["id"]
        verify_status, verified, _ = _request(
            f"{base_url}/api/v1/admin/mcp-integrations/{integration_id}/verify",
            method="POST", body={"reason": "verify advertised tools"},
            cookie=cookie, csrf=csrf, request_id="mcp-verify-contract",
        )
        invoke_status, invoked, _ = _request(
            f"{base_url}/api/v1/internal/mcp-tools/query_metrics",
            method="POST",
            body={
                "integration_id": integration_id,
                "integration_revision": verified["mcp_integration"]["revision"],
                "arguments": {
                    "request_id": "mcp-tool-contract", "cluster_id": "cluster-a",
                    "namespace": "payments", "query": "up",
                },
            },
            request_id="mcp-tool-contract",
        )
        list_status, listed, _ = _request(
            f"{base_url}/api/v1/admin/mcp-integrations", cookie=cookie,
        )
        update_status, updated, _ = _request(
            f"{base_url}/api/v1/admin/mcp-integrations/{integration_id}", method="PATCH",
            body={
                "name": integration["name"], "endpoint": integration["endpoint"],
                "capabilities": policy, "allowed_scope": allowed_scope, "enabled": False,
                "expected_revision": verified["mcp_integration"]["revision"],
                "reason": "disable integration after test",
            },
            cookie=cookie, csrf=csrf, request_id="mcp-disable-contract",
        )
        _, audit, _ = _request(f"{base_url}/api/v1/admin/audit", cookie=cookie)

        assert unauthorized_status == 401
        assert stale_status == 403 and stale["error"]["code"] == "fresh_auth_required"
        assert reauth_status == 200
        assert create_status == 201
        assert integration["credential_configured"] is True
        assert verify_status == invoke_status == list_status == update_status == 200
        assert verified["mcp_integration"]["verification"]["state"] == "verified"
        assert listed["mcp_integrations"][0]["health"]["status"] == "ok"
        assert updated["mcp_integration"]["enabled"] is False
        assert FakeMCP.authorization == "Bearer mcp-contract-secret"
        assert FakeMCP.arguments == {
            "request_id": "mcp-tool-contract", "cluster_id": "cluster-a",
            "namespace": "payments", "query": "up",
        }
        assert invoked["status"] == "succeeded"
        serialized = json.dumps([created, verified, listed, updated, audit], ensure_ascii=False)
        assert "mcp-contract-secret" not in serialized
        assert {item["action"] for item in audit["audit"]} >= {
            "mcp_integration_create", "mcp_integration_verify", "mcp_integration_disable",
        }
        assert all("before_json" not in item and "after_json" not in item for item in audit["audit"])

        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        for schema_name, payload in (
            ("MCPIntegrationResponse", created),
            ("MCPIntegrationResponse", verified),
            ("MCPIntegrationListResponse", listed),
            ("MCPIntegrationResponse", updated),
            ("AdminAuditResponse", audit),
        ):
            jsonschema.Draft202012Validator(
                spec["components"]["schemas"][schema_name], resolver=resolver,
            ).validate(payload)
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        mcp_server.shutdown()
        mcp_server.server_close()
        mcp_thread.join(timeout=2)
