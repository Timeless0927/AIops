from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from aiops.acceptance.command import CommandResult
from aiops.acceptance.cluster_install import ClusterInstallRunner
from aiops.acceptance.environment_qualification import EnvironmentQualification
from aiops.acceptance.evidence import AcceptanceEvidence, EvidenceError
from aiops.acceptance.evidence_files import sha256
from aiops.acceptance.gate_contract import GATE_CONTRACT_REVISION, GATE_SEQUENCE


IMAGE = "registry.example.test/aiops/gateway@sha256:" + "1" * 64
NOW = datetime(2026, 7, 17, 1, 2, 3, tzinfo=timezone.utc)


def _cluster_identity() -> tuple[dict, str]:
    config = {
        "clusters": [{"cluster": {
            "server": "https://10.0.0.1:6443",
            "certificate-authority-data": "public-ca-data",
        }}]
    }
    identity = {
        "server": "https://10.0.0.1:6443",
        "certificate_authority_data": "public-ca-data",
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return config, digest


def _freeze(tmp_path: Path) -> Path:
    root = tmp_path / "freeze"
    product = root / "product"
    product.mkdir(parents=True)
    archive = product / "aiops-pilot-v0.1.0.tar.gz"
    release_root = "aiops-pilot-v0.1.0"
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
            "spec": {"template": {"spec": {"containers": [
                {"name": "gateway", "image": IMAGE}
            ]}}},
        },
    ]
    manifest = "---\n".join(yaml.safe_dump(item) for item in resources).encode()
    with tarfile.open(archive, "w:gz") as bundle:
        for name, content in {
            f"{release_root}/manifest.yaml": manifest,
            f"{release_root}/release.json": b'{"release_version":"v0.1.0"}',
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))
    checksums = product / "SHA256SUMS"
    checksums.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
    tool = root / "acceptance-tool-v1.tar.gz"
    tool.write_bytes(b"acceptance-tool-v3")
    record = {
        "format_version": 1,
        "contracts": {
            "gate_contract_revision": GATE_CONTRACT_REVISION,
            "evidence_format_version": 3,
            "environment_qualification_format_version": 1,
        },
        "artifacts": {
            "pilot_release": {
                "path": str(archive.relative_to(root)), "sha256": sha256(archive),
            },
            "pilot_release_checksums": {
                "path": str(checksums.relative_to(root)), "sha256": sha256(checksums),
            },
            "acceptance_tool": {
                "path": tool.name, "sha256": sha256(tool),
            },
        },
        "release": {"archive_sha256": sha256(archive)},
    }
    freeze_record = root / "freeze-record.json"
    freeze_record.write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    files = sorted(path for path in root.rglob("*") if path.is_file())
    (root / "SHA256SUMS").write_text(
        "".join(f"{sha256(path)}  {path.relative_to(root)}\n" for path in files),
        encoding="utf-8",
    )
    return root


