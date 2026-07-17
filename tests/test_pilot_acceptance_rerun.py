from __future__ import annotations

from pathlib import Path

import pytest

from aiops.acceptance.evidence import GateFailed
from aiops.acceptance.rerun import RerunGateRunner, RerunInputs
from tests.pilot_acceptance_recovery_support import recovery_ledger


REPORT_V1 = {
    "id": "report-run-one", "version": 1,
    "included_investigation_ids": ["investigation-run-one"],
    "status": "published", "source_revision": 7, "source_resolved_at": 1_500.0,
}


class Effects:
    def trigger(self, _scope, *, operation_id: str):
        return {
            "status": "succeeded", "operation_id": operation_id,
            "run_id": "run-controller-uid-2", "trigger_started_at": 2_000.0,
        }

    def reconcile_trigger(self, _scope, *, operation_id: str):
        return self.trigger(_scope, operation_id=operation_id)


class Chain:
    def prepare(self, _scope, _trigger, *, operation_id: str, **_credentials):
        return {
            "status": "succeeded", "operation_id": operation_id,
            "run_id": "run-controller-uid-2", "incident_id": "incident-run-one",
            "alert_fingerprint": "fingerprint-run-two",
            "investigation_id": "investigation-run-two", "investigation_sequence": 2,
            "evidence_step_ids": ["evidence-run-two"],
            "recommended_action_id": "action-run-two",
            "change_request_id": "change-run-two", "phase_id": "phase-run-two",
            "revision_id": "revision-run-two", "dry_run_hash": "e" * 64,
            "target_confirmation": "apps/v1:Deployment:aiops-verification/verification-api",
            "authorization_denials": 3, "grant_count": 0, "command_count": 0,
            "approval_review": {"diff": [{"path": "/spec/template/metadata/annotations"}]},
            "report_v1": REPORT_V1,
            "destination": {"id": "destination-pilot", "revision": "7"},
            "receipt_review": None,
        }

    def reconcile_prepare(self, scope, trigger, *, operation_id: str):
        return self.prepare(scope, trigger, operation_id=operation_id)

    def execute(self, _scope, prepared, *, operation_id: str, **_credentials):
        return {
            "status": "succeeded", "operation_id": operation_id,
            "run_id": prepared["run_id"], "incident_id": prepared["incident_id"],
            "investigation_id": prepared["investigation_id"],
            "change_request_id": prepared["change_request_id"],
            "phase_id": prepared["phase_id"], "revision_id": prepared["revision_id"],
            "approval_id": "approval-run-two", "grant_id": "grant-run-two",
            "command_id": "command-run-two", "execution_id": "execution-run-two",
            "execution_status": "succeeded", "recovery_status": "resolved",
            "resolution": {
                "incident_id": prepared["incident_id"],
                "alert_fingerprint": prepared["alert_fingerprint"],
                "recovery_observation_id": "recovery-run-two",
                "resolved_webhook_request_id": "resolved-webhook-run-two",
            },
            "report_review": {
                "draft_id": "report-draft-run-two", "source_revision": 14,
                "included_investigation_ids": ["investigation-run-one", "investigation-run-two"],
            },
            "report_v1": REPORT_V1,
            "destination": prepared["destination"],
        }

    def reconcile_execute(self, scope, prepared, *, operation_id: str):
        return self.execute(scope, prepared, operation_id=operation_id)

    def publish(self, _scope, executed, narrative, *, operation_id: str, **_credentials):
        return {
            "status": "succeeded", "operation_id": operation_id,
            "run_id": executed["run_id"], "incident_id": executed["incident_id"],
            "investigation_id": executed["investigation_id"],
            "report_v1": REPORT_V1,
            "report_v2": {
                "id": "report-run-two", "version": 2, "status": "published",
                "source_revision": 14,
                "included_investigation_ids": ["investigation-run-one", "investigation-run-two"],
                "narrative": narrative,
            },
            "notification_delivery": {
                "id": "delivery-run-two", "status": "sent",
                "event_id": "incident.resolved:incident-run-one:14", "is_test": False,
                "request_id": "request-run-two", "destination_id": "destination-pilot",
                "destination_revision": "7", "provider_identity": "provider-message-run-two",
                "attempt_count": 1, "attempts": [{"id": "attempt-run-two"}],
                "request": {
                    "event_id": "incident.resolved:incident-run-one:14",
                    "event_type": "incident.resolved",
                    "subject": {"type": "incident", "id": "incident-run-one", "version": 14},
                    "facts": {
                        "incident_id": "incident-run-one", "status": "resolved",
                        "recovery_observation_id": "recovery-run-two",
                        "resolved_webhook_request_id": "resolved-webhook-run-two",
                    },
                },
            },
        }

    def reconcile_publish(self, scope, executed, narrative, *, operation_id: str):
        return self.publish(scope, executed, narrative, operation_id=operation_id)


def _attest(evidence, note: str, *, role: str = "sre") -> None:
    statement = evidence.attestation_statement(
        actor="Pilot User", role=role, gate_ids=["V08"],
        conclusion="passed", note=note,
    )
    evidence.append_attestation(
        statement, signature="signature", public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )


def test_v08_builds_one_independent_second_chain_and_preserves_report_v1(
    tmp_path: Path,
) -> None:
    evidence = recovery_ledger(tmp_path / "evidence", "V08")
    runner = RerunGateRunner(evidence=evidence, effects=Effects(), chain=Chain())
    inputs = RerunInputs(
        release_root=tmp_path,
        sre_username="pilot-sre", sre_password="sre-password",
        no_authority_username="ordinary", no_authority_password="ordinary-password",
        platform_admin_username="platform-admin", platform_admin_password="admin-password",
        narrative={
            "impact": "second run impact", "root_cause": "same controlled fault",
            "resolution_summary": "second governed recovery", "follow_up": "none",
        },
    )

    approval = runner.run_v08(inputs)
    assert approval["status"] == "awaiting_attestation"
    _attest(evidence, f"approval_review_sha256={approval['review_sha256']}")
    report = runner.resume_v08(inputs)
    assert report["status"] == "awaiting_attestation"
    _attest(evidence, f"report_review_sha256={report['review_sha256']}")
    result = runner.resume_v08(inputs)

    assert result == {
        "gate_id": "V08", "status": "passed", "run_id": "run-controller-uid-2",
        "incident_id": "incident-run-one", "investigation_id": "investigation-run-two",
        "report_publication_id": "report-run-two",
        "notification_delivery_id": "delivery-run-two",
    }
    final = evidence.passed_artifact_json("V08", "rerun-and-delivery.json")["value"]
    assert final["report_v1_before"] == final["report_v1_after"] == REPORT_V1
    assert final["report_v2"]["version"] == 2
    assert final["notification_delivery"]["destination_revision"] == "7"


def test_v08_changed_destination_requires_receipt_attestation_before_sre_review(
    tmp_path: Path,
) -> None:
    class ChangedDestinationChain(Chain):
        def prepare(self, *args, **kwargs):
            value = super().prepare(*args, **kwargs)
            value["destination"] = {"id": "destination-pilot", "revision": "8"}
            value["receipt_review"] = {
                "destination_id": "destination-pilot", "revision": "8",
                "delivery_id": "delivery-test-8", "status": "sent",
                "attempt_ids": ["attempt-test-8"],
                "provider_identity": "provider-test-8",
            }
            return value

    evidence = recovery_ledger(tmp_path / "evidence", "V08")
    runner = RerunGateRunner(
        evidence=evidence, effects=Effects(), chain=ChangedDestinationChain(),
    )
    inputs = RerunInputs(
        release_root=tmp_path,
        sre_username="pilot-sre", sre_password="sre-password",
        no_authority_username="ordinary", no_authority_password="ordinary-password",
        platform_admin_username="platform-admin", platform_admin_password="admin-password",
        narrative={
            "impact": "impact", "root_cause": "cause",
            "resolution_summary": "resolved", "follow_up": "follow",
        },
    )

    receipt = runner.run_v08(inputs)
    assert receipt["role"] == "platform_administrator"
    _attest(
        evidence,
        f"notification_receipt_sha256={receipt['review_sha256']}",
        role="platform_administrator",
    )
    approval = runner.resume_v08(inputs)

    assert approval["role"] == "sre"


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_incident", "reused_identity", "reused_execution",
        "report_v1", "delivery_revision", "delivery_failure", "delivery_resolution",
    ],
)
def test_v08_rejects_non_independent_or_mutated_second_chain(
    tmp_path: Path, failure: str,
) -> None:
    class BrokenChain(Chain):
        def prepare(self, *args, **kwargs):
            value = super().prepare(*args, **kwargs)
            if failure == "wrong_incident":
                value["incident_id"] = "incident-run-two"
            if failure == "reused_identity":
                value["change_request_id"] = "change-run-one"
            if failure == "report_v1":
                value["report_v1"] = {**REPORT_V1, "source_revision": 8}
            return value

        def publish(self, *args, **kwargs):
            value = super().publish(*args, **kwargs)
            if failure == "delivery_revision":
                value["notification_delivery"]["destination_revision"] = "8"
            if failure == "delivery_failure":
                value["notification_delivery"]["status"] = "failed"
            if failure == "delivery_resolution":
                value["notification_delivery"]["request"]["facts"][
                    "resolved_webhook_request_id"
                ] = "unrelated-resolution"
            return value

        def execute(self, *args, **kwargs):
            value = super().execute(*args, **kwargs)
            if failure == "reused_execution":
                value["execution_id"] = "execution-run-one"
            return value

    evidence = recovery_ledger(tmp_path / "evidence", "V08")
    runner = RerunGateRunner(evidence=evidence, effects=Effects(), chain=BrokenChain())
    inputs = RerunInputs(
        release_root=tmp_path, sre_username="sre", sre_password="secret",
        no_authority_username="ordinary", no_authority_password="ordinary-secret",
        platform_admin_username="platform-admin", platform_admin_password="admin-secret",
        narrative={
            "impact": "impact", "root_cause": "cause",
            "resolution_summary": "resolved", "follow_up": "follow",
        },
    )
    if failure in {"wrong_incident", "reused_identity", "report_v1"}:
        with pytest.raises(GateFailed, match="V08"):
            runner.run_v08(inputs)
        assert evidence.failed_gate == "V08"
        return
    approval = runner.run_v08(inputs)
    _attest(evidence, f"approval_review_sha256={approval['review_sha256']}")
    if failure == "reused_execution":
        with pytest.raises(GateFailed, match="V08"):
            runner.resume_v08(inputs)
        return
    report = runner.resume_v08(inputs)
    _attest(evidence, f"report_review_sha256={report['review_sha256']}")
    with pytest.raises(GateFailed, match="V08"):
        runner.resume_v08(inputs)


def test_v08_interrupted_trigger_reconciles_without_recreating_job(tmp_path: Path) -> None:
    class InterruptedEffects(Effects):
        def __init__(self) -> None:
            self.dispatched = 0
            self.reconciled = 0

        def trigger(self, scope, *, operation_id: str):
            self.dispatched += 1
            self.result = super().trigger(scope, operation_id=operation_id)
            raise KeyboardInterrupt

        def reconcile_trigger(self, _scope, *, operation_id: str):
            self.reconciled += 1
            return self.result

    effects = InterruptedEffects()
    evidence = recovery_ledger(tmp_path / "evidence", "V08")
    inputs = RerunInputs(
        release_root=tmp_path, sre_username="sre", sre_password="secret",
        no_authority_username="ordinary", no_authority_password="ordinary-secret",
        platform_admin_username="platform-admin", platform_admin_password="admin-secret",
        narrative={
            "impact": "impact", "root_cause": "cause",
            "resolution_summary": "resolved", "follow_up": "follow",
        },
    )
    with pytest.raises(KeyboardInterrupt):
        RerunGateRunner(evidence=evidence, effects=effects, chain=Chain()).run_v08(inputs)
    reopened = type(evidence).open(evidence.root, attestation_verifier=lambda _item: None)
    result = RerunGateRunner(evidence=reopened, effects=effects, chain=Chain()).resume_v08(inputs)

    assert result["status"] == "awaiting_attestation"
    assert effects.dispatched == effects.reconciled == 1


def test_v08_unprovable_interrupted_prepare_fails_without_replay(tmp_path: Path) -> None:
    class InterruptedChain(Chain):
        def __init__(self) -> None:
            self.dispatched = 0
            self.reconciled = 0

        def prepare(self, *args, **kwargs):
            self.dispatched += 1
            raise KeyboardInterrupt

        def reconcile_prepare(self, *args, **kwargs):
            self.reconciled += 1
            return None

    chain = InterruptedChain()
    evidence = recovery_ledger(tmp_path / "evidence", "V08")
    inputs = RerunInputs(
        release_root=tmp_path, sre_username="sre", sre_password="secret",
        no_authority_username="ordinary", no_authority_password="ordinary-secret",
        platform_admin_username="platform-admin", platform_admin_password="admin-secret",
        narrative={
            "impact": "impact", "root_cause": "cause",
            "resolution_summary": "resolved", "follow_up": "follow",
        },
    )
    with pytest.raises(KeyboardInterrupt):
        RerunGateRunner(evidence=evidence, effects=Effects(), chain=chain).run_v08(inputs)
    reopened = type(evidence).open(evidence.root, attestation_verifier=lambda _item: None)
    with pytest.raises(GateFailed, match="unprovable"):
        RerunGateRunner(evidence=reopened, effects=Effects(), chain=chain).resume_v08(inputs)

    assert chain.dispatched == chain.reconciled == 1
