from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from aiops.acceptance.cluster_install import ClusterInstallRunner
from aiops.acceptance.command import CommandResult
from aiops.acceptance.evidence import A01_GATE_SEQUENCE, AcceptanceEvidence, GateFailed
from tests.pilot_acceptance_support import create_evidence


IMAGE = "registry.example.test/aiops/gateway@sha256:" + "1" * 64
SECRET_DATA = {
    "aiops-runtime-secret": {
        "AIOPS_BOOTSTRAP_ADMIN_PASSWORD": "YWRtaW4tcGFzcw==",
        "AIOPS_ALERTMANAGER_WEBHOOK_TOKEN": "d2ViaG9vay10b2tlbg==",
    },
    "aiops-model-encryption": {"key": "bW9kZWwta2V5"},
    "aiops-notification-encryption": {"key": "bm90aWZpY2F0aW9uLWtleQ=="},
    "aiops-change-encryption": {"key": "Y2hhbmdlLWtleQ=="},
}


def _identity() -> tuple[dict, str]:
    config = {
        "clusters": [
            {
                "name": "clean",
                "cluster": {
                    "server": "https://10.0.0.1:6443",
                    "certificate-authority-data": "public-ca-data",
                },
            }
        ]
    }
    identity = {
        "server": config["clusters"][0]["cluster"]["server"],
        "certificate_authority_data": "public-ca-data",
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return config, digest


def _evidence(tmp_path: Path) -> AcceptanceEvidence:
    _, cluster_digest = _identity()
    return create_evidence(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-cluster-test",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v3",
        kube_context="clean",
        cluster_identity_sha256=cluster_digest,
        access_profile="http_nodeport",
        now=lambda: "2026-07-14T01:02:03Z",
        attestation_verifier=lambda _item: None,
    )


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "release"
    release.mkdir()
    resources = [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "aiops-change-executor"},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "aiops-gateway", "namespace": "aiops-system"},
            "spec": {"template": {"spec": {"containers": [{"name": "gateway", "image": IMAGE}]}}},
        },
    ]
    (release / "manifest.yaml").write_text(
        "---\n".join(yaml.safe_dump(resource) for resource in resources), encoding="utf-8"
    )
    return release


def _advance(evidence: AcceptanceEvidence, gate_id: str) -> None:
    for predecessor in A01_GATE_SEQUENCE[: A01_GATE_SEQUENCE.index(gate_id)]:
        evidence.start_gate(predecessor)
        evidence.record_gate(
            predecessor,
            "not_applicable" if predecessor == "I04" else "passed",
            [],
        )


class InstallCommands:
    def __init__(self) -> None:
        self.secret_reads = 0
        self.calls: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs) -> CommandResult:
        command = tuple(command)
        self.calls.append(command)
        if command[:3] == ("kubectl", "apply", "-k"):
            return CommandResult(command, 0, "applied", "", 0.2)
        if command[:2] == ("kubectl", "wait"):
            return CommandResult(command, 0, "condition met", "", 0.2)
        if command[:3] == ("kubectl", "rollout", "status"):
            return CommandResult(command, 0, "daemonset rolled out", "", 0.2)
        if command[:4] == ("kubectl", "-n", "aiops-system", "get"):
            resource = command[4]
            if resource == "job":
                body = {"items": [{"metadata": {"name": "aiops-bootstrap", "uid": "job-uid"}, "status": {"conditions": [{"type": "Complete", "status": "True"}]}}]}
            elif resource == "pvc":
                body = {"items": [{"metadata": {"name": f"pvc-{i}", "uid": f"pvc-{i}"}, "status": {"phase": "Bound"}} for i in range(6)]}
            elif resource == "deployment":
                body = {"items": [{"metadata": {"name": "aiops-gateway", "uid": "deploy-uid"}, "spec": {"template": {"spec": {"containers": [{"name": "gateway", "image": IMAGE}]}}}, "status": {"conditions": [{"type": "Available", "status": "True"}]}}]}
            elif resource == "daemonset":
                body = {"items": [{"metadata": {"name": "aiops-alloy", "uid": "daemonset-uid", "generation": 1}, "status": {"observedGeneration": 1, "desiredNumberScheduled": 1, "numberReady": 1}}]}
            elif resource == "secret":
                self.secret_reads += 1
                body = {"items": [{"metadata": {"name": name, "uid": f"uid-{name}"}, "data": data} for name, data in SECRET_DATA.items()]}
            elif resource == "configmap":
                body = {"metadata": {"name": "aiops-bootstrap-state", "uid": "marker-uid"}, "immutable": True, "data": {"status": "complete"}}
            elif resource == "events":
                body = {"items": []}
            else:
                raise AssertionError(command)
            return CommandResult(command, 0, json.dumps(body), "", 0.1)
        raise AssertionError(command)


def test_i01_and_i02_install_then_reapply_without_persisting_secret_values(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "I01")
    commands = InstallCommands()
    runner = ClusterInstallRunner(evidence=evidence, commands=commands, sleep=lambda _seconds: None)
    release = _release(tmp_path)

    runner.run_i01(release)
    runner.run_i02(release)

    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["I01"][0]["status"] == "passed"
    assert manifest["gates"]["I02"][0]["status"] == "passed"
    assert (evidence.root / "01-install/I01-attempt-1/command-index.json").is_file()
    assert (evidence.root / "01-install/I02-attempt-1/command-index.json").is_file()
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in evidence.root.rglob("*")
        if path.is_file()
    )
    for encoded in {value for data in SECRET_DATA.values() for value in data.values()}:
        assert encoded not in persisted
    assert commands.secret_reads == 2
    daemonset_wait = next(
        command for command in commands.calls
        if command[:4] == ("kubectl", "rollout", "status", "daemonset")
    )
    assert "--all" not in daemonset_wait
