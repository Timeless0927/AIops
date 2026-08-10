from contextlib import contextmanager
from pathlib import Path

import pytest

from aiops.acceptance.evidence_types import Artifact, GateResult
from aiops.acceptance.gate_contract import EVIDENCE_FORMAT_VERSION, GATE_SEQUENCE
from aiops.acceptance.ledger import (
    REQUIRED_ROLE_ATTESTATIONS,
    AcceptanceLedger,
    EvidenceError,
)
from aiops.acceptance.promotion import PromotionDecision, PromotionError
from tests.pilot_acceptance_support import qualified_environment


class _MemoryEvidence:
    manifest_path = Path("/unused/manifest.json")
    attestation_path = Path("/unused/human-attestation.yaml")

    def __init__(
        self,
        attestations: list[dict] | None = None,
        *,
        artifact_matches: bool = True,
    ) -> None:
        self._attestations = attestations or []
        self._sealed = False
        self._artifact_matches = artifact_matches

    def persist(self, manifest: dict, *, locked: bool = False) -> None:
        pass

    def is_sealed(self, manifest: dict) -> bool:
        return self._sealed and manifest.get("seal") is not None

    def checksum_exists(self) -> bool:
        return self._sealed

    @property
    def checksum_name(self) -> str:
        return "SHA256SUMS"

    def materialize_seal(self) -> Path:
        self._sealed = True
        return Path("/unused/SHA256SUMS")

    def manifest_matches(self, manifest: dict) -> bool:
        return True

    def validate_artifact(self, *args, **kwargs) -> None:
        return None

    def artifact_exists(self, artifact: Artifact) -> bool:
        return True

    def artifact_matches(self, artifact: Artifact) -> bool:
        return self._artifact_matches

    def unindexed_file_error(self, seen_paths: set[str]) -> None:
        return None

    def attestation_exists(self) -> bool:
        return False

    def attestation_index_error(self, value: object) -> None:
        return None

    def seal_validation_error(self, value: object) -> None:
        return None

    def attestations(self) -> list[dict]:
        return self._attestations

    def artifact(self, gate_id: str, record: dict) -> Artifact:
        return Artifact(
            Path("/unused") / record["path"],
            record["path"],
            record["sha256"],
            record["bytes"],
            gate_id,
        )

    @contextmanager
    def unchanged(self):
        yield True


def _manifest() -> dict:
    precondition = qualified_environment(
        release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision="pilot-clean-acceptance-v4",
        kube_context="kind-aiops",
        cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
    )
    return {
        "format_version": EVIDENCE_FORMAT_VERSION,
        "acceptance_id": "acceptance-1",
        "created_at": "2026-07-01T00:00:00Z",
        "release": {"version": "v1", "sha256": "a" * 64},
        "acceptance_tool": {"sha256": "b" * 64},
        "gate_contract_revision": "pilot-clean-acceptance-v4",
        "cluster": {"kube_context": "kind-aiops", "identity_sha256": "c" * 64},
        "access_profile": "http_nodeport",
        "deployment_mode": "clean_install",
        "deployment_precondition": precondition,
        "deployment_precondition_sha256": precondition["bundle_sha256"],
        "gates": {},
        "evaluator_corrections": [],
        "identity_violations": [],
    }


def _ledger(
    manifest: dict | None = None,
    *,
    evidence: _MemoryEvidence | None = None,
) -> AcceptanceLedger:
    return AcceptanceLedger(
        root=Path("/unused"),
        manifest=manifest or _manifest(),
        now=lambda: "2026-07-01T00:00:00Z",
        new_execution_id=lambda: "execution-1",
        attestation_verifier=lambda _item: None,
        evidence=evidence or _MemoryEvidence(),
    )


def _terminal_attempt(gate_id: str, status: str = "passed") -> dict:
    attempt = {
        "attempt": 1,
        "execution_id": f"execution-{gate_id.lower()}",
        "status": status,
        "started_at": "2026-07-01T00:00:00Z",
        "completed_at": "2026-07-01T00:00:00Z",
        "operations": [
            {
                "kind": "gate_execution",
                "operation_id": f"execution-{gate_id.lower()}",
                "bound_at": "2026-07-01T00:00:00Z",
            }
        ],
        "reconciliations": [],
        "artifacts": [],
    }
    if status == "failed":
        attempt["failure_attribution"] = "tool_failure"
    return attempt


def _required_attestations() -> list[dict]:
    return [
        {
            "statement": {
                "gate_ids": [gate_id],
                "role": role,
                "conclusion": "passed",
            },
            "signature": "valid-signature",
            "public_key": "ssh-ed25519 AAAATEST",
            "fingerprint": "SHA256:release-owner",
        }
        for gate_id, roles in REQUIRED_ROLE_ATTESTATIONS.items()
        for role in roles
    ]


def test_ledger_advances_one_typed_gate_attempt_without_io() -> None:
    ledger = _ledger()

    assert ledger.frontier == "P01"
    started_at = ledger.start_gate("P01")
    assert ledger.resume_gate("P01").execution_id == "execution-1"

    attempt = ledger.record_gate(
        "P01",
        GateResult(status="passed", artifacts=()),
        started_at=started_at,
    )

    assert attempt.status == "passed"
    assert ledger.frontier == "P02"
    assert ledger.gate_attempt_count() == 1
    with pytest.raises(EvidenceError, match="P01 already has its only gate attempt"):
        ledger.next_attempt("P01")


