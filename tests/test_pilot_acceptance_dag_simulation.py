from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.conductor import AcceptanceConductor
from aiops.acceptance.evidence import AcceptanceEvidence, EvidenceError
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION, GATE_SEQUENCE
from aiops.acceptance.promotion import PromotionDecision, PromotionError, REQUIRED_ROLE_ATTESTATIONS
from tests.pilot_acceptance_support import create_evidence, open_evidence


def _ledger(tmp_path: Path) -> AcceptanceEvidence:
    ids = count(1)

    def verify(item) -> None:
        if item["signature"] != "valid-signature":
            raise ValueError("invalid signature")

    return create_evidence(
        tmp_path / "acceptance", acceptance_id="v0.1.0-dag-simulation",
        release_version="v0.1.0", release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION, kube_context="pilot-clean",
        cluster_identity_sha256="c" * 64, access_profile="http_nodeport",
        now=lambda: "2026-07-17T01:02:03Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
        attestation_verifier=verify,
    )


def _terminal(evidence: AcceptanceEvidence, gate_id: str, status: str = "passed"):
    def command() -> dict[str, str]:
        started_at = evidence.start_gate(gate_id)
        evidence.record_gate(gate_id, status, [], started_at=started_at)
        return {"gate_id": gate_id, "status": status}

    return command


def _commands(evidence: AcceptanceEvidence, failed_gate: str | None = None):
    return {
        gate_id: _terminal(
            evidence, gate_id, "failed" if gate_id == failed_gate else "passed",
        )
        for gate_id in GATE_SEQUENCE
    }


def _attest_required(evidence: AcceptanceEvidence) -> None:
    for gate_id, roles in REQUIRED_ROLE_ATTESTATIONS.items():
        for role in roles:
            statement = evidence.attestation_statement(
                actor=f"{role}@example.test", role=role, gate_ids=[gate_id],
                conclusion="passed", note=f"offline simulation {gate_id}:{role}",
            )
            evidence.append_attestation(
                statement, signature="valid-signature", public_key="public-key",
                fingerprint="SHA256:simulation",
            )


def test_complete_dag_evaluate_decide_and_seal(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    conductor = AcceptanceConductor(evidence, advance_commands=_commands(evidence))

    for gate_id in GATE_SEQUENCE:
        assert conductor.status()["frontier"] == gate_id
        assert conductor.advance()["gate_id"] == gate_id
    _attest_required(evidence)
    assert evidence.evaluate()["conclusion"] == "eligible"
    decision = PromotionDecision(evidence)
    statement = decision.statement(
        actor="release-owner@example.test", decision="promote", note="simulation passed",
    )
    with pytest.raises(PromotionError, match="signature"):
        decision.record(
            statement, signature="wrong", public_key="public-key",
            fingerprint="SHA256:simulation",
        )
    decision.record(
        statement, signature="valid-signature", public_key="public-key",
        fingerprint="SHA256:simulation",
    )
    checksum = evidence.seal()
    assert checksum.is_file()
    assert conductor.status()["status"] == "sealed"


@pytest.mark.parametrize("failed_gate", GATE_SEQUENCE)
def test_every_mandatory_gate_failure_terminalizes_the_simulation(
    tmp_path: Path, failed_gate: str,
) -> None:
    evidence = _ledger(tmp_path)
    conductor = AcceptanceConductor(
        evidence, advance_commands=_commands(evidence, failed_gate),
    )
    for gate_id in GATE_SEQUENCE[: GATE_SEQUENCE.index(failed_gate) + 1]:
        conductor.advance()
    assert conductor.status() == {
        "status": "ineligible", "frontier": None, "open_gate": None,
    }
    assert evidence.evaluate()["conclusion"] == "ineligible"
    decision = PromotionDecision(evidence)
    with pytest.raises(PromotionError, match="cannot be promoted"):
        statement = decision.statement(
            actor="owner", decision="promote", note="must fail",
        )
        decision.record(
            statement, signature="valid-signature", public_key="public-key",
            fingerprint="SHA256:simulation",
        )


def test_interruption_resumes_once_and_duplicate_effect_is_rejected(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)

    def pause() -> dict[str, str]:
        evidence.start_gate("P01")
        evidence.bind_operation("P01", kind="fixture", operation_id="p01/effect")
        return {"gate_id": "P01", "status": "open"}

    def resume() -> dict[str, str]:
        execution = evidence.resume_gate("P01")
        evidence.reconcile_operation(
            "P01", operation_id="p01/effect", outcome="succeeded",
            public_fact={"terminal": True},
        )
        evidence.record_gate("P01", "passed", [], started_at=execution.started_at)
        return {"gate_id": "P01", "status": "passed"}

    commands = _commands(evidence)
    commands["P01"] = pause
    conductor = AcceptanceConductor(
        evidence, advance_commands=commands, resume_commands={"P01": resume},
    )
    conductor.advance()
    with pytest.raises(EvidenceError, match="already bound"):
        evidence.bind_operation("P01", kind="fixture", operation_id="p01/effect")
    assert conductor.resume()["status"] == "passed"
    with pytest.raises(ValueError, match="no open gate"):
        conductor.resume()


def test_tamper_and_old_format_fail_closed(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    started_at = evidence.start_gate("P01")
    artifact = evidence.write_text("P01", "result.txt", "bounded")
    evidence.record_gate("P01", "passed", [artifact], started_at=started_at)
    artifact.path.write_text("tampered")
    with pytest.raises(EvidenceError, match="hash"):
        evidence.status()

    manifest = json.loads(evidence.manifest_path.read_text())
    manifest["format_version"] = 2
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="unsupported_evidence_format"):
        open_evidence(legacy)
