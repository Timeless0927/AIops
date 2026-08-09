"""R03/R04 dependency degradation gates for the Recovery Module."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Protocol

from .ledger import AcceptanceLedger
from .evidence_types import Artifact, GateResult
from .integration_support import fail_gate
from .recovery import (
    NAMESPACE,
    RecoveryScope,
    RecoveryTarget,
    load_recovery_scope,
    parse_recovery_scope,
)
from .recovery_journal import RecoveryJournal


R03_TARGET = RecoveryTarget("connector", "deployment", "aiops-connector")
R04_TARGET = RecoveryTarget("loki", "deployment", "aiops-loki")


class DependencyEffects(Protocol):
    def snapshot_dependency(
        self, scope: RecoveryScope, target: RecoveryTarget,
    ) -> dict[str, object]: ...

    def scale_to_zero(
        self, target: RecoveryTarget, *, original_replicas: int, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_scaled_to_zero(
        self, target: RecoveryTarget, *, original_replicas: int, operation_id: str,
    ) -> dict[str, object] | None: ...

    def reapply_candidate(self, *, operation_id: str) -> dict[str, object]: ...

    def reconcile_reapply(self, *, operation_id: str) -> dict[str, object] | None: ...


class DependencyProbe(Protocol):
    def snapshot_ready(
        self, scope: RecoveryScope, target: RecoveryTarget,
    ) -> dict[str, object]: ...

    def prepare_connector_probe(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_connector_probe(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None: ...

    def verify_connector_unavailable(
        self,
        scope: RecoveryScope,
        prepared: dict[str, object],
        *,
        operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_connector_unavailable(
        self,
        scope: RecoveryScope,
        prepared: dict[str, object],
        *,
        operation_id: str,
    ) -> dict[str, object] | None: ...

    def verify_connector_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_connector_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None: ...

    def verify_loki_unavailable(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_loki_unavailable(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None: ...

    def verify_loki_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_loki_recovered(
        self, scope: RecoveryScope, *, operation_id: str,
    ) -> dict[str, object] | None: ...


class DependencyDegradationGateRunner:
    """Owns fixed Connector/Loki outages, fail-closed probes, and same-candidate recovery."""

    def __init__(
        self, *, evidence: AcceptanceLedger, effects: DependencyEffects,
        probe: DependencyProbe,
    ) -> None:
        self.evidence = evidence
        self.effects = effects
        self.probe = probe
        self.journal = RecoveryJournal(evidence)

    def run_r03(self) -> dict[str, str]:
        gate_id = "R03"
        started_at = self.evidence.start_gate(gate_id)
        artifacts: list[Artifact] = []
        try:
            scope = load_recovery_scope(self.evidence)
            before = self._snapshot(scope, R03_TARGET)
            self._validate_before(before, scope, R03_TARGET)
            before_artifact = self.evidence.write_json(gate_id, "before.json", before)
            artifacts.append(before_artifact)
            intent = {
                "scope": asdict(scope),
                "target": asdict(R03_TARGET),
                "effects": self._r03_effects(),
                "before_sha256": before_artifact.sha256,
            }
            artifacts.append(self.evidence.write_json(gate_id, "intent.json", intent))
            review = {
                "gate_id": gate_id,
                "namespace": NAMESPACE,
                "candidate_sha256": scope.candidate_sha256,
                "effects": intent["effects"],
                "before_sha256": before_artifact.sha256,
                "claims_excluded": [
                    "parallel_dependency_outages", "credential_rotation", "database_patch",
                ],
            }
            review_artifact = self.evidence.write_json(
                gate_id, "recovery-review.json", review,
            )
            artifacts.append(review_artifact)
            return {
                "status": "awaiting_attestation",
                "gate_id": gate_id,
                "review_sha256": review_artifact.sha256,
            }
        except Exception as exc:
            fail_gate(self.evidence, gate_id, artifacts, exc, (), started_at)

    def run_r04(self) -> dict[str, str]:
        gate_id = "R04"
        started_at = self.evidence.start_gate(gate_id)
        artifacts: list[Artifact] = []
        try:
            scope = load_recovery_scope(self.evidence)
            r03 = self.evidence.passed_artifact_json(
                "R03", "dependency-degradation.json",
            )["value"]
            r03_recovered = r03.get("recovered") if isinstance(r03, dict) else None
            if (
                not isinstance(r03_recovered, dict)
                or r03_recovered.get("availability") != "available"
                or r03_recovered.get("heartbeat") != "online"
                or r03_recovered.get("read_verification") != "verified"
            ):
                raise ValueError("R04 requires complete R03 Connector recovery")
            before = self._snapshot(scope, R04_TARGET)
            self._validate_before(before, scope, R04_TARGET)
            before_artifact = self.evidence.write_json(gate_id, "before.json", before)
            artifacts.append(before_artifact)
            intent = {
                "scope": asdict(scope), "target": asdict(R04_TARGET),
                "effects": self._r04_effects(), "before_sha256": before_artifact.sha256,
                "r03_after_sha256": self.evidence.passed_artifact(
                    "R03", "dependency-degradation.json",
                ).sha256,
            }
            artifacts.append(self.evidence.write_json(gate_id, "intent.json", intent))
            review = {
                "gate_id": gate_id, "namespace": NAMESPACE,
                "candidate_sha256": scope.candidate_sha256,
                "effects": intent["effects"], "before_sha256": before_artifact.sha256,
                "claims_excluded": ["parallel_dependency_outages", "fake_log_injection"],
            }
            review_artifact = self.evidence.write_json(
                gate_id, "recovery-review.json", review,
            )
            artifacts.append(review_artifact)
            return {
                "status": "awaiting_attestation", "gate_id": gate_id,
                "review_sha256": review_artifact.sha256,
            }
        except Exception as exc:
            fail_gate(self.evidence, gate_id, artifacts, exc, (), started_at)

    def resume_r03(self) -> dict[str, str]:
        gate_id = "R03"
        execution = self.evidence.resume_gate(gate_id)
        artifacts = list(execution.artifacts)
        try:
            intent = self.journal.artifact_json(artifacts, "intent.json")
            before = self.journal.artifact_json(artifacts, "before.json")
            review = self.journal.artifact(artifacts, "recovery-review.json")
            scope = parse_recovery_scope(intent.get("scope"))
            if (
                scope != load_recovery_scope(self.evidence)
                or
                intent.get("target") != asdict(R03_TARGET)
                or intent.get("effects") != self._r03_effects()
                or intent.get("before_sha256")
                != self.journal.artifact(artifacts, "before.json").sha256
            ):
                raise ValueError("R03 durable intent drifted")
            self.journal.require_operator_attestation(gate_id, review.sha256)
            prepared = self._r03_prepare(execution, artifacts, scope)
            approval_review = self._r03_approval_review(artifacts, prepared)
            if not self.evidence.attestations_for(gate_id, role="sre"):
                return {
                    "status": "awaiting_attestation", "gate_id": gate_id,
                    "role": "sre", "review_sha256": approval_review.sha256,
                }
            self.journal.require_bound_attestation(
                gate_id,
                role="sre",
                note=f"approval_review_sha256={approval_review.sha256}",
            )
            execution = self.evidence.resume_gate(gate_id)
            scaled = self._scale(gate_id, execution, artifacts, R03_TARGET, before)
            execution = self.evidence.resume_gate(gate_id)
            during = self._r03_during(execution, artifacts, scope, prepared)
            execution = self.evidence.resume_gate(gate_id)
            reapplied = self._reapply(gate_id, execution, artifacts, scope)
            execution = self.evidence.resume_gate(gate_id)
            recovered = self._r03_recovered(execution, artifacts, scope)
            after = self._snapshot(scope, R03_TARGET)
            self._validate_before(after, scope, R03_TARGET)
            self._require_stable(before, after)
            final = {
                "scope": asdict(scope), "target": R03_TARGET.key,
                "prepared": prepared, "scaled": scaled, "during": during,
                "reapplied": reapplied, "recovered": recovered, "after": after,
            }
            artifacts.append(self.evidence.write_json(
                gate_id, "dependency-degradation.json", final,
            ))
            self.evidence.record_gate(gate_id, GateResult("passed", tuple(artifacts)), started_at=execution.started_at)
            return {"gate_id": gate_id, "status": "passed", "operations": "5"}
        except Exception as exc:
            fail_gate(self.evidence, gate_id, artifacts, exc, (), execution.started_at)

    def resume_r04(self) -> dict[str, str]:
        gate_id = "R04"
        execution = self.evidence.resume_gate(gate_id)
        artifacts = list(execution.artifacts)
        try:
            intent = self.journal.artifact_json(artifacts, "intent.json")
            before = self.journal.artifact_json(artifacts, "before.json")
            review = self.journal.artifact(artifacts, "recovery-review.json")
            scope = parse_recovery_scope(intent.get("scope"))
            if (
                scope != load_recovery_scope(self.evidence)
                or
                intent.get("target") != asdict(R04_TARGET)
                or intent.get("effects") != self._r04_effects()
                or intent.get("before_sha256")
                != self.journal.artifact(artifacts, "before.json").sha256
                or intent.get("r03_after_sha256")
                != self.evidence.passed_artifact("R03", "dependency-degradation.json").sha256
            ):
                raise ValueError("R04 durable intent drifted")
            self.journal.require_operator_attestation(gate_id, review.sha256)
            scaled = self._scale(gate_id, execution, artifacts, R04_TARGET, before)
            execution = self.evidence.resume_gate(gate_id)
            during = self._r04_during(execution, artifacts, scope)
            execution = self.evidence.resume_gate(gate_id)
            reapplied = self._reapply(gate_id, execution, artifacts, scope)
            execution = self.evidence.resume_gate(gate_id)
            recovered = self._r04_recovered(execution, artifacts, scope)
            after = self._snapshot(scope, R04_TARGET)
            self._validate_before(after, scope, R04_TARGET)
            self._require_stable(before, after)
            final = {
                "scope": asdict(scope), "target": R04_TARGET.key,
                "scaled": scaled, "during": during, "reapplied": reapplied,
                "recovered": recovered, "after": after,
            }
            artifacts.append(self.evidence.write_json(
                gate_id, "dependency-degradation.json", final,
            ))
            self.evidence.record_gate(gate_id, GateResult("passed", tuple(artifacts)), started_at=execution.started_at)
            return {"gate_id": gate_id, "status": "passed", "operations": "4"}
        except Exception as exc:
            fail_gate(self.evidence, gate_id, artifacts, exc, (), execution.started_at)

    def _r03_prepare(
        self, execution, artifacts: list[Artifact], scope: RecoveryScope,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("R03", execution.execution_id, "prepare")
        result = self.journal.effect(
            "R03", execution, artifacts, operation_id=operation_id,
            kind="prepare_connector_probe", artifact_name="connector-probe.json",
            dispatch=lambda: self.probe.prepare_connector_probe(
                scope, operation_id=operation_id,
            ),
            reconcile=lambda: self.probe.reconcile_connector_probe(
                scope, operation_id=operation_id,
            ),
        )
        if (
            result.get("status") != "succeeded"
            or result.get("operation_id") != operation_id
            or result.get("approval_status") != "awaiting_approval"
            or any(not result.get(key) for key in (
                "change_request_id", "phase_id", "revision_id", "target_confirmation",
            ))
            or re.fullmatch(r"[0-9a-f]{64}", str(result.get("dry_run_hash"))) is None
            or not self._valid_prepared_change(result.get("change_summary"))
        ):
            raise ValueError("R03 Connector probe preparation is invalid")
        return result

    def _r03_approval_review(
        self, artifacts: list[Artifact], prepared: dict[str, object],
    ) -> Artifact:
        value = {
            "gate_id": "R03",
            "change_request_id": prepared["change_request_id"],
            "phase_id": prepared["phase_id"],
            "revision_id": prepared["revision_id"],
            "dry_run_hash": prepared["dry_run_hash"],
            "target_confirmation": prepared["target_confirmation"],
            "change_summary": prepared["change_summary"],
            "decision": "approve only to probe Grant fail-closed while Connector is unavailable",
        }
        retained = self.journal.optional_artifact_json(artifacts, "approval-review.json")
        if retained is not None:
            if retained != value:
                raise ValueError("R03 retained approval review drifted")
            return self.journal.artifact(artifacts, "approval-review.json")
        artifact = self.evidence.write_json("R03", "approval-review.json", value)
        artifacts.append(artifact)
        return artifact

    def _scale(
        self, gate_id: str, execution, artifacts: list[Artifact], target: RecoveryTarget,
        before: dict[str, object],
    ) -> dict[str, object]:
        before_target = before.get("target")
        original_replicas = before_target.get("replicas") if isinstance(before_target, dict) else None
        if not isinstance(original_replicas, int) or original_replicas < 1:
            raise ValueError(f"{gate_id} original dependency replicas are invalid")
        operation_id = self.journal.operation_id(gate_id, execution.execution_id, "scale-zero")
        result = self.journal.effect(
            gate_id, execution, artifacts, operation_id=operation_id,
            kind="scale_dependency_to_zero", artifact_name="scale-to-zero.json",
            dispatch=lambda: self.effects.scale_to_zero(
                target, original_replicas=original_replicas, operation_id=operation_id,
            ),
            reconcile=lambda: self.effects.reconcile_scaled_to_zero(
                target, original_replicas=original_replicas, operation_id=operation_id,
            ),
        )
        if (
            result.get("status") != "succeeded"
            or result.get("operation_id") != operation_id
            or result.get("target") != target.key
            or result.get("replicas") != 0
            or result.get("ready") is not False
            or not result.get("uid")
            or not isinstance(result.get("original_replicas"), int)
            or result["original_replicas"] < 1
        ):
            raise ValueError(f"{gate_id} exact dependency scale-to-zero is invalid")
        return result

    def _r03_during(
        self, execution, artifacts: list[Artifact], scope: RecoveryScope,
        prepared: dict[str, object],
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("R03", execution.execution_id, "deny")
        result = self.journal.effect(
            "R03", execution, artifacts, operation_id=operation_id,
            kind="verify_connector_fail_closed", artifact_name="connector-unavailable.json",
            dispatch=lambda: self.probe.verify_connector_unavailable(
                scope, prepared, operation_id=operation_id,
            ),
            reconcile=lambda: self.probe.reconcile_connector_unavailable(
                scope, prepared, operation_id=operation_id,
            ),
        )
        denials = result.get("denials")
        approval = result.get("approval")
        if (
            result.get("status") != "succeeded"
            or result.get("operation_id") != operation_id
            or result.get("dependency") != "connector"
            or result.get("platform") != {
                "availability": "unavailable", "reason_code": "owner_unavailable",
            }
            or not isinstance(denials, dict)
            or set(denials) != {"live_evidence", "dry_run", "grant", "dispatch"}
            or any(
                not isinstance(item, dict)
                or item.get("status") != 409
                or item.get("error_code") != "cluster_not_ready"
                or not item.get("request_id")
                for item in denials.values()
            )
            or len({item["request_id"] for item in denials.values()}) != 4
            or not isinstance(approval, dict)
            or approval.get("status") != 201
            or not approval.get("request_id")
            or approval.get("response_request_id") != approval.get("request_id")
            or approval.get("error_code") is not None
            or approval["request_id"] in {
                item["request_id"] for item in denials.values()
            }
            or result.get("active_commands") != 0
            or result.get("grants_created") != 0
            or result.get("prepared_change_request_id") != prepared.get("change_request_id")
            or result.get("other_dependency") != {
                "owner": "loki", "availability": "available",
                "observability_readiness": "ready", "loki_state": "available",
            }
        ):
            raise ValueError("R03 Connector unavailable proof is invalid")
        return result

    def _reapply(
        self, gate_id: str, execution, artifacts: list[Artifact], scope: RecoveryScope,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id(gate_id, execution.execution_id, "reapply")
        result = self.journal.effect(
            gate_id, execution, artifacts, operation_id=operation_id,
            kind="reapply_same_candidate", artifact_name="candidate-reapply.json",
            dispatch=lambda: self.effects.reapply_candidate(operation_id=operation_id),
            reconcile=lambda: self.effects.reconcile_reapply(operation_id=operation_id),
        )
        if (
            result.get("status") != "succeeded"
            or result.get("operation_id") != operation_id
            or result.get("candidate_sha256") != scope.candidate_sha256
            or result.get("release_inventory_sha256") != scope.release_inventory_sha256
            or result.get("owners_ready") is not True
        ):
            raise ValueError(f"{gate_id} did not reapply the exact candidate")
        return result

    def _r03_recovered(
        self, execution, artifacts: list[Artifact], scope: RecoveryScope,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("R03", execution.execution_id, "verify-ready")
        result = self.journal.effect(
            "R03", execution, artifacts, operation_id=operation_id,
            kind="verify_connector_recovered", artifact_name="connector-recovered.json",
            dispatch=lambda: self.probe.verify_connector_recovered(
                scope, operation_id=operation_id,
            ),
            reconcile=lambda: self.probe.reconcile_connector_recovered(
                scope, operation_id=operation_id,
            ),
        )
        if (
            result.get("status") != "succeeded"
            or result.get("operation_id") != operation_id
            or result.get("dependency") != "connector"
            or result.get("availability") != "available"
            or result.get("heartbeat") != "online"
            or result.get("read_verification") != "verified"
        ):
            raise ValueError("R03 Connector recovery proof is invalid")
        return result

    def _r04_during(
        self, execution, artifacts: list[Artifact], scope: RecoveryScope,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("R04", execution.execution_id, "unavailable")
        result = self.journal.effect(
            "R04", execution, artifacts, operation_id=operation_id,
            kind="verify_loki_unavailable", artifact_name="loki-unavailable.json",
            dispatch=lambda: self.probe.verify_loki_unavailable(
                scope, operation_id=operation_id,
            ),
            reconcile=lambda: self.probe.reconcile_loki_unavailable(
                scope, operation_id=operation_id,
            ),
        )
        mcp = result.get("mcp")
        if (
            result.get("status") != "succeeded"
            or result.get("operation_id") != operation_id
            or result.get("dependency") != "loki"
            or result.get("platform") != {
                "availability": "unavailable", "reason_code": "owner_unavailable",
            }
            or not isinstance(mcp, dict)
            or mcp.get("status") != "failed"
            or mcp.get("error_code") != "backend_unavailable"
            or mcp.get("evidence_refs") != []
            or result.get("other_dependency") != {
                "owner": "connector", "availability": "available",
                "connector_id": scope.connector_id, "cluster_id": scope.cluster_id,
                "heartbeat": "online", "read_verification": "verified",
            }
        ):
            raise ValueError("R04 Loki unavailable proof is invalid")
        return result

    def _r04_recovered(
        self, execution, artifacts: list[Artifact], scope: RecoveryScope,
    ) -> dict[str, object]:
        operation_id = self.journal.operation_id("R04", execution.execution_id, "verify-logs")
        result = self.journal.effect(
            "R04", execution, artifacts, operation_id=operation_id,
            kind="verify_loki_recovered", artifact_name="loki-recovered.json",
            dispatch=lambda: self.probe.verify_loki_recovered(
                scope, operation_id=operation_id,
            ),
            reconcile=lambda: self.probe.reconcile_loki_recovered(
                scope, operation_id=operation_id,
            ),
        )
        if (
            result.get("status") != "succeeded"
            or result.get("operation_id") != operation_id
            or result.get("dependency") != "loki"
            or result.get("availability") != "available"
            or result.get("retained_log_refs_sha256") != scope.recovery_log_refs_sha256
            or not result.get("fresh_probe_request_id")
            or not self._valid_loki_query(result.get("retained_query"))
            or not self._valid_loki_query(result.get("fresh_query"))
        ):
            raise ValueError("R04 Loki recovery proof is invalid")
        return result

    def _snapshot(
        self, scope: RecoveryScope, target: RecoveryTarget,
    ) -> dict[str, object]:
        cluster = self.effects.snapshot_dependency(scope, target)
        if "public" in cluster:
            raise ValueError("Dependency Kubernetes snapshot cannot own public product facts")
        return cluster | {"public": self.probe.snapshot_ready(scope, target)}

    @staticmethod
    def _r03_effects() -> list[dict[str, str]]:
        return [
            {"action": "prepare_connector_probe", "owner": "connector"},
            {"action": "scale_to_zero", "namespace": NAMESPACE, **asdict(R03_TARGET)},
            {"action": "verify_fail_closed", "owner": "connector"},
            {"action": "reapply_same_candidate", "owner": "candidate"},
            {"action": "verify_recovered", "owner": "connector"},
        ]

    @staticmethod
    def _r04_effects() -> list[dict[str, str]]:
        return [
            {"action": "scale_to_zero", "namespace": NAMESPACE, **asdict(R04_TARGET)},
            {"action": "verify_bounded_unavailable", "owner": "loki"},
            {"action": "reapply_same_candidate", "owner": "candidate"},
            {"action": "verify_retained_and_fresh_logs", "owner": "loki"},
        ]

    @staticmethod
    def _valid_loki_query(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        evidence_ref = value.get("evidence_ref")
        return (
            value.get("status") == "succeeded"
            and isinstance(value.get("matched"), int)
            and value["matched"] >= 1
            and isinstance(evidence_ref, dict)
            and evidence_ref.get("source") == "loki"
            and bool(evidence_ref.get("ref_id"))
        )

    @staticmethod
    def _valid_prepared_change(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        target = value.get("target")
        diff = value.get("diff")
        post_checks = value.get("post_checks")
        rollback = value.get("rollback")
        if (
            target != {
                "api_version": "apps/v1", "kind": "Deployment",
                "namespace": "aiops-verification", "name": "verification-api",
            }
            or value.get("operation") != "patch"
            or not isinstance(diff, list)
            or len(diff) != 1
            or not isinstance(diff[0], dict)
            or diff[0].get("op") not in {"add", "replace"}
            or diff[0].get("path")
            != "/metadata/annotations/aiops.dev~1r03-grant-probe"
            or not isinstance(diff[0].get("after"), str)
            or not diff[0]["after"]
            or not isinstance(post_checks, list)
            or not post_checks
            or not isinstance(rollback, dict)
            or rollback.get("status") not in {"available", "unavailable"}
        ):
            return False
        return rollback.get("status") == "available" or bool(rollback.get("concrete_loss"))

    @staticmethod
    def _validate_before(
        value: dict[str, object], scope: RecoveryScope, target: RecoveryTarget,
    ) -> None:
        if value.get("identity") != {
            "candidate_sha256": scope.candidate_sha256,
            "release_inventory_sha256": scope.release_inventory_sha256,
            "kube_context": scope.kube_context,
            "cluster_identity_sha256": scope.cluster_identity_sha256,
        }:
            raise ValueError("Dependency snapshot identity is invalid")
        item = value.get("target")
        public = value.get("public")
        image_ids = item.get("image_ids") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("owner") != target.owner
            or item.get("kind") != target.kind
            or item.get("name") != target.name
            or not item.get("uid")
            or not item.get("resource_version")
            or not isinstance(item.get("replicas"), int)
            or item["replicas"] < 1
            or item.get("ready") is not True
            or not isinstance(image_ids, list)
            or not image_ids
            or any(re.fullmatch(r"sha256:[0-9a-f]{64}", str(image)) is None for image in image_ids)
            or not isinstance(public, dict)
            or public.get("availability") != "available"
            or public.get("reason_code") != "ready"
        ):
            raise ValueError("Dependency ready snapshot is invalid")
        if target == R03_TARGET and (
            public.get("heartbeat") != "online"
            or public.get("read_verification") != "verified"
        ):
            raise ValueError("Connector ready snapshot is invalid")
        material = value.get("connector_material")
        if target == R03_TARGET and (
            not isinstance(material, dict)
            or set(material) != {"uid", "resource_version", "key_inventory_sha256", "value_sha256"}
            or not material.get("uid")
            or not material.get("resource_version")
            or any(
                re.fullmatch(r"[0-9a-f]{64}", str(material.get(key))) is None
                for key in ("key_inventory_sha256", "value_sha256")
            )
        ):
            raise ValueError("Connector enrollment material identity is invalid")
        if target == R04_TARGET and (
            public.get("observability_readiness") != "ready"
            or public.get("loki_state") != "available"
        ):
            raise ValueError("Loki ready snapshot is invalid")

    @staticmethod
    def _require_stable(before: dict[str, object], after: dict[str, object]) -> None:
        old = before["target"]
        new = after["target"]
        assert isinstance(old, dict) and isinstance(new, dict)
        for key in ("owner", "kind", "name", "uid", "replicas", "image_ids"):
            if old.get(key) != new.get(key):
                raise ValueError(f"Dependency restored {key} drifted")
        if before.get("connector_material") != after.get("connector_material"):
            raise ValueError("Dependency restored Connector enrollment material drifted")
        if before.get("public") != after.get("public"):
            raise ValueError("Dependency restored public state drifted")
