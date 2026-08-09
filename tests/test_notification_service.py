from __future__ import annotations

from pathlib import Path
import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from aiops.contracts.notification import notification_request
from notification_service.presentation import feishu_signature, render_feishu_card
from notification_service.configuration import NotificationConfiguration, NotificationConfigurationError
from notification_service.delivery_sender import send_delivery
from notification_service.noise_controls import NotificationNoiseControls
from notification_service.requests import NotificationRequestError, NotificationRequestLifecycle
from notification_service.requests import start_delivery_worker
from notification_service import service_main
from notification_service import configuration_http


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
    store = NotificationRequestLifecycle(
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
    store = NotificationRequestLifecycle(
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
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: 1_700_000_001)
    store.accept(_request())
    nested_results: list[bool] = []

    def sender(_payload: dict[str, object]) -> dict[str, object]:
        nested_results.append(store.run_delivery_once(lambda _: {"ok": True}))
        return {"ok": True, "message_id": "fake-1"}

    assert store.run_delivery_once(sender) is True
    assert nested_results == [False]


def test_retryable_delivery_obeys_retry_after(tmp_path: Path) -> None:
    now = [1_700_000_001.0]
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: now[0])
    store.accept(_request())

    def rate_limited(_payload):
        now[0] += 10
        return {"ok": False, "retryable": True, "retry_after": 1, "error": "provider returned 429"}

    assert store.run_delivery_once(rate_limited) is True

    delivery = store.list_delivery_results()[0]
    assert delivery["status"] == "failed"
    assert delivery["attempt_count"] == 1
    assert delivery["next_attempt_at"] == now[0] + 1
    assert delivery["attempts"][0]["completed_at"] == now[0]
    assert store.run_delivery_once(lambda _payload: {"ok": True}) is False


def test_delivery_result_retains_request_and_provider_correlations(tmp_path: Path) -> None:
    store = NotificationRequestLifecycle(tmp_path / "notification.db")
    store.accept(_request(), request_id="gateway-outbox-request-1")
    assert store.run_delivery_once(
        lambda _payload: {"ok": True, "message_id": "provider-message-1"}
    ) is True

    result = store.list_delivery_results()[0]

    assert result["request_id"] == "gateway-outbox-request-1"
    assert result["request"] == _request()
    assert result["provider_identity"] == "provider-message-1"
    assert store.get_delivery_results(str(_request()["event_id"])) == [result]


def test_provider_success_without_identity_fails_closed(tmp_path: Path) -> None:
    store = NotificationRequestLifecycle(tmp_path / "notification.db", max_attempts=3)
    store.accept(_request(), request_id="gateway-outbox-request-1")

    assert store.run_delivery_once(lambda _payload: {"ok": True}) is True

    [result] = store.list_delivery_results()
    assert result["status"] == "dead_letter"
    assert result["provider_identity"] is None
    assert result["last_reason_code"] == "invalid_response"


def test_delivery_attempts_are_capped_at_three(tmp_path: Path) -> None:
    now = [1_700_000_001.0]
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: now[0], max_attempts=99)
    store.accept(_request(), request_id="gateway-outbox-request-1")
    failure = lambda _payload: {"ok": False, "retryable": True, "error": "provider unavailable"}

    for _ in range(3):
        assert store.run_delivery_once(failure)
        now[0] += 300

    delivery = store.list_delivery_results()[0]
    assert delivery["status"] == "dead_letter"
    assert delivery["attempt_count"] == 3


def test_non_retryable_delivery_enters_dead_letter_immediately(tmp_path: Path) -> None:
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: 1_700_000_001, max_attempts=5)
    store.accept(_request())

    assert store.run_delivery_once(
        lambda _payload: {"ok": False, "retryable": False, "error": "x" * 1000}
    ) is True

    delivery = store.list_delivery_results()[0]
    assert delivery["status"] == "dead_letter"
    assert delivery["attempt_count"] == 1
    assert delivery["next_attempt_at"] is None
    assert delivery["last_error"] == "Notification provider rejected the delivery"
    assert delivery["last_reason_code"] == "provider_rejected"


