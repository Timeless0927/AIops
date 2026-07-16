"""Concrete public-state and Kubernetes Adapters for Stateful Recovery."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Protocol

from .command import CommandExecutor, CommandResult
from .recovery import (
    NAMESPACE,
    R01_TARGETS,
    R02_TARGETS,
    SECRET_NAMES,
    RecoveryScope,
    RecoveryTarget,
    canonical_sha256,
)
from .release_inventory import build_release_inventory, inventory_artifact_sha256
from .verification_trigger import UserSession


_OPERATION_ANNOTATION = "aiops.dev/acceptance-recovery-operation"


class RetentionTelemetry(Protocol):
    def probe_retention(
        self,
        run_id: str,
        *,
        metric_at: float,
        log_at: float,
    ) -> dict[str, object]: ...


class PublicRecoveryProbe(Protocol):
    def capture(self, scope: RecoveryScope) -> dict[str, object]: ...


class ExactPodDeleter(Protocol):
    def delete(
        self, *, namespace: str, name: str, uid: str, resource_version: str,
    ) -> None: ...


class KubernetesExactPodDeleter:
    """Uses Kubernetes DeleteOptions preconditions for the exact Pod object."""

    def __init__(self, *, kube_context: str) -> None:
        self.kube_context = kube_context

    def delete(
        self, *, namespace: str, name: str, uid: str, resource_version: str,
    ) -> None:
        from kubernetes import client, config  # type: ignore[import-untyped]

        with config.new_client_from_config(context=self.kube_context) as api_client:
            client.CoreV1Api(api_client).delete_namespaced_pod(
                name,
                namespace,
                body=client.V1DeleteOptions(
                    propagation_policy="Background",
                    preconditions=client.V1Preconditions(
                        uid=uid, resource_version=resource_version,
                    ),
                ),
                _request_timeout=60,
            )


class GatewayRecoveryProbe:
    """Normalizes existing actor-scoped product and telemetry projections."""

    def __init__(
        self,
        *,
        user: UserSession,
        admin: UserSession,
        telemetry: RetentionTelemetry,
    ) -> None:
        self.user = user
        self.admin = admin
        self.telemetry = telemetry

    def capture(self, scope: RecoveryScope) -> dict[str, object]:
        workbench = self._get(
            self.user, f"/api/v1/incidents/{scope.incident_id}/workbench",
        )
        report_body = self._get(
            self.user, f"/api/v1/incidents/{scope.incident_id}/report",
        )
        approval = self._get(
            self.user,
            f"/api/v1/change-requests/{scope.change_request_id}/phase-approval",
        )
        execution = self._get(
            self.user,
            f"/api/v1/change-requests/{scope.change_request_id}/phase-execution",
        )
        deliveries = self._get(self.admin, "/api/v1/admin/notification-deliveries")
        platform = self._get(self.user, "/api/v1/platform/status")
        publications = report_body.get("publications")
        report = self._unique(publications, "id", scope.report_publication_id, "Report")
        if canonical_sha256(report) != scope.report_sha256:
            raise ValueError("Recovery public Report changed after V07")
        delivery = self._unique(
            deliveries.get("deliveries"), "id", scope.notification_delivery_id, "Delivery",
        )
        phase_execution = execution.get("phase_execution")
        phase_review = approval.get("phase_review")
        incident = workbench.get("incident")
        investigation = workbench.get("investigation")
        request = delivery.get("request")
        subject = request.get("subject") if isinstance(request, dict) else None
        if (
            not isinstance(incident, dict)
            or incident.get("id") != scope.incident_id
            or incident.get("status") != "resolved"
            or not isinstance(investigation, dict)
            or investigation.get("id") != scope.investigation_id
            or not isinstance(phase_execution, dict)
            or phase_execution.get("id") != scope.execution_id
            or phase_execution.get("command_id") != scope.command_id
            or phase_execution.get("status") != "succeeded"
            or not isinstance(phase_review, dict)
            or phase_review.get("phase_id") != scope.phase_id
            or delivery.get("status") != "sent"
            or not delivery.get("request_id")
            or not delivery.get("provider_identity")
            or not isinstance(request, dict)
            or request.get("event_type") != "incident.resolved"
            or not isinstance(subject, dict)
            or subject.get("id") != scope.incident_id
        ):
            raise ValueError("Recovery public durable history drifted")
        telemetry = self.telemetry.probe_retention(
            scope.run_id,
            metric_at=float(scope.recovery_metric_observed_at),
            log_at=float(scope.recovery_log_observed_at),
        )
        metric_hashes = telemetry.get("recovery_metric_ref_hashes")
        log_hashes = telemetry.get("recovery_log_ref_hashes")
        if (
            telemetry.get("run_id") != scope.run_id
            or not isinstance(metric_hashes, list)
            or not metric_hashes
            or scope.recovery_metric_ref_sha256 not in metric_hashes
            or not isinstance(log_hashes, list)
            or not log_hashes
            or canonical_sha256(sorted(log_hashes)) != scope.recovery_log_refs_sha256
        ):
            raise ValueError("Recovery retained telemetry is unprovable")
        capabilities = platform.get("capabilities")
        if not isinstance(capabilities, dict) or not capabilities:
            raise ValueError("Recovery Platform Status omitted capability owners")
        configurations: dict[str, object] = {}
        owners: dict[str, str] = {}
        for name, value in capabilities.items():
            if not isinstance(value, dict):
                raise ValueError("Recovery Platform Status capability is invalid")
            readiness = str(value.get("readiness") or "unknown")
            owners[str(name)] = readiness
            configurations[str(name)] = {
                "configuration_revision": value.get("configuration_revision"),
                "verification_revision": (
                    value.get("verification", {}).get("revision")
                    if isinstance(value.get("verification"), dict) else None
                ),
            }
        connector_result = phase_execution.get("result")
        return {
            "configurations": configurations,
            "public_owners": owners,
            "durable": {
                "incident": canonical_sha256(workbench),
                "governance": canonical_sha256({
                    "phase_review": phase_review, "phase_execution": phase_execution,
                }),
                "connector_journal": canonical_sha256({
                    "command_id": scope.command_id,
                    "journal_recorded_at": phase_execution.get("journal_recorded_at"),
                    "result": connector_result,
                }),
                "delivery": canonical_sha256(delivery),
                "report": canonical_sha256(report),
            },
            "durable_identities": scope.durable_identities(),
            "telemetry": {
                "metric_observed_at": telemetry.get("recovery_metric_observed_at"),
                "log_observed_at": telemetry.get("recovery_log_observed_at"),
                "metric_ref_hashes": metric_hashes,
                "log_ref_hashes": log_hashes,
            },
        }

    @staticmethod
    def _get(session: UserSession, path: str) -> dict[str, object]:
        response = session.request("GET", path)
        if response.status != 200 or not isinstance(response.body, dict):
            raise RuntimeError(f"Recovery public read {path} returned HTTP {response.status}")
        return response.body

    @staticmethod
    def _unique(value: object, key: str, expected: str, label: str) -> dict[str, object]:
        matches = [
            item for item in value if isinstance(item, dict) and item.get(key) == expected
        ] if isinstance(value, list) else []
        if len(matches) != 1:
            raise ValueError(f"Recovery {label} public identity is not unique")
        return matches[0]


class KubernetesRecoveryAdapter:
    """Runs the matrix-frozen R01/R02 Kubernetes operations without a shell."""

    def __init__(
        self,
        *,
        commands: CommandExecutor,
        public_probe: PublicRecoveryProbe,
        release_root: Path,
        candidate_sha256: str,
        kube_context: str,
        cluster_identity_sha256: str,
        pod_deleter: ExactPodDeleter | None = None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        if (
            not release_root.is_dir()
            or not re.fullmatch(r"[0-9a-f]{64}", candidate_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", cluster_identity_sha256)
            or not kube_context
        ):
            raise ValueError("Recovery Kubernetes Adapter identity is invalid")
        self.commands = commands
        self.public_probe = public_probe
        self.release_root = release_root
        self.candidate_sha256 = candidate_sha256
        self.release_inventory_sha256 = self._release_inventory_sha256()
        self.kube_context = kube_context
        self.cluster_identity_sha256 = cluster_identity_sha256
        self.pod_deleter = pod_deleter or KubernetesExactPodDeleter(
            kube_context=kube_context,
        )
        self.sleep = sleep
        self.monotonic = monotonic

    def snapshot(self, scope: RecoveryScope) -> dict[str, object]:
        if (
            scope.candidate_sha256 != self.candidate_sha256
            or scope.release_inventory_sha256 != self.release_inventory_sha256
            or scope.kube_context != self.kube_context
            or scope.cluster_identity_sha256 != self.cluster_identity_sha256
        ):
            raise ValueError("Recovery Adapter identity does not match the ledger")
        return self._cluster_snapshot() | self.public_probe.capture(scope)

    def delete_current_pod(
        self, target: RecoveryTarget, *, scope: RecoveryScope,
        pod_uid: str, operation_id: str,
    ) -> dict[str, object]:
        self._require_target(target, R01_TARGETS)
        before = self._cluster_snapshot()
        workload = self._workload(before, target)
        pod_name = workload.get("current_pod_name")
        resource_version = workload.get("current_pod_resource_version")
        if (
            workload.get("current_pod_uid") != pod_uid
            or not isinstance(pod_name, str)
            or not isinstance(resource_version, str)
        ):
            raise ValueError("R01 exact current Pod identity drifted before delete")
        self._require(self._run([
            "annotate", "pod", pod_name,
            f"{_OPERATION_ANNOTATION}={operation_id}",
            f"--resource-version={resource_version}",
        ]), f"bind exact {target.owner} Pod delete identity")
        marked = self._workload(self._cluster_snapshot(), target)
        if (
            marked.get("current_pod_uid") != pod_uid
            or marked.get("current_pod_operation_id") != operation_id
        ):
            raise ValueError("R01 exact current Pod changed before delete dispatch")
        self.pod_deleter.delete(
            namespace=NAMESPACE,
            name=pod_name,
            uid=pod_uid,
            resource_version=resource_version,
        )
        during = self._cluster_snapshot()
        public_observation = self._during_public(scope)
        after_workload = self._wait_target(
            target,
            lambda item: item.get("ready") is True
            and item.get("current_pod_uid") not in {None, "", pod_uid},
        )
        after = self.snapshot(scope)
        if self._workload(after, target).get("current_pod_uid") \
                != after_workload.get("current_pod_uid"):
            raise ValueError("R01 replacement Pod drifted before restored-state capture")
        return self._delete_result(
            target,
            pod_uid,
            operation_id,
            during,
            public_observation,
            self._workload(during, target),
            after_workload,
            after,
        )

    def reconcile_deleted_pod(
        self, target: RecoveryTarget, *, scope: RecoveryScope,
        pod_uid: str, operation_id: str,
    ) -> dict[str, object] | None:
        self._require_target(target, R01_TARGETS)
        snapshot = self._cluster_snapshot()
        workload = self._workload(snapshot, target)
        if workload.get("current_pod_uid") == pod_uid:
            return None
        if workload.get("ready") is not True or not workload.get("current_pod_uid"):
            return None
        after = self.snapshot(scope)
        return self._delete_result(
            target,
            pod_uid,
            operation_id,
            snapshot,
            {
                "availability": "not_observed",
                "reason_code": "interrupted_effect",
            },
            workload,
            workload,
            after,
        )

    def rollout(
        self, target: RecoveryTarget, *, operation_id: str,
    ) -> dict[str, object]:
        self._require_target(target, R02_TARGETS)
        patch = json.dumps(
            {"spec": {"template": {"metadata": {"annotations": {
                _OPERATION_ANNOTATION: operation_id,
            }}}}},
            sort_keys=True,
            separators=(",", ":"),
        )
        self._require(self._run([
            "patch", target.kind, target.name, "--type=merge", "-p", patch,
        ]), f"roll out exact {target.owner} owner")
        after = self._wait_target(
            target,
            lambda item: item.get("ready") is True
            and item.get("annotation_operation_id") == operation_id,
        )
        return self._rollout_result(target, operation_id, after)

    def reconcile_rollout(
        self, target: RecoveryTarget, *, operation_id: str,
    ) -> dict[str, object] | None:
        self._require_target(target, R02_TARGETS)
        workload = self._workload(self._cluster_snapshot(), target)
        if (
            workload.get("ready") is not True
            or workload.get("annotation_operation_id") != operation_id
        ):
            return None
        return self._rollout_result(target, operation_id, workload)

    def reapply_candidate(self, *, operation_id: str) -> dict[str, object]:
        if self._release_inventory_sha256() != self.release_inventory_sha256:
            raise ValueError("Recovery candidate release tree changed before reapply")
        manager = self._field_manager(operation_id)
        self._require(self._run([
            "apply", "-k", str(self.release_root), f"--field-manager={manager}",
        ], timeout=300), "reapply exact candidate")
        self._wait_all(lambda item: item.get("ready") is True)
        return self._reapply_result(operation_id, manager)

    def reconcile_reapply(self, *, operation_id: str) -> dict[str, object] | None:
        if self._release_inventory_sha256() != self.release_inventory_sha256:
            return None
        manager = self._field_manager(operation_id)
        snapshot = self._cluster_snapshot()
        workloads = snapshot.get("workloads")
        if not isinstance(workloads, dict) or any(
            not isinstance(workloads.get(target.key), dict)
            or workloads[target.key].get("ready") is not True
            or manager not in workloads[target.key].get("field_managers", [])
            for target in R02_TARGETS
        ):
            return None
        return self._reapply_result(operation_id, manager)

    def _cluster_snapshot(self) -> dict[str, object]:
        resources = self._json(self._run([
            "get", "deployment,daemonset,pod,pvc,configmap", "-o", "json",
        ]), "read Recovery Kubernetes resources")
        secret_payload = self._json(self._run([
            "get", "secret", *SECRET_NAMES, "-o", "json",
        ]), "read Recovery Secret identities")
        items = resources.get("items")
        secrets = secret_payload.get("items")
        if not isinstance(items, list) or not isinstance(secrets, list):
            raise ValueError("Recovery Kubernetes list response is invalid")
        return {
            "identity": {
                "candidate_sha256": self.candidate_sha256,
                "release_inventory_sha256": self.release_inventory_sha256,
                "kube_context": self.kube_context,
                "cluster_identity_sha256": self.cluster_identity_sha256,
            },
            "workloads": {
                target.key: self._project_workload(items, target) for target in R02_TARGETS
            },
            "pvcs": self._project_pvcs(items),
            "protected_resources": self._project_secrets(secrets),
            "cluster_configurations": self._project_configmaps(items),
        }

    def _project_workload(
        self, items: list[object], target: RecoveryTarget,
    ) -> dict[str, object]:
        kind = "Deployment" if target.kind == "deployment" else "DaemonSet"
        owners = [
            item for item in items
            if isinstance(item, dict)
            and item.get("kind") == kind
            and item.get("metadata", {}).get("name") == target.name
        ]
        pods = [
            item for item in items
            if isinstance(item, dict)
            and item.get("kind") == "Pod"
            and item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name")
            == target.name
        ]
        if len(owners) != 1:
            raise ValueError(f"Recovery owner {target.name} inventory is not exact")
        owner = owners[0]
        metadata = owner.get("metadata", {})
        spec = owner.get("spec", {})
        status = owner.get("status", {})
        ready = self._ready(kind, metadata, spec, status)
        ready_pods = [pod for pod in pods if self._pod_ready(pod)]
        images = sorted(
            {
                (
                    str(container.get("name")),
                    str(container.get("image")),
                    str(container.get("imageID")),
                )
                for pod in pods
                for container in pod.get("status", {}).get("containerStatuses", [])
                if container.get("imageID")
            }
        )
        annotations = spec.get("template", {}).get("metadata", {}).get("annotations", {})
        return {
            "owner": target.owner,
            "uid": metadata.get("uid"),
            "resource_version": metadata.get("resourceVersion"),
            "generation": metadata.get("generation"),
            "ready": ready and bool(pods) and len(ready_pods) == len(pods),
            "current_pod_name": (
                ready_pods[0].get("metadata", {}).get("name")
                if len(pods) == len(ready_pods) == 1 else None
            ),
            "current_pod_uid": (
                ready_pods[0].get("metadata", {}).get("uid")
                if len(pods) == len(ready_pods) == 1 else None
            ),
            "current_pod_resource_version": (
                ready_pods[0].get("metadata", {}).get("resourceVersion")
                if len(pods) == len(ready_pods) == 1 else None
            ),
            "current_pod_operation_id": (
                ready_pods[0].get("metadata", {}).get("annotations", {}).get(
                    _OPERATION_ANNOTATION
                ) if len(pods) == len(ready_pods) == 1 else None
            ),
            "pod_uids": sorted(str(pod.get("metadata", {}).get("uid")) for pod in pods),
            "images": [
                {"container": name, "image": image, "image_id": image_id}
                for name, image, image_id in images
            ],
            "annotation_operation_id": annotations.get(_OPERATION_ANNOTATION),
            "field_managers": sorted({
                str(item.get("manager")) for item in metadata.get("managedFields", [])
                if item.get("manager")
            }),
        }

    @staticmethod
    def _project_pvcs(items: list[object]) -> dict[str, object]:
        return {
            str(item.get("metadata", {}).get("name")): {
                "uid": item.get("metadata", {}).get("uid"),
                "phase": item.get("status", {}).get("phase"),
            }
            for item in items
            if isinstance(item, dict) and item.get("kind") == "PersistentVolumeClaim"
        }

    @staticmethod
    def _project_secrets(items: list[object]) -> dict[str, object]:
        return {
            str(item.get("metadata", {}).get("name")): {
                "uid": item.get("metadata", {}).get("uid"),
                "resource_version": item.get("metadata", {}).get("resourceVersion"),
                "key_inventory_sha256": canonical_sha256(sorted(item.get("data", {}))),
                "value_sha256": canonical_sha256(item.get("data", {})),
            }
            for item in items if isinstance(item, dict)
        }

    @staticmethod
    def _project_configmaps(items: list[object]) -> dict[str, object]:
        return {
            str(item.get("metadata", {}).get("name")): {
                "uid": item.get("metadata", {}).get("uid"),
                "resource_version": item.get("metadata", {}).get("resourceVersion"),
                "data_sha256": canonical_sha256(item.get("data", {})),
            }
            for item in items
            if isinstance(item, dict) and item.get("kind") == "ConfigMap"
        }

    @staticmethod
    def _ready(kind: str, metadata: dict, spec: dict, status: dict) -> bool:
        generation = metadata.get("generation")
        if status.get("observedGeneration") != generation:
            return False
        if kind == "Deployment":
            replicas = int(spec.get("replicas", 1))
            return (
                status.get("updatedReplicas") == replicas
                and status.get("availableReplicas") == replicas
                and any(
                    item.get("type") == "Available" and item.get("status") == "True"
                    for item in status.get("conditions", [])
                )
            )
        return (
            status.get("desiredNumberScheduled") == status.get("updatedNumberScheduled")
            == status.get("numberReady")
        )

    @staticmethod
    def _pod_ready(pod: dict) -> bool:
        return any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in pod.get("status", {}).get("conditions", [])
        )

    def _wait_target(
        self, target: RecoveryTarget, predicate,
    ) -> dict[str, object]:
        deadline = self.monotonic() + 600
        while True:
            workload = self._workload(self._cluster_snapshot(), target)
            if predicate(workload):
                return workload
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Recovery owner {target.owner} did not become Ready")
            self.sleep(min(5.0, remaining))

    def _wait_all(self, predicate) -> None:
        deadline = self.monotonic() + 600
        while True:
            workloads = self._cluster_snapshot().get("workloads")
            if isinstance(workloads, dict) and all(
                isinstance(workloads.get(target.key), dict)
                and predicate(workloads[target.key]) for target in R02_TARGETS
            ):
                return
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise TimeoutError("Recovery candidate workloads did not converge")
            self.sleep(min(5.0, remaining))

    @staticmethod
    def _workload(snapshot: dict[str, object], target: RecoveryTarget) -> dict[str, object]:
        workloads = snapshot.get("workloads")
        value = workloads.get(target.key) if isinstance(workloads, dict) else None
        if not isinstance(value, dict):
            raise ValueError(f"Recovery owner {target.owner} snapshot is missing")
        return value

    def _run(self, args: list[str], *, timeout: float = 120) -> CommandResult:
        return self.commands.run(
            ["kubectl", "--context", self.kube_context, "-n", NAMESPACE, *args],
            timeout=timeout,
        )

    @staticmethod
    def _require(result: CommandResult, action: str) -> CommandResult:
        if result.exit_code != 0:
            raise RuntimeError(f"Recovery failed to {action} (exit {result.exit_code})")
        return result

    @classmethod
    def _json(cls, result: CommandResult, action: str) -> dict[str, object]:
        payload = json.loads(cls._require(result, action).stdout)
        if not isinstance(payload, dict):
            raise ValueError(f"Recovery {action} returned invalid JSON")
        return payload

    @staticmethod
    def _require_target(
        target: RecoveryTarget, allowed: tuple[RecoveryTarget, ...],
    ) -> None:
        if target not in allowed:
            raise ValueError("Recovery target is outside the frozen matrix")

    @staticmethod
    def _field_manager(operation_id: str) -> str:
        return "aiops-acceptance-" + hashlib.sha256(operation_id.encode()).hexdigest()[:20]

    @staticmethod
    def _delete_result(
        target: RecoveryTarget,
        pod_uid: str,
        operation_id: str,
        during: dict[str, object],
        public_observation: dict[str, object],
        during_workload: dict[str, object],
        after_workload: dict[str, object],
        after: dict[str, object],
    ) -> dict[str, object]:
        return {
            "status": "succeeded",
            "operation_id": operation_id,
            "target": target.key,
            "deleted_pod_uid": pod_uid,
            "replacement_pod_uid": after_workload.get("current_pod_uid"),
            "owner_ready": after_workload.get("ready"),
            "during": {
                "old_pod_absent": pod_uid not in during_workload.get("pod_uids", []),
                "pod_uids": during_workload.get("pod_uids"),
                "owner_ready": during_workload.get("ready"),
                "pvcs": during.get("pvcs"),
                "protected_resources": during.get("protected_resources"),
                "cluster_configurations": during.get("cluster_configurations"),
                "public_observation": public_observation,
            },
            "after": after,
        }

    def _during_public(self, scope: RecoveryScope) -> dict[str, object]:
        try:
            facts = self.public_probe.capture(scope)
        except Exception as exc:
            return {
                "availability": "unavailable",
                "reason_code": "public_projection_unavailable",
                "error_type": type(exc).__name__,
            }
        return {"availability": "available", "facts": facts}

    @staticmethod
    def _rollout_result(
        target: RecoveryTarget, operation_id: str, workload: dict[str, object],
    ) -> dict[str, object]:
        return {
            "status": "succeeded",
            "operation_id": operation_id,
            "target": target.key,
            "annotation_operation_id": workload.get("annotation_operation_id"),
            "owner_ready": workload.get("ready"),
            "images": workload.get("images"),
        }

    def _reapply_result(self, operation_id: str, manager: str) -> dict[str, object]:
        return {
            "status": "succeeded",
            "operation_id": operation_id,
            "candidate_sha256": self.candidate_sha256,
            "release_inventory_sha256": self.release_inventory_sha256,
            "field_manager": manager,
            "owners_ready": True,
        }

    def _release_inventory_sha256(self) -> str:
        return inventory_artifact_sha256(build_release_inventory(self.release_root))
