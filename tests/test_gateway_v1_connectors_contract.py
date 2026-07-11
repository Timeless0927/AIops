"""T05 Connector Enrollment and Cluster presence HTTP contract."""

from __future__ import annotations

import json
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
    csrf_status, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    assert csrf_status == 200
    return cookie, csrf["csrf_token"]


def test_connector_enrollment_controls_cluster_presence_and_runtime(tmp_path: Path, monkeypatch) -> None:
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
        enroll_status, enrolled, _ = _request(
            f"{base_url}/api/v1/admin/connector-enrollments",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "reason": "接入生产集群"},
            cookie=cookie,
            csrf=csrf,
        )
        credential = enrolled["credential"]
        state_status, state, _ = _request(
            f"{base_url}/api/v1/admin/connector-enrollments",
            cookie=cookie,
        )
        assert enroll_status == 201
        assert state_status == 200
        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ConnectorAdminStateResponse"], resolver=resolver
        ).validate(state)
        assert state["clusters"] == []
        assert state["connector_enrollments"] == [
            {
                "id": enrolled["connector_enrollment"]["id"],
                "connector_id": "connector-prod",
                "cluster_id": "cluster-prod",
                "active": True,
                "registered": False,
            }
        ]
        assert credential not in json.dumps(state)

        duplicate_connector, duplicate_connector_body, _ = _request(
            f"{base_url}/api/v1/admin/connector-enrollments",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-other", "reason": "重复 identity"},
            cookie=cookie,
            csrf=csrf,
        )
        duplicate_cluster, _, _ = _request(
            f"{base_url}/api/v1/admin/connector-enrollments",
            method="POST",
            body={"connector_id": "connector-other", "cluster_id": "cluster-prod", "reason": "重复 Cluster"},
            cookie=cookie,
            csrf=csrf,
        )
        assert duplicate_connector == duplicate_cluster == 409
        assert duplicate_connector_body["error"]["code"] == "enrollment_exists"

        mismatch_status, mismatch, _ = _request(
            f"{base_url}/api/v1/connectors/register",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-other"},
            credential=credential,
        )
        register_status, registered, _ = _request(
            f"{base_url}/api/v1/connectors/register",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod"},
            credential=credential,
        )
        assert mismatch_status == 403
        assert mismatch["error"]["code"] == "identity_mismatch"
        assert register_status == 201
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ConnectorClusterResponse"], resolver=resolver
        ).validate(registered)
        assert registered["cluster"]["mutation_enabled"] is False
        assert registered["cluster"]["runtime_status"] == "offline"

        invalid_status, invalid, _ = _request(
            f"{base_url}/api/v1/connectors/heartbeat",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "extra": True},
            credential=credential,
        )
        assert invalid_status == 400
        assert invalid["error"]["code"] == "invalid_request"
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ErrorResponse"], resolver=resolver
        ).validate(invalid)

        heartbeat_status, heartbeat, _ = _request(
            f"{base_url}/api/v1/connectors/heartbeat",
            method="POST",
            body={
                "connector_id": "connector-prod",
                "cluster_id": "cluster-prod",
                "status": "degraded",
                "failure_summary": "Kubernetes API latency is high",
            },
            credential=credential,
        )
        assert heartbeat_status == 200
        assert heartbeat["cluster"]["runtime_status"] == "degraded"

        update_status, updated, _ = _request(
            f"{base_url}/api/v1/admin/clusters/cluster-prod",
            method="PATCH",
            body={
                "display_name": "生产集群",
                "environment": "prod",
                "governance_notes": "核心支付工作负载",
                "mutation_enabled": True,
                "reason": "确认集群治理策略",
            },
            cookie=cookie,
            csrf=csrf,
        )
        assert update_status == 200
        assert updated["cluster"]["display_name"] == "生产集群"
        assert updated["cluster"]["mutation_enabled"] is True

        rotate_status, rotated, _ = _request(
            f"{base_url}/api/v1/admin/connector-enrollments/{enrolled['connector_enrollment']['id']}",
            method="PATCH",
            body={"rotate_credential": True, "reason": "定期轮换"},
            cookie=cookie,
            csrf=csrf,
        )
        old_status, _, _ = _request(
            f"{base_url}/api/v1/connectors/heartbeat",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "status": "online"},
            credential=credential,
        )
        new_credential = rotated["credential"]
        reregister_status, _, _ = _request(
            f"{base_url}/api/v1/connectors/register",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod"},
            credential=new_credential,
        )
        new_status, _, _ = _request(
            f"{base_url}/api/v1/connectors/heartbeat",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "status": "online"},
            credential=new_credential,
        )
        repeated_status, _, _ = _request(
            f"{base_url}/api/v1/connectors/heartbeat",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "status": "online"},
            credential=new_credential,
        )
        assert rotate_status == 200
        assert old_status == 401
        assert reregister_status == 200
        assert new_status == 200
        assert repeated_status == 200

        revoke_status, _, _ = _request(
            f"{base_url}/api/v1/admin/connector-enrollments/{enrolled['connector_enrollment']['id']}",
            method="PATCH",
            body={"active": False, "reason": "Connector 下线"},
            cookie=cookie,
            csrf=csrf,
        )
        revoked_status, _, _ = _request(
            f"{base_url}/api/v1/connectors/heartbeat",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "status": "online"},
            credential=new_credential,
        )
        _, final_state, _ = _request(
            f"{base_url}/api/v1/admin/connector-enrollments",
            cookie=cookie,
        )
        ready_status, ready, _ = _request(f"{base_url}/readyz")
        audit_status, audit, _ = _request(f"{base_url}/api/v1/admin/audit", cookie=cookie)
        assert revoke_status == 200
        assert revoked_status == 401
        assert ready_status == 200
        assert ready["registered_connectors"] == 0
        assert audit_status == 200
        assert {row["action"] for row in audit["audit"]} >= {"connector_register", "connector_heartbeat"}
        assert sum(row["action"] == "connector_register" and row["result"] == "success" for row in audit["audit"]) == 1
        assert sum(row["action"] == "connector_heartbeat" and row["result"] == "success" for row in audit["audit"]) == 2
        assert any(row["result"] == "enrollment_exists" for row in audit["audit"])
        duplicate_audit = next(row for row in audit["audit"] if row["result"] == "enrollment_exists")
        assert duplicate_audit["target_id"] in {"connector-prod:cluster-other", "connector-other:cluster-prod"}
        assert credential not in json.dumps(audit)
        assert new_credential not in json.dumps(audit)
        assert final_state["clusters"][0]["runtime_status"] == "offline"
        assert "credential" not in json.dumps(final_state["connector_enrollments"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()


def test_read_command_long_poll_is_durable_idempotent_and_reconciles_late_results(
    tmp_path: Path, monkeypatch
) -> None:
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
        _, enrolled, _ = _request(
            f"{base_url}/api/v1/admin/connector-enrollments",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "reason": "接入生产集群"},
            cookie=cookie,
            csrf=csrf,
        )
        credential = enrolled["credential"]
        _request(
            f"{base_url}/api/v1/connectors/register",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod"},
            credential=credential,
        )
        queued_status, queued, _ = _request(
            f"{base_url}/api/v1/admin/connector-commands",
            method="POST",
            body={
                "cluster_id": "cluster-prod",
                "namespace": "payments",
                "action": "get_resource",
                "parameters": {"resource_kind": "pods", "selector": "app=api", "output": "json"},
                "reason": "确认告警工作负载",
            },
            cookie=cookie,
            csrf=csrf,
        )
        wrong_identity_status, wrong_identity, _ = _request(
            f"{base_url}/api/v1/connectors/commands/poll",
            method="POST",
            body={"connector_id": "connector-other", "cluster_id": "cluster-prod", "wait_seconds": 0},
            credential=credential,
        )
        invalid_wait_status, invalid_wait, _ = _request(
            f"{base_url}/api/v1/connectors/commands/poll",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "wait_seconds": float("nan")},
            credential=credential,
        )
        poll_status, polled, _ = _request(
            f"{base_url}/api/v1/connectors/commands/poll",
            method="POST",
            body={"connector_id": "connector-prod", "cluster_id": "cluster-prod", "wait_seconds": 0},
            credential=credential,
        )
        command = polled["command"]
        assert queued_status == 201
        assert wrong_identity_status == 403 and wrong_identity["error"]["code"] == "identity_mismatch"
        assert invalid_wait_status == 400 and invalid_wait["error"]["code"] == "invalid_request"
        assert poll_status == 200
        assert command["id"] == queued["command"]["id"]
        assert "argv" not in json.dumps(command)
        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ConnectorCommandResponse"], resolver=resolver
        ).validate(queued)
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ConnectorCommandPollResponse"], resolver=resolver
        ).validate(polled)

        start_status, started, _ = _request(
            f"{base_url}/api/v1/connectors/commands/{command['id']}/start",
            method="POST",
            body={
                "connector_id": "connector-prod",
                "cluster_id": "cluster-prod",
                "lease_id": command["lease_id"],
            },
            credential=credential,
        )
        result = {
            "status": "succeeded",
            "stdout": '{"items":[]}',
            "stderr": "",
            "exit_code": 0,
            "truncated": False,
            "error_code": None,
            "error_message": None,
        }
        result_status, accepted, _ = _request(
            f"{base_url}/api/v1/connectors/commands/{command['id']}/result",
            method="POST",
            body={
                "connector_id": "connector-prod",
                "cluster_id": "cluster-prod",
                "lease_id": command["lease_id"],
                "result": result,
            },
            credential=credential,
        )
        repeated_status, repeated, _ = _request(
            f"{base_url}/api/v1/connectors/commands/{command['id']}/result",
            method="POST",
            body={
                "connector_id": "connector-prod",
                "cluster_id": "cluster-prod",
                "lease_id": command["lease_id"],
                "result": result,
            },
            credential=credential,
        )
        conflict_status, conflict, _ = _request(
            f"{base_url}/api/v1/connectors/commands/{command['id']}/result",
            method="POST",
            body={
                "connector_id": "connector-prod",
                "cluster_id": "cluster-prod",
                "lease_id": command["lease_id"],
                "result": {**result, "stdout": "conflicting"},
            },
            credential=credential,
        )
        _, state, _ = _request(f"{base_url}/api/v1/admin/connector-enrollments", cookie=cookie)
        audit_status, audit, _ = _request(f"{base_url}/api/v1/admin/audit", cookie=cookie)

        assert start_status == 200 and started["status"] == "started"
        assert result_status == repeated_status == 200
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ConnectorCommandStartResponse"], resolver=resolver
        ).validate(started)
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ConnectorCommandResultResponse"], resolver=resolver
        ).validate(accepted)
        assert accepted["status"] == "succeeded"
        assert repeated["idempotent"] is True
        assert conflict_status == 409 and conflict["error"]["code"] == "conflicting_result"
        assert state["clusters"][0]["pending_read_commands"] == 0
        assert state["clusters"][0]["last_read_command"]["status"] == "succeeded"
        assert state["clusters"][0]["last_read_result"]["status"] == "succeeded"
        jsonschema.Draft202012Validator(
            spec["components"]["schemas"]["ConnectorAdminStateResponse"], resolver=resolver
        ).validate(state)
        assert audit_status == 200
        assert any(row["action"] == "connector_command_result_conflict" for row in audit["audit"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
