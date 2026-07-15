"""A01 clean-cluster preflight and installation gates."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Sequence

import yaml

from .command import CommandExecutor, CommandResult
from .evidence import AcceptanceEvidence, Artifact
from .integration_support import fail_gate


NAMESPACE = "aiops-system"
BOOTSTRAP_SECRETS = {
    "aiops-runtime-secret": {
        "AIOPS_BOOTSTRAP_ADMIN_PASSWORD",
        "AIOPS_ALERTMANAGER_WEBHOOK_TOKEN",
    },
    "aiops-model-encryption": {"key"},
    "aiops-notification-encryption": {"key"},
    "aiops-change-encryption": {"key"},
}


class InputRequired(RuntimeError):
    """A signed human assertion is required before mutating the Cluster."""


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ClusterInstallRunner:
    def __init__(
        self,
        *,
        evidence: AcceptanceEvidence,
        commands: CommandExecutor,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.evidence = evidence
        self.commands = commands
        self.sleep = sleep
        self._command_results: list[CommandResult] = []

    def run_p03(self, release: Path) -> None:
        started_at = self.evidence.start_gate("P03")
        self._command_results = []
        try:
            attestations = self.evidence.require_verified_attestation(
                "P03", role="platform_operator"
            )
        except ValueError as exc:
            raise InputRequired(
                "P03 requires a signed Platform Operator attestation for non-production use, "
                "32Gi capacity and enforced NetworkPolicy"
            ) from exc
        artifacts: list[Artifact] = []
        preflight_namespace = "aiops-acceptance-preflight-" + self.evidence.candidate_sha256[:10]
        namespace_created = False
        cleanup_result: CommandResult | None = None
        try:
            resources = self._release_resources(release)
            images = self._images(resources)
            baseline = self._clean_baseline(resources)
            preflight = self._preflight_resources(preflight_namespace, images)
            applied = self._require(
                self._run(
                    ["kubectl", "apply", "-f", "-"],
                    stdin=yaml.safe_dump_all(preflight, sort_keys=False),
                    timeout=120,
                ),
                "create preflight resources",
            )
            namespace_created = True
            self._require(
                self._run(
                    [
                        "kubectl",
                        "wait",
                        "--for=jsonpath={.status.phase}=Bound",
                        "pvc/capacity-probe",
                        "-n",
                        preflight_namespace,
                        "--timeout=10m",
                    ],
                    timeout=660,
                ),
                "bind 32Gi preflight PVC",
            )
            image_pull = self._wait_for_image_pulls(
                preflight_namespace, images, set(baseline["nodes"])
            )
            artifacts.extend(
                [
                    self.evidence.write_json("P03", "clean-baseline.json", baseline),
                    self.evidence.write_text(
                        "P03", "preflight-apply.txt", self.evidence.command_text(applied)
                    ),
                    self.evidence.write_json("P03", "image-pull.json", image_pull),
                    self.evidence.write_json(
                        "P03",
                        "operator-attestation-index.json",
                        [
                            {
                                "actor": item["statement"]["actor"],
                                "role": item["statement"]["role"],
                                "observed_at": item["statement"]["observed_at"],
                                "fingerprint": item["fingerprint"],
                            }
                            for item in attestations
                        ],
                    ),
                ]
            )
        except Exception as exc:
            failure = exc
        else:
            failure = None
        finally:
            if namespace_created:
                cleanup_result = self._run(
                    [
                        "kubectl",
                        "delete",
                        "namespace",
                        preflight_namespace,
                        "--wait=true",
                        "--timeout=5m",
                    ],
                    timeout=330,
                )
                artifacts.append(
                    self.evidence.write_text(
                        "P03", "preflight-cleanup.txt", self.evidence.command_text(cleanup_result)
                    )
                )
                if cleanup_result.exit_code != 0 and failure is None:
                    failure = RuntimeError("preflight namespace cleanup failed")
        if failure is not None:
            artifacts.append(self._command_artifact("P03"))
            fail_gate(self.evidence, "P03", artifacts, failure, (), started_at)
        artifacts.append(self._command_artifact("P03"))
        self.evidence.record_gate("P03", "passed", artifacts, started_at=started_at)

    def run_i01(self, release: Path) -> None:
        started_at = self.evidence.start_gate("I01")
        self._command_results = []
        artifacts: list[Artifact] = []
        try:
            apply = self._require(
                self._run(["kubectl", "apply", "-k", str(release)], timeout=300),
                "apply release",
            )
            waits = [
                [
                    "kubectl", "wait", "-n", NAMESPACE, "--for=condition=complete",
                    "job/aiops-bootstrap", "--timeout=2m",
                ],
                [
                    "kubectl", "wait", "-n", NAMESPACE,
                    "--for=jsonpath={.status.phase}=Bound", "pvc", "--all", "--timeout=10m",
                ],
                [
                    "kubectl", "wait", "-n", NAMESPACE, "--for=condition=Available",
                    "deployment", "--all", "--timeout=10m",
                ],
            ]
            wait_results = [
                self._require(self._run(command, timeout=660), "wait for installation")
                for command in waits
            ]
            snapshot = self._installation_snapshot()
            events = self._event_snapshot()
            artifacts.append(
                self.evidence.write_text(
                    "I01", "apply.txt", self.evidence.command_text(apply)
                )
            )
            artifacts.append(
                self.evidence.write_text(
                    "I01",
                    "waits.txt",
                    "\n".join(self.evidence.command_text(result) for result in wait_results),
                )
            )
            artifacts.append(self.evidence.write_json("I01", "objects.json", snapshot))
            artifacts.append(self.evidence.write_json("I01", "events.json", events))
            artifacts.append(self._command_artifact("I01"))
            self.evidence.record_gate("I01", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            artifacts.extend(self._i01_diagnostics())
            artifacts.append(self._command_artifact("I01"))
            fail_gate(self.evidence, "I01", artifacts, exc, (), started_at)

    def run_i02(self, release: Path) -> None:
        started_at = self.evidence.start_gate("I02")
        self._command_results = []
        artifacts: list[Artifact] = []
        try:
            before = self._bootstrap_snapshot()
            apply = self._require(
                self._run(["kubectl", "apply", "-k", str(release)], timeout=300),
                "reapply release",
            )
            convergence = [
                self._require(
                    self._run(command, timeout=660),
                    "wait after reapply",
                )
                for command in (
                    [
                        "kubectl", "wait", "-n", NAMESPACE, "--for=condition=Available",
                        "deployment", "--all", "--timeout=10m",
                    ],
                    [
                        "kubectl", "rollout", "status", "daemonset",
                        "-n", NAMESPACE, "--timeout=10m",
                    ],
                )
            ]
            after = self._bootstrap_snapshot()
            workloads = self._reapply_workload_snapshot()
            if before != after:
                raise ValueError("bootstrap Secret UID/value hash or completion marker changed")
            artifacts.extend(
                [
                    self.evidence.write_json("I02", "bootstrap-before.json", before),
                    self.evidence.write_text(
                        "I02", "reapply.txt", self.evidence.command_text(apply)
                    ),
                    self.evidence.write_text(
                        "I02",
                        "workload-convergence.txt",
                        "\n".join(
                            self.evidence.command_text(result) for result in convergence
                        ),
                    ),
                    self.evidence.write_json("I02", "bootstrap-after.json", after),
                    self.evidence.write_json("I02", "workloads-after.json", workloads),
                    self._command_artifact("I02"),
                ]
            )
            self.evidence.record_gate("I02", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            artifacts.append(self._command_artifact("I02"))
            fail_gate(self.evidence, "I02", artifacts, exc, (), started_at)

    def _clean_baseline(self, resources: list[dict[str, Any]]) -> dict[str, Any]:
        context = self._require(
            self._run(["kubectl", "config", "current-context"], timeout=15),
            "read kube context",
        ).stdout.strip()
        if context != self.evidence.kube_context:
            raise ValueError("current kube context changed after acceptance initialization")
        config_result = self._require(
            self._run(
                ["kubectl", "config", "view", "--minify", "-o", "json"], timeout=15
            ),
            "read cluster identity",
        )
        config = json.loads(config_result.stdout)
        clusters = config.get("clusters", [])
        if len(clusters) != 1:
            raise ValueError("current kube context must resolve one cluster")
        cluster = clusters[0].get("cluster", {})
        identity = {
            "server": cluster.get("server"),
            "certificate_authority_data": cluster.get("certificate-authority-data", ""),
        }
        if _canonical_sha256(identity) != self.evidence.cluster_identity_sha256:
            raise ValueError("cluster identity changed after acceptance initialization")
        for namespace in (NAMESPACE, "aiops-verification"):
            result = self._run(
                ["kubectl", "get", "namespace", namespace, "-o", "name"], timeout=15
            )
            if result.exit_code == 0 or "NotFound" not in result.stderr:
                raise ValueError(f"clean baseline requires absent namespace {namespace}")
        cluster_scoped: list[str] = []
        for resource in resources:
            metadata = resource.get("metadata", {})
            if metadata.get("namespace") or resource.get("kind") in {
                "Namespace", "StorageClass", "CustomResourceDefinition"
            }:
                continue
            kind = str(resource.get("kind", ""))
            name = str(metadata.get("name", ""))
            if not kind or not name:
                continue
            result = self._run(
                ["kubectl", "get", kind.lower(), name, "-o", "name"], timeout=15
            )
            if result.exit_code == 0 or "NotFound" not in result.stderr:
                raise ValueError(f"clean baseline contains release cluster resource {kind}/{name}")
            cluster_scoped.append(f"{kind}/{name}")
        services = json.loads(
            self._require(
                self._run(
                    ["kubectl", "get", "services", "--all-namespaces", "-o", "json"],
                    timeout=30,
                ),
                "inspect NodePorts",
            ).stdout
        )
        occupants = [
            f"{item['metadata'].get('namespace', 'default')}/{item['metadata']['name']}"
            for item in services.get("items", [])
            if any(port.get("nodePort") == 30088 for port in item.get("spec", {}).get("ports", []))
        ]
        if occupants:
            raise ValueError(f"NodePort 30088 is already used by {occupants}")
        storage = json.loads(
            self._require(
                self._run(["kubectl", "get", "storageclass", "-o", "json"], timeout=30),
                "inspect StorageClasses",
            ).stdout
        )
        defaults = [
            item
            for item in storage.get("items", [])
            if item.get("metadata", {}).get("annotations", {}).get(
                "storageclass.kubernetes.io/is-default-class"
            ) == "true"
        ]
        if len(defaults) != 1:
            raise ValueError("clean baseline requires exactly one default StorageClass")
        node_payload = json.loads(
            self._require(
                self._run(["kubectl", "get", "nodes", "-o", "json"], timeout=30),
                "inspect Cluster nodes",
            ).stdout
        )
        nodes = sorted(
            str(item.get("metadata", {}).get("name", ""))
            for item in node_payload.get("items", [])
            if item.get("metadata", {}).get("name")
        )
        if not nodes:
            raise ValueError("clean baseline contains no Kubernetes nodes")
        return {
            "kube_context": context,
            "cluster_identity_sha256": self.evidence.cluster_identity_sha256,
            "namespaces_absent": [NAMESPACE, "aiops-verification"],
            "cluster_resources_absent": sorted(cluster_scoped),
            "nodeport_30088_free": True,
            "nodes": nodes,
            "default_storage_class": {
                "name": defaults[0]["metadata"]["name"],
                "provisioner": defaults[0].get("provisioner"),
                "volume_binding_mode": defaults[0].get("volumeBindingMode"),
            },
        }

    def _wait_for_image_pulls(
        self,
        namespace: str,
        images: set[str],
        expected_nodes: set[str],
    ) -> list[dict[str, str]]:
        last: list[dict[str, Any]] = []
        for _ in range(60):
            result = self._require(
                self._run(
                    [
                        "kubectl", "get", "pods", "-n", namespace, "-l",
                        "aiops.dev/acceptance-preflight=true", "-o", "json",
                    ],
                    timeout=30,
                ),
                "inspect node image pulls",
            )
            pods = json.loads(result.stdout).get("items", [])
            last = pods
            observed: dict[tuple[str, str], str] = {}
            nodes = {str(pod.get("spec", {}).get("nodeName", "")) for pod in pods}
            for pod in pods:
                node = str(pod.get("spec", {}).get("nodeName", ""))
                image_by_name = {
                    item["name"]: item["image"] for item in pod.get("spec", {}).get("containers", [])
                }
                for status in pod.get("status", {}).get("containerStatuses", []):
                    image_id = str(status.get("imageID", ""))
                    if image_id:
                        observed[(node, image_by_name.get(status["name"], ""))] = image_id
            if nodes == expected_nodes and all(
                (node, image) in observed for node in expected_nodes for image in images
            ):
                return [
                    {
                        "node": node,
                        "image": image,
                        "image_id_sha256": hashlib.sha256(observed[(node, image)].encode()).hexdigest(),
                    }
                    for node in sorted(expected_nodes)
                    for image in sorted(images)
                ]
            self.sleep(3)
        failures: list[dict[str, str]] = []
        for pod in last:
            node = str(pod.get("spec", {}).get("nodeName") or "unscheduled")
            containers = {
                item.get("name"): item.get("image", "unknown-image")
                for item in pod.get("spec", {}).get("containers", [])
            }
            statuses = pod.get("status", {}).get("containerStatuses", [])
            if not statuses:
                failures.extend(
                    {
                        "node": node,
                        "image": str(image),
                        "reason": str(pod.get("status", {}).get("phase") or "Pending"),
                    }
                    for image in containers.values()
                )
            for status in statuses:
                image = str(containers.get(status.get("name"), status.get("image", "unknown-image")))
                if (node, image) in observed:
                    continue
                state = status.get("state", {})
                detail = state.get("waiting") or state.get("terminated") or {}
                reason = detail.get("reason") or ("Running" if state.get("running") else "Pending")
                failures.append({
                    "node": node,
                    "image": image,
                    "reason": str(reason),
                })
        reported = {(item["node"], item["image"]) for item in failures}
        failures.extend(
            {"node": node, "image": image, "reason": "PodMissing"}
            for node in expected_nodes for image in images
            if (node, image) not in observed and (node, image) not in reported
        )
        failures.sort(key=lambda item: (item["node"], item["image"], item["reason"]))
        raise RuntimeError(
            f"node image pull preflight did not converge: {json.dumps(failures, sort_keys=True)}"
        )

    @staticmethod
    def _preflight_resources(namespace: str, images: set[str]) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace},
            },
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "capacity-probe", "namespace": namespace},
                "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "32Gi"}}},
            },
        ]
        capacity_image = sorted(images)[0]
        resources.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "capacity-probe",
                    "namespace": namespace,
                    "labels": {"aiops.dev/acceptance-preflight": "true"},
                },
                "spec": {
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "image",
                            "image": capacity_image,
                            "imagePullPolicy": "Always",
                            "command": ["/__aiops_acceptance_capacity_probe__"],
                            "volumeMounts": [{"name": "capacity", "mountPath": "/capacity"}],
                            "resources": {
                                "requests": {"cpu": "1m", "memory": "4Mi"},
                                "limits": {"cpu": "10m", "memory": "16Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "runAsUser": 65532,
                                "runAsGroup": 65532,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "capacity", "persistentVolumeClaim": {"claimName": "capacity-probe"}}
                    ],
                },
            }
        )
        for index, image in enumerate(sorted(images), start=1):
            name = f"pull-{index:02d}-{hashlib.sha256(image.encode()).hexdigest()[:8]}"
            labels = {
                "app.kubernetes.io/name": name,
                "aiops.dev/acceptance-preflight": "true",
            }
            resources.append(
                {
                    "apiVersion": "apps/v1",
                    "kind": "DaemonSet",
                    "metadata": {"name": name, "namespace": namespace},
                    "spec": {
                        "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "automountServiceAccountToken": False,
                                "tolerations": [{"operator": "Exists"}],
                                "containers": [
                                    {
                                        "name": "image",
                                        "image": image,
                                        "imagePullPolicy": "Always",
                                        "command": ["/__aiops_acceptance_image_pull_probe__"],
                                        "resources": {
                                            "requests": {"cpu": "1m", "memory": "4Mi"},
                                            "limits": {"cpu": "10m", "memory": "16Mi"},
                                        },
                                        "securityContext": {
                                            "allowPrivilegeEscalation": False,
                                            "readOnlyRootFilesystem": True,
                                            "runAsNonRoot": True,
                                            "runAsUser": 65532,
                                            "runAsGroup": 65532,
                                            "capabilities": {"drop": ["ALL"]},
                                        },
                                    }
                                ],
                            },
                        },
                    },
                }
            )
        return resources

    def _installation_snapshot(self) -> dict[str, Any]:
        jobs = self._get_list("job")
        pvcs = self._get_list("pvc")
        deployments = self._get_list("deployment")
        bootstrap = next(
            (item for item in jobs if item.get("metadata", {}).get("name") == "aiops-bootstrap"),
            None,
        )
        if bootstrap is None or not self._condition(bootstrap, "Complete"):
            raise ValueError("bootstrap Job is not Complete")
        if len(pvcs) != 6 or any(item.get("status", {}).get("phase") != "Bound" for item in pvcs):
            raise ValueError("exactly six release PVCs must be Bound")
        if not deployments or any(not self._condition(item, "Available") for item in deployments):
            raise ValueError("all release Deployments must be Available")
        return {
            "bootstrap": self._object_identity(bootstrap)
            | {"conditions": self._safe_conditions(bootstrap)},
            "pvcs": [self._object_identity(item) | {"phase": "Bound"} for item in pvcs],
            "deployments": [
                self._object_identity(item)
                | {
                    "available": True,
                    "conditions": self._safe_conditions(item),
                    "images": sorted(
                        container["image"]
                        for container in item["spec"]["template"]["spec"].get("containers", [])
                    ),
                }
                for item in deployments
            ],
        }

    def _event_snapshot(self) -> list[dict[str, Any]]:
        events = self._get_list("events")
        return [
            {
                "type": item.get("type"),
                "reason": item.get("reason"),
                "message": item.get("message"),
                "involved_object": {
                    "kind": item.get("involvedObject", {}).get("kind"),
                    "name": item.get("involvedObject", {}).get("name"),
                    "uid": item.get("involvedObject", {}).get("uid"),
                },
                "first_timestamp": item.get("firstTimestamp"),
                "last_timestamp": item.get("lastTimestamp"),
                "count": item.get("count"),
            }
            for item in events
        ]

    def _reapply_workload_snapshot(self) -> dict[str, Any]:
        deployments = self._get_list("deployment")
        daemonsets = self._get_list("daemonset")
        if not deployments or any(not self._condition(item, "Available") for item in deployments):
            raise ValueError("release Deployments did not converge after reapply")
        for item in daemonsets:
            status = item.get("status", {})
            if (
                status.get("desiredNumberScheduled") != status.get("numberReady")
                or status.get("observedGeneration") != item.get("metadata", {}).get("generation")
            ):
                raise ValueError("release DaemonSet did not converge after reapply")
        if not daemonsets:
            raise ValueError("release contains no converged DaemonSet after reapply")
        return {
            "deployments": [
                self._object_identity(item) | {"conditions": self._safe_conditions(item)}
                for item in deployments
            ],
            "daemonsets": [
                self._object_identity(item)
                | {
                    "generation": item["metadata"].get("generation"),
                    "observed_generation": item["status"].get("observedGeneration"),
                    "desired": item["status"].get("desiredNumberScheduled"),
                    "ready": item["status"].get("numberReady"),
                }
                for item in daemonsets
            ],
        }

    def _i01_diagnostics(self) -> list[Artifact]:
        artifacts: list[Artifact] = []
        commands = (
            (["kubectl", "-n", NAMESPACE, "describe", "job/aiops-bootstrap"], "bootstrap-describe.txt"),
            (["kubectl", "-n", NAMESPACE, "logs", "job/aiops-bootstrap"], "bootstrap-log.txt"),
            (["kubectl", "-n", NAMESPACE, "describe", "pvc"], "pvc-describe.txt"),
            (["kubectl", "-n", NAMESPACE, "describe", "deployment"], "deployment-describe.txt"),
            (["kubectl", "-n", NAMESPACE, "get", "events", "-o", "wide"], "events.txt"),
        )
        for command, name in commands:
            try:
                result = self._run(command, timeout=60)
                artifacts.append(
                    self.evidence.write_text(
                        "I01", name, self.evidence.command_text(result)
                    )
                )
            except Exception:
                continue
        return artifacts

    def _bootstrap_snapshot(self) -> dict[str, Any]:
        secrets = self._get_list("secret", *BOOTSTRAP_SECRETS)
        if {item.get("metadata", {}).get("name") for item in secrets} != set(BOOTSTRAP_SECRETS):
            raise ValueError("bootstrap Secret inventory is incomplete")
        safe_secrets: list[dict[str, Any]] = []
        for item in secrets:
            name = item["metadata"]["name"]
            data = item.get("data", {})
            if set(data) != BOOTSTRAP_SECRETS[name]:
                raise ValueError(f"bootstrap Secret {name} has an unexpected key inventory")
            safe_secrets.append(
                {
                    "name": name,
                    "uid": item["metadata"]["uid"],
                    "keys": sorted(data),
                    "value_sha256": {
                        key: hashlib.sha256(str(value).encode()).hexdigest()
                        for key, value in sorted(data.items())
                    },
                }
            )
        marker_result = self._require(
            self._run(
                [
                    "kubectl", "-n", NAMESPACE, "get", "configmap", "aiops-bootstrap-state",
                    "-o", "json",
                ],
                timeout=30,
            ),
            "read bootstrap completion marker",
        )
        marker = json.loads(marker_result.stdout)
        if marker.get("immutable") is not True:
            raise ValueError("bootstrap completion marker is not immutable")
        return {
            "secrets": sorted(safe_secrets, key=lambda item: item["name"]),
            "completion_marker": {
                "name": marker["metadata"]["name"],
                "uid": marker["metadata"]["uid"],
                "immutable": True,
                "data_sha256": _canonical_sha256(marker.get("data", {})),
            },
        }

    def _get_list(self, resource: str, *names: str) -> list[dict[str, Any]]:
        command = ["kubectl", "-n", NAMESPACE, "get", resource, *names, "-o", "json"]
        result = self._require(self._run(command, timeout=60), f"read {resource}")
        payload = json.loads(result.stdout)
        return payload.get("items", [payload])

    @staticmethod
    def _release_resources(release: Path) -> list[dict[str, Any]]:
        return [item for item in yaml.safe_load_all((release / "manifest.yaml").read_text()) if item]

    @staticmethod
    def _images(resources: list[dict[str, Any]]) -> set[str]:
        return {
            container["image"]
            for resource in resources
            if resource.get("kind") in {"Deployment", "DaemonSet", "StatefulSet", "Job"}
            for container in [
                *resource["spec"]["template"]["spec"].get("initContainers", []),
                *resource["spec"]["template"]["spec"].get("containers", []),
            ]
        }

    @staticmethod
    def _condition(resource: dict[str, Any], condition_type: str) -> bool:
        return any(
            item.get("type") == condition_type and item.get("status") == "True"
            for item in resource.get("status", {}).get("conditions", [])
        )

    @staticmethod
    def _safe_conditions(resource: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "type": item.get("type"),
                "status": item.get("status"),
                "reason": item.get("reason"),
                "last_transition_time": item.get("lastTransitionTime"),
            }
            for item in resource.get("status", {}).get("conditions", [])
        ]

    @staticmethod
    def _object_identity(resource: dict[str, Any]) -> dict[str, str]:
        metadata = resource["metadata"]
        return {"name": metadata["name"], "uid": metadata["uid"]}

    @staticmethod
    def _require(result: CommandResult, action: str) -> CommandResult:
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no detail"
            raise RuntimeError(f"{action} failed: {detail}")
        return result

    def _run(
        self,
        command: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: float = 900,
    ) -> CommandResult:
        result = self.commands.run(command, stdin=stdin, timeout=timeout)
        self._command_results.append(result)
        return result

    def _command_artifact(self, gate_id: str) -> Artifact:
        return self.evidence.write_json(
            gate_id,
            "command-index.json",
            self.evidence.contextualize(
                [
                    {
                        "command": list(result.command),
                        "exit_code": result.exit_code,
                        "started_at": result.started_at,
                        "completed_at": result.completed_at,
                        "duration_seconds": result.duration_seconds,
                        "stdout_bytes": len(result.stdout.encode()),
                        "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                        "stderr_bytes": len(result.stderr.encode()),
                        "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
                        "output_redacted": True,
                    }
                    for result in self._command_results
                ]
            ),
        )