def test_dead_letter_can_be_redelivered_without_losing_failure_history(tmp_path: Path) -> None:
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: 1_700_000_001, max_attempts=1)
    store.accept(_request(), request_id="gateway-outbox-request-1")
    store.run_delivery_once(lambda _payload: {"ok": False, "retryable": True, "error": "timeout"})
    dead_letter = store.list_delivery_results()[0]

    redelivered = store.redeliver(str(dead_letter["id"]))

    assert redelivered["status"] == "pending"
    assert redelivered["attempt_count"] == 0
    assert redelivered["redelivery_count"] == 1
    assert redelivered["request_id"] == "gateway-outbox-request-1"
    assert redelivered["request"] == _request()
    assert redelivered["provider_identity"] is None
    assert redelivered["attempts"] == [{
        "id": f"{dead_letter['id']}:0:1",
        "attempt": 1,
        "redelivery": 0,
        "outcome": "dead_letter",
        "retryable": True,
        "error": "Notification provider is unavailable",
        "started_at": 1_700_000_001.0,
        "completed_at": 1_700_000_001.0,
    }]
    assert store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "redelivered"}) is True
    completed = store.list_delivery_results()[0]
    assert completed["status"] == "sent"
    assert [attempt["outcome"] for attempt in completed["attempts"]] == ["dead_letter", "sent"]


def test_cleanup_expires_only_terminal_notification_history_after_ninety_days(tmp_path: Path) -> None:
    now = [1_700_000_001.0]

    def route(request: dict[str, object]) -> dict[str, object]:
        pending = request["event_id"] == "incident.opened:incident-pending:1"
        return {
            "route_id": None,
            "destination_ids": ["builtin-fake"],
            "suppressed_reason": None,
            "deliveries": [{
                "destination_id": "builtin-fake",
                "template_id": None,
                "template_version": None,
                "presentation": {"title": "Frozen", "body": "Frozen presentation"},
                "noise": {
                    "result": "quiet_hours" if pending else "immediate",
                    "next_attempt_at": now[0] + 365 * 24 * 60 * 60 if pending else now[0],
                    "reason": "deferred" if pending else None,
                },
            }],
        }

    store = NotificationRequestLifecycle(
        tmp_path / "notification.db", clock=lambda: now[0], max_attempts=1, router=route
    )
    old_sent = _request()
    old_dead_letter = _request() | {
        "event_id": "incident.opened:incident-dead:1",
        "subject": {"type": "incident", "id": "incident-dead", "version": 1},
        "facts": {"incident_id": "incident-dead", "status": "opened"},
    }
    old_pending = _request() | {
        "event_id": "incident.opened:incident-pending:1",
        "subject": {"type": "incident", "id": "incident-pending", "version": 1},
        "facts": {"incident_id": "incident-pending", "status": "opened"},
    }
    store.accept(old_sent)
    store.run_delivery_once(lambda _: {"ok": True, "message_id": "sent-old"})
    store.accept(old_dead_letter)
    store.run_delivery_once(lambda _: {"ok": False, "retryable": False, "error": "bad destination"})
    store.accept(old_pending)

    now[0] += 90 * 24 * 60 * 60 + 1
    recent_sent = _request() | {
        "event_id": "incident.opened:incident-recent:1",
        "subject": {"type": "incident", "id": "incident-recent", "version": 1},
        "facts": {"incident_id": "incident-recent", "status": "opened"},
    }
    store.accept(recent_sent)
    store.run_delivery_once(lambda _: {"ok": True, "message_id": "sent-recent"})

    assert store.cleanup_expired() == 1
    with pytest.raises(NotificationRequestError, match="request not found"):
        store.get_request(str(old_sent["event_id"]))
    assert store.get_request(str(old_dead_letter["event_id"]))["delivery_status"] == "dead_letter"
    assert store.get_request(str(old_pending["event_id"]))["delivery_status"] == "pending"
    assert store.get_request(str(recent_sent["event_id"]))["delivery_status"] == "sent"
    assert {result["event_id"] for result in store.list_delivery_results()} == {
        old_dead_letter["event_id"], old_pending["event_id"], recent_sent["event_id"]
    }


