"""Append-only, no-I/O corrections over hash-bound acceptance artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .evidence_files import sha256
from .gate_contract import GATE_SEQUENCE

if TYPE_CHECKING:
    from .evidence import AcceptanceEvidence


_SHA256 = re.compile(r"[0-9a-f]{64}")
_FIELDS = {
    "kind", "policy_revision", "gate_id", "source_execution_id", "source_artifacts",
    "from_acceptance_tool_sha256", "to_acceptance_tool_sha256",
    "diagnostic_conclusion_sha256", "corrected_status", "corrected_at", "reason",
}


def effective_status(ledger: AcceptanceEvidence, gate_id: str) -> str | None:
    attempts = ledger._manifest["gates"].get(gate_id, [])
    if not attempts:
        return None
    correction = next(
        (
            item for item in ledger._manifest.get("evaluator_corrections", [])
            if item.get("gate_id") == gate_id
        ),
        None,
    )
    return correction["corrected_status"] if correction is not None else attempts[0]["status"]


def correct_s01(
    ledger: AcceptanceEvidence,
    *,
    acceptance_tool: Path,
    diagnostic: Path,
    reason: str,
) -> dict[str, Any]:
    """Re-evaluate only S01's persisted initial response, without live I/O."""
    ledger._ensure_writable()
    if ledger.failed_gate != "S01":
        raise ValueError("S01 evaluator correction requires S01 to be the effective failed gate")
    if any(
        item.get("gate_id") == "S01"
        for item in ledger._manifest.get("evaluator_corrections", [])
    ):
        raise ValueError("S01 evaluator correction already exists")
    attempt = ledger._manifest["gates"]["S01"][0]
    if (
        len(attempt.get("operations", [])) != 1
        or attempt["operations"][0].get("kind") != "gate_execution"
        or attempt.get("reconciliations")
    ):
        raise ValueError("S01 evaluator correction cannot cover external operations")
    artifact = next(
        (
            item for item in attempt.get("artifacts", [])
            if Path(item["path"]).name == "platform-initial.json"
        ),
        None,
    )
    if artifact is None:
        raise ValueError("S01 evaluator correction requires platform-initial.json")
    source_path = ledger.root / artifact["path"]
    if source_path.is_symlink() or not source_path.is_file() or sha256(source_path) != artifact["sha256"]:
        raise ValueError("S01 evaluator correction source artifact drifted")
    try:
        status = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("S01 evaluator correction source artifact is invalid") from exc
    if not isinstance(status, dict):
        raise ValueError("S01 evaluator correction source artifact is invalid")
    from .platform_status_gates import PlatformStatusGateRunner
    PlatformStatusGateRunner._validate_platform(status)
    diagnostic_sha = _validate_diagnostic(ledger, diagnostic)
    target_sha = sha256(acceptance_tool)
    previous_sha = _active_tool_sha256(ledger)
    if target_sha == previous_sha:
        raise ValueError("evaluator correction requires a changed acceptance tool")
    if not 1 <= len(reason.strip()) <= 2048:
        raise ValueError("evaluator correction reason is invalid")
    correction = {
        "kind": "evaluator_only",
        "policy_revision": "evaluator-correction-v1",
        "gate_id": "S01",
        "source_execution_id": attempt["execution_id"],
        "source_artifacts": [{"path": artifact["path"], "sha256": artifact["sha256"]}],
        "from_acceptance_tool_sha256": previous_sha,
        "to_acceptance_tool_sha256": target_sha,
        "diagnostic_conclusion_sha256": diagnostic_sha,
        "corrected_status": "passed",
        "corrected_at": ledger.now(),
        "reason": reason.strip(),
    }
    ledger._manifest.setdefault("evaluator_corrections", []).append(correction)
    error = validation_error(ledger)
    if error:
        ledger._manifest["evaluator_corrections"].pop()
        raise ValueError(error)
    ledger._persist_manifest()
    return json.loads(json.dumps(correction))


def validation_error(ledger: AcceptanceEvidence) -> str | None:
    corrections = ledger._manifest.get("evaluator_corrections", [])
    if not isinstance(corrections, list):
        return "evaluator corrections are invalid"
    previous_sha = ledger._manifest.get("acceptance_tool", {}).get("sha256")
    seen: set[str] = set()
    for item in corrections:
        if not isinstance(item, dict) or set(item) != _FIELDS:
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
            or _SHA256.fullmatch(str(item.get("to_acceptance_tool_sha256", ""))) is None
            or item.get("to_acceptance_tool_sha256") == previous_sha
            or _SHA256.fullmatch(str(item.get("diagnostic_conclusion_sha256", ""))) is None
            or not isinstance(item.get("corrected_at"), str)
            or not 1 <= len(str(item.get("reason", ""))) <= 2048
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


def _active_tool_sha256(ledger: AcceptanceEvidence) -> str:
    corrections = ledger._manifest.get("evaluator_corrections", [])
    return (
        corrections[-1]["to_acceptance_tool_sha256"]
        if corrections else ledger.acceptance_tool_sha256
    )


def _validate_diagnostic(ledger: AcceptanceEvidence, root: Path) -> str:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        conclusion_path = root / "conclusion.json"
        conclusion = json.loads(conclusion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("evaluator correction diagnostic is invalid") from exc
    if (
        manifest.get("source_acceptance_id") != ledger._manifest["acceptance_id"]
        or manifest.get("source_failed_gate") != "S01"
        or manifest.get("release_sha256") != ledger.candidate_sha256
        or manifest.get("acceptance_tool_sha256") != ledger.acceptance_tool_sha256
        or conclusion.get("diagnosed_attribution") != "tool_failure"
        or conclusion.get("operation_accounting_complete") is not True
        or conclusion.get("recovered_operation_ids") != []
    ):
        raise ValueError("evaluator correction diagnostic does not match the failed gate")
    return sha256(conclusion_path)
