"""Apprise transport result normalization for Notification Delivery."""

from __future__ import annotations

import importlib
import multiprocessing
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from multiprocessing.connection import Connection
from typing import Any


JSON = dict[str, object]
_ATTEMPT_TIMEOUT_SECONDS = 10.0
# ponytail: one disposable process per low-volume attempt; add a supervised pool only if startup cost is measured.
_PROCESS_CONTEXT = multiprocessing.get_context("spawn")


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
    request: JSON = {
        "url": url,
        "title": title,
        "body": body,
        "body_format": body_format,
        "now": clock(),
    }
    return _run_isolated(request, timeout_seconds=_ATTEMPT_TIMEOUT_SECONDS)


def _run_isolated(request: JSON, *, timeout_seconds: float) -> JSON:
    receiver, sender = _PROCESS_CONTEXT.Pipe(duplex=False)
    process = _PROCESS_CONTEXT.Process(
        target=_send_in_child,
        args=(request, sender),
        name="notification-provider-attempt",
    )
    try:
        process.start()
        sender.close()
        process.join(timeout_seconds)
        if process.is_alive():
            process.kill()
            process.join()
            return {
                "ok": False,
                "retryable": True,
                "error": "Apprise transport timed out",
                "reason_code": "timeout",
            }
        if not receiver.poll():
            raise RuntimeError("Apprise transport process exited without a result")
        envelope = receiver.recv()
    finally:
        receiver.close()
        sender.close()
        if process.exitcode is not None:
            process.close()
    if envelope.get("error_type") == "ValueError":
        raise ValueError(str(envelope["error"]))
    if envelope.get("error_type"):
        raise RuntimeError("Apprise transport failed")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Apprise transport returned an invalid result")
    return result


def _send_in_child(request: JSON, sender: Connection) -> None:
    try:
        result = _send_result_direct(
            str(request["url"]),
            str(request["title"]),
            str(request["body"]),
            body_format=str(request["body_format"]),
            now=float(request["now"]),
        )
        sender.send({"result": result})
    except Exception as exc:
        sender.send({
            "error_type": type(exc).__name__,
            "error": str(exc) if isinstance(exc, ValueError) else "Apprise transport failed",
        })
    finally:
        sender.close()


def _send_result_direct(
    url: str,
    title: str,
    body: str,
    *,
    body_format: str,
    now: float,
) -> JSON:
    import apprise

    client = apprise.Apprise()
    if not client.add(url):
        raise ValueError("Apprise rejected destination configuration")
    for configured_server in client.servers:
        configured_server.socket_connect_timeout = 5.0
        configured_server.socket_read_timeout = 5.0
    response: dict[str, Any] = {}
    server = client.servers[0]
    plugin_module = importlib.import_module(type(server).__module__)
    requests_module = getattr(plugin_module, "requests", None)
    smtplib_module = getattr(plugin_module, "smtplib", None)

    class RequestsProxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(requests_module, name)

        def post(self, *args: object, **kwargs: object) -> Any:
            result = requests_module.post(*args, **kwargs)
            response["value"] = result
            return result

    class SMTPClientProxy:
        def __init__(self, client: Any) -> None:
            self._client = client

        def __getattr__(self, name: str) -> Any:
            return getattr(self._client, name)

        def login(self, *args: object, **kwargs: object) -> Any:
            try:
                return self._client.login(*args, **kwargs)
            except smtplib_module.SMTPAuthenticationError:
                response["reason_code"] = "authentication_failed"
                raise

    class SmtplibProxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(smtplib_module, name)

        def SMTP(self, *args: object, **kwargs: object) -> SMTPClientProxy:  # noqa: N802
            return SMTPClientProxy(smtplib_module.SMTP(*args, **kwargs))

        def SMTP_SSL(self, *args: object, **kwargs: object) -> SMTPClientProxy:  # noqa: N802
            return SMTPClientProxy(smtplib_module.SMTP_SSL(*args, **kwargs))

    if requests_module is not None:
        plugin_module.requests = RequestsProxy()
    if smtplib_module is not None:
        plugin_module.smtplib = SmtplibProxy()
    try:
        ok = bool(client.notify(
            title=title, body=body, body_format=body_format,
            notify_type=apprise.NotifyType.INFO,
        ))
    finally:
        if requests_module is not None:
            plugin_module.requests = requests_module
        if smtplib_module is not None:
            plugin_module.smtplib = smtplib_module
    http_response = response.get("value")
    status = getattr(http_response, "status_code", None)
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    provider_rejected = bool(ok and status == 200 and _provider_rejected(http_response))
    if ok and not provider_rejected:
        return {"ok": True}
    reason_code = str(
        response.get("reason_code")
        or ("provider_rejected" if provider_rejected else _reason_code(status))
    )
    retryable = False if reason_code in {"authentication_failed", "provider_rejected"} else (
        status is None or status in {408, 429} or status >= 500
    )
    result: JSON = {
        "ok": False,
        "retryable": retryable,
        "error": (
            "Provider returned an error response"
            if provider_rejected
            else f"Apprise transport returned HTTP {status}"
            if status is not None
            else "Apprise transport failed"
        ),
        "reason_code": reason_code,
    }
    retry_after = _retry_after(getattr(http_response, "headers", {}).get("Retry-After"), now=now) if retryable and status is not None else None
    if retry_after is not None:
        result["retry_after"] = retry_after
    return result


def _provider_rejected(response: object) -> bool:
    try:
        payload = response.json()  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False
    return isinstance(payload, dict) and any(
        field in payload and payload[field] != 0 and payload[field] != "0"
        for field in ("errcode", "code")
    )


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


def _reason_code(status: int | None) -> str:
    if status in {401, 403}:
        return "authentication_failed"
    if status == 429:
        return "rate_limited"
    if status == 408:
        return "timeout"
    if status is not None and 400 <= status < 500:
        return "provider_rejected"
    return "provider_unavailable"
