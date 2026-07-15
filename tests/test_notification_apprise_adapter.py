from __future__ import annotations

import multiprocessing
import sys
import time
from types import SimpleNamespace

from notification_service import apprise_adapter
from notification_service.apprise_adapter import send_result


def test_apprise_http_result_preserves_retry_classification_and_retry_after(monkeypatch) -> None:
    monkeypatch.setattr(apprise_adapter, "_PROCESS_CONTEXT", multiprocessing.get_context("fork"))
    response = SimpleNamespace(
        status_code=429,
        headers={"Retry-After": "45"},
        json=lambda: {"code": 19021},
    )
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
        "reason_code": "rate_limited",
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
        "reason_code": "provider_rejected",
    }


def test_apprise_rejects_http_200_provider_error_envelopes(monkeypatch) -> None:
    monkeypatch.setattr(apprise_adapter, "_PROCESS_CONTEXT", multiprocessing.get_context("fork"))
    payload = {"errcode": 40014, "errmsg": "invalid access token"}
    response = SimpleNamespace(status_code=200, headers={}, json=lambda: payload)
    requests = SimpleNamespace(post=lambda *_args, **_kwargs: response)
    plugin_module = SimpleNamespace(requests=requests)
    plugin_type = type("Plugin", (), {})
    plugin_type.__module__ = "test_apprise_provider_envelope"

    class Client:
        servers = [plugin_type()]

        def add(self, _url): return True
        def notify(self, **_kwargs):
            return plugin_module.requests.post("https://provider.invalid").status_code == 200

    monkeypatch.setitem(sys.modules, "test_apprise_provider_envelope", plugin_module)
    monkeypatch.setitem(
        sys.modules,
        "apprise",
        SimpleNamespace(Apprise=Client, NotifyType=SimpleNamespace(INFO="info")),
    )

    expected = {
        "ok": False,
        "retryable": False,
        "error": "Provider returned an error response",
        "reason_code": "provider_rejected",
    }
    assert send_result("dingtalk://secret@token", "title", "body") == expected
    payload.clear()
    payload.update({"code": 19021, "msg": "invalid webhook token"})
    assert send_result("feishu://token", "title", "body") == expected
    payload.clear()
    payload.update({"errcode": 0, "errmsg": "ok"})
    assert send_result("dingtalk://secret@token", "title", "body") == {"ok": True}
    payload.clear()
    payload.update({"code": 0, "msg": "success"})
    assert send_result("feishu://token", "title", "body") == {"ok": True}


def test_apprise_smtp_authentication_failure_is_deterministic(monkeypatch) -> None:
    monkeypatch.setattr(apprise_adapter, "_PROCESS_CONTEXT", multiprocessing.get_context("fork"))
    class SMTPException(Exception):
        pass

    class SMTPAuthenticationError(SMTPException):
        pass

    class Socket:
        def login(self, *_args):
            raise SMTPAuthenticationError("raw SMTP credential detail")

    smtplib = SimpleNamespace(
        SMTP=lambda *_args, **_kwargs: Socket(),
        SMTP_SSL=lambda *_args, **_kwargs: Socket(),
        SMTPException=SMTPException,
        SMTPAuthenticationError=SMTPAuthenticationError,
    )
    plugin_module = SimpleNamespace(smtplib=smtplib)
    plugin_type = type("Plugin", (), {})
    plugin_type.__module__ = "test_apprise_smtp_plugin"

    class Client:
        servers = [plugin_type()]

        def add(self, _url): return True
        def notify(self, **_kwargs):
            try:
                plugin_module.smtplib.SMTP().login("user", "password")
            except plugin_module.smtplib.SMTPException:
                return False
            return True

    monkeypatch.setitem(sys.modules, "test_apprise_smtp_plugin", plugin_module)
    monkeypatch.setitem(
        sys.modules,
        "apprise",
        SimpleNamespace(Apprise=Client, NotifyType=SimpleNamespace(INFO="info")),
    )

    assert send_result("mailto://user:password@example.test", "title", "body") == {
        "ok": False,
        "retryable": False,
        "error": "Apprise transport failed",
        "reason_code": "authentication_failed",
    }


def test_apprise_attempt_has_one_ten_second_wall_clock_deadline(monkeypatch) -> None:
    monkeypatch.setattr(apprise_adapter, "_PROCESS_CONTEXT", multiprocessing.get_context("fork"))
    sent = multiprocessing.Value("i", 0)
    plugin_module = SimpleNamespace()
    plugin_type = type("Plugin", (), {})
    plugin_type.__module__ = "test_apprise_timeout_plugin"

    class Client:
        servers = [plugin_type()]

        def add(self, _url): return True
        def notify(self, **_kwargs):
            time.sleep(0.2)
            with sent.get_lock():
                sent.value += 1
            return True

    monkeypatch.setitem(sys.modules, "test_apprise_timeout_plugin", plugin_module)
    monkeypatch.setitem(
        sys.modules,
        "apprise",
        SimpleNamespace(Apprise=Client, NotifyType=SimpleNamespace(INFO="info")),
    )
    assert apprise_adapter._ATTEMPT_TIMEOUT_SECONDS == 10.0
    monkeypatch.setattr(apprise_adapter, "_ATTEMPT_TIMEOUT_SECONDS", 0.01)

    assert send_result("feishu://token", "title", "body") == {
        "ok": False,
        "retryable": True,
        "error": "Apprise transport timed out",
        "reason_code": "timeout",
    }
    time.sleep(0.25)
    assert sent.value == 0
