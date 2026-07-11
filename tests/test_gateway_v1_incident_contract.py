"""T07 Alert Signal to visible Incident HTTP contract."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    authorization: str | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
) -> tuple[int, dict[str, object], str | None]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if cookie:
        request_headers["Cookie"] = cookie
    if authorization:
        request_headers["Authorization"] = authorization
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=request_headers,
        method=method or ("POST" if body is not None else "GET"),
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


def _alert(fingerprint: str, *, cluster_id: str = "cluster-prod", status: str = "firing") -> dict[str, object]:
    return {
        "alerts": [
            {
                "fingerprint": fingerprint,
                "status": status,
                "startsAt": "2026-07-11T01:00:00Z",
                "labels": {
                    "alertname": "HighErrorRate",
                    "severity": "critical",
                    "cluster": cluster_id,
                    "namespace": "payments",
                    "deployment": "checkout-api",
                },
                "annotations": {"summary": "checkout error rate is above 10%"},
            }
        ]
    }


def _register_bound_target(db_path: Path) -> None:
    store = GatewayV1Store(db_path, credential_factory=lambda: "connector-secret")
    _, credential = store.create_connector_enrollment(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin",
        reason="接入生产集群",
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
        reason="确认工作负载归属",
        request_id="req-binding",
    )


def test_alertmanager_ingress_lists_incident_and_returns_workbench_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("AIOPS_ALERTMANAGER_WEBHOOK_TOKEN", "alert-token")
    monkeypatch.setenv("AIOPS_INCIDENT_STABILIZATION_SECONDS", "0")
    monkeypatch.setenv("AIOPS_INCIDENT_REOPEN_SECONDS", "120")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())

    try:
        hidden_status, hidden, _ = _request(f"{base_url}/api/v1/investigations/not-real/events")
        login_status, _, set_cookie = _request(
            f"{base_url}/auth/login",
            body={"username": "admin", "password": "correct-horse-battery-staple", "session_mode": "cookie"},
        )
        assert login_status == 200
        assert hidden_status == 401
        assert hidden["error"]["code"] == "unauthorized"  # type: ignore[index]
        cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
        _register_bound_target(tmp_path / "gateway.db")

        first_status, first, _ = _request(
            f"{base_url}/webhooks/alertmanager",
            body=_alert("fp-pod-a"),
            authorization="Bearer alert-token",
        )
        second_status, second, _ = _request(
            f"{base_url}/webhooks/alertmanager",
            body=_alert("fp-pod-b"),
            authorization="Bearer alert-token",
        )
        list_status, listing, _ = _request(f"{base_url}/api/v1/incidents", cookie=cookie)

        assert first_status == second_status == list_status == 200
        assert first["incidents"][0]["created"] is True  # type: ignore[index]
        assert second["incidents"][0]["created"] is False  # type: ignore[index]
        assert len(listing["incidents"]) == 1  # type: ignore[arg-type]
        incident = listing["incidents"][0]  # type: ignore[index]
        assert incident["binding_status"] == "bound"
        assert incident["status"] == "active"
        assert incident["signal_count"] == 2

        workbench_status, workbench, _ = _request(
            f"{base_url}/api/v1/incidents/{incident['id']}/workbench",
            cookie=cookie,
        )
        assert workbench_status == 200
        assert [signal["fingerprint"] for signal in workbench["alert_signals"]] == ["fp-pod-a", "fp-pod-b"]  # type: ignore[index]
        assert workbench["investigation"]["status"] == "queued"  # type: ignore[index]
        assert workbench["resource_context"]["service_name"] == "Checkout"  # type: ignore[index]
        assert len(workbench["snapshot_revision"]) == 16
        assert "session_id" not in json.dumps(workbench)
        _validate(spec, "IncidentListResponse", listing)
        _validate(spec, "WorkbenchResponse", workbench)

        _, csrf, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
        investigation_id = workbench["investigation"]["id"]  # type: ignore[index]
        extra_status, _, _ = _request(
            f"{base_url}/api/v1/investigations/{investigation_id}/human-input",
            body={"kind": "assertion", "content": "发布发生在告警前", "idempotency_key": "input-extra", "admin": True},
            cookie=cookie,
            headers={"X-CSRF-Token": str(csrf["csrf_token"])},
        )
        denied_status, _, _ = _request(
            f"{base_url}/api/v1/investigations/{investigation_id}/human-input",
            body={"kind": "assertion", "content": "发布发生在告警前", "idempotency_key": "input-denied"},
            cookie=cookie,
        )
        input_status, human_input, _ = _request(
            f"{base_url}/api/v1/investigations/{investigation_id}/human-input",
            body={"kind": "assertion", "content": "发布发生在告警前", "idempotency_key": "input-1"},
            cookie=cookie,
            headers={"X-CSRF-Token": str(csrf["csrf_token"])},
        )
        events_status, replay, _ = _request(
            f"{base_url}/api/v1/investigations/{investigation_id}/events?after=1&limit=10",
            cookie=cookie,
        )
        assert denied_status == 403
        assert extra_status == 400
        assert input_status == events_status == 200
        assert human_input["event"]["type"] == "human_input.assertion"  # type: ignore[index]
        assert [event["id"] for event in replay["events"]] == [2]  # type: ignore[index]
        _validate(spec, "InvestigationEventResponse", human_input)
        _validate(spec, "InvestigationEventsResponse", replay)

        stream_request = urllib.request.Request(
            f"{base_url}/api/v1/investigations/{investigation_id}/events/stream",
            headers={"Cookie": cookie, "Last-Event-ID": "1", "Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            lines = [stream.readline().decode().strip() for _ in range(3)]
        assert lines[0] == "id: 2"
        assert lines[1] == "event: investigation"
        assert json.loads(lines[2].removeprefix("data: "))["type"] == "human_input.assertion"

        control_status, control, _ = _request(
            f"{base_url}/api/v1/investigations/{investigation_id}/controls",
            body={"action": "terminate", "idempotency_key": "control-1"},
            cookie=cookie,
            headers={"X-CSRF-Token": str(csrf["csrf_token"])},
        )
        reinvestigate_status, reinvestigated, _ = _request(
            f"{base_url}/api/v1/incidents/{incident['id']}/reinvestigate",
            body={"idempotency_key": "reinvestigate-1"},
            cookie=cookie,
            headers={"X-CSRF-Token": str(csrf["csrf_token"])},
        )
        assert control_status == reinvestigate_status == 200
        assert control["event"]["payload"]["to"] == "terminated"  # type: ignore[index]
        assert reinvestigated["investigation"]["sequence"] == 2  # type: ignore[index]

        _request(
            f"{base_url}/webhooks/alertmanager",
            body=_alert("fp-pod-a", status="resolved"),
            authorization="Bearer alert-token",
        )
        _request(
            f"{base_url}/webhooks/alertmanager",
            body=_alert("fp-pod-b", status="resolved"),
            authorization="Bearer alert-token",
        )
        _, recovered, _ = _request(
            f"{base_url}/api/v1/incidents/{incident['id']}/workbench",
            cookie=cookie,
        )
        assert recovered["incident"]["lifecycle_state"] == "resolved"  # type: ignore[index]
        assert recovered["recovery_observation"]["status"] == "resolved"  # type: ignore[index]
        _validate(spec, "WorkbenchResponse", recovered)

        _, refired, _ = _request(
            f"{base_url}/webhooks/alertmanager",
            body=_alert("fp-pod-a"),
            authorization="Bearer alert-token",
        )
        assert refired["incidents"][0]["incident_id"] == incident["id"]  # type: ignore[index]
        _, reopened, _ = _request(
            f"{base_url}/api/v1/incidents/{incident['id']}/workbench",
            cookie=cookie,
        )
        assert reopened["incident"]["lifecycle_state"] == "reopened"  # type: ignore[index]
        _validate(spec, "WorkbenchResponse", reopened)

        rejected_status, rejected, _ = _request(
            f"{base_url}/webhooks/alertmanager",
            body=_alert("fp-unknown", cluster_id="cluster-missing"),
            authorization="Bearer alert-token",
        )
        assert rejected_status == 422
        assert rejected["error"]["code"] == "cluster_not_registered"  # type: ignore[index]

        current_investigation_id = reinvestigated["investigation"]["id"]  # type: ignore[index]
        revoked_stream_request = urllib.request.Request(
            f"{base_url}/api/v1/investigations/{current_investigation_id}/events/stream?after=1",
            headers={"Cookie": cookie, "Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(revoked_stream_request, timeout=3) as revoked_stream:
            logout_status, _, _ = _request(
                f"{base_url}/auth/logout",
                cookie=cookie,
                headers={"X-CSRF-Token": str(csrf["csrf_token"])},
                method="POST",
            )
            revoked_lines = [revoked_stream.readline().decode().strip() for _ in range(2)]
        assert logout_status == 200
        assert revoked_lines == ["event: permission_denied", "data: {}"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
