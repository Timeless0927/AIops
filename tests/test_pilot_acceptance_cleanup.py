from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops.acceptance.cleanup import CleanupGateRunner, CleanupHistoryScope, CleanupScope
from aiops.acceptance.ledger import GateFailed
from tests.pilot_acceptance_recovery_support import recovery_ledger


class Effects:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def snapshot(self, _scope: CleanupScope):
        return {
            "system_namespace": {"name": "aiops-system", "uid": "system-uid"},
            "system_resources": [{
                "kind": "Deployment", "name": "aiops-gateway", "uid": "gateway-uid",
            }],
            "fixture_namespace": {"name": "aiops-verification", "uid": "fixture-uid"},
            "run_job": {
                "name": "verification-trigger", "namespace": "aiops-verification",
                "uid": "run-controller-uid-2",
            },
        }

    def delete_run(self, _scope, _before, *, operation_id: str):
        self.calls.append("run")
        return {
            "operation_id": operation_id,
            "system_namespace": {"name": "aiops-system", "uid": "system-uid"},
            "system_resources": [{
                "kind": "Deployment", "name": "aiops-gateway", "uid": "gateway-uid",
            }],
            "fixture_namespace": {"name": "aiops-verification", "uid": "fixture-uid"},
            "run_job": None,
        }

    def reconcile_delete_run(self, scope, before, *, operation_id: str):
        return self.delete_run(scope, before, operation_id=operation_id)

    def delete_base(self, _scope, _before, *, operation_id: str):
        self.calls.append("base")
        return {
            "operation_id": operation_id,
            "system_namespace": {"name": "aiops-system", "uid": "system-uid"},
            "system_resources": [{
                "kind": "Deployment", "name": "aiops-gateway", "uid": "gateway-uid",
            }],
            "fixture_namespace": None, "run_job": None,
        }

    def reconcile_delete_base(self, scope, before, *, operation_id: str):
        return self.delete_base(scope, before, operation_id=operation_id)


class History:
    def read(self, scope: CleanupHistoryScope):
        return {
            "incident_id": scope.incident_id,
            "identity_inventory": scope.identity_inventory,
            "phase_executions": [
                {"id": execution_id, "status": "succeeded"}
                for execution_id in scope.identity_inventory["execution_ids"]
            ],
            "reports": [scope.report_v1, scope.report_v2],
            "notification_deliveries": list(scope.notification_deliveries),
            "resource": {
                "id": scope.deployment_target_id,
                "binding_state": "bound", "availability": "unavailable",
            },
        }


def _runner(tmp_path: Path, frontier: str):
    evidence = recovery_ledger(tmp_path / "evidence", frontier)
    effects = Effects()
    return evidence, effects, CleanupGateRunner(
        evidence=evidence, effects=effects, history=History(),
    )


def _attest(evidence, role: str, review_sha256: str) -> None:
    statement = evidence.attestation_statement(
        actor=f"Pilot {role}", role=role, gate_ids=["C03"], conclusion="passed",
        note=f"manifest_summary_sha256={review_sha256}",
    )
    evidence.append_attestation(
        statement, signature=f"signature-{role}", public_key="ssh-ed25519 test",
        fingerprint="SHA256:test",
    )


def test_c01_deletes_only_run_then_base_and_preserves_system_namespace(tmp_path: Path) -> None:
    evidence, effects, runner = _runner(tmp_path, "C01")
    release_root = tmp_path / "evidence/release"

    result = runner.run_c01(release_root)

    assert result == {"gate_id": "C01", "status": "passed"}
    assert effects.calls == ["run", "base"]
    cleanup = evidence.passed_artifact_json("C01", "fixture-cleanup.json")["value"]
    assert cleanup["before"]["system_namespace"] == cleanup["after"]["system_namespace"]
    assert cleanup["after"]["fixture_namespace"] is None


def test_c01_fails_if_cleanup_changes_aiops_system_identity(tmp_path: Path) -> None:
    class ProductDeletingEffects(Effects):
        def delete_base(self, *args, **kwargs):
            value = super().delete_base(*args, **kwargs)
            value["system_namespace"]["uid"] = "replacement-system-uid"
            return value

    evidence = recovery_ledger(tmp_path / "evidence", "C01")
    runner = CleanupGateRunner(
        evidence=evidence, effects=ProductDeletingEffects(), history=History(),
    )

    with pytest.raises(GateFailed, match="C01"):
        runner.run_c01(tmp_path / "evidence/release")


