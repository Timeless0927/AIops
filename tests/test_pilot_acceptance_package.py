from __future__ import annotations

import gzip
import hashlib
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from aiops.acceptance.command import CommandResult
from aiops.acceptance.evidence import AcceptanceEvidence, GateFailed
from aiops.acceptance.package_install import (
    PackageInstallRunner,
    release_connector_identity,
)


IMAGE = "registry.example.test/aiops/gateway@sha256:" + "1" * 64


class FakeCommands:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs) -> CommandResult:
        self.calls.append(tuple(command))
        return self.results.pop(0)


def _evidence(tmp_path: Path, archive: Path | None = None) -> AcceptanceEvidence:
    return AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-package-test",
        release_version="v0.1.0",
        release_sha256=(
            hashlib.sha256(archive.read_bytes()).hexdigest() if archive else "a" * 64
        ),
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v2",
        kube_context="clean",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        now=lambda: "2026-07-14T01:02:03Z",
    )


def _package(tmp_path: Path, *, remote: bool = False, mutable: bool = False) -> tuple[Path, Path]:
    name = "aiops-pilot-v0.1.0"
    root = tmp_path / name
    root.mkdir()
    image = "registry.example.test/aiops/gateway:latest" if mutable else IMAGE
    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "aiops-gateway", "namespace": "aiops-system"},
        "spec": {
            "template": {
                "spec": {"containers": [{"name": "gateway", "image": image}]},
            }
        },
    }
    runtime = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "aiops-runtime-config", "namespace": "aiops-system"},
        "data": {"AIOPS_CONNECTOR_ID": "connector-dev", "AIOPS_CLUSTER_ID": "pilot-cluster"},
    }
    (root / "manifest.yaml").write_text(
        yaml.safe_dump_all([manifest, runtime]), encoding="utf-8"
    )
    (root / "kustomization.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": [
                    "https://example.test/remote.yaml" if remote else "manifest.yaml"
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "release.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "release_version": "v0.1.0",
                "images": {"pilot/Deployment/aiops-gateway/gateway": image},
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / f"{name}.tar.gz"
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as bundle:
                for path in [root, *sorted(root.rglob("*"))]:
                    bundle.add(path, arcname=path.relative_to(tmp_path), recursive=False)
    checksum = tmp_path / "SHA256SUMS"
    checksum.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
        encoding="utf-8",
    )
    return archive, checksum


def test_p01_verifies_and_extracts_package_with_hashed_inventory(tmp_path: Path) -> None:
    archive, checksum = _package(tmp_path)
    evidence = _evidence(tmp_path, archive)
    runner = PackageInstallRunner(evidence=evidence, commands=FakeCommands([]))

    release = runner.run_p01(archive, checksum, work_dir=tmp_path / "work")

    assert release.name == "aiops-pilot-v0.1.0"
    assert release_connector_identity(release) == ("connector-dev", "pilot-cluster")
    manifest = json.loads(evidence.manifest_path.read_text(encoding="utf-8"))
    attempt = manifest["gates"]["P01"][0]
    assert attempt["status"] == "passed"
    assert {Path(item["path"]).name for item in attempt["artifacts"]} == {
        "artifact-inventory.json",
        "checksum.txt",
        "image-list.json",
        "rendered-manifest.yaml",
        "release-contract.json",
    }


@pytest.mark.parametrize("remote,mutable", [(True, False), (False, True)])
def test_p01_records_failed_attempt_for_remote_or_mutable_package(
    tmp_path: Path, remote: bool, mutable: bool,
) -> None:
    archive, checksum = _package(tmp_path, remote=remote, mutable=mutable)
    evidence = _evidence(tmp_path, archive)
    runner = PackageInstallRunner(evidence=evidence, commands=FakeCommands([]))

    with pytest.raises(GateFailed, match="P01"):
        runner.run_p01(archive, checksum, work_dir=tmp_path / "work")

    manifest = json.loads(evidence.manifest_path.read_text(encoding="utf-8"))
    assert manifest["gates"]["P01"][0]["status"] == "failed"
    failure = evidence.root / manifest["gates"]["P01"][0]["artifacts"][-1]["path"]
    assert "Traceback" not in failure.read_text(encoding="utf-8")


def test_p01_rejects_archive_links_before_extraction(tmp_path: Path) -> None:
    archive = tmp_path / "aiops-pilot-v0.1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        link = tarfile.TarInfo("aiops-pilot-v0.1.0/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        bundle.addfile(link)
    checksum = tmp_path / "SHA256SUMS"
    checksum.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
        encoding="utf-8",
    )
    evidence = _evidence(tmp_path, archive)

    with pytest.raises(GateFailed):
        PackageInstallRunner(evidence=evidence, commands=FakeCommands([])).run_p01(
            archive, checksum, work_dir=tmp_path / "work"
        )
    assert not (tmp_path / "outside").exists()


def test_p02_runs_fixed_selectors_and_labels_them_non_live(tmp_path: Path) -> None:
    commands = FakeCommands(
        [
            CommandResult(("python3",), 0, "27 passed", "", 1.0),
            CommandResult(("npm",), 0, "18 passed", "", 2.0),
            CommandResult(("npm",), 0, "built", "", 3.0),
        ]
    )
    evidence = _evidence(tmp_path)
    evidence.start_gate("P01")
    evidence.record_gate("P01", "passed", [])

    PackageInstallRunner(evidence=evidence, commands=commands).run_p02(Path("/repo"))

    assert len(commands.calls) == 3
    assert commands.calls[0][:4] == ("python3", "-m", "pytest", "-q")
    manifest = json.loads(evidence.manifest_path.read_text(encoding="utf-8"))
    python_artifact = next(
        item
        for item in manifest["gates"]["P02"][0]["artifacts"]
        if item["path"].endswith("python-contract-tests.txt")
    )
    command_evidence = (evidence.root / python_artifact["path"]).read_text(encoding="utf-8")
    assert "release_sha256: " + "a" * 64 in command_evidence
    assert "kube_context: clean" in command_evidence
    assert "started_at:" in command_evidence and "completed_at:" in command_evidence
    artifact = evidence.root / manifest["gates"]["P02"][0]["artifacts"][-1]["path"]
    assert json.loads(artifact.read_text(encoding="utf-8"))["live_evidence"] is False
