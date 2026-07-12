"""Connector outbound Gateway client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict
from urllib.parse import urlparse

from .discovery import discover_candidates
from .stream_client import ConnectorRegistration


DISCOVERY_BATCH_SIZE = 1000


def request_context_headers(payload: dict[str, object]) -> dict[str, str]:
    request_id = str(payload.get("request_id") or f"req-{uuid.uuid4().hex}")
    correlation_id = str(
        payload.get("correlation_id")
        or payload.get("command_id")
        or payload.get("id")
        or payload.get("cluster_id")
        or request_id
    )
    return {"X-Request-ID": request_id, "X-Correlation-ID": correlation_id}


def sync_gateway_registration(
    gateway_url: str,
    registration: ConnectorRegistration,
    credential: str,
    *,
    allow_insecure: bool = False,
) -> bool:
    if not connector_gateway_url_is_secure(gateway_url, allow_insecure=allow_insecure) or not credential:
        return False
    if not _post_gateway(gateway_url, "/api/v1/connectors/register", asdict(registration), credential):
        return False
    heartbeat_ok = _post_gateway(
        gateway_url,
        "/api/v1/connectors/heartbeat",
        {"connector_id": registration.connector_id, "cluster_id": registration.cluster_id, "status": "online"},
        credential,
    )
    if not heartbeat_ok:
        return False
    candidates = discover_candidates(registration)
    if candidates is not None:
        for offset in range(0, len(candidates) or 1, DISCOVERY_BATCH_SIZE):
            if not _post_gateway(
                gateway_url,
                "/api/v1/connectors/discovery-candidates",
                {
                    "connector_id": registration.connector_id,
                    "cluster_id": registration.cluster_id,
                    "candidates": candidates[offset : offset + DISCOVERY_BATCH_SIZE],
                },
                credential,
            ):
                break
    return True


def connector_gateway_url_is_secure(gateway_url: str, *, allow_insecure: bool = False) -> bool:
    parsed = urlparse(gateway_url)
    return parsed.scheme == "https" or (
        parsed.scheme == "http"
        and (allow_insecure or parsed.hostname in {"127.0.0.1", "localhost", "::1"})
    )


def _post_gateway(gateway_url: str, path: str, payload: dict, credential: str) -> bool:
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {credential}",
            **request_context_headers(payload),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return 200 <= response.status < 300
    except (OSError, TimeoutError, urllib.error.URLError):
        return False
