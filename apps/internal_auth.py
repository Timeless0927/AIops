"""Kubernetes ServiceAccount authentication for internal HTTP routes."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


INTERNAL_AUDIENCE = "aiops-internal"
INTERNAL_TOKEN_FILE = "/var/run/secrets/aiops-internal/token"
TokenReviewer = Callable[[str, tuple[str, ...]], dict[str, object]]


@dataclass(frozen=True)
class AuthorizationResult:
    status: HTTPStatus
    identity: str | None = None
    reason: str | None = None


def authorize_request(
    headers: Mapping[str, str],
    *,
    namespace: str,
    allowed_service_account: str,
    review_token: TokenReviewer,
) -> AuthorizationResult:
    """Authenticate and authorize one internal HTTP request."""
    parts = headers.get("Authorization", "").split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return AuthorizationResult(HTTPStatus.UNAUTHORIZED, reason="missing_bearer_token")

    if not namespace:
        return AuthorizationResult(HTTPStatus.SERVICE_UNAVAILABLE, reason="service_namespace_unavailable")
    try:
        review = review_token(parts[1], (INTERNAL_AUDIENCE,))
    except Exception:
        return AuthorizationResult(HTTPStatus.SERVICE_UNAVAILABLE, reason="token_review_unavailable")
    identity = str(review.get("username") or "")
    expected = f"system:serviceaccount:{namespace}:{allowed_service_account}"
    audiences = review.get("audiences")
    if not review.get("authenticated") or not isinstance(audiences, (list, tuple)) or INTERNAL_AUDIENCE not in audiences:
        return AuthorizationResult(HTTPStatus.UNAUTHORIZED, identity=identity or None, reason="invalid_token")
    if identity != expected:
        return AuthorizationResult(HTTPStatus.FORBIDDEN, identity=identity, reason="service_account_forbidden")
    return AuthorizationResult(HTTPStatus.OK, identity=identity)


def internal_auth_headers(token_path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Read the rotating projected token immediately before an internal call."""
    path = Path(token_path or os.getenv("AIOPS_INTERNAL_TOKEN_FILE", INTERNAL_TOKEN_FILE))
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise OSError(f"projected service token is empty: {path}")
    return {"Authorization": f"Bearer {token}"}


@lru_cache(maxsize=1)
def _token_review_api() -> Any:
    from kubernetes import client, config

    config.load_incluster_config()
    return client.AuthenticationV1Api()


def review_service_account_token(token: str, audiences: tuple[str, ...]) -> dict[str, object]:
    """Authenticate a projected token with the Kubernetes TokenReview API."""
    from kubernetes import client

    response = _token_review_api().create_token_review(
        client.V1TokenReview(
            spec=client.V1TokenReviewSpec(token=token, audiences=list(audiences)),
        )
    )
    status = response.status
    user = status.user if status else None
    return {
        "authenticated": bool(status and status.authenticated),
        "audiences": list(status.audiences or ()) if status else [],
        "username": user.username if user else None,
    }


def enforce_internal_auth(
    handler: Any,
    *,
    service_name: str,
    allowed_service_account: str,
    namespace: str | None = None,
    review_token: TokenReviewer = review_service_account_token,
) -> str | None:
    """Enforce ServiceAccount authorization and emit a token-free denial audit."""
    result = authorize_request(
        handler.headers,
        namespace=namespace if namespace is not None else os.getenv("AIOPS_POD_NAMESPACE", ""),
        allowed_service_account=allowed_service_account,
        review_token=review_token,
    )
    if result.status == HTTPStatus.OK:
        return result.identity

    audit = {
        "event": "internal_auth_denied",
        "service": service_name,
        "path": urlparse(handler.path).path,
        "request_id": _request_id(handler),
        "correlation_id": _correlation_id(handler),
        "identity": result.identity,
        "reason": result.reason,
        "status": result.status.value,
    }
    sys.stdout.write(json.dumps(audit, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    response_status = {
        HTTPStatus.UNAUTHORIZED: "unauthorized",
        HTTPStatus.FORBIDDEN: "forbidden",
    }.get(result.status, "unavailable")
    handler.write_json(result.status, {"status": response_status, "reason": result.reason})
    return None


def _request_id(handler: Any) -> str | None:
    method = getattr(handler, "request_id", None)
    return method() if callable(method) else handler.headers.get("X-Request-ID")


def _correlation_id(handler: Any) -> str | None:
    method = getattr(handler, "correlation_id", None)
    if callable(method):
        return method()
    return handler.headers.get("X-Correlation-ID") or _request_id(handler)
