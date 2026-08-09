"""Freeze and apply exact, no-effect gate reuse from one signed continuation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .credentials import assert_public_payload
from .evidence_types import GateResult
from .evidence_files import sha256
from .execution_journal import valid_operation_id
from .gate_contract import GATE_PHASE, GATE_REUSE_POLICIES, GATE_SEQUENCE

if TYPE_CHECKING:
    from .ledger import AcceptanceLedger


_SHA256 = re.compile(r"[0-9a-f]{64}")
_PLAN_FIELDS = {"gate_id", "operation_ids"}
_GATE_FIELDS = {
    "gate_id", "policy", "status", "source_execution_id", "artifacts",
    "operation_ids",
}


def completed_artifact_index(ledger: AcceptanceLedger) -> list[dict[str, Any]]:
    """Return hash-verified terminal artifact facts in canonical gate order."""
    ledger._validate_loaded()
    return [
        {
            "gate_id": gate_id,
            "status": attempt["status"],
            "execution_id": attempt["execution_id"],
            "artifacts": [dict(item) for item in attempt["artifacts"]],
        }
        for gate_id in GATE_SEQUENCE
        for attempt in ledger._manifest["gates"].get(gate_id, [])
        if attempt["status"] != "open"
    ]


def terminal_gate_fact(
    ledger: AcceptanceLedger, gate_id: str,
) -> dict[str, Any]:
    """Return one complete terminal gate fact through the Evidence public Interface."""
    if gate_id not in GATE_SEQUENCE:
        raise ValueError("gate reuse source gate is unknown")
    ledger._validate_loaded()
    attempts = ledger._manifest["gates"].get(gate_id, [])
    if len(attempts) != 1 or attempts[0].get("status") == "open":
        raise ValueError("gate reuse source is not terminal")
    attempt = attempts[0]
    return {
        "gate_id": gate_id,
        "status": attempt["status"],
        "execution_id": attempt["execution_id"],
        "operations": [dict(item) for item in attempt["operations"]],
        "artifacts": [dict(item) for item in attempt["artifacts"]],
    }


def freeze_reuse_plan(
    source: AcceptanceLedger,
    plan: Iterable[dict[str, object]],
    *,
    failed_gate: str,
    reconciled: set[str],
) -> list[dict[str, object]]:
    """Resolve an operator plan to immutable source gate facts before signing."""
    requested = list(plan)
    if (
        len(requested) > len(GATE_REUSE_POLICIES)
        or any(
            not isinstance(item, dict) or set(item) != _PLAN_FIELDS
            for item in requested
        )
    ):
        raise ValueError("gate reuse plan is invalid")
    gate_ids = [str(item["gate_id"]) for item in requested]
    if len(gate_ids) != len(set(gate_ids)):
        raise ValueError("gate reuse plan contains a duplicate gate")
    result: list[dict[str, object]] = []
    for item in requested:
        gate_id = str(item["gate_id"])
        policy = GATE_REUSE_POLICIES.get(gate_id)
        if policy is None:
            raise ValueError(f"{gate_id} is not reusable")
        if GATE_SEQUENCE.index(gate_id) >= GATE_SEQUENCE.index(failed_gate):
            raise ValueError("gate reuse cannot import the failed gate or its successors")
        operation_ids = item["operation_ids"]
        if (
            not isinstance(operation_ids, list)
            or len(operation_ids) > 128
            or any(
                not isinstance(value, str) or not valid_operation_id(value)
                for value in operation_ids
            )
            or len(operation_ids) != len(set(operation_ids))
            or not set(operation_ids) <= reconciled
            or (policy == "reconciled_effects") != bool(operation_ids)
        ):
            raise ValueError("gate reuse operation accounting is invalid")
        fact = source.terminal_gate_fact(gate_id)
        allowed = {"passed"} | (
            {"not_applicable"}
            if gate_id == "I04" and source.access_profile == "http_nodeport"
            else set()
        )
        bound = {
            value["operation_id"] for value in fact["operations"]
            if value["kind"] != "gate_execution"
        }
        planned = set(operation_ids)
        if fact["status"] not in allowed or (
            policy == "reconciled_effects" and not bound <= planned
        ) or (
            policy != "reconciled_effects" and bound != planned
        ):
            raise ValueError("gate reuse source result or operation inventory is invalid")
        if not fact["artifacts"]:
            raise ValueError("gate reuse source has no promotion-grade artifacts")
        result.append({
            "gate_id": gate_id,
            "policy": policy,
            "status": fact["status"],
            "source_execution_id": fact["execution_id"],
            "artifacts": fact["artifacts"],
            "operation_ids": sorted(operation_ids),
        })
    result.sort(key=lambda value: GATE_SEQUENCE.index(str(value["gate_id"])))
    validate_reusable_gates(result, failed_gate=failed_gate, reconciled=reconciled)
    return result


def validate_reusable_gates(
    value: object,
    *,
    failed_gate: str,
    reconciled: set[str],
) -> None:
    """Validate the exact reusable gates embedded in a Continuation record."""
    if (
        failed_gate not in GATE_SEQUENCE
        or not isinstance(value, list)
        or len(value) > len(GATE_REUSE_POLICIES)
    ):
        raise ValueError("gate reuse plan is invalid")
    seen_gates: set[str] = set()
    seen_operations: set[str] = set()
    for gate in value:
        if not isinstance(gate, dict) or set(gate) != _GATE_FIELDS:
            raise ValueError("gate reuse entry is invalid")
        gate_id = gate.get("gate_id")
        operations = gate.get("operation_ids")
        artifacts = gate.get("artifacts")
        if (
            gate_id not in GATE_REUSE_POLICIES
            or gate_id in seen_gates
            or GATE_SEQUENCE.index(gate_id) >= GATE_SEQUENCE.index(failed_gate)
            or gate.get("policy") != GATE_REUSE_POLICIES[gate_id]
            or gate.get("status") not in (
                {"passed", "not_applicable"} if gate_id == "I04" else {"passed"}
            )
            or not valid_operation_id(str(gate.get("source_execution_id", "")))
            or not isinstance(operations, list)
            or any(
                not isinstance(item, str) or not valid_operation_id(item)
                for item in operations
            )
            or len(operations) != len(set(operations))
            or not set(operations) <= reconciled
            or seen_operations & set(operations)
            or (gate["policy"] == "reconciled_effects") != bool(operations)
            or not isinstance(artifacts, list)
            or not artifacts
            or len(artifacts) > 128
        ):
            raise ValueError("gate reuse entry is invalid")
        names: set[str] = set()
        for artifact in artifacts:
            relative = (
                Path(str(artifact.get("path", "")))
                if isinstance(artifact, dict) else Path()
            )
            if (
                not isinstance(artifact, dict)
                or set(artifact) != {"path", "sha256", "bytes"}
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.parent
                != Path(GATE_PHASE[gate_id]) / f"{gate_id}-attempt-1"
                or relative.name in names
                or _SHA256.fullmatch(str(artifact.get("sha256", ""))) is None
                or not isinstance(artifact.get("bytes"), int)
                or not 0 <= artifact["bytes"] <= 5 * 1024 * 1024
            ):
                raise ValueError("gate reuse artifact identity is invalid")
            names.add(relative.name)
        seen_gates.add(gate_id)
        seen_operations.update(operations)
    if [gate["gate_id"] for gate in value] != sorted(
        seen_gates, key=GATE_SEQUENCE.index,
    ):
        raise ValueError("gate reuse entries are not in canonical order")
    assert_public_payload(value)


def reuse_gate(
    *,
    source: AcceptanceLedger,
    target: AcceptanceLedger,
    continuation: dict[str, Any],
    now: Callable[[], datetime],
    verifier: Callable[[dict[str, Any]], None] | None,
) -> dict[str, object]:
    """Reuse exactly the current frontier without issuing an external effect."""
    from .deployment_continuation import validate_bundle
    from .evaluator_successor import FORMAT as SUCCESSOR_FORMAT, validate as validate_successor

    if continuation.get("format") == SUCCESSOR_FORMAT:
        validate_successor(continuation)
    else:
        validate_bundle(continuation, now=now, verifier=verifier)
    record = continuation["record"]
    source_record = record["source"]
    replacement = record["replacement"]
    failure = source.failure_summary()
    target_status = target.status()
    if (
        not source.is_sealed
        or source.status().get("status") != "sealed"
        or source_record.get("acceptance_id") != failure["acceptance_id"]
        or source_record.get("failed_gate") != failure["gate_id"]
        or source_record.get("seal_sha256") != sha256(source.root / "SHA256SUMS")
        or source_record.get("product_sha256") != source.candidate_sha256
        or source_record.get("acceptance_tool_sha256")
        != source.acceptance_tool_sha256
        or source_record.get("failure_attribution")
        != failure["failure_attribution"]
        or not set(failure["issued_operation_ids"]) <= set(
            source_record.get("issued_operation_ids", [])
        )
        or record["cluster"].get("kube_context") != source.kube_context
        or record["cluster"].get("identity_sha256")
        != source.cluster_identity_sha256
        or record.get("access_profile") != source.access_profile
    ):
        raise ValueError("gate reuse source identity drifted")
    if (
        target.candidate_sha256 != replacement.get("product_sha256")
        or target.acceptance_tool_sha256
        != replacement.get("acceptance_tool_sha256")
        or target.gate_contract_revision
        != replacement.get("gate_contract_revision")
        or target.kube_context != record["cluster"].get("kube_context")
        or target.cluster_identity_sha256
        != record["cluster"].get("identity_sha256")
        or target.access_profile != record.get("access_profile")
        or target.deployment_precondition_sha256 != continuation["bundle_sha256"]
    ):
        raise ValueError("gate reuse replacement identity drifted")
    if target_status["status"] not in {"active/ready", "active/open"}:
        raise ValueError("gate reuse target is not active")
    gate_id = target.frontier
    entries = [
        item for item in record["reusable_gates"] if item["gate_id"] == gate_id
    ]
    if len(entries) != 1:
        raise ValueError("current frontier is not authorized for gate reuse")
    entry = entries[0]
    fact = source.terminal_gate_fact(gate_id)
    if any(
        entry[key] != fact[source_key]
        for key, source_key in (
            ("status", "status"),
            ("source_execution_id", "execution_id"),
            ("artifacts", "artifacts"),
        )
    ):
        raise ValueError("gate reuse source fact drifted")
    bound = {
        item["operation_id"] for item in fact["operations"]
        if item["kind"] != "gate_execution"
    }
    planned = set(entry["operation_ids"])
    if (
        entry["policy"] == "reconciled_effects" and not bound <= planned
    ) or (
        entry["policy"] != "reconciled_effects" and bound != planned
    ):
        raise ValueError("gate reuse operation inventory drifted")
    contents: list[tuple[str, bytes]] = []
    for artifact in entry["artifacts"]:
        path = source.root / artifact["path"]
        if (
            path.is_symlink()
            or not path.is_file()
            or sha256(path) != artifact["sha256"]
            or path.stat().st_size != artifact["bytes"]
        ):
            raise ValueError("gate reuse source artifact drifted")
        contents.append((path.name, path.read_bytes()))
    provenance_name = f"reuse-{source_record['acceptance_id']}.json"
    if provenance_name in {name for name, _content in contents}:
        raise ValueError("gate reuse provenance name collides with source evidence")
    provenance = {
        "format": "continuation_gate_reuse_provenance_v1",
        "source": source_record,
        "replacement": replacement,
        "cluster": record["cluster"],
        "access_profile": record["access_profile"],
        "gate": entry,
        "deployment_continuation_bundle_sha256": continuation["bundle_sha256"],
        "effect_replayed": False,
    }
    if target.open_gate is None:
        started_at = target.start_gate(gate_id)
        artifacts = [target.write_json(gate_id, provenance_name, provenance)]
    else:
        if target.open_gate != gate_id:
            raise ValueError("gate reuse target has a different open gate")
        execution = target.resume_gate(gate_id)
        started_at = execution.started_at
        indexed = {item.path.name: item for item in execution.artifacts}
        expected_names = {provenance_name, *(name for name, _content in contents)}
        if set(indexed) - expected_names or provenance_name not in indexed:
            raise ValueError("open gate is not a resumable gate reuse attempt")
        retained_provenance = indexed[provenance_name]
        if not retained_provenance.path.is_file():
            retained_provenance = target.write_json(
                gate_id, provenance_name, provenance,
            )
        if _object(retained_provenance.path) != provenance:
            raise ValueError("open gate reuse provenance drifted")
        artifacts = [retained_provenance]
    indexed = {item.path.name: item for item in target.resume_gate(gate_id).artifacts}
    for name, content in contents:
        retained = indexed.get(name)
        if retained is not None:
            if retained.path.read_bytes() != content:
                raise ValueError("open gate reuse artifact drifted")
            artifacts.append(retained)
        else:
            artifacts.append(target.write_bytes(gate_id, name, content))
    execution = target.resume_gate(gate_id)
    public_fact = {
        "deployment_continuation_bundle_sha256": continuation["bundle_sha256"],
        "source_seal_sha256": source_record["seal_sha256"],
        "effect_replayed": False,
    }
    reconciliations = {
        item["operation_id"]: item for item in execution.reconciliations
    }
    if (
        len(execution.operations) != 1
        or execution.operations[0].get("kind") != "gate_execution"
        or execution.operations[0].get("operation_id") != execution.execution_id
        or set(reconciliations) - {execution.execution_id}
    ):
        raise ValueError("open gate is not a bounded gate reuse attempt")
    existing = reconciliations.get(execution.execution_id)
    if existing is None:
        target.reconcile_operation(
            gate_id,
            operation_id=execution.execution_id,
            outcome="succeeded",
            public_fact=public_fact,
        )
    elif (
        existing.get("outcome") != "succeeded"
        or existing.get("public_fact") != public_fact
    ):
        raise ValueError("open gate reuse reconciliation drifted")
    target.record_gate(gate_id, GateResult(entry["status"], tuple(artifacts)), started_at=started_at)
    return {"gate_id": gate_id, "status": entry["status"], "reused": True}


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("gate reuse provenance is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("gate reuse provenance is invalid")
    return value
