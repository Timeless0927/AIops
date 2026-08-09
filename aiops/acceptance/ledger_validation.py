"""Persisted manifest validation inside the Acceptance Ledger Module."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from . import execution_journal
from .deployment_continuation import deployment_precondition, valid_failure_attribution
from .gate_contract import EVIDENCE_FORMAT_VERSION, GATE_CONTRACT_REVISION, GATE_SEQUENCE
from .ledger_promotion import promotion_facts_error
from .redaction import redact_text

if TYPE_CHECKING:
    from .ledger import AcceptanceLedger


_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_CORRECTION_FIELDS = {
    "kind", "policy_revision", "gate_id", "source_execution_id", "source_artifacts",
    "from_acceptance_tool_sha256", "to_acceptance_tool_sha256",
    "diagnostic_conclusion_sha256", "corrected_status", "corrected_at", "reason",
}


def validate_loaded(
    ledger: AcceptanceLedger,
    required_attestations: dict[str, tuple[str, ...]],
) -> None:
    from .ledger import EvidenceError

    manifest = ledger._manifest
    if not ledger.evidence.manifest_matches(manifest):
        raise EvidenceError("acceptance manifest changed outside its owner Interface")
    if manifest.get("format_version") != EVIDENCE_FORMAT_VERSION:
        raise EvidenceError("unsupported_evidence_format")
    if not _ID_PATTERN.fullmatch(str(manifest.get("acceptance_id", ""))):
        raise EvidenceError("invalid acceptance_id in manifest")
    release = manifest.get("release")
    tool = manifest.get("acceptance_tool")
    cluster = manifest.get("cluster")
    if not isinstance(release, dict) or not _SHA256_PATTERN.fullmatch(
        str(release.get("sha256", ""))
    ):
        raise EvidenceError("invalid release identity in manifest")
    if not isinstance(tool, dict) or not _SHA256_PATTERN.fullmatch(
        str(tool.get("sha256", ""))
    ):
        raise EvidenceError("invalid acceptance-tool identity in manifest")
    if manifest.get("gate_contract_revision") != GATE_CONTRACT_REVISION:
        raise EvidenceError("unsupported gate contract revision")
    if not isinstance(cluster, dict) or not _SHA256_PATTERN.fullmatch(
        str(cluster.get("identity_sha256", ""))
    ):
        raise EvidenceError("invalid cluster identity in manifest")
    if manifest.get("access_profile") not in {"http_nodeport", "https_ingress"}:
        raise EvidenceError("invalid access profile in manifest")
    try:
        mode = manifest.get("deployment_mode")
        precondition = manifest.get("deployment_precondition")
        record = precondition.get("record") if isinstance(precondition, dict) else None
        historical = mode == "adopt_existing" and ledger.is_sealed
        historical = (
            historical and isinstance(record, dict) and record.get("format_version") == 1
        )
        if not historical:
            deployment_precondition(
                environment_qualification=precondition if mode == "clean_install" else None,
                deployment_continuation=precondition if mode == "adopt_existing" else None,
                evaluator_successor=precondition if mode == "evaluator_successor" else None,
                at=str(manifest.get("created_at", "")),
                verifier=ledger._attestation_verifier,
                release_sha256=release["sha256"],
                acceptance_tool_sha256=tool["sha256"],
                gate_contract_revision=manifest["gate_contract_revision"],
                kube_context=cluster.get("kube_context"),
                cluster_identity_sha256=cluster["identity_sha256"],
                access_profile=manifest["access_profile"],
            )
    except ValueError as exc:
        raise EvidenceError(str(exc)) from exc
    if (
        manifest.get("deployment_mode")
        not in {"clean_install", "adopt_existing", "evaluator_successor"}
        or not isinstance(manifest.get("deployment_precondition"), dict)
        or manifest.get("deployment_precondition_sha256")
        != manifest["deployment_precondition"].get("bundle_sha256")
    ):
        raise EvidenceError("deployment precondition index is invalid")
    violations = manifest.get("identity_violations")
    if not isinstance(violations, list) or any(
        not isinstance(item, str) for item in violations
    ):
        raise EvidenceError("identity violation facts are invalid")
    gates = manifest.get("gates")
    if not isinstance(gates, dict) or any(
        gate_id not in GATE_SEQUENCE for gate_id in gates
    ):
        raise EvidenceError("manifest contains an invalid gate index")
    correction_error = correction_validation_error(ledger)
    if correction_error:
        raise EvidenceError(correction_error)
    seen_paths: set[str] = set()
    seen_operation_ids = set(
        ledger._manifest["deployment_precondition"]
        .get("record", {})
        .get("source", {})
        .get("issued_operation_ids", [])
    )
    open_gates = 0
    terminal_seen = False
    for gate_id in GATE_SEQUENCE:
        attempts = gates.get(gate_id, [])
        if not isinstance(attempts, list) or len(attempts) > 1:
            raise EvidenceError("each gate allows at most one attempt")
        if not attempts:
            continue
        attempt = attempts[0]
        if not isinstance(attempt, dict) or attempt.get("attempt") != 1:
            raise EvidenceError("gate attempt identity is invalid")
        status = attempt.get("status")
        if status not in {"open", "passed", "failed", "not_applicable"}:
            raise EvidenceError("manifest contains an invalid gate status")
        if terminal_seen:
            raise EvidenceError("manifest contains a gate after a terminal failure")
        if gate_id != ledger._expected_gate_before(gate_id):
            raise EvidenceError("manifest gate history is not the canonical frontier")
        if status == "open":
            open_gates += 1
        elif not isinstance(attempt.get("completed_at"), str):
            raise EvidenceError("terminal gate timestamp is invalid")
        if status == "failed":
            if not valid_failure_attribution(attempt.get("failure_attribution")):
                raise EvidenceError("failed gate attribution is invalid")
            terminal_seen = ledger.effective_status(gate_id) == "failed"
        elif "failure_attribution" in attempt:
            raise EvidenceError("non-failed gate contains failure attribution")
        if status == "not_applicable" and (
            gate_id != "I04" or ledger.access_profile != "http_nodeport"
        ):
            raise EvidenceError("manifest contains an invalid conditional gate result")
        if not execution_journal.valid_operation_id(str(attempt.get("execution_id", ""))):
            raise EvidenceError("gate execution identity is invalid")
        if not isinstance(attempt.get("started_at"), str):
            raise EvidenceError("gate start timestamp is invalid")
        journal_error = execution_journal.validation_error(attempt)
        if journal_error:
            raise EvidenceError(journal_error)
        operation_ids = {item["operation_id"] for item in attempt["operations"]}
        if seen_operation_ids & operation_ids:
            raise EvidenceError("gate operation identity is duplicated across the ledger")
        seen_operation_ids.update(operation_ids)
        artifacts = attempt.get("artifacts")
        if not isinstance(artifacts, list):
            raise EvidenceError("gate artifact index must be a list")
        for artifact in artifacts:
            error = ledger.evidence.validate_artifact(
                gate_id,
                artifact,
                seen_paths,
                status=status,
                phase=ledger._phase(gate_id),
            )
            if error:
                raise EvidenceError(error)
    if open_gates > 1:
        raise EvidenceError("manifest contains more than one open gate")
    storage_error = ledger.evidence.unindexed_file_error(seen_paths)
    if storage_error:
        raise EvidenceError(storage_error)
    if ledger.evidence.attestation_exists():
        error = ledger.evidence.attestation_validation_error(
            acceptance_id=ledger._manifest["acceptance_id"],
            candidate_sha256=ledger.candidate_sha256,
            gate_ids=set(GATE_SEQUENCE),
        )
        if error:
            raise EvidenceError(error)
    attestation_error = ledger.evidence.attestation_index_error(
        manifest.get("human_attestation")
    )
    if attestation_error:
        raise EvidenceError(attestation_error)
    promotion_error = promotion_facts_error(ledger, required_attestations)
    if promotion_error:
        raise EvidenceError(promotion_error)
    seal_error = ledger.evidence.seal_validation_error(manifest.get("seal"))
    if seal_error:
        raise EvidenceError(seal_error)


def correction_validation_error(ledger: AcceptanceLedger) -> str | None:
    corrections = ledger._manifest.get("evaluator_corrections", [])
    if not isinstance(corrections, list):
        return "evaluator corrections are invalid"
    previous_sha = ledger._manifest.get("acceptance_tool", {}).get("sha256")
    seen: set[str] = set()
    for item in corrections:
        if not isinstance(item, dict) or set(item) != _CORRECTION_FIELDS:
            return "evaluator correction record is invalid"
        gate_id = item.get("gate_id")
        attempts = ledger._manifest.get("gates", {}).get(gate_id, [])
        artifacts = item.get("source_artifacts")
        if (
            item.get("kind") != "evaluator_only"
            or item.get("policy_revision") != "evaluator-correction-v1"
            or gate_id not in GATE_SEQUENCE
            or gate_id in seen
            or len(attempts) != 1
            or attempts[0].get("status") != "failed"
            or item.get("source_execution_id") != attempts[0].get("execution_id")
            or item.get("corrected_status") != "passed"
            or item.get("from_acceptance_tool_sha256") != previous_sha
            or _SHA256_PATTERN.fullmatch(
                str(item.get("to_acceptance_tool_sha256", ""))
            )
            is None
            or item.get("to_acceptance_tool_sha256") == previous_sha
            or _SHA256_PATTERN.fullmatch(
                str(item.get("diagnostic_conclusion_sha256", ""))
            )
            is None
            or not isinstance(item.get("corrected_at"), str)
            or not valid_public_reason(item.get("reason"))
            or not isinstance(artifacts, list)
            or not artifacts
        ):
            return "evaluator correction record is invalid"
        indexed = {value["path"]: value for value in attempts[0].get("artifacts", [])}
        for artifact in artifacts:
            if (
                not isinstance(artifact, dict)
                or set(artifact) != {"path", "sha256"}
                or artifact.get("path") not in indexed
                or artifact.get("sha256") != indexed[artifact["path"]].get("sha256")
            ):
                return "evaluator correction source artifact is invalid"
        seen.add(gate_id)
        previous_sha = item["to_acceptance_tool_sha256"]
    return None


def valid_public_reason(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and 1 <= len(value) <= 2048
        and redact_text(value) == value
    )