def test_orphan_checksum_makes_ledger_read_only() -> None:
    evidence = _MemoryEvidence()
    evidence._sealed = True

    with pytest.raises(EvidenceError, match="permanently read-only"):
        _ledger(evidence=evidence).start_gate("P01")


def test_record_gate_uses_evidence_adapter_for_artifact_existence() -> None:
    manifest = _manifest()
    attempt = _terminal_attempt("P01")
    attempt["status"] = "open"
    attempt.pop("completed_at")
    record = {
        "path": "00-package/P01-attempt-1/result.json",
        "sha256": "d" * 64,
        "bytes": 10,
    }
    attempt["artifacts"] = [record]
    manifest["gates"]["P01"] = [attempt]
    evidence = _MemoryEvidence(artifact_matches=False)
    artifact = evidence.artifact("P01", record)

    with pytest.raises(EvidenceError, match="artifact changed before gate recording"):
        _ledger(manifest, evidence=evidence).record_gate(
            "P01", GateResult("failed", (artifact,))
        )


def test_interrupted_gate_requires_public_reconciliation_before_safe_resume() -> None:
    ledger = _ledger()
    started_at = ledger.start_gate("P01")
    ledger.bind_operation("P01", kind="external_effect", operation_id="effect-1")
    ledger._requires_reconciliation = True

    with pytest.raises(
        EvidenceError, match="interrupted gate lacks proved terminal public facts"
    ):
        ledger.record_gate(
            "P01", GateResult("passed", ()), started_at=started_at
        )

    ledger.reconcile_operation(
        "P01",
        operation_id="execution-1",
        outcome="succeeded",
        public_fact={"terminal": True},
    )
    ledger.reconcile_operation(
        "P01",
        operation_id="effect-1",
        outcome="succeeded",
        public_fact={"terminal": True},
    )
    assert ledger.record_gate(
        "P01", GateResult("passed", ()), started_at=started_at
    ).status == "passed"


def test_failed_gate_is_terminal_and_evaluates_ineligible() -> None:
    ledger = _ledger()
    started_at = ledger.start_gate("P01")
    ledger.record_gate(
        "P01",
        GateResult("failed", (), "product_failure"),
        started_at=started_at,
    )

    assert ledger.frontier is None
    eligibility = ledger.evaluate()
    assert eligibility["conclusion"] == "ineligible"
    assert eligibility["reasons"][0] == {
        "code": "mandatory_gates_missing",
        "gate_ids": list(GATE_SEQUENCE[1:]),
    }
    assert eligibility["reasons"][1]["failure_attributions"] == {
        "P01": "product_failure"
    }


def test_evaluator_correction_restores_frontier_without_io_or_rewriting_failure() -> None:
    manifest = _manifest()
    for gate_id in GATE_SEQUENCE[: GATE_SEQUENCE.index("S01")]:
        manifest["gates"][gate_id] = [_terminal_attempt(gate_id)]
    failed = _terminal_attempt("S01", "failed")
    failed["artifacts"] = [
        {
            "path": "02-setup/S01-attempt-1/platform-initial.json",
            "sha256": "d" * 64,
            "bytes": 10,
        }
    ]
    manifest["gates"]["S01"] = [failed]
    ledger = _ledger(manifest)
    source = ledger.evidence.artifact("S01", failed["artifacts"][0])

    ledger.correct_s01(
        source_artifact=source,
        acceptance_tool_sha256="e" * 64,
        diagnostic_conclusion_sha256="f" * 64,
        reason="Correct the evaluator only.",
    )

    assert ledger._manifest["gates"]["S01"][0]["status"] == "failed"
    assert ledger.frontier == "S02"
    with pytest.raises(ValueError, match="effective failed gate"):
        ledger.correct_s01(
            source_artifact=source,
            acceptance_tool_sha256="1" * 64,
            diagnostic_conclusion_sha256="2" * 64,
            reason="A second correction is forbidden.",
        )


def test_eligible_ledger_requires_release_owner_decision_before_seal() -> None:
    manifest = _manifest()
    for gate_id in GATE_SEQUENCE:
        manifest["gates"][gate_id] = [_terminal_attempt(gate_id)]
    evidence = _MemoryEvidence(_required_attestations())
    ledger = _ledger(manifest, evidence=evidence)

    assert ledger.evaluate()["conclusion"] == "eligible"
    with pytest.raises(PromotionError, match="signed Promotion Decision"):
        ledger.seal()

    decision = PromotionDecision(ledger)
    statement = decision.statement(
        actor="owner@example.test", decision="promote", note="Promote exact bundle."
    )
    decision.record(
        statement,
        signature="valid-signature",
        public_key="ssh-ed25519 AAAATEST",
        fingerprint="SHA256:release-owner",
    )

    assert ledger.seal() == Path("/unused/SHA256SUMS")
    assert ledger.status()["status"] == "sealed"
