from __future__ import annotations

from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.adapters import PlaywrightBrowser, PlaywrightV01Console
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION
from aiops.acceptance.ledger import (
    REQUIRED_ROLE_ATTESTATIONS,
    AcceptanceLedger,
    EvidenceError,
)
from tests.pilot_acceptance_support import create_evidence


def _ledger(tmp_path: Path) -> AcceptanceLedger:
    ids = count(1)
    return create_evidence(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-u10-hitl",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="pilot-clean",
        cluster_identity_sha256="c" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-16T01:02:03Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
        attestation_verifier=lambda _item: None,
    )


def test_required_hitl_points_cannot_be_satisfied_by_browser_automation(tmp_path: Path) -> None:
    assert {"S04", "S05", "V04", "V05", "V07", "V08", "C03"} <= set(
        REQUIRED_ROLE_ATTESTATIONS
    )
    assert not hasattr(PlaywrightBrowser, "sign")
    assert not hasattr(PlaywrightV01Console, "approve")

    evidence = _ledger(tmp_path)
    for gate_id, roles in REQUIRED_ROLE_ATTESTATIONS.items():
        for role in roles:
            with pytest.raises(EvidenceError, match="missing"):
                evidence.require_verified_attestation(gate_id, role=role)


def test_only_externally_supplied_signed_attestation_releases_hitl_pause(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    statement = evidence.attestation_statement(
        actor="platform-admin@example.test",
        role="platform_administrator",
        gate_ids=["S04"],
        conclusion="passed",
        note="received exact provider message",
    )
    evidence.append_attestation(
        statement,
        signature="external-signature",
        public_key="external-public-key",
        fingerprint="SHA256:external",
    )

    assert evidence.require_verified_attestation(
        "S04", role="platform_administrator"
    )[0]["statement"] == statement
