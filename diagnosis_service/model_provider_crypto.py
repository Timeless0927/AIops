"""Credential encryption adapter for Model Provider revisions."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_AAD = b"aiops-model-provider-v1"


class CredentialCipher:
    def __init__(self, key: bytes, *, nonce_source: Callable[[int], bytes]) -> None:
        if len(key) != 32:
            raise ValueError("Model Provider encryption key must contain 32 bytes")
        self._key = key
        self._nonce_source = nonce_source

    def encrypt(self, api_key: str) -> str:
        nonce = self._nonce_source(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, api_key.encode(), _AAD)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode()

    def decrypt(self, encoded: str) -> str:
        raw = base64.urlsafe_b64decode(encoded)
        return AESGCM(self._key).decrypt(raw[:12], raw[12:], _AAD).decode()


def read_encryption_key(path: Path) -> bytes:
    value = path.read_bytes().strip()
    try:
        decoded = base64.urlsafe_b64decode(value)
    except ValueError:
        decoded = b""
    key = decoded if len(decoded) == 32 else value
    if len(key) != 32:
        raise ValueError("Model Provider encryption key must contain 32 bytes")
    return key
