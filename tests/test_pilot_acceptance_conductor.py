from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.conductor import AcceptanceConductor
from aiops.acceptance.evidence import AcceptanceEvidence
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION, GATE_SEQUENCE
from tests.pilot_acceptance_support import create_evidence


def _ledger(tmp_path: Path) -> AcceptanceEvidence:
    ids = count(1)
    return create_evidence(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-conductor",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-17T01:02:03Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
    )


def _passing(evidence: AcceptanceEvidence, gate_id: str):
    def command() -> dict[str, str]:
        started_at = evidence.start_gate(gate_id)
        evidence.record_gate(gate_id, "passed", [], started_at=started_at)
        return {"gate_id": gate_id, "status": "passed"}

    return command


def _commands(evidence: AcceptanceEvidence):
    return {gate_id: _passing(evidence, gate_id) for gate_id in GATE_SEQUENCE}


def test_status_is_read_only_and_advance_dispatches_only_the_frontier(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    commands = _commands(evidence)
    called: list[str] = []
    original = commands["P01"]
    commands["P01"] = lambda: (called.append("P01"), original())[1]
    conductor = AcceptanceConductor(evidence, advance_commands=commands)
    before = evidence.manifest_path.read_bytes()

    assert conductor.status() == {
        "status": "active/ready", "frontier": "P01", "open_gate": None,
    }
    assert evidence.manifest_path.read_bytes() == before
    assert conductor.advance() == {"gate_id": "P01", "status": "passed"}
    assert called == ["P01"]
    assert conductor.status()["frontier"] == "P02"


def test_resume_reconciles_only_the_open_gate_and_never_advances_next(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    commands = _commands(evidence)

    def pause() -> dict[str, str]:
        evidence.start_gate("P01")
        return {"gate_id": "P01", "status": "open"}

    def resume() -> dict[str, str]:
        execution = evidence.resume_gate("P01")
        evidence.record_gate("P01", "passed", [], started_at=execution.started_at)
        return {"gate_id": "P01", "status": "passed"}

    commands["P01"] = pause
    conductor = AcceptanceConductor(
        evidence, advance_commands=commands, resume_commands={"P01": resume},
    )

    conductor.advance()
    with pytest.raises(ValueError, match="use resume"):
        conductor.advance()
    assert conductor.resume() == {"gate_id": "P01", "status": "passed"}
    assert conductor.status()["frontier"] == "P02"


def test_resume_without_reconciliation_fails_closed_without_replay(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    conductor = AcceptanceConductor(evidence, advance_commands=_commands(evidence))

    assert conductor.resume() == {"gate_id": "P01", "status": "failed"}
    assert conductor.status() == {
        "status": "ineligible", "frontier": None, "open_gate": None,
    }
    failure = json.loads(
        next((evidence.root / "00-package/P01-attempt-1").glob("resume-failure.json")).read_text()
    )
    assert failure["effect_replayed"] is False


def test_conductor_rejects_incomplete_dispatch_or_a_command_that_runs_ahead(
    tmp_path: Path,
) -> None:
    evidence = _ledger(tmp_path)
    with pytest.raises(ValueError, match="no advance command"):
        AcceptanceConductor(evidence, advance_commands={}).advance()
    commands = _commands(evidence)

    def run_two() -> None:
        _passing(evidence, "P01")()
        _passing(evidence, "P02")()

    commands["P01"] = run_two
    with pytest.raises(RuntimeError, match="invalid number"):
        AcceptanceConductor(evidence, advance_commands=commands).advance()
