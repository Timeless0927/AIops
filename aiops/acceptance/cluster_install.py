"""Clean Acceptance deployment qualification gates."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Sequence

from .cluster_identity import KubernetesClusterIdentitySource
from .command import CommandExecutor, CommandResult
from .environment_qualification import NAMESPACE
from .evidence import AcceptanceEvidence
from .evidence_types import Artifact
from .integration_support import fail_gate


BOOTSTRAP_SECRETS = {
    "aiops-runtime-secret": {
        "AIOPS_BOOTSTRAP_ADMIN_PASSWORD",
        "AIOPS_ALERTMANAGER_WEBHOOK_TOKEN",
    },
    "aiops-model-encryption": {"key"},
    "aiops-notification-encryption": {"key"},
    "aiops-change-encryption": {"key"},
}
_GENERATION_DIFF = re.compile(r"^([+-])(\s+)generation: ([0-9]+)$")


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

    def run_i01(self, release: Path) -> None:
        started_at = self.evidence.start_gate("I01")
        self._command_results = []
        artifacts: list[Artifact] = []
        try:
            if self.evidence.deployment_mode == "adopt_existing":
                observed = self.observe_existing(release)
                diff = observed["manifest_diff"]
                artifacts.extend([
                    self.evidence.write_json("I01", "adoption.json", {
                        "mode": "adopt_existing", "zero_apply": True,
                        "server_generation_only": observed["server_generation_only"],
                        "deployment_precondition_sha256": (
                            self.evidence.deployment_precondition_sha256
                        ),
                    }),
                    self.evidence.write_text(
                        "I01", "manifest-diff.txt", self.evidence.command_text(diff),
                    ),
                    self.evidence.write_json("I01", "objects.json", observed["objects"]),
                    self.evidence.write_json(
                        "I01", "configuration.json", observed["configuration"],
                    ),
                    self.evidence.write_json("I01", "bootstrap.json", observed["bootstrap"]),
                    self.evidence.write_json("I01", "workloads.json", observed["workloads"]),
                    self.evidence.write_json("I01", "events.json", observed["events"]),
                    self._command_artifact("I01"),
                ])
                self.evidence.record_gate(
                    "I01", "passed", artifacts, started_at=started_at,
                )
                return
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

    def observe_existing(self, release: Path) -> dict[str, Any]:
        """Read and validate one existing deployment without mutating it."""
        self._command_results = []
        context, identity = KubernetesClusterIdentitySource(self.commands).read()
        if (
            context != self.evidence.kube_context
            or identity != self.evidence.cluster_identity_sha256
        ):
            raise ValueError("existing deployment Cluster identity drifted")
        diff = self._run(["kubectl", "diff", "-k", str(release)], timeout=300)
        generation_only = _server_generation_only(diff)
        if diff.exit_code not in {0, 1} or (diff.exit_code == 1 and not generation_only):
            self._require(diff, "verify existing release manifest")
        return {
            "cluster_identity_sha256": identity,
            "manifest_diff": diff,
            "server_generation_only": generation_only,
            "objects": self._installation_snapshot(),
            "configuration": self._adoption_configuration_snapshot(),
            "bootstrap": self._bootstrap_snapshot(),
            "workloads": self._workload_snapshot(),
            "events": self._event_snapshot(),
        }

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
            workloads = self._workload_snapshot()
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

    def _workload_snapshot(self) -> dict[str, Any]:
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

    def _adoption_configuration_snapshot(self) -> dict[str, Any]:
        result = self._require(
            self._run(
                ["kubectl", "get", "services", "--all-namespaces", "-o", "json"],
                timeout=60,
            ),
            "inspect NodePort owner",
        )
        services = json.loads(result.stdout).get("items", [])
        owners = [
            item
            for item in services
            if any(
                port.get("nodePort") == 30088
                for port in item.get("spec", {}).get("ports", [])
            )
        ]
        if len(owners) != 1 or (
            owners[0].get("metadata", {}).get("namespace"),
            owners[0].get("metadata", {}).get("name"),
        ) != (NAMESPACE, "aiops-console"):
            raise ValueError("NodePort 30088 owner is not aiops-system/aiops-console")
        owner = owners[0]
        configmaps = self._get_list("configmap")
        if not configmaps:
            raise ValueError("release ConfigMap inventory is empty")
        return {
            "nodeport_owner": {
                "namespace": NAMESPACE,
                **self._object_identity(owner),
                "spec_sha256": _canonical_sha256(owner.get("spec", {})),
            },
            "configmaps": sorted(
                [
                    self._object_identity(item)
                    | {
                        "immutable": item.get("immutable", False),
                        "content_sha256": _canonical_sha256({
                            "data": item.get("data", {}),
                            "binary_data": item.get("binaryData", {}),
                        }),
                    }
                    for item in configmaps
                ],
                key=lambda item: item["name"],
            ),
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


def _server_generation_only(result: CommandResult) -> bool:
    if result.exit_code != 1 or result.stderr.strip():
        return False
    lines = result.stdout.splitlines()
    if (
        not any(line.startswith("diff -u -N ") for line in lines)
        or sum(line.startswith("--- ") for line in lines) == 0
        or sum(line.startswith("--- ") for line in lines)
        != sum(line.startswith("+++ ") for line in lines)
    ):
        return False
    changes = [
        line for line in lines
        if line.startswith(("+", "-")) and not line.startswith(("+++ ", "--- "))
    ]
    if not changes or len(changes) % 2:
        return False
    for removed, added in zip(changes[::2], changes[1::2], strict=True):
        old = _GENERATION_DIFF.fullmatch(removed)
        new = _GENERATION_DIFF.fullmatch(added)
        if (
            old is None or new is None or old.group(1) != "-" or new.group(1) != "+"
            or old.group(2) != new.group(2) or int(new.group(3)) != int(old.group(3)) + 1
        ):
            return False
    return True
