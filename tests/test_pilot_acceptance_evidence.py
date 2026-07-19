from __future__ import annotations

import hashlib
import json
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.deployment_continuation import create_diagnostic_bundle
from aiops.acceptance.evidence import (
    GATE_CONTRACT_REVISION,
    GATE_SEQUENCE,
    MAX_ARTIFACT_BYTES,
    AcceptanceEvidence,
    EvidenceError,
)
from aiops.acceptance.redaction import redact_json, redact_text
from aiops.acceptance.promotion import PromotionError
from tests.pilot_acceptance_support import create_evidence, open_evidence, qualified_environment


def _ledger(tmp_path: Path, *, profile: str = "http_nodeport") -> AcceptanceEvidence:
    ids = count(1)
    return create_evidence(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-20260714T010203Z",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile=profile,
        now=lambda: "2026-07-14T01:02:03Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
    )


def _complete(evidence: AcceptanceEvidence, gate_id: str, status: str = "passed") -> None:
    started_at = evidence.start_gate(gate_id)
    evidence.record_gate(gate_id, status, [], started_at=started_at)  # type: ignore[arg-type]


def _advance_to(evidence: AcceptanceEvidence, gate_id: str) -> None:
    for predecessor in GATE_SEQUENCE[: GATE_SEQUENCE.index(gate_id)]:
        _complete(
            evidence,
            predecessor,
            "not_applicable" if predecessor == "I04" else "passed",
        )


