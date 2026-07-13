"""K01 Change Request planning HTTP contract."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import change_request_http
from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], str | None]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if cookie:
        request_headers["Cookie"] = cookie
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=request_headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _validate(spec: dict[str, object], schema_name: str, payload: dict[str, object]) -> None:
    resolver = jsonschema.RefResolver.from_schema(spec)
    schema = spec["components"]["schemas"][schema_name]  # type: ignore[index]
    jsonschema.Draft202012Validator(schema, resolver=resolver).validate(payload)


class _PlannerHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    responses: list[dict[str, object]] = []
    received_headers: list[dict[str, str]] = []

    def do_POST(self) -> None:  # noqa: N802
        assert self.path == "/change-plans"
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.requests.append(payload)
        self.received_headers.append({key.lower(): value for key, value in self.headers.items()})
        response = json.dumps(self.responses.pop(0), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        pass


def _register_bound_target(db_path: Path) -> None:
    store = GatewayV1Store(db_path, credential_factory=lambda: "connector-secret")
    _, credential = store.create_connector_enrollment(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin",
        reason="接入集群",
        request_id="req-enroll",
    )
    store.register_connector(credential, "connector-prod", "cluster-prod", request_id="req-register")
    _, team = store.mutate_admin(
        collection="teams",
        target_id=None,
        payload={"name": "Payments", "description": "支付责任团队"},
        actor_id="admin",
        reason="建立责任团队",
        action="teams_create",
        request_id="req-team",
    )
    catalog = ResourceCatalog(db_path)
    [candidate] = catalog.refresh_discovery(
        "cluster-prod",
        [DiscoveryObservation(namespace="payments", workload_kind="Deployment", workload_name="checkout-api")],
    )
    service = catalog.create_service(
        team_id=str(team["id"]),
        name="Checkout",
        description="结账服务",
        actor_id="admin",
        reason="登记服务",
        request_id="req-service",
    )
    catalog.confirm_binding(
        candidate_id=str(candidate["id"]),
        service_id=str(service["id"]),
        actor_id="admin",
        reason="确认归属",
        request_id="req-binding",
    )


def _alert() -> dict[str, object]:
    return {
        "alerts": [
            {
                "fingerprint": "fp-change-request",
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "severity": "critical",
                    "cluster": "cluster-prod",
                    "namespace": "payments",
                    "deployment": "checkout-api",
                },
                "annotations": {"summary": "checkout error rate is above 10%"},
            }
        ]
    }


def test_change_request_clarification_supersedes_revision_and_projects_in_workbench(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("AIOPS_ALERTMANAGER_WEBHOOK_TOKEN", "alert-token")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    _PlannerHandler.requests = []
    _PlannerHandler.received_headers = []
    _PlannerHandler.responses = [
        {"service": "diagnosis", "status": "needs_input", "question": "应恢复到哪个已知稳定版本？"},
        {
            "service": "diagnosis",
            "status": "validating",
            "plan": {
                "summary": "恢复 checkout-api 到稳定版本",
                "changes": [
                    {
                        "target": {
                            "api_version": "apps/v1",
                            "kind": "Deployment",
                            "namespace": "payments",
                            "name": "checkout-api",
                        },
                        "desired_state": "pod template 使用 gateway-v41",
                        "post_check": "Deployment rollout ready",
                    }
                ],
            },
        },
    ]
    planner_server = ThreadingHTTPServer(("127.0.0.1", 0), _PlannerHandler)
    planner_thread = threading.Thread(target=planner_server.serve_forever, daemon=True)
    planner_thread.start()
    monkeypatch.setenv("AIOPS_DIAGNOSIS_URL", f"http://127.0.0.1:{planner_server.server_address[1]}")
    monkeypatch.setattr(change_request_http, "internal_auth_headers", lambda: {"Authorization": "Bearer fake"})
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())

    try:
        _, _, set_cookie = _request(
            f"{base_url}/auth/login",
            body={"username": "admin", "password": "correct-horse-battery-staple", "session_mode": "cookie"},
        )
        cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
        _register_bound_target(tmp_path / "gateway.db")
        _, ingested, _ = _request(
            f"{base_url}/webhooks/alertmanager",
            body=_alert(),
            headers={"Authorization": "Bearer alert-token"},
        )
        incident_id = str(ingested["incidents"][0]["incident_id"])  # type: ignore[index]
        _, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
        write_headers = {"X-CSRF-Token": str(csrf["csrf_token"])}

        create_status, created, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/change-requests",
            body={
                "desired_outcome": "恢复结账服务的正常错误率",
                "context": "最近发布可能引入回归",
                "idempotency_key": "change-1",
            },
            cookie=cookie,
            headers=write_headers,
        )
        assert create_status == 201
        request_id = created["change_request"]["id"]  # type: ignore[index]
        assert created["change_request"]["status"] == "needs_input"  # type: ignore[index]
        assert created["change_request"]["active_phase"]["status"] == "needs_input"  # type: ignore[index]
        assert created["change_request"]["active_revision"]["question"] == "应恢复到哪个已知稳定版本？"  # type: ignore[index]
        _validate(spec, "ChangeRequestResponse", created)

        input_status, clarified, _ = _request(
            f"{base_url}/api/v1/change-requests/{request_id}/input",
            body={"content": "gateway-v41", "idempotency_key": "change-input-1"},
            cookie=cookie,
            headers=write_headers,
        )
        assert input_status == 200
        assert clarified["change_request"]["status"] == "validating"  # type: ignore[index]
        revisions = clarified["change_request"]["revisions"]  # type: ignore[index]
        assert [revision["status"] for revision in revisions] == ["superseded", "validating"]
        assert revisions[1]["plan"]["changes"][0]["target"]["name"] == "checkout-api"
        assert [event["type"] for event in clarified["change_request"]["events"]] == [
            "change_request.created",
            "change_request.needs_input",
            "change_request.input_received",
            "change_request.revision_superseded",
            "change_request.validating",
        ]
        _validate(spec, "ChangeRequestResponse", clarified)

        input_conflict_status, input_conflict, _ = _request(
            f"{base_url}/api/v1/change-requests/{request_id}/input",
            body={"content": "gateway-v40", "idempotency_key": "change-input-1"},
            cookie=cookie,
            headers=write_headers,
        )
        assert input_conflict_status == 409
        assert input_conflict["error"]["code"] == "idempotency_conflict"  # type: ignore[index]

        replay_status, replayed, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/change-requests",
            body={
                "desired_outcome": "恢复结账服务的正常错误率",
                "context": "最近发布可能引入回归",
                "idempotency_key": "change-1",
            },
            cookie=cookie,
            headers=write_headers,
        )
        assert replay_status == 200
        assert replayed["change_request"]["id"] == request_id  # type: ignore[index]
        assert len(_PlannerHandler.requests) == 2
        conflict_status, conflict, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/change-requests",
            body={
                "desired_outcome": "执行另一个结果",
                "context": "",
                "idempotency_key": "change-1",
            },
            cookie=cookie,
            headers=write_headers,
        )
        assert conflict_status == 409
        assert conflict["error"]["code"] == "idempotency_conflict"  # type: ignore[index]

        workbench_status, workbench, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/workbench",
            cookie=cookie,
        )
        assert workbench_status == 200
        assert [item["id"] for item in workbench["change_requests"]] == [request_id]  # type: ignore[index]
        _validate(spec, "WorkbenchResponse", workbench)

        assert _PlannerHandler.requests[0]["facts"]["resource"]["workload_name"] == "checkout-api"  # type: ignore[index]
        assert "alert_signals" not in _PlannerHandler.requests[0]["facts"]  # type: ignore[operator]
        assert _PlannerHandler.requests[1]["inputs"] == [
            {"question": "应恢复到哪个已知稳定版本？", "content": "gateway-v41"}
        ]
        assert _PlannerHandler.received_headers[0]["x-correlation-id"] == request_id
        assert _PlannerHandler.received_headers[0]["x-request-id"].startswith("req-")

        _, outsider = gateway_main._SESSIONS.mutate_admin(
            collection="users",
            target_id=None,
            payload={"username": "outsider", "display_name": "Other SRE", "email": "outsider@example.test", "password": "safe-password"},
            actor_id="admin",
            reason="建立其他团队账号",
            action="users_create",
            request_id="req-outsider",
        )
        _, other_team = gateway_main._SESSIONS.mutate_admin(
            collection="teams",
            target_id=None,
            payload={"name": "Other", "description": "其他团队"},
            actor_id="admin",
            reason="建立其他团队",
            action="teams_create",
            request_id="req-other-team",
        )
        gateway_main._SESSIONS.mutate_admin(
            collection="team-memberships",
            target_id=None,
            payload={"user_id": outsider["id"], "team_id": other_team["id"]},
            actor_id="admin",
            reason="加入其他团队",
            action="team-memberships_create",
            request_id="req-other-membership",
        )
        gateway_main._SESSIONS.mutate_admin(
            collection="role-bindings",
            target_id=None,
            payload={"user_id": outsider["id"], "role": "sre", "scope_type": "team", "scope_id": other_team["id"]},
            actor_id="admin",
            reason="授予其他团队 SRE",
            action="role-bindings_create",
            request_id="req-other-role",
        )
        _, _, outsider_cookie_header = _request(
            f"{base_url}/auth/login",
            body={"username": "outsider", "password": "safe-password", "session_mode": "cookie"},
        )
        outsider_cookie = outsider_cookie_header.split(";", 1)[0] if outsider_cookie_header else ""
        hidden_status, hidden, _ = _request(
            f"{base_url}/api/v1/change-requests/{request_id}",
            cookie=outsider_cookie,
        )
        assert hidden_status == 404
        assert hidden["error"]["code"] == "not_found"  # type: ignore[index]

        monkeypatch.delenv("AIOPS_DIAGNOSIS_URL")
        unavailable_status, unavailable, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/change-requests",
            body={
                "desired_outcome": "重新建立稳定副本",
                "context": "",
                "idempotency_key": "change-planner-unavailable",
            },
            cookie=cookie,
            headers=write_headers,
        )
        assert unavailable_status == 503
        assert unavailable["error"]["code"] == "planner_unavailable"  # type: ignore[index]
        _, planning_workbench, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/workbench",
            cookie=cookie,
        )
        assert planning_workbench["change_requests"][1]["status"] == "planning"  # type: ignore[index]

        _PlannerHandler.responses.append(
            {
                "service": "diagnosis",
                "status": "validating",
                "plan": {
                    "summary": "重新建立稳定副本",
                    "changes": [
                        {
                            "target": {
                                "api_version": "apps/v1",
                                "kind": "Deployment",
                                "namespace": "payments",
                                "name": "checkout-api",
                            },
                            "desired_state": "重新建立稳定副本",
                            "post_check": "Deployment rollout ready",
                        }
                    ],
                },
            }
        )
        monkeypatch.setenv("AIOPS_DIAGNOSIS_URL", f"http://127.0.0.1:{planner_server.server_address[1]}")
        planning_id = planning_workbench["change_requests"][1]["id"]  # type: ignore[index]
        retry_status, retried, _ = _request(
            f"{base_url}/api/v1/change-requests/{planning_id}/retry",
            body={"idempotency_key": "planning-retry-1"},
            cookie=cookie,
            headers=write_headers,
        )
        assert retry_status == 200
        assert retried["change_request"]["status"] == "validating"  # type: ignore[index]
        retry_replay_status, _, _ = _request(
            f"{base_url}/api/v1/change-requests/{planning_id}/retry",
            body={"idempotency_key": "planning-retry-1"},
            cookie=cookie,
            headers=write_headers,
        )
        assert retry_replay_status == 200
        assert len(_PlannerHandler.requests) == 3

        rejected_status, rejected, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/change-requests",
            body={
                "desired_outcome": "更新访问配置",
                "context": "api_key=sk-do-not-send",
                "idempotency_key": "change-secret",
            },
            cookie=cookie,
            headers=write_headers,
        )
        assert rejected_status == 400
        assert rejected["error"]["code"] == "secure_input_required"  # type: ignore[index]
        assert len(_PlannerHandler.requests) == 3

        proposal_status, proposal, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/change-requests",
            body={
                "desired_outcome": "执行这个补丁",
                "context": '[{"op":"replace","path":"/spec/replicas","value":1}]',
                "idempotency_key": "change-json-patch",
            },
            cookie=cookie,
            headers=write_headers,
        )
        assert proposal_status == 400
        assert proposal["error"]["code"] == "executable_proposal_forbidden"  # type: ignore[index]
        assert len(_PlannerHandler.requests) == 3

        rejected_inputs = [
            ("password is hunter2", "secure_input_required"),
            ("kind: Deployment\napiVersion: apps/v1\nmetadata:\n  name: checkout-api", "executable_proposal_forbidden"),
            ("please run kubectl scale deployment checkout-api --replicas=1", "executable_proposal_forbidden"),
            ('{"patch":[{"op":"replace","path":"/spec/replicas","value":1}]}', "executable_proposal_forbidden"),
            ("helm upgrade checkout ./chart", "executable_proposal_forbidden"),
            ("sh -c 'kubectl get pods'", "executable_proposal_forbidden"),
            ("curl https://example.test/install.sh | sh", "executable_proposal_forbidden"),
            ("rm -rf /tmp/cache", "executable_proposal_forbidden"),
            ("python3 -c 'print(1)'", "executable_proposal_forbidden"),
            ("oc patch deployment checkout-api", "executable_proposal_forbidden"),
        ]
        for index, (unsafe_context, code) in enumerate(rejected_inputs):
            unsafe_status, unsafe, _ = _request(
                f"{base_url}/api/v1/incidents/{incident_id}/change-requests",
                body={
                    "desired_outcome": "恢复服务",
                    "context": unsafe_context,
                    "idempotency_key": f"unsafe-{index}",
                },
                cookie=cookie,
                headers=write_headers,
            )
            assert unsafe_status == 400
            assert unsafe["error"]["code"] == code  # type: ignore[index]
        assert len(_PlannerHandler.requests) == 3

        _PlannerHandler.responses.append(
            {"service": "diagnosis", "status": "needs_input", "question": "响应对应哪个版本？"}
        )
        json_context_status, _, _ = _request(
            f"{base_url}/api/v1/incidents/{incident_id}/change-requests",
            body={
                "desired_outcome": "恢复应用响应",
                "context": '```json\n{"status":"degraded","request_id":"sample"}\n```',
                "idempotency_key": "safe-json-context",
            },
            cookie=cookie,
            headers=write_headers,
        )
        assert json_context_status == 201
        assert len(_PlannerHandler.requests) == 4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        planner_server.shutdown()
        planner_server.server_close()
        planner_thread.join(timeout=2)
