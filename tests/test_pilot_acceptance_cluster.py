from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from aiops.acceptance.cluster_install import ClusterInstallRunner, InputRequired
from aiops.acceptance.command import CommandResult
from aiops.acceptance.evidence import A01_GATE_SEQUENCE, AcceptanceEvidence, GateFailed


IMAGE = "registry.example.test/aiops/gateway@sha256:" + "1" * 64
SECRET_DATA = {
    "aiops-runtime-secret": {
        "AIOPS_BOOTSTRAP_ADMIN_PASSWORD": "YWRtaW4tcGFzcw==",
        "AIOPS_ALERTMANAGER_WEBHOOK_TOKEN": "d2ViaG9vay10b2tlbg==",
    },
    "aiops-model-encryption": {"key": "bW9kZWwta2V5"},
    "aiops-notification-encryption": {"key": "bm90aWZpY2F0aW9uLWtleQ=="},
    "aiops-change-encryption": {"key": "Y2hhbmdlLWtleQ=="},
}


def _identity() -> tuple[dict, str]:
    config = {
        "clusters": [
            {
                "name": "clean",
                "cluster": {
                    "server": "https://10.0.0.1:6443",
                    "certificate-authority-data": "public-ca-data",
                },
            }
        ]
    }
    identity = {
        "server": config["clusters"][0]["cluster"]["server"],
        "certificate_authority_data": "public-ca-data",
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return config, digest


def _evidence(tmp_path: Path) -> AcceptanceEvidence:
    _, cluster_digest = _identity()
    return AcceptanceEvidence.create(
        tmp_path / "acceptance",
        acceptance_id="v0.1.0-cluster-test",
        release_version="v0.1.0",
        release_sha256="a" * 64,
        kube_context="clean",
        cluster_identity_sha256=cluster_digest,
        access_profile="http_nodeport",
        now=lambda: "2026-07-14T01:02:03Z",
        attestation_verifier=lambda _item: None,
    )


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "release"
    release.mkdir()
    resources = [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "aiops-change-executor"},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "aiops-gateway", "namespace": "aiops-system"},
            "spec": {"template": {"spec": {"containers": [{"name": "gateway", "image": IMAGE}]}}},
        },
    ]
    (release / "manifest.yaml").write_text(
        "---\n".join(yaml.safe_dump(resource) for resource in resources), encoding="utf-8"
    )
    return release


def _advance(evidence: AcceptanceEvidence, gate_id: str) -> None:
    for predecessor in A01_GATE_SEQUENCE[: A01_GATE_SEQUENCE.index(gate_id)]:
        evidence.record_gate(
            predecessor,
            "not_applicable" if predecessor == "I04" else "passed",
            [],
        )


class PreflightCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.config, _ = _identity()

    def run(self, command, *, stdin=None, **_kwargs) -> CommandResult:
        command = tuple(command)
        self.calls.append((command, stdin))
        if command == ("kubectl", "config", "current-context"):
            return CommandResult(command, 0, "clean\n", "", 0.1)
        if command[:4] == ("kubectl", "config", "view", "--minify"):
            return CommandResult(command, 0, json.dumps(self.config), "", 0.1)
        if command[:3] == ("kubectl", "get", "namespace"):
            return CommandResult(command, 1, "", "NotFound", 0.1)
        if command[:3] == ("kubectl", "get", "clusterrole"):
            return CommandResult(command, 1, "", "NotFound", 0.1)
        if command[:4] == ("kubectl", "get", "services", "--all-namespaces"):
            return CommandResult(command, 0, json.dumps({"items": []}), "", 0.1)
        if command[:3] == ("kubectl", "get", "storageclass"):
            storage = {
                "items": [
                    {
                        "metadata": {
                            "name": "standard",
                            "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                        },
                        "provisioner": "example.test/dynamic",
                        "volumeBindingMode": "WaitForFirstConsumer",
                    }
                ]
            }
            return CommandResult(command, 0, json.dumps(storage), "", 0.1)
        if command[:3] == ("kubectl", "get", "nodes"):
            return CommandResult(
                command,
                0,
                json.dumps({"items": [{"metadata": {"name": "node-1"}}]}),
                "",
                0.1,
            )
        if command[:3] == ("kubectl", "apply", "-f"):
            assert stdin and "32Gi" in stdin and IMAGE in stdin
            return CommandResult(command, 0, "resources created", "", 0.2)
        if command[:3] == ("kubectl", "wait", "--for=jsonpath={.status.phase}=Bound"):
            return CommandResult(command, 0, "pvc bound", "", 0.2)
        if command[:3] == ("kubectl", "get", "pods"):
            pod = {
                "metadata": {"name": "pull-gateway"},
                "spec": {"nodeName": "node-1", "containers": [{"name": "image", "image": IMAGE}]},
                "status": {"containerStatuses": [{"name": "image", "imageID": IMAGE}]},
            }
            return CommandResult(command, 0, json.dumps({"items": [pod]}), "", 0.1)
        if command[:3] == ("kubectl", "delete", "namespace"):
            return CommandResult(command, 0, "deleted", "", 0.1)
        raise AssertionError(command)


def _attest(evidence: AcceptanceEvidence, gate_id: str) -> None:
    statement = evidence.attestation_statement(
        actor="operator@example.test",
        role="platform_operator",
        gate_ids=[gate_id],
        conclusion="passed",
        note="observed the required manual boundary",
    )
    evidence.append_attestation(
        statement,
        signature="signature",
        public_key="ssh-ed25519 AAAATEST operator@example.test",
        fingerprint="SHA256:test",
    )


def test_p03_requires_capacity_and_cni_attestation_before_cluster_mutation(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "P03")
    commands = PreflightCommands()
    with pytest.raises(InputRequired, match="P03"):
        ClusterInstallRunner(evidence=evidence, commands=commands, sleep=lambda _seconds: None).run_p03(
            _release(tmp_path)
        )
    assert commands.calls == []
    assert "P03" not in json.loads(evidence.manifest_path.read_text())["gates"]


def test_p03_proves_clean_baseline_storage_nodeport_and_image_pull(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "P03")
    _attest(evidence, "P03")
    commands = PreflightCommands()

    ClusterInstallRunner(evidence=evidence, commands=commands, sleep=lambda _seconds: None).run_p03(
        _release(tmp_path)
    )

    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["P03"][0]["status"] == "passed"
    command_index = json.loads(
        (evidence.root / "00-package/P03-attempt-1/command-index.json").read_text()
    )
    assert command_index["release_sha256"] == "a" * 64
    assert command_index["kube_context"] == "clean"
    assert len(command_index["result"]) == len(commands.calls)
    assert all(item["output_redacted"] is True for item in command_index["result"])
    applied = next(stdin for command, stdin in commands.calls if command[:3] == ("kubectl", "apply", "-f"))
    probes = [
        container
        for resource in yaml.safe_load_all(applied)
        if resource and resource.get("kind") in {"Pod", "DaemonSet"}
        for container in (
            resource["spec"]["containers"]
            if resource["kind"] == "Pod"
            else resource["spec"]["template"]["spec"]["containers"]
        )
    ]
    assert probes and all(
        container["securityContext"]["runAsUser"] == 65532
        and container["securityContext"]["runAsGroup"] == 65532
        for container in probes
    )
    assert commands.calls[-1][0][:3] == ("kubectl", "delete", "namespace")
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in evidence.root.rglob("*")
        if path.is_file()
    )
    assert "public-ca-data" not in persisted
    assert "https://10.0.0.1:6443" not in persisted


class PendingProbeCommands(PreflightCommands):
    def run(self, command, *, stdin=None, **kwargs) -> CommandResult:
        command = tuple(command)
        if command[:3] == ("kubectl", "get", "pods"):
            self.calls.append((command, stdin))
            pod = {
                "spec": {"containers": [{"name": "image", "image": IMAGE}]},
                "status": {"phase": "Pending"},
            }
            return CommandResult(command, 0, json.dumps({"items": [pod]}), "", 0.1)
        return super().run(command, stdin=stdin, **kwargs)


class MissingProbeCommands(PreflightCommands):
    def run(self, command, *, stdin=None, **kwargs) -> CommandResult:
        command = tuple(command)
        if command[:3] == ("kubectl", "get", "pods"):
            self.calls.append((command, stdin))
            return CommandResult(command, 0, '{"items": []}', "", 0.1)
        return super().run(command, stdin=stdin, **kwargs)


class PartialProbeCommands(PreflightCommands):
    def run(self, command, *, stdin=None, **kwargs) -> CommandResult:
        command = tuple(command)
        if command[:3] == ("kubectl", "get", "nodes"):
            self.calls.append((command, stdin))
            nodes = [{"metadata": {"name": name}} for name in ("node-1", "node-2")]
            return CommandResult(command, 0, json.dumps({"items": nodes}), "", 0.1)
        if command[:3] == ("kubectl", "get", "pods"):
            self.calls.append((command, stdin))
            pods = [
                {
                    "spec": {"nodeName": "node-1", "containers": [{"name": "image", "image": IMAGE}]},
                    "status": {"containerStatuses": [{"name": "image", "imageID": IMAGE, "state": {"terminated": {"reason": "ContainerCannotRun"}}}]},
                },
                {
                    "spec": {"nodeName": "node-2", "containers": [{"name": "image", "image": IMAGE}]},
                    "status": {"containerStatuses": [{"name": "image", "state": {"waiting": {"reason": "ImagePullBackOff"}}}]},
                },
            ]
            return CommandResult(command, 0, json.dumps({"items": pods}), "", 0.1)
        return super().run(command, stdin=stdin, **kwargs)


