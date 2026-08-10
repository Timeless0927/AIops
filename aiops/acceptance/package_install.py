"""Pilot Release identity and frozen admission gates."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any, Callable

import yaml

from .command import CommandExecutor
from .ledger import AcceptanceLedger
from .evidence_types import Artifact, GateResult
from .integration_support import fail_gate
from .release_inventory import build_release_inventory
from .tool_artifact import self_check


IMAGE_PATTERN = re.compile(r"^[^:@\s]+(?:/[^:@\s]+)+@sha256:([0-9a-f]{64})$")
ARCHIVE_PATTERN = re.compile(r"^aiops-pilot-(v\d+\.\d+\.\d+)\.tar\.gz$")
FORBIDDEN_KINDS = {
    "CustomResourceDefinition",
    "HelmRelease",
    "AlertmanagerConfig",
    "PrometheusRule",
    "ServiceMonitor",
}
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_connector_identity(release: Path) -> tuple[str, str]:
    resources = [
        item
        for item in yaml.safe_load_all((release / "manifest.yaml").read_text(encoding="utf-8"))
        if item
    ]
    runtime = next(
        (
            item
            for item in resources
            if item.get("kind") == "ConfigMap"
            and item.get("metadata", {}).get("name") == "aiops-runtime-config"
        ),
        None,
    )
    if runtime is None:
        raise ValueError("release is missing aiops-runtime-config")
    data = runtime.get("data", {})
    connector_id = str(data.get("AIOPS_CONNECTOR_ID", ""))
    cluster_id = str(data.get("AIOPS_CLUSTER_ID", ""))
    if not connector_id or not cluster_id:
        raise ValueError("release Connector/Cluster identity is incomplete")
    return connector_id, cluster_id


def release_image_inventory(release: Path) -> set[str]:
    resources = [
        item
        for item in yaml.safe_load_all((release / "manifest.yaml").read_text(encoding="utf-8"))
        if item
    ]
    return PackageInstallRunner._validate_resources(resources)


class PackageInstallRunner:
    def __init__(self, *, evidence: AcceptanceLedger, commands: CommandExecutor) -> None:
        self.evidence = evidence
        self.commands = commands

    def run_p01(self, archive: Path, checksums: Path, *, work_dir: Path) -> Path:
        started_at = self.evidence.start_gate("P01")
        artifacts: list[Artifact] = []
        try:
            release = self._verify_and_extract(archive, checksums, work_dir)
            manifest_path = release / "manifest.yaml"
            manifest_text = manifest_path.read_text(encoding="utf-8")
            resources = [item for item in yaml.safe_load_all(manifest_text) if item]
            images = self._validate_resources(resources)
            metadata = json.loads((release / "release.json").read_text(encoding="utf-8"))
            self._validate_contract(metadata, archive, images)
            inventory = build_release_inventory(release)
            artifacts.extend(
                [
                    self.evidence.write_text(
                        "P01",
                        "checksum.txt",
                        f"{sha256(archive)}  {archive.name}\nverified=true\n",
                    ),
                    self.evidence.write_json("P01", "artifact-inventory.json", inventory),
                    self.evidence.write_text(
                        "P01", "rendered-manifest.yaml", manifest_text
                    ),
                    self.evidence.write_json("P01", "image-list.json", sorted(images)),
                    self.evidence.write_json(
                        "P01",
                        "release-contract.json",
                        {
                            "release_version": metadata["release_version"],
                            "format_version": metadata["format_version"],
                            "archive_sha256": sha256(archive),
                            "top_level": release.name,
                            "local_kustomize_only": True,
                            "fake_backends": False,
                        },
                    ),
                ]
            )
            self.evidence.record_gate("P01", GateResult("passed", tuple(artifacts)), started_at=started_at)
            return release
        except Exception as exc:
            fail_gate(self.evidence, "P01", artifacts, exc, (), started_at)

    def prepare_release(self, archive: Path, checksums: Path, *, work_dir: Path) -> Path:
        """Re-extract an already-recorded candidate without creating a new gate attempt."""
        release = self._verify_and_extract(archive, checksums, work_dir)
        images = release_image_inventory(release)
        metadata = json.loads((release / "release.json").read_text(encoding="utf-8"))
        self._validate_contract(metadata, archive, images)
        return release

    def run_p02(
        self,
        archive: Path,
        acceptance_tool: Path,
        *,
        admission_verifier: Callable[[dict[str, Any]], None],
    ) -> None:
        started_at = self.evidence.start_gate("P02")
        artifacts: list[Artifact] = []
        try:
            if sha256(archive) != self.evidence.candidate_sha256:
                raise ValueError("P02 Pilot Release Bundle identity drifted")
            result = self_check(
                acceptance_tool,
                release_sha256=self.evidence.candidate_sha256,
                acceptance_tool_sha256=self.evidence.acceptance_tool_sha256,
                gate_contract_revision=self.evidence.gate_contract_revision,
                verifier=admission_verifier,
            )
            artifacts.append(
                self.evidence.write_json("P02", "admission-self-check.json", result)
            )
            self.evidence.record_gate("P02", GateResult("passed", tuple(artifacts)), started_at=started_at)
        except Exception as exc:
            fail_gate(self.evidence, "P02", artifacts, exc, (), started_at)

    def _verify_and_extract(
        self, archive: Path, checksums: Path, work_dir: Path
    ) -> Path:
        match = ARCHIVE_PATTERN.fullmatch(archive.name)
        if not match:
            raise ValueError("archive name must be aiops-pilot-vX.Y.Z.tar.gz")
        checksum_lines = [line for line in checksums.read_text(encoding="utf-8").splitlines() if line]
        expected_line = f"{sha256(archive)}  {archive.name}"
        if checksum_lines != [expected_line]:
            raise ValueError("SHA256SUMS does not exactly match the candidate archive")
        if sha256(archive) != self.evidence.candidate_sha256:
            raise ValueError("archive does not match the acceptance candidate identity")
        expected_root = archive.name.removesuffix(".tar.gz")
        work_dir.mkdir(parents=True, exist_ok=True)
        release = work_dir / expected_root
        if release.exists():
            shutil.rmtree(release)
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if not members or sum(member.size for member in members) > 100 * 1024 * 1024:
                raise ValueError("archive is empty or exceeds the extraction limit")
            top_levels = set()
            member_names: set[str] = set()
            for member in members:
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ValueError("archive contains an unsafe path")
                if not (member.isfile() or member.isdir()):
                    raise ValueError("archive may contain only regular files and directories")
                if member.name in member_names:
                    raise ValueError("archive contains duplicate paths")
                member_names.add(member.name)
                top_levels.add(path.parts[0])
            if top_levels != {expected_root}:
                raise ValueError("archive must contain one version-matched top-level directory")
            bundle.extractall(work_dir, filter="data")
        for required in ("manifest.yaml", "kustomization.yaml", "release.json"):
            if not (release / required).is_file():
                raise ValueError(f"release is missing {required}")
        self._validate_local_kustomizations(release)
        return release

    @staticmethod
    def _validate_local_kustomizations(release: Path) -> None:
        for path in release.rglob("kustomization.yaml"):
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            for resource in value.get("resources", []):
                if not isinstance(resource, str):
                    raise ValueError("Kustomize resource must be a local string path")
                candidate = (path.parent / resource).resolve()
                if not candidate.is_relative_to(release.resolve()) or not candidate.exists():
                    raise ValueError(f"Kustomize resource is remote or missing: {resource}")

    @staticmethod
    def _validate_resources(resources: list[dict[str, Any]]) -> set[str]:
        images: set[str] = set()
        for resource in resources:
            kind = str(resource.get("kind", ""))
            name = str(resource.get("metadata", {}).get("name", ""))
            if kind in FORBIDDEN_KINDS or kind.endswith("Operator"):
                raise ValueError(f"forbidden release resource kind: {kind}")
            marker = f"{kind}/{name}".lower()
            if re.search(r"(?:^|[-_/])(fake|mock|synthetic)(?:$|[-_/])", marker):
                raise ValueError(f"synthetic backend is forbidden: {kind}/{name}")
            if kind == "Secret" and (resource.get("data") or resource.get("stringData")):
                raise ValueError(f"release manifest contains Secret values: {name}")
            if kind not in {"Deployment", "DaemonSet", "StatefulSet", "Job"}:
                continue
            pod = resource.get("spec", {}).get("template", {}).get("spec", {})
            for container in [*pod.get("initContainers", []), *pod.get("containers", [])]:
                image = str(container.get("image", ""))
                digest = IMAGE_PATTERN.fullmatch(image)
                if not digest or digest.group(1) == "0" * 64:
                    raise ValueError(f"workload image is mutable or invalid: {image}")
                if re.search(r"(?:^|[-_/])(fake|mock|synthetic)(?:$|[-_/])", image.lower()):
                    raise ValueError(f"synthetic backend image is forbidden: {image}")
                images.add(image)
        if not images:
            raise ValueError("release contains no workload images")
        return images

    @staticmethod
    def _validate_contract(metadata: dict[str, Any], archive: Path, images: set[str]) -> None:
        version = ARCHIVE_PATTERN.fullmatch(archive.name).group(1)  # type: ignore[union-attr]
        if metadata.get("format_version") != 1 or metadata.get("release_version") != version:
            raise ValueError("release metadata version does not match archive")
        inventory = metadata.get("images")
        if not isinstance(inventory, dict) or not images <= set(inventory.values()):
            raise ValueError("release image inventory does not cover rendered workloads")
