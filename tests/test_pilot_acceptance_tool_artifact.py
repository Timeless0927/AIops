from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops.acceptance import tool_artifact
from aiops.acceptance.evidence_files import sha256
from aiops.acceptance.evidence_files import sha256_bytes
from aiops.acceptance.freeze import build_admission_statement
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION
from aiops.acceptance.tool_artifact import (
    REQUIRED_CHECKS,
    SELF_CHECK_ID,
    build_acceptance_tool,
    inspect_acceptance_tool,
    self_check,
)


ROOT = Path(__file__).resolve().parents[1]


def _admission(release_sha256: str = "a" * 64) -> dict[str, object]:
    commit = "1" * 40
    reports = {
        name: {
            "status": "passed",
            "command": f"check {name}",
            "summary": f"{name} passed",
            "details": {
                "reviewed_commit": commit,
                **({"fixed_point": commit} if name.endswith("_review") else {}),
            },
        }
        for name in REQUIRED_CHECKS
    }
    return {
        "statement": build_admission_statement(
            release_identity={
                "archive_sha256": release_sha256,
                "openapi": {
                    "api_version": "1.0.0",
                    "producer_sha256": "b" * 64,
                    "console_consumer_sha256": "c" * 64,
                },
                "images_sha256": "d" * 64,
                "config_revisions_sha256": "e" * 64,
                "defaults_sha256": "f" * 64,
            },
            source_inventory=[{"path": "source.py", "sha256": "0" * 64, "bytes": 1}],
            reports=reports,
            pre_f10_commit=commit,
            reviewed_commit=commit,
            reviewed_tree="2" * 40,
        ),
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
    assert set(result["admission_checks"]) == set(REQUIRED_CHECKS)
    assert result["fixed_point"]["reviewed_commit"] == "1" * 40
    assert result["source_inventory_sha256"]
    assert result["live_evidence"] is False
    playwright = json.loads(
        (ROOT / "apps/aiops_console_web/node_modules/playwright/package.json").read_text()
    )
    assert result["browser_runtime"]["playwright_version"] == playwright["version"]
    assert result["browser_runtime"]["chromium_bytes"] > 0

    source_paths = {item["path"] for item in inspected["source_inventory"]}
    assert {
        "aiops/acceptance/gate_reuse.py",
        "scripts/pilot_acceptance_browser.mjs",
        "scripts/pilot_acceptance_governed_change.mjs",
        "scripts/pilot_acceptance_report.mjs",
        "scripts/pilot_acceptance_v01_browser.mjs",
        "apps/aiops_console_web/package.json",
        "apps/aiops_console_web/node_modules/playwright/package.json",
        "apps/aiops_console_web/node_modules/playwright-core/package.json",
    } <= source_paths


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


def test_self_check_rejects_incomplete_browser_runtime(tmp_path: Path) -> None:
    artifact = build_acceptance_tool(
        ROOT, _admission(), tmp_path / "tool.tar.gz", verifier=_verify,
    )
    entries = tool_artifact._read_archive(artifact)
    source = f"{tool_artifact.TOOL_ROOT}/source/"
    del entries[f"{source}apps/aiops_console_web/node_modules/playwright/index.js"]
    inventory = tool_artifact._inventory(entries, prefix=source)
    manifest_name = f"{tool_artifact.TOOL_ROOT}/manifest.json"
    manifest = json.loads(entries[manifest_name])
    source_sha256 = sha256_bytes(tool_artifact._json_bytes(inventory))
    manifest["source_sha256"] = source_sha256
    manifest["self_check"]["source_sha256"] = source_sha256
    entries[manifest_name] = tool_artifact._json_bytes(manifest)
    checksum_name = f"{tool_artifact.TOOL_ROOT}/SHA256SUMS"
    entries[checksum_name] = "".join(
        f"{sha256_bytes(content)}  {name}\n"
        for name, content in sorted(entries.items()) if name != checksum_name
    ).encode()
    artifact.write_bytes(tool_artifact._archive(entries))

    inspect_acceptance_tool(artifact)
    with pytest.raises(ValueError, match="browser runtime"):
        self_check(
            artifact, release_sha256="a" * 64,
            acceptance_tool_sha256=sha256(artifact),
            gate_contract_revision=GATE_CONTRACT_REVISION, verifier=_verify,
        )