def test_c01_interruption_reconciles_delete_without_replay(tmp_path: Path) -> None:
    class InterruptedEffects(Effects):
        dispatched = 0
        reconciled = 0

        def delete_run(self, *args, **kwargs):
            self.dispatched += 1
            self.result = super().delete_run(*args, **kwargs)
            raise KeyboardInterrupt

        def reconcile_delete_run(self, *_args, **_kwargs):
            self.reconciled += 1
            return self.result

    evidence = recovery_ledger(tmp_path / "evidence", "C01")
    effects = InterruptedEffects()
    release_root = tmp_path / "evidence/release"
    runner = CleanupGateRunner(evidence=evidence, effects=effects, history=History())
    with pytest.raises(KeyboardInterrupt):
        runner.run_c01(release_root)
    reopened = type(evidence).open(
        evidence.root, attestation_verifier=lambda _item: None,
    )

    result = CleanupGateRunner(
        evidence=reopened, effects=effects, history=History(),
    ).resume_c01(release_root)

    assert result["status"] == "passed"
    assert effects.dispatched == effects.reconciled == 1


def test_c02_records_two_public_histories_and_unavailable_target(tmp_path: Path) -> None:
    evidence, _effects, runner = _runner(tmp_path, "C02")

    result = runner.run_c02()

    assert result == {"gate_id": "C02", "status": "passed"}
    history = evidence.passed_artifact_json("C02", "public-history.json")["value"]
    assert history["identity_inventory"]["investigation_ids"] == [
        "investigation-run-one", "investigation-run-two",
    ]
    assert history["resource"]["availability"] == "unavailable"
    assert all(item["status"] == "succeeded" for item in history["phase_executions"])
    assert [item["id"] for item in history["reports"]] == [
        "report-run-one", "report-run-two",
    ]


def test_c02_rejects_online_projection_for_deleted_target(tmp_path: Path) -> None:
    class OnlineHistory(History):
        def read(self, scope):
            value = super().read(scope)
            value["resource"]["availability"] = "available"
            return value

    evidence = recovery_ledger(tmp_path / "evidence", "C02")
    runner = CleanupGateRunner(evidence=evidence, effects=Effects(), history=OnlineHistory())

    with pytest.raises(GateFailed, match="C02"):
        runner.run_c02()


def test_c02_rejects_secret_bearing_public_history(tmp_path: Path) -> None:
    class SecretHistory(History):
        def read(self, scope):
            value = super().read(scope)
            value["password"] = "must-not-enter-evidence"
            return value

    evidence = recovery_ledger(tmp_path / "evidence", "C02")
    runner = CleanupGateRunner(evidence=evidence, effects=Effects(), history=SecretHistory())

    with pytest.raises(GateFailed, match="C02"):
        runner.run_c02()


@pytest.mark.parametrize("field", ["report", "delivery"])
def test_c02_rejects_mutated_published_history(tmp_path: Path, field: str) -> None:
    class MutatedHistory(History):
        def read(self, scope):
            value = super().read(scope)
            if field == "report":
                value["reports"][0] = {**value["reports"][0], "version": 99}
            else:
                value["notification_deliveries"][0] = {
                    **value["notification_deliveries"][0], "status": "failed",
                }
            return value

    evidence = recovery_ledger(tmp_path / "evidence", "C02")
    runner = CleanupGateRunner(evidence=evidence, effects=Effects(), history=MutatedHistory())
    with pytest.raises(GateFailed, match="C02"):
        runner.run_c02()


def test_c03_requires_three_roles_on_exact_bounded_manifest_summary(tmp_path: Path) -> None:
    evidence, _effects, runner = _runner(tmp_path, "C03")

    waiting = runner.run_c03(known_secrets=("must-not-leak",))
    assert waiting["role"] == "platform_operator"
    review_sha256 = waiting["review_sha256"]
    for role, next_role in (
        ("platform_operator", "platform_administrator"),
        ("platform_administrator", "sre"),
    ):
        _attest(evidence, role, review_sha256)
        waiting = runner.resume_c03(known_secrets=("must-not-leak",))
        assert waiting["role"] == next_role
    _attest(evidence, "sre", review_sha256)

    result = runner.resume_c03(known_secrets=("must-not-leak",))

    assert result == {"gate_id": "C03", "status": "passed"}
    index = evidence.passed_artifact_json("C03", "artifact-index.json")["value"]
    summary = evidence.passed_artifact_json("C03", "manifest-summary.json")["value"]
    assert [item["gate_id"] for item in index["gates"]][-1] == "C02"
    assert summary["artifact_index_sha256"]
    assert "must-not-leak" not in json.dumps(index) + json.dumps(summary)


def test_c03_rejects_attestation_not_bound_to_manifest_hash(tmp_path: Path) -> None:
    evidence, _effects, runner = _runner(tmp_path, "C03")
    runner.run_c03()
    _attest(evidence, "platform_operator", "0" * 64)

    with pytest.raises(GateFailed, match="C03"):
        runner.resume_c03()
