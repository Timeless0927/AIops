from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema
import pytest

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.mcp_registry import MCPRegistry
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    request_id: str | None = None,
) -> tuple[int, dict[str, object], str | None]:
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
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(outbound, timeout=3) as response:
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
    status, payload, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    assert status == 200
    return cookie, str(payload["csrf_token"])


def test_skill_admin_versions_switches_dependencies_and_audit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    store = GatewayV1Store(tmp_path / "gateway.db")
    mcp = MCPRegistry(
        store.database,
        id_factory=lambda: "mcp-metrics",
        revision_id=lambda: "mcp-revision:metrics",
    )
    mcp.create(
        name="Prometheus",
        endpoint="https://mcp.example.test",
        credential=None,
        capabilities=[{"name": "query_metrics", "version": "prometheus-query-v1", "read_only": True}],
        allowed_scope=[{"cluster_id": "cluster-prod", "namespace": "payments"}],
        enabled=True,
        actor_id="admin",
        reason="register metrics",
        request_id="mcp-create",
    )
    mcp.verify(
        "mcp-metrics",
        actor_id="admin",
        reason="verify metrics",
        request_id="mcp-verify",
        probe=lambda *_args: {
            "status": "ok",
            "capabilities": [{
                "name": "query_metrics", "version": "prometheus-query-v1",
                "read_only": True, "mutation": False, "path": "/query_metrics",
            }],
        },
    )
    monkeypatch.setattr(gateway_main, "_GATEWAY", store)
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    reference = {
        "integration_id": "mcp-metrics",
        "integration_revision": "mcp-revision:metrics",
        "name": "query_metrics",
        "version": "prometheus-query-v1",
    }
    create_body = {
        "name": "Payments triage",
        "instruction": "Check error-rate Observation before concluding.",
        "workflow": ["Query metrics", "Cite accepted Evidence"],
        "applicable_scope": [{"cluster_id": "cluster-prod", "namespace": "payments"}],
        "required_mcp": [reference],
        "reason": "capture reviewed triage practice",
    }
    try:
        unauthorized_status, _, _ = _request(f"{base_url}/api/v1/admin/skills")
        cookie, csrf = _login(base_url)
        with store.database.connect() as conn:
            conn.execute("UPDATE sessions SET fresh_at = 0")
        stale_status, stale, _ = _request(
            f"{base_url}/api/v1/admin/skills",
            method="POST",
            body=create_body,
            cookie=cookie,
            csrf=csrf,
            request_id="skill-stale-create",
        )
        reauth_status, _, _ = _request(
            f"{base_url}/auth/reauth",
            method="POST",
            body={"password": "admin-pass"},
            cookie=cookie,
            csrf=csrf,
        )
        create_status, created, _ = _request(
            f"{base_url}/api/v1/admin/skills",
            method="POST",
            body=create_body,
            cookie=cookie,
            csrf=csrf,
            request_id="skill-create-contract",
        )
        skill_id = str(created["skill"]["id"])  # type: ignore[index]
        version_status, versioned, _ = _request(
            f"{base_url}/api/v1/admin/skills/{skill_id}/versions",
            method="POST",
            body={
                **{key: value for key, value in create_body.items() if key != "name"},
                "instruction": "Check saturation before concluding.",
                "reason": "refine first check",
            },
            cookie=cookie,
            csrf=csrf,
            request_id="skill-version-contract",
        )
        enable_v1_status, enabled_v1, _ = _request(
            f"{base_url}/api/v1/admin/skills/{skill_id}/enable",
            method="POST",
            body={"version": 1, "expected_active_version": None, "reason": "enable reviewed v1"},
            cookie=cookie,
            csrf=csrf,
            request_id="skill-enable-v1-contract",
        )
        enable_v2_status, enabled_v2, _ = _request(
            f"{base_url}/api/v1/admin/skills/{skill_id}/enable",
            method="POST",
            body={"version": 2, "expected_active_version": 1, "reason": "switch to reviewed v2"},
            cookie=cookie,
            csrf=csrf,
            request_id="skill-enable-v2-contract",
        )
        bad_version_status, bad_version, _ = _request(
            f"{base_url}/api/v1/admin/skills/{skill_id}/versions",
            method="POST",
            body={
                **{key: value for key, value in create_body.items() if key != "name"},
                "required_mcp": [{**reference, "integration_revision": "stale-revision"}],
                "reason": "record dependency update",
            },
            cookie=cookie,
            csrf=csrf,
            request_id="skill-version-stale-contract",
        )
        denied_status, denied, _ = _request(
            f"{base_url}/api/v1/admin/skills/{skill_id}/enable",
            method="POST",
            body={"version": 3, "expected_active_version": 2, "reason": "attempt stale dependency"},
            cookie=cookie,
            csrf=csrf,
            request_id="skill-enable-denied-contract",
        )
        disable_status, disabled, _ = _request(
            f"{base_url}/api/v1/admin/skills/{skill_id}/disable",
            method="POST",
            body={"expected_active_version": 2, "reason": "disable practice"},
            cookie=cookie,
            csrf=csrf,
            request_id="skill-disable-contract",
        )
        invalid_status, invalid, _ = _request(
            f"{base_url}/api/v1/admin/skills",
            method="POST",
            body={**create_body, "script": "kubectl delete deployment checkout-api"},
            cookie=cookie,
            csrf=csrf,
            request_id="skill-script-contract",
        )
        list_status, listed, _ = _request(f"{base_url}/api/v1/admin/skills", cookie=cookie)
        detail_status, detail, _ = _request(f"{base_url}/api/v1/admin/skills/{skill_id}", cookie=cookie)
        _, audit, _ = _request(f"{base_url}/api/v1/admin/audit", cookie=cookie)

        assert unauthorized_status == 401
        assert stale_status == 403 and stale["error"]["code"] == "fresh_auth_required"  # type: ignore[index]
        assert reauth_status == 200 and create_status == version_status == bad_version_status == 201
        assert enable_v1_status == enable_v2_status == disable_status == list_status == detail_status == 200
        assert denied_status == 409 and denied["error"]["code"] == "skill_dependency_unavailable"  # type: ignore[index]
        assert invalid_status == 400 and invalid["error"]["code"] == "invalid_request"  # type: ignore[index]
        assert created["skill"]["enabled"] is False  # type: ignore[index]
        assert versioned["skill"]["latest_version"] == 2  # type: ignore[index]
        assert enabled_v1["skill"]["active_version"] == 1  # type: ignore[index]
        assert enabled_v2["skill"]["active_version"] == 2  # type: ignore[index]
        assert bad_version["skill"]["versions"][2]["dependency"]["state"] == "unavailable"  # type: ignore[index]
        assert disabled["skill"]["enabled"] is False  # type: ignore[index]
        assert listed["skills"] == [detail["skill"]]
        assert {item["action"] for item in audit["audit"]} >= {  # type: ignore[index]
            "skill_create", "skill_version_create", "skill_enable",
            "skill_enable_denied", "skill_disable",
        }

        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        for schema_name, payload in (
            ("SkillCreateRequest", create_body),
            ("SkillVersionCreateRequest", {
                **{key: value for key, value in create_body.items() if key != "name"},
                "instruction": "Check saturation before concluding.",
            }),
            ("SkillEnableRequest", {
                "version": 2, "expected_active_version": 1, "reason": "switch to reviewed v2",
            }),
            ("SkillDisableRequest", {
                "expected_active_version": 2, "reason": "disable practice",
            }),
        ):
            jsonschema.Draft202012Validator(
                spec["components"]["schemas"][schema_name], resolver=resolver,
            ).validate(payload)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(
                spec["components"]["schemas"]["SkillCreateRequest"], resolver=resolver,
            ).validate({**create_body, "script": "kubectl delete deployment checkout-api"})
        for schema_name, payload in (
            ("SkillResponse", created),
            ("SkillResponse", versioned),
            ("SkillResponse", enabled_v1),
            ("SkillResponse", enabled_v2),
            ("SkillResponse", bad_version),
            ("SkillResponse", disabled),
            ("SkillListResponse", listed),
            ("SkillResponse", detail),
        ):
            jsonschema.Draft202012Validator(
                spec["components"]["schemas"][schema_name], resolver=resolver,
            ).validate(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
