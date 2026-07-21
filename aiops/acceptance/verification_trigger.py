"""V01 Console provisioning and unique verification Job trigger."""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from .command import CommandExecutor
from .evidence import AcceptanceEvidence
from .evidence_types import Artifact, GateExecution
from .integration_support import fail_gate
from .run_one_decisions import valid_run_id
from .verification_run import parse_verification_run
from .web_gates import BrowserResult


@dataclass(frozen=True)
class V01Inputs:
    release_root: Path
    admin_username: str
    admin_password: str
    sre_username: str
    sre_password: str


class V01Console(Protocol):
    def provision_v01(
        self,
        *,
        base_url: str,
        admin_username: str,
        admin_password: str,
        sre_username: str,
        sre_password: str,
    ) -> BrowserResult: ...


class RunSignalProbe(Protocol):
    def probe_v02(self, run_id: str) -> dict[str, object]: ...


class UserSession(Protocol):
    def request(self, method: str, path: str, **kwargs: Any) -> Any: ...


class VerificationTriggerGateRunner:
    def __init__(
        self,
        *,
        evidence: AcceptanceEvidence,
        commands: CommandExecutor,
        console: V01Console,
        base_url: str,
        user: UserSession | None = None,
        telemetry: RunSignalProbe | None = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.evidence = evidence
        self.commands = commands
        self.console = console
        self.base_url = base_url
        self.user = user
        self.telemetry = telemetry
        self.now = now
        self.monotonic = monotonic
        self.sleep = sleep

    def run_v01(self, inputs: V01Inputs) -> dict[str, object]:
        started_at = self.evidence.start_gate("V01")
        execution = self.evidence.resume_gate("V01")
        artifacts: list[Artifact] = []
        secrets = (inputs.admin_password, inputs.sre_password)
        try:
            base = inputs.release_root / "verification/base"
            run = inputs.release_root / "verification/run"
            fixture_operation_id = f"v01-fixture:{execution.execution_id}"
            self.evidence.bind_operation(
                "V01", kind="verification_fixture", operation_id=fixture_operation_id
            )
            apply_base = self.commands.run(
                ["kubectl", "apply", "-k", str(base)], timeout=60
            )
            wait_base = self.commands.run(
                [
                    "kubectl", "wait", "-n", "aiops-verification",
                    "--for=condition=Available", "deployment/verification-api",
                    "--timeout=2m",
                ],
                timeout=150,
            )
            preflight = self.commands.run(
                [
                    "kubectl", "get", "job/verification-trigger",
                    "-n", "aiops-verification", "--ignore-not-found", "-o", "json",
                ],
                timeout=30,
            )
            setup_results = (apply_base, wait_base, preflight)
            if any(result.exit_code != 0 for result in setup_results):
                raise RuntimeError("verification fixture setup failed")
            self.evidence.reconcile_operation(
                "V01",
                operation_id=fixture_operation_id,
                outcome="succeeded",
                public_fact={
                    "operation_id": fixture_operation_id,
                    "deployment": "aiops-verification/verification-api",
                    "available": True,
                },
            )
            if preflight.stdout.strip():
                raise ValueError("verification trigger Job already exists before V01")
            browser = self.console.provision_v01(
                base_url=self.base_url,
                admin_username=inputs.admin_username,
                admin_password=inputs.admin_password,
                sre_username=inputs.sre_username,
                sre_password=inputs.sre_password,
            )
            self._verify_console(browser)
            artifacts.extend([
                self.evidence.write_text(
                    "V01",
                    "fixture-setup-commands.txt",
                    "\n".join(
                        self.evidence.command_text(result) for result in setup_results
                    ),
                    known_secrets=secrets,
                ),
                self.evidence.write_json(
                    "V01", "console-provision.json", browser.summary,
                    known_secrets=secrets,
                ),
            ])
            artifacts.extend(
                self.evidence.write_bytes("V01", name, value)
                for name, value in sorted(browser.screenshots.items())
            )
            operation_id = f"v01-trigger:{execution.execution_id}"
            self.evidence.bind_operation(
                "V01", kind="verification_job", operation_id=operation_id
            )
            artifacts.append(self.evidence.write_json("V01", "trigger-intent.json", {
                "operation_id": operation_id,
                "namespace": "aiops-verification",
                "job_name": "verification-trigger",
            }))
            create_run = self.commands.run(
                ["kubectl", "create", "-k", str(run)], timeout=60
            )
            wait_run = self.commands.run(
                [
                    "kubectl", "wait", "-n", "aiops-verification",
                    "--for=condition=complete", "job/verification-trigger",
                    "--timeout=2m",
                ],
                timeout=150,
            )
            job_result = self.commands.run(
                [
                    "kubectl", "get", "job/verification-trigger",
                    "-n", "aiops-verification", "-o", "json",
                ],
                timeout=30,
            )
            log_result = self.commands.run(
                [
                    "kubectl", "logs", "job/verification-trigger",
                    "-n", "aiops-verification",
                ],
                timeout=30,
            )
            trigger_results = (create_run, wait_run, job_result, log_result)
            if any(result.exit_code != 0 for result in trigger_results):
                raise RuntimeError("verification trigger create or observation failed")
            job, trigger, run_id, trigger_started_at = parse_verification_run(
                job_result.stdout, log_result.stdout
            )
            self.evidence.reconcile_operation(
                "V01",
                operation_id=operation_id,
                outcome="succeeded",
                public_fact={
                    "operation_id": operation_id,
                    "job_uid": run_id,
                    "job_name": "verification-trigger",
                    "namespace": "aiops-verification",
                    "succeeded": 1,
                },
            )
            self.evidence.reconcile_operation(
                "V01",
                operation_id=execution.execution_id,
                outcome="succeeded",
                public_fact={"run_id": run_id, "terminal": True},
            )
            artifacts.extend([
                self.evidence.write_text(
                    "V01",
                    "trigger-commands.txt",
                    "\n".join(
                        self.evidence.command_text(result) for result in trigger_results
                    ),
                    known_secrets=secrets,
                ),
                self.evidence.write_json(
                    "V01",
                    "run.json",
                    {
                        "operation_id": operation_id,
                        "run_id": run_id,
                        "trigger_started_at": trigger_started_at,
                        "job": job,
                        "trigger": trigger,
                    },
                ),
            ])
            self.evidence.record_gate("V01", "passed", artifacts, started_at=started_at)
            return {
                "run_id": run_id,
                "trigger_started_at": trigger_started_at,
                **browser.summary,
            }
        except Exception as exc:
            fail_gate(self.evidence, "V01", artifacts, exc, secrets, started_at)

    def resume_v01(self) -> dict[str, object]:
        execution = self.evidence.resume_gate("V01")
        artifacts = list(execution.artifacts)
        try:
            operations = [
                item for item in execution.operations
                if item.get("kind") == "verification_job"
            ]
            if len(operations) != 1:
                raise ValueError("V01 interrupted trigger identity is unprovable")
            unresolved = self._unresolved_operations(execution)
            if any(
                item.get("kind") not in {"gate_execution", "verification_job"}
                for item in unresolved
            ):
                raise ValueError("V01 interrupted Console mutation is unprovable")
            operation_id = operations[0]["operation_id"]
            intent = self._artifact_json(artifacts, "trigger-intent.json")
            if intent != {
                "operation_id": operation_id,
                "namespace": "aiops-verification",
                "job_name": "verification-trigger",
            }:
                raise ValueError("V01 interrupted trigger does not match its durable intent")
            wait_result = self.commands.run(
                [
                    "kubectl", "wait", "-n", "aiops-verification",
                    "--for=condition=complete", "job/verification-trigger",
                    "--timeout=2m",
                ],
                timeout=150,
            )
            job_result = self.commands.run(
                [
                    "kubectl", "get", "job/verification-trigger",
                    "-n", "aiops-verification", "-o", "json",
                ],
                timeout=30,
            )
            log_result = self.commands.run(
                [
                    "kubectl", "logs", "job/verification-trigger",
                    "-n", "aiops-verification",
                ],
                timeout=30,
            )
            if any(
                result.exit_code != 0 for result in (wait_result, job_result, log_result)
            ):
                raise ValueError("V01 interrupted trigger has no terminal public Job facts")
            job, trigger, run_id, trigger_started_at = parse_verification_run(
                job_result.stdout, log_result.stdout
            )
            self._reconcile_unresolved(
                "V01",
                execution,
                outcome="succeeded",
                public_fact={"run_id": run_id, "terminal": True},
            )
            artifacts.append(self.evidence.write_json("V01", "run.json", {
                "operation_id": operation_id,
                "run_id": run_id,
                "trigger_started_at": trigger_started_at,
                "job": job,
                "trigger": trigger,
            }))
            self.evidence.record_gate(
                "V01", "passed", artifacts, started_at=execution.started_at
            )
            summary = self._artifact_json(artifacts, "console-provision.json")
            return {
                "run_id": run_id,
                "trigger_started_at": trigger_started_at,
                **summary,
            }
        except Exception as exc:
            self._reconcile_unresolved(
                "V01",
                execution,
                outcome="unprovable",
                public_fact={"terminal": False, "reason": "public_job_unprovable"},
            )
            fail_gate(self.evidence, "V01", artifacts, exc, (), execution.started_at)

    @staticmethod
    def _artifact_json(artifacts: list[Artifact], name: str) -> dict[str, object]:
        matches = [item for item in artifacts if item.path.name == name]
        if len(matches) != 1:
            raise ValueError(f"durable {name} artifact is missing or duplicated")
        value = json.loads(matches[0].path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"durable {name} artifact is invalid")
        return value

    @staticmethod
    def _unresolved_operations(execution: GateExecution) -> list[dict[str, str]]:
        reconciled = {item["operation_id"] for item in execution.reconciliations}
        return [
            item for item in execution.operations
            if item["operation_id"] not in reconciled
        ]

    def _reconcile_unresolved(
        self,
        gate_id: str,
        execution: GateExecution,
        *,
        outcome: Literal["succeeded", "failed", "unprovable"],
        public_fact: dict[str, object],
    ) -> None:
        for operation in self._unresolved_operations(execution):
            self.evidence.reconcile_operation(
                gate_id,
                operation_id=operation["operation_id"],
                outcome=outcome,
                public_fact={**public_fact, "operation_id": operation["operation_id"]},
            )

    def _verify_console(self, browser: BrowserResult) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if browser.summary.get("same_origin") is not True or any(
            item != origin for item in browser.summary.get("origins", [])
        ):
            raise ValueError("V01 Console workflow left the Gateway origin")
        cluster = browser.summary.get("cluster", {})
        binding = browser.summary.get("binding", {})
        authority = browser.summary.get("authority", {})
        scope = authority.get("scope", {}) if isinstance(authority, dict) else {}
        if (
            not isinstance(cluster, dict)
            or cluster.get("cluster_id") != "pilot-cluster"
            or cluster.get("environment") not in {"dev", "test", "staging"}
            or cluster.get("mutation_enabled") is not True
            or not isinstance(binding, dict)
            or not binding.get("deployment_target_id")
            or binding.get("namespace") != "aiops-verification"
            or binding.get("workload_kind") != "Deployment"
            or binding.get("workload_name") != "verification-api"
            or not isinstance(authority, dict)
            or authority.get("environment") != cluster.get("environment")
            or authority.get("scope_type") != "namespace"
            or scope != {
                "cluster_id": "pilot-cluster",
                "namespace": "aiops-verification",
            }
        ):
            raise ValueError(
                "V01 Console provisioning did not bind the exact non-production scope"
            )
        for owner in ("sre", "team", "service", "binding", "authority"):
            item = browser.summary.get(owner)
            if not isinstance(item, dict) or not item.get("id"):
                raise ValueError(f"V01 Console provisioning omitted {owner} identity")