def test_complete_canonical_dag_has_single_frontier_and_conditional_i04(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    assert GATE_SEQUENCE == (
        "P01", "P02", "I01", "I02", "I03", "I04", "I05",
        "S01", "S02", "S03", "S04", "S05", "S06",
        "V01", "V02", "V03", "V04", "R05", "V05", "V06", "V07",
        "R01", "R02", "R03", "R04", "R06", "V08", "C01", "C02", "C03",
    )
    assert evidence.status() == {
        "status": "active/ready", "frontier": "P01", "open_gate": None
    }
    _advance_to(evidence, "I04")
    _complete(evidence, "I04", "not_applicable")
    assert evidence.frontier == "I05"

    with pytest.raises(EvidenceError, match="not frontier"):
        evidence.start_gate("V05")
    for gate_id in GATE_SEQUENCE[GATE_SEQUENCE.index("I05") :]:
        _complete(evidence, gate_id)
    assert evidence.status() == {
        "status": "active/ready", "frontier": None, "open_gate": None
    }

    https = _ledger(tmp_path / "https", profile="https_ingress")
    _advance_to(https, "I04")
    https.start_gate("I04")
    with pytest.raises(EvidenceError, match="only for http_nodeport"):
        https.record_gate("I04", "not_applicable", [])


def test_gate_begin_operation_binding_and_resume_are_durable_without_replay(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    started_at = evidence.start_gate("P01")
    evidence.bind_operation("P01", kind="request", operation_id="request-1")
    evidence.write_json("P01", "intent.json", {"request_id": "request-1"})

    assert evidence.status() == {
        "status": "active/open", "frontier": "P01", "open_gate": "P01"
    }
    with pytest.raises(EvidenceError, match="already open"):
        evidence.start_gate("P01")
    with pytest.raises(EvidenceError, match="duplicate effect"):
        evidence.bind_operation("P01", kind="request", operation_id="request-1")

    reopened = open_evidence(
        evidence.root,
        now=lambda: "2026-07-14T01:03:03Z",
        new_execution_id=lambda: "must-not-be-used",
    )
    execution = reopened.resume_gate("P01")
    assert execution.execution_id == "execution-1"
    assert execution.operations[0] == {
        "kind": "gate_execution",
        "operation_id": "execution-1",
        "bound_at": "2026-07-14T01:02:03Z",
    }
    assert execution.operations[1]["operation_id"] == "request-1"
    assert execution.artifacts[0].relative_path.endswith("intent.json")
    for operation in execution.operations:
        reopened.reconcile_operation(
            "P01",
            operation_id=operation["operation_id"],
            outcome="succeeded",
            public_fact={"operation_id": operation["operation_id"], "terminal": True},
        )
    reopened.record_gate("P01", "passed", execution.artifacts, started_at=started_at)
    assert reopened.frontier == "P02"
    with pytest.raises(EvidenceError, match="no open gate execution"):
        reopened.record_gate("P01", "passed", [])


def test_interrupted_unprovable_operation_can_only_fail(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    reopened = open_evidence(evidence.root)
    execution = reopened.resume_gate("P01")
    with pytest.raises(EvidenceError, match="lacks proved terminal public facts"):
        reopened.record_gate("P01", "passed", execution.artifacts)
    reopened.reconcile_operation(
        "P01",
        operation_id=execution.execution_id,
        outcome="unprovable",
        public_fact={"query": "public projection", "matches": 0},
    )
    with pytest.raises(EvidenceError, match="lacks proved terminal public facts"):
        reopened.record_gate("P01", "passed", execution.artifacts)
    reopened.record_gate("P01", "failed", execution.artifacts)
    assert reopened.status()["status"] == "ineligible"


def test_failed_gate_records_attribution_separately_from_terminal_result(
    tmp_path: Path,
) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    evidence.record_gate("P01", "failed", [])

    assert evidence.failure_summary()["failure_attribution"] == "inconclusive"
    assert evidence.failure_summary()["status"] == "failed"
    assert evidence.status()["failure"] == {
        "gate_id": "P01", "attribution": "inconclusive",
    }

    explicit = _ledger(tmp_path / "explicit")
    explicit.start_gate("P01")
    explicit.record_gate(
        "P01", "failed", [], failure_attribution="tool_failure",
    )
    assert explicit.failure_summary()["failure_attribution"] == "tool_failure"

    invalid = _ledger(tmp_path / "invalid")
    invalid.start_gate("P01")
    with pytest.raises(EvidenceError, match="only failed gates"):
        invalid.record_gate(
            "P01", "passed", [], failure_attribution="tool_failure",
        )


def test_open_rejects_missing_or_duplicate_journal_identities(tmp_path: Path) -> None:
    missing = _ledger(tmp_path / "missing")
    missing.start_gate("P01")
    manifest = json.loads(missing.manifest_path.read_text())
    manifest["gates"]["P01"][0]["operations"] = []
    missing.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="operation journal"):
        open_evidence(missing.root)

    duplicate = _ledger(tmp_path / "duplicate")
    duplicate.start_gate("P01")
    manifest = json.loads(duplicate.manifest_path.read_text())
    operation = dict(manifest["gates"]["P01"][0]["operations"][0])
    operation["kind"] = "request"
    manifest["gates"]["P01"][0]["operations"].append(operation)
    duplicate.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="missing or duplicated"):
        open_evidence(duplicate.root)

    reconciled_twice = _ledger(tmp_path / "reconciled-twice")
    reconciled_twice.start_gate("P01")
    manifest = json.loads(reconciled_twice.manifest_path.read_text())
    attempt = manifest["gates"]["P01"][0]
    fact = {
        "operation_id": attempt["execution_id"],
        "outcome": "succeeded",
        "public_fact": {"terminal": True},
        "recorded_at": "2026-07-14T01:02:03Z",
    }
    attempt["reconciliations"] = [fact, fact]
    reconciled_twice.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="reconciliation fact"):
        open_evidence(reconciled_twice.root)


def test_operation_identity_is_unique_across_the_ledger(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path / "dispatch")
    evidence.start_gate("P01")
    evidence.bind_operation("P01", kind="request", operation_id="execution-2")
    evidence.record_gate("P01", "passed", [])
    with pytest.raises(EvidenceError, match="not unique"):
        evidence.start_gate("P02")

    tampered = _ledger(tmp_path / "tampered")
    _complete(tampered, "P01")
    tampered.start_gate("P02")
    manifest = json.loads(tampered.manifest_path.read_text())
    first_id = manifest["gates"]["P01"][0]["execution_id"]
    second = manifest["gates"]["P02"][0]
    second["execution_id"] = first_id
    second["operations"][0]["operation_id"] = first_id
    tampered.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="duplicated across the ledger"):
        open_evidence(tampered.root)


