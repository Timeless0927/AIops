from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from aiops.acceptance.ledger import AcceptanceLedger, GateFailed
from aiops.acceptance.command import CommandResult
from aiops.acceptance.http import HttpResponse
from aiops.acceptance.dependency_degradation import R03_TARGET
from aiops.acceptance.recovery import (
    PVC_NAMES,
    R01_TARGETS,
    R02_TARGETS,
    SECRET_NAMES,
    RecoveryGateRunner,
    RecoveryScope,
    RecoveryTarget,
    canonical_sha256,
    recovery_metric_ref_sha256,
)
from aiops.acceptance.recovery_adapters import (
    GatewayRecoveryProbe,
    KubernetesRecoveryAdapter,
)
from tests.pilot_acceptance_recovery_support import attest_recovery, recovery_ledger


_ledger = recovery_ledger
_attest = attest_recovery


class FakeRecoveryAdapter:
    def __init__(
        self,
        *,
        interrupt_action: str | None = None,
        fail_action: str | None = None,
        final_drift: tuple[str, str] | None = None,
    ) -> None:
        self.interrupt_action = interrupt_action
        self.fail_action = fail_action
        self.final_drift = final_drift
        self.interrupted: set[str] = set()
        self.calls: list[str] = []
        self.snapshot_count = 0
        self.pods = {target.key: f"pod-{target.owner}-1" for target in R02_TARGETS}
        self.rollouts: dict[str, str] = {}
        self.reapplied: str | None = None

    def snapshot(self, scope: RecoveryScope) -> dict[str, object]:
        self.snapshot_count += 1
        value: dict[str, object] = {
            "identity": {
                "candidate_sha256": scope.candidate_sha256,
                "release_inventory_sha256": scope.release_inventory_sha256,
                "kube_context": scope.kube_context,
                "cluster_identity_sha256": scope.cluster_identity_sha256,
            },
            "workloads": {
                target.key: {
                    "owner": target.owner,
                    "uid": f"owner-{target.owner}",
                    "ready": True,
                    "current_pod_uid": self.pods[target.key],
                    "images": [{
                        "container": target.owner,
                        "image": f"example/{target.owner}:candidate",
                        "image_id": f"sha256:{hashlib.sha256(target.owner.encode()).hexdigest()}",
                    }],
                }
                for target in R02_TARGETS
            },
            "pvcs": {
                name: {"uid": f"uid-{name}", "phase": "Bound"}
                for name in PVC_NAMES
            },
            "protected_resources": {
                name: {
                    "uid": f"uid-{name}",
                    "resource_version": "1",
                    "key_inventory_sha256": "7" * 64,
                    "value_sha256": "e" * 64,
                }
                for name in SECRET_NAMES
            },
            "cluster_configurations": {"runtime": {"uid": "config-runtime"}},
            "configurations": {"runtime": "config-v1", "notification": "notification-v1"},
            "durable": {
                "incident": "1" * 64,
                "governance": "2" * 64,
                "connector_journal": "3" * 64,
                "delivery": "4" * 64,
                "report": "5" * 64,
            },
            "durable_identities": scope.durable_identities(),
            "telemetry": {
                "metric_observed_at": 1310.0,
                "log_observed_at": 1305.0,
                "metric_ref_hashes": ["8" * 64],
                "log_ref_hashes": ["6" * 64],
            },
            "public_owners": {"gateway": "ready", "connector": "ready"},
        }
        if self.final_drift is not None and self.snapshot_count > 1:
            section, key = self.final_drift
            if section == "images":
                workloads = value["workloads"]
                assert isinstance(workloads, dict)
                workloads[key]["images"][0]["image_id"] = "sha256:drift"  # type: ignore[index]
            else:
                collection = value[section]
                assert isinstance(collection, dict)
                collection[key] = {"drift": True}
        return copy.deepcopy(value)

    def delete_current_pod(
        self, target: RecoveryTarget, *, scope: RecoveryScope,
        pod_uid: str, operation_id: str,
    ) -> dict[str, object]:
        action = f"delete:{target.owner}"
        self._effect(action, operation_id)
        self.pods[target.key] = f"pod-{target.owner}-2"
        if self.interrupt_action == action and action not in self.interrupted:
            self.interrupted.add(action)
            raise KeyboardInterrupt
        return self._delete_result(target, scope, pod_uid, operation_id)

    def reconcile_deleted_pod(
        self, target: RecoveryTarget, *, scope: RecoveryScope,
        pod_uid: str, operation_id: str,
    ) -> dict[str, object] | None:
        self.calls.append(f"reconcile-delete:{target.owner}")
        if self.pods[target.key] == pod_uid:
            return None
        return self._delete_result(target, scope, pod_uid, operation_id)

    def rollout(
        self, target: RecoveryTarget, *, operation_id: str,
    ) -> dict[str, object]:
        action = f"rollout:{target.owner}"
        self._effect(action, operation_id)
        self.rollouts[target.key] = operation_id
        if self.interrupt_action == action and action not in self.interrupted:
            self.interrupted.add(action)
            raise KeyboardInterrupt
        return self._rollout_result(target, operation_id)

    def reconcile_rollout(
        self, target: RecoveryTarget, *, operation_id: str,
    ) -> dict[str, object] | None:
        self.calls.append(f"reconcile-rollout:{target.owner}")
        if self.rollouts.get(target.key) != operation_id:
            return None
        return self._rollout_result(target, operation_id)

    def reapply_candidate(self, *, operation_id: str) -> dict[str, object]:
        self._effect("reapply", operation_id)
        self.reapplied = operation_id
        if self.interrupt_action == "reapply" and "reapply" not in self.interrupted:
            self.interrupted.add("reapply")
            raise KeyboardInterrupt
        return self._reapply_result(operation_id)

    def reconcile_reapply(self, *, operation_id: str) -> dict[str, object] | None:
        self.calls.append("reconcile-reapply")
        return self._reapply_result(operation_id) if self.reapplied == operation_id else None

    def _effect(self, action: str, operation_id: str) -> None:
        self.calls.append(action)
        assert operation_id
        if self.fail_action == action:
            raise RuntimeError(f"{action} failed")

    def _delete_result(
        self,
        target: RecoveryTarget,
        scope: RecoveryScope,
        pod_uid: str,
        operation_id: str,
    ) -> dict[str, object]:
        after = self.snapshot(scope)
        return {
            "status": "succeeded",
            "operation_id": operation_id,
            "target": target.key,
            "deleted_pod_uid": pod_uid,
            "replacement_pod_uid": self.pods[target.key],
            "owner_ready": True,
            "during": {
                "old_pod_absent": True,
                "pod_uids": [self.pods[target.key]],
                "owner_ready": True,
                "pvcs": after["pvcs"],
                "protected_resources": after["protected_resources"],
                "cluster_configurations": after["cluster_configurations"],
                "public_observation": {"availability": "available", "facts": {
                    key: after[key] for key in ("configurations", "durable", "telemetry", "public_owners")}},
            },
            "after": after,
        }
    @staticmethod
    def _rollout_result(target: RecoveryTarget, operation_id: str) -> dict[str, object]:
        return {
            "status": "succeeded",
            "operation_id": operation_id,
            "target": target.key,
            "annotation_operation_id": operation_id,
            "owner_ready": True,
        }

    @staticmethod
    def _reapply_result(operation_id: str) -> dict[str, object]:
        return {
            "status": "succeeded",
            "operation_id": operation_id,
            "candidate_sha256": "a" * 64,
            "release_inventory_sha256": hashlib.sha256(b"[]\n").hexdigest(),
            "owners_ready": True,
        }


