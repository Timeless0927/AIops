from __future__ import annotations

from pathlib import Path

import pytest

from aiops.acceptance.evidence import (
    GATE_CONTRACT_REVISION,
    AcceptanceEvidence,
    EvidenceError,
)
from tests.pilot_acceptance_support import create_evidence, open_evidence


def _ledger(tmp_path: Path) -> AcceptanceEvidence:
    return create_evidence(
        tmp_path / "acceptance",
        acceptance_id="late-writer",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-21T05:44:45Z",
        new_execution_id=lambda: "execution-1",
    )


def test_late_writer_cannot_overwrite_terminal_gate(tmp_path: Path) -> None:
    stale = _ledger(tmp_path)
    started_at = stale.start_gate("P01")
    result = stale.write_json("P01", "result.json", {"terminal": True})

    terminal = open_evidence(stale.root)
    execution = terminal.resume_gate("P01")
    failure = terminal.write_json(
        "P01", "resume-failure.json", {"effect_replayed": False}
    )
    terminal.record_gate(
        "P01", "failed", [*execution.artifacts, failure], started_at=execution.started_at
    )
    terminal_manifest = stale.manifest_path.read_bytes()

    with pytest.raises(EvidenceError, match="stale ledger writer"):
        stale.write_json("P01", "late.json", {"terminal": True})
    with pytest.raises(EvidenceError, match="stale ledger writer"):
        stale.record_gate("P01", "passed", [result], started_at=started_at)

    assert stale.manifest_path.read_bytes() == terminal_manifest
    assert not (stale.root / "00-package/P01-attempt-1/late.json").exists()
    assert open_evidence(stale.root).status()["status"] == "ineligible"


def test_stale_attestation_writer_changes_neither_file(tmp_path: Path) -> None:
    current = _ledger(tmp_path)
    stale = open_evidence(current.root)
    first = current.attestation_statement(
        actor="mao", role="platform_administrator", gate_ids=["I05"],
        conclusion="passed", note="current writer",
    )
    current.append_attestation(
        first, signature="sig-1", public_key="ssh-ed25519 AAAATEST",
        fingerprint="SHA256:first",
    )
    manifest = current.manifest_path.read_bytes()
    attestations = current.attestation_path.read_bytes()

    second = stale.attestation_statement(
        actor="mao", role="sre", gate_ids=["V04"],
        conclusion="passed", note="stale writer",
    )
    with pytest.raises(EvidenceError, match="stale ledger writer"):
        stale.append_attestation(
            second, signature="sig-2", public_key="ssh-ed25519 AAAATEST",
            fingerprint="SHA256:second",
        )

    assert current.manifest_path.read_bytes() == manifest
    assert current.attestation_path.read_bytes() == attestations
    open_evidence(current.root)


def test_manifest_and_ledger_symlinks_are_rejected(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    ledger_link = tmp_path / "ledger-link"
    ledger_link.symlink_to(evidence.root, target_is_directory=True)
    with pytest.raises(EvidenceError, match="manifest does not exist"):
        open_evidence(ledger_link)

    manifest_copy = tmp_path / "manifest-copy.json"
    manifest_copy.write_bytes(evidence.manifest_path.read_bytes())
    evidence.manifest_path.unlink()
    evidence.manifest_path.symlink_to(manifest_copy)
    with pytest.raises(EvidenceError, match="manifest does not exist"):
        open_evidence(evidence.root)
