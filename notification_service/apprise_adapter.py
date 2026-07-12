"""Apprise transport result normalization for Notification Delivery."""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any


JSON = dict[str, object]
_SEND_LOCK = threading.Lock()


def send(url: str, title: str, body: str, *, body_format: str = "text") -> bool:
    return bool(send_result(url, title, body, body_format=body_format)["ok"])


def send_result(
    url: str,
    title: str,
    body: str,
    *,
    body_format: str = "text",
    clock: Callable[[], float] = time.time,
) -> JSON:
    import apprise

    client = apprise.Apprise()
    if not client.add(url):
        raise ValueError("Apprise rejected destination configuration")
    response: dict[str, Any] = {}
    server = client.servers[0]
    plugin_module = importlib.import_module(type(server).__module__)
    requests_module = getattr(plugin_module, "requests", None)

    class RequestsProxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(requests_module, name)

        def post(self, *args: object, **kwargs: object) -> Any:
            result = requests_module.post(*args, **kwargs)
            response["value"] = result
            return result

    with _SEND_LOCK:
        if requests_module is not None:
            plugin_module.requests = RequestsProxy()
        try:
            ok = bool(client.notify(
                title=title, body=body, body_format=body_format,
                notify_type=apprise.NotifyType.INFO,
            ))
        finally:
            if requests_module is not None:
                plugin_module.requests = requests_module
    if ok:
        return {"ok": True}
    http_response = response.get("value")
    status = getattr(http_response, "status_code", None)
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    retryable = status is None or status in {408, 429} or status >= 500
    result: JSON = {
        "ok": False,
        "retryable": retryable,
        "error": f"Apprise transport returned HTTP {status}" if status is not None else "Apprise transport failed",
    }
    retry_after = _retry_after(getattr(http_response, "headers", {}).get("Retry-After"), now=clock()) if retryable and status is not None else None
    if retry_after is not None:
        result["retry_after"] = retry_after
    return result


def _retry_after(value: object, *, now: float) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None
