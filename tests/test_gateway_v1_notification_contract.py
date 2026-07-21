from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from aiops.contracts.notification import notification_request
from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import notification_admin_http
from notification_service import service_main as notification_main
from notification_service.configuration import NotificationConfiguration
from notification_service.delivery_sender import send_delivery
from notification_service.noise_controls import NotificationNoiseControls
from notification_service.requests import NotificationStore


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    request_id: str | None = None,
):
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if request_id:
        headers["X-Request-ID"] = request_id
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
    store = NotificationStore(tmp_path / "notification.db")
    monkeypatch.setattr(notification_main, "_CONFIGURATION", configuration)
    monkeypatch.setattr(notification_main, "_NOISE", noise)
    monkeypatch.setattr(notification_main, "_STORE", store)
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
            request_id="notification-create-1",
        )
        destination_id = created["destination"]["id"]
        replay_status, replayed, _ = _request(
            f"{base_url}/api/v1/admin/notification-destinations",
            method="POST",
            body={"name": "Primary Feishu", "provider": "feishu", "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"}, "reason": "configure notifications"},
            cookie=cookie,
            csrf=csrf,
            request_id="notification-create-1",
        )
        assert status == 201
        assert replay_status == 201
        assert replayed["destination"]["id"] == destination_id
        assert replayed["destination"]["configuration_revision"] == created["destination"]["configuration_revision"]
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

        revision = created["destination"]["configuration_revision"]
        test_status, started, _ = _request(
            f"{base_url}/api/v1/admin/notification-destinations/{destination_id}/test",
            method="POST",
            body={"expected_revision": revision, "reason": "verify destination"},
            cookie=cookie,
            csrf=csrf,
            request_id="notification-test-1",
        )
        assert sent == []
        store.run_delivery_once(lambda payload: send_delivery(configuration, payload))
        select_status, enabled, _ = _request(
            f"{base_url}/api/v1/admin/notification-destinations/{destination_id}/select-pilot-route",
            method="POST",
            body={"expected_revision": revision, "reason": "select Pilot catch-all"},
            cookie=cookie,
            csrf=csrf,
            request_id="notification-select-1",
        )
        public_status, public, _ = _request(f"{base_url}/api/v1/notification/status", cookie=cookie)
        stale_status, stale, _ = _request(
            f"{base_url}/api/v1/admin/notification-destinations/{destination_id}/test",
            method="POST",
            body={"expected_revision": "notification-destination-revision:stale", "reason": "stale browser"},
            cookie=cookie,
            csrf=csrf,
        )
        extra_status, extra, _ = _request(
            f"{base_url}/api/v1/admin/notification-destinations/{destination_id}/test",
            method="POST",
            body={"expected_revision": revision, "reason": "reject extras", "unexpected": True},
            cookie=cookie,
            csrf=csrf,
        )
        unauthenticated_status, _, _ = _request(f"{base_url}/api/v1/notification/status")
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

        assert test_status == 202
        assert select_status == public_status == simulation_status == noise_status == list_silence_status == 200
        assert copy_status == silence_status == 201
        assert edit_status == preview_status == template_enable_status == 200
        assert route_status == 201
        assert enabled["destination"]["enabled"] is True
        assert enabled["destination"]["readiness"] == "ready"
        assert started["verification"] == {
            "operation_id": "notification-delivery:notification-test-1",
            "delivery_id": "notification-delivery:notification-test-1",
            "revision": revision,
            "state": "verifying",
        }
        assert public["notification"]["readiness"] == "ready"
        assert "secret-token" not in json.dumps(public)
        assert stale_status == 409
        assert stale["error"]["code"] == "revision_conflict"
        assert extra_status == 400
        assert extra["error"]["code"] == "invalid_request"
        assert unauthenticated_status == 401
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
            router=lambda _request: {"route_id": None, "destination_ids": [destination_id], "suppressed_reason": None, "deliveries": [{"destination_id": destination_id, "destination_revision": revision, "template_id": None, "template_version": None, "presentation": None, "noise": {"result": "digest", "next_attempt_at": time.time() + 900, "reason": "digest interval 900 seconds"}}]},
        )
        query_request = sample | {
            "event_id": "incident.resolved:incident-1:2",
            "event_type": "incident.resolved",
            "severity": "warning",
            "subject": {"type": "incident", "id": "incident-1", "version": 2},
            "facts": {
                "incident_id": "incident-1", "status": "resolved",
                "recovery_observation_id": "recovery-1",
                "resolved_webhook_request_id": "resolved-webhook-1",
                "recovery_observed_at": 1_700_000_000,
                "stabilizes_at": 1_700_000_300,
                "resolved_at": 1_700_000_300,
            },
        }
        store.accept(query_request, request_id="gateway-handoff-incident-resolved-1")
        monkeypatch.setattr(notification_main, "_STORE", store)
        delivery_status, delivery_results, _ = _request(f"{base_url}/api/v1/admin/notification-deliveries", cookie=cookie)
        event_delivery_status, event_delivery_results, _ = _request(
            f"{base_url}/api/v1/admin/notification-deliveries/by-event/incident.resolved%3Aincident-1%3A2",
            cookie=cookie,
        )
        invalid_event_status, _, _ = _request(f"{base_url}/api/v1/admin/notification-deliveries/by-event/{'x' * 301}", cookie=cookie)
        assert delivery_status == 200
        assert event_delivery_status == 200
        assert invalid_event_status == 400
        assert delivery_results["deliveries"][0]["noise_result"] == "digest"
        assert delivery_results["deliveries"][0]["request"] == notification_request(**query_request)
        assert delivery_results["deliveries"][0]["request_id"]
        assert delivery_results["deliveries"][0]["request"]["subject"]["id"] == "incident-1"
        assert delivery_results["deliveries"][0]["destination_revision"] == revision
        assert delivery_results["deliveries"][0]["provider_identity"] is None
        assert event_delivery_results["deliveries"] == [
            item for item in delivery_results["deliveries"]
            if item["event_id"] == "incident.resolved:incident-1:2"
        ]

        dead_store = NotificationStore(
            tmp_path / "notification.db", max_attempts=1,
            router=lambda _request: {"route_id": None, "destination_ids": [destination_id], "suppressed_reason": None},
        )
        dead_store.accept(sample | {"event_id": "dead:1"})
        dead_store.run_delivery_once(lambda _payload: {"ok": False, "retryable": False, "error": "bad credential"})
        dead_letter = next(item for item in dead_store.list_delivery_results() if item["event_id"] == "dead:1")
        monkeypatch.setattr(notification_main, "_STORE", dead_store)
        redelivery_status, redelivery, _ = _request(
            f"{base_url}/api/v1/admin/notification-deliveries/{urllib.parse.quote(str(dead_letter['id']), safe='')}/redeliver",
            method="POST", body={"reason": "credential repaired"}, cookie=cookie, csrf=csrf,
        )
        assert redelivery_status == 200
        assert redelivery["delivery"]["status"] == "pending"
        assert redelivery["delivery"]["request"]["event_id"] == "dead:1"
        assert redelivery["delivery"]["provider_identity"] is None
        assert redelivery["delivery"]["attempts"][0]["id"].startswith(
            f"{dead_letter['id']}:"
        )
        assert redelivery["delivery"]["attempts"][0]["error"] == "Notification provider rejected the delivery"

        spec = json.loads(Path("api/openapi/gateway-v1.json").read_text())
        resolver = jsonschema.RefResolver.from_schema(spec)
        for schema_name, payload in (("NotificationDestinationResponse", created), ("NotificationVerificationResponse", started), ("NotificationStatusResponse", public), ("NotificationNoiseControlResponse", noise), ("NotificationSilenceResponse", silence), ("NotificationSilenceListResponse", listed_silences), ("NotificationDeliveryListResponse", delivery_results), ("NotificationDeliveryResponse", redelivery), ("NotificationTemplateResponse", copied), ("NotificationTemplatePreviewResponse", preview), ("NotificationRouteResponse", route), ("NotificationSimulationResponse", simulation)):
            jsonschema.Draft202012Validator(spec["components"]["schemas"][schema_name], resolver=resolver).validate(payload)
        _, audit, _ = _request(f"{base_url}/api/v1/admin/audit", cookie=cookie)
        assert "secret-token" not in json.dumps(audit)
        verification_audit = next(item for item in audit["audit"] if item["request_id"] == "notification-test-1")
        assert verification_audit["target_id"] == destination_id
        assert verification_audit["after"]["verification"]["operation_id"] == "notification-delivery:notification-test-1"
        selection_audit = next(item for item in audit["audit"] if item["action"] == "notification-destinations_select_pilot_route")
        assert selection_audit["target_id"] == destination_id
        assert selection_audit["after"]["destination"]["pilot_route_selected"] is True
        redelivery_audit = next(item for item in audit["audit"] if item["action"] == "notification-deliveries_redeliver")
        assert redelivery_audit["target_id"] == dead_letter["id"]
        audit_values = {
            "actor_id": "user:bootstrap-admin",
            "target_type": "notification-destinations",
            "target_id": destination_id,
            "action": "notification-destinations_update",
            "reason": "repair credential",
            "before": None,
            "after": None,
            "request_id": "notification-update-unknown",
        }
        gateway_main._SESSIONS.record_admin_audit(**audit_values, result="outcome_unknown")
        assert gateway_main._SESSIONS.unresolved_admin_request(
            "notification-destinations", destination_id, "notification-destinations_update",
        ) == "notification-update-unknown"
        gateway_main._SESSIONS.record_admin_audit(**audit_values, result="success")
        assert gateway_main._SESSIONS.unresolved_admin_request(
            "notification-destinations", destination_id, "notification-destinations_update",
        ) is None
    finally:
        gateway_server.shutdown()
        gateway_server.server_close()
        gateway_thread.join(timeout=2)
        notification_server.shutdown()
        notification_server.server_close()
        notification_thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
