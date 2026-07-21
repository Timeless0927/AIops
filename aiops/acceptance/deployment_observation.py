"""Read-only existing-deployment identity observation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .cluster_install import ClusterInstallRunner
from .command import CommandExecutor
from .freeze import verify_final_checksums
from .package_install import (
    PackageInstallRunner,
    release_image_inventory,
    sha256,
)


def observe_existing_deployment(
    source: Any,
    commands: CommandExecutor,
    archive: Path,
    checksums: Path,
    *,
    rendered_manifest_sha256: str,
    deployment_images_sha256: str,
    observed_at: Callable[[], str],
) -> dict[str, object]:
    """Verify the frozen release against the live Cluster without applying it."""
    with TemporaryDirectory(prefix="aiops-continuation-") as temporary:
        release = PackageInstallRunner(
            evidence=source, commands=commands,
        ).prepare_release(archive, checksums, work_dir=Path(temporary))
        if sha256(release / "manifest.yaml") != rendered_manifest_sha256:
            raise ValueError("replacement rendered manifest drifted; rebuild required")
        images = sorted(release_image_inventory(release))
        if _sha256(images) != deployment_images_sha256:
            raise ValueError("replacement deployment images drifted; rebuild required")
        observed = ClusterInstallRunner(
            evidence=source, commands=commands,
        ).observe_existing(release)
    return {
        "product_sha256": source.candidate_sha256,
        "cluster_identity_sha256": observed["cluster_identity_sha256"],
        "rendered_manifest_sha256": rendered_manifest_sha256,
        "deployment_images_sha256": deployment_images_sha256,
        "deployment_configuration_sha256": _sha256(observed["configuration"]),
        "health_snapshot_sha256": _sha256({
            "objects": observed["objects"],
            "bootstrap": observed["bootstrap"],
            "workloads": observed["workloads"],
        }),
        "manifest_diff_sha256": hashlib.sha256(
            observed["manifest_diff"].stdout.encode()
        ).hexdigest(),
        "manifest_diff_exit_code": observed["manifest_diff"].exit_code,
        "manifest_diff_server_generation_only": observed["server_generation_only"],
        "healthy": True,
        "observed_at": observed_at(),
    }


def replacement_release_paths(freeze_root: Path) -> tuple[Path, Path]:
    verify_final_checksums(freeze_root)
    try:
        record = json.loads((freeze_root / "freeze-record.json").read_text())
        artifacts = record["artifacts"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("replacement freeze record is invalid") from exc
    if not isinstance(artifacts, dict):
        raise ValueError("replacement freeze record is invalid")
    paths: list[Path] = []
    for name in ("pilot_release", "pilot_release_checksums"):
        item = artifacts.get(name)
        relative = Path(str(item.get("path", ""))) if isinstance(item, dict) else Path()
        path = freeze_root / relative
        if (
            relative.is_absolute() or ".." in relative.parts or relative == Path()
            or path.is_symlink() or not path.is_file()
        ):
            raise ValueError("replacement freeze product artifact is invalid")
        paths.append(path)
    return paths[0], paths[1]


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    return hashlib.sha256(encoded).hexdigest()
