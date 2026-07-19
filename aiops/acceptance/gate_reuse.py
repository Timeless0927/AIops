"""Gate-level evidence reuse and its source terminal facts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .gate_contract import GATE_SEQUENCE

if TYPE_CHECKING:
    from .evidence import AcceptanceEvidence


def completed_artifact_index(ledger: AcceptanceEvidence) -> list[dict[str, Any]]:
    """Return hash-verified terminal artifact facts in canonical gate order."""
    ledger._validate_loaded()
    return [
        {
            "gate_id": gate_id,
            "status": attempt["status"],
            "execution_id": attempt["execution_id"],
            "artifacts": [dict(item) for item in attempt["artifacts"]],
        }
        for gate_id in GATE_SEQUENCE
        for attempt in ledger._manifest["gates"].get(gate_id, [])
        if attempt["status"] != "open"
    ]
