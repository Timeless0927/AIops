"""Unsigned audit handoff for a sealed, effect-free evaluator failure."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .evidence_files import atomic_write, sha256, sha256_bytes
from .gate_contract import EVIDENCE_FORMAT_VERSION, GATE_CONTRACT_REVISION
from .gate_reuse import freeze_reuse_plan, validate_reusable_gates

if TYPE_CHECKING:
    from .ledger import AcceptanceLedger


FORMAT = "evaluator_successor_v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REUSABLE = ("P01", "I02", "I03", "I04")
_SOURCE_FIELDS = {
    "acceptance_id", "failed_gate", "failure_attribution",
    "diagnosed_failure_attribution", "seal_sha256",
    "product_sha256", "acceptance_tool_sha256", "issued_operation_ids",
    "reconciled_operation_ids", "deployment_precondition_sha256",
}
_REPLACEMENT_FIELDS = {
    "product_sha256", "acceptance_tool_sha256", "gate_contract_revision",
    "evidence_format_version",
}
_RECORD_FIELDS = {
    "policy_revision", "source", "replacement", "cluster", "access_profile",
    "diagnostic_manifest_sha256", "diagnostic_conclusion_sha256",
    "reusable_gates", "created_at",
}


def create(
    source: AcceptanceLedger,
    *,
    diagnostic: Path,
    acceptance_tool: Path,
    output: Path,
    now: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
) -> Path:
    """Create a checksummed handoff without granting deployment authority."""
    failure = source.failure_summary()
    if (
        not source.is_sealed
        or source.status().get("status") != "sealed"
        or failure.get("decision") != "no_promote"
        or failure.get("gate_id") != "S01"
    ):
        raise ValueError("evaluator successor requires a sealed S01 no-promote source")
    if failure.get("failure_attribution") == "product_failure":
        raise ValueError("diagnosed Product failure requires rebuild")
    attempt = source._manifest["gates"]["S01"][0]
    if (
        len(attempt.get("operations", [])) != 1
        or attempt["operations"][0].get("kind") != "gate_execution"
        or attempt.get("reconciliations")
    ):
        raise ValueError("evaluator successor cannot cover a failed external operation")
    diagnostic_manifest, diagnostic_conclusion = _diagnostic(source, diagnostic)
    target_tool_sha = sha256(acceptance_tool)
    if target_tool_sha == source.acceptance_tool_sha256:
        raise ValueError("evaluator successor requires a changed acceptance tool")
    external_operations = {
        item["operation_id"]
        for attempts in source._manifest["gates"].values()
        for attempt_value in attempts
        for item in attempt_value.get("operations", [])
        if item.get("kind") != "gate_execution"
    }
    reconciled = {
        item["operation_id"]
        for attempts in source._manifest["gates"].values()
        for attempt_value in attempts
        for item in attempt_value.get("reconciliations", [])
        if item.get("outcome") == "succeeded"
        and item.get("operation_id") in external_operations
    }
    plan = freeze_reuse_plan(
        source,
        [
            {
                "gate_id": gate_id,
                "operation_ids": [
                    item["operation_id"]
                    for item in source.terminal_gate_fact(gate_id)["operations"]
                    if item["kind"] != "gate_execution"
                ],
            }
            for gate_id in _REUSABLE
        ],
        failed_gate="S01",
        reconciled=reconciled,
    )
    record = {
        "policy_revision": FORMAT,
        "source": {
            "acceptance_id": failure["acceptance_id"],
            "failed_gate": failure["gate_id"],
            "failure_attribution": failure["failure_attribution"],
            "diagnosed_failure_attribution": "tool_failure",
            "seal_sha256": sha256(source.root / "SHA256SUMS"),
            "product_sha256": source.candidate_sha256,
            "acceptance_tool_sha256": source.acceptance_tool_sha256,
            "issued_operation_ids": failure["issued_operation_ids"],
            "reconciled_operation_ids": sorted(reconciled),
            "deployment_precondition_sha256": source.deployment_precondition_sha256,
        },
        "replacement": {
            "product_sha256": source.candidate_sha256,
            "acceptance_tool_sha256": target_tool_sha,
            "gate_contract_revision": source.gate_contract_revision,
            "evidence_format_version": EVIDENCE_FORMAT_VERSION,
        },
        "cluster": {
            "kube_context": source.kube_context,
            "identity_sha256": source.cluster_identity_sha256,
        },
        "access_profile": source.access_profile,
        "diagnostic_manifest_sha256": sha256(diagnostic_manifest),
        "diagnostic_conclusion_sha256": sha256(diagnostic_conclusion),
        "reusable_gates": plan,
        "created_at": now(),
    }
    unsigned = {"format": FORMAT, "record": record}
    bundle = {**unsigned, "bundle_sha256": sha256_bytes(_json_bytes(unsigned))}
    validate(bundle)
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, _json_bytes(bundle), staging_dir=output.parent)
    return output


def inspect(path: Path, **identity: Any) -> dict[str, Any]:
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("evaluator successor bundle is invalid") from exc
    validate(bundle, **identity)
    return bundle


def is_bundle(path: Path) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("format") == FORMAT
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def validate(value: object, **identity: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"format", "record", "bundle_sha256"}:
        raise ValueError("evaluator successor bundle is invalid")
    unsigned = {"format": value.get("format"), "record": value.get("record")}
    record = value.get("record")
    if (
        value.get("format") != FORMAT
        or value.get("bundle_sha256") != sha256_bytes(_json_bytes(unsigned))
        or not isinstance(record, dict)
        or set(record) != _RECORD_FIELDS
        or record.get("policy_revision") != FORMAT
    ):
        raise ValueError("evaluator successor bundle is invalid")
    source = record.get("source")
    replacement = record.get("replacement")
    cluster = record.get("cluster")
    if (
        not isinstance(source, dict)
        or set(source) != _SOURCE_FIELDS
        or source.get("failed_gate") != "S01"
        or source.get("failure_attribution") not in {
            "tool_failure", "environment_failure", "inconclusive"
        }
        or source.get("diagnosed_failure_attribution") != "tool_failure"
        or not isinstance(replacement, dict)
        or set(replacement) != _REPLACEMENT_FIELDS
        or replacement.get("gate_contract_revision") != GATE_CONTRACT_REVISION
        or replacement.get("evidence_format_version") != EVIDENCE_FORMAT_VERSION
        or source.get("product_sha256") != replacement.get("product_sha256")
        or source.get("acceptance_tool_sha256") == replacement.get("acceptance_tool_sha256")
        or not isinstance(cluster, dict)
        or set(cluster) != {"kube_context", "identity_sha256"}
        or record.get("access_profile") not in {"http_nodeport", "https_ingress"}
        or not isinstance(record.get("created_at"), str)
        or any(
            _SHA256.fullmatch(str(item)) is None
            for item in (
                source.get("seal_sha256"), source.get("product_sha256"),
                source.get("acceptance_tool_sha256"),
                source.get("deployment_precondition_sha256"),
                replacement.get("acceptance_tool_sha256"), cluster.get("identity_sha256"),
                record.get("diagnostic_manifest_sha256"),
                record.get("diagnostic_conclusion_sha256"),
            )
        )
    ):
        raise ValueError("evaluator successor record is invalid")
    issued = source.get("issued_operation_ids")
    reconciled = source.get("reconciled_operation_ids")
    if (
        not isinstance(source.get("acceptance_id"), str)
        or not isinstance(issued, list)
        or not isinstance(reconciled, list)
        or any(not isinstance(item, str) for item in issued + reconciled)
        or not set(reconciled) <= set(issued)
    ):
        raise ValueError("evaluator successor operation accounting is invalid")
    validate_reusable_gates(
        record.get("reusable_gates"), failed_gate="S01", reconciled=set(reconciled)
    )
    expected = {
        "release_sha256": replacement["product_sha256"],
        "acceptance_tool_sha256": replacement["acceptance_tool_sha256"],
        "gate_contract_revision": replacement["gate_contract_revision"],
        "kube_context": cluster["kube_context"],
        "cluster_identity_sha256": cluster["identity_sha256"],
        "access_profile": record["access_profile"],
    }
    if any(identity.get(key, expected[key]) != expected[key] for key in expected):
        raise ValueError("evaluator successor replacement identity drifted")


def _diagnostic(source: AcceptanceLedger, root: Path) -> tuple[Path, Path]:
    manifest_path = root / "manifest.json"
    conclusion_path = root / "conclusion.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        conclusion = json.loads(conclusion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("evaluator successor diagnostic is invalid") from exc
    if (
        manifest.get("source_acceptance_id") != source._manifest["acceptance_id"]
        or manifest.get("source_failed_gate") != "S01"
        or manifest.get("release_sha256") != source.candidate_sha256
        or manifest.get("acceptance_tool_sha256") != source.acceptance_tool_sha256
        or conclusion.get("diagnosed_attribution") != "tool_failure"
        or conclusion.get("operation_accounting_complete") is not True
        or conclusion.get("recovered_operation_ids") != []
    ):
        raise ValueError("evaluator successor diagnostic does not match the source")
    return manifest_path, conclusion_path


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
