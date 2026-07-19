"""Signed, fail-closed reuse of one source gate at one new ledger frontier."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .credentials import assert_public_payload
from .deployment_continuation import validate_bundle as validate_continuation
from .evidence_files import atomic_write, sha256, sha256_bytes
from .execution_journal import valid_operation_id
from .gate_contract import (
    EVIDENCE_FORMAT_VERSION,
    GATE_PHASE,
    GATE_REUSE_POLICIES,
    GATE_SEQUENCE,
)
from .human_attestation import signature_identity_error
from .promotion import is_sealed

if TYPE_CHECKING:
    from .evidence import AcceptanceEvidence


FORMAT_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SIGNED_FIELDS = {"statement", "signature", "public_key", "fingerprint"}
_PLAN_FIELDS = {"gate_id", "operation_ids"}
_GATE_FIELDS = {
    "gate_id", "policy", "status", "source_execution_id", "artifacts",
    "operation_ids",
}


def completed_artifact_index(ledger: AcceptanceEvidence) -> list[dict[str, Any]]:
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
    ledger: AcceptanceEvidence, gate_id: str,
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


class GateReuseEpoch:
    """Own one checksummed, Platform-Operator-signed gate reuse plan."""

    def __init__(
        self,
        root: Path,
        *,
        verifier: Callable[[dict[str, Any]], None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.root = root
        self.verifier = verifier
        self.now = now

    def create(
        self,
        *,
        reuse_id: str,
        source: AcceptanceEvidence,
        continuation: dict[str, Any],
        plan: Iterable[dict[str, object]],
        ttl_seconds: int = 3600,
    ) -> Path:
        if not _ID.fullmatch(reuse_id):
            raise ValueError("gate reuse ID contains unsupported characters")
        if not 60 <= ttl_seconds <= 86400:
            raise ValueError("gate reuse TTL is invalid")
        if self.verifier is None:
            raise ValueError("gate reuse requires a continuation signature verifier")
        validate_continuation(
            continuation, now=self.now, verifier=self.verifier,
        )
        continuation_record = continuation["record"]
        failure = source.failure_summary()
        source_record = continuation_record["source"]
        if (
            source.status().get("status") != "sealed"
            or failure.get("decision") != "no_promote"
            or not is_sealed(source)
            or source_record.get("acceptance_id") != failure["acceptance_id"]
            or source_record.get("failed_gate") != failure["gate_id"]
            or source_record.get("product_sha256") != failure["product_sha256"]
            or source_record.get("acceptance_tool_sha256")
            != failure["acceptance_tool_sha256"]
            or source_record.get("failure_attribution")
            != failure["failure_attribution"]
            or not set(failure["issued_operation_ids"]) <= set(
                source_record.get("issued_operation_ids", [])
            )
            or continuation_record["cluster"].get("kube_context")
            != failure["kube_context"]
            or continuation_record["cluster"].get("identity_sha256")
            != failure["cluster_identity_sha256"]
            or continuation_record.get("access_profile") != failure["access_profile"]
            or source_record.get("seal_sha256") != sha256(source.root / "SHA256SUMS")
        ):
            raise ValueError("gate reuse source does not match the signed continuation")
        reconciled = {
            item["operation_id"] for item in continuation_record["reconciliations"]
        }
        gates = _derive_gates(
            source, plan, failed_gate=failure["gate_id"], reconciled=reconciled,
        )
        created = self.now()
        record = {
            "format_version": FORMAT_VERSION,
            "reuse_id": reuse_id,
            "source": {
                "acceptance_id": failure["acceptance_id"],
                "failed_gate": failure["gate_id"],
                "seal_sha256": source_record["seal_sha256"],
                "product_sha256": failure["product_sha256"],
                "acceptance_tool_sha256": failure["acceptance_tool_sha256"],
                "failure_attribution": failure["failure_attribution"],
                "issued_operation_ids": list(failure["issued_operation_ids"]),
                "kube_context": failure["kube_context"],
                "cluster_identity_sha256": failure["cluster_identity_sha256"],
                "access_profile": failure["access_profile"],
            },
            "replacement": {
                **continuation_record["replacement"],
                "kube_context": continuation_record["cluster"]["kube_context"],
                "cluster_identity_sha256": continuation_record["cluster"][
                    "identity_sha256"
                ],
                "access_profile": continuation_record["access_profile"],
                "continuation_bundle_sha256": continuation["bundle_sha256"],
            },
            "reconciled_operation_ids": sorted(reconciled),
            "gates": gates,
            "created_at": _iso(created),
            "expires_at": _iso(min(
                created + timedelta(seconds=ttl_seconds),
                _parse_utc(continuation_record["expires_at"]),
            )),
        }
        _validate_record(record)
        assert_public_payload(record)
        path = self.root / reuse_id
        path.mkdir(parents=True, exist_ok=False)
        _write_record(path, record)
        return path

    def attestation_statement(
        self, path: Path, *, actor: str, note: str,
    ) -> dict[str, Any]:
        bundle = inspect(path)
        record = bundle["record"]
        statement = {
            "reuse_id": record["reuse_id"],
            "record_sha256": bundle["record_sha256"],
            "source": record["source"],
            "replacement": record["replacement"],
            "gates": record["gates"],
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "actor": actor,
            "role": "platform_operator",
            "conclusion": "reuse_exact_gates",
            "signed_at": _iso(self.now()),
            "note": note,
        }
        assert_public_payload(statement)
        return statement

    @staticmethod
    def attach_attestation(path: Path, item: dict[str, Any]) -> None:
        if (path / "attestation.json").exists():
            raise ValueError("gate reuse epoch is already signed")
        bundle = inspect(path)
        _validate_attestation(bundle["record"], bundle["record_sha256"], item, None)
        atomic_write(path / "attestation.json", _json_bytes(item), staging_dir=path)

    inspect = staticmethod(lambda path, **kwargs: inspect(path, **kwargs))


def inspect(
    path: Path,
    *,
    require_signed: bool = False,
    verifier: Callable[[dict[str, Any]], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    record_path = path / "record.json"
    checksum_path = path / "SHA256SUMS"
    if record_path.is_symlink() or not record_path.is_file() or not checksum_path.is_file():
        raise ValueError("gate reuse record is incomplete")
    digest = sha256(record_path)
    if checksum_path.read_text(encoding="utf-8") != f"{digest}  record.json\n":
        raise ValueError("gate reuse checksum mismatch")
    record = _object(record_path, "gate reuse record")
    _validate_record(record)
    attestation_path = path / "attestation.json"
    attestation = (
        _object(attestation_path, "gate reuse attestation")
        if attestation_path.is_file() else None
    )
    if require_signed and attestation is None:
        raise ValueError("gate reuse epoch is not signed")
    if attestation is not None:
        _validate_attestation(record, digest, attestation, verifier)
    unsigned = {"record": record, "record_sha256": digest, "attestation": attestation}
    bundle = {**unsigned, "bundle_sha256": sha256_bytes(_json_bytes(unsigned))}
    if require_signed:
        validate_bundle(bundle, now=now, verifier=verifier)
    return bundle


def validate_bundle(
    bundle: Any,
    *,
    now: Callable[[], datetime],
    verifier: Callable[[dict[str, Any]], None] | None,
) -> None:
    if verifier is None:
        raise ValueError("gate reuse signature verifier is required")
    if not isinstance(bundle, dict) or set(bundle) != {
        "record", "record_sha256", "attestation", "bundle_sha256",
    }:
        raise ValueError("gate reuse bundle is invalid")
    if bundle.get("attestation") is None:
        raise ValueError("gate reuse epoch is not signed")
    record = bundle.get("record")
    _validate_record(record)
    digest = sha256_bytes(_json_bytes(record))
    unsigned = {key: bundle[key] for key in ("record", "record_sha256", "attestation")}
    if (
        bundle.get("record_sha256") != digest
        or bundle.get("bundle_sha256") != sha256_bytes(_json_bytes(unsigned))
    ):
        raise ValueError("gate reuse bundle checksum mismatch")
    _validate_attestation(record, digest, bundle.get("attestation"), verifier)
    current = now()
    if current < _parse_utc(record["created_at"]) or current > _parse_utc(
        record["expires_at"]
    ):
        raise ValueError("gate reuse epoch expired")


def reuse_gate(
    *,
    source: AcceptanceEvidence,
    target: AcceptanceEvidence,
    bundle: dict[str, Any],
    now: Callable[[], datetime],
    verifier: Callable[[dict[str, Any]], None] | None,
) -> dict[str, object]:
    """Reuse exactly the current frontier without issuing an external effect."""
    validate_bundle(bundle, now=now, verifier=verifier)
    record = bundle["record"]
    source_record = record["source"]
    replacement = record["replacement"]
    failure = source.failure_summary()
    target_status = target.status()
    if (
        not is_sealed(source)
        or source.status().get("status") != "sealed"
        or source_record.get("acceptance_id") != failure["acceptance_id"]
        or source_record.get("failed_gate") != failure["gate_id"]
        or source_record.get("seal_sha256") != sha256(source.root / "SHA256SUMS")
        or source_record.get("product_sha256") != source.candidate_sha256
        or source_record.get("acceptance_tool_sha256") != source.acceptance_tool_sha256
        or source_record.get("failure_attribution") != failure["failure_attribution"]
        or source_record.get("issued_operation_ids") != failure["issued_operation_ids"]
        or source_record.get("kube_context") != source.kube_context
        or source_record.get("cluster_identity_sha256")
        != source.cluster_identity_sha256
        or source_record.get("access_profile") != source.access_profile
        or not set(source_record.get("issued_operation_ids", [])) <= set(
            record["reconciled_operation_ids"]
        )
    ):
        raise ValueError("gate reuse source identity drifted")
    if (
        target.candidate_sha256 != replacement.get("product_sha256")
        or target.acceptance_tool_sha256 != replacement.get("acceptance_tool_sha256")
        or target.gate_contract_revision != replacement.get("gate_contract_revision")
        or target.kube_context != replacement.get("kube_context")
        or target.cluster_identity_sha256 != replacement.get("cluster_identity_sha256")
        or target.access_profile != replacement.get("access_profile")
        or target.deployment_precondition_sha256
        != replacement.get("continuation_bundle_sha256")
    ):
        raise ValueError("gate reuse replacement identity drifted")
    if target_status["status"] not in {"active/ready", "active/open"}:
        raise ValueError("gate reuse target is not active")
    gate_id = target.frontier
    entries = [item for item in record["gates"] if item["gate_id"] == gate_id]
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
            path.is_symlink() or not path.is_file()
            or sha256(path) != artifact["sha256"]
            or path.stat().st_size != artifact["bytes"]
        ):
            raise ValueError("gate reuse source artifact drifted")
        contents.append((path.name, path.read_bytes()))
    provenance_name = f"reuse-{source_record['acceptance_id']}.json"
    if provenance_name in {name for name, _content in contents}:
        raise ValueError("gate reuse provenance name collides with source evidence")
    provenance = {
        "format": "gate_reuse_provenance_v1",
        "source": source_record,
        "replacement": replacement,
        "gate": entry,
        "gate_reuse_bundle_sha256": bundle["bundle_sha256"],
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
        if _object(retained_provenance.path, "gate reuse provenance") != provenance:
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
        "gate_reuse_bundle_sha256": bundle["bundle_sha256"],
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
    target.record_gate(
        gate_id, entry["status"], artifacts, started_at=started_at,
    )
    return {"gate_id": gate_id, "status": entry["status"], "reused": True}


def _derive_gates(
    source: AcceptanceEvidence,
    plan: Iterable[dict[str, object]],
    *,
    failed_gate: str,
    reconciled: set[str],
) -> list[dict[str, object]]:
    requested = list(plan)
    if (
        not requested or len(requested) > len(GATE_REUSE_POLICIES)
        or any(not isinstance(item, dict) or set(item) != _PLAN_FIELDS for item in requested)
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
            {"not_applicable"} if gate_id == "I04" and source.access_profile == "http_nodeport"
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
    return sorted(result, key=lambda value: GATE_SEQUENCE.index(str(value["gate_id"])))


def _validate_record(value: Any) -> None:
    required = {
        "format_version", "reuse_id", "source", "replacement",
        "reconciled_operation_ids", "gates", "created_at", "expires_at",
    }
    if (
        not isinstance(value, dict) or set(value) != required
        or value.get("format_version") != FORMAT_VERSION
        or not _ID.fullmatch(str(value.get("reuse_id", "")))
    ):
        raise ValueError("gate reuse record contract is invalid")
    source = value.get("source")
    replacement = value.get("replacement")
    reconciled = value.get("reconciled_operation_ids")
    gates = value.get("gates")
    if (
        not isinstance(source, dict)
        or set(source) != {
            "acceptance_id", "failed_gate", "seal_sha256", "product_sha256",
            "acceptance_tool_sha256", "failure_attribution", "issued_operation_ids",
            "kube_context", "cluster_identity_sha256", "access_profile",
        }
        or not _ID.fullmatch(str(source.get("acceptance_id", "")))
        or source.get("failed_gate") not in GATE_SEQUENCE
        or any(_SHA256.fullmatch(str(source.get(key, ""))) is None for key in (
            "seal_sha256", "product_sha256", "acceptance_tool_sha256",
            "cluster_identity_sha256",
        ))
        or source.get("failure_attribution") not in {
            "tool_failure", "environment_failure", "inconclusive",
        }
        or not isinstance(source.get("issued_operation_ids"), list)
        or not source["issued_operation_ids"]
        or len(source["issued_operation_ids"]) > 128
        or any(
            not isinstance(item, str) or not valid_operation_id(item)
            for item in source["issued_operation_ids"]
        )
        or len(source["issued_operation_ids"])
        != len(set(source["issued_operation_ids"]))
        or not isinstance(source.get("kube_context"), str)
        or source.get("access_profile") not in {"http_nodeport", "https_ingress"}
        or not isinstance(replacement, dict)
        or set(replacement) != {
            "product_sha256", "acceptance_tool_sha256", "gate_contract_revision",
            "evidence_format_version", "kube_context", "cluster_identity_sha256",
            "access_profile", "continuation_bundle_sha256",
        }
        or any(_SHA256.fullmatch(str(replacement.get(key, ""))) is None for key in (
            "product_sha256", "acceptance_tool_sha256", "cluster_identity_sha256",
            "continuation_bundle_sha256",
        ))
        or replacement.get("product_sha256") != source.get("product_sha256")
        or replacement.get("kube_context") != source.get("kube_context")
        or replacement.get("cluster_identity_sha256")
        != source.get("cluster_identity_sha256")
        or replacement.get("access_profile") != source.get("access_profile")
        or not isinstance(replacement.get("gate_contract_revision"), str)
        or replacement.get("evidence_format_version") != EVIDENCE_FORMAT_VERSION
        or not isinstance(replacement.get("kube_context"), str)
        or replacement.get("access_profile") not in {"http_nodeport", "https_ingress"}
        or not isinstance(reconciled, list)
        or len(reconciled) > 128
        or any(
            not isinstance(item, str) or not valid_operation_id(item)
            for item in reconciled
        )
        or len(reconciled) != len(set(reconciled))
        or not set(source["issued_operation_ids"]) <= set(reconciled)
        or not isinstance(gates, list)
        or not gates
        or len(gates) > len(GATE_REUSE_POLICIES)
    ):
        raise ValueError("gate reuse record contract is invalid")
    seen_gates: set[str] = set()
    seen_operations: set[str] = set()
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) != _GATE_FIELDS:
            raise ValueError("gate reuse entry is invalid")
        gate_id = gate.get("gate_id")
        operations = gate.get("operation_ids")
        artifacts = gate.get("artifacts")
        if (
            gate_id not in GATE_REUSE_POLICIES
            or gate_id in seen_gates
            or GATE_SEQUENCE.index(gate_id) >= GATE_SEQUENCE.index(source["failed_gate"])
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
            or not set(operations) <= set(reconciled)
            or seen_operations & set(operations)
            or (gate["policy"] == "reconciled_effects") != bool(operations)
            or not isinstance(artifacts, list)
            or not artifacts
            or len(artifacts) > 128
        ):
            raise ValueError("gate reuse entry is invalid")
        names: set[str] = set()
        for artifact in artifacts:
            relative = Path(str(artifact.get("path", ""))) if isinstance(artifact, dict) else Path()
            if (
                not isinstance(artifact, dict)
                or set(artifact) != {"path", "sha256", "bytes"}
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.parent != Path(GATE_PHASE[gate_id]) / f"{gate_id}-attempt-1"
                or relative.name in names
                or _SHA256.fullmatch(str(artifact.get("sha256", ""))) is None
                or not isinstance(artifact.get("bytes"), int)
                or not 0 <= artifact["bytes"] <= 5 * 1024 * 1024
            ):
                raise ValueError("gate reuse artifact identity is invalid")
            names.add(relative.name)
        seen_gates.add(gate_id)
        seen_operations.update(operations)
    if [gate["gate_id"] for gate in gates] != sorted(
        seen_gates, key=GATE_SEQUENCE.index,
    ):
        raise ValueError("gate reuse entries are not in canonical order")
    if _parse_utc(str(value.get("created_at", ""))) > _parse_utc(
        str(value.get("expires_at", ""))
    ):
        raise ValueError("gate reuse record timestamp is invalid")
    assert_public_payload(value)


def _validate_attestation(
    record: dict[str, Any],
    record_sha: str,
    item: Any,
    verifier: Callable[[dict[str, Any]], None] | None,
) -> None:
    statement = item.get("statement") if isinstance(item, dict) else None
    expected = {
        "reuse_id": record["reuse_id"],
        "record_sha256": record_sha,
        "source": record["source"],
        "replacement": record["replacement"],
        "gates": record["gates"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "role": "platform_operator",
        "conclusion": "reuse_exact_gates",
    }
    if (
        not isinstance(item, dict) or set(item) != _SIGNED_FIELDS
        or not isinstance(statement, dict)
        or any(statement.get(key) != expected_value for key, expected_value in expected.items())
        or set(statement) != set(expected) | {"actor", "signed_at", "note"}
        or not all(isinstance(statement.get(key), str) and statement[key] for key in (
            "actor", "signed_at", "note",
        ))
        or not (
            _parse_utc(record["created_at"])
            <= _parse_utc(statement["signed_at"])
            <= _parse_utc(record["expires_at"])
        )
        or signature_identity_error(
            item.get("signature"), item.get("public_key"), item.get("fingerprint"),
        )
    ):
        raise ValueError("gate reuse attestation contract is invalid")
    if verifier is not None:
        try:
            verifier(item)
        except Exception as exc:
            raise ValueError("gate reuse signature is invalid") from exc


def _write_record(path: Path, record: dict[str, Any]) -> None:
    content = _json_bytes(record)
    atomic_write(path / "record.json", content, staging_dir=path)
    atomic_write(
        path / "SHA256SUMS",
        f"{sha256_bytes(content)}  record.json\n".encode(),
        staging_dir=path,
    )


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("gate reuse timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("gate reuse timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
