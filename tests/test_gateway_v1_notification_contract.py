from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import notification_admin_http
from notification_service import service_main as notification_main
from notification_service.configuration import NotificationConfiguration
from notification_service.noise_controls import NotificationNoiseControls
from notification_service.requests import NotificationStore


def _request(url: str, *, method: str = "GET", body: dict | None = None, cookie: str | None = None, csrf: str | None = None):
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get("Set-Cookie")


def _login(base_url: str) -> tuple[str, str]:
    status, _, set_cookie = _request(f"{base_url}/auth/login", method="POST", body={"username": "admin", "password": "admin-pass", "session_mode": "cookie"})
    assert status == 200 and set_cookie
    cookie = set_cookie.split(";", 1)[0]
    status, payload, _ = _request(f"{base_url}/auth/csrf", cookie=cookie)
    assert status == 200
    return cookie, payload["csrf_token"]


def test_notification_administration_contract_through_gateway(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.delenv("AIOPS_IDENTITY_CONFIG", raising=False)
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    sent: list[str] = []
    noise = NotificationNoiseControls(tmp_path / "notification.db")
    configuration = NotificationConfiguration(tmp_path / "notification.db", key, noise, send=lambda url, *_args: not sent.append(url))
    monkeypatch.setattr(notification_main, "_CONFIGURATION", configuration)
    monkeypatch.setattr(notification_main, "_NOISE", noise)
    monkeypatch.setattr(notification_main, "enforce_internal_auth", lambda *_args, **_kwargs: "gateway-identity")
    monkeypatch.setattr(notification_admin_http, "internal_auth_headers", lambda: {})
    gateway_main._SESSIONS.clear()

    notification_server = ThreadingHTTPServer(("127.0.0.1", 0), notification_main.NotificationServiceHandler)
    notification_thread = threading.Thread(target=notification_server.serve_forever, daemon=True)
    notification_thread.start()
    monkeypatch.setenv("AIOPS_NOTIFICATION_ENGINE_URL", f"http://127.0.0.1:{notification_server.server_address[1]}")
    gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    gateway_thread.start()
    base_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"

    try:
        cookie, csrf = _login(base_url)
        status, created, _ = _request(
            f"{base_url}/api/v1/admin/notification-destinations",
            method="POST",
            body={"name": "Primary Feishu", "provider": "feishu", "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"}, "reason": "configure notifications"},
            cookie=cookie,
            csrf=csrf,
        )
        destination_id = created["destination"]["id"]
        assert status == 201
        assert created["destination"]["config"]["webhook_url"] == "https://open.feishu.cn/***"
        assert "secret-token" not in json.dumps(created)

        noise_status, noise, _ = _request(
            f"{base_url}/api/v1/admin/notification-destinations/{destination_id}/noise-control",
            method="PATCH",
            body={"timezone": "Asia/Shanghai", "quiet_hours": {"start": "22:00", "end": "07:00"}, "hourly_limit": 20, "digest_interval_seconds": 900, "reason": "reduce overnight noise"},
            cookie=cookie,
            csrf=csrf,
        )
        silence_status, silence, _ = _request(
            f"{base_url}/api/v1/admin/notification-silences",
            method="POST",
            body={"match": {"environment": ["prod"], "service": ["service-checkout"]}, "expires_at": time.time() + 3600, "reason": "planned checkout maintenance"},
            cookie=cookie,
            csrf=csrf,
        )
        list_silence_status, listed_silences, _ = _request(f"{base_url}/api/v1/admin/notification-silences", cookie=cookie)

        test_status, _, _ = _request(f"{base_url}/api/v1/admin/notification-destinations/{destination_id}/test", method="POST", body={"reason": "verify destination"}, cookie=cookie, csrf=csrf)
        enable_status, enabled, _ = _request(f"{base_url}/api/v1/admin/notification-destinations/{destination_id}", method="PATCH", body={"enabled": True, "reason": "activate destination"}, cookie=cookie, csrf=csrf)
        _, listed_templates, _ = _request(f"{base_url}/api/v1/admin/notification-templates", cookie=cookie)
        builtin = next(item for item in listed_templates["templates"] if item["event_type"] == "incident.opened" and item["provider"] == "feishu")
        copy_status, copied, _ = _request(f"{base_url}/api/v1/admin/notification-templates", method="POST", body={"source_template_id": builtin["id"], "name": "Production incident", "reason": "customize incident presentation"}, cookie=cookie, csrf=csrf)
        template_id = copied["template"]["id"]
        edit_status, _, _ = _request(f"{base_url}/api/v1/admin/notification-templates/{template_id}", method="PATCH", body={"title": "Incident {{summary}}", "reason": "clarify title"}, cookie=cookie, csrf=csrf)
        sample = {"event_id": "preview:1", "event_type": "incident.opened", "occurred_at": 1_700_000_000, "severity": "critical", "subject": {"type": "incident", "id": "incident-1", "version": 1}, "scope": {"environment": "prod"}, "summary": "Checkout unavailable", "facts": {"incident_id": "incident-1", "status": "opened"}, "console_path": "/incidents/incident-1"}
        preview_status, preview, _ = _request(f"{base_url}/api/v1/admin/notification-templates/{template_id}/preview", method="POST", body={"request": sample, "reason": "verify rendering"}, cookie=cookie, csrf=csrf)
        template_enable_status, template_enabled, _ = _request(f"{base_url}/api/v1/admin/notification-templates/{template_id}", method="PATCH", body={"enabled": True, "reason": "activate template"}, cookie=cookie, csrf=csrf)
        route_status, route, _ = _request(f"{base_url}/api/v1/admin/notification-routes", method="POST", body={"name": "Production critical", "priority": 10, "enabled": True, "match": {"event": ["incident.opened"], "severity": ["critical"], "environment": ["prod"]}, "destination_ids": [destination_id, destination_id], "template_id": template_id, "reason": "route critical incidents"}, cookie=cookie, csrf=csrf)
        simulation_status, simulation, _ = _request(
            f"{base_url}/api/v1/admin/notification-routes/simulate",
            method="POST",
            body={"event_id": "simulation:1", "event_type": "incident.opened", "occurred_at": 1_700_000_000, "severity": "critical", "subject": {"type": "incident", "id": "incident-1", "version": 1}, "scope": {"environment": "prod"}, "summary": "simulation", "facts": {"incident_id": "incident-1", "status": "opened"}, "console_path": "/admin"},
            cookie=cookie,
            csrf=csrf,
        )

        assert test_status == enable_status == simulation_status == noise_status == list_silence_status == 200
        assert copy_status == silence_status == 201
        assert edit_status == preview_status == template_enable_status == 200
        assert route_status == 201
        assert enabled["destination"]["enabled"] is True
        assert sent == ["feishu://secret-token"]
        assert route["route"]["destination_ids"] == [destination_id]
        assert route["route"]["template_id"] == template_id
        assert preview["preview"]["title"] == "Incident Checkout unavailable"
        assert template_enabled["template"]["enabled"] is True
        assert simulation["simulation"]["route_name"] == "Production critical"
        assert simulation["simulation"]["destination_ids"] == [destination_id]
        assert noise["noise_control"]["timezone"] == "Asia/Shanghai"
        assert listed_silences["silences"] == [silence["silence"]]

        store = NotificationStore(
            tmp_path / "notification.db",
            router=lambda _request: {"route_id": None, "destination_ids": [destination_id], "suppressed_reason": None, "deliveries": [{"destination_id": destination_id, "template_id": None, "template_version": None, "presentation": None, "noise": {"result": "digest", "next_attempt_at": time.time() + 900, "reason": "digest interval 900 seconds"}}]},
        )
        query_request = sample | {"event_id": "query:1", "severity": "warning"}
        store.accept(query_request)
        monkeypatch.setattr(notification_main, "_STORE", store)
        delivery_status, delivery_results, _ = _request(f"{base_url}/api/v1/admin/notification-deliveries", cookie=cookie)
        event_delivery_status, event_delivery_results, _ = _request(f"{base_url}/api/v1/admin/notification-deliveries/by-event/query%3A1", cookie=cookie)
        invalid_event_status, _, _ = _request(f"{base_url}/api/v1/admin/notification-deliveries/by-event/{'x' * 301}", cookie=cookie)
        assert delivery_status == 200
        assert event_delivery_status == 200
        assert invalid_event_status == 400
        assert delivery_results["deliveries"][0]["noise_result"] == "digest"
        assert event_delivery_results["deliveries"] == delivery_results["deliveries"]

        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        for schema_name, payload in (("NotificationDestinationResponse", created), ("NotificationNoiseControlResponse", noise), ("NotificationSilenceResponse", silence), ("NotificationSilenceListResponse", listed_silences), ("NotificationDeliveryListResponse", delivery_results), ("NotificationTemplateResponse", copied), ("NotificationTemplatePreviewResponse", preview), ("NotificationRouteResponse", route), ("NotificationSimulationResponse", simulation)):
            jsonschema.Draft202012Validator(spec["components"]["schemas"][schema_name], resolver=resolver).validate(payload)
        _, audit, _ = _request(f"{base_url}/api/v1/admin/audit", cookie=cookie)
        assert "secret-token" not in json.dumps(audit)
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        notification_server.shutdown()
        notification_server.server_close()
        notification_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
