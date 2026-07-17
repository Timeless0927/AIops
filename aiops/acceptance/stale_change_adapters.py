"""Fixed Kubernetes metadata drift Adapter for R06."""

from __future__ import annotations

import json
import re

from .command import CommandExecutor, CommandResult
from .recovery import RecoveryScope, canonical_sha256
from .stale_change import ANNOTATION_PATH, TARGET


ANNOTATION_KEY = "aiops.dev/r06-stale-probe"


class KubectlStaleChangeAdapter:
    """Changes one top-level verification annotation and nothing else."""

    def __init__(
        self,
        commands: CommandExecutor,
        *,
        kube_context: str,
        candidate_sha256: str,
        release_inventory_sha256: str,
        cluster_identity_sha256: str,
    ) -> None:
        if not all((
            kube_context, candidate_sha256, release_inventory_sha256,
            cluster_identity_sha256,
        )):
            raise ValueError("R06 Kubernetes Adapter requires exact acceptance identities")
        self.commands = commands
        self.kube_context = kube_context
        self.candidate_sha256 = candidate_sha256
        self.release_inventory_sha256 = release_inventory_sha256
        self.cluster_identity_sha256 = cluster_identity_sha256

    def snapshot(self, scope: RecoveryScope) -> dict[str, object]:
        self._require_scope(scope)
        resource = self._json(self._run([
            "get", "deployment", TARGET["name"], "-o", "json",
        ]), "read exact verification Deployment")
        metadata = resource.get("metadata")
        spec = resource.get("spec")
        template = spec.get("template") if isinstance(spec, dict) else None
        annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
        if (
            resource.get("apiVersion") != TARGET["api_version"]
            or resource.get("kind") != TARGET["kind"]
            or not isinstance(metadata, dict)
            or metadata.get("namespace") != TARGET["namespace"]
            or metadata.get("name") != TARGET["name"]
            or not metadata.get("uid")
            or not metadata.get("resourceVersion")
            or not isinstance(template, dict)
        ):
            raise ValueError("R06 verification Deployment identity is invalid")
        return {
            "identity": {
                "candidate_sha256": scope.candidate_sha256,
                "release_inventory_sha256": scope.release_inventory_sha256,
                "kube_context": scope.kube_context,
                "cluster_identity_sha256": scope.cluster_identity_sha256,
            },
            "target": {
                **TARGET,
                "uid": str(metadata["uid"]),
                "resource_version": str(metadata["resourceVersion"]),
            },
            "annotation_path": ANNOTATION_PATH,
            "annotation_value": annotations.get(ANNOTATION_KEY)
            if isinstance(annotations, dict) else None,
            "pod_template_sha256": canonical_sha256(template),
        }

    def drift_metadata(
        self, scope: RecoveryScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object]:
        self._require_operation(before, operation_id)
        current = self.snapshot(scope)
        if current != before:
            raise ValueError("R06 verification target changed before Operator drift")
        target = before["target"]
        assert isinstance(target, dict)
        value = f"operator-drift:{operation_id}"
        patch = json.dumps({
            "metadata": {
                "resourceVersion": target["resource_version"],
                "annotations": {ANNOTATION_KEY: value},
            },
        }, sort_keys=True, separators=(",", ":"))
        self._require(self._run([
            "patch", "deployment", TARGET["name"], "--type=merge", "-p", patch,
        ]), "apply fixed verification metadata drift")
        after = self.snapshot(scope)
        return self._result(before, after, operation_id, value)

    def reconcile_metadata_drift(
        self, scope: RecoveryScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None:
        self._require_operation(before, operation_id)
        after = self.snapshot(scope)
        value = f"operator-drift:{operation_id}"
        if after.get("annotation_value") != value:
            return None
        return self._result(before, after, operation_id, value)

    def _require_scope(self, scope: RecoveryScope) -> None:
        if (
            scope.kube_context != self.kube_context
            or scope.candidate_sha256 != self.candidate_sha256
            or scope.release_inventory_sha256 != self.release_inventory_sha256
            or scope.cluster_identity_sha256 != self.cluster_identity_sha256
        ):
            raise ValueError("R06 Kubernetes Adapter identity does not match the ledger")

    @staticmethod
    def _require_operation(before: dict[str, object], operation_id: str) -> None:
        target = before.get("target")
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._:-]{0,299}", operation_id) is None
            or not isinstance(target, dict)
            or {key: target.get(key) for key in TARGET} != TARGET
            or not target.get("uid")
            or not target.get("resource_version")
            or before.get("annotation_path") != ANNOTATION_PATH
            or not before.get("pod_template_sha256")
        ):
            raise ValueError("R06 fixed Operator drift request is invalid")

    @staticmethod
    def _result(
        before: dict[str, object], after: dict[str, object],
        operation_id: str, value: str,
    ) -> dict[str, object]:
        old = before.get("target")
        new = after.get("target")
        if (
            not isinstance(old, dict) or not isinstance(new, dict)
            or new.get("uid") != old.get("uid")
            or new.get("resource_version") == old.get("resource_version")
            or after.get("annotation_value") != value
            or after.get("pod_template_sha256") != before.get("pod_template_sha256")
        ):
            raise ValueError("R06 Operator drift changed more than object metadata")
        return {
            "status": "succeeded", "operation_id": operation_id,
            "target": TARGET, "annotation_path": ANNOTATION_PATH,
            "annotation_value": value,
            "before": old, "after": new,
            "pod_template_sha256": after["pod_template_sha256"],
        }

    def _run(self, args: list[str]) -> CommandResult:
        return self.commands.run([
            "kubectl", "--context", self.kube_context,
            "-n", str(TARGET["namespace"]), *args,
        ], timeout=120)

    @staticmethod
    def _require(result: CommandResult, action: str) -> None:
        if result.exit_code != 0:
            raise RuntimeError(f"R06 kubectl failed to {action}")

    @classmethod
    def _json(cls, result: CommandResult, action: str) -> dict[str, object]:
        cls._require(result, action)
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise RuntimeError(f"R06 kubectl returned invalid JSON for {action}")
        return value
