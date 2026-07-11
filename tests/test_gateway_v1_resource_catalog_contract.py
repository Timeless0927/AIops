"""T06 Resource Catalog HTTP contract."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    credential: str | None = None,
) -> tuple[int, dict, str | None]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _login(base_url: str) -> tuple[str, str]:
    status, _, set_cookie = _request(
        f"{base_url}/auth/login",
        method="POST",
        body={"username": "admin", "password": "admin-pass", "session_mode": "cookie"},
    )
    assert status == 200 and set_cookie
    cookie = set_cookie.split(";", 1)[0]
    _, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    return cookie, csrf["csrf_token"]


def test_connector_discovery_and_fresh_admin_binding_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        cookie, csrf = _login(base_url)
        _, enrollment, _ = _request(
            f"{base_url}/api/v1/admin/connector-enrollments",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "reason": "接入生产集群"},
            cookie=cookie,
            csrf=csrf,
        )
        credential = enrollment["credential"]
        _request(
            f"{base_url}/api/v1/connectors/register",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod"},
            credential=credential,
        )
        discovery_body = {
            "connector_id": "connector-prod",
            "cluster_id": "cluster-prod",
            "candidates": [
                {
                    "namespace": "payments",
                    "workload_kind": "Deployment",
                    "workload_name": "checkout-api",
                    "service_name": "checkout",
                    "service_hint": "checkout-label",
                    "team_hint": "payments-label",
                }
            ],
        }
        denied_status, denied, _ = _request(
            f"{base_url}/api/v1/connectors/discovery-candidates",
            method="POST",
            body=discovery_body,
            credential="wrong-secret",
        )
        discovery_status, discovered, _ = _request(
            f"{base_url}/api/v1/connectors/discovery-candidates",
            method="POST",
            body=discovery_body,
            credential=credential,
        )
        assert denied_status == 401
        assert denied["error"]["code"] == "invalid_connector_credential"
        assert discovery_status == 200
        candidate = discovered["discovery_candidates"][0]
        assert candidate["binding_status"] == "unbound"

        with sqlite3.connect(tmp_path / "gateway.db") as conn:
            conn.execute("UPDATE sessions SET fresh_at = 0")
        stale_status, stale, _ = _request(
            f"{base_url}/api/v1/admin/services",
            method="POST",
            body={"team_id": "missing", "name": "Checkout", "description": "结账服务", "reason": "登记服务"},
            cookie=cookie,
            csrf=csrf,
        )
        assert stale_status == 403
        assert stale["error"]["code"] == "fresh_auth_required"
        _request(
            f"{base_url}/auth/reauth",
            method="POST",
            body={"password": "admin-pass"},
            cookie=cookie,
            csrf=csrf,
        )
        _, team_payload, _ = _request(
            f"{base_url}/api/v1/admin/teams",
            method="POST",
            body={"name": "Payments", "description": "支付责任团队", "reason": "建立责任团队"},
            cookie=cookie,
            csrf=csrf,
        )
        service_status, service_payload, _ = _request(
            f"{base_url}/api/v1/admin/services",
            method="POST",
            body={"team_id": team_payload["team"]["id"], "name": "Checkout", "description": "结账服务", "reason": "登记服务"},
            cookie=cookie,
            csrf=csrf,
        )
        binding_status, binding_payload, _ = _request(
            f"{base_url}/api/v1/admin/resource-bindings",
            method="POST",
            body={"candidate_id": candidate["id"], "service_id": service_payload["service"]["id"], "reason": "确认资源归属"},
            cookie=cookie,
            csrf=csrf,
        )
        correction_status, _, _ = _request(
            f"{base_url}/api/v1/admin/resource-bindings/{binding_payload['resource_binding']['id']}",
            method="PATCH",
            body={"service_id": "missing-service", "reason": "验证失败纠正审计"},
            cookie=cookie,
            csrf=csrf,
        )
        state_status, state, _ = _request(f"{base_url}/api/v1/admin/resource-catalog", cookie=cookie)
        _, audit, _ = _request(f"{base_url}/api/v1/admin/audit", cookie=cookie)

        assert service_status == binding_status == 201
        assert correction_status == 404
        assert state_status == 200
        assert binding_payload["resource_binding"]["team_id"] == team_payload["team"]["id"]
        assert state["discovery_candidates"][0]["binding_status"] == "bound"
        failed_correction = next(row for row in audit["audit"] if row["result"] == "service_not_found")
        assert failed_correction["before"]["id"] == binding_payload["resource_binding"]["id"]
        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ResourceCatalogStateResponse"], resolver=resolver
        ).validate(state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
