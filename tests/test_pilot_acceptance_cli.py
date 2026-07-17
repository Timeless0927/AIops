from __future__ import annotations

import argparse
import json
from itertools import count
from pathlib import Path

import pytest

from aiops.acceptance.evidence import AcceptanceEvidence
from aiops.acceptance.cluster_identity import KubernetesClusterIdentitySource
from aiops.acceptance.command import CommandResult
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION, GATE_SEQUENCE
from aiops.acceptance.runtime import AcceptanceRuntime
from scripts import run_pilot_acceptance as cli


def _ledger(tmp_path: Path) -> AcceptanceEvidence:
    ids = count(1)
    return AcceptanceEvidence.create(
        tmp_path / "acceptance", acceptance_id="v0.1.0-cli",
        release_version="v0.1.0", release_sha256="a" * 64,
        acceptance_tool_sha256="b" * 64,
        gate_contract_revision=GATE_CONTRACT_REVISION, kube_context="pilot-clean",
        cluster_identity_sha256="c" * 64, access_profile="http_nodeport",
        now=lambda: "2026-07-17T01:02:03Z",
        new_execution_id=lambda: f"execution-{next(ids)}",
        attestation_verifier=lambda _item: None,
    )


def _config(tmp_path: Path) -> dict[str, object]:
    narrative = {
        "impact_summary": "bounded impact",
        "root_cause_explanation": "verified root cause",
        "resolution_summary": "controlled resolution",
        "follow_up_narrative": "follow up owner recorded",
    }
    return {
        "format_version": 1,
        "archive": str((tmp_path / "release.tar.gz").resolve()),
        "checksums": str((tmp_path / "SHA256SUMS").resolve()),
        "work_dir": str((tmp_path / "work").resolve()),
        "acceptance_tool": str((tmp_path / "tool.tar.gz").resolve()),
        "base_url": "http://pilot.example.test",
        "https_profile": {
            "base_url": None, "ingress": None, "http_url": None,
            "require_redirect": False,
        },
        "usernames": {"admin": "admin", "ordinary": "ordinary", "sre": "pilot-sre"},
        "model": {
            "endpoint": "https://model.example.test/v1", "endpoint_scope": "external",
            "model": "pilot-model", "timeout_seconds": 30,
        },
        "notification_provider": "feishu",
        "report_v1_narrative": narrative,
        "report_v2_narrative": narrative,
    }


def _record(evidence: AcceptanceEvidence, gate_id: str, values=()) -> None:
    started_at = evidence.start_gate(gate_id)
    artifacts = [
        evidence.write_json(gate_id, name, value) for name, value in values
    ]
    evidence.record_gate(gate_id, "passed", artifacts, started_at=started_at)


def test_cli_exposes_single_gate_and_finalization_commands_only() -> None:
    choices = cli.parser()._subparsers._group_actions[0].choices
    assert {"status", "advance", "resume", "evaluate", "decide", "seal"} <= set(choices)
    assert {"package", "install", "web", "setup"}.isdisjoint(choices)


def test_cluster_identity_is_owned_by_its_adapter() -> None:
    class Commands:
        def __init__(self) -> None:
            self.results = [
                CommandResult(("kubectl",), 0, "pilot-clean\n", "", 0),
                CommandResult(("kubectl",), 0, json.dumps({
                    "clusters": [{"cluster": {
                        "server": "https://cluster.example.test",
                        "certificate-authority-data": "ca-data",
                    }}],
                }), "", 0),
            ]

        def run(self, _command, **_kwargs):
            return self.results.pop(0)

    context, digest = KubernetesClusterIdentitySource(Commands()).read()
    assert context == "pilot-clean"
    assert len(digest) == 64


def test_status_reads_the_ledger_without_writing(tmp_path: Path, capsys) -> None:
    evidence = _ledger(tmp_path)
    before = evidence.manifest_path.read_bytes()

    cli.cmd_status(argparse.Namespace(acceptance=evidence.root))

    assert json.loads(capsys.readouterr().out) == {
        "status": "active/ready", "frontier": "P01", "open_gate": None,
    }
    assert evidence.manifest_path.read_bytes() == before


def test_runtime_config_rejects_secret_fields_and_derives_v02_identity_from_v01(
    tmp_path: Path, monkeypatch,
) -> None:
    evidence = _ledger(tmp_path)
    bad = _config(tmp_path) | {"password": "must-not-be-in-config"}
    with pytest.raises(ValueError, match="public payload"):
        AcceptanceRuntime(
            evidence=evidence, config=bad, source_root=tmp_path,
            credential_store=None, admission_verifier=lambda _item: None,
            attest=lambda _gate, _role: None,
        )
    runtime = AcceptanceRuntime(
        evidence=evidence, config=_config(tmp_path), source_root=tmp_path,
        credential_store=None, admission_verifier=lambda _item: None,
        attest=lambda _gate, _role: None,
    )
    for gate_id in GATE_SEQUENCE[: GATE_SEQUENCE.index("V01")]:
        _record(evidence, gate_id)
    _record(evidence, "V01", (("run.json", {
        "run_id": "12345678-1234-1234-1234-123456789012",
        "trigger_started_at": 1_700_000_000.0,
    }),))
    captured: dict[str, object] = {}

    class Runner:
        def run_v02(self, run_id: str, *, trigger_started_at: float):
            captured.update(run_id=run_id, trigger_started_at=trigger_started_at)
            return {"gate_id": "V02"}

    monkeypatch.setattr(runtime, "_run_one", lambda **_kwargs: Runner())

    assert runtime.advance("V02") == {"gate_id": "V02"}
    assert captured == {
        "run_id": "12345678-1234-1234-1234-123456789012",
        "trigger_started_at": 1_700_000_000.0,
    }


def test_attestation_note_binds_the_exact_open_review(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path)
    for gate_id in GATE_SEQUENCE[: GATE_SEQUENCE.index("S04")]:
        _record(evidence, gate_id)
    evidence.start_gate("S04")
    review = evidence.write_json("S04", "receipt-review.json", {"status": "sent"})

    assert cli._attestation_note(
        evidence, "S04", "platform_administrator",
    ) == f"notification_receipt_sha256={review.sha256}"
