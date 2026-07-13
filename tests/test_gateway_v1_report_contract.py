"""T15 V1 Incident Report HTTP and OpenAPI contract."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.incident import AlertSignal
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    method: str | None = None,
) -> tuple[int, dict[str, object], str | None]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method=method or ("POST" if body is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _validate(spec: dict[str, object], schema: str, payload: dict[str, object]) -> None:
    jsonschema.Draft202012Validator(
        spec["components"]["schemas"][schema],  # type: ignore[index]
        resolver=jsonschema.RefResolver.from_schema(spec),
    ).validate(payload)


def _bound_incident() -> str:
    store = gateway_main._SESSIONS
    _, team = store.mutate_admin(
        collection="teams", target_id=None, payload={"name": "Payments", "description": ""},
        actor_id="admin", reason="test", action="teams_create", request_id="req-team",
    )
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="req-enroll",
    )
    store.connector_enrollments.register(credential, "connector-prod", "cluster-prod", request_id="req-register")
    catalog = ResourceCatalog(store.database)
    [candidate] = catalog.refresh_discovery(
        "cluster-prod",
        [DiscoveryObservation(namespace="payments", workload_kind="Deployment", workload_name="checkout-api")],
    )
    service = catalog.create_service(
        team_id=str(team["id"]), name="Checkout", description="", actor_id="admin",
        reason="test", request_id="req-service",
    )
    catalog.confirm_binding(
        candidate_id=str(candidate["id"]), service_id=str(service["id"]), actor_id="admin",
        reason="test", request_id="req-binding",
    )
    created = gateway_main._incident_service().ingest(AlertSignal(
        fingerprint="fp-report-contract", alertname="HighErrorRate", cluster_id="cluster-prod",
        namespace="payments", status="firing", severity="critical", summary="errors",
        workload_kind="Deployment", workload_name="checkout-api",
    ))
    return str(created["incident"]["id"])


def test_report_draft_edit_and_explicit_immutable_publish(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "correct-horse-battery-staple")
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
        assert login_status == 200 and set_cookie
        cookie = set_cookie.split(";", 1)[0]
        csrf_status, csrf_response, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
        assert csrf_status == 200
        csrf = str(csrf_response["csrf_token"])
        incident_id = _bound_incident()
        report_url = f"{base_url}/api/v1/incidents/{incident_id}/report"

        unauthenticated, hidden, _ = _request(report_url)
        assert unauthenticated == 401 and hidden["error"]["code"] == "unauthorized"  # type: ignore[index]
        waiting_status, waiting, _ = _request(report_url, cookie=cookie)
        assert waiting_status == 200 and waiting["availability"] == "not_ready"
        _validate(spec, "IncidentReportResponse", waiting)

        with gateway_main._SESSIONS.database.connect() as conn:
            conn.execute("UPDATE investigations SET status = 'completed' WHERE incident_id = ?", (incident_id,))
            conn.execute(
                "UPDATE incidents SET status = 'resolved', lifecycle_state = 'resolved', resolved_at = updated_at + 1, updated_at = updated_at + 1, revision = revision + 1 WHERE id = ?",
                (incident_id,),
            )
        ready_status, ready, _ = _request(report_url, cookie=cookie)
        assert ready_status == 200 and ready["availability"] == "ready"
        assert ready["draft"]["facts"]["incident"]["status"] == "resolved"  # type: ignore[index]
        assert "session_id" not in json.dumps(ready) and "html" not in ready["draft"]  # type: ignore[operator]
        _validate(spec, "IncidentReportResponse", ready)

        narrative = {
            "impact": "Checkout unavailable for 12 minutes.",
            "root_cause": "Deployment worker saturation.",
            "resolution_summary": "Rolled back the Deployment.",
            "follow_up": "Add rollout saturation alerting.",
        }
        csrf_denied, denied, _ = _request(report_url, body=narrative, cookie=cookie, method="PATCH")
        assert csrf_denied == 403 and denied["error"]["code"] == "csrf_required"  # type: ignore[index]
        update_status, updated, _ = _request(report_url, body=narrative, cookie=cookie, csrf=csrf, method="PATCH")
        assert update_status == 200 and updated["draft"]["narrative"] == narrative  # type: ignore[index]
        _validate(spec, "IncidentReportDraftResponse", updated)

        publish_status, published, _ = _request(
            f"{report_url}/publish", body={}, cookie=cookie, csrf=csrf,
        )
        assert publish_status == 201 and published["publication"]["status"] == "published"  # type: ignore[index]
        _validate(spec, "IncidentReportPublicationResponse", published)
        edit_status, edit_error, _ = _request(report_url, body=narrative, cookie=cookie, csrf=csrf, method="PATCH")
        assert edit_status == 409 and edit_error["error"]["code"] == "report_published"  # type: ignore[index]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
