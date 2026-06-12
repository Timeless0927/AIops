"""Shared HMAC contract for Gateway diagnosis writeback surfaces."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlparse


WRITEBACK_SIGNATURE_HEADER = "x-aiops-writeback-signature"
WRITEBACK_SECRET_ENV = "AIOPS_GATEWAY_WRITEBACK_SECRET"


def build_writeback_signature(secret: str, *, method: str, path: str, body: bytes = b"") -> str:
    """Build a stable HMAC signature for protected Gateway diagnosis routes."""
    digest = hmac.new(
        secret.encode("utf-8"),
        _signature_message(method=method, path=path, body=body),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_writeback_signature(
    secret: str,
    *,
    method: str,
    path: str,
    body: bytes,
    signature: str | None,
) -> bool:
    """Verify a Gateway diagnosis route signature."""
    if not signature:
        return False
    expected = build_writeback_signature(secret, method=method, path=path, body=body)
    return hmac.compare_digest(_normalize_signature(signature), expected)


def _normalize_signature(signature: str) -> str:
    value = signature.strip()
    if not value.startswith("sha256="):
        value = f"sha256={value}"
    return value


def _signature_message(*, method: str, path: str, body: bytes) -> bytes:
    route_path = urlparse(path).path
    return b"\n".join([method.upper().encode("ascii"), route_path.encode("utf-8"), body])
