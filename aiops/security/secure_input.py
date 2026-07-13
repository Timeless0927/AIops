"""AES-GCM transport contract for opaque Secure Input placeholders."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


DEFAULT_CHANGE_KEY_PATH = Path("/var/run/secrets/aiops-change/key")
_PLACEHOLDER = re.compile(r"^\{\{secure-input:([A-Za-z0-9_-]{1,200})\}\}$")
_AAD_VERSION = "aiops-change-secure-input-v1"
_REF_FIELDS = {
    "id", "key_name", "placeholder", "sha256", "key_fingerprint", "nonce", "ciphertext",
}


class SecureInputCryptoError(ValueError):
    pass


def secure_input_placeholder(input_id: str) -> str:
    if not isinstance(input_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", input_id):
        raise SecureInputCryptoError("Secure Input id is invalid")
    return f"{{{{secure-input:{input_id}}}}}"


def secure_input_ids(value: object) -> list[str]:
    found: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, str):
            match = _PLACEHOLDER.fullmatch(item)
            if match is not None:
                found.add(match.group(1))
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return sorted(found)


def contains_secure_input_placeholder(value: object) -> bool:
    return bool(secure_input_ids(value))


def read_change_encryption_key(path: Path | str = DEFAULT_CHANGE_KEY_PATH) -> bytes:
    key_path = Path(path)
    try:
        value = key_path.read_bytes().strip()
    except OSError as exc:
        raise SecureInputCryptoError("change encryption key is unavailable") from exc
    try:
        decoded = base64.urlsafe_b64decode(value)
    except (ValueError, TypeError):
        decoded = b""
    key = decoded if len(decoded) == 32 else value
    if len(key) != 32:
        raise SecureInputCryptoError("change encryption key must contain 32 raw or base64-encoded bytes")
    return key


def key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()


def value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encrypt_secure_input(
    *, input_id: str, key_name: str, value: str, key: bytes, nonce: bytes,
) -> tuple[bytes, str, str]:
    digest = value_hash(value)
    fingerprint = key_fingerprint(key)
    encrypted = AESGCM(key).encrypt(
        nonce, value.encode("utf-8"), _aad(input_id, key_name, digest, fingerprint),
    )
    return encrypted, digest, fingerprint


def decrypt_secure_input_refs(
    refs: object, *, key_path: Path | str = DEFAULT_CHANGE_KEY_PATH,
) -> dict[str, str]:
    if not isinstance(refs, list):
        raise SecureInputCryptoError("encrypted Secure Input refs are invalid")
    if not refs:
        return {}
    key = read_change_encryption_key(key_path)
    fingerprint = key_fingerprint(key)
    values: dict[str, str] = {}
    for raw in refs:
        if not isinstance(raw, dict) or set(raw) != _REF_FIELDS:
            raise SecureInputCryptoError("encrypted Secure Input ref is invalid")
        input_id = str(raw["id"])
        placeholder = secure_input_placeholder(input_id)
        if raw["placeholder"] != placeholder or raw["key_fingerprint"] != fingerprint:
            raise SecureInputCryptoError("Secure Input key is unavailable")
        try:
            nonce = base64.urlsafe_b64decode(str(raw["nonce"]))
            ciphertext = base64.urlsafe_b64decode(str(raw["ciphertext"]))
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                _aad(input_id, str(raw["key_name"]), str(raw["sha256"]), fingerprint),
            ).decode("utf-8")
        except (ValueError, UnicodeDecodeError, InvalidTag) as exc:
            raise SecureInputCryptoError("Secure Input is unavailable") from exc
        if value_hash(plaintext) != raw["sha256"]:
            raise SecureInputCryptoError("Secure Input hash does not match")
        values[placeholder] = plaintext
    return values


def materialize_secure_input_placeholders(
    value: object,
    refs: object,
    *,
    key_path: Path | str = DEFAULT_CHANGE_KEY_PATH,
) -> tuple[object, dict[str, str]]:
    plaintext = decrypt_secure_input_refs(refs, key_path=key_path)

    def replace(item: object) -> object:
        if isinstance(item, str) and _PLACEHOLDER.fullmatch(item):
            if item not in plaintext:
                raise SecureInputCryptoError("Secure Input placeholder is unresolved")
            return plaintext[item]
        if isinstance(item, dict):
            return {key: replace(child) for key, child in item.items()}
        if isinstance(item, list):
            return [replace(child) for child in item]
        return copy.deepcopy(item)

    materialized = replace(value)
    unresolved = secure_input_ids(materialized)
    if unresolved:
        raise SecureInputCryptoError("Secure Input placeholder is unresolved")
    return materialized, plaintext


def public_secure_input_ref(ref: dict[str, object]) -> dict[str, str]:
    return {
        "id": str(ref["id"]),
        "key_name": str(ref["key_name"]),
        "placeholder": str(ref["placeholder"]),
        "sha256": str(ref["sha256"]),
    }


def public_secure_input_facts(refs: object) -> list[dict[str, str]]:
    if not isinstance(refs, list):
        return []
    return [
        {"key_name": str(ref["key_name"]), "sha256": str(ref["sha256"])}
        for ref in refs
        if isinstance(ref, dict) and "key_name" in ref and "sha256" in ref
    ]


def redact_materialized_secure_values(
    value: object,
    plaintext_by_placeholder: dict[str, str],
    *,
    public: bool,
    refs: list[dict[str, object]],
) -> object:
    replacement_by_value: dict[str, object] = {}
    refs_by_placeholder = {str(ref["placeholder"]): ref for ref in refs}
    for placeholder, plaintext in plaintext_by_placeholder.items():
        ref = refs_by_placeholder[placeholder]
        replacement = (
            {"secure_input": {"key_name": str(ref["key_name"]), "sha256": str(ref["sha256"])}}
            if public else placeholder
        )
        replacement_by_value[plaintext] = replacement
        replacement_by_value[base64.b64encode(plaintext.encode("utf-8")).decode("ascii")] = replacement

    def replace(item: object) -> object:
        if isinstance(item, str) and item in replacement_by_value:
            return copy.deepcopy(replacement_by_value[item])
        if isinstance(item, dict):
            return {key: replace(child) for key, child in item.items()}
        if isinstance(item, list):
            return [replace(child) for child in item]
        return copy.deepcopy(item)

    return replace(value)


def _aad(input_id: str, key_name: str, digest: str, fingerprint: str) -> bytes:
    return json.dumps(
        [_AAD_VERSION, input_id, key_name, digest, fingerprint], separators=(",", ":"),
    ).encode("utf-8")
