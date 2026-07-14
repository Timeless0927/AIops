"""Shared fail-closed HTTP/evidence constraints for real integration gates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .evidence import AcceptanceEvidence, Artifact
from .evidence import GateFailed


def expect(response: Any, statuses: set[int]) -> Any:
    if response.status not in statuses:
        code = (
            response.body.get("error", {}).get("code")
            if isinstance(response.body, Mapping)
            else None
        )
        raise RuntimeError(
            f"Gateway returned HTTP {response.status} ({code or 'request_failed'})"
        )
    return response


def reauthenticate(session: Any, password: str, suffix: str) -> None:
    expect(
        session.request(
            "POST",
            "/auth/reauth",
            body={"password": password},
            request_id=f"acceptance-{suffix}-reauth",
        ),
        {200},
    )


def string_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(item for child in value.values() for item in string_values(child))
    if isinstance(value, (list, tuple)):
        return tuple(item for child in value for item in string_values(child))
    return ()


def fail_gate(
    evidence: AcceptanceEvidence,
    gate_id: str,
    artifacts: list[Artifact],
    error: Exception,
    secrets: tuple[str, ...],
    started_at: str,
) -> None:
    artifacts.append(
        evidence.write_json(
            gate_id,
            "failure.json",
            {"error_type": type(error).__name__, "message": str(error)},
            known_secrets=secrets,
        )
    )
    evidence.record_gate(
        gate_id, "failed", artifacts, started_at=started_at
    )
    raise GateFailed(f"{gate_id} failed: {error}") from error