def test_delivery_metrics_use_only_bounded_status_and_outcome_labels(tmp_path: Path) -> None:
    store = NotificationRequestLifecycle(tmp_path / "notification.db", max_attempts=1)
    store.accept(_request())
    store.run_delivery_once(lambda _payload: {"ok": False, "retryable": True, "error": "timeout"})

    metrics = store.metrics()

    assert 'aiops_notification_deliveries{status="dead_letter"} 1' in metrics
    assert 'aiops_notification_delivery_attempts{outcome="dead_letter"} 1' in metrics
    assert "aiops_notification_cleanup_eligible 0" in metrics
    assert str(_request()["event_id"]) not in metrics
    assert "builtin-fake" not in metrics


def test_delivery_logs_carry_request_and_correlation_ids_without_payload(tmp_path: Path, caplog) -> None:
    store = NotificationRequestLifecycle(tmp_path / "notification.db", max_attempts=1)
    with caplog.at_level("INFO", logger="notification_service.requests"):
        store.accept(_request(), request_id="req-1")
        store.run_delivery_once(lambda _payload: {"ok": False, "retryable": False, "error": "bad request"})

    entries = [json.loads(record.message) for record in caplog.records]
    assert entries[0] == {
        "service": "notification-engine", "event": "request_accepted", "request_id": "req-1",
        "correlation_id": _request()["event_id"], "duplicate": False,
    }
    assert entries[1]["event"] == "delivery_attempted"
    assert entries[1]["correlation_id"] == _request()["event_id"]
    assert "Checkout is unavailable" not in caplog.text


def test_expired_worker_cannot_overwrite_reclaimed_delivery_result(tmp_path: Path) -> None:
    now = [1_700_000_001.0]
    store = NotificationRequestLifecycle(
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


def test_expired_delivery_lease_is_recovered_after_store_restart(tmp_path: Path) -> None:
    now = [1_700_000_001.0]
    path = tmp_path / "notification.db"
    store = NotificationRequestLifecycle(path, clock=lambda: now[0], delivery_lease_seconds=1)
    store.accept(_request())

    with pytest.raises(KeyboardInterrupt):
        store.run_delivery_once(lambda _payload: (_ for _ in ()).throw(KeyboardInterrupt()))

    now[0] += 2
    reopened = NotificationRequestLifecycle(path, clock=lambda: now[0], delivery_lease_seconds=1)
    assert reopened.run_delivery_once(lambda _payload: {"ok": True, "message_id": "after-restart"}) is True
    assert reopened.get_request(str(_request()["event_id"]))["message_id"] == "after-restart"
    assert [attempt["outcome"] for attempt in reopened.list_delivery_results()[0]["attempts"]] == ["delivering", "sent"]


def test_delivery_worker_survives_one_iteration_failure() -> None:
    stop = threading.Event()

    class FlakyStore:
        calls = 0
        cleanup_calls = 0

        def cleanup_expired(self) -> None:
            self.cleanup_calls += 1

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
    assert store.cleanup_calls == 1
    assert not worker.is_alive()


def test_noise_result_is_durable_and_digest_sends_one_message_for_each_window(tmp_path: Path) -> None:
    now = [1_704_153_601.0]

    def route(request: dict[str, object]) -> dict[str, object]:
        return {
            "route_id": None,
            "destination_ids": ["destination:feishu"],
            "suppressed_reason": None,
            "deliveries": [{
                "destination_id": "destination:feishu",
                "template_id": None,
                "template_version": None,
                "presentation": {"title": str(request["summary"]), "body": str(request["summary"])},
                "noise": {"result": "digest", "next_attempt_at": 1_704_154_500.0, "reason": "digest interval 900 seconds"},
            }],
        }

    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: now[0], router=route)
    first = _request() | {"severity": "warning"}
    second = first | {"event_id": "incident.opened:incident-2:1", "summary": "Payments are slow", "subject": {"type": "incident", "id": "incident-2", "version": 1}, "facts": {"incident_id": "incident-2", "status": "opened"}}
    store.accept(first)
    store.accept(second)

    assert store.list_deliveries(str(first["event_id"]))[0]["noise_result"] == "digest"
    assert store.run_delivery_once(lambda _payload: {"ok": True}) is False
    now[0] = 1_704_154_500.0
    sent: list[dict[str, object]] = []
    assert store.run_delivery_once(lambda payload: sent.append(payload) or {"ok": True, "message_id": "digest-1"}) is True
    assert sent == [{
        "destination": "destination:feishu",
        "event_id": first["event_id"],
        "title": "2 AIOps notifications",
        "body": "Checkout is unavailable\nCheckout is unavailable\n\nPayments are slow\nPayments are slow",
        "digest_count": 2,
        "destination_revision": None,
        "is_test": False,
    }]
    assert store.get_request(str(first["event_id"]))["delivery_status"] == "sent"
    assert store.get_request(str(second["event_id"]))["delivery_status"] == "sent"