class UnprovableDeleteAdapter(FakeRecoveryAdapter):
    def delete_current_pod(
        self, target: RecoveryTarget, *, scope: RecoveryScope,
        pod_uid: str, operation_id: str,
    ) -> dict[str, object]:
        self.calls.append(f"delete:{target.owner}")
        raise KeyboardInterrupt


def _run_r01(evidence: AcceptanceLedger, adapter: FakeRecoveryAdapter) -> None:
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)
    paused = runner.run_r01()
    _attest(evidence, "R01", paused["review_sha256"])
    runner.resume_r01()


def test_r01_pauses_then_deletes_exact_current_pods_in_owner_order(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "R01")
    adapter = FakeRecoveryAdapter()
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)

    paused = runner.run_r01()

    for name, kwargs in (("invalid", {"run_id": "../escape"}),
                         ("cross-run", {"run_id": "run-v07", "v06_run_id": "run-v06"})):
        invalid = _ledger(tmp_path / name, "R01", **kwargs)
        with pytest.raises(GateFailed, match="invalid"):
            RecoveryGateRunner(evidence=invalid, adapter=FakeRecoveryAdapter()).run_r01()

    assert paused["status"] == "awaiting_attestation"
    assert adapter.calls == []
    review = json.loads(next(tmp_path.rglob("recovery-review.json")).read_text())
    assert [item["owner"] for item in review["effects"]] == [
        target.owner for target in R01_TARGETS
    ]

    _attest(evidence, "R01", paused["review_sha256"])
    result = runner.resume_r01()

    assert result == {"gate_id": "R01", "status": "passed", "operations": "6"}
    assert adapter.calls == [f"delete:{target.owner}" for target in R01_TARGETS]
    final = evidence.passed_artifact_json("R01", "stateful-recovery.json")["value"]
    assert len(final["operations"]) == 6
    assert all(item["owner_ready"] for item in final["operations"])
    assert set(final["after"]["protected_resources"]) == set(SECRET_NAMES)