def test_identity_drift_is_a_durable_ineligibility_fact(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    with pytest.raises(EvidenceError, match="acceptance_tool.sha256"):
        AcceptanceEvidence.create(
            evidence.root.parent,
            acceptance_id=evidence.root.name,
            release_version="v0.1.0",
            release_sha256="a" * 64,
            acceptance_tool_sha256="d" * 64,
            gate_contract_revision=GATE_CONTRACT_REVISION,
            kube_context="pilot-clean",
            cluster_identity_sha256="b" * 64,
            access_profile="http_nodeport",
            environment_qualification=qualified_environment(
                release_sha256="a" * 64, acceptance_tool_sha256="d" * 64,
                gate_contract_revision=GATE_CONTRACT_REVISION,
                kube_context="pilot-clean", cluster_identity_sha256="b" * 64,
                access_profile="http_nodeport",
            ),
            attestation_verifier=lambda _item: None,
        )

    reopened = open_evidence(evidence.root)
    assert reopened.status()["status"] == "ineligible"
    assert reopened.frontier is None
    with pytest.raises(EvidenceError, match="identity drift"):
        reopened.start_gate("P01")


def test_failed_gate_terminalizes_run_and_diagnostics_stay_separate(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    evidence.record_gate("P01", "failed", [])

    assert evidence.status()["status"] == "ineligible"
    with pytest.raises(EvidenceError, match="mandatory gate P01 failed"):
        evidence.start_gate("P02")
    with pytest.raises(EvidenceError, match="only gate attempt"):
        evidence.next_attempt("P01")

    diagnostic = create_diagnostic_bundle(
        evidence, tmp_path / "diagnostics", diagnostic_id="p01-investigation"
    )
    payload = json.loads((diagnostic / "manifest.json").read_text())
    assert payload["source_acceptance_id"] == evidence.root.name
    assert payload["source_failed_gate"] == "P01"
    assert "diagnostics" not in json.loads(evidence.manifest_path.read_text())


@pytest.mark.parametrize("format_version", [1, 3])
def test_legacy_evidence_formats_are_explicitly_unsupported(
    tmp_path: Path, format_version: int,
) -> None:
    root = tmp_path / f"legacy-{format_version}"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"format_version": format_version}))
    with pytest.raises(EvidenceError, match="unsupported_evidence_format"):
        open_evidence(root)


