"""Promotion capability within the Acceptance Evidence Module."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from . import human_attestation
from .evidence_files import atomic_write, sha256
from .gate_contract import GATE_SEQUENCE

if TYPE_CHECKING:
    from .evidence import AcceptanceEvidence


Decision = Literal["promote", "no_promote"]
REQUIRED_ROLE_ATTESTATIONS = {
    "I05": ("platform_administrator",),
    "S04": ("platform_administrator",),
    "S05": ("platform_operator",),
    "V04": ("sre",),
    "V05": ("sre",),
    "V07": ("sre",),
    "V08": ("sre",),
    "C03": ("platform_operator", "platform_administrator", "sre"),
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DECISION_FIELDS = {
    "acceptance_id", "candidate_sha256", "acceptance_tool_sha256",
    "gate_contract_revision", "actor", "role", "decision", "eligibility",
    "decided_at", "note",
}


class PromotionError(ValueError):
    """Promotion facts violate the evaluated ledger contract."""


def _derive_eligibility(ledger: AcceptanceEvidence) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    gates = ledger._manifest["gates"]
    missing = [gate_id for gate_id in GATE_SEQUENCE if not gates.get(gate_id)]
    failed = [
        gate_id for gate_id in GATE_SEQUENCE
        if gates.get(gate_id) and gates[gate_id][0].get("status") == "failed"
    ]
    opened = [
        gate_id for gate_id in GATE_SEQUENCE
        if gates.get(gate_id) and gates[gate_id][0].get("status") == "open"
    ]
    invalid = [
        gate_id for gate_id in GATE_SEQUENCE
        if gates.get(gate_id)
        and gates[gate_id][0].get("status") not in (
            {"passed", "not_applicable"} if gate_id == "I04" and ledger.access_profile == "http_nodeport" else {"passed"}
        )
        and gate_id not in failed
        and gate_id not in opened
    ]
    if missing:
        reasons.append({"code": "mandatory_gates_missing", "gate_ids": missing})
    if failed:
        reasons.append({"code": "mandatory_gates_failed", "gate_ids": failed})
    if opened:
        reasons.append({"code": "gate_still_open", "gate_ids": opened})
    if invalid:
        reasons.append({"code": "mandatory_gate_result_invalid", "gate_ids": invalid})
    violations = list(ledger._manifest.get("identity_violations", []))
    if violations:
        reasons.append({"code": "acceptance_identity_drift", "fields": violations})
    reasons.extend(_attestation_reasons(ledger))
    return {"conclusion": "ineligible" if reasons else "eligible", "reasons": reasons}


def _attestation_reasons(ledger: AcceptanceEvidence) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    items = human_attestation.load(ledger.attestation_path)
    verifier = ledger._attestation_verifier
    invalid: list[str] = []
    if verifier is None:
        reasons.append({"code": "attestation_verifier_unavailable"})
    else:
        for item in items:
            try:
                verifier(item)
            except Exception:
                statement = item.get("statement", {})
                invalid.append(
                    f"{statement.get('gate_ids', ['?'])[0]}:{statement.get('role', '?')}"
                )
    if invalid:
        reasons.append({"code": "attestation_signature_invalid", "requirements": sorted(set(invalid))})
    missing = [
        f"{gate_id}:{role}"
        for gate_id, roles in REQUIRED_ROLE_ATTESTATIONS.items()
        for role in roles
        if not any(
            item.get("statement", {}).get("gate_ids") == [gate_id]
            and item["statement"].get("role") == role
            and item["statement"].get("conclusion") == "passed"
            for item in items
        )
    ]
    if missing:
        reasons.append({"code": "required_attestations_missing", "requirements": missing})
    return reasons


def evaluate(ledger: AcceptanceEvidence) -> dict[str, Any]:
    if ledger._manifest.get("eligibility") is not None:
        raise PromotionError("eligibility has already been evaluated")
    ledger._ensure_writable()
    if ledger.failed_gate is None and ledger.frontier is not None:
        raise PromotionError("evaluate requires C03 completion or a terminal gate failure")
    integrity_failed = False
    try:
        ledger._validate_loaded()
    except ValueError:
        integrity_failed = True
    result = _derive_eligibility(ledger)
    if integrity_failed:
        result["reasons"].insert(0, {"code": "ledger_integrity_failed"})
        result["conclusion"] = "ineligible"
    result["evaluated_at"] = ledger._now()
    ledger._manifest["eligibility"] = result
    ledger._persist_manifest()
    return json.loads(json.dumps(result))


class PromotionDecision:
    """The release owner's distinct signed decision action."""

    def __init__(self, ledger: AcceptanceEvidence) -> None:
        self.ledger = ledger

    def statement(
        self, *, actor: str, decision: Decision, note: str
    ) -> dict[str, Any]:
        eligibility = self.ledger._manifest.get("eligibility")
        if eligibility is None:
            raise PromotionError("Promotion Decision requires eligibility evaluation")
        if not actor or not note or decision not in {"promote", "no_promote"}:
            raise PromotionError("Promotion Decision fields must be valid and non-empty")
        return {
            "acceptance_id": self.ledger._manifest["acceptance_id"],
            "candidate_sha256": self.ledger.candidate_sha256,
            "acceptance_tool_sha256": self.ledger.acceptance_tool_sha256,
            "gate_contract_revision": self.ledger.gate_contract_revision,
            "actor": actor,
            "role": "release_owner",
            "decision": decision,
            "eligibility": eligibility["conclusion"],
            "decided_at": self.ledger._now(),
            "note": note,
        }

    def record(
        self,
        statement: dict[str, Any],
        *,
        signature: str,
        public_key: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        if self.ledger._manifest.get("seal") or (self.ledger.root / "SHA256SUMS").exists():
            raise PromotionError("sealed evidence ledger is permanently read-only")
        if self.ledger._manifest.get("promotion_decision") is not None:
            raise PromotionError("Promotion Decision is immutable and already exists")
        error = _decision_error(self.ledger, statement, signature, public_key, fingerprint)
        if error:
            raise PromotionError(error)
        item = {
            "statement": json.loads(json.dumps(statement)),
            "signature": signature,
            "public_key": public_key,
            "fingerprint": fingerprint,
        }
        if self.ledger._attestation_verifier is None:
            raise PromotionError("Promotion Decision verifier is required")
        try:
            self.ledger._attestation_verifier(item)
        except Exception as exc:
            raise PromotionError("Promotion Decision signature is invalid") from exc
        self.ledger._manifest["promotion_decision"] = item
        self.ledger._persist_manifest()
        return json.loads(json.dumps(item))


def _decision_error(
    ledger: AcceptanceEvidence,
    statement: Any,
    signature: str,
    public_key: str,
    fingerprint: str,
) -> str | None:
    eligibility = ledger._manifest.get("eligibility")
    if not isinstance(eligibility, dict):
        return "Promotion Decision requires eligibility evaluation"
    expected = {
        "acceptance_id": ledger._manifest["acceptance_id"],
        "candidate_sha256": ledger.candidate_sha256,
        "acceptance_tool_sha256": ledger.acceptance_tool_sha256,
        "gate_contract_revision": ledger.gate_contract_revision,
        "role": "release_owner",
        "eligibility": eligibility.get("conclusion"),
    }
    if (
        not isinstance(statement, dict)
        or set(statement) != _DECISION_FIELDS
        or any(statement.get(key) != value for key, value in expected.items())
    ):
        return "Promotion Decision does not match the evaluated acceptance identity"
    if not all(isinstance(statement.get(key), str) and statement[key] for key in ("actor", "decided_at", "note")):
        return "Promotion Decision actor and statement fields must be non-empty"
    if len(statement["actor"]) > 256 or len(statement["decided_at"]) > 64 or len(statement["note"]) > 2048:
        return "Promotion Decision statement exceeds its bounded size"
    decision = statement.get("decision")
    if decision not in {"promote", "no_promote"}:
        return "Promotion Decision conclusion is invalid"
    if eligibility.get("conclusion") == "ineligible" and decision == "promote":
        return "ineligible acceptance cannot be promoted"
    if human_attestation.signature_identity_error(signature, public_key, fingerprint):
        return "Promotion Decision signature identity is required"
    return None


def _persisted_facts_error(ledger: AcceptanceEvidence) -> str | None:
    eligibility = ledger._manifest.get("eligibility")
    if eligibility is None:
        if ledger._manifest.get("promotion_decision") is not None or ledger._manifest.get("seal") is not None:
            return "promotion facts exist before eligibility"
        return None
    if (
        not isinstance(eligibility, dict)
        or eligibility.get("conclusion") not in {"eligible", "ineligible"}
        or not isinstance(eligibility.get("reasons"), list)
        or not isinstance(eligibility.get("evaluated_at"), str)
    ):
        return "eligibility fact is invalid"
    derived = _derive_eligibility(ledger)
    if (
        eligibility.get("conclusion") != derived["conclusion"]
        or eligibility.get("reasons") != derived["reasons"]
    ):
        return "stored eligibility does not match immutable ledger facts"
    decision = ledger._manifest.get("promotion_decision")
    if decision is None:
        return "seal exists before Promotion Decision" if ledger._manifest.get("seal") else None
    if not isinstance(decision, dict):
        return "Promotion Decision fact is invalid"
    error = _decision_error(
        ledger,
        decision.get("statement"),
        decision.get("signature"),
        decision.get("public_key"),
        decision.get("fingerprint"),
    )
    if error:
        return error
    verify = ledger._attestation_verifier
    if verify is None:
        return "Promotion Decision verifier is required"
    try:
        verify(decision)
    except Exception:
        return "Promotion Decision signature is invalid"
    return None


def seal(ledger: AcceptanceEvidence) -> Path:
    checksum_path = ledger.root / "SHA256SUMS"
    if checksum_path.exists():
        raise PromotionError("evidence ledger is already sealed")
    ledger._validate_loaded()
    seal_fact = ledger._manifest.get("seal")
    if seal_fact is None:
        error = _persisted_facts_error(ledger)
        if error:
            raise PromotionError(error)
        if ledger._manifest.get("eligibility") is None:
            raise PromotionError("seal requires eligibility evaluation")
        if ledger._manifest.get("promotion_decision") is None:
            raise PromotionError("seal requires a signed Promotion Decision")
        ledger._manifest["seal"] = {
            "sealed_at": ledger._now(),
            "checksum_path": checksum_path.name,
        }
        ledger._persist_manifest()
    else:
        error = _persisted_facts_error(ledger)
        if error:
            raise PromotionError(error)
    files = sorted(path for path in ledger.root.rglob("*") if path.is_file())
    lines = [f"{sha256(path)}  {path.relative_to(ledger.root)}" for path in files]
    atomic_write(
        checksum_path,
        ("\n".join(lines) + "\n").encode(),
        staging_dir=ledger.root.parent,
    )
    return checksum_path


def manifest_validation_error(ledger: AcceptanceEvidence) -> str | None:
    return _persisted_facts_error(ledger) or seal_validation_error(ledger)


def is_sealed(ledger: AcceptanceEvidence) -> bool:
    return ledger._manifest.get("seal") is not None and (ledger.root / "SHA256SUMS").is_file()


def seal_validation_error(ledger: AcceptanceEvidence) -> str | None:
    checksum_path = ledger.root / "SHA256SUMS"
    seal_fact = ledger._manifest.get("seal")
    if seal_fact is None:
        return "final checksum exists without a seal fact" if checksum_path.exists() else None
    if (
        not isinstance(seal_fact, dict)
        or not isinstance(seal_fact.get("sealed_at"), str)
        or seal_fact.get("checksum_path") != checksum_path.name
    ):
        return "seal fact is invalid"
    if not checksum_path.exists():
        return None
    try:
        entries = _checksum_entries(checksum_path)
    except (OSError, ValueError):
        return "final checksum manifest is invalid"
    files = sorted(path for path in ledger.root.rglob("*") if path.is_file() and path != checksum_path)
    actual = {str(path.relative_to(ledger.root)): sha256(path) for path in files}
    if entries != actual:
        return "final checksum does not match the sealed ledger"
    return None


def _checksum_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative_value = line.partition("  ")
        relative = Path(relative_value)
        if (
            separator != "  "
            or not _SHA256.fullmatch(digest)
            or relative.is_absolute()
            or ".." in relative.parts
            or relative_value in entries
            or relative_value == path.name
        ):
            raise ValueError("invalid checksum entry")
        entries[relative_value] = digest
    return entries