def test_r01_interruption_reconciles_without_replaying_delete(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "R01")
    adapter = FakeRecoveryAdapter(interrupt_action="delete:gateway")
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)
    paused = runner.run_r01()
    _attest(evidence, "R01", paused["review_sha256"])

    with pytest.raises(KeyboardInterrupt):
        runner.resume_r01()
    result = runner.resume_r01()

    assert result["status"] == "passed"
    assert adapter.calls.count("delete:gateway") == 1
    assert adapter.calls.count("reconcile-delete:gateway") == 1


def test_r01_interruption_before_delete_dispatch_fails_unprovable_without_replay(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "R01")
    adapter = UnprovableDeleteAdapter()
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)
    paused = runner.run_r01()
    _attest(evidence, "R01", paused["review_sha256"])

    with pytest.raises(KeyboardInterrupt):
        runner.resume_r01()
    with pytest.raises(GateFailed, match="unprovable"):
        runner.resume_r01()

    assert adapter.calls == ["delete:gateway", "reconcile-delete:gateway"]


def test_r01_partial_failure_stops_before_later_owner(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "R01")
    adapter = FakeRecoveryAdapter(fail_action="delete:connector")
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)
    paused = runner.run_r01()
    _attest(evidence, "R01", paused["review_sha256"])

    with pytest.raises(GateFailed, match="R01"):
        runner.resume_r01()

    assert adapter.calls == ["delete:gateway", "delete:diagnosis", "delete:connector"]
    assert evidence.failed_gate == "R01"


@pytest.mark.parametrize(
    "drift",
    [
        ("pvcs", PVC_NAMES[0]),
        ("protected_resources", SECRET_NAMES[0]),
        ("configurations", "runtime"),
        ("durable", "incident"),
        ("durable", "unexpected"),
        ("durable_identities", "report"),
        ("telemetry", "metric_observed_at"),
        ("images", R01_TARGETS[0].key),
    ],
)
def test_r01_fails_on_durable_state_or_image_loss(tmp_path: Path, drift: tuple[str, str]) -> None:
    evidence = _ledger(tmp_path, "R01")
    adapter = FakeRecoveryAdapter(final_drift=drift)
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)
    paused = runner.run_r01()
    _attest(evidence, "R01", paused["review_sha256"])

    with pytest.raises(GateFailed, match="R01"):
        runner.resume_r01()


def test_r02_rolls_each_candidate_owner_then_reapplies_same_bundle(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "R01")
    adapter = FakeRecoveryAdapter()
    _run_r01(evidence, adapter)
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)
    paused = runner.run_r02()

    assert adapter.calls == [f"delete:{target.owner}" for target in R01_TARGETS]
    _attest(evidence, "R02", paused["review_sha256"])
    result = runner.resume_r02()

    assert result == {"gate_id": "R02", "status": "passed", "operations": "13"}
    assert adapter.calls[-13:] == [
        *(f"rollout:{target.owner}" for target in R02_TARGETS),
        "reapply",
    ]


