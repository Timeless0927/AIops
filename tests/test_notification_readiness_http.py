from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from notification_service import service_main
from notification_service.configuration import NotificationConfiguration
from notification_service.noise_controls import NotificationNoiseControls
from notification_service.requests import NotificationRequestLifecycle


def test_destination_test_http_accepts_durable_delivery_without_sending_inline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    db_path = tmp_path / "notification.db"
    sent: list[str] = []
    noise = NotificationNoiseControls(db_path, clock=lambda: 1_700_000_000.0)
    configuration = NotificationConfiguration(
        db_path,
        key,
        noise,
        clock=lambda: 1_700_000_000.0,
        send=lambda url, *_args: not sent.append(url),
    )
    destination = configuration.create_destination({
        "name": "Pilot Feishu",
        "provider": "feishu",
        "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"},
    })
    store = NotificationRequestLifecycle(db_path, clock=lambda: 1_700_000_000.0)
    monkeypatch.setattr(service_main, "_CONFIGURATION", configuration)
    monkeypatch.setattr(service_main, "_NOISE", noise)
    monkeypatch.setattr(service_main, "_REQUESTS", store)
    monkeypatch.setattr(service_main, "enforce_internal_auth", lambda *_args, **_kwargs: "gateway")
    server = ThreadingHTTPServer(("127.0.0.1", 0), service_main.NotificationServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        operation_id = "notification-delivery:http-test-1"
        request = urllib.request.Request(
            f"{base_url}/admin/notification-destinations/{urllib.parse.quote(str(destination['id']), safe='')}/test",
            data=json.dumps({
                "expected_revision": destination["configuration_revision"],
                "operation_id": operation_id,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            started = json.loads(response.read())
        with urllib.request.urlopen(f"{base_url}/notification/status", timeout=3) as response:
            status = json.loads(response.read())

        assert response.status == 200
        assert started["verification"] == {
            "operation_id": operation_id,
            "delivery_id": operation_id,
            "revision": destination["configuration_revision"],
            "state": "verifying",
        }
        assert sent == []
        assert store.list_delivery_results()[0]["status"] == "pending"
        assert status["notification"]["readiness"] == "not_ready"
        assert "secret-token" not in json.dumps(status)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
