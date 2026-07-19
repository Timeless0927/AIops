"""Shared signed deployment-precondition fixtures for acceptance ledger tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiops.acceptance.evidence import AcceptanceEvidence
from aiops.acceptance.evidence_files import sha256_bytes
from aiops.acceptance.gate_contract import EVIDENCE_FORMAT_VERSION


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
        "evidence_format_version": EVIDENCE_FORMAT_VERSION,
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
        "cleanup": {"namespace_absent": True, "exit_code": 0, "proof": "deleted"},
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


def qualified_continuation(
    *,
    release_sha256: str,
    acceptance_tool_sha256: str,
    gate_contract_revision: str,
    kube_context: str,
    cluster_identity_sha256: str,
    access_profile: str,
) -> dict[str, Any]:
    created_at = "2026-07-01T00:00:00Z"
    expires_at = "2026-08-01T00:00:00Z"
    record = {
        "format_version": 1,
        "epoch_id": "continuation-test",
        "source": {
            "acceptance_id": "source-no-promote",
            "failed_gate": "I05",
            "seal_sha256": "1" * 64,
            "diagnostic_sha256": "2" * 64,
            "product_sha256": release_sha256,
            "acceptance_tool_sha256": "3" * 64,
            "failure_attribution": "inconclusive",
            "rendered_manifest_sha256": "4" * 64,
            "deployment_images_sha256": "5" * 64,
            "issued_operation_ids": ["request-user-1"],
        },
        "replacement": {
            "product_sha256": release_sha256,
            "acceptance_tool_sha256": acceptance_tool_sha256,
            "gate_contract_revision": gate_contract_revision,
            "evidence_format_version": 4,
        },
        "cluster": {
            "kube_context": kube_context,
            "identity_sha256": cluster_identity_sha256,
        },
        "access_profile": access_profile,
        "deployment_identity": {
            "product_sha256": release_sha256,
            "cluster_identity_sha256": cluster_identity_sha256,
            "rendered_manifest_sha256": "4" * 64,
            "deployment_images_sha256": "5" * 64,
            "deployment_configuration_sha256": "6" * 64,
            "health_snapshot_sha256": "7" * 64,
            "manifest_diff_sha256": sha256_bytes(b""),
            "manifest_diff_exit_code": 0,
            "manifest_diff_server_generation_only": False,
            "healthy": True,
            "observed_at": created_at,
        },
        "diagnosed_attribution": "tool_failure",
        "reconciliations": [{
            "operation_id": "request-user-1",
            "request_id": "request-user-1",
            "object_identity": {"user.id": "user-1"},
            "revision_identity": {"user.updated_at": "2026-07-01T00:00:00Z"},
            "outcome": "succeeded",
            "unknown_side_effects": False,
            "irreversible_side_effects": False,
        }],
        "environment_contaminated": False,
        "disposition": "retain_existing",
        "created_at": created_at,
        "expires_at": expires_at,
    }
    record_sha = sha256_bytes(_json_bytes(record))
    statement = {
        "epoch_id": record["epoch_id"],
        "record_sha256": record_sha,
        "source": record["source"],
        "replacement": record["replacement"],
        "cluster": record["cluster"],
        "access_profile": access_profile,
        "deployment_identity": record["deployment_identity"],
        "disposition": "retain_existing",
        "created_at": created_at,
        "expires_at": expires_at,
        "actor": "operator@example.test",
        "role": "platform_operator",
        "conclusion": "retain_existing",
        "signed_at": created_at,
        "note": "reviewed bounded continuation and reconciliation facts",
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
    if "deployment_continuation" not in kwargs:
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