def test_r02_interruption_reconciles_rollout_without_replay(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "R01")
    adapter = FakeRecoveryAdapter()
    _run_r01(evidence, adapter)
    adapter.interrupt_action = "rollout:gateway"
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)
    paused = runner.run_r02()
    _attest(evidence, "R02", paused["review_sha256"])

    with pytest.raises(KeyboardInterrupt):
        runner.resume_r02()
    assert runner.resume_r02()["status"] == "passed"
    assert adapter.calls.count("rollout:gateway") == 1
    assert adapter.calls.count("reconcile-rollout:gateway") == 1


def test_r02_interruption_reconciles_reapply_without_replay(tmp_path: Path) -> None:
    evidence = _ledger(tmp_path, "R01")
    adapter = FakeRecoveryAdapter()
    _run_r01(evidence, adapter)
    adapter.interrupt_action = "reapply"
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)
    paused = runner.run_r02()
    _attest(evidence, "R02", paused["review_sha256"])

    with pytest.raises(KeyboardInterrupt):
        runner.resume_r02()
    assert runner.resume_r02()["status"] == "passed"
    assert adapter.calls.count("reapply") == 1
    assert adapter.calls.count("reconcile-reapply") == 1


@pytest.mark.parametrize(
    ("fail_action", "drift"),
    [
        ("reapply", None),
        (None, ("images", R02_TARGETS[0].key)),
        (None, ("protected_resources", SECRET_NAMES[-1])),
    ],
)
def test_r02_fails_for_reapply_digest_or_credential_drift(
    tmp_path: Path, fail_action: str | None, drift: tuple[str, str] | None,
) -> None:
    evidence = _ledger(tmp_path, "R01")
    adapter = FakeRecoveryAdapter()
    _run_r01(evidence, adapter)
    adapter.fail_action = fail_action
    adapter.final_drift = drift
    adapter.snapshot_count = 0
    runner = RecoveryGateRunner(evidence=evidence, adapter=adapter)
    paused = runner.run_r02()
    _attest(evidence, "R02", paused["review_sha256"])

    with pytest.raises(GateFailed, match="R02"):
        runner.resume_r02()


class PublicProbe:
    def capture(self, _scope: RecoveryScope) -> dict[str, object]:
        return {
            "configurations": {"connector": {"configuration_revision": "connector-v1"}},
            "public_owners": {"connector": "ready", "observability": "ready"},
            "durable": {
                "incident": "1" * 64,
                "governance": "2" * 64,
                "connector_journal": "3" * 64,
                "delivery": "4" * 64,
                "report": "5" * 64,
            },
            "telemetry": {
                "metric_observed_at": 1310.0,
                "log_observed_at": 1305.0,
                "log_ref_hashes": ["6" * 64],
            },
        }


