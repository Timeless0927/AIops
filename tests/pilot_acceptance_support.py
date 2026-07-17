"""Shared signed Environment Qualification fixture for format-v3 ledger tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiops.acceptance.evidence import AcceptanceEvidence
from aiops.acceptance.evidence_files import sha256_bytes


def qualified_environment(
    *,
    release_sha256: str,
    acceptance_tool_sha256: str,
    gate_contract_revision: str,
    kube_context: str,
    cluster_identity_sha256: str,
    access_profile: str,
) -> dict[str, Any]:
    observed_at = "2026-07-01T00:00:00Z"
    expires_at = "2026-08-01T00:00:00Z"
    freeze = {
        "record_sha256": "f" * 64,
        "product_sha256": release_sha256,
        "acceptance_tool_sha256": acceptance_tool_sha256,
        "gate_contract_revision": gate_contract_revision,
        "evidence_format_version": 3,
        "environment_qualification_format_version": 1,
    }
    cluster = {
        "kube_context": kube_context,
        "identity_sha256": cluster_identity_sha256,
    }
    record = {
        "format_version": 1,
        "qualification_id": "qualification-test",
        "freeze": freeze,
        "cluster": cluster,
        "access_profile": access_profile,
        "temporary_namespace": "aiops-environment-test",
        "operation": {"id": "qualification:test:preflight", "status": "terminal"},
        "facts": {
            "nodes": ["node-test"],
            "network_policy_probe": "created",
            "pvc_capacity": "32Gi",
            "exact_image_pulls": [{
                "node": "node-test", "image": "example.test/image@sha256:" + "1" * 64,
                "image_id_sha256": "2" * 64,
            }],
        },
        "cleanup": {"namespace_absent": True, "exit_code": 0},
        "outcome": "passed",
        "failure": None,
        "observed_at": observed_at,
        "expires_at": expires_at,
    }
    record_sha = sha256_bytes(_json_bytes(record))
    statement = {
        "qualification_id": record["qualification_id"],
        "record_sha256": record_sha,
        "freeze": freeze,
        "cluster": cluster,
        "access_profile": access_profile,
        "observed_at": observed_at,
        "expires_at": expires_at,
        "actor": "operator@example.test",
        "role": "platform_operator",
        "conclusion": "passed",
        "signed_at": observed_at,
        "note": "reviewed bounded Environment Qualification facts",
    }
    unsigned = {
        "record": record,
        "record_sha256": record_sha,
        "attestation": {
            "statement": statement,
            "signature": "valid-signature",
            "public_key": "ssh-ed25519 AAAATEST operator@example.test",
            "fingerprint": "SHA256:release-owner",
        },
    }
    return {**unsigned, "bundle_sha256": sha256_bytes(_json_bytes(unsigned))}


def create_evidence(parent: Path, **kwargs: Any) -> AcceptanceEvidence:
    kwargs.setdefault("attestation_verifier", lambda _item: None)
    kwargs["environment_qualification"] = qualified_environment(
        release_sha256=kwargs["release_sha256"],
        acceptance_tool_sha256=kwargs["acceptance_tool_sha256"],
        gate_contract_revision=kwargs["gate_contract_revision"],
        kube_context=kwargs["kube_context"],
        cluster_identity_sha256=kwargs["cluster_identity_sha256"],
        access_profile=kwargs["access_profile"],
    )
    return AcceptanceEvidence.create(parent, **kwargs)


def open_evidence(root: Path, **kwargs: Any) -> AcceptanceEvidence:
    kwargs.setdefault("attestation_verifier", lambda _item: None)
    return AcceptanceEvidence.open(root, **kwargs)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
