"""Signed human-attestation storage inside the Acceptance Evidence Module."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import yaml


MAX_ATTESTATION_BYTES = 1024 * 1024
_STATEMENT_FIELDS = {
    "acceptance_id", "candidate_sha256", "actor", "role", "gate_ids",
    "conclusion", "observed_at", "note",
}

def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(payload.get("attestations", []))


def matching(
    path: Path, gate_id: str, *, conclusion: str, role: str | None
) -> list[dict[str, Any]]:
    return [
        item for item in load(path)
        if gate_id in item.get("statement", {}).get("gate_ids", [])
        and item.get("statement", {}).get("conclusion") == conclusion
        and (role is None or item.get("statement", {}).get("role") == role)
    ]


def statement(
    *,
    acceptance_id: str,
    candidate_sha256: str,
    actor: str,
    role: str,
    gate_ids: Iterable[str],
    conclusion: Literal["passed", "failed"],
    observed_at: str,
    note: str,
) -> dict[str, Any]:
    return {
        "acceptance_id": acceptance_id,
        "candidate_sha256": candidate_sha256,
        "actor": actor,
        "role": role,
        "gate_ids": sorted(set(gate_ids)),
        "conclusion": conclusion,
        "observed_at": observed_at,
        "note": note,
    }


def append(
    path: Path,
    statement_value: dict[str, Any],
    *,
    signature: str,
    public_key: str,
    fingerprint: str,
    atomic_write: Callable[[Path, bytes], None],
) -> str:
    current = {"format_version": 2, "attestations": load(path)}
    current["attestations"].append({
        "statement": statement_value,
        "signature": signature,
        "public_key": public_key,
        "fingerprint": fingerprint,
    })
    encoded = yaml.safe_dump(current, sort_keys=False, allow_unicode=True).encode()
    if len(encoded) > MAX_ATTESTATION_BYTES:
        raise ValueError("human attestation file exceeds its bounded size")
    atomic_write(path, encoded)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validation_error(
    path: Path, *, acceptance_id: str, candidate_sha256: str, gate_ids: set[str]
) -> str | None:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format_version") != 2:
        return "human attestation file has an invalid format"
    items = payload.get("attestations")
    if not isinstance(items, list):
        return "human attestation list is invalid"
    for item in items:
        signed = item.get("statement") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not all(isinstance(item.get(key), str) and item[key] for key in ("signature", "public_key", "fingerprint"))
            or statement_error(
                signed,
                acceptance_id=acceptance_id,
                candidate_sha256=candidate_sha256,
                gate_ids=gate_ids,
            )
        ):
            return "human attestation statement is invalid"
    return None


def statement_error(
    value: Any, *, acceptance_id: str, candidate_sha256: str, gate_ids: set[str]
) -> bool:
    return (
        not isinstance(value, dict)
        or set(value) != _STATEMENT_FIELDS
        or value.get("acceptance_id") != acceptance_id
        or value.get("candidate_sha256") != candidate_sha256
        or value.get("conclusion") not in {"passed", "failed"}
        or not all(
            isinstance(value.get(field), str) and value[field]
            for field in ("actor", "role", "observed_at", "note")
        )
        or len(value["actor"]) > 256
        or len(value["role"]) > 64
        or len(value["observed_at"]) > 64
        or len(value["note"]) > 2048
        or not isinstance(value.get("gate_ids"), list)
        or not value["gate_ids"]
        or any(not isinstance(gate_id, str) for gate_id in value["gate_ids"])
        or any(gate_id not in gate_ids for gate_id in value["gate_ids"])
    )


def signature_identity_error(signature: Any, public_key: Any, fingerprint: Any) -> bool:
    return (
        not all(isinstance(value, str) and value for value in (signature, public_key, fingerprint))
        or len(signature) > 16 * 1024
        or len(public_key) > 4 * 1024
        or len(fingerprint) > 256
    )