class KubeCommands:
    def __init__(self, *, uid_race_after_annotate: bool = False) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.pod_versions = {target.key: 1 for target in R02_TARGETS}
        self.annotations: dict[str, str] = {}
        self.owner_annotations: dict[str, str] = {}
        self.replicas = {target.key: 1 for target in R02_TARGETS}
        self.pod_annotations: dict[str, str] = {}
        self.managers: set[str] = set()
        self.uid_race_after_annotate = uid_race_after_annotate

    def run(self, command, **_kwargs) -> CommandResult:
        command = tuple(command)
        self.commands.append(command)
        args = command[5:]
        if args[:2] == ("get", "deployment,daemonset,pod,pvc,configmap"):
            return self._result(command, {"items": self._resources()})
        if args[:2] == ("get", "secret"):
            return self._result(command, {"items": self._secrets()})
        if args[:2] == ("annotate", "pod"):
            name = args[2]
            target = next(item for item in R02_TARGETS if f"pod-{item.owner}-1" == name)
            self.pod_annotations[target.key] = args[3].split("=", 1)[1]
            assert args[4] == "--resource-version=1"
            if self.uid_race_after_annotate:
                self.pod_versions[target.key] += 1
            return self._result(command, {})
        if args[0] == "patch":
            target = next(item for item in R02_TARGETS if item.name == args[2])
            payload = json.loads(args[args.index("-p") + 1])
            if "replicas" in payload["spec"]:
                self.replicas[target.key] = payload["spec"]["replicas"]
                self.owner_annotations[target.key] = payload["metadata"]["annotations"][
                    "aiops.dev/acceptance-recovery-operation"
                ]
            else:
                self.annotations[target.key] = payload["spec"]["template"]["metadata"]["annotations"][
                    "aiops.dev/acceptance-recovery-operation"
                ]
            self.pod_versions[target.key] += 1
            return self._result(command, {})
        if args[:2] == ("apply", "-k"):
            manager = next(item.split("=", 1)[1] for item in args if item.startswith("--field-manager="))
            self.managers.add(manager)
            self.replicas = {target.key: 1 for target in R02_TARGETS}
            return self._result(command, {})
        raise AssertionError(f"unexpected command: {command}")

    @staticmethod
    def _result(command: tuple[str, ...], payload: dict[str, object]) -> CommandResult:
        return CommandResult(command, 0, json.dumps(payload), "", 0.1)

    def _resources(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for target in R02_TARGETS:
            generation = 1
            replicas = self.replicas[target.key]
            kind = "Deployment" if target.kind == "deployment" else "DaemonSet"
            status = {
                "observedGeneration": generation,
                "updatedReplicas": replicas,
                "availableReplicas": replicas,
                "conditions": [{"type": "Available", "status": "True"}],
            } if kind == "Deployment" else {
                "observedGeneration": generation,
                "desiredNumberScheduled": 1,
                "updatedNumberScheduled": 1,
                "numberReady": 1,
            }
            items.append({
                "kind": kind,
                "metadata": {
                    "name": target.name,
                    "uid": f"owner-{target.owner}",
                    "resourceVersion": "1",
                    "generation": generation,
                    "managedFields": [{"manager": item} for item in sorted(self.managers)],
                    "annotations": ({
                        "aiops.dev/acceptance-recovery-operation": self.owner_annotations[target.key],
                    } if target.key in self.owner_annotations else {}),
                },
                "spec": {
                    "replicas": replicas,
                    "template": {"metadata": {"annotations": ({
                        "aiops.dev/acceptance-recovery-operation": self.annotations[target.key],
                    } if target.key in self.annotations else {})}},
                },
                "status": status,
            })
            if replicas == 0:
                continue
            version = self.pod_versions[target.key]
            items.append({
                "kind": "Pod",
                "metadata": {
                    "name": f"pod-{target.owner}-1",
                    "uid": f"pod-{target.owner}-{version}",
                    "resourceVersion": "1",
                    "labels": {"app.kubernetes.io/name": target.name},
                    "annotations": ({
                        "aiops.dev/acceptance-recovery-operation": self.pod_annotations[target.key],
                    } if target.key in self.pod_annotations else {}),
                },
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [{
                        "name": target.owner,
                        "image": f"example/{target.owner}:candidate",
                        "imageID": f"sha256:{hashlib.sha256(target.owner.encode()).hexdigest()}",
                    }],
                },
            })
        items.extend({
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "uid": f"uid-{name}"},
            "status": {"phase": "Bound"},
        } for name in PVC_NAMES)
        items.append({
            "kind": "ConfigMap",
            "metadata": {"name": "aiops-runtime-config", "uid": "config-1", "resourceVersion": "1"},
            "data": {"AIOPS_CONFIGURATION_REVISION": "candidate-v1"},
        })
        return items

    @staticmethod
    def _secrets() -> list[dict[str, object]]:
        return [{
            "kind": "Secret",
            "metadata": {"name": name, "uid": f"uid-{name}", "resourceVersion": "1"},
            "data": {"credential": "Y3JlZGVudGlhbA=="},
        } for name in SECRET_NAMES]
class PodDeleter:
    def __init__(self, commands: KubeCommands) -> None:
        self.commands, self.calls = commands, []
    def delete(self, **identity: str) -> None:
        self.calls.append(identity)
        key = next(item.key for item in R02_TARGETS if f"pod-{item.owner}-1" == identity["name"])
        self.commands.pod_versions[key] += 1
