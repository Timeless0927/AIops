from __future__ import annotations

import sys
from types import SimpleNamespace

from notification_service.apprise_adapter import send_result


def test_apprise_http_result_preserves_retry_classification_and_retry_after(monkeypatch) -> None:
    response = SimpleNamespace(status_code=429, headers={"Retry-After": "45"})
    requests = SimpleNamespace(post=lambda *_args, **_kwargs: response)
    plugin_module = SimpleNamespace(requests=requests)
    plugin_type = type("Plugin", (), {})
    plugin_type.__module__ = "test_apprise_plugin"

    class Client:
        servers = [plugin_type()]

        def add(self, _url): return True
        def notify(self, **_kwargs):
            return plugin_module.requests.post("https://provider.invalid").status_code == 200

    monkeypatch.setitem(sys.modules, "test_apprise_plugin", plugin_module)
    monkeypatch.setitem(sys.modules, "apprise", SimpleNamespace(Apprise=Client, NotifyType=SimpleNamespace(INFO="info")))

    assert send_result("feishu://token", "title", "body") == {
        "ok": False,
        "retryable": True,
        "retry_after": 45.0,
        "error": "Apprise transport returned HTTP 429",
    }

    response.headers = {"Retry-After": "Thu, 01 Jan 1970 00:01:00 GMT"}
    assert send_result("feishu://token", "title", "body", clock=lambda: 0)["retry_after"] == 60.0

    response.status_code = 503
    response.headers = {"Retry-After": "120"}
    assert send_result("feishu://token", "title", "body")["retry_after"] == 120.0

    response.status_code = 400
    response.headers = {}
    assert send_result("feishu://token", "title", "body") == {
        "ok": False,
        "retryable": False,
        "error": "Apprise transport returned HTTP 400",
    }
