"""Gateway Notification Center tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway import notification_center as nc


ROOT = Path(__file__).resolve().parents[1]


class _FakeFeishuChannel:
    def __init__(self, responses: list[dict[str, object]] | None = None) -> None:
        self.responses = responses or [{"ok": True, "message_id": "om_default"}]
        self.calls: list[tuple[nc.NotificationTarget, dict[str, object]]] = []

    def send_card(self, target: nc.NotificationTarget, card: dict[str, object]) -> dict[str, object]:
        self.calls.append((target, card))
        if self.responses:
            return dict(self.responses.pop(0))
        return {"ok": True, "message_id": "om_default"}


def _settings() -> nc.NotificationSettings:
    return nc.NotificationSettings(
        console_base_url="https://console.example.test",
        max_attempts=3,
        retry_delay_seconds=0,
        channel_config={
            "default_team_id": "default",
            "teams": {
                "default": {"name": "Default SRE", "feishu_chat_id": "oc_default"},
                "payments": {"name": "Payments", "feishu_chat_id": "oc_payments"},
            },
            "services": {
                "checkout-api": {"team_id": "payments"},
                "billing-api": {"team_id": "payments", "feishu_chat_id": "oc_billing"},
            },
        },
    )


def _center(tmp_path: Path, channel: _FakeFeishuChannel | None = None) -> nc.NotificationCenter:
    settings = _settings()
    return nc.NotificationCenter(
        db=nc.NotificationDeliveryDB(tmp_path / "notification_deliveries.db"),
        channel=channel or _FakeFeishuChannel(),
        settings=settings,
    )


def _payload(notification_type: str = "approval_required") -> dict[str, object]:
    return {
        "notification_id": "ntf-1",
        "notification_type": notification_type,
        "incident_id": "inc-1",
        "approval_id": "ap-1",
        "summary": "需要审批重启 checkout-api",
        "context": {
            "service_id": "checkout-api",
            "severity": "critical",
            "risk_level": "high",
            "status": "pending",
        },
        "dedupe_key": f"{notification_type}:inc-1:ap-1",
    }


def _post(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            assert isinstance(data, dict)
            return int(response.status), data
    except urllib.error.HTTPError as exc:
        data = json.loads(exc.read().decode("utf-8"))
        assert isinstance(data, dict)
        return int(exc.code), data


def _get(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
        assert isinstance(data, dict)
        return data


def _wait_for_json(url: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _get(url)
        except Exception as exc:  # pragma: no cover - diagnostic wait loop
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"{url} did not become ready: {last_error}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_supported_notification_types_have_templates_without_feishu_approval_actions() -> None:
    catalog = nc.template_catalog(_settings())

    assert catalog["notification_types"] == list(nc.SUPPORTED_NOTIFICATION_TYPES)
    for notification_type in nc.SUPPORTED_NOTIFICATION_TYPES:
        card = catalog["templates"][notification_type]
        action = card["elements"][1]["actions"][0]
        assert action["url"].startswith("https://console.example.test")
        assert "url" in action
        assert "value" not in action
        assert json.dumps(card, ensure_ascii=False).find("approval_decision") == -1
        assert json.dumps(card, ensure_ascii=False).find("approved") == -1


def test_service_and_team_channel_resolution_uses_owner_mapping() -> None:
    settings = _settings()

    service_target = nc.resolve_notification_target(
        {
            "notification_type": "new_incident",
            "context": {"service_id": "billing-api"},
        },
        settings,
    )
    team_target = nc.resolve_notification_target(
        {
            "notification_type": "new_incident",
            "context": {"service_id": "checkout-api"},
        },
        settings,
    )

    assert service_target is not None
    assert service_target.chat_id == "oc_billing"
    assert service_target.team_id == "payments"
    assert service_target.reason == "service_channel"
    assert team_target is not None
    assert team_target.chat_id == "oc_payments"
    assert team_target.team_id == "payments"
    assert team_target.reason == "team_channel"


def test_unowned_alert_uses_default_team_channel(tmp_path: Path) -> None:
    channel = _FakeFeishuChannel([{"ok": True, "message_id": "om_unowned"}])
    center = _center(tmp_path, channel)

    result = center.send_notification(
        {
            "notification_id": "ntf-unowned",
            "notification_type": "unowned_alert",
            "incident_id": "inc-unowned",
            "summary": "PodCrashLooping 未匹配服务归属",
            "context": {"severity": "warning"},
            "dedupe_key": "unowned:inc-unowned",
        }
    )

    assert result["ok"] is True
    assert result["target"]["chat_id"] == "oc_default"
    assert result["target"]["reason"] == "default_team_channel"
    assert result["delivery"]["delivery_status"] == "sent"
    assert result["delivery"]["target_message_id"] == "om_unowned"


def test_notification_delivery_record_example_and_idempotency(tmp_path: Path) -> None:
    channel = _FakeFeishuChannel([{"ok": True, "message_id": "om_approval"}])
    center = _center(tmp_path, channel)

    first = center.send_notification(_payload("approval_required"))
    second = center.send_notification(_payload("approval_required"))
    deliveries = center.list_deliveries()

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["idempotent"] is True
    assert len(channel.calls) == 1
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery["notification_type"] == "approval_required"
    assert delivery["incident_id"] == "inc-1"
    assert delivery["approval_id"] == "ap-1"
    assert delivery["service_id"] == "checkout-api"
    assert delivery["team_id"] == "payments"
    assert delivery["chat_id"] == "oc_payments"
    assert delivery["delivery_status"] == "sent"
    assert delivery["delivery_attempts"] == 0
    assert delivery["target_message_id"] == "om_approval"
    assert delivery["payload"]["notification_type"] == "approval_required"
    assert delivery["card"]["elements"][1]["actions"][0]["url"] == "https://console.example.test/approval-center/ap-1"


def test_approval_button_url_ignores_payload_external_url(tmp_path: Path) -> None:
    channel = _FakeFeishuChannel([{"ok": True, "message_id": "om_approval_safe_url"}])
    center = _center(tmp_path, channel)
    payload = _payload("approval_required")
    payload["console_url"] = "https://evil.example/approve?decision=approved"
    payload["url"] = "https://evil.example/reject"

    result = center.send_notification(payload)
    card = result["card"]
    serialized_card = json.dumps(card, ensure_ascii=False)

    assert result["ok"] is True
    assert card["elements"][1]["actions"][0]["url"] == "https://console.example.test/approval-center/ap-1"
    assert "evil.example" not in serialized_card
    assert channel.calls[0][1]["elements"][1]["actions"][0]["url"] == "https://console.example.test/approval-center/ap-1"


def test_failure_retry_records_attempts_and_dead_letter(tmp_path: Path) -> None:
    channel = _FakeFeishuChannel(
        [
            {"ok": False, "error": "timeout"},
            {"ok": False, "error": "still failing"},
            {"ok": True, "message_id": "om_after_retry"},
        ]
    )
    center = _center(tmp_path, channel)
    payload = _payload("diagnosis_ready")
    payload["dedupe_key"] = "diagnosis:inc-1"
    payload.pop("approval_id")

    first = center.send_notification(payload)
    due = center.db.list_due_retries()
    retry = center.retry_due_deliveries()

    assert first["ok"] is False
    assert first["delivery"]["delivery_status"] == "failed"
    assert first["delivery"]["delivery_attempts"] == 1
    assert first["delivery"]["last_delivery_error"] == "timeout"
    assert due[0]["id"] == first["delivery"]["id"]
    assert retry["deliveries"][0]["ok"] is False
    assert retry["deliveries"][0]["delivery"]["delivery_status"] == "failed"
    assert retry["deliveries"][0]["delivery"]["delivery_attempts"] == 2

    final = center.retry_delivery(first["delivery"]["id"])
    assert final["ok"] is True
    assert final["delivery"]["delivery_status"] == "sent"
    assert final["delivery"]["target_message_id"] == "om_after_retry"


def test_non_retryable_failure_dead_letters_without_due_retry(tmp_path: Path) -> None:
    channel = _FakeFeishuChannel([{"ok": False, "retryable": False, "error": "feishu bad request"}])
    center = _center(tmp_path, channel)
    payload = _payload("diagnosis_ready")
    payload["dedupe_key"] = "diagnosis:non-retryable"
    payload.pop("approval_id")

    result = center.send_notification(payload)

    assert result["ok"] is False
    assert result["delivery"]["delivery_status"] == "dead_letter"
    assert result["delivery"]["delivery_attempts"] == 1
    assert result["delivery"]["next_retry_at"] is None
    assert result["delivery"]["last_delivery_error"] == "feishu bad request"
    assert center.db.list_due_retries() == []
    assert center.retry_due_deliveries()["retried"] == 0


def test_suppressed_notification_records_status_without_send(tmp_path: Path) -> None:
    channel = _FakeFeishuChannel()
    center = _center(tmp_path, channel)
    payload = _payload("execution_result")
    payload["dedupe_key"] = "execution:inc-1"
    payload["suppress"] = True
    payload["suppress_reason"] = "maintenance_window"

    result = center.send_notification(payload)

    assert result["ok"] is True
    assert result["suppressed"] is True
    assert result["delivery"]["delivery_status"] == "suppressed"
    assert result["delivery"]["suppressed_reason"] == "maintenance_window"
    assert channel.calls == []


def test_http_send_returns_accepted_when_feishu_delivery_is_recorded_failed(tmp_path: Path) -> None:
    center = _center(
        tmp_path,
        _FakeFeishuChannel([{"ok": False, "error": "feishu temporary unavailable"}]),
    )
    original_center = nc._CENTER
    nc._CENTER = center
    try:
        status, result = nc.handle_send_http_request(_payload("new_incident"))
    finally:
        nc._CENTER = original_center

    assert status == 202
    assert result["ok"] is False
    assert result["delivery"]["delivery_status"] == "failed"
    assert result["delivery"]["last_delivery_error"] == "feishu temporary unavailable"


def test_notification_only_boundary_does_not_import_approval_mutators() -> None:
    source = Path("apps/aiops_k8s_gateway/notification_center.py").read_text(encoding="utf-8")

    assert "approval_reply" not in source
    assert "resolve_approval" not in source
    assert "resolve_external_approval" not in source
    assert "publish_approval_card" not in source
    assert "\"value\"" not in source


def test_gateway_http_notification_routes(tmp_path: Path) -> None:
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "AIOPS_DATA_DIR": str(tmp_path / "data"),
            "AIOPS_GATEWAY_HOST": "127.0.0.1",
            "AIOPS_GATEWAY_PORT": str(port),
            "AIOPS_NOTIFICATION_DRY_RUN": "true",
            "AIOPS_CONSOLE_BASE_URL": "https://console.example.test",
            "AIOPS_NOTIFICATION_CHANNELS_JSON": json.dumps(
                {
                    "default_team_id": "default",
                    "teams": {"default": {"feishu_chat_id": "oc_default"}},
                    "services": {"checkout-api": {"team_id": "default"}},
                }
            ),
        }
    )
    gateway = subprocess.Popen(
        [sys.executable, "-m", "apps.aiops_k8s_gateway.main", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for_json(f"http://127.0.0.1:{port}/healthz")["status"] == "ok"
        status, sent = _post(
            f"http://127.0.0.1:{port}/notifications/send",
            _payload("new_incident"),
        )
        listed = _get(f"http://127.0.0.1:{port}/notifications/deliveries")
        types = _get(f"http://127.0.0.1:{port}/notifications/types")
    finally:
        gateway.terminate()
        try:
            gateway.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gateway.kill()

    assert status == 202
    assert sent["ok"] is True
    assert sent["delivery"]["delivery_status"] == "sent"
    assert sent["delivery"]["target_message_id"].startswith("dry-run-")
    assert listed["deliveries"][0]["id"] == sent["delivery"]["id"]
    assert "approval_required" in types["notification_types"]
