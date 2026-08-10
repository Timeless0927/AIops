"""Append-only, no-I/O corrections over hash-bound acceptance artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .evidence_files import sha256

if TYPE_CHECKING:
    from .ledger import AcceptanceLedger


def correct_s01(
    ledger: AcceptanceLedger,
    *,
    acceptance_tool: Path,
    diagnostic: Path,
    reason: str,
) -> dict[str, Any]:
    """Materialize S01 correction inputs, then apply the no-I/O ledger decision."""
    attempt = ledger._manifest["gates"]["S01"][0]
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
    return ledger.correct_s01(
        source_artifact=ledger.evidence.artifact("S01", artifact),
        acceptance_tool_sha256=target_sha,
        diagnostic_conclusion_sha256=diagnostic_sha,
        reason=reason,
    )


def _validate_diagnostic(ledger: AcceptanceLedger, root: Path) -> str:
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
