"""Native observability availability Adapter for Platform Status."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from urllib import parse, request

from apps.service_http import read_bounded_body



def read_status(
    _request_id: str,
    *,
    clock: Callable[[], float] = time.time,
    timeout_seconds: float = 2.5,
) -> dict[str, object]:
    prometheus_url = os.getenv("PROMETHEUS_URL", "").strip()
    loki_url = os.getenv("LOKI_URL", "").strip()
    configured = bool(prometheus_url and loki_url)
    if not configured:
        return {
            "configuration": "absent",
            "prometheus": {"state": "unavailable", "observed_at": None},
            "loki": {"state": "unavailable", "observed_at": None},
        }

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="observability-status")
    futures = {
        "prometheus": executor.submit(
            _prometheus_available, prometheus_url, timeout_seconds,
        ),
        "loki": executor.submit(_loki_available, loki_url, timeout_seconds),
    }
    completed, _ = wait(futures.values(), timeout=timeout_seconds)
    observed_at = clock()
    result: dict[str, object] = {"configuration": "present"}
    for name, future in futures.items():
        available = False
        if future in completed:
            try:
                available = bool(future.result())
            except Exception:
                pass
        result[name] = {
            "state": "available" if available else "unavailable",
            "observed_at": observed_at,
        }
    executor.shutdown(wait=False, cancel_futures=True)
    return result


def _prometheus_available(base_url: str, timeout_seconds: float) -> bool:
    query = parse.urlencode({"query": "up"})
    payload = _read(f"{base_url.rstrip('/')}/api/v1/query?{query}", timeout_seconds)
    value = json.loads(payload.decode() or "{}")
    return isinstance(value, dict) and value.get("status") == "success"


def _loki_available(base_url: str, timeout_seconds: float) -> bool:
    _read(f"{base_url.rstrip('/')}/ready", timeout_seconds)
    return True


def _read(url: str, timeout_seconds: float) -> bytes:
    outbound = request.Request(url, headers={"Accept": "application/json"})
    with request.urlopen(outbound, timeout=timeout_seconds) as response:
        return read_bounded_body(response)