class QualificationCommands:
    def __init__(
        self, *, dirty_cluster: bool = False, image_pull_failure: bool = False,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.config, _ = _cluster_identity()
        self.dirty_cluster = dirty_cluster
        self.image_pull_failure = image_pull_failure

    def run(self, command, *, stdin=None, **_kwargs) -> CommandResult:
        command = tuple(command)
        self.calls.append((command, stdin))
        if command == ("kubectl", "config", "current-context"):
            return CommandResult(command, 0, "clean\n", "", 0.1)
        if command[:4] == ("kubectl", "config", "view", "--minify"):
            return CommandResult(command, 0, json.dumps(self.config), "", 0.1)
        if command[:3] == ("kubectl", "get", "namespace"):
            return CommandResult(command, 1, "", "NotFound", 0.1)
        if command[:3] == ("kubectl", "get", "clusterrole"):
            return CommandResult(
                command,
                0 if self.dirty_cluster else 1,
                "clusterrole/aiops-change-executor" if self.dirty_cluster else "",
                "" if self.dirty_cluster else "NotFound",
                0.1,
            )
        if command[:4] == ("kubectl", "get", "services", "--all-namespaces"):
            return CommandResult(command, 0, '{"items":[]}', "", 0.1)
        if command[:3] == ("kubectl", "get", "storageclass"):
            return CommandResult(command, 0, json.dumps({"items": [{
                "metadata": {"name": "standard", "annotations": {
                    "storageclass.kubernetes.io/is-default-class": "true"
                }},
                "provisioner": "example.test/dynamic",
                "volumeBindingMode": "WaitForFirstConsumer",
            }]}), "", 0.1)
        if command[:3] == ("kubectl", "get", "nodes"):
            return CommandResult(command, 0, json.dumps({"items": [{
                "metadata": {"name": "node-1"},
                "spec": {"unschedulable": False},
                "status": {"conditions": [{
                    "type": "Ready", "status": "True",
                    "lastHeartbeatTime": "2026-07-17T01:02:03Z",
                }]},
            }]}), "", 0.1)
        if command[:3] == ("kubectl", "apply", "-f"):
            assert stdin and "32Gi" in stdin and "NetworkPolicy" in stdin and IMAGE in stdin
            return CommandResult(command, 0, "resources created", "", 0.2)
        if command[:3] == ("kubectl", "wait", "--for=jsonpath={.status.phase}=Bound"):
            return CommandResult(command, 0, "pvc bound", "", 0.2)
        if command[:4] == ("kubectl", "get", "pvc", "capacity-probe"):
            return CommandResult(command, 0, json.dumps({
                "status": {"capacity": {"storage": "32Gi"}}
            }), "", 0.1)
        if command[:3] == ("kubectl", "get", "networkpolicy"):
            return CommandResult(command, 0, '{"items":[{"metadata":{"name":"deny-all"}}]}', "", 0.1)
        if command[:3] == ("kubectl", "get", "pods"):
            pod = {
                "spec": {"nodeName": "node-1", "containers": [
                    {"name": "image", "image": IMAGE}
                ]},
                "status": (
                    {"phase": "Pending"}
                    if self.image_pull_failure
                    else {"containerStatuses": [{"name": "image", "imageID": IMAGE}]}
                ),
            }
            return CommandResult(command, 0, json.dumps({"items": [pod]}), "", 0.1)
        if command[:3] == ("kubectl", "delete", "namespace"):
            return CommandResult(command, 0, "deleted", "", 0.1)
        raise AssertionError(command)


def test_qualification_passes_before_any_acceptance_ledger_exists(tmp_path: Path) -> None:
    _, cluster_sha = _cluster_identity()
    qualification = EnvironmentQualification(
        tmp_path / "qualifications",
        commands=QualificationCommands(),
        now=lambda: NOW,
        new_id=lambda: "qualification-1",
        sleep=lambda _seconds: None,
    )

    path = qualification.qualify(
        _freeze(tmp_path),
        kube_context="clean",
        cluster_identity_sha256=cluster_sha,
        access_profile="http_nodeport",
        ttl_seconds=3600,
    )

    inspected = qualification.inspect(path)
    record = inspected["record"]
    assert record["qualification_id"] == "qualification-1"
    assert record["outcome"] == "passed"
    assert record["observed_at"] == "2026-07-17T01:02:03Z"
    assert record["expires_at"] == "2026-07-17T02:02:03Z"
    assert record["cleanup"] == {"namespace_absent": True, "exit_code": 0}
    assert record["facts"]["nodes"] == ["node-1"]
    assert record["facts"]["network_policy_probe"] == "created"
    assert len(record["facts"]["exact_image_pulls"]) == 1
    assert inspected["record_sha256"] == sha256(path / "record.json")
    assert not (tmp_path / "acceptance").exists()


def test_failed_qualification_is_immutable_and_retry_uses_a_new_id(tmp_path: Path) -> None:
    _, cluster_sha = _cluster_identity()
    ids = iter(("qualification-failed", "qualification-passed"))
    qualification = EnvironmentQualification(
        tmp_path / "qualifications",
        commands=QualificationCommands(dirty_cluster=True),
        now=lambda: NOW,
        new_id=lambda: next(ids),
        sleep=lambda _seconds: None,
    )
    freeze = _freeze(tmp_path)

    failed_path = qualification.qualify(
        freeze,
        kube_context="clean",
        cluster_identity_sha256=cluster_sha,
        access_profile="http_nodeport",
    )
    failed_bytes = (failed_path / "record.json").read_bytes()
    failed = qualification.inspect(failed_path)["record"]
    assert failed["outcome"] == "environment_not_ready"
    assert failed["operation"]["status"] == "terminal"
    assert failed["cleanup"] == {"namespace_absent": True, "exit_code": None}

    qualification.commands = QualificationCommands()
    passed_path = qualification.qualify(
        freeze,
        kube_context="clean",
        cluster_identity_sha256=cluster_sha,
        access_profile="http_nodeport",
    )

    assert passed_path != failed_path
    assert qualification.inspect(passed_path)["record"]["outcome"] == "passed"
    assert (failed_path / "record.json").read_bytes() == failed_bytes
    assert not (tmp_path / "acceptance").exists()


def test_exact_image_pull_failure_is_environment_not_ready_and_cleans_up(tmp_path: Path) -> None:
    _, cluster_sha = _cluster_identity()
    qualification = EnvironmentQualification(
        tmp_path / "qualifications",
        commands=QualificationCommands(image_pull_failure=True), now=lambda: NOW,
        new_id=lambda: "qualification-image-failed", sleep=lambda _seconds: None,
    )
    path = qualification.qualify(
        _freeze(tmp_path), kube_context="clean",
        cluster_identity_sha256=cluster_sha, access_profile="http_nodeport",
    )

    record = qualification.inspect(path)["record"]
    assert record["outcome"] == "environment_not_ready"
    assert "node image pull preflight did not converge" in record["failure"]
    assert record["cleanup"] == {"namespace_absent": True, "exit_code": 0}
    assert not (tmp_path / "acceptance").exists()


def _signed_qualification(
    qualification: EnvironmentQualification, path: Path,
) -> dict:
    statement = qualification.attestation_statement(
        path,
        actor="operator@example.test",
        note="reviewed clean Cluster, capacity, CNI, NodePort, nodes and exact pulls",
    )
    qualification.attach_attestation(path, {
        "statement": statement,
        "signature": "signature",
        "public_key": "ssh-ed25519 AAAATEST operator@example.test",
        "fingerprint": "SHA256:test",
    })
    return qualification.inspect(
        path, require_signed=True, verifier=lambda _item: None, now=lambda: NOW,
    )


def test_signed_fresh_qualification_is_required_before_ledger_creation(tmp_path: Path) -> None:
    _, cluster_sha = _cluster_identity()
    qualification = EnvironmentQualification(
        tmp_path / "qualifications", commands=QualificationCommands(), now=lambda: NOW,
        new_id=lambda: "qualification-signed", sleep=lambda _seconds: None,
    )
    path = qualification.qualify(
        _freeze(tmp_path), kube_context="clean",
        cluster_identity_sha256=cluster_sha, access_profile="http_nodeport",
    )
    bundle = _signed_qualification(qualification, path)

    evidence = AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-qualified",
        release_version="v0.1.0",
        release_sha256=bundle["record"]["freeze"]["product_sha256"],
        acceptance_tool_sha256=bundle["record"]["freeze"]["acceptance_tool_sha256"],
        gate_contract_revision=GATE_CONTRACT_REVISION,
        kube_context="clean",
        cluster_identity_sha256=cluster_sha,
        access_profile="http_nodeport",
        environment_qualification=bundle,
        now=lambda: "2026-07-17T01:02:03Z",
        attestation_verifier=lambda _item: None,
    )

    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["format_version"] == 3
    assert manifest["environment_qualification"]["record_sha256"] == bundle["record_sha256"]
    assert manifest["environment_qualification_sha256"] == bundle["bundle_sha256"]
    assert evidence.frontier == "P01"


