from __future__ import annotations

from pathlib import Path
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from aiops.contracts.notification import notification_request
from notification_service.presentation import feishu_signature, render_feishu_card
from notification_service.requests import NotificationRequestError, NotificationStore
from notification_service import service_main


def _request() -> dict[str, object]:
    return notification_request(
        event_id="incident.opened:incident-1:1",
        event_type="incident.opened",
        occurred_at=1_700_000_000,
        severity="critical",
        subject={"type": "incident", "id": "incident-1", "version": 1},
        scope={"environment": "prod", "cluster_id": "cluster-prod", "namespace": "payments"},
        summary="Checkout is unavailable",
        facts={"status": "opened", "reason": "health checks failed"},
        console_path="/incidents/incident-1",
    )


def test_engine_durably_accepts_exact_duplicates_before_delivery(tmp_path: Path) -> None:
    store = NotificationStore(
        tmp_path / "notification.db",
        clock=lambda: 1_700_000_001,
        console_base_url="https://console.example.test",
    )

    first = store.accept(_request())
    duplicate = store.accept(_request())

    assert first == {"status": "accepted", "event_id": _request()["event_id"], "duplicate": False}
    assert duplicate["duplicate"] is True
    assert store.get_request(str(_request()["event_id"]))["delivery_status"] == "pending"

    conflict = _request() | {"summary": "Different content"}
    with pytest.raises(NotificationRequestError, match="event_id conflict"):
        store.accept(conflict)


def test_fake_destination_delivery_is_async_and_uses_safe_feishu_presentation(tmp_path: Path) -> None:
    store = NotificationStore(
        tmp_path / "notification.db",
        clock=lambda: 1_700_000_001,
        console_base_url="https://console.example.test",
    )
    store.accept(_request())
    sent: list[dict[str, object]] = []

    assert store.run_delivery_once(lambda payload: sent.append(payload) or {"ok": True, "message_id": "fake-1"}) is True

    delivery = store.get_request(str(_request()["event_id"]))
    assert delivery["delivery_status"] == "sent"
    assert sent[0]["card"] == render_feishu_card(_request(), "https://console.example.test")
    assert sent[0]["destination"] == "builtin-fake"
    assert "approval_decision" not in str(sent[0])
    assert "session" not in str(sent[0]).lower()


def test_feishu_group_bot_signature_matches_documented_algorithm() -> None:
    assert feishu_signature("1700000000", "test-secret") == "mbm4Y4oluIPQ00qlBIhX8vAZ0EKv3nw0LuTb91jPL84="


def test_notification_engine_has_no_gateway_governance_dependency() -> None:
    source = Path("notification_service/requests.py").read_text(encoding="utf-8")
    assert "aiops_k8s_gateway" not in source
    assert "incident" not in source.lower().replace("incident.opened", "")
    assert "approval" not in source.lower()
    assert "execution_grant" not in source.lower()
    assert "connector_command" not in source.lower()


def test_authenticated_http_handoff_returns_202_after_durable_acceptance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    store = NotificationStore(tmp_path / "notification.db")
    monkeypatch.setattr(service_main, "_STORE", store)
    monkeypatch.setattr(service_main, "enforce_internal_auth", lambda *_args, **_kwargs: "gateway-identity")
    server = ThreadingHTTPServer(("127.0.0.1", 0), service_main.NotificationServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/notification-requests",
            data=json.dumps(_request()).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            body = json.loads(response.read())
        assert response.status == 202
        assert body["status"] == "accepted"
        assert store.get_request(str(_request()["event_id"]))["delivery_status"] == "pending"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
