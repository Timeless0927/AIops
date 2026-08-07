"""Shared redaction rules for Chat messages and attachment text."""

from __future__ import annotations

import re


SECURE_INPUT = re.compile(r"\{\{secure-input:[^}]+\}\}", re.IGNORECASE)
SECRET = re.compile(
    r"(?i)\b(?:(?:[\"']?(?:authorization|credential|password|secret|token|api[_-]?key)[\"']?)\s*[:=]\s*[\"']?(?:bearer\s+)?[^\s,;\"'}]+|bearer\s+[^\s,;]+)"
)
SENSITIVE_KEY = re.compile(r"authorization|credential|password|secret|token|api[_-]?key|secure[_-]?input|raw[_-]?payload", re.IGNORECASE)


def redact(value: str) -> str:
    return SECRET.sub("[REDACTED]", SECURE_INPUT.sub("[REDACTED]", value))


def contains_sensitive(value: str) -> bool:
    return bool(SECRET.search(value) or SECURE_INPUT.search(value) or re.search(
        r"(?is)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|-----BEGIN .*? CERTIFICATE-----",
        value,
    ))
