"""Repeatable Kubernetes environment preflight mechanics."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .command import CommandExecutor, CommandResult


NAMESPACE = "aiops-system"


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class EnvironmentPreflightResult:
    namespace: str
    baseline: dict[str, Any] | None
    applied: CommandResult | None
    image_pull: list[dict[str, str]] | None
    cleanup: CommandResult | None
    commands: tuple[CommandResult, ...]
    failure: Exception | None


class EnvironmentPreflight:
    """Run and clean one exact environment preflight without owning a ledger."""

    def __init__(
        self,
        *,
        commands: CommandExecutor,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.commands = commands
        self.sleep = sleep
        self._command_results: list[CommandResult] = []

    def run(
        self,
        release: Path,
        *,
        candidate_sha256: str,
        kube_context: str,
        cluster_identity_sha256: str,
    ) -> EnvironmentPreflightResult:
        self._command_results = []
        namespace = "aiops-acceptance-preflight-" + candidate_sha256[:10]
        baseline: dict[str, Any] | None = None
        applied: CommandResult | None = None
        image_pull: list[dict[str, str]] | None = None
        cleanup: CommandResult | None = None
        namespace_created = False
        try:
            resources = self._release_resources(release)
            images = self._images(resources)
            baseline = self._clean_baseline(
                resources,
                kube_context=kube_context,
                cluster_identity_sha256=cluster_identity_sha256,
            )
            applied = self._require(
                self._run(
                    ["kubectl", "apply", "-f", "-"],
                    stdin=yaml.safe_dump_all(
                        self._preflight_resources(namespace, images), sort_keys=False
                    ),
                    timeout=120,
                ),
                "create preflight resources",
            )
            namespace_created = True
            self._require(
                self._run(
                    [
                        "kubectl", "wait", "--for=jsonpath={.status.phase}=Bound",
                        "pvc/capacity-probe", "-n", namespace, "--timeout=10m",
                    ],
                    timeout=660,
                ),
                "bind 32Gi preflight PVC",
            )
            image_pull = self._wait_for_image_pulls(
                namespace, images, set(baseline["nodes"])
            )
        except Exception as exc:
            failure: Exception | None = exc
        else:
            failure = None
        finally:
            if namespace_created:
                cleanup = self._run(
                    [
                        "kubectl", "delete", "namespace", namespace,
                        "--wait=true", "--timeout=5m",
                    ],
                    timeout=330,
                )
                if cleanup.exit_code != 0 and failure is None:
                    failure = RuntimeError("preflight namespace cleanup failed")
        return EnvironmentPreflightResult(
            namespace=namespace,
            baseline=baseline,
            applied=applied,
            image_pull=image_pull,
            cleanup=cleanup,
            commands=tuple(self._command_results),
            failure=failure,
        )

    def _clean_baseline(
        self,
        resources: list[dict[str, Any]],
        *,
        kube_context: str,
        cluster_identity_sha256: str,
    ) -> dict[str, Any]:
        context = self._require(
            self._run(["kubectl", "config", "current-context"], timeout=15),
            "read kube context",
        ).stdout.strip()
        if context != kube_context:
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
        if _canonical_sha256(identity) != cluster_identity_sha256:
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
                raise ValueError(
                    f"clean baseline contains release cluster resource {kind}/{name}"
                )
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
            if any(
                port.get("nodePort") == 30088
                for port in item.get("spec", {}).get("ports", [])
            )
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
            "cluster_identity_sha256": cluster_identity_sha256,
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
        observed: dict[tuple[str, str], str] = {}
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
            observed = {}
            nodes = {str(pod.get("spec", {}).get("nodeName", "")) for pod in pods}
            for pod in pods:
                node = str(pod.get("spec", {}).get("nodeName", ""))
                image_by_name = {
                    item["name"]: item["image"]
                    for item in pod.get("spec", {}).get("containers", [])
                }
                for status in pod.get("status", {}).get("containerStatuses", []):
                    image_id = str(status.get("imageID", ""))
                    if image_id:
                        observed[(node, image_by_name.get(status["name"], ""))] = image_id
            if nodes == expected_nodes and all(
                (node, image) in observed
                for node in expected_nodes
                for image in images
            ):
                return [
                    {
                        "node": node,
                        "image": image,
                        "image_id_sha256": hashlib.sha256(
                            observed[(node, image)].encode()
                        ).hexdigest(),
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
                image = str(
                    containers.get(status.get("name"), status.get("image", "unknown-image"))
                )
                if (node, image) in observed:
                    continue
                state = status.get("state", {})
                detail = state.get("waiting") or state.get("terminated") or {}
                reason = detail.get("reason") or (
                    "Running" if state.get("running") else "Pending"
                )
                failures.append({"node": node, "image": image, "reason": str(reason)})
        reported = {(item["node"], item["image"]) for item in failures}
        failures.extend(
            {"node": node, "image": image, "reason": "PodMissing"}
            for node in expected_nodes
            for image in images
            if (node, image) not in observed and (node, image) not in reported
        )
        failures.sort(key=lambda item: (item["node"], item["image"], item["reason"]))
        raise RuntimeError(
            "node image pull preflight did not converge: "
            + json.dumps(failures, sort_keys=True)
        )

    @staticmethod
    def _preflight_resources(
        namespace: str, images: set[str]
    ) -> list[dict[str, Any]]:
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
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "32Gi"}},
                },
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
                            "volumeMounts": [
                                {"name": "capacity", "mountPath": "/capacity"}
                            ],
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
                        {
                            "name": "capacity",
                            "persistentVolumeClaim": {"claimName": "capacity-probe"},
                        }
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
                                        "command": [
                                            "/__aiops_acceptance_image_pull_probe__"
                                        ],
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

    @staticmethod
    def _release_resources(release: Path) -> list[dict[str, Any]]:
        return [item for item in yaml.safe_load_all((release / "manifest.yaml").read_text()) if item]

    @staticmethod
    def _images(resources: list[dict[str, Any]]) -> set[str]:
        return {
            container["image"]
            for resource in resources
            for spec in (
                resource.get("spec", {}).get("template", {}).get("spec", {}),
                resource.get("spec", {}),
            )
            for key in ("initContainers", "containers")
            for container in spec.get(key, [])
            if "image" in container
        }

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