def _scope() -> RecoveryScope:
    return RecoveryScope(
        candidate_sha256="a" * 64, release_inventory_sha256=hashlib.sha256(b"[]\n").hexdigest(),
        kube_context="pilot-context", cluster_identity_sha256="b" * 64,
        connector_id="connector-prod", cluster_id="pilot-cluster",
        run_id="run-1", alert_fingerprint="fingerprint-1",
        recovery_metric_observed_at="1310.0", recovery_log_observed_at="1305.0",
        recovery_metric_ref_sha256=recovery_metric_ref_sha256("run-1", 1310.0),
        recovery_log_refs_sha256=canonical_sha256(["f" * 64]),
        incident_id="incident-1", investigation_id="investigation-1",
        report_publication_id="report-1", report_sha256="d" * 64,
        notification_delivery_id="delivery-1", change_request_id="change-1",
        phase_id="phase-1", execution_id="execution-1",
        command_id="command-1",
    )


def _kube_adapter(tmp_path: Path, commands: KubeCommands, *, pod_deleter: PodDeleter | None = None) -> KubernetesRecoveryAdapter:
    release = tmp_path / "release"
    release.mkdir(exist_ok=True)
    return KubernetesRecoveryAdapter(
        commands=commands,
        public_probe=PublicProbe(),
        release_root=release,
        candidate_sha256="a" * 64,
        kube_context="pilot-context",
        cluster_identity_sha256="b" * 64,
        pod_deleter=pod_deleter or PodDeleter(commands),
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )


def test_kubernetes_recovery_adapter_uses_fixed_delete_and_rollout_commands(tmp_path: Path) -> None:
    commands = KubeCommands()
    deleter = PodDeleter(commands)
    adapter = _kube_adapter(tmp_path, commands, pod_deleter=deleter)
    before = adapter.snapshot(_scope())
    gateway = R01_TARGETS[0]
    workload = before["workloads"][gateway.key]  # type: ignore[index]

    deleted = adapter.delete_current_pod(
        gateway,
        scope=_scope(),
        pod_uid=workload["current_pod_uid"],  # type: ignore[index]
        operation_id="r01/execution/gateway",
    )
    rolled = adapter.rollout(gateway, operation_id="r02/execution/gateway")

    assert deleted["deleted_pod_uid"] != deleted["replacement_pod_uid"]
    assert rolled["annotation_operation_id"] == "r02/execution/gateway"
    mutations = [item for item in commands.commands if item[5] in {"annotate", "patch"}]
    assert mutations[0][5:8] == ("annotate", "pod", "pod-gateway-1")
    assert mutations[0][8].endswith("/gateway")
    assert mutations[0][9] == "--resource-version=1"
    assert deleter.calls == [{"namespace": "aiops-system", "name": "pod-gateway-1",
                              "uid": "pod-gateway-1", "resource_version": "1"}]
    assert mutations[1][5:8] == ("patch", "deployment", "aiops-gateway")


def test_kubernetes_recovery_adapter_rejects_uid_race_before_delete(tmp_path: Path) -> None:
    commands = KubeCommands(uid_race_after_annotate=True)
    deleter = PodDeleter(commands)
    adapter = _kube_adapter(tmp_path, commands, pod_deleter=deleter)
    gateway = R01_TARGETS[0]
    before = adapter.snapshot(_scope())
    workload = before["workloads"][gateway.key]  # type: ignore[index]

    with pytest.raises(ValueError, match="changed before delete"):
        adapter.delete_current_pod(
            gateway,
            scope=_scope(),
            pod_uid=workload["current_pod_uid"],  # type: ignore[index]
            operation_id="r01/execution/gateway",
        )

    assert deleter.calls == []


def test_kubernetes_recovery_adapter_reapply_is_identified_and_reconcilable(tmp_path: Path) -> None:
    commands = KubeCommands()
    adapter = _kube_adapter(tmp_path, commands)

    result = adapter.reapply_candidate(operation_id="r02/execution/candidate-reapply")
    reconciled = adapter.reconcile_reapply(operation_id="r02/execution/candidate-reapply")

    assert reconciled == result
    apply = next(item for item in commands.commands if item[5] == "apply")
    assert apply[5:8] == ("apply", "-k", str(tmp_path / "release"))
    assert any(item.startswith("--field-manager=aiops-acceptance-") for item in apply)
    (tmp_path / "release" / "drift.yaml").write_text("changed")
    with pytest.raises(ValueError, match="release tree changed"):
        adapter.reapply_candidate(operation_id="r02/execution/another-reapply")


