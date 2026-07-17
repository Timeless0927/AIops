"""Single-frontier dispatch for the Acceptance Runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .evidence import AcceptanceEvidence
from .gate_contract import GATE_SEQUENCE


GateCommand = Callable[[], Any]


class AcceptanceConductor:
    """Dispatch exactly the ledger-owned frontier; never own gate order."""

    def __init__(
        self,
        evidence: AcceptanceEvidence,
        *,
        advance_commands: Mapping[str, GateCommand],
        resume_commands: Mapping[str, GateCommand] | None = None,
    ) -> None:
        if set(advance_commands) != set(GATE_SEQUENCE):
            raise ValueError("advance commands must cover the canonical gate contract exactly")
        if any(gate_id not in GATE_SEQUENCE for gate_id in (resume_commands or {})):
            raise ValueError("resume commands contain an unknown gate")
        self.evidence = evidence
        self.advance_commands = dict(advance_commands)
        self.resume_commands = dict(resume_commands or {})

    def status(self) -> dict[str, Any]:
        return self.evidence.status()

    def advance(self) -> Any:
        status = self.evidence.status()
        gate_id = status["frontier"]
        if status["open_gate"] is not None:
            raise ValueError(f"{status['open_gate']} is open; use resume")
        if gate_id is None:
            raise ValueError("acceptance has no legal gate frontier")
        result = self.advance_commands[gate_id]()
        self._require_single_gate(gate_id)
        return result

    def resume(self) -> Any:
        gate_id = self.evidence.status()["open_gate"]
        if gate_id is None:
            raise ValueError("acceptance has no open gate to resume")
        command = self.resume_commands.get(gate_id)
        if command is None:
            return self._fail_unresumable(gate_id)
        result = command()
        self._require_single_gate(gate_id)
        return result

    def _fail_unresumable(self, gate_id: str) -> dict[str, str]:
        execution = self.evidence.resume_gate(gate_id)
        artifacts = list(execution.artifacts)
        artifacts.append(self.evidence.write_json(gate_id, "resume-failure.json", {
            "code": "interrupted_outcome_unprovable",
            "gate_id": gate_id,
            "effect_replayed": False,
        }))
        self.evidence.record_gate(
            gate_id, "failed", artifacts, started_at=execution.started_at,
        )
        return {"gate_id": gate_id, "status": "failed"}

    def _require_single_gate(self, gate_id: str) -> None:
        status = self.evidence.status()
        index = GATE_SEQUENCE.index(gate_id)
        next_gate = GATE_SEQUENCE[index + 1] if index + 1 < len(GATE_SEQUENCE) else None
        if (
            status["open_gate"] not in {None, gate_id}
            or (status["open_gate"] is None and status["frontier"] not in {None, next_gate})
        ):
            raise RuntimeError("one conductor invocation advanced more than one gate")
