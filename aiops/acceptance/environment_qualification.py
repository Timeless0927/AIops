"""Repeatable Kubernetes environment preflight mechanics."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .command import CommandExecutor, CommandResult
from .evidence_files import sha256
from .environment_qualification_record import (
    attach_attestation as _attach_attestation,
    attestation_statement as _attestation_statement,
    inspect_record,
    resume_cleanup as _resume_cleanup,
    write_record,
)
from .freeze import verify_final_checksums
from .redaction import redact_text


NAMESPACE = "aiops-system"
ENVIRONMENT_QUALIFICATION_FORMAT_VERSION = 1
_QUALIFICATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


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
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.commands = commands
        self.sleep = sleep
        self.now = now
        self._command_results: list[CommandResult] = []

    def run(
        self,
        release: Path,
        *,
        candidate_sha256: str,
        kube_context: str,
        cluster_identity_sha256: str,
        namespace: str | None = None,
        before_apply: Callable[[], None] = lambda: None,
    ) -> EnvironmentPreflightResult:
        self._command_results = []
        namespace = namespace or "aiops-acceptance-preflight-" + candidate_sha256[:10]
        baseline: dict[str, Any] | None = None
        applied: CommandResult | None = None
        image_pull: list[dict[str, str]] | None = None
        cleanup: CommandResult | None = None
        namespace_effect_started = False
        try:
            resources = self._release_resources(release)
            images = self._images(resources)
            baseline = self._clean_baseline(
                resources,
                kube_context=kube_context,
                cluster_identity_sha256=cluster_identity_sha256,
                temporary_namespace=namespace,
            )
            before_apply()
            namespace_effect_started = True
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
            pvc = json.loads(self._require(self._run(
                ["kubectl", "get", "pvc", "capacity-probe", "-n", namespace, "-o", "json"],
                timeout=30,
            ), "inspect bound preflight PVC").stdout)
            if _storage_bytes(str(pvc.get("status", {}).get("capacity", {}).get("storage", ""))) < 32 * 1024**3:
                raise RuntimeError("bound preflight PVC capacity is smaller than 32Gi")
            policies = json.loads(self._require(self._run(
                ["kubectl", "get", "networkpolicy", "deny-all", "-n", namespace, "-o", "json"],
                timeout=30,
            ), "inspect NetworkPolicy preflight").stdout)
            policy_names = {
                str(item.get("metadata", {}).get("name", ""))
                for item in policies.get("items", [policies])
            }
            if "deny-all" not in policy_names:
                raise RuntimeError("NetworkPolicy preflight was not created")
            baseline["pvc_capacity"] = pvc["status"]["capacity"]["storage"]
            baseline["network_policy_probe"] = "created"
            image_pull = self._wait_for_image_pulls(
                namespace, images, set(baseline["nodes"])
            )
        except Exception as exc:
            failure: Exception | None = exc
        else:
            failure = None
        finally:
            if namespace_effect_started:
                cleanup = self._run(
                    [
                        "kubectl", "delete", "namespace", namespace,
                        "--wait=true", "--timeout=5m",
                    ],
                    timeout=330,
                )
                if (
                    cleanup.exit_code != 0
                    and "NotFound" not in cleanup.stderr
                    and failure is None
                ):
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
        temporary_namespace: str,
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
        clock = self._require(
            self._run(["kubectl", "get", "--raw=/version", "--v=8"], timeout=15),
            "read control-plane clock",
        )
        date_header = re.search(
            r"(?im)\bDate:\s*(?:\[([^\]]+)\]|([^\r\n]+))", clock.stderr,
        )
        if date_header is None:
            raise ValueError("control-plane response omitted its Date header")
        try:
            control_plane_time = parsedate_to_datetime(
                (date_header.group(1) or date_header.group(2)).strip().strip('"')
            ).astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ValueError("control-plane Date header is invalid") from exc
        control_plane_skew = abs((self.now() - control_plane_time).total_seconds())
        if control_plane_skew > 300:
            raise ValueError("control-plane clock skew exceeds 300s")
        for namespace in (NAMESPACE, "aiops-verification", temporary_namespace):
            result = self._run(
                ["kubectl", "get", "namespace", namespace, "-o", "name"], timeout=15
            )
            if result.exit_code == 0 or "NotFound" not in result.stderr:
                raise ValueError(f"clean baseline requires absent namespace {namespace}")
        cluster_scoped: list[str] = []
        for resource in resources:
            metadata = resource.get("metadata", {})
            if metadata.get("namespace") or resource.get("kind") == "Namespace":
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
        nodes = []
        heartbeat_skew_seconds: dict[str, float] = {}
        for item in node_payload.get("items", []):
            name = str(item.get("metadata", {}).get("name", ""))
            ready = next(
                (condition for condition in item.get("status", {}).get("conditions", [])
                 if condition.get("type") == "Ready"),
                None,
            )
            if (
                not name or item.get("spec", {}).get("unschedulable") is True
                or not isinstance(ready, dict) or ready.get("status") != "True"
            ):
                raise ValueError(f"clean baseline requires schedulable Ready node {name or '<unknown>'}")
            heartbeat = _parse_utc(str(ready.get("lastHeartbeatTime", "")))
            skew = abs((self.now() - heartbeat).total_seconds())
            if skew > 300:
                raise ValueError(f"node clock/heartbeat skew exceeds 300s for {name}")
            nodes.append(name)
            heartbeat_skew_seconds[name] = skew
        nodes.sort()
        if not nodes:
            raise ValueError("clean baseline contains no Kubernetes nodes")
        return {
            "kube_context": context,
            "cluster_identity_sha256": cluster_identity_sha256,
            "control_plane_clock_skew_seconds": control_plane_skew,
            "namespaces_absent": [NAMESPACE, "aiops-verification"],
            "cluster_resources_absent": sorted(cluster_scoped),
            "nodeport_30088_free": True,
            "nodes": nodes,
            "node_heartbeat_skew_seconds": heartbeat_skew_seconds,
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
                    image = image_by_name.get(status["name"], "")
                    digest = image.rpartition("@sha256:")[2]
                    if image_id and digest and digest in image_id:
                        observed[(node, image)] = image_id
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
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "deny-all", "namespace": namespace},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
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


class EnvironmentQualification:
    """Own repeatable, ledger-external environment qualification records."""

    def __init__(
        self,
        root: Path,
        *,
        commands: CommandExecutor,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        new_id: Callable[[], str] = lambda: str(uuid4()),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = root
        self.commands = commands
        self.now = now
        self.new_id = new_id
        self.sleep = sleep

    def qualify(
        self,
        freeze_root: Path,
        *,
        kube_context: str,
        cluster_identity_sha256: str,
        access_profile: str,
        ttl_seconds: int = 3600,
    ) -> Path:
        qualification_id = self.new_id()
        if not _QUALIFICATION_ID.fullmatch(qualification_id):
            raise ValueError("qualification_id contains unsupported characters")
        if access_profile not in {"http_nodeport", "https_ingress"}:
            raise ValueError("unsupported access profile")
        if _SHA256.fullmatch(cluster_identity_sha256) is None or not 60 <= ttl_seconds <= 86400:
            raise ValueError("qualification identity or TTL is invalid")
        freeze = _freeze_identity(freeze_root)
        release_path = Path(freeze.pop("pilot_release_path"))
        freeze.pop("acceptance_tool_path")
        path = self.root / qualification_id
        path.mkdir(parents=True, exist_ok=False)
        observed = self.now()
        namespace = "aiops-environment-" + hashlib.sha256(qualification_id.encode()).hexdigest()[:12]
        record: dict[str, Any] = {
            "format_version": ENVIRONMENT_QUALIFICATION_FORMAT_VERSION,
            "qualification_id": qualification_id,
            "freeze": freeze,
            "cluster": {
                "kube_context": kube_context,
                "identity_sha256": cluster_identity_sha256,
            },
            "access_profile": access_profile,
            "temporary_namespace": namespace,
            "operation": {
                "id": f"qualification:{qualification_id}:preflight",
                "status": "planned",
            },
            "facts": {},
            "cleanup": {
                "namespace_absent": True, "exit_code": None, "proof": "not_created",
            },
            "outcome": "running",
            "failure": None,
            "observed_at": _iso(observed),
            "expires_at": _iso(observed + timedelta(seconds=ttl_seconds)),
        }
        self._write_record(path, record)

        def bind_effect() -> None:
            record["operation"] = {
                **record["operation"], "status": "dispatched", "dispatched_at": _iso(self.now())
            }
            record["cleanup"] = {
                "namespace_absent": False, "exit_code": None, "proof": "pending",
            }
            self._write_record(path, record)

        with _extracted_release(release_path) as release:
            result = EnvironmentPreflight(
                commands=self.commands, sleep=self.sleep, now=self.now,
            ).run(
                release,
                candidate_sha256=str(freeze["product_sha256"]),
                kube_context=kube_context,
                cluster_identity_sha256=cluster_identity_sha256,
                namespace=namespace,
                before_apply=bind_effect,
            )
        record["facts"] = {
            **(result.baseline or {}),
            "exact_image_pulls": result.image_pull or [],
        }
        effect_dispatched = record["operation"]["status"] == "dispatched"
        cleanup_absent = bool(
            result.cleanup
            and (result.cleanup.exit_code == 0 or "NotFound" in result.cleanup.stderr)
        )
        cleanup_proof = (
            "not_created" if not effect_dispatched
            else "not_found" if result.cleanup and "NotFound" in result.cleanup.stderr
            else "deleted" if cleanup_absent else "unproved"
        )
        record["cleanup"] = {
            "namespace_absent": (
                not effect_dispatched
                or cleanup_absent
            ),
            "exit_code": result.cleanup.exit_code if result.cleanup else None,
            "proof": cleanup_proof,
        }
        record["operation"] = {**record["operation"], "status": "terminal"}
        record["outcome"] = (
            "passed" if result.failure is None and record["cleanup"]["namespace_absent"]
            else "environment_not_ready"
        )
        record["failure"] = (
            None if result.failure is None else redact_text(str(result.failure))[:2000]
        )
        self._write_record(path, record)
        return path

    inspect = staticmethod(inspect_record)

    def attestation_statement(self, path: Path, *, actor: str, note: str) -> dict[str, Any]:
        return _attestation_statement(path, actor=actor, note=note, now=self.now)

    @staticmethod
    def attach_attestation(path: Path, item: dict[str, Any]) -> None:
        _attach_attestation(path, item)

    def resume_cleanup(self, path: Path) -> dict[str, Any]:
        return _resume_cleanup(path, commands=self.commands)

    _write_record = staticmethod(write_record)


def _freeze_identity(root: Path) -> dict[str, Any]:
    verify_final_checksums(root)
    record_path = root / "freeze-record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    contracts = record.get("contracts", {})
    artifacts = record.get("artifacts", {})
    release = artifacts.get("pilot_release", {})
    tool = artifacts.get("acceptance_tool", {})
    if (
        contracts.get("environment_qualification_format_version")
        != ENVIRONMENT_QUALIFICATION_FORMAT_VERSION
        or contracts.get("evidence_format_version") != 3
        or not isinstance(contracts.get("gate_contract_revision"), str)
    ):
        raise ValueError("freeze record does not admit Environment Qualification v1")
    release_path = _frozen_artifact(root, release)
    tool_path = _frozen_artifact(root, tool)
    if record.get("release", {}).get("archive_sha256") != release.get("sha256"):
        raise ValueError("freeze product identity drifted")
    return {
        "record_sha256": sha256(record_path),
        "product_sha256": release["sha256"],
        "acceptance_tool_sha256": tool["sha256"],
        "gate_contract_revision": contracts["gate_contract_revision"],
        "evidence_format_version": contracts["evidence_format_version"],
        "environment_qualification_format_version": contracts[
            "environment_qualification_format_version"
        ],
        "pilot_release_path": str(release_path),
        "acceptance_tool_path": str(tool_path),
    }


def _frozen_artifact(root: Path, value: Any) -> Path:
    if not isinstance(value, dict) or _SHA256.fullmatch(str(value.get("sha256", ""))) is None:
        raise ValueError("freeze artifact identity is invalid")
    path = (root / str(value.get("path", ""))).resolve()
    if not path.is_relative_to(root.resolve()) or path.is_symlink() or not path.is_file():
        raise ValueError("freeze artifact path is invalid")
    if sha256(path) != value["sha256"]:
        raise ValueError("freeze artifact checksum mismatch")
    return path


class _extracted_release:
    def __init__(self, archive: Path) -> None:
        self.archive = archive
        self.temporary: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiops-environment-release-")
        root = Path(self.temporary.name)
        with tarfile.open(self.archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if (
                not members or sum(item.size for item in members) > 100 * 1024 * 1024
                or any(not (item.isfile() or item.isdir()) for item in members)
            ):
                raise ValueError("freeze release archive is unsafe")
            top = {Path(item.name).parts[0] for item in members if Path(item.name).parts}
            if len(top) != 1:
                raise ValueError("freeze release archive has an invalid root")
            bundle.extractall(root, filter="data")
        release = root / next(iter(top))
        if not (release / "manifest.yaml").is_file():
            raise ValueError("freeze release archive is missing manifest.yaml")
        return release

    def __exit__(self, *_args: object) -> None:
        if self.temporary is not None:
            self.temporary.cleanup()


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("node Ready heartbeat timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("node Ready heartbeat timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _storage_bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+)(Ki|Mi|Gi|Ti)", value)
    if match is None:
        raise ValueError("PVC capacity uses an unsupported quantity")
    return int(match.group(1)) * 1024 ** {"Ki": 1, "Mi": 2, "Gi": 3, "Ti": 4}[match.group(2)]
