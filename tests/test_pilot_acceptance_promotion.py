from __future__ import annotations

import hashlib
import json
import stat
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.evidence_types import GateResult
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION, GATE_SEQUENCE
from aiops.acceptance.ledger import (
    REQUIRED_ROLE_ATTESTATIONS,
    AcceptanceLedger,
    EvidenceError,
)
from aiops.acceptance.promotion import PromotionDecision, PromotionError
from tests.pilot_acceptance_support import create_evidence


def _verify(item: dict) -> None:
    if item.get("signature") != "valid-signature":
        raise ValueError("invalid test signature")
    if item.get("fingerprint") != "SHA256:release-owner":
        raise ValueError("invalid test fingerprint")


def _ledger(tmp_path: Path) -> AcceptanceLedger:
    ids = count(1)
    return create_evidence(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-promotion",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-16T01:02:03Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
        attestation_verifier=_verify,
    )


def _complete(evidence: AcceptanceLedger, gate_id: str, status: str = "passed") -> None:
    evidence.start_gate(gate_id)
    evidence.record_gate(gate_id, GateResult(status, ()))  # type: ignore[arg-type]


def _complete_dag(evidence: AcceptanceLedger) -> None:
    for gate_id in GATE_SEQUENCE:
        _complete(
            evidence,
            gate_id,
            "not_applicable" if gate_id == "I04" else "passed",
        )


def _attest_required(evidence: AcceptanceLedger, *, signature: str = "valid-signature") -> None:
    for gate_id, roles in REQUIRED_ROLE_ATTESTATIONS.items():
        for role in roles:
            statement = evidence.attestation_statement(
                actor="same-person@example.test",
                role=role,
                gate_ids=[gate_id],
                conclusion="passed",
                note=f"{gate_id} checked as {role}",
            )
            evidence.append_attestation(
                statement,
                signature=signature,
                public_key="ssh-ed25519 AAAATEST",
                fingerprint="SHA256:release-owner",
            )


def _record_decision(
    evidence: AcceptanceLedger, decision: str = "promote", *, signature: str = "valid-signature"
) -> dict:
    owner = PromotionDecision(evidence)
    statement = owner.statement(
        actor="release-owner@example.test",
        decision=decision,  # type: ignore[arg-type]
        note="reviewed exact eligibility and candidate",
    )
    return owner.record(
        statement,
        signature=signature,
        public_key="ssh-ed25519 AAAATEST",
        fingerprint="SHA256:release-owner",
    )