def test_kubernetes_recovery_adapter_scales_only_the_frozen_dependency_target(
    tmp_path: Path,
) -> None:
    commands = KubeCommands()
    adapter = _kube_adapter(tmp_path, commands)
    before = adapter.snapshot_dependency(_scope(), R03_TARGET)

    scaled = adapter.scale_to_zero(
        R03_TARGET, original_replicas=1, operation_id="r03/execution/scale-zero",
    )
    reconciled = adapter.reconcile_scaled_to_zero(
        R03_TARGET, original_replicas=1, operation_id="r03/execution/scale-zero",
    )

    assert before["target"]["replicas"] == 1  # type: ignore[index]
    assert scaled == reconciled
    assert scaled["target"] == "deployment/aiops-connector"
    patch = next(item for item in commands.commands if item[5:8] == (
        "patch", "deployment", "aiops-connector",
    ) and any('"replicas":0' in arg for arg in item))
    payload = json.loads(patch[patch.index("-p") + 1])
    assert payload["metadata"]["resourceVersion"] == "1"
    assert payload["metadata"]["annotations"] == {
        "aiops.dev/acceptance-recovery-operation": "r03/execution/scale-zero",
    }


class Session:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses

    def request(self, method: str, path: str, **_kwargs) -> HttpResponse:
        assert method == "GET"
        return HttpResponse(200, copy.deepcopy(self.responses[path]), {})


class RetentionProbe:
    def probe_retention(
        self,
        run_id: str,
        *,
        metric_at: float,
        log_at: float,
    ) -> dict[str, object]:
        return {
            "run_id": run_id,
            "recovery_metric_observed_at": metric_at,
            "recovery_log_observed_at": log_at,
            "recovery_metric_ref_hashes": [recovery_metric_ref_sha256(run_id, metric_at)],
            "recovery_log_ref_hashes": ["f" * 64],
        }


def test_gateway_recovery_probe_hashes_public_history_without_raw_payloads() -> None:
    scope = _scope()
    report = {"id": scope.report_publication_id, "version": 1, "narrative": {"影响": "已恢复"}}
    scope = RecoveryScope(**{**scope.__dict__, "report_sha256": canonical_sha256(report)})
    user = Session({
        f"/api/v1/incidents/{scope.incident_id}/workbench": {
            "incident": {"id": scope.incident_id, "status": "resolved"},
            "investigation": {"id": scope.investigation_id, "status": "completed"},
        },
        f"/api/v1/incidents/{scope.incident_id}/report": {"publications": [report]},
        f"/api/v1/change-requests/{scope.change_request_id}/phase-approval": {
            "phase_review": {"phase_id": scope.phase_id, "approval": {"id": "approval-1"}},
        },
        f"/api/v1/change-requests/{scope.change_request_id}/phase-execution": {
            "phase_execution": {
                "id": scope.execution_id,
                "command_id": scope.command_id,
                "status": "succeeded",
                "result": {"status": "succeeded"},
            },
        },
        "/api/v1/platform/status": {
            "capabilities": {
                "connector": {"readiness": "ready", "configuration_revision": "connector-v1"},
                "observability": {"readiness": "ready", "configuration_revision": "observability-v1"},
            },
        },
    })
    admin = Session({
        "/api/v1/admin/notification-deliveries": {
            "deliveries": [{
                "id": scope.notification_delivery_id,
                "status": "sent",
                "request_id": "request-1",
                "provider_identity": "provider-1",
                "request": {
                    "event_type": "incident.resolved",
                    "subject": {"type": "incident", "id": scope.incident_id},
                },
            }],
        },
    })

    value = GatewayRecoveryProbe(
        user=user, admin=admin, telemetry=RetentionProbe(),
    ).capture(scope)

    assert value["public_owners"] == {"connector": "ready", "observability": "ready"}
    assert value["durable"]["report"] == scope.report_sha256  # type: ignore[index]
    assert "已恢复" not in json.dumps(value, ensure_ascii=False)
