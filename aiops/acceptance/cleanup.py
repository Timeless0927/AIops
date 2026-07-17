"""C01-C03 fixture cleanup and public-history evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .credentials import assert_public_payload
from .evidence import AcceptanceEvidence, Artifact, GateExecution
from .integration_support import fail_gate
from .recovery_journal import RecoveryJournal


@dataclass(frozen=True)
class CleanupScope:
    release_root: Path
    expected_run_id: str


@dataclass(frozen=True)
class CleanupHistoryScope:
    incident_id: str
    identity_inventory: dict[str, list[str]]
    report_v1: dict[str, object]
    report_v2: dict[str, object]
    notification_deliveries: tuple[dict[str, object], dict[str, object]]
    deployment_target_id: str


class CleanupEffects(Protocol):
    def snapshot(self, scope: CleanupScope) -> dict[str, object]: ...

    def delete_run(
        self, scope: CleanupScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_delete_run(
        self, scope: CleanupScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None: ...

    def delete_base(
        self, scope: CleanupScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_delete_base(
        self, scope: CleanupScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None: ...


class CleanupHistory(Protocol):
    def read(self, scope: CleanupHistoryScope) -> dict[str, object]: ...


class CleanupGateRunner:
    """Advances the three cleanup gates without owning product state."""

    def __init__(
        self, *, evidence: AcceptanceEvidence,
        effects: CleanupEffects, history: CleanupHistory,
    ) -> None:
        self.evidence = evidence
        self.effects = effects
        self.history = history
        self.journal = RecoveryJournal(evidence)

    def run_c01(self, release_root: Path) -> dict[str, str]:
        started_at = self.evidence.start_gate("C01")
        execution = self.evidence.resume_gate("C01")
        artifacts: list[Artifact] = []
        try:
            scope = self._cleanup_scope(release_root)
            before = self.effects.snapshot(scope)
            self._validate_before(before, scope)
            artifacts.extend([
                self.evidence.write_json("C01", "intent.json", {
                    "release_root": str(scope.release_root),
                    "expected_run_id": scope.expected_run_id,
                }),
                self.evidence.write_json("C01", "before.json", before),
            ])
            return self._advance_c01(execution, artifacts, scope, before)
        except Exception as exc:
            fail_gate(self.evidence, "C01", artifacts, exc, (), started_at)

    def resume_c01(self, release_root: Path) -> dict[str, str]:
        execution = self.evidence.resume_gate("C01")
        artifacts = list(execution.artifacts)
        try:
            scope = self._cleanup_scope(release_root)
            intent = self.journal.artifact_json(artifacts, "intent.json")
            before = self.journal.artifact_json(artifacts, "before.json")
            if intent != {
                "release_root": str(scope.release_root),
                "expected_run_id": scope.expected_run_id,
            }:
                raise ValueError("C01 resume inputs drifted from durable intent")
            self._validate_before(before, scope)
            return self._advance_c01(execution, artifacts, scope, before)
        except Exception as exc:
            fail_gate(self.evidence, "C01", artifacts, exc, (), execution.started_at)

    def _advance_c01(
        self, execution: GateExecution, artifacts: list[Artifact],
        scope: CleanupScope, before: dict[str, object],
    ) -> dict[str, str]:
        run_operation = self.journal.operation_id("C01", execution.execution_id, "delete-run")
        after_run = self.journal.effect(
            "C01", execution, artifacts, operation_id=run_operation,
            kind="verification_run_delete", artifact_name="run-deleted.json",
            dispatch=lambda: self.effects.delete_run(
                scope, before, operation_id=run_operation,
            ),
            reconcile=lambda: self.effects.reconcile_delete_run(
                scope, before, operation_id=run_operation,
            ),
        )
        self._validate_run_deleted(before, after_run, run_operation)
        execution = self.evidence.resume_gate("C01")
        base_operation = self.journal.operation_id("C01", execution.execution_id, "delete-base")
        after = self.journal.effect(
            "C01", execution, artifacts, operation_id=base_operation,
            kind="verification_base_delete", artifact_name="base-deleted.json",
            dispatch=lambda: self.effects.delete_base(
                scope, before, operation_id=base_operation,
            ),
            reconcile=lambda: self.effects.reconcile_delete_base(
                scope, before, operation_id=base_operation,
            ),
        )
        self._validate_base_deleted(before, after, base_operation)
        artifacts.append(self.evidence.write_json("C01", "fixture-cleanup.json", {
            "before": before, "after_run": after_run, "after": after,
        }))
        execution = self.evidence.resume_gate("C01")
        self._reconcile_execution("C01", execution, {
            "fixture_namespace_absent": True,
            "system_namespace": after["system_namespace"],
            "system_resources": after["system_resources"],
        })
        self.evidence.record_gate("C01", "passed", artifacts, started_at=execution.started_at)
        return {"gate_id": "C01", "status": "passed"}

    def run_c02(self) -> dict[str, str]:
        started_at = self.evidence.start_gate("C02")
        execution = self.evidence.resume_gate("C02")
        artifacts: list[Artifact] = []
        try:
            return self._advance_c02(execution, artifacts, self._history_scope())
        except Exception as exc:
            fail_gate(self.evidence, "C02", artifacts, exc, (), started_at)

    def resume_c02(self) -> dict[str, str]:
        execution = self.evidence.resume_gate("C02")
        artifacts = list(execution.artifacts)
        try:
            return self._advance_c02(execution, artifacts, self._history_scope())
        except Exception as exc:
            fail_gate(self.evidence, "C02", artifacts, exc, (), execution.started_at)

    def _advance_c02(
        self, execution: GateExecution, artifacts: list[Artifact], scope: CleanupHistoryScope,
    ) -> dict[str, str]:
        retained = self.journal.optional_artifact_json(artifacts, "public-history.json")
        history = retained if retained is not None else self.history.read(scope)
        assert_public_payload(history)
        self._validate_history(history, scope)
        if retained is None:
            artifacts.append(self.evidence.write_json("C02", "public-history.json", history))
        self._reconcile_execution("C02", execution, {
            "incident_id": scope.incident_id,
            "deployment_target_id": scope.deployment_target_id,
            "terminal": True,
        })
        self.evidence.record_gate("C02", "passed", artifacts, started_at=execution.started_at)
        return {"gate_id": "C02", "status": "passed"}

    def run_c03(self, *, known_secrets: tuple[str, ...] = ()) -> dict[str, str]:
        started_at = self.evidence.start_gate("C03")
        execution = self.evidence.resume_gate("C03")
        artifacts: list[Artifact] = []
        try:
            review = self._c03_review(artifacts, known_secrets)
            waiting = self._await_c03_role(review)
            return waiting if waiting is not None else self._complete_c03(
                execution, artifacts, review, known_secrets,
            )
        except Exception as exc:
            fail_gate(self.evidence, "C03", artifacts, exc, known_secrets, started_at)

    def resume_c03(self, *, known_secrets: tuple[str, ...] = ()) -> dict[str, str]:
        execution = self.evidence.resume_gate("C03")
        artifacts = list(execution.artifacts)
        try:
            review = self._c03_review(artifacts, known_secrets)
            waiting = self._await_c03_role(review)
            if waiting is not None:
                return waiting
            return self._complete_c03(execution, artifacts, review, known_secrets)
        except Exception as exc:
            fail_gate(
                self.evidence, "C03", artifacts, exc, known_secrets, execution.started_at,
            )

    def _complete_c03(
        self, execution: GateExecution, artifacts: list[Artifact], review: Artifact,
        known_secrets: tuple[str, ...],
    ) -> dict[str, str]:
        statements = []
        for role in ("platform_operator", "platform_administrator", "sre"):
            statements.extend(
                item["statement"]
                for item in self.evidence.require_verified_attestation("C03", role=role)
            )
        artifacts.append(self.evidence.write_json("C03", "cleanup-evidence.json", {
            "manifest_summary_sha256": review.sha256, "attestations": statements,
        }, known_secrets=known_secrets))
        self._reconcile_execution("C03", execution, {
            "manifest_summary_sha256": review.sha256,
            "roles": ["platform_operator", "platform_administrator", "sre"],
            "terminal": True,
        })
        self.evidence.record_gate(
            "C03", "passed", artifacts, started_at=execution.started_at,
        )
        return {"gate_id": "C03", "status": "passed"}

    def _reconcile_execution(
        self, gate_id: str, execution: GateExecution, public_fact: dict[str, object],
    ) -> None:
        if any(
            item.get("operation_id") == execution.execution_id
            for item in execution.reconciliations
        ):
            return
        self.evidence.reconcile_operation(
            gate_id, operation_id=execution.execution_id,
            outcome="succeeded", public_fact=public_fact,
        )

    def _c03_review(
        self, artifacts: list[Artifact], known_secrets: tuple[str, ...],
    ) -> Artifact:
        current = self.evidence.completed_artifact_index()
        retained_index = self.journal.optional_artifact_json(artifacts, "artifact-index.json")
        index_value = {"gates": current}
        if retained_index is not None and retained_index != index_value:
            raise ValueError("C03 completed artifact index drifted")
        if retained_index is None:
            artifacts.append(self.evidence.write_json(
                "C03", "artifact-index.json", index_value, known_secrets=known_secrets,
            ))
        index_artifact = self.journal.artifact(artifacts, "artifact-index.json")
        artifact_count = sum(len(item["artifacts"]) for item in current)
        review_value = {
            "release_sha256": self.evidence.candidate_sha256,
            "acceptance_tool_sha256": self.evidence.acceptance_tool_sha256,
            "gate_contract_revision": self.evidence.gate_contract_revision,
            "cluster_identity_sha256": self.evidence.cluster_identity_sha256,
            "access_profile": self.evidence.access_profile,
            "covered_gate_ids": [item["gate_id"] for item in current],
            "artifact_count": artifact_count,
            "artifact_index_path": index_artifact.relative_path,
            "artifact_index_sha256": index_artifact.sha256,
        }
        retained_review = self.journal.optional_artifact_json(artifacts, "manifest-summary.json")
        if retained_review is not None and retained_review != review_value:
            raise ValueError("C03 manifest summary drifted")
        if retained_review is None:
            artifacts.append(self.evidence.write_json(
                "C03", "manifest-summary.json", review_value,
                known_secrets=known_secrets,
            ))
        return self.journal.artifact(artifacts, "manifest-summary.json")

    def _await_c03_role(self, review: Artifact) -> dict[str, str] | None:
        note = f"manifest_summary_sha256={review.sha256}"
        for role in ("platform_operator", "platform_administrator", "sre"):
            if not self.evidence.attestations_for("C03", role=role):
                return {
                    "gate_id": "C03", "status": "awaiting_attestation",
                    "role": role, "review_sha256": review.sha256,
                }
            self.journal.require_bound_attestation("C03", role=role, note=note)
        return None

    def _cleanup_scope(self, release_root: Path) -> CleanupScope:
        intent = self.evidence.passed_artifact_json("V08", "intent.json")["value"]
        final = self.evidence.passed_artifact_json(
            "V08", "rerun-and-delivery.json",
        )["value"]
        if (
            not release_root.is_absolute()
            or intent.get("release_root") != str(release_root)
            or not isinstance(final.get("run_id"), str)
            or not final["run_id"]
        ):
            raise ValueError("C01 cleanup scope does not match V08")
        return CleanupScope(release_root, str(final["run_id"]))

    def _history_scope(self) -> CleanupHistoryScope:
        v03 = self.evidence.passed_artifact_json("V03", "diagnosis.json")["value"]
        v05 = self.evidence.passed_artifact_json(
            "V05", "approval-and-execution.json",
        )["value"]["execution"]
        v07 = self.evidence.passed_artifact_json(
            "V07", "report-and-delivery.json",
        )["value"]
        prepared = self.evidence.passed_artifact_json(
            "V08", "prepared-chain.json",
        )["value"]
        executed = self.evidence.passed_artifact_json(
            "V08", "executed-chain.json",
        )["value"]
        v08 = self.evidence.passed_artifact_json(
            "V08", "rerun-and-delivery.json",
        )["value"]
        report_v2 = v08.get("report_v2")
        facts = report_v2.get("facts") if isinstance(report_v2, dict) else None
        incident = facts.get("incident") if isinstance(facts, dict) else None
        recoveries = facts.get("recovery_observations") if isinstance(facts, dict) else None
        old_steps = v03.get("evidence_steps")
        inventory = {
            "investigation_ids": [v07.get("investigation_id"), prepared.get("investigation_id")],
            "evidence_step_ids": [
                *(item.get("id") for item in old_steps if isinstance(item, dict)),
                *prepared.get("evidence_step_ids", []),
            ] if isinstance(old_steps, list) else [],
            "recommended_action_ids": [
                v03.get("recommended_action", {}).get("id"),
                prepared.get("recommended_action_id"),
            ],
            "change_request_ids": [v05.get("change_request_id"), prepared.get("change_request_id")],
            "phase_ids": [v05.get("phase_id"), prepared.get("phase_id")],
            "revision_ids": [v05.get("revision_id"), prepared.get("revision_id")],
            "approval_ids": [v05.get("approval_id"), executed.get("approval_id")],
            "grant_ids": [v05.get("grant", {}).get("id"), executed.get("grant_id")],
            "command_ids": [v05.get("command_id"), executed.get("command_id")],
            "execution_ids": [v05.get("id"), executed.get("execution_id")],
            "recovery_observation_ids": [
                item.get("id") for item in recoveries if isinstance(item, dict)
            ] if isinstance(recoveries, list) else [],
            "report_ids": [v07.get("report", {}).get("id"), report_v2.get("id")],
            "delivery_ids": [
                v07.get("notification_delivery", {}).get("id"),
                v08.get("notification_delivery", {}).get("id"),
            ],
        }
        if (
            v08.get("incident_id") != v07.get("incident_id")
            or v08.get("report_v1_before") != v07.get("report")
            or v08.get("report_v1_after") != v07.get("report")
            or not isinstance(incident, dict)
            or not isinstance(incident.get("deployment_target_id"), str)
            or any(
                len(values) < 2
                or any(not isinstance(item, str) or not item for item in values)
                or len(values) != len(set(values))
                for values in inventory.values()
            )
        ):
            raise ValueError("C02 immutable two-round identity scope is incomplete")
        return CleanupHistoryScope(
            incident_id=str(v08["incident_id"]),
            identity_inventory={key: list(values) for key, values in inventory.items()},
            report_v1=dict(v07["report"]), report_v2=dict(report_v2),
            notification_deliveries=(
                dict(v07["notification_delivery"]),
                dict(v08["notification_delivery"]),
            ),
            deployment_target_id=str(incident["deployment_target_id"]),
        )

    @staticmethod
    def _validate_before(value: dict[str, object], scope: CleanupScope) -> None:
        system = value.get("system_namespace")
        fixture = value.get("fixture_namespace")
        job = value.get("run_job")
        if (
            not isinstance(system, dict) or system.get("name") != "aiops-system"
            or not system.get("uid")
            or not isinstance(value.get("system_resources"), list)
            or not value["system_resources"]
            or not isinstance(fixture, dict)
            or fixture.get("name") != "aiops-verification" or not fixture.get("uid")
            or not isinstance(job, dict)
            or job.get("name") != "verification-trigger"
            or job.get("namespace") != "aiops-verification"
            or job.get("uid") != scope.expected_run_id
        ):
            raise ValueError("C01 exact fixture baseline is incomplete")

    @staticmethod
    def _validate_run_deleted(
        before: dict[str, object], value: dict[str, object], operation_id: str,
    ) -> None:
        if (
            value.get("operation_id") != operation_id
            or value.get("run_job") is not None
            or value.get("fixture_namespace") != before.get("fixture_namespace")
            or value.get("system_namespace") != before.get("system_namespace")
            or value.get("system_resources") != before.get("system_resources")
        ):
            raise ValueError("C01 verification run deletion is not exact")

    @staticmethod
    def _validate_base_deleted(
        before: dict[str, object], value: dict[str, object], operation_id: str,
    ) -> None:
        if (
            value.get("operation_id") != operation_id
            or value.get("run_job") is not None
            or value.get("fixture_namespace") is not None
            or value.get("system_namespace") != before.get("system_namespace")
            or value.get("system_resources") != before.get("system_resources")
        ):
            raise ValueError("C01 verification base deletion changed product resources")

    @staticmethod
    def _validate_history(value: dict[str, object], scope: CleanupHistoryScope) -> None:
        resource = value.get("resource")
        executions = value.get("phase_executions")
        if (
            value.get("incident_id") != scope.incident_id
            or value.get("identity_inventory") != scope.identity_inventory
            or value.get("reports") != [scope.report_v1, scope.report_v2]
            or value.get("notification_deliveries") != list(scope.notification_deliveries)
            or not isinstance(executions, list) or len(executions) != 2
            or [item.get("id") for item in executions if isinstance(item, dict)]
            != scope.identity_inventory["execution_ids"]
            or any(
                not isinstance(item, dict) or item.get("status") != "succeeded"
                for item in executions
            )
            or not isinstance(resource, dict)
            or resource.get("id") != scope.deployment_target_id
            or resource.get("binding_state") != "bound"
            or resource.get("availability") != "unavailable"
        ):
            raise ValueError("C02 public two-round history is incomplete or mutable")
