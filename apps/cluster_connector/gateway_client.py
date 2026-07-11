"""Connector outbound Gateway client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict

from .discovery import discover_candidates
from .stream_client import ConnectorRegistration


DISCOVERY_BATCH_SIZE = 1000


def sync_gateway_registration(gateway_url: str, registration: ConnectorRegistration, credential: str) -> bool:
    if not gateway_url or not credential:
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


def _post_gateway(gateway_url: str, path: str, payload: dict, credential: str) -> bool:
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {credential}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return 200 <= response.status < 300
    except (OSError, TimeoutError, urllib.error.URLError):
        return False