def test_expired_or_wrong_identity_qualification_creates_no_ledger(tmp_path: Path) -> None:
    _, cluster_sha = _cluster_identity()
    qualification = EnvironmentQualification(
        tmp_path / "qualifications", commands=QualificationCommands(), now=lambda: NOW,
        new_id=lambda: "qualification-expiring", sleep=lambda _seconds: None,
    )
    path = qualification.qualify(
        _freeze(tmp_path), kube_context="clean",
        cluster_identity_sha256=cluster_sha, access_profile="http_nodeport",
        ttl_seconds=60,
    )
    bundle = _signed_qualification(qualification, path)

    for name, now, identity in (
        ("expired", "2026-07-17T01:03:04Z", cluster_sha),
        ("wrong-cluster", "2026-07-17T01:02:03Z", "f" * 64),
    ):
        root = tmp_path / name
        try:
            AcceptanceEvidence.create(
                root, acceptance_id=f"v0.1.0-{name}", release_version="v0.1.0",
                release_sha256=bundle["record"]["freeze"]["product_sha256"],
                acceptance_tool_sha256=bundle["record"]["freeze"]["acceptance_tool_sha256"],
                gate_contract_revision=GATE_CONTRACT_REVISION, kube_context="clean",
                cluster_identity_sha256=identity, access_profile="http_nodeport",
                environment_qualification=bundle, now=lambda now=now: now,
                attestation_verifier=lambda _item: None,
            )
        except EvidenceError:
            pass
        else:
            raise AssertionError("invalid qualification unexpectedly created a ledger")
        assert not root.exists()


def test_v3_clean_dag_removes_p03_and_starts_live_work_at_i01() -> None:
    assert GATE_CONTRACT_REVISION == "pilot-clean-acceptance-v3"
    assert GATE_SEQUENCE[:3] == ("P01", "P02", "I01")
    assert "P03" not in GATE_SEQUENCE
    assert not hasattr(ClusterInstallRunner, "run_p03")