def test_eligible_decision_and_seal_are_distinct_irreversible_facts(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    _complete_dag(evidence)
    _attest_required(evidence)

    eligibility = evidence.evaluate()
    assert eligibility == {
        "conclusion": "eligible",
        "reasons": [],
        "evaluated_at": "2026-07-16T01:02:03Z",
    }
    assert evidence.status()["status"] == "eligible"
    with pytest.raises(EvidenceError, match="permanently read-only"):
        evidence.append_attestation(
            evidence.attestation_statement(
                actor="late@example.test",
                role="sre",
                gate_ids=["C03"],
                conclusion="passed",
                note="late",
            ),
            signature="valid-signature",
            public_key="ssh-ed25519 AAAATEST",
            fingerprint="SHA256:release-owner",
        )

    decision = _record_decision(evidence)
    assert decision["statement"]["role"] == "release_owner"
    assert decision["statement"]["candidate_sha256"] == "a" * 64
    assert decision["statement"]["eligibility"] == "eligible"
    with pytest.raises(PromotionError, match="already exists"):
        _record_decision(evidence, "no_promote")

    checksum = evidence.seal()
    assert evidence.status()["status"] == "sealed"
    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["eligibility"] == eligibility
    assert manifest["promotion_decision"] == decision
    assert manifest["seal"]["checksum_path"] == "SHA256SUMS"
    indexed = dict(
        line.split("  ", 1)[::-1]
        for line in checksum.read_text(encoding="utf-8").splitlines()
    )
    assert indexed["manifest.json"] == hashlib.sha256(evidence.manifest_path.read_bytes()).hexdigest()
    assert "SHA256SUMS" not in indexed
    AcceptanceLedger.open(evidence.root, attestation_verifier=_verify)
    for path in [evidence.root, *evidence.root.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (
            0o555 if path.is_dir() else 0o444
        )
    evidence.manifest_path.chmod(0o644)
    with pytest.raises(EvidenceError, match="permanently read-only"):
        AcceptanceLedger.open(evidence.root, attestation_verifier=_verify)
    evidence.manifest_path.chmod(0o444)
    checksum.chmod(0o644)
    checksum.write_text(checksum.read_text() + f"{'0' * 64}  extra.txt\n")
    with pytest.raises(EvidenceError, match="checksum"):
        AcceptanceLedger.open(evidence.root, attestation_verifier=_verify)
    with pytest.raises(EvidenceError, match="read-only"):
        evidence.verify_identity(
            release_version="v0.1.0",
            release_sha256="d" * 64,
            acceptance_tool_sha256="c" * 64,
            gate_contract_revision=GATE_CONTRACT_REVISION,
            kube_context="pilot-clean",
            cluster_identity_sha256="b" * 64,
            access_profile="http_nodeport",
        )
    with pytest.raises(PromotionError, match="already sealed"):
        evidence.seal()


def test_failed_incomplete_matrix_is_ineligible_and_only_no_promote_can_be_signed(
    tmp_path: Path,
) -> None:
    evidence = _ledger(tmp_path)
    _complete(evidence, "P01", "failed")

    eligibility = evidence.evaluate()
    codes = {reason["code"] for reason in eligibility["reasons"]}
    assert eligibility["conclusion"] == "ineligible"
    assert {"mandatory_gates_missing", "mandatory_gates_failed"} <= codes
    with pytest.raises(PromotionError, match="cannot be promoted"):
        _record_decision(evidence, "promote")
    decision = _record_decision(evidence, "no_promote")
    assert decision["statement"]["decision"] == "no_promote"
    evidence.seal()


def test_evaluate_rejects_active_incomplete_matrix_and_is_single_use(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    with pytest.raises(PromotionError, match="C03 completion"):
        evidence.evaluate()
    _complete(evidence, "P01", "failed")
    evidence.evaluate()
    with pytest.raises(PromotionError, match="already been evaluated"):
        evidence.evaluate()


def test_missing_or_invalid_required_attestations_make_complete_dag_ineligible(
    tmp_path: Path,
) -> None:
    missing = _ledger(tmp_path / "missing")
    _complete_dag(missing)
    result = missing.evaluate()
    assert result["conclusion"] == "ineligible"
    assert "required_attestations_missing" in {item["code"] for item in result["reasons"]}

    invalid = _ledger(tmp_path / "invalid")
    _complete_dag(invalid)
    _attest_required(invalid, signature="wrong-signature")
    result = invalid.evaluate()
    assert result["conclusion"] == "ineligible"
    assert "attestation_signature_invalid" in {item["code"] for item in result["reasons"]}


def test_decision_requires_complete_identity_and_valid_signature(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    _complete(evidence, "P01", "failed")
    evidence.evaluate()
    owner = PromotionDecision(evidence)
    statement = owner.statement(
        actor="release-owner@example.test",
        decision="no_promote",
        note="candidate is ineligible",
    )
    with pytest.raises(PromotionError, match="signature identity"):
        owner.record(statement, signature="", public_key="", fingerprint="")
    with pytest.raises(PromotionError, match="signature is invalid"):
        owner.record(
            statement,
            signature="wrong-signature",
            public_key="ssh-ed25519 AAAATEST",
            fingerprint="SHA256:release-owner",
        )
    changed = dict(statement, candidate_sha256="d" * 64)
    with pytest.raises(PromotionError, match="acceptance identity"):
        owner.record(
            changed,
            signature="valid-signature",
            public_key="ssh-ed25519 AAAATEST",
            fingerprint="SHA256:release-owner",
        )
    assert "promotion_decision" not in json.loads(evidence.manifest_path.read_text())


def test_seal_reloads_manifest_and_reverifies_decision_signature(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    _complete(evidence, "P01", "failed")
    evidence.evaluate()
    _record_decision(evidence, "no_promote")
    manifest = json.loads(evidence.manifest_path.read_text())
    manifest["promotion_decision"]["signature"] = "wrong-signature"
    evidence.manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(EvidenceError, match="changed outside"):
        evidence.seal()
    with pytest.raises(EvidenceError, match="signature is invalid"):
        AcceptanceLedger.open(evidence.root, attestation_verifier=_verify)


def test_artifact_mutation_is_revalidated_as_ineligible(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    artifact = evidence.write_text("P01", "result.txt", "trusted")
    evidence.record_gate("P01", GateResult("passed", tuple([artifact])))
    for gate_id in GATE_SEQUENCE[1:]:
        _complete(
            evidence,
            gate_id,
            "not_applicable" if gate_id == "I04" else "passed",
        )
    _attest_required(evidence)
    artifact.path.write_text("tampered")

    result = evidence.evaluate()
    assert result["conclusion"] == "ineligible"
    assert result["reasons"][0] == {"code": "ledger_integrity_failed"}


def test_promotion_verifier_cannot_be_overridden_per_call(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)

    with pytest.raises(TypeError):
        PromotionDecision(evidence, lambda _: None)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        evidence.seal(lambda _: None)  # type: ignore[call-arg]
