from __future__ import annotations

from pathlib import Path

import pytest

from aiops.acceptance import tool_artifact
from aiops.acceptance.evidence_files import sha256
from aiops.acceptance.evidence_files import sha256_bytes
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION
from aiops.acceptance.tool_artifact import (
    EVIDENCE_FORMAT_VERSION,
    REQUIRED_CHECKS,
    SELF_CHECK_ID,
    build_acceptance_tool,
    inspect_acceptance_tool,
    self_check,
)


ROOT = Path(__file__).resolve().parents[1]


def _admission(release_sha256: str = "a" * 64) -> dict[str, object]:
    return {
        "statement": {
            "format_version": 1,
            "release_sha256": release_sha256,
            "gate_contract_revision": GATE_CONTRACT_REVISION,
            "evidence_format_version": EVIDENCE_FORMAT_VERSION,
            "checks": {name: "passed" for name in REQUIRED_CHECKS},
            "live_evidence": False,
        },
        "signature": "signed-admission",
        "public_key": "ssh-ed25519 release-owner",
        "fingerprint": "SHA256:release-owner",
    }


def _verify(item: dict[str, object]) -> None:
    if item["signature"] != "signed-admission":
        raise ValueError("bad signature")


def test_tool_artifact_is_deterministic_hashed_and_self_checking(tmp_path: Path) -> None:
    first = build_acceptance_tool(
        ROOT, _admission(), tmp_path / "first.tar.gz", verifier=_verify,
    )
    second = build_acceptance_tool(
        ROOT, _admission(), tmp_path / "second.tar.gz", verifier=_verify,
    )

    assert first.read_bytes() == second.read_bytes()
    inspected = inspect_acceptance_tool(first)
    assert inspected["manifest"]["self_check"]["id"] == SELF_CHECK_ID
    assert inspected["admission"]["statement"]["live_evidence"] is False
    result = self_check(
        first,
        release_sha256="a" * 64,
        acceptance_tool_sha256=sha256(first),
        gate_contract_revision=GATE_CONTRACT_REVISION,
        verifier=_verify,
    )
    assert result["id"] == SELF_CHECK_ID
    assert result["gate_contract_revision"] == GATE_CONTRACT_REVISION
    assert result["live_evidence"] is False


def test_tool_artifact_rejects_bad_signature_identity_and_tamper(tmp_path: Path) -> None:
    admission = _admission()
    admission["signature"] = ""
    with pytest.raises(ValueError, match="contract"):
        build_acceptance_tool(
            ROOT, admission, tmp_path / "bad.tar.gz", verifier=_verify,
        )
    artifact = build_acceptance_tool(
        ROOT, _admission(), tmp_path / "tool.tar.gz", verifier=_verify,
    )
    content = bytearray(artifact.read_bytes())
    content[len(content) // 2] ^= 1
    artifact.write_bytes(content)
    with pytest.raises(ValueError):
        inspect_acceptance_tool(artifact)


def test_self_check_rejects_wrong_release_tool_or_signature(tmp_path: Path) -> None:
    artifact = build_acceptance_tool(
        ROOT, _admission(), tmp_path / "tool.tar.gz", verifier=_verify,
    )
    digest = sha256(artifact)
    with pytest.raises(ValueError, match="ledger identity"):
        self_check(
            artifact, release_sha256="a" * 64,
            acceptance_tool_sha256="b" * 64,
            gate_contract_revision=GATE_CONTRACT_REVISION, verifier=_verify,
        )
    with pytest.raises(ValueError, match="Pilot Release"):
        self_check(
            artifact, release_sha256="d" * 64,
            acceptance_tool_sha256=digest,
            gate_contract_revision=GATE_CONTRACT_REVISION, verifier=_verify,
        )
    with pytest.raises(ValueError, match="signature"):
        self_check(
            artifact, release_sha256="a" * 64,
            acceptance_tool_sha256=digest,
            gate_contract_revision=GATE_CONTRACT_REVISION,
            verifier=lambda _item: (_ for _ in ()).throw(ValueError("bad")),
        )
    with pytest.raises(ValueError, match="gate contract"):
        self_check(
            artifact, release_sha256="a" * 64, acceptance_tool_sha256=digest,
            gate_contract_revision="different-contract", verifier=_verify,
        )


def test_recomputed_checksums_cannot_smuggle_an_undeclared_entry(tmp_path: Path) -> None:
    artifact = build_acceptance_tool(
        ROOT, _admission(), tmp_path / "tool.tar.gz", verifier=_verify,
    )
    entries = tool_artifact._read_archive(artifact)
    checksum_name = f"{tool_artifact.TOOL_ROOT}/SHA256SUMS"
    entries[f"{tool_artifact.TOOL_ROOT}/undeclared.txt"] = b"not tool source"
    entries[checksum_name] = "".join(
        f"{sha256_bytes(content)}  {name}\n"
        for name, content in sorted(entries.items()) if name != checksum_name
    ).encode()
    artifact.write_bytes(tool_artifact._archive(entries))

    with pytest.raises(ValueError, match="undeclared"):
        inspect_acceptance_tool(artifact)
