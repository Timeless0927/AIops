"""Fail-closed redaction for acceptance evidence."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from typing import Any


REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:authorization|auth|cookie|credential|password|secret|token|api_key|"
    r"recipient|webhook_url|model_response|raw_reasoning|reasoning)(?:$|_)",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"(?im)^(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]*$"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|password|secret|token|credential|cookie)\s*([=:])\s*([^\s,;]+)"
)


def redact_text(value: str, *, known_secrets: Iterable[str] = ()) -> str:
    """Redact credential-shaped text and exact runtime secrets."""
    redacted = _HEADER.sub(lambda match: f"{match.group(1)}: {REDACTED}", value)
    redacted = _BEARER.sub(f"Bearer {REDACTED}", redacted)
    redacted = _ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", redacted
    )
    for secret in sorted({item for item in known_secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, REDACTED)
    return redacted


def redact_json(value: Any, *, known_secrets: Iterable[str] = ()) -> Any:
    """Return a detached JSON-compatible value with sensitive fields removed."""
    secrets = tuple(secret for secret in known_secrets if secret)

    def contains_text(item: Any) -> bool:
        if isinstance(item, str):
            return True
        if isinstance(item, Mapping):
            return any(contains_text(child) for child in item.values())
        if isinstance(item, (list, tuple)):
            return any(contains_text(child) for child in item)
        return False

    def visit(item: Any, key: str | None = None) -> Any:
        if key is not None and _SENSITIVE_KEY.search(key) and contains_text(item):
            return REDACTED
        if isinstance(item, Mapping):
            return {str(child_key): visit(child, str(child_key)) for child_key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, tuple):
            return [visit(child) for child in item]
        if isinstance(item, str):
            return redact_text(item, known_secrets=secrets)
        return copy.deepcopy(item)

    return visit(value)


def assert_secrets_absent(value: str, known_secrets: Iterable[str]) -> None:
    remaining = [secret for secret in known_secrets if secret and secret in value]
    if remaining:
        raise ValueError("redaction left a known secret in evidence")
