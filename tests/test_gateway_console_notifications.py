"""Console Next notification center route tests."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway import notification_center
from toolsets.incident_store import IncidentStore


class _FakeFeishuChannel:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def send_card(self, target, card):  # noqa: ANN001, ANN201
        self.calls.append((target.to_dict(), card))
        return self.responses.pop(0) if self.responses else {"ok": True, "message_id": "om-default"}


def _write_identity_config(path: Path) -> None:
    path.write_text(
        f"""
identity:
  store_path: "{path.with_name("identity.db")}"
  ldap:
    enabled: false
  users:
    - username: operator
      password: operator-pass
      display_name: Operator
      roles: [operator]
      scope:
        clusters: ["prod-a"]
        services: ["checkout"]
        teams: ["payments"]
        namespaces: ["default"]
    - username: outsider
      password: outsider-pass
      display_name: Outsider
      roles: [operator]
      scope:
        clusters: ["prod-b"]
        services: ["billing"]
        teams: ["finance"]
        namespaces: ["default"]
""",
        encoding="utf-8",
    )


def _request_json(url: str, *, body: dict | None = None, token: str | None = None, method: str = "GET") -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _request_text(url: str, *, token: str) -> tuple[int, str, str]:
    headers = {"Accept": "text/event-stream", "Authorization": f"Bearer {token}"}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")


def _login(base_url: str, username: str, password: str) -> str:
    status, payload = _request_json(f"{base_url}/auth/login", body={"username": username, "password": password}, method="POST")
    assert status == 200
    return str(payload["token"])


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "identity.yaml"
    data_dir = tmp_path / "data"
    _write_identity_config(config_path)
    monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(config_path))
    monkeypatch.setenv("AIOPS_DATA_DIR", str(data_dir))
    old_center = notification_center._CENTER
    old_audit_db = gateway_main.audit_log._DB
    old_incident_store = gateway_main.incident_store._STORE
    settings = notification_center.NotificationSettings(
        console_base_url="https://console.example.test",
        max_attempts=3,
        retry_delay_seconds=0,
        channel_config={
            "services": {"checkout": {"team_id": "payments", "chat_id": "oc_checkout"}},
            "teams": {"finance": {"chat_id": "oc_finance"}},
        },
        dry_run=False,
    )
    channel = _FakeFeishuChannel(
        [
            {"ok": True, "message_id": "om-visible"},
            {"ok": False, "retryable": True, "error": "Authorization token leaked"},
            {"ok": True, "message_id": "om-hidden"},
            {"ok": True, "message_id": "om-retry"},
        ]
    )
    notification_center._CENTER = notification_center.NotificationCenter(
        db=notification_center.NotificationDeliveryDB(data_dir / "notification_deliveries.db"),
        channel=channel,
        settings=settings,
    )
    gateway_main.audit_log._DB = gateway_main.audit_log.AuditLogDB(data_dir / "audit_log.db")
    gateway_main.incident_store._STORE = IncidentStore(data_dir / "incidents.db")
    gateway_main._SESSIONS.clear()
    gateway_main._ROUTES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", channel
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._SESSIONS.clear()
        gateway_main._ROUTES.clear()
        notification_center._CENTER.db.close()
        gateway_main.audit_log._DB.close()
        gateway_main.incident_store._STORE.close()
        notification_center._CENTER = old_center
        gateway_main.audit_log._DB = old_audit_db
        gateway_main.incident_store._STORE = old_incident_store


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "notification_type": "approval_required",
        "notification_id": "ntf-visible",
        "incident_id": "inc-visible",
        "approval_id": "ap-visible",
        "summary": "approval pending",
        "dedupe_key": "visible",
        "context": {
            "cluster": "prod-a",
            "namespace": "default",
            "service": "checkout",
            "team": "payments",
            "status": "pending",
        },
    }
    payload.update(overrides)
    return payload


def test_console_notifications_scope_stream_retry_and_sanitized_errors(gateway) -> None:  # noqa: ANN001
    base_url, channel = gateway
    incident_id = asyncio.run(
        gateway_main.incident_store.create_incident(
            "CheckoutDown",
            "default",
            "prod-a",
            "checkout degraded",
            service="checkout",
            team="payments",
        )
    )
    visible = _payload(incident_id=incident_id)
    first = notification_center.send_notification(visible)
    duplicate = notification_center.send_notification(visible)
    failed = notification_center.send_notification(
        _payload(
            notification_type="execution_result",
            notification_id="ntf-failed",
            approval_id="ap-failed",
            dedupe_key="failed",
            incident_id=incident_id,
            summary="execution notification failed",
        )
    )
    hidden = notification_center.send_notification(
        _payload(
            notification_id="ntf-hidden",
            incident_id=None,
            approval_id=None,
            service_id="billing",
            team_id="finance",
            dedupe_key="hidden",
            context={
                "cluster": "prod-b",
                "namespace": "default",
                "service": "billing",
                "team": "finance",
            },
        )
    )

    operator = _login(base_url, "operator", "operator-pass")
    outsider = _login(base_url, "outsider", "outsider-pass")
    unauth_status, unauth = _request_json(f"{base_url}/api/notifications")
    list_status, listed = _request_json(f"{base_url}/api/notifications", token=operator)
    stream_status, content_type, stream = _request_text(f"{base_url}/api/notifications/stream", token=operator)
    outsider_status, outsider_list = _request_json(f"{base_url}/api/notifications", token=outsider)
    retry_status, retry = _request_json(
        f"{base_url}/api/notifications/retry",
        body={"delivery_id": failed["delivery"]["id"]},
        token=operator,
        method="POST",
    )

    assert first["delivery"]["id"] == duplicate["delivery"]["id"]
    assert duplicate["idempotent"] is True
    assert hidden["delivery"]["delivery_status"] == "sent"
    assert channel.calls[0][1]["elements"][1]["actions"][0]["url"].startswith("https://console.example.test/")
    assert unauth_status == 401
    assert unauth["error"]["code"] == "unauthorized"
    assert list_status == 200
    assert {item["notification_id"] for item in listed["notifications"]} == {"ntf-visible", "ntf-failed"}
    failed_item = next(item for item in listed["notifications"] if item["notification_id"] == "ntf-failed")
    assert failed_item["delivery_status"] == "failed"
    assert failed_item["last_delivery_error"] == "[redacted]"
    assert stream_status == 200
    assert "text/event-stream" in content_type
    assert "ntf-visible" in stream
    assert "ntf-hidden" not in stream
    assert outsider_status == 200
    assert [item["notification_id"] for item in outsider_list["notifications"]] == ["ntf-hidden"]
    assert retry_status == 200
    assert retry["result"]["delivery"]["delivery_status"] == "sent"
    assert retry["result"]["delivery"]["target_message_id"] == "om-retry"