def test_notification_engine_has_no_gateway_governance_dependency() -> None:
    source = Path("notification_service/requests.py").read_text(encoding="utf-8")
    assert "aiops_k8s_gateway" not in source
    assert "incident" not in source.lower().replace("incident.opened", "")
    assert "approval" not in source.lower()
    assert "execution_grant" not in source.lower()
    assert "connector_command" not in source.lower()


def test_authenticated_http_handoff_returns_202_after_durable_acceptance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    store = NotificationRequestLifecycle(tmp_path / "notification.db")
    monkeypatch.setattr(service_main, "_REQUESTS", store)
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


def test_internal_admin_http_redelivers_dead_letter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    store = NotificationRequestLifecycle(tmp_path / "notification.db", max_attempts=1)
    store.accept(_request())
    store.run_delivery_once(lambda _payload: {"ok": False, "retryable": False, "error": "bad credential"})
    delivery_id = str(store.list_delivery_results()[0]["id"])
    monkeypatch.setattr(service_main, "_REQUESTS", store)
    monkeypatch.setattr(service_main, "enforce_internal_auth", lambda *_args, **_kwargs: "gateway-identity")
    server = ThreadingHTTPServer(("127.0.0.1", 0), service_main.NotificationServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/admin/notification-deliveries/{urllib.parse.quote(delivery_id, safe='')}/redeliver",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            body = json.loads(response.read())
        assert response.status == 200
        assert body["delivery"]["status"] == "pending"
        assert body["delivery"]["attempts"][0]["error"] == "Notification provider rejected the delivery"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_metrics_http_exposes_delivery_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    store = NotificationRequestLifecycle(tmp_path / "notification.db", max_attempts=1)
    store.accept(_request())
    store.run_delivery_once(lambda _payload: {"ok": False, "retryable": False, "error": "bad request"})
    monkeypatch.setattr(service_main, "_REQUESTS", store)
    server = ThreadingHTTPServer(("127.0.0.1", 0), service_main.NotificationServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/metrics", timeout=3) as response:
            metrics = response.read().decode()
        assert 'aiops_service_up{service="notification-engine"} 1' in metrics
        assert 'aiops_notification_deliveries{status="dead_letter"} 1' in metrics
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_internal_admin_http_returns_only_masked_destination_configuration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    noise = NotificationNoiseControls(tmp_path / "notification.db")
    configuration = NotificationConfiguration(tmp_path / "notification.db", key, noise, send=lambda *_args: True)
    monkeypatch.setattr(service_main, "_CONFIGURATION", configuration)
    monkeypatch.setattr(service_main, "enforce_internal_auth", lambda *_args, **_kwargs: "gateway-identity")
    server = ThreadingHTTPServer(("127.0.0.1", 0), service_main.NotificationServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/admin/notification-destinations",
            data=json.dumps({"name": "Feishu", "provider": "feishu", "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"}}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            created = json.loads(response.read())
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/admin/notification-destinations", timeout=3) as response:
            listed = json.loads(response.read())

        assert created["destination"]["config"]["webhook_url"] == "https://open.feishu.cn/***"
        assert "secret-token" not in json.dumps(created)
        assert listed == {"destinations": [created["destination"]]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_internal_template_http_lists_copies_and_previews_restricted_templates(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    noise = NotificationNoiseControls(tmp_path / "notification.db")
    configuration = NotificationConfiguration(tmp_path / "notification.db", key, noise, send=lambda *_args: True)

    class Handler:
        command = "GET"
        path = "/admin/notification-templates"
        payload: dict[str, object] = {}

        def read_json_body(self): return self.payload
        def write_json(self, status, payload): self.response = (status, payload)
        def write_not_found(self): raise AssertionError("unexpected not found")

    handler = Handler()
    store = NotificationRequestLifecycle(tmp_path / "notification.db")
    assert configuration_http.dispatch(handler, configuration, noise, store, lambda _handler: "gateway")
    source = next(item for item in handler.response[1]["templates"] if item["event_type"] == "incident.opened" and item["provider"] == "feishu")
    handler.command = "POST"
    handler.payload = {"source_template_id": source["id"], "name": "Feishu incident"}
    configuration_http.dispatch(handler, configuration, noise, store, lambda _handler: "gateway")
    template = handler.response[1]["template"]
    handler.path = f"/admin/notification-templates/{template['id']}/preview"
    handler.payload = {"request": _request()}
    configuration_http.dispatch(handler, configuration, noise, store, lambda _handler: "gateway")

    assert handler.response[1]["preview"]["title"] == "critical: Checkout is unavailable"
    assert "recipient" not in handler.response[1]


def test_provider_send_uses_frozen_smtp_subject_and_sanitized_html() -> None:
    sent: list[tuple[str, str, str, str]] = []

    class Configuration:
        db_path = Path("/data/aiops/notification.db")

        def delivery_result(self, destination_id, title, body, *, body_format="text", **_kwargs):
            sent.append((destination_id, title, body, body_format))
            return {"ok": True}

    result = send_delivery(
        Configuration(),
        {"destination": "destination:smtp", "title": "Card title", "body": "**raw**", "subject": "Mail subject", "html": "<p><strong>safe</strong></p>", "plain_text": "safe"}
    )

    assert result["ok"] is True
    assert sent == [("destination:smtp", "Mail subject", "<p><strong>safe</strong></p>", "html")]


def test_provider_send_normalizes_apprise_failures() -> None:
    class Configuration:
        db_path = Path("/data/aiops/notification.db")

        def delivery_result(self, *_args, **_kwargs):
            return {"ok": False, "retryable": True, "error": "Apprise transport failed"}

    payload = {"destination": "destination:feishu", "title": "Title", "body": "Body"}
    configuration = Configuration()
    assert send_delivery(configuration, payload) == {
        "ok": False, "retryable": True, "error": "Apprise transport failed",
    }

    Configuration.delivery_result = lambda self, *_args, **_kwargs: (_ for _ in ()).throw(NotificationConfigurationError("destination is unavailable"))
    assert send_delivery(configuration, payload) == {
        "ok": False, "retryable": False, "reason_code": "provider_rejected", "error": "destination is unavailable",
    }
