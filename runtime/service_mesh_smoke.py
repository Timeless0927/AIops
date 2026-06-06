"""Compose smoke for gateway, Hermes, and connector split services."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _get_json(url: str, *, timeout: float = 2.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
        data = json.loads(payload or "{}")
        if not isinstance(data, dict):
            raise ValueError(f"{url} did not return a JSON object")
        return data


def _wait_json(url: str, *, deadline_seconds: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _get_json(url)
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"{url} did not become ready: {last_error}")


def _assert_ok(name: str, payload: dict[str, Any]) -> None:
    if payload.get("status") != "ok":
        raise AssertionError(f"{name} returned non-ok payload: {payload}")


def main() -> None:
    gateway_url = os.getenv("AIOPS_SMOKE_GATEWAY_URL", "http://gateway:8080").rstrip("/")
    hermes_url = os.getenv("AIOPS_SMOKE_HERMES_URL", "http://hermes:8082").rstrip("/")
    connector_url = os.getenv("AIOPS_SMOKE_CONNECTOR_URL", "http://connector:8081").rstrip("/")

    _assert_ok("gateway health", _wait_json(f"{gateway_url}/healthz"))
    _assert_ok("hermes health", _wait_json(f"{hermes_url}/healthz"))
    _assert_ok("connector health", _wait_json(f"{connector_url}/healthz"))
    _assert_ok("gateway to connector", _wait_json(f"{gateway_url}/connectivity/connector"))
    _assert_ok("hermes to gateway", _wait_json(f"{hermes_url}/connectivity/gateway"))

    print("service mesh smoke passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - process-level smoke failure output
        print(f"service mesh smoke failed: {exc}", file=sys.stderr)
        raise
