from __future__ import annotations

import json
from datetime import datetime, timezone
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance import evaluator_successor
from aiops.acceptance.evidence import AcceptanceEvidence
from aiops.acceptance.evidence_files import sha256
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION, GATE_SEQUENCE
from aiops.acceptance.gate_reuse import reuse_gate
from aiops.acceptance.promotion import PromotionDecision
from tests.pilot_acceptance_support import create_evidence


NOW = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


def _source(tmp_path: Path) -> tuple[AcceptanceEvidence, Path]:
    ids = count(1)
    source = create_evidence(
        tmp_path / "source",
        acceptance_id="source-s01-evaluator",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-20T08:00:00Z",
        new_execution_id=lambda: f"source-execution-{next(ids)}",
        attestation_verifier=lambda _item: None,
    )
    reusable = {"P01", "I02", "I03", "I04"}
    for gate_id in GATE_SEQUENCE[: GATE_SEQUENCE.index("S01")]:
        started_at = source.start_gate(gate_id)
        artifacts = (
            [source.write_json(gate_id, "evidence.json", {"gate_id": gate_id})]
            if gate_id in reusable else []
        )
        if gate_id == "P01":
            execution_id = source.resume_gate(gate_id).execution_id
            source.reconcile_operation(
                gate_id,
                operation_id=execution_id,
                outcome="succeeded",
                public_fact={"effect_replayed": False},
            )
        source.record_gate(
            gate_id,
            "not_applicable" if gate_id == "I04" else "passed",
            artifacts,
            started_at=started_at,
        )
    source.start_gate("S01")
    source.record_gate("S01", "failed", [], failure_attribution="inconclusive")
    source.evaluate()
    decision = PromotionDecision(source)
    statement = decision.statement(
        actor="release-owner@example.test",
        decision="no_promote",
        note="S01 evaluator failed",
    )
    decision.record(
        statement,
        signature="valid-signature",
        public_key="ssh-ed25519 AAAATEST",
        fingerprint="SHA256:test",
    )
    source.seal()
    diagnostic = tmp_path / "diagnostic"
    diagnostic.mkdir()
    (diagnostic / "manifest.json").write_text(json.dumps({
        "source_acceptance_id": source._manifest["acceptance_id"],
        "source_failed_gate": "S01",
        "release_sha256": source.candidate_sha256,
        "acceptance_tool_sha256": source.acceptance_tool_sha256,
    }), encoding="utf-8")
    (diagnostic / "conclusion.json").write_text(json.dumps({
        "diagnosed_attribution": "tool_failure",
        "operation_accounting_complete": True,
        "recovered_operation_ids": [],
    }), encoding="utf-8")
    return source, diagnostic


def test_evaluator_successor_reuses_safe_predecessors_without_signature(tmp_path: Path) -> None:
    source, diagnostic = _source(tmp_path)
    tool = tmp_path / "corrected-tool.tar.gz"
    tool.write_bytes(b"corrected evaluator tool")
    bundle_path = evaluator_successor.create(
        source,
        diagnostic=diagnostic,
        acceptance_tool=tool,
        output=tmp_path / "successor.json",
        now=lambda: "2026-07-20T08:01:00Z",
    )
    bundle = evaluator_successor.inspect(bundle_path)
    target_ids = count(1)

    target = AcceptanceEvidence.create(
        tmp_path / "target",
        acceptance_id="target-s01-evaluator",
        release_version="v0.1.0",
        release_sha256=source.candidate_sha256,
        acceptance_tool_sha256=sha256(tool),
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context=source.kube_context,
        cluster_identity_sha256=source.cluster_identity_sha256,
        access_profile=source.access_profile,
        deployment_continuation=bundle,
        now=lambda: "2026-07-20T08:02:00Z",
        new_execution_id=lambda: f"target-execution-{next(target_ids)}",
        attestation_verifier=lambda _item: None,
    )
    reuse_gate(
        source=source,
        target=target,
        continuation=bundle,
        now=lambda: NOW,
        verifier=lambda _item: None,
    )
    for gate_id in ("P02", "I01"):
        target.start_gate(gate_id)
        target.record_gate(gate_id, "passed", [])
    for gate_id in ("I02", "I03", "I04"):
        reuse_gate(
            source=source,
            target=target,
            continuation=bundle,
            now=lambda: NOW,
            verifier=lambda _item: None,
        )

    assert target.frontier == "I05"
    assert "attestation" not in bundle
    assert bundle["record"]["source"]["diagnosed_failure_attribution"] == "tool_failure"


def test_evaluator_successor_rejects_tamper(tmp_path: Path) -> None:
    source, diagnostic = _source(tmp_path)
    tool = tmp_path / "corrected-tool.tar.gz"
    tool.write_bytes(b"corrected evaluator tool")
    path = evaluator_successor.create(
        source,
        diagnostic=diagnostic,
        acceptance_tool=tool,
        output=tmp_path / "successor.json",
    )
    value = json.loads(path.read_text())
    value["record"]["replacement"]["product_sha256"] = "d" * 64
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="bundle is invalid"):
        evaluator_successor.inspect(path)
