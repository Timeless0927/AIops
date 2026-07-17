"""Format-v3 ledger creation kept inside the Acceptance Evidence Module."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .gate_contract import PHASE_DIRECTORIES


def create(
    owner: type,
    parent: Path,
    *,
    acceptance_id: str,
    release_version: str,
    release_sha256: str,
    acceptance_tool_sha256: str,
    gate_contract_revision: str,
    kube_context: str,
    cluster_identity_sha256: str,
    access_profile: str,
    environment_qualification: dict[str, Any],
    now: Callable[[], str],
    new_execution_id: Callable[[], str],
    attestation_verifier: Callable[[dict[str, Any]], None] | None,
) -> Any:
    owner._validate_create_inputs(
        acceptance_id=acceptance_id,
        release_sha256=release_sha256,
        acceptance_tool_sha256=acceptance_tool_sha256,
        gate_contract_revision=gate_contract_revision,
        cluster_identity_sha256=cluster_identity_sha256,
        access_profile=access_profile,
    )
    root = parent / acceptance_id
    if (root / "manifest.json").exists():
        existing = owner.open(
            root, now=now, new_execution_id=new_execution_id,
            attestation_verifier=attestation_verifier,
        )
        existing.verify_identity(
            release_version=release_version, release_sha256=release_sha256,
            acceptance_tool_sha256=acceptance_tool_sha256,
            gate_contract_revision=gate_contract_revision, kube_context=kube_context,
            cluster_identity_sha256=cluster_identity_sha256,
            access_profile=access_profile,
            environment_qualification_sha256=environment_qualification["bundle_sha256"],
        )
        return existing
    owner._validate_qualification(
        environment_qualification, at=now(), verifier=attestation_verifier,
        release_sha256=release_sha256,
        acceptance_tool_sha256=acceptance_tool_sha256,
        gate_contract_revision=gate_contract_revision, kube_context=kube_context,
        cluster_identity_sha256=cluster_identity_sha256,
        access_profile=access_profile,
    )
    root.mkdir(parents=True, exist_ok=False)
    for phase in PHASE_DIRECTORIES:
        (root / phase).mkdir()
    manifest = {
        "format_version": 3,
        "acceptance_id": acceptance_id,
        "created_at": now(),
        "release": {"version": release_version, "sha256": release_sha256},
        "acceptance_tool": {"sha256": acceptance_tool_sha256},
        "gate_contract_revision": gate_contract_revision,
        "cluster": {
            "kube_context": kube_context,
            "identity_sha256": cluster_identity_sha256,
        },
        "access_profile": access_profile,
        "environment_qualification": environment_qualification,
        "environment_qualification_sha256": environment_qualification["bundle_sha256"],
        "gates": {},
        "identity_violations": [],
    }
    instance = owner(root, manifest, now, new_execution_id, attestation_verifier)
    instance._persist_manifest()
    return instance
