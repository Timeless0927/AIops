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
) -> tuple[int, dict[str, object], str | None]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
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


def _alert(fingerprint: str, *, cluster_id: str = "cluster-prod") -> dict[str, object]:
    return {
        "alerts": [
            {
                "fingerprint": fingerprint,
                "status": "firing",
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
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    gateway_main._SESSIONS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())

    try:
        login_status, _, set_cookie = _request(
            f"{base_url}/auth/login",
            body={"username": "admin", "password": "correct-horse-battery-staple", "session_mode": "cookie"},
        )
        assert login_status == 200
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

        rejected_status, rejected, _ = _request(
            f"{base_url}/webhooks/alertmanager",
            body=_alert("fp-unknown", cluster_id="cluster-missing"),
            authorization="Bearer alert-token",
        )
        assert rejected_status == 422
        assert rejected["error"]["code"] == "cluster_not_registered"  # type: ignore[index]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
