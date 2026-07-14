"""Bounded Job HTTP client for activating one controlled verification run."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from typing import BinaryIO

from verification_service.events import emit_event
from verification_service.fault import TriggerOutcome


MAX_ACK_BYTES = 1024


def trigger_job(timeout_seconds: float = 90) -> int:
    run_id = os.getenv("AIOPS_VERIFICATION_RUN_ID") or os.environ[
        "AIOPS_VERIFICATION_RUN_ID_LEGACY"
    ]
    service_host = os.getenv("VERIFICATION_API_SERVICE_HOST", "verification-api")
    endpoint = os.getenv(
        "AIOPS_VERIFICATION_TRIGGER_URL", f"http://{service_host}:8081/trigger"
    )
    ready_url = os.getenv(
        "AIOPS_VERIFICATION_READY_URL", f"http://{service_host}:8080/readyz"
    )
    payload = json.dumps({"run_id": run_id}, separators=(",", ":")).encode()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(ready_url, timeout=3):
                pass
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code != HTTPStatus.SERVICE_UNAVAILABLE:
                raise
        except OSError:
            time.sleep(1)
            continue
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                result = read_ack(response, run_id)
            if result["outcome"] not in {
                TriggerOutcome.ACTIVATED.value,
                TriggerOutcome.REPLAYED.value,
            }:
                raise RuntimeError("trigger endpoint returned an invalid outcome")
            emit_event("verification_trigger_job_succeeded", run_id)
            return 0
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == HTTPStatus.CONFLICT:
                emit_event("verification_trigger_job_conflict", run_id)
                return 2
            if exc.code < 500:
                raise
        except OSError:
            pass
        time.sleep(1)
    emit_event("verification_trigger_job_timeout", run_id)
    return 1


def read_ack(response: BinaryIO, expected_run_id: str) -> dict[str, str]:
    payload = response.read(MAX_ACK_BYTES + 1)
    if len(payload) > MAX_ACK_BYTES:
        raise RuntimeError("trigger acknowledgement is too large")
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("trigger acknowledgement is not valid JSON") from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"run_id", "outcome"}
        or result.get("run_id") != expected_run_id
        or not isinstance(result.get("outcome"), str)
    ):
        raise RuntimeError("trigger acknowledgement has an invalid shape")
    return result