def test_p03_failure_identifies_unscheduled_image_without_container_status(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "P03")
    _attest(evidence, "P03")
    with pytest.raises(GateFailed, match='"node": "unscheduled"') as failure:
        ClusterInstallRunner(
            evidence=evidence,
            commands=PendingProbeCommands(),
            sleep=lambda _seconds: None,
        ).run_p03(_release(tmp_path))
    assert f'"image": "{IMAGE}"' in str(failure.value)
    assert '"reason": "Pending"' in str(failure.value)


def test_p03_failure_identifies_missing_node_image_pair(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "P03")
    _attest(evidence, "P03")
    with pytest.raises(GateFailed, match='"reason": "PodMissing"') as failure:
        ClusterInstallRunner(
            evidence=evidence,
            commands=MissingProbeCommands(),
            sleep=lambda _seconds: None,
        ).run_p03(_release(tmp_path))
    assert '"node": "node-1"' in str(failure.value)
    assert f'"image": "{IMAGE}"' in str(failure.value)


def test_p03_failure_excludes_node_image_pair_that_already_pulled(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "P03")
    _attest(evidence, "P03")
    with pytest.raises(GateFailed, match="ImagePullBackOff") as failure:
        ClusterInstallRunner(
            evidence=evidence,
            commands=PartialProbeCommands(),
            sleep=lambda _seconds: None,
        ).run_p03(_release(tmp_path))
    assert '"node": "node-2"' in str(failure.value)
    assert '"node": "node-1"' not in str(failure.value)


class InstallCommands:
    def __init__(self) -> None:
        self.secret_reads = 0
        self.calls: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs) -> CommandResult:
        command = tuple(command)
        self.calls.append(command)
        if command[:3] == ("kubectl", "apply", "-k"):
            return CommandResult(command, 0, "applied", "", 0.2)
        if command[:2] == ("kubectl", "wait"):
            return CommandResult(command, 0, "condition met", "", 0.2)
        if command[:3] == ("kubectl", "rollout", "status"):
            return CommandResult(command, 0, "daemonset rolled out", "", 0.2)
        if command[:4] == ("kubectl", "-n", "aiops-system", "get"):
            resource = command[4]
            if resource == "job":
                body = {"items": [{"metadata": {"name": "aiops-bootstrap", "uid": "job-uid"}, "status": {"conditions": [{"type": "Complete", "status": "True"}]}}]}
            elif resource == "pvc":
                body = {"items": [{"metadata": {"name": f"pvc-{i}", "uid": f"pvc-{i}"}, "status": {"phase": "Bound"}} for i in range(6)]}
            elif resource == "deployment":
                body = {"items": [{"metadata": {"name": "aiops-gateway", "uid": "deploy-uid"}, "spec": {"template": {"spec": {"containers": [{"name": "gateway", "image": IMAGE}]}}}, "status": {"conditions": [{"type": "Available", "status": "True"}]}}]}
            elif resource == "daemonset":
                body = {"items": [{"metadata": {"name": "aiops-alloy", "uid": "daemonset-uid", "generation": 1}, "status": {"observedGeneration": 1, "desiredNumberScheduled": 1, "numberReady": 1}}]}
            elif resource == "secret":
                self.secret_reads += 1
                body = {"items": [{"metadata": {"name": name, "uid": f"uid-{name}"}, "data": data} for name, data in SECRET_DATA.items()]}
            elif resource == "configmap":
                body = {"metadata": {"name": "aiops-bootstrap-state", "uid": "marker-uid"}, "immutable": True, "data": {"status": "complete"}}
            elif resource == "events":
                body = {"items": []}
            else:
                raise AssertionError(command)
            return CommandResult(command, 0, json.dumps(body), "", 0.1)
        raise AssertionError(command)


def test_i01_and_i02_install_then_reapply_without_persisting_secret_values(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _advance(evidence, "I01")
    commands = InstallCommands()
    runner = ClusterInstallRunner(evidence=evidence, commands=commands, sleep=lambda _seconds: None)
    release = _release(tmp_path)

    runner.run_i01(release)
    runner.run_i02(release)

    manifest = json.loads(evidence.manifest_path.read_text())
    assert manifest["gates"]["I01"][0]["status"] == "passed"
    assert manifest["gates"]["I02"][0]["status"] == "passed"
    assert (evidence.root / "01-install/I01-attempt-1/command-index.json").is_file()
    assert (evidence.root / "01-install/I02-attempt-1/command-index.json").is_file()
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in evidence.root.rglob("*")
        if path.is_file()
    )
    for encoded in {value for data in SECRET_DATA.values() for value in data.values()}:
        assert encoded not in persisted
    assert commands.secret_reads == 2
    daemonset_wait = next(
        command for command in commands.calls
        if command[:4] == ("kubectl", "rollout", "status", "daemonset")
    )
    assert "--all" not in daemonset_wait
