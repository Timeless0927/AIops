from __future__ import annotations

from datetime import datetime, timezone
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.deployment_continuation import (
    DeploymentContinuation,
    create_diagnostic_bundle,
)
from aiops.acceptance.evidence import AcceptanceEvidence, GATE_CONTRACT_REVISION
from aiops.acceptance.gate_contract import EVIDENCE_FORMAT_VERSION
from aiops.acceptance.promotion import PromotionDecision
from tests.pilot_acceptance_support import create_evidence


NOW = datetime(2026, 7, 19, 3, 0, tzinfo=timezone.utc)


def _verify(item: dict) -> None:
    if item.get("signature") != "valid-signature":
        raise ValueError("invalid signature")


def _source(
    tmp_path: Path, *, failure_attribution: str | None = None,
) -> tuple[AcceptanceEvidence, Path]:
    ids = count(1)
    evidence = create_evidence(
        tmp_path / "acceptance",
        acceptance_id="source-tool-failure",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-19T03:00:00Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
        attestation_verifier=_verify,
    )
    for gate_id in ("P01", "P02", "I01", "I02", "I03", "I04"):
        evidence.start_gate(gate_id)
        evidence.record_gate(
            gate_id, "not_applicable" if gate_id == "I04" else "passed", [],
        )
    evidence.start_gate("I05")
    evidence.bind_operation(
        "I05", kind="browser_mutation", operation_id="request-user-1",
    )
    evidence.record_gate(
        "I05", "failed", [], failure_attribution=failure_attribution,
    )
    diagnostic = create_diagnostic_bundle(
        evidence, tmp_path / "diagnostics", diagnostic_id="diag-i05",
    )
    evidence.evaluate()
    decision = PromotionDecision(evidence)
    statement = decision.statement(
        actor="release-owner@example.test", decision="no_promote",
        note="tool failure requires replacement evidence",
    )
    decision.record(
        statement, signature="valid-signature",
        public_key="ssh-ed25519 AAAATEST", fingerprint="SHA256:test",
    )
    evidence.seal()
    return evidence, diagnostic


def _reconciliation(**changes: object) -> dict:
    value = {
        "operation_id": "request-user-1",
        "request_id": "request-user-1",
        "object_identity": {"user.id": "user-1"},
        "revision_identity": {"user.updated_at": "2026-07-19T02:59:00Z"},
        "outcome": "succeeded",
        "unknown_side_effects": False,
        "irreversible_side_effects": False,
    }
    value.update(changes)
    return value


def _replacement(product: str = "a" * 64) -> dict[str, object]:
    return {
        "product_sha256": product,
        "acceptance_tool_sha256": "d" * 64,
        "gate_contract_revision": GATE_CONTRACT_REVISION,
        "evidence_format_version": EVIDENCE_FORMAT_VERSION,
    }


def test_tool_failure_creates_signed_epoch_without_inheriting_old_gates(
    tmp_path: Path,
) -> None:
    source, diagnostic = _source(tmp_path)
    owner = DeploymentContinuation(
        tmp_path / "epochs", now=lambda: NOW,
    )
    path = owner.create(
        epoch_id="epoch-i05", source=source, diagnostic=diagnostic,
        replacement=_replacement(), reconciliations=[_reconciliation()],
    )
    statement = owner.attestation_statement(
        path, actor="operator@example.test", note="reviewed exact deployment handoff",
    )
    owner.attach_attestation(path, {
        "statement": statement,
        "signature": "valid-signature",
        "public_key": "ssh-ed25519 AAAATEST",
        "fingerprint": "SHA256:test",
    })
    bundle = owner.inspect(
        path, require_signed=True, verifier=_verify, now=lambda: NOW,
    )

    replacement = AcceptanceEvidence.create(
        tmp_path / "replacement",
        acceptance_id="replacement-run",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="d" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        deployment_continuation=bundle,
        now=lambda: "2026-07-19T03:05:00Z",
        attestation_verifier=_verify,
    )
    assert replacement.deployment_mode == "adopt_existing"
    assert replacement.frontier == "P01"
    assert replacement.gate_attempt_count() == 0


@pytest.mark.parametrize(
    ("replacement", "reconciliation", "message"),
    [
        (_replacement("e" * 64), _reconciliation(), "product identity changed"),
        (_replacement(), _reconciliation(unknown_side_effects=True), "unknown side effects"),
        (_replacement(), _reconciliation(revision_identity={"fact": "value"}), "unprovable"),
        (_replacement(), None, "every issued mutation"),
    ],
)
def test_epoch_rejects_rebuild_required_conditions(
    tmp_path: Path,
    replacement: dict[str, object],
    reconciliation: dict | None,
    message: str,
) -> None:
    source, diagnostic = _source(tmp_path)
    with pytest.raises(ValueError, match=message):
        DeploymentContinuation(tmp_path / "epochs", now=lambda: NOW).create(
            epoch_id="epoch-rejected", source=source, diagnostic=diagnostic,
            replacement=replacement,
            reconciliations=[] if reconciliation is None else [reconciliation],
        )


def test_product_failure_cannot_be_reclassified_for_deployment_retention(
    tmp_path: Path,
) -> None:
    source, diagnostic = _source(
        tmp_path, failure_attribution="product_failure",
    )
    with pytest.raises(ValueError, match="Acceptance Runner failure"):
        DeploymentContinuation(tmp_path / "epochs", now=lambda: NOW).create(
            epoch_id="epoch-rejected", source=source, diagnostic=diagnostic,
            replacement=_replacement(), reconciliations=[_reconciliation()],
        )
