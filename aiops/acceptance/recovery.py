"""Stateful Recovery gates with a frozen, structured operation matrix."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from .evidence import AcceptanceEvidence, Artifact, GateExecution
from .integration_support import fail_gate
from .recovery_journal import RecoveryJournal
from .run_one_decisions import valid_run_id


NAMESPACE = "aiops-system"
PVC_NAMES = (
    "aiops-connector-data",
    "aiops-diagnosis-data",
    "aiops-gateway-data",
    "aiops-loki-data",
    "aiops-notification-data",
    "aiops-prometheus-data",
)
SECRET_NAMES = (
    "aiops-change-encryption",
    "aiops-connector-secret",
    "aiops-model-encryption",
    "aiops-notification-encryption",
    "aiops-runtime-secret",
)


@dataclass(frozen=True)
class RecoveryTarget:
    owner: str
    kind: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.name}"


R01_TARGETS = tuple(
    RecoveryTarget(owner, "deployment", name)
    for owner, name in (
        ("gateway", "aiops-gateway"),
        ("diagnosis", "aiops-diagnosis"),
        ("connector", "aiops-connector"),
        ("notification", "aiops-notification"),
        ("prometheus", "aiops-prometheus"),
        ("loki", "aiops-loki"),
    )
)
R02_TARGETS = tuple(
    RecoveryTarget(name.removeprefix("aiops-"), "deployment", name)
    for name in (
        "aiops-gateway",
        "aiops-connector",
        "aiops-diagnosis",
        "aiops-mcp-prometheus",
        "aiops-mcp-loki",
        "aiops-notification",
        "aiops-mcp-topology",
        "aiops-alertmanager",
        "aiops-kube-state-metrics",
        "aiops-loki",
        "aiops-prometheus",
    )
) + (RecoveryTarget("alloy", "daemonset", "aiops-alloy"),)


@dataclass(frozen=True)
class RecoveryScope:
    candidate_sha256: str
    release_inventory_sha256: str
    kube_context: str
    cluster_identity_sha256: str
    connector_id: str
    cluster_id: str
    run_id: str
    alert_fingerprint: str
    recovery_metric_observed_at: str
    recovery_metric_ref_sha256: str
    recovery_log_observed_at: str
    recovery_log_refs_sha256: str
    incident_id: str
    investigation_id: str
    report_publication_id: str
    report_sha256: str
    notification_delivery_id: str
    change_request_id: str
    phase_id: str
    execution_id: str
    command_id: str

    def durable_identities(self) -> dict[str, dict[str, str]]:
        return {
            "incident": {"incident_id": self.incident_id, "investigation_id": self.investigation_id},
            "governance": {
                "change_request_id": self.change_request_id, "phase_id": self.phase_id,
                "execution_id": self.execution_id, "command_id": self.command_id,
            },
            "connector_journal": {"command_id": self.command_id},
            "delivery": {"delivery_id": self.notification_delivery_id},
            "report": {
                "publication_id": self.report_publication_id, "sha256": self.report_sha256,
            },
        }


class RecoveryAdapter(Protocol):
    """Executes only the frozen R01/R02 operations declared by this module."""

    def snapshot(self, scope: RecoveryScope) -> dict[str, object]: ...

    def delete_current_pod(
        self, target: RecoveryTarget, *, scope: RecoveryScope,
        pod_uid: str, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_deleted_pod(
        self, target: RecoveryTarget, *, scope: RecoveryScope,
        pod_uid: str, operation_id: str,
    ) -> dict[str, object] | None: ...

    def rollout(
        self, target: RecoveryTarget, *, operation_id: str,
    ) -> dict[str, object]: ...

    def reconcile_rollout(
        self, target: RecoveryTarget, *, operation_id: str,
    ) -> dict[str, object] | None: ...

    def reapply_candidate(self, *, operation_id: str) -> dict[str, object]: ...

    def reconcile_reapply(self, *, operation_id: str) -> dict[str, object] | None: ...


class RecoveryGateRunner:
    """Owns R01/R02 intent, HITL, ordering, reconciliation, and invariants."""

    def __init__(self, *, evidence: AcceptanceEvidence, adapter: RecoveryAdapter) -> None:
        self.evidence = evidence
        self.adapter = adapter
        self.journal = RecoveryJournal(evidence)

    def run_r01(self) -> dict[str, str]:
        return self._start("R01", R01_TARGETS)

    def resume_r01(self) -> dict[str, str]:
        return self._resume("R01", R01_TARGETS, self._complete_r01)

    def run_r02(self) -> dict[str, str]:
        return self._start("R02", R02_TARGETS)

    def resume_r02(self) -> dict[str, str]:
        return self._resume("R02", R02_TARGETS, self._complete_r02)

    def _start(self, gate_id: str, targets: tuple[RecoveryTarget, ...]) -> dict[str, str]:
        started_at = self.evidence.start_gate(gate_id)
        artifacts: list[Artifact] = []
        try:
            scope = load_recovery_scope(self.evidence)
            before = self.adapter.snapshot(scope)
            self._validate_snapshot(before, scope, targets)
            if gate_id == "R02":
                r01 = self.evidence.passed_artifact_json("R01", "stateful-recovery.json")
                r01_after = r01["value"].get("after")
                if not isinstance(r01_after, dict):
                    raise ValueError("R02 requires the retained R01 final state")
                self._require_stable_state(r01_after, before, targets=R01_TARGETS)
            before_artifact = self.evidence.write_json(gate_id, "before.json", before)
            artifacts.append(before_artifact)
            intent = {
                "scope": asdict(scope),
                "targets": [asdict(item) for item in targets],
                "effects": self._effects(gate_id, targets),
                "before_sha256": before_artifact.sha256,
            }
            artifacts.append(self.evidence.write_json(gate_id, "intent.json", intent))
            review = {
                "gate_id": gate_id,
                "namespace": NAMESPACE,
                "candidate_sha256": scope.candidate_sha256,
                "effects": intent["effects"],
                "before_sha256": before_artifact.sha256,
                "claims_excluded": ["high_availability", "cross_version_upgrade"],
            }
            review_artifact = self.evidence.write_json(gate_id, "recovery-review.json", review)
            artifacts.append(review_artifact)
            return {
                "status": "awaiting_attestation",
                "gate_id": gate_id,
                "review_sha256": review_artifact.sha256,
            }
        except Exception as exc:
            fail_gate(self.evidence, gate_id, artifacts, exc, (), started_at)

    def _resume(
        self,
        gate_id: str,
        targets: tuple[RecoveryTarget, ...],
        complete: Callable[[GateExecution, list[Artifact], RecoveryScope, dict[str, object]], dict[str, str]],
    ) -> dict[str, str]:
        execution = self.evidence.resume_gate(gate_id)
        artifacts = list(execution.artifacts)
        try:
            intent = self.journal.artifact_json(artifacts, "intent.json")
            before = self.journal.artifact_json(artifacts, "before.json")
            review = self.journal.artifact(artifacts, "recovery-review.json")
            scope = parse_recovery_scope(intent.get("scope"))
            if intent.get("targets") != [asdict(item) for item in targets]:
                raise ValueError(f"{gate_id} durable target matrix drifted")
            if intent.get("effects") != self._effects(gate_id, targets):
                raise ValueError(f"{gate_id} durable effect matrix drifted")
            if intent.get("before_sha256") != self.journal.artifact(artifacts, "before.json").sha256:
                raise ValueError(f"{gate_id} durable before-state identity drifted")
            self.journal.require_operator_attestation(gate_id, review.sha256)
            return complete(execution, artifacts, scope, before)
        except Exception as exc:
            fail_gate(self.evidence, gate_id, artifacts, exc, (), execution.started_at)

    def _complete_r01(
        self,
        execution: GateExecution,
        artifacts: list[Artifact],
        scope: RecoveryScope,
        before: dict[str, object],
    ) -> dict[str, str]:
        workloads = before["workloads"]
        assert isinstance(workloads, dict)
        results: list[dict[str, object]] = []
        for target in R01_TARGETS:
            workload = workloads[target.key]
            if not isinstance(workload, dict):
                raise ValueError("R01 before-state workload is invalid")
            pod_uid = str(workload.get("current_pod_uid") or "")
            operation_id = self.journal.operation_id("R01", execution.execution_id, target.owner)
            result = self.journal.effect(
                "R01",
                execution,
                artifacts,
                operation_id=operation_id,
                kind="pod_delete",
                artifact_name=f"{target.owner}-recovery.json",
                dispatch=lambda target=target, pod_uid=pod_uid: self.adapter.delete_current_pod(
                    target, scope=scope, pod_uid=pod_uid, operation_id=operation_id,
                ),
                reconcile=lambda target=target, pod_uid=pod_uid: self.adapter.reconcile_deleted_pod(
                    target, scope=scope, pod_uid=pod_uid, operation_id=operation_id,
                ),
            )
            self._validate_r01_result(
                result, target, pod_uid, operation_id, before=before, scope=scope,
            )
            results.append(result)
            execution = self.evidence.resume_gate("R01")
        after = self.adapter.snapshot(scope)
        self._validate_snapshot(after, scope, R02_TARGETS)
        self._require_stable_state(before, after, targets=R01_TARGETS)
        final = {"scope": asdict(scope), "operations": results, "after": after}
        artifacts.append(self.evidence.write_json("R01", "stateful-recovery.json", final))
        self.evidence.record_gate("R01", "passed", artifacts, started_at=execution.started_at)
        return {"gate_id": "R01", "status": "passed", "operations": str(len(results))}

    def _complete_r02(
        self,
        execution: GateExecution,
        artifacts: list[Artifact],
        scope: RecoveryScope,
        before: dict[str, object],
    ) -> dict[str, str]:
        results: list[dict[str, object]] = []
        for target in R02_TARGETS:
            operation_id = self.journal.operation_id("R02", execution.execution_id, target.owner)
            result = self.journal.effect(
                "R02",
                execution,
                artifacts,
                operation_id=operation_id,
                kind="fixed_rollout",
                artifact_name=f"{target.owner}-rollout.json",
                dispatch=lambda target=target: self.adapter.rollout(
                    target, operation_id=operation_id,
                ),
                reconcile=lambda target=target: self.adapter.reconcile_rollout(
                    target, operation_id=operation_id,
                ),
            )
            self._validate_r02_result(result, target, operation_id)
            results.append(result)
            execution = self.evidence.resume_gate("R02")
        operation_id = self.journal.operation_id(
            "R02", execution.execution_id, "candidate-reapply",
        )
        reapply = self.journal.effect(
            "R02",
            execution,
            artifacts,
            operation_id=operation_id,
            kind="candidate_reapply",
            artifact_name="candidate-reapply.json",
            dispatch=lambda: self.adapter.reapply_candidate(operation_id=operation_id),
            reconcile=lambda: self.adapter.reconcile_reapply(operation_id=operation_id),
        )
        if (
            reapply.get("status") != "succeeded"
            or reapply.get("candidate_sha256") != scope.candidate_sha256
            or reapply.get("release_inventory_sha256") != scope.release_inventory_sha256
            or reapply.get("operation_id") != operation_id
        ):
            raise ValueError("R02 did not prove exact candidate reapply")
        results.append(reapply)
        after = self.adapter.snapshot(scope)
        self._validate_snapshot(after, scope, R02_TARGETS)
        self._require_stable_state(before, after, targets=R02_TARGETS)
        final = {"scope": asdict(scope), "operations": results, "after": after}
        artifacts.append(self.evidence.write_json("R02", "stateful-recovery.json", final))
        self.evidence.record_gate("R02", "passed", artifacts, started_at=execution.started_at)
        return {"gate_id": "R02", "status": "passed", "operations": str(len(results))}

    @staticmethod
    def _effects(gate_id: str, targets: tuple[RecoveryTarget, ...]) -> list[dict[str, str]]:
        action = "delete_current_pod" if gate_id == "R01" else "fixed_annotation_rollout"
        effects = [
            {"action": action, "namespace": NAMESPACE, **asdict(target)}
            for target in targets
        ]
        if gate_id == "R02":
            effects.append({
                "action": "reapply_same_candidate", "namespace": NAMESPACE,
                "owner": "candidate", "kind": "kustomization", "name": "release",
            })
        return effects

    @staticmethod
    def _validate_snapshot(
        value: dict[str, object],
        scope: RecoveryScope,
        targets: tuple[RecoveryTarget, ...],
    ) -> None:
        identity = value.get("identity")
        workloads = value.get("workloads")
        if identity != {
            "candidate_sha256": scope.candidate_sha256,
            "release_inventory_sha256": scope.release_inventory_sha256,
            "kube_context": scope.kube_context,
            "cluster_identity_sha256": scope.cluster_identity_sha256,
        } or not isinstance(workloads, dict):
            raise ValueError("Recovery snapshot identity is invalid")
        for target in targets:
            item = workloads.get(target.key)
            images = item.get("images") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or item.get("owner") != target.owner
                or item.get("ready") is not True
                or not item.get("uid")
                or not isinstance(images, list)
                or not images
                or any(
                    not isinstance(image, dict)
                    or not isinstance(image.get("image_id"), str)
                    or re.search(r"sha256:[0-9a-f]{64}$", image["image_id"]) is None
                    for image in images
                )
            ):
                raise ValueError(f"Recovery snapshot owner {target.owner} is not Ready")
        for key in (
            "cluster_configurations", "configurations", "durable", "durable_identities",
            "telemetry", "public_owners",
        ):
            if not isinstance(value.get(key), dict) or not value[key]:
                raise ValueError(f"Recovery snapshot {key} facts are missing")
        public_owners = value["public_owners"]
        assert isinstance(public_owners, dict)
        if any(readiness != "ready" for readiness in public_owners.values()):
            raise ValueError("Recovery public capability owner is not Ready")
        pvcs = value.get("pvcs")
        protected = value.get("protected_resources")
        if not isinstance(pvcs, dict) or set(pvcs) != set(PVC_NAMES):
            raise ValueError("Recovery snapshot PVC inventory drifted")
        if any(
            not isinstance(item, dict)
            or not item.get("uid")
            or item.get("phase") != "Bound"
            for item in pvcs.values()
        ):
            raise ValueError("Recovery snapshot PVC identity or binding is invalid")
        if not isinstance(protected, dict) or set(protected) != set(SECRET_NAMES):
            raise ValueError("Recovery snapshot Secret inventory drifted")
        if any(
            not isinstance(item, dict)
            or not item.get("uid")
            or not item.get("resource_version")
            or not isinstance(item.get("key_inventory_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["key_inventory_sha256"])
            or not isinstance(item.get("value_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["value_sha256"])
            for item in protected.values()
        ):
            raise ValueError("Recovery snapshot Secret identity or credential proof is invalid")
        durable = value["durable"]
        durable_identities = value["durable_identities"]
        telemetry = value["telemetry"]
        assert all(isinstance(item, dict) for item in (durable, durable_identities, telemetry))
        if (
            set(durable) != set(scope.durable_identities())
            or durable_identities != scope.durable_identities()
            or any(
            not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
            for item in durable.values()
            )
        ):
            raise ValueError("Recovery durable public fact hash is invalid")
        metric_hashes = telemetry.get("metric_ref_hashes")
        log_hashes = telemetry.get("log_ref_hashes")
        metric_at = telemetry.get("metric_observed_at")
        log_at = telemetry.get("log_observed_at")
        if (
            not isinstance(metric_at, (int, float))
            or isinstance(metric_at, bool)
            or not math.isfinite(float(metric_at))
            or not isinstance(log_at, (int, float))
            or isinstance(log_at, bool)
            or not math.isfinite(float(log_at))
            or not isinstance(log_hashes, list)
            or not log_hashes
            or not isinstance(metric_hashes, list)
            or not metric_hashes
            or any(
                not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                for item in [*metric_hashes, *log_hashes]
            )
        ):
            raise ValueError("Recovery retained metric/log facts are invalid")

    @classmethod
    def _require_stable_state(
        cls,
        before: dict[str, object],
        after: dict[str, object],
        *,
        targets: tuple[RecoveryTarget, ...],
    ) -> None:
        for key in (
            "identity", "pvcs", "protected_resources", "cluster_configurations",
            "configurations", "durable", "durable_identities", "telemetry", "public_owners",
        ):
            if before.get(key) != after.get(key):
                raise ValueError(f"Recovery durable {key} state drifted")
        before_workloads = before.get("workloads")
        after_workloads = after.get("workloads")
        assert isinstance(before_workloads, dict) and isinstance(after_workloads, dict)
        for target in targets:
            old = before_workloads.get(target.key)
            new = after_workloads.get(target.key)
            if not isinstance(old, dict) or not isinstance(new, dict):
                raise ValueError("Recovery workload state is missing")
            if old.get("uid") != new.get("uid") or old.get("images") != new.get("images"):
                raise ValueError(f"Recovery owner {target.owner} identity or image digest drifted")

    @classmethod
    def _validate_r01_result(
        cls,
        value: dict[str, object],
        target: RecoveryTarget,
        pod_uid: str,
        operation_id: str,
        *,
        before: dict[str, object],
        scope: RecoveryScope,
    ) -> None:
        during = value.get("during")
        after = value.get("after")
        if (
            value.get("status") != "succeeded"
            or value.get("operation_id") != operation_id
            or value.get("target") != target.key
            or value.get("deleted_pod_uid") != pod_uid
            or not value.get("replacement_pod_uid")
            or value.get("replacement_pod_uid") == pod_uid
            or value.get("owner_ready") is not True
            or not isinstance(during, dict)
            or set(during) != {
                "old_pod_absent", "pod_uids", "owner_ready", "pvcs",
                "protected_resources", "cluster_configurations", "public_observation",
            }
            or not isinstance(during.get("old_pod_absent"), bool)
            or not isinstance(during.get("owner_ready"), bool)
            or not isinstance(during.get("pod_uids"), list)
            or any(not isinstance(item, str) or not item for item in during["pod_uids"])
            or (
                during["old_pod_absent"] is True
                and pod_uid in during["pod_uids"]
            )
        ):
            raise ValueError(f"R01 {target.owner} recovery result is invalid")
        for key in ("pvcs", "protected_resources", "cluster_configurations"):
            if during.get(key) != before.get(key):
                raise ValueError(f"R01 {target.owner} during {key} identity drifted")
        observation = during.get("public_observation")
        availability = observation.get("availability") if isinstance(observation, dict) else None
        if availability == "available":
            facts = observation.get("facts")
            if not isinstance(facts, dict) or any(
                facts.get(key) != before.get(key)
                for key in ("configurations", "durable", "telemetry")
            ):
                raise ValueError(f"R01 {target.owner} during public facts drifted")
        elif (
            availability not in {"unavailable", "not_observed"}
            or not observation.get("reason_code")
        ):
            raise ValueError(f"R01 {target.owner} during public observation is invalid")
        if not isinstance(after, dict):
            raise ValueError(f"R01 {target.owner} restored state is missing")
        cls._validate_snapshot(after, scope, R02_TARGETS)
        cls._require_stable_state(before, after, targets=R02_TARGETS)

    @staticmethod
    def _validate_r02_result(
        value: dict[str, object], target: RecoveryTarget, operation_id: str,
    ) -> None:
        if (
            value.get("status") != "succeeded"
            or value.get("operation_id") != operation_id
            or value.get("target") != target.key
            or value.get("annotation_operation_id") != operation_id
            or value.get("owner_ready") is not True
        ):
            raise ValueError(f"R02 {target.owner} rollout result is invalid")

def load_recovery_scope(evidence: AcceptanceEvidence) -> RecoveryScope:
    inventory = evidence.passed_artifact("P01", "artifact-inventory.json")
    s05 = evidence.passed_artifact_json("S05", "connector-read-verification.json")["value"]
    v05 = evidence.passed_artifact_json("V05", "approval-and-execution.json")["value"]
    v06 = evidence.passed_artifact_json("V06", "recovery.json")["value"]
    v07 = evidence.passed_artifact_json("V07", "report-and-delivery.json")["value"]
    execution = v05.get("execution") if isinstance(v05, dict) else None
    report = v07.get("report") if isinstance(v07, dict) else None
    delivery = v07.get("notification_delivery") if isinstance(v07, dict) else None
    telemetry = v06.get("telemetry") if isinstance(v06, dict) else None
    run_id = v07.get("run_id") if isinstance(v07, dict) else None
    alert_fingerprint = v06.get("alert_fingerprint") if isinstance(v06, dict) else None
    if (
        not isinstance(telemetry, dict)
        or v06.get("run_id") != run_id
        or telemetry.get("run_id") != run_id
        or telemetry.get("alert_fingerprint") != alert_fingerprint
    ):
        raise ValueError("Recovery V06/V07 correlation is invalid")
    return parse_recovery_scope({
        "candidate_sha256": evidence.candidate_sha256,
        "release_inventory_sha256": inventory.sha256,
        "kube_context": evidence.kube_context,
        "cluster_identity_sha256": evidence.cluster_identity_sha256,
        "connector_id": s05.get("connector_id") if isinstance(s05, dict) else None,
        "cluster_id": s05.get("cluster_id") if isinstance(s05, dict) else None,
        "run_id": run_id,
        "alert_fingerprint": alert_fingerprint,
        "recovery_metric_observed_at": str(telemetry.get("recovery_metric_observed_at")),
        "recovery_metric_ref_sha256": recovery_metric_ref_sha256(
            str(run_id or ""), float(telemetry.get("recovery_metric_observed_at")),
        ),
        "recovery_log_observed_at": str(telemetry.get("recovery_log_observed_at")),
        "recovery_log_refs_sha256": canonical_sha256(
            sorted(telemetry.get("recovery_log_ref_hashes", [])),
        ),
        "incident_id": v07.get("incident_id") if isinstance(v07, dict) else None,
        "investigation_id": v07.get("investigation_id") if isinstance(v07, dict) else None,
        "report_publication_id": report.get("id") if isinstance(report, dict) else None,
        "report_sha256": v07.get("report_sha256") if isinstance(v07, dict) else None,
        "notification_delivery_id": delivery.get("id") if isinstance(delivery, dict) else None,
        "change_request_id": execution.get("change_request_id") if isinstance(execution, dict) else None,
        "phase_id": execution.get("phase_id") if isinstance(execution, dict) else None,
        "execution_id": execution.get("id") if isinstance(execution, dict) else None,
        "command_id": execution.get("command_id") if isinstance(execution, dict) else None,
    })


def parse_recovery_scope(value: object) -> RecoveryScope:
    if not isinstance(value, dict):
        raise ValueError("Recovery durable scope is invalid")
    fields = RecoveryScope.__dataclass_fields__
    if set(value) != set(fields) or any(
        not isinstance(value[key], str) or not value[key] for key in fields
    ):
        raise ValueError("Recovery durable scope is incomplete")
    if not valid_run_id(value["run_id"]) or any(
        len(value[key]) > 300 for key in fields
    ) or any(
        len(value[key]) != 64 or any(char not in "0123456789abcdef" for char in value[key])
        for key in (
            "candidate_sha256", "release_inventory_sha256",
            "cluster_identity_sha256", "report_sha256",
            "recovery_metric_ref_sha256", "recovery_log_refs_sha256",
        )
    ):
        raise ValueError("Recovery durable scope identity is invalid")
    try:
        anchors = (
            float(value["recovery_metric_observed_at"]),
            float(value["recovery_log_observed_at"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Recovery telemetry anchors are invalid") from exc
    if any(not math.isfinite(item) or item < 0 for item in anchors):
        raise ValueError("Recovery telemetry anchors are invalid")
    return RecoveryScope(**{key: str(value[key]) for key in fields})


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()


def recovery_metric_ref_sha256(run_id: str, observed_at: float) -> str:
    return canonical_sha256({
        "metric": {
            "namespace": "aiops-verification",
            "deployment": "verification-api",
            "service": "verification-api",
            "run_id": run_id,
        },
        "sample": [float(observed_at), "0"],
    })
