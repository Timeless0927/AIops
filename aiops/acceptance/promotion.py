"""Promotion capability within the Acceptance Evidence Module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from . import human_attestation

if TYPE_CHECKING:
    from .ledger import AcceptanceLedger


Decision = Literal["promote", "no_promote"]
_DECISION_FIELDS = {
    "acceptance_id", "candidate_sha256", "acceptance_tool_sha256",
    "gate_contract_revision", "actor", "role", "decision", "eligibility",
    "decided_at", "note",
}


class PromotionError(ValueError):
    """Promotion facts violate the evaluated ledger contract."""


class PromotionDecision:
    """The release owner's distinct signed decision action."""

    def __init__(self, ledger: AcceptanceLedger) -> None:
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
        if self.ledger._manifest.get("seal") or self.ledger.evidence.checksum_exists():
            raise PromotionError("sealed evidence ledger is permanently read-only")
        if self.ledger._manifest.get("promotion_decision") is not None:
            raise PromotionError("Promotion Decision is immutable and already exists")
        error = decision_error(self.ledger, statement, signature, public_key, fingerprint)
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


def decision_error(
    ledger: AcceptanceLedger,
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
