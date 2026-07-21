"""Single-frontier dispatch for the Acceptance Runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .evidence import AcceptanceEvidence


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
        try:
            command = self.advance_commands[gate_id]
        except KeyError as exc:
            raise ValueError(f"frontier {gate_id} has no advance command") from exc
        before = self.evidence.gate_attempt_count()
        result = command()
        self._require_attempt_delta(before, expected=1)
        return result

    def resume(self) -> Any:
        gate_id = self.evidence.status()["open_gate"]
        if gate_id is None:
            raise ValueError("acceptance has no open gate to resume")
        command = self.resume_commands.get(gate_id)
        if command is None:
            return self._fail_unresumable(gate_id)
        before = self.evidence.gate_attempt_count()
        result = command()
        self._require_attempt_delta(before, expected=0)
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

    def _require_attempt_delta(self, before: int, *, expected: int) -> None:
        if self.evidence.gate_attempt_count() - before != expected:
            raise RuntimeError("conductor invocation changed an invalid number of gate attempts")
