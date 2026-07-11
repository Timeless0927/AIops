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
from notification_service.requests import start_delivery_worker
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
        facts={"incident_id": "incident-1", "status": "opened"},
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
    assert sent[0]["timestamp"] == "1700000001"
    assert sent[0]["sign"] == feishu_signature("1700000001", "builtin-fake-signing-secret")
    assert "approval_decision" not in str(sent[0])
    assert "session" not in str(sent[0]).lower()


def test_feishu_group_bot_signature_matches_documented_algorithm() -> None:
    assert feishu_signature("1700000000", "test-secret") == "mbm4Y4oluIPQ00qlBIhX8vAZ0EKv3nw0LuTb91jPL84="


def test_delivery_lease_prevents_two_workers_from_sending_same_record(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "notification.db", clock=lambda: 1_700_000_001)
    store.accept(_request())
    nested_results: list[bool] = []

    def sender(_payload: dict[str, object]) -> dict[str, object]:
        nested_results.append(store.run_delivery_once(lambda _: {"ok": True}))
        return {"ok": True, "message_id": "fake-1"}

    assert store.run_delivery_once(sender) is True
    assert nested_results == [False]


def test_expired_worker_cannot_overwrite_reclaimed_delivery_result(tmp_path: Path) -> None:
    now = [1_700_000_001.0]
    store = NotificationStore(
        tmp_path / "notification.db",
        clock=lambda: now[0],
        delivery_lease_seconds=1,
    )
    store.accept(_request())

    def stale_sender(_payload: dict[str, object]) -> dict[str, object]:
        now[0] += 2
        assert store.run_delivery_once(lambda _: {"ok": True, "message_id": "new-worker"}) is True
        return {"ok": False, "error": "stale worker timeout"}

    assert store.run_delivery_once(stale_sender) is True
    delivery = store.get_request(str(_request()["event_id"]))
    assert delivery["delivery_status"] == "sent"
    assert delivery["message_id"] == "new-worker"


def test_delivery_worker_survives_one_iteration_failure() -> None:
    stop = threading.Event()

    class FlakyStore:
        calls = 0

        def run_delivery_once(self, _sender) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary sqlite failure")
            stop.set()
            return False

    store = FlakyStore()
    worker = start_delivery_worker(store, sender=lambda _: {"ok": True}, interval_seconds=0.01, stop_event=stop)  # type: ignore[arg-type]
    worker.join(timeout=1)

    assert store.calls == 2
    assert not worker.is_alive()


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
