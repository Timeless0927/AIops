from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops.acceptance.evidence import A01_GATE_SEQUENCE, AcceptanceEvidence, EvidenceError
from tests.pilot_acceptance_support import create_evidence, open_evidence


def _evidence(tmp_path: Path) -> AcceptanceEvidence:
    evidence = create_evidence(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-evaluator-correction",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision="pilot-clean-acceptance-v4",
        kube_context="clean",
        cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-20T08:00:00Z",
    )
    for gate_id in A01_GATE_SEQUENCE[: A01_GATE_SEQUENCE.index("S01")]:
        evidence.start_gate(gate_id)
        evidence.record_gate(
            gate_id,
            "not_applicable" if gate_id == "I04" else "passed",
            [],
        )
    return evidence


def _status() -> dict:
    capability = {
        "configuration": "absent",
        "configuration_revision": None,
        "readiness": "not_ready",
        "setup_decision": "active",
        "verification": {"state": "not_applicable"},
        "availability": {"state": "unavailable"},
    }
    return {
        "capabilities": {
            "model": capability | {
                "configuration": "present",
                "configuration_revision": "model-provider:retained",
            },
            "notification": capability | {
                "readiness": "skipped",
                "setup_decision": "skipped",
            },
            "connector": capability,
            "observability": capability | {
                "configuration": "present",
                "readiness": "ready",
            },
        }
    }


def _fail_s01(evidence: AcceptanceEvidence, *, include_source: bool = True) -> None:
    started_at = evidence.start_gate("S01")
    artifacts = []
    if include_source:
        artifacts.append(evidence.write_json("S01", "platform-initial.json", _status()))
    artifacts.append(evidence.write_json(
        "S01", "failure.json", {"message": "old evaluator rejected retained state"}
    ))
    evidence.record_gate("S01", "failed", artifacts, started_at=started_at)


def _diagnostic(evidence: AcceptanceEvidence, tmp_path: Path) -> Path:
    root = tmp_path / "diagnostics" / "D-S01-evaluator"
    root.mkdir(parents=True)
    manifest = {
        "source_acceptance_id": evidence._manifest["acceptance_id"],
        "source_failed_gate": "S01",
        "release_sha256": evidence.candidate_sha256,
        "acceptance_tool_sha256": evidence.acceptance_tool_sha256,
    }
    conclusion = {
        "diagnosed_attribution": "tool_failure",
        "operation_accounting_complete": True,
        "recovered_operation_ids": [],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "conclusion.json").write_text(json.dumps(conclusion), encoding="utf-8")
    return root


def test_s01_evaluator_correction_preserves_failure_and_restores_frontier(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _fail_s01(evidence)
    diagnostic = _diagnostic(evidence, tmp_path)
    tool = tmp_path / "acceptance-tool.tar.gz"
    tool.write_bytes(b"corrected evaluator")

    correction = evidence.correct_s01(
        acceptance_tool=tool,
        diagnostic=diagnostic,
        reason="Accept retained owner states and do not replay an existing skip.",
    )

    assert correction["gate_id"] == "S01"
    assert evidence._manifest["gates"]["S01"][0]["status"] == "failed"
    assert evidence.status() == {
        "status": "active/ready",
        "frontier": "S02",
        "open_gate": None,
    }
    assert open_evidence(evidence.root).frontier == "S02"


def test_evaluator_correction_rejects_missing_source_artifact(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _fail_s01(evidence, include_source=False)
    diagnostic = _diagnostic(evidence, tmp_path)
    tool = tmp_path / "acceptance-tool.tar.gz"
    tool.write_bytes(b"corrected evaluator")

    with pytest.raises(ValueError, match="requires platform-initial.json"):
        evidence.correct_s01(
            acceptance_tool=tool,
            diagnostic=diagnostic,
            reason="No source fact means no correction.",
        )


def test_evaluator_correction_rejects_secret_shaped_reason(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _fail_s01(evidence)
    diagnostic = _diagnostic(evidence, tmp_path)
    tool = tmp_path / "acceptance-tool.tar.gz"
    tool.write_bytes(b"corrected evaluator")

    with pytest.raises(ValueError, match="reason is invalid"):
        evidence.correct_s01(
            acceptance_tool=tool,
            diagnostic=diagnostic,
            reason="api_key=must-not-enter-the-ledger",
        )

    assert evidence._manifest["evaluator_corrections"] == []


def test_evaluator_correction_rejects_evaluated_ledger(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _fail_s01(evidence)
    diagnostic = _diagnostic(evidence, tmp_path)
    evidence.evaluate()
    tool = tmp_path / "acceptance-tool.tar.gz"
    tool.write_bytes(b"corrected evaluator")

    with pytest.raises(EvidenceError, match="permanently read-only"):
        evidence.correct_s01(
            acceptance_tool=tool,
            diagnostic=diagnostic,
            reason="Evaluated ledgers stay immutable.",
        )
