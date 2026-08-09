"""Eligibility and promotion-fact validation inside the Acceptance Ledger Module."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .gate_contract import GATE_SEQUENCE
from . import promotion

if TYPE_CHECKING:
    from .ledger import AcceptanceLedger


def derive_eligibility(
    ledger: AcceptanceLedger,
    required_attestations: dict[str, tuple[str, ...]],
) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    gates = ledger._manifest["gates"]
    missing = [gate_id for gate_id in GATE_SEQUENCE if not gates.get(gate_id)]
    failed = [
        gate_id
        for gate_id in GATE_SEQUENCE
        if ledger.effective_status(gate_id) == "failed"
    ]
    opened = [
        gate_id
        for gate_id in GATE_SEQUENCE
        if ledger.effective_status(gate_id) == "open"
    ]
    invalid = [
        gate_id
        for gate_id in GATE_SEQUENCE
        if gates.get(gate_id)
        and ledger.effective_status(gate_id)
        not in (
            {"passed", "not_applicable"}
            if gate_id == "I04" and ledger.access_profile == "http_nodeport"
            else {"passed"}
        )
        and gate_id not in failed
        and gate_id not in opened
    ]
    if missing:
        reasons.append({"code": "mandatory_gates_missing", "gate_ids": missing})
    if failed:
        reasons.append(
            {
                "code": "mandatory_gates_failed",
                "gate_ids": failed,
                "failure_attributions": {
                    gate_id: gates[gate_id][0]["failure_attribution"]
                    for gate_id in failed
                },
            }
        )
    if opened:
        reasons.append({"code": "gate_still_open", "gate_ids": opened})
    if invalid:
        reasons.append({"code": "mandatory_gate_result_invalid", "gate_ids": invalid})
    violations = list(ledger._manifest.get("identity_violations", []))
    if violations:
        reasons.append({"code": "acceptance_identity_drift", "fields": violations})
    reasons.extend(_attestation_reasons(ledger, required_attestations))
    return {"conclusion": "ineligible" if reasons else "eligible", "reasons": reasons}


def promotion_facts_error(
    ledger: AcceptanceLedger,
    required_attestations: dict[str, tuple[str, ...]],
) -> str | None:
    eligibility = ledger._manifest.get("eligibility")
    if eligibility is None:
        if (
            ledger._manifest.get("promotion_decision") is not None
            or ledger._manifest.get("seal") is not None
        ):
            return "promotion facts exist before eligibility"
        return None
    if (
        not isinstance(eligibility, dict)
        or eligibility.get("conclusion") not in {"eligible", "ineligible"}
        or not isinstance(eligibility.get("reasons"), list)
        or not isinstance(eligibility.get("evaluated_at"), str)
    ):
        return "eligibility fact is invalid"
    derived = derive_eligibility(ledger, required_attestations)
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
    error = promotion.decision_error(
        ledger,
        decision.get("statement"),
        decision.get("signature"),
        decision.get("public_key"),
        decision.get("fingerprint"),
    )
    if error:
        return error
    if ledger._attestation_verifier is None:
        return "Promotion Decision verifier is required"
    try:
        ledger._attestation_verifier(decision)
    except Exception:
        return "Promotion Decision signature is invalid"
    return None


def _attestation_reasons(
    ledger: AcceptanceLedger,
    required_attestations: dict[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    items = ledger.evidence.attestations()
    invalid: list[str] = []
    if ledger._attestation_verifier is None:
        reasons.append({"code": "attestation_verifier_unavailable"})
    else:
        for item in items:
            try:
                ledger._attestation_verifier(item)
            except Exception:
                statement = item.get("statement", {})
                invalid.append(
                    f"{statement.get('gate_ids', ['?'])[0]}:{statement.get('role', '?')}"
                )
    if invalid:
        reasons.append(
            {
                "code": "attestation_signature_invalid",
                "requirements": sorted(set(invalid)),
            }
        )
    missing = [
        f"{gate_id}:{role}"
        for gate_id, roles in required_attestations.items()
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