def test_interrupted_qualification_resumes_cleanup_without_replaying_apply(tmp_path: Path) -> None:
    _, cluster_sha = _cluster_identity()
    qualification = EnvironmentQualification(
        tmp_path / "qualifications", commands=QualificationCommands(), now=lambda: NOW,
        new_id=lambda: "qualification-interrupted", sleep=lambda _seconds: None,
    )
    path = qualification.qualify(
        _freeze(tmp_path), kube_context="clean",
        cluster_identity_sha256=cluster_sha, access_profile="http_nodeport",
    )
    record = qualification.inspect(path)["record"]
    record["outcome"] = "running"
    record["operation"] = {
        "id": record["operation"]["id"], "status": "dispatched",
        "dispatched_at": record["observed_at"],
    }
    record["cleanup"] = {"namespace_absent": False, "exit_code": None}
    record["failure"] = None
    content = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    (path / "record.json").write_text(content, encoding="utf-8")
    (path / "SHA256SUMS").write_text(
        f"{sha256(path / 'record.json')}  record.json\n", encoding="utf-8"
    )

    class CleanupCommands:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, command, **_kwargs):
            command = tuple(command)
            self.calls.append(command)
            if command[:3] == ("kubectl", "get", "namespace"):
                return CommandResult(command, 0, "namespace/present", "", 0.1)
            if command[:3] == ("kubectl", "delete", "namespace"):
                return CommandResult(command, 0, "deleted", "", 0.1)
            raise AssertionError(command)

    commands = CleanupCommands()
    qualification.commands = commands
    resumed = qualification.resume_cleanup(path)["record"]

    assert resumed["outcome"] == "environment_not_ready"
    assert resumed["cleanup"] == {"namespace_absent": True, "exit_code": 0}
    assert sum(call[:3] == ("kubectl", "apply", "-f") for call in commands.calls) == 0
    assert sum(call[:3] == ("kubectl", "delete", "namespace") for call in commands.calls) == 1


def test_tamper_or_wrong_signature_is_rejected_before_init(tmp_path: Path) -> None:
    _, cluster_sha = _cluster_identity()
    qualification = EnvironmentQualification(
        tmp_path / "qualifications", commands=QualificationCommands(), now=lambda: NOW,
        new_id=lambda: "qualification-tamper", sleep=lambda _seconds: None,
    )
    path = qualification.qualify(
        _freeze(tmp_path), kube_context="clean",
        cluster_identity_sha256=cluster_sha, access_profile="http_nodeport",
    )
    _signed_qualification(qualification, path)
    with pytest.raises(ValueError, match="signature"):
        qualification.inspect(
            path, require_signed=True,
            verifier=lambda _item: (_ for _ in ()).throw(ValueError("wrong signature")),
            now=lambda: NOW,
        )
    (path / "record.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        qualification.inspect(path)


def test_qualification_must_still_be_fresh_when_i01_starts(tmp_path: Path) -> None:
    _, cluster_sha = _cluster_identity()
    qualification = EnvironmentQualification(
        tmp_path / "qualifications", commands=QualificationCommands(), now=lambda: NOW,
        new_id=lambda: "qualification-i01", sleep=lambda _seconds: None,
    )
    path = qualification.qualify(
        _freeze(tmp_path), kube_context="clean",
        cluster_identity_sha256=cluster_sha, access_profile="http_nodeport",
        ttl_seconds=60,
    )
    bundle = _signed_qualification(qualification, path)
    current = ["2026-07-17T01:02:03Z"]
    evidence = AcceptanceEvidence.create(
        tmp_path / "acceptance", acceptance_id="v0.1.0-i01-expiry",
        release_version="v0.1.0",
        release_sha256=bundle["record"]["freeze"]["product_sha256"],
        acceptance_tool_sha256=bundle["record"]["freeze"]["acceptance_tool_sha256"],
        gate_contract_revision=GATE_CONTRACT_REVISION, kube_context="clean",
        cluster_identity_sha256=cluster_sha, access_profile="http_nodeport",
        environment_qualification=bundle, now=lambda: current[0],
        attestation_verifier=lambda _item: None,
    )
    for gate_id in ("P01", "P02"):
        started_at = evidence.start_gate(gate_id)
        evidence.record_gate(gate_id, "passed", [], started_at=started_at)
    current[0] = "2026-07-17T01:03:04Z"
    with pytest.raises(EvidenceError, match="expired"):
        evidence.start_gate("I01")
