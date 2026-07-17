"""Shared format-v2 Recovery ledger fixtures for public gate tests."""

from __future__ import annotations

from itertools import count
from pathlib import Path

from aiops.acceptance.evidence import GATE_SEQUENCE, AcceptanceEvidence


def recovery_ledger(
    tmp_path: Path, gate_id: str, *, run_id: str = "run-controller-uid-1",
    v06_run_id: str | None = None,
) -> AcceptanceEvidence:
    ids = count(1)
    v06_run_id = v06_run_id or run_id
    evidence = AcceptanceEvidence.create(
        tmp_path,
        acceptance_id=f"stateful-recovery-{gate_id.lower()}",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        acceptance_tool_sha256="c" * 64,
        gate_contract_revision="pilot-clean-acceptance-v2",
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        access_profile="http_nodeport",
        new_execution_id=lambda: f"execution-{next(ids)}",
        attestation_verifier=lambda _item: None,
    )
    for predecessor in GATE_SEQUENCE[: GATE_SEQUENCE.index(gate_id)]:
        started_at = evidence.start_gate(predecessor)
        artifacts = []
        if predecessor == "V05":
            artifacts.append(evidence.write_json("V05", "approval-and-execution.json", {
                "execution": {
                    "id": "execution-run-one",
                    "change_request_id": "change-run-one",
                    "phase_id": "phase-run-one",
                    "revision_id": "revision-run-one",
                    "approval_id": "approval-run-one",
                    "command_id": "command-run-one",
                    "grant": {"id": "grant-run-one"},
                    "steps": [{"command_id": "command-run-one"}],
                },
            }))
        elif predecessor == "V03":
            artifacts.append(evidence.write_json("V03", "diagnosis.json", {
                "evidence_steps": [{"id": "evidence-run-one"}],
                "recommended_action": {"id": "action-run-one"},
            }))
        elif predecessor == "P01":
            artifacts.append(evidence.write_json("P01", "artifact-inventory.json", []))
        elif predecessor == "S05":
            artifacts.append(evidence.write_json("S05", "connector-read-verification.json", {
                "enrollment_id": "enrollment-run-one",
                "connector_id": "connector-prod",
                "cluster_id": "pilot-cluster",
                "state": "online",
                "read_verification": {"status": "verified"},
                "platform_status": {"readiness": "ready"},
            }))
        elif predecessor == "V06":
            artifacts.append(evidence.write_json("V06", "recovery.json", {
                "run_id": v06_run_id,
                "alert_fingerprint": "fingerprint-run-one",
                "telemetry": {
                    "run_id": v06_run_id, "alert_fingerprint": "fingerprint-run-one",
                    "recovery_metric_observed_at": 1310.0,
                    "recovery_log_observed_at": 1305.0,
                    "recovery_log_ref_hashes": ["f" * 64],
                },
            }))
        elif predecessor == "V07":
            artifacts.append(evidence.write_json("V07", "report-and-delivery.json", {
                "run_id": run_id,
                "incident_id": "incident-run-one",
                "investigation_id": "investigation-run-one",
                "report": {
                    "id": "report-run-one", "version": 1,
                    "included_investigation_ids": ["investigation-run-one"],
                    "status": "published", "source_revision": 7,
                    "source_resolved_at": 1_500.0,
                },
                "report_sha256": "d" * 64,
                "notification_delivery": {
                    "id": "delivery-run-one",
                    "event_id": "incident.resolved:incident-run-one:7",
                    "request_id": "request-run-one",
                    "provider_identity": "provider-message-run-one",
                    "attempt_count": 1,
                    "attempts": [{"id": "attempt-run-one"}],
                },
                "destination": {"id": "destination-pilot", "revision": "7"},
            }))
        evidence.record_gate(
            predecessor,
            "not_applicable" if predecessor == "I04" else "passed",
            artifacts,
            started_at=started_at,
        )
    return evidence


def attest_recovery(
    evidence: AcceptanceEvidence, gate_id: str, review_sha256: str,
) -> None:
    statement = evidence.attestation_statement(
        actor="Pilot Operator",
        role="platform_operator",
        gate_ids=[gate_id],
        conclusion="passed",
        note=f"recovery_review_sha256={review_sha256}",
    )
    evidence.append_attestation(
        statement,
        signature=f"signature-{gate_id}",
        public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )


def attest_recovery_approval(
    evidence: AcceptanceEvidence, gate_id: str, review_sha256: str,
) -> None:
    statement = evidence.attestation_statement(
        actor="Pilot SRE",
        role="sre",
        gate_ids=[gate_id],
        conclusion="passed",
        note=f"approval_review_sha256={review_sha256}",
    )
    evidence.append_attestation(
        statement,
        signature=f"signature-{gate_id}-approval",
        public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )


def attest_recovery_drift(
    evidence: AcceptanceEvidence, gate_id: str, review_sha256: str,
) -> None:
    statement = evidence.attestation_statement(
        actor="Pilot Operator",
        role="platform_operator",
        gate_ids=[gate_id],
        conclusion="passed",
        note=f"drift_review_sha256={review_sha256}",
    )
    evidence.append_attestation(
        statement,
        signature=f"signature-{gate_id}-drift",
        public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )
