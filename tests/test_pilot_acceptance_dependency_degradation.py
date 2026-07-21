from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiops.acceptance.dependency_degradation import (
    R03_TARGET,
    R04_TARGET,
    DependencyDegradationGateRunner,
)
from aiops.acceptance.evidence import EvidenceError, GateFailed
from aiops.acceptance.recovery import RecoveryScope, RecoveryTarget
from tests.pilot_acceptance_recovery_support import (
    attest_recovery,
    attest_recovery_approval,
    recovery_ledger,
)


class DependencyAdapter:
    def __init__(
        self, *, interrupt_action: str | None = None,
        invalid_action: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.interrupt_action = interrupt_action
        self.invalid_action = invalid_action
        self.interrupted = False
        self.scaled = False
        self.reapplied = False

    def snapshot_dependency(
        self, scope: RecoveryScope, target: RecoveryTarget,
    ) -> dict[str, object]:
        value = {
            "identity": {
                "candidate_sha256": scope.candidate_sha256,
                "release_inventory_sha256": scope.release_inventory_sha256,
                "kube_context": scope.kube_context,
                "cluster_identity_sha256": scope.cluster_identity_sha256,
            },
            "target": {
                "owner": target.owner,
                "kind": target.kind,
                "name": target.name,
                "uid": f"uid-{target.owner}",
                "resource_version": "1",
                "replicas": 1,
                "ready": True,
                "image_ids": ["sha256:" + "1" * 64],
            },
        }
        if target == R03_TARGET:
            value["connector_material"] = {
                "uid": "connector-material-uid", "resource_version": "1",
                "key_inventory_sha256": "2" * 64, "value_sha256": "3" * 64,
            }
        return value

    def snapshot_ready(
        self, scope: RecoveryScope, target: RecoveryTarget,
    ) -> dict[str, object]:
        return (
            {
                "availability": "available", "reason_code": "ready",
                "connector_id": "connector-prod", "cluster_id": "pilot-cluster",
                "heartbeat": "online", "read_verification": "verified",
            }
            if target == R03_TARGET else {
                "availability": "available", "reason_code": "ready",
                "observability_readiness": "ready", "loki_state": "available",
            }
        )

    def prepare_connector_probe(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]:
        self.calls.append("prepare")
        result = {
            "status": "succeeded",
            "operation_id": operation_id,
            "change_request_id": "change-r03-probe",
            "phase_id": "phase-r03-probe",
            "revision_id": "revision-r03-probe",
            "dry_run_hash": "4" * 64,
            "target_confirmation": "apps/v1:Deployment:aiops-verification/verification-api",
            "approval_status": "awaiting_approval",
            "change_summary": {
                "target": {
                    "api_version": "apps/v1", "kind": "Deployment",
                    "namespace": "aiops-verification", "name": "verification-api",
                },
                "operation": "patch",
                "diff": [{
                    "op": "add",
                    "path": "/metadata/annotations/aiops.dev~1r03-grant-probe",
                    "after": operation_id,
                }],
                "post_checks": [{"type": "json_pointer"}],
                "rollback": {"status": "available"},
            },
        }
        self._interrupt("prepare")
        return result

    def reconcile_connector_probe(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None:
        self.calls.append("reconcile-prepare")
        return None

    def scale_to_zero(
        self, target: RecoveryTarget, *, original_replicas: int, operation_id: str,
    ) -> dict[str, object]:
        self.calls.append("scale")
        self.scaled = True
        if self.interrupt_action == "scale" and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        result = self._scale_result(target, operation_id, original_replicas)
        if self.invalid_action == "scale":
            result["target"] = "deployment/wrong-target"
        return result

    @staticmethod
    def _scale_result(
        target: RecoveryTarget, operation_id: str, original_replicas: int,
    ) -> dict[str, object]:
        return {
            "status": "succeeded", "operation_id": operation_id,
            "target": target.key, "uid": f"uid-{target.owner}",
            "original_replicas": original_replicas, "replicas": 0, "ready": False,
        }

    def reconcile_scaled_to_zero(
        self, target: RecoveryTarget, *, original_replicas: int, operation_id: str,
    ) -> dict[str, object] | None:
        self.calls.append("reconcile-scale")
        return self._scale_result(target, operation_id, original_replicas) if self.scaled else None

    def verify_connector_unavailable(
        self,
        scope: RecoveryScope,
        prepared: dict[str, object],
        *,
        operation_id: str,
    ) -> dict[str, object]:
        self.calls.append("verify-unavailable")
        denials = {
            name: {
                "request_id": f"{operation_id}/{name}",
                "status": 409,
                "error_code": "cluster_not_ready",
            }
            for name in ("live_evidence", "dry_run", "grant", "dispatch")
        }
        result = {
            "status": "succeeded", "operation_id": operation_id,
            "dependency": "connector",
            "platform": {"availability": "unavailable", "reason_code": "owner_unavailable"},
            "denials": denials, "active_commands": 0, "grants_created": 0,
            "prepared_change_request_id": prepared["change_request_id"],
            "approval": {
                "request_id": f"{operation_id}/approval", "status": 201,
                "response_request_id": f"{operation_id}/approval",
            },
            "other_dependency": {
                "owner": "loki", "availability": "available",
                "observability_readiness": "ready", "loki_state": "available",
            },
        }
        if self.invalid_action == "connector-unavailable":
            result["active_commands"] = 1
        self._interrupt("connector-unavailable")
        return result

    def reconcile_connector_unavailable(
        self,
        scope: RecoveryScope,
        prepared: dict[str, object],
        *,
        operation_id: str,
    ) -> dict[str, object] | None:
        self.calls.append("reconcile-unavailable")
        return None

    def reapply_candidate(self, *, operation_id: str) -> dict[str, object]:
        self.calls.append("reapply")
        self.reapplied = True
        result = {
            "status": "succeeded", "operation_id": operation_id,
            "candidate_sha256": "a" * 64,
            "release_inventory_sha256": hashlib.sha256(b"[]\n").hexdigest(),
            "owners_ready": True,
        }
        if self.invalid_action == "reapply":
            result["candidate_sha256"] = "0" * 64
        self._interrupt("reapply")
        return result

    def reconcile_reapply(self, *, operation_id: str) -> dict[str, object] | None:
        self.calls.append("reconcile-reapply")
        if not self.reapplied:
            return None
        return {
            "status": "succeeded", "operation_id": operation_id,
            "candidate_sha256": "a" * 64,
            "release_inventory_sha256": hashlib.sha256(b"[]\n").hexdigest(),
            "owners_ready": True,
        }

    def verify_connector_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]:
        self.calls.append("verify-recovered")
        result = {
            "status": "succeeded", "operation_id": operation_id,
            "dependency": "connector", "availability": "available",
            "heartbeat": "online", "read_verification": "verified",
        }
        self._interrupt("connector-recovered")
        return result

    def reconcile_connector_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None:
        self.calls.append("reconcile-recovered")
        return None

    def verify_loki_unavailable(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]:
        self.calls.append("verify-loki-unavailable")
        result = {
            "status": "succeeded", "operation_id": operation_id,
            "dependency": "loki",
            "platform": {"availability": "unavailable", "reason_code": "owner_unavailable"},
            "mcp": {"status": "failed", "error_code": "backend_unavailable", "evidence_refs": []},
            "other_dependency": {
                "owner": "connector", "availability": "available",
                "connector_id": scope.connector_id, "cluster_id": scope.cluster_id,
                "heartbeat": "online", "read_verification": "verified",
            },
        }
        if self.invalid_action == "loki-unavailable":
            result["mcp"]["evidence_refs"] = [{"source": "loki", "ref_id": "fake"}]  # type: ignore[index]
        self._interrupt("loki-unavailable")
        return result

    def reconcile_loki_unavailable(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None:
        self.calls.append("reconcile-loki-unavailable")
        return None

    def verify_loki_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]:
        self.calls.append("verify-loki-recovered")
        result = {
            "status": "succeeded", "operation_id": operation_id,
            "dependency": "loki", "availability": "available",
            "retained_log_refs_sha256": scope.recovery_log_refs_sha256,
            "retained_query": {"status": "succeeded", "matched": 1,
                               "evidence_ref": {"source": "loki", "ref_id": "ref-retained"}},
            "fresh_probe_request_id": operation_id + "/fresh",
            "fresh_query": {"status": "succeeded", "matched": 1,
                            "evidence_ref": {"source": "loki", "ref_id": "ref-fresh"}},
        }
        if self.invalid_action == "loki-recovered":
            result["fresh_query"] = {"status": "succeeded", "matched": 0}
        self._interrupt("loki-recovered")
        return result

    def reconcile_loki_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None:
        self.calls.append("reconcile-loki-recovered")
        return None

    def _interrupt(self, action: str) -> None:
        if self.interrupt_action == action and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt


def _complete_r03(
    evidence, runner: DependencyDegradationGateRunner,
) -> dict[str, str]:
    paused = runner.run_r03()
    attest_recovery(evidence, "R03", paused["review_sha256"])
    approval = runner.resume_r03()
    assert approval["status"] == "awaiting_attestation"
    assert approval["role"] == "sre"
    attest_recovery_approval(evidence, "R03", approval["review_sha256"])
    return runner.resume_r03()


def test_r03_scales_connector_to_zero_proves_fail_closed_then_reapplies(
    tmp_path: Path,
) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter()
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)

    result = _complete_r03(evidence, runner)

    assert result == {"gate_id": "R03", "status": "passed", "operations": "5"}
    assert adapter.calls == [
        "prepare", "scale", "verify-unavailable", "reapply", "verify-recovered",
    ]
    final = evidence.passed_artifact_json("R03", "dependency-degradation.json")["value"]
    assert final["target"] == R03_TARGET.key
    assert final["during"]["denials"]["dispatch"]["error_code"] == "cluster_not_ready"


def test_r03_interrupted_scale_reconciles_without_replaying(tmp_path: Path) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter(interrupt_action="scale")
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)
    paused = runner.run_r03()
    attest_recovery(evidence, "R03", paused["review_sha256"])
    approval = runner.resume_r03()
    attest_recovery_approval(evidence, "R03", approval["review_sha256"])

    try:
        runner.resume_r03()
    except KeyboardInterrupt:
        pass
    result = runner.resume_r03()

    assert result["status"] == "passed"
    assert adapter.calls.count("scale") == 1
    assert adapter.calls.count("reconcile-scale") == 1


def test_r04_proves_loki_unavailable_then_queries_retained_and_fresh_logs(
    tmp_path: Path,
) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter()
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)
    _complete_r03(evidence, runner)
    adapter.calls.clear()

    paused = runner.run_r04()
    assert paused["status"] == "awaiting_attestation"
    assert adapter.calls == []
    attest_recovery(evidence, "R04", paused["review_sha256"])
    result = runner.resume_r04()

    assert result == {"gate_id": "R04", "status": "passed", "operations": "4"}
    assert adapter.calls == [
        "scale", "verify-loki-unavailable", "reapply", "verify-loki-recovered",
    ]
    final = evidence.passed_artifact_json("R04", "dependency-degradation.json")["value"]
    assert final["target"] == R04_TARGET.key
    assert final["recovered"]["fresh_query"]["evidence_ref"]["source"] == "loki"


@pytest.mark.parametrize(
    ("action", "reconcile_call"),
    [
        ("prepare", "reconcile-prepare"),
        ("connector-unavailable", "reconcile-unavailable"),
        ("connector-recovered", "reconcile-recovered"),
    ],
)
def test_r03_unprovable_browser_probe_interruption_fails_without_replay(
    tmp_path: Path, action: str, reconcile_call: str,
) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter(interrupt_action=action)
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)
    paused = runner.run_r03()
    attest_recovery(evidence, "R03", paused["review_sha256"])

    if action == "prepare":
        with pytest.raises(KeyboardInterrupt):
            runner.resume_r03()
    else:
        approval = runner.resume_r03()
        attest_recovery_approval(evidence, "R03", approval["review_sha256"])
        with pytest.raises(KeyboardInterrupt):
            runner.resume_r03()

    with pytest.raises(GateFailed, match="unprovable"):
        runner.resume_r03()
    assert adapter.calls.count(
        "prepare" if action == "prepare" else (
            "verify-unavailable" if action == "connector-unavailable" else "verify-recovered"
        )
    ) == 1
    assert adapter.calls.count(reconcile_call) == 1


def test_r03_interrupted_reapply_reconciles_without_replaying(tmp_path: Path) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter(interrupt_action="reapply")
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)
    paused = runner.run_r03()
    attest_recovery(evidence, "R03", paused["review_sha256"])
    approval = runner.resume_r03()
    attest_recovery_approval(evidence, "R03", approval["review_sha256"])

    with pytest.raises(KeyboardInterrupt):
        runner.resume_r03()
    assert runner.resume_r03()["status"] == "passed"
    assert adapter.calls.count("reapply") == 1
    assert adapter.calls.count("reconcile-reapply") == 1


@pytest.mark.parametrize("action", ["scale", "reapply"])
def test_r04_interrupted_kubernetes_effect_reconciles_without_replay(
    tmp_path: Path, action: str,
) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter()
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)
    _complete_r03(evidence, runner)
    adapter.calls.clear()
    adapter.interrupt_action = action
    adapter.interrupted = False
    paused = runner.run_r04()
    attest_recovery(evidence, "R04", paused["review_sha256"])

    with pytest.raises(KeyboardInterrupt):
        runner.resume_r04()
    assert runner.resume_r04()["status"] == "passed"
    assert adapter.calls.count(action) == 1
    assert adapter.calls.count(f"reconcile-{action}") == 1


@pytest.mark.parametrize(
    ("action", "dispatch_call", "reconcile_call"),
    [
        ("loki-unavailable", "verify-loki-unavailable", "reconcile-loki-unavailable"),
        ("loki-recovered", "verify-loki-recovered", "reconcile-loki-recovered"),
    ],
)
def test_r04_unprovable_probe_interruption_fails_without_replay(
    tmp_path: Path, action: str, dispatch_call: str, reconcile_call: str,
) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter()
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)
    _complete_r03(evidence, runner)
    adapter.calls.clear()
    adapter.interrupt_action = action
    adapter.interrupted = False
    paused = runner.run_r04()
    attest_recovery(evidence, "R04", paused["review_sha256"])

    with pytest.raises(KeyboardInterrupt):
        runner.resume_r04()
    with pytest.raises(GateFailed, match="unprovable"):
        runner.resume_r04()
    assert adapter.calls.count(dispatch_call) == 1
    assert adapter.calls.count(reconcile_call) == 1


@pytest.mark.parametrize(
    ("gate_id", "invalid_action", "forbidden_call"),
    [
        ("R03", "connector-unavailable", "reapply"),
        ("R04", "loki-unavailable", "reapply"),
        ("R03", "scale", "verify-unavailable"),
        ("R03", "reapply", "verify-recovered"),
        ("R04", "loki-recovered", "unused"),
    ],
)
def test_dependency_degradation_invalid_stage_fails_closed(
    tmp_path: Path, gate_id: str, invalid_action: str, forbidden_call: str,
) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter()
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)
    if gate_id == "R04":
        _complete_r03(evidence, runner)
        adapter.calls.clear()
        adapter.invalid_action = invalid_action
        paused = runner.run_r04()
        attest_recovery(evidence, "R04", paused["review_sha256"])
        with pytest.raises(GateFailed):
            runner.resume_r04()
    else:
        adapter.invalid_action = invalid_action
        paused = runner.run_r03()
        attest_recovery(evidence, "R03", paused["review_sha256"])
        approval = runner.resume_r03()
        attest_recovery_approval(evidence, "R03", approval["review_sha256"])
        with pytest.raises(GateFailed):
            runner.resume_r03()
    if forbidden_call != "unused":
        assert forbidden_call not in adapter.calls


def test_r04_cannot_start_before_complete_r03_recovery(tmp_path: Path) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter()
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)

    with pytest.raises(EvidenceError, match="frontier"):
        runner.run_r04()
    assert adapter.calls == []


def test_recovery_reviews_exclude_parallel_outages_and_fake_logs(tmp_path: Path) -> None:
    evidence = recovery_ledger(tmp_path, "R03")
    adapter = DependencyAdapter()
    runner = DependencyDegradationGateRunner(evidence=evidence, effects=adapter, probe=adapter)
    r03 = runner.run_r03()
    assert r03["status"] == "awaiting_attestation"
    r03_review = evidence.resume_gate("R03").artifacts[-1].path.read_text()
    assert "parallel_dependency_outages" in r03_review
    attest_recovery(evidence, "R03", r03["review_sha256"])
    approval = runner.resume_r03()
    attest_recovery_approval(evidence, "R03", approval["review_sha256"])
    runner.resume_r03()

    r04 = runner.run_r04()
    assert r04["status"] == "awaiting_attestation"
    r04_review = evidence.resume_gate("R04").artifacts[-1].path.read_text()
    assert "parallel_dependency_outages" in r04_review
    assert "fake_log_injection" in r04_review