def test_artifacts_are_bounded_redacted_indexed_and_hash_verified(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    started_at = evidence.start_gate("P01")
    secret = "sk-live-secret-value"
    artifact = evidence.write_json(
        "P01",
        "result.json",
        {"request_id": "request-1", "api_key": secret, "recipient": "person@example.test"},
        known_secrets=[secret],
    )
    assert artifact.sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
    assert secret not in artifact.path.read_text()
    assert "person@example.test" not in artifact.path.read_text()
    with pytest.raises(EvidenceError, match="already exists"):
        evidence.write_text("P01", "result.json", "duplicate")
    with pytest.raises(EvidenceError, match="artifact name"):
        evidence.write_text("P01", "../escape.txt", "no")
    with pytest.raises(EvidenceError, match="exceeds"):
        evidence.write_bytes("P01", "large.bin", b"x" * (MAX_ARTIFACT_BYTES + 1))

    evidence.record_gate("P01", "passed", [artifact], started_at=started_at)
    retained = evidence.passed_artifact_json("P01", "result.json")
    assert retained["value"]["request_id"] == "request-1"
    artifact.path.write_text("{}")
    with pytest.raises(EvidenceError, match="artifact changed"):
        evidence.passed_artifact_json("P01", "result.json")
    with pytest.raises(EvidenceError, match="hash, size or bound"):
        open_evidence(evidence.root)


def test_completed_artifact_index_exposes_only_hash_verified_terminal_facts(
    tmp_path: Path,
) -> None:
    evidence = _ledger(tmp_path)
    started_at = evidence.start_gate("P01")
    artifact = evidence.write_text("P01", "result.txt", "bounded")
    evidence.record_gate("P01", "passed", [artifact], started_at=started_at)
    evidence.start_gate("P02")

    assert evidence.completed_artifact_index() == [{
        "gate_id": "P01", "status": "passed", "execution_id": "execution-1",
        "artifacts": [{
            "path": artifact.relative_path, "sha256": artifact.sha256,
            "bytes": artifact.size,
        }],
    }]
    artifact.path.write_text("changed")
    with pytest.raises(EvidenceError, match="hash, size or bound"):
        evidence.completed_artifact_index()


def test_unindexed_files_and_symlinks_fail_closed(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    (evidence.root / "orphan.txt").write_text("unindexed")
    with pytest.raises(EvidenceError, match="unindexed file"):
        open_evidence(evidence.root)
    (evidence.root / "orphan.txt").unlink()
    (evidence.root / "link").symlink_to(evidence.manifest_path)
    with pytest.raises(EvidenceError, match="symlink"):
        open_evidence(evidence.root)


def test_artifact_write_failure_rolls_back_index_and_remains_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiops.acceptance.evidence as evidence_module

    evidence = _ledger(tmp_path)
    evidence.start_gate("P01")
    atomic_write = evidence_module._atomic_write

    def fail_artifact(path: Path, content: bytes, **kwargs) -> None:
        if path.name == "result.txt":
            raise OSError("simulated artifact fsync failure")
        atomic_write(path, content, **kwargs)

    monkeypatch.setattr(evidence_module, "_atomic_write", fail_artifact)
    with pytest.raises(OSError, match="fsync failure"):
        evidence.write_text("P01", "result.txt", "bounded")
    assert not (evidence.root / "00-package/P01-attempt-1/result.txt").exists()
    reopened = open_evidence(evidence.root)
    assert reopened.resume_gate("P01").artifacts == ()


def test_interrupted_pending_artifact_is_idempotently_completed_or_failed(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path / "retry")
    evidence.start_gate("P01")
    artifact = evidence.write_text("P01", "result.txt", "bounded")
    artifact.path.unlink()

    reopened = open_evidence(evidence.root)
    execution = reopened.resume_gate("P01")
    retried = reopened.write_text("P01", "result.txt", "bounded")
    assert retried.sha256 == artifact.sha256
    with pytest.raises(EvidenceError, match="already exists"):
        reopened.write_text("P01", "result.txt", "bounded")
    reopened.reconcile_operation(
        "P01",
        operation_id=execution.execution_id,
        outcome="succeeded",
        public_fact={"terminal": True},
    )
    reopened.record_gate("P01", "passed", execution.artifacts)

    failed = _ledger(tmp_path / "failed")
    failed.start_gate("P01")
    failed_artifact = failed.write_text("P01", "result.txt", "bounded")
    failed_artifact.path.unlink()
    reopened = open_evidence(failed.root)
    execution = reopened.resume_gate("P01")
    reopened.record_gate("P01", "failed", execution.artifacts)
    assert open_evidence(failed.root).status()["status"] == "ineligible"


def test_atomic_write_cleans_real_temp_file_on_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiops.acceptance import evidence_files

    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    destination.mkdir()

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(evidence_files.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync failure"):
        evidence_files.atomic_write(
            destination / "artifact.txt", b"bounded", staging_dir=staging
        )
    assert list(staging.iterdir()) == []


def test_text_and_json_redaction_keep_bounded_public_facts() -> None:
    secret = "sk-live-secret-value"
    text = redact_text(
        f"Authorization: Bearer token\nCookie: session=value\napi_key={secret}\n",
        known_secrets=[secret],
    )
    assert secret not in text and "token" not in text and "session=value" not in text
    assert redact_json(
        {"request_id": "request-1", "password": secret, "wrong_password": 401},
        known_secrets=[secret],
    ) == {"request_id": "request-1", "password": "[REDACTED]", "wrong_password": 401}


def test_attestation_index_is_revalidated_and_cannot_evaluate_early(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    statement = evidence.attestation_statement(
        actor="operator@example.test",
        role="platform_administrator",
        gate_ids=["I05"],
        conclusion="passed",
        note="manual boundary observed",
    )
    evidence.append_attestation(
        statement,
        signature="test-signature",
        public_key="ssh-ed25519 AAAATEST",
        fingerprint="SHA256:test",
    )
    open_evidence(evidence.root)
    with pytest.raises(PromotionError, match="C03 completion"):
        evidence.evaluate()
    tampered = evidence.attestation_path.read_text().replace("a" * 64, "d" * 64)
    evidence.attestation_path.write_text(tampered)
    manifest = json.loads(evidence.manifest_path.read_text())
    manifest["human_attestation"]["sha256"] = hashlib.sha256(tampered.encode()).hexdigest()
    evidence.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="attestation statement"):
        open_evidence(evidence.root)
    evidence.attestation_path.write_text("format_version: 2\nattestations: []\n")
    with pytest.raises(EvidenceError, match="attestation index"):
        open_evidence(evidence.root)
