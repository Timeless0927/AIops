"""Clean Acceptance ledger invariants without external I/O."""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal
from . import (
    execution_journal,
    human_attestation,
    ledger_promotion,
    ledger_validation,
    promotion,
)
from .deployment_continuation import deployment_precondition, valid_failure_attribution
from .evidence import EvidenceAdapter
from .evidence_types import Artifact, GateAttempt, GateExecution, GateResult
from .gate_contract import (
    GATE_CONTRACT_REVISION,
    GATE_PHASE,
    GATE_SEQUENCE,
)

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
REQUIRED_ROLE_ATTESTATIONS = {
    "I05": ("platform_administrator",),
    "S04": ("platform_administrator",),
    "S05": ("platform_operator",),
    "V04": ("sre",),
    "V05": ("sre",),
    "V07": ("sre",),
    "V08": ("sre",),
    "C03": ("platform_operator", "platform_administrator", "sre"),
}
class EvidenceError(ValueError):
    """Acceptance evidence violates its immutable run contract."""
class GateFailed(RuntimeError):
    """An acceptance gate was recorded as failed."""
def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
class AcceptanceLedger:
    """Own one Clean Acceptance frontier and its immutable gate facts."""
    def __init__(
        self,
        root: Path,
        manifest: dict[str, Any],
        now: Callable[[], str],
        new_execution_id: Callable[[], str],
        attestation_verifier: Callable[[dict[str, Any]], None] | None = None,
        evidence: Any | None = None,
    ) -> None:
        self.root = root
        self._manifest = manifest
        self._now = now
        self._new_execution_id = new_execution_id
        self._attestation_verifier = attestation_verifier
        self.evidence = evidence or EvidenceAdapter(root)
        self.manifest_path = self.evidence.manifest_path
        self.attestation_path = self.evidence.attestation_path
        self._requires_reconciliation = False
    @classmethod
    def create(
        cls,
        parent: Path,
        *,
        acceptance_id: str,
        release_version: str,
        release_sha256: str,
        acceptance_tool_sha256: str,
        gate_contract_revision: str,
        kube_context: str,
        cluster_identity_sha256: str,
        access_profile: str,
        environment_qualification: dict[str, Any] | None = None,
        deployment_continuation: dict[str, Any] | None = None,
        evaluator_successor: dict[str, Any] | None = None,
        now: Callable[[], str] = _utc_now,
        new_execution_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        attestation_verifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> "AcceptanceLedger":
        try:
            return EvidenceAdapter.create(
                cls, parent, acceptance_id=acceptance_id,
                release_version=release_version, release_sha256=release_sha256,
                acceptance_tool_sha256=acceptance_tool_sha256,
                gate_contract_revision=gate_contract_revision, kube_context=kube_context,
                cluster_identity_sha256=cluster_identity_sha256,
                access_profile=access_profile,
                environment_qualification=environment_qualification,
                deployment_continuation=deployment_continuation,
                evaluator_successor=evaluator_successor,
                now=now, new_execution_id=new_execution_id,
                attestation_verifier=attestation_verifier,
            )
        except EvidenceError:
            raise
        except ValueError as exc:
            raise EvidenceError(str(exc)) from exc
    @classmethod
    def open(
        cls,
        root: Path,
        *,
        now: Callable[[], str] = _utc_now,
        new_execution_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        attestation_verifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> "AcceptanceLedger":
        return EvidenceAdapter.open(
            cls,
            root,
            now=now,
            new_execution_id=new_execution_id,
            attestation_verifier=attestation_verifier,
        )
    @staticmethod
    def _validate_create_inputs(**values: str) -> None:
        if not _ID_PATTERN.fullmatch(values["acceptance_id"]):
            raise EvidenceError("acceptance_id contains unsupported characters")
        if values["access_profile"] not in {"http_nodeport", "https_ingress"}:
            raise EvidenceError("unsupported access profile")
        for field in ("release_sha256", "acceptance_tool_sha256", "cluster_identity_sha256"):
            if not _SHA256_PATTERN.fullmatch(values[field]):
                raise EvidenceError(f"{field} must be lowercase SHA256 hexadecimal")
        if values["gate_contract_revision"] != GATE_CONTRACT_REVISION:
            raise EvidenceError("unsupported gate contract revision")
    def verify_identity(
        self,
        *,
        release_version: str,
        release_sha256: str,
        acceptance_tool_sha256: str,
        gate_contract_revision: str,
        kube_context: str,
        cluster_identity_sha256: str,
        access_profile: str,
        deployment_precondition_sha256: str | None = None,
    ) -> None:
        expected = {
            "release.version": release_version,
            "release.sha256": release_sha256,
            "acceptance_tool.sha256": acceptance_tool_sha256,
            "gate_contract_revision": gate_contract_revision,
            "cluster.kube_context": kube_context,
            "cluster.identity_sha256": cluster_identity_sha256,
            "access_profile": access_profile,
            "deployment_precondition_sha256": (
                deployment_precondition_sha256
                or self._manifest["deployment_precondition_sha256"]
            ),
        }
        actual = {
            "release.version": self._manifest["release"]["version"],
            "release.sha256": self.candidate_sha256,
            "acceptance_tool.sha256": self.acceptance_tool_sha256,
            "gate_contract_revision": self.gate_contract_revision,
            "cluster.kube_context": self.kube_context,
            "cluster.identity_sha256": self.cluster_identity_sha256,
            "access_profile": self.access_profile,
            "deployment_precondition_sha256": self._manifest["deployment_precondition_sha256"],
        }
        drift = sorted(field for field, value in expected.items() if actual[field] != value)
        if not drift:
            return
        self._ensure_writable()
        violations = self._manifest["identity_violations"]
        for field in drift:
            if field not in violations:
                violations.append(field)
        violations.sort()
        self._persist_manifest()
        raise EvidenceError(f"acceptance identity drift: {', '.join(drift)}")
    def status(self) -> dict[str, Any]:
        """Verify the ledger and derive its run status without persisting another state."""
        self._validate_loaded()
        failed = self.failed_gate
        if self.is_sealed:
            state = "sealed"
        elif self._manifest["identity_violations"] or failed:
            state = "ineligible"
        elif self._manifest.get("eligibility", {}).get("conclusion") in {"eligible", "ineligible"}:
            state = self._manifest["eligibility"]["conclusion"]
        elif self.open_gate is not None:
            state = "active/open"
        else:
            state = "active/ready"
        return {
            "status": state, "frontier": self.frontier, "open_gate": self.open_gate,
            **({"failure": {"gate_id": failed, "attribution": self.failure_summary()["failure_attribution"]}} if failed else {}),
        }
    def gate_attempt_count(self) -> int:
        self._validate_loaded()
        return sum(
            bool(self._manifest["gates"].get(gate_id)) for gate_id in GATE_SEQUENCE
        )
    @property
    def frontier(self) -> str | None:
        if self.failed_gate or self._manifest["identity_violations"]:
            return None
        for gate_id in GATE_SEQUENCE:
            attempts = self._manifest["gates"].get(gate_id, [])
            if not attempts or attempts[0]["status"] == "open":
                return gate_id
        return None
    @property
    def open_gate(self) -> str | None:
        return next(
            (
                gate_id
                for gate_id, attempts in self._manifest["gates"].items()
                if attempts and attempts[0]["status"] == "open"
            ),
            None,
        )
    @property
    def failed_gate(self) -> str | None:
        return next(
            (
                gate_id
                for gate_id in GATE_SEQUENCE
                if self.effective_status(gate_id) == "failed"
            ),
            None,
        )
    @property
    def is_sealed(self) -> bool:
        return self.evidence.is_sealed(self._manifest)
    def effective_status(self, gate_id: str) -> str | None:
        attempts = self._manifest["gates"].get(gate_id, [])
        if not attempts:
            return None
        correction = next(
            (
                item
                for item in self._manifest.get("evaluator_corrections", [])
                if item.get("gate_id") == gate_id
            ),
            None,
        )
        return correction["corrected_status"] if correction else attempts[0]["status"]
    def correct_evaluator(self, correction: dict[str, Any]) -> dict[str, Any]:
        """Append one no-I/O replacement over already hash-bound evidence."""
        self._ensure_writable()
        self._manifest.setdefault("evaluator_corrections", []).append(
            json.loads(json.dumps(correction))
        )
        error = self._correction_validation_error()
        if error:
            self._manifest["evaluator_corrections"].pop()
            raise ValueError(error)
        self._persist_manifest()
        return json.loads(json.dumps(correction))
    def correct_s01(
        self,
        *,
        source_artifact: Artifact,
        acceptance_tool_sha256: str,
        diagnostic_conclusion_sha256: str,
        reason: str,
    ) -> dict[str, Any]:
        """Correct S01 from typed, pre-materialized hash facts without I/O."""
        self._ensure_writable()
        if self.failed_gate != "S01":
            raise ValueError(
                "S01 evaluator correction requires S01 to be the effective failed gate"
            )
        if any(
            item.get("gate_id") == "S01"
            for item in self._manifest.get("evaluator_corrections", [])
        ):
            raise ValueError("S01 evaluator correction already exists")
        attempt = self._manifest["gates"]["S01"][0]
        if (
            len(attempt.get("operations", [])) != 1
            or attempt["operations"][0].get("kind") != "gate_execution"
            or attempt.get("reconciliations")
        ):
            raise ValueError("S01 evaluator correction cannot cover external operations")
        indexed = next(
            (
                item
                for item in attempt.get("artifacts", [])
                if item.get("path") == source_artifact.relative_path
                and item.get("sha256") == source_artifact.sha256
            ),
            None,
        )
        if indexed is None or source_artifact.gate_id != "S01":
            raise ValueError("S01 evaluator correction source artifact drifted")
        previous_sha = self.active_acceptance_tool_sha256
        if acceptance_tool_sha256 == previous_sha:
            raise ValueError("evaluator correction requires a changed acceptance tool")
        normalized_reason = reason.strip()
        if not self._valid_public_reason(normalized_reason):
            raise ValueError("evaluator correction reason is invalid")
        return self.correct_evaluator(
            {
                "kind": "evaluator_only",
                "policy_revision": "evaluator-correction-v1",
                "gate_id": "S01",
                "source_execution_id": attempt["execution_id"],
                "source_artifacts": [
                    {"path": source_artifact.relative_path, "sha256": source_artifact.sha256}
                ],
                "from_acceptance_tool_sha256": previous_sha,
                "to_acceptance_tool_sha256": acceptance_tool_sha256,
                "diagnostic_conclusion_sha256": diagnostic_conclusion_sha256,
                "corrected_status": "passed",
                "corrected_at": self.now(),
                "reason": normalized_reason,
            }
        )
    @property
    def active_acceptance_tool_sha256(self) -> str:
        corrections = self._manifest.get("evaluator_corrections", [])
        return (
            corrections[-1]["to_acceptance_tool_sha256"]
            if corrections
            else self.acceptance_tool_sha256
        )
    def start_gate(self, gate_id: str) -> str:
        self.require_frontier(gate_id)
        if gate_id == "I01":
            try:
                deployment_precondition(
                    environment_qualification=(
                        self._manifest["deployment_precondition"]
                        if self.deployment_mode == "clean_install" else None
                    ),
                    deployment_continuation=(
                        self._manifest["deployment_precondition"]
                        if self.deployment_mode == "adopt_existing" else None
                    ), evaluator_successor=(
                        self._manifest["deployment_precondition"]
                        if self.deployment_mode == "evaluator_successor" else None
                    ), at=self._now(), verifier=self._attestation_verifier,
                    release_sha256=self.candidate_sha256,
                    acceptance_tool_sha256=self.acceptance_tool_sha256,
                    gate_contract_revision=self.gate_contract_revision,
                    kube_context=self.kube_context,
                    cluster_identity_sha256=self.cluster_identity_sha256,
                    access_profile=self.access_profile,
                )
            except ValueError as exc:
                raise EvidenceError(str(exc)) from exc
        if self.open_gate is not None:
            raise EvidenceError(f"{self.open_gate} is already open; use resume")
        started_at = self._now()
        execution_id = self._new_execution_id()
        if not execution_journal.valid_operation_id(execution_id):
            raise EvidenceError("generated gate execution identity is invalid")
        if any(
            operation.get("operation_id") == execution_id
            for attempts in self._manifest["gates"].values()
            for operation in attempts[0].get("operations", [])
        ):
            raise EvidenceError("generated gate execution identity is not unique")
        self._manifest["gates"][gate_id] = [
            execution_journal.new_attempt(execution_id, started_at)
        ]
        self._persist_manifest(); return started_at
    def resume_gate(self, gate_id: str) -> GateExecution:
        self._phase(gate_id)
        attempts = self._manifest["gates"].get(gate_id, [])
        if len(attempts) != 1 or attempts[0].get("status") != "open":
            raise EvidenceError(f"{gate_id} has no open execution to reconcile")
        attempt = attempts[0]
        return GateExecution(
            gate_id,
            attempt["execution_id"],
            attempt["started_at"],
            tuple(dict(item) for item in attempt["operations"]),
            tuple(self.evidence.artifact(gate_id, item) for item in attempt["artifacts"]),
            tuple(dict(item) for item in attempt["reconciliations"]),
        )
    def bind_operation(self, gate_id: str, *, kind: str, operation_id: str) -> None:
        """Persist an external operation identity before its Adapter dispatch."""
        self._ensure_writable()
        if operation_id in self._manifest["deployment_precondition"].get("record", {}).get("source", {}).get("issued_operation_ids", []):
            raise EvidenceError("operation identity was already issued by the retained deployment")
        attempt = self._open_attempt(gate_id)
        try:
            execution_journal.bind(
                self._manifest, attempt, kind=kind, operation_id=operation_id, now=self._now
            )
        except execution_journal.JournalError as exc:
            raise EvidenceError(str(exc)) from exc
        self._persist_manifest()
    def reconcile_operation(
        self,
        gate_id: str,
        *,
        operation_id: str,
        outcome: Literal["succeeded", "failed", "unprovable"],
        public_fact: dict[str, Any],
    ) -> None:
        """Persist the bounded public fact used to reconcile an interrupted effect."""
        attempt = self._open_attempt(gate_id)
        try:
            execution_journal.reconcile(
                attempt,
                operation_id=operation_id,
                outcome=outcome,
                public_fact=public_fact,
                now=self._now,
            )
        except execution_journal.JournalError as exc:
            raise EvidenceError(str(exc)) from exc
        self._persist_manifest()
    def require_frontier(self, gate_id: str) -> None:
        self._phase(gate_id)
        self._ensure_writable()
        if self.failed_gate:
            raise EvidenceError(f"run is ineligible after mandatory gate {self.failed_gate} failed")
        if self._manifest["identity_violations"]:
            raise EvidenceError("run is ineligible after acceptance identity drift")
        if gate_id != self.frontier:
            raise EvidenceError(f"{gate_id} is not frontier; current frontier is {self.frontier}")
    def next_attempt(self, gate_id: str) -> int:
        self._phase(gate_id)
        if gate_id in self._manifest["gates"]:
            raise EvidenceError(f"{gate_id} already has its only gate attempt")
        return 1
    @property
    def candidate_sha256(self) -> str:
        return str(self._manifest["release"]["sha256"])
    @property
    def acceptance_tool_sha256(self) -> str:
        return str(self._manifest["acceptance_tool"]["sha256"])
    @property
    def gate_contract_revision(self) -> str:
        return str(self._manifest["gate_contract_revision"])
    @property
    def kube_context(self) -> str:
        return str(self._manifest["cluster"]["kube_context"])
    @property
    def cluster_identity_sha256(self) -> str:
        return str(self._manifest["cluster"]["identity_sha256"])
    @property
    def access_profile(self) -> str:
        return str(self._manifest["access_profile"])
    @property
    def deployment_mode(self) -> str:
        return str(self._manifest["deployment_mode"])
    @property
    def deployment_precondition_sha256(self) -> str:
        return str(self._manifest["deployment_precondition_sha256"])
    def now(self) -> str:
        return self._now()
    def failure_summary(self) -> dict[str, Any]:
        gate_id = self.failed_gate
        if gate_id is None:
            raise EvidenceError("failure summary requires a failed mandatory gate")
        attempt = self._manifest["gates"][gate_id][0]
        decision = self._manifest.get("promotion_decision", {}).get("statement", {}).get("decision")
        prior = self._manifest["deployment_precondition"].get("record", {}).get("source", {}).get("issued_operation_ids", [])
        issued = set(prior)
        issued.update(item["operation_id"] for attempts in self._manifest["gates"].values()
                      for completed in attempts for item in completed["operations"] if item["kind"] != "gate_execution")
        return {
            "acceptance_id": self._manifest["acceptance_id"], "gate_id": gate_id,
            "status": "failed", "failure_attribution": attempt["failure_attribution"],
            "product_sha256": self.candidate_sha256,
            "acceptance_tool_sha256": self.acceptance_tool_sha256,
            "kube_context": self.kube_context,
            "cluster_identity_sha256": self.cluster_identity_sha256,
            "access_profile": self.access_profile, "decision": decision,
            "issued_operation_ids": sorted(issued),
        }
    def completed_artifact_index(self) -> list[dict[str, Any]]:
        from .gate_reuse import completed_artifact_index
        return completed_artifact_index(self)
    def terminal_gate_fact(self, gate_id: str) -> dict[str, Any]:
        from .gate_reuse import terminal_gate_fact
        return terminal_gate_fact(self, gate_id)
    def passed_artifact_json(self, gate_id: str, name: str) -> dict[str, Any]:
        return self.evidence.passed_artifact_json(self, gate_id, name)
    def passed_artifact(self, gate_id: str, name: str) -> Artifact:
        """Return one hash-verified artifact identity from a passed gate."""
        return self.evidence.passed_artifact(self, gate_id, name)
    def write_text(self, gate_id: str, name: str, value: str, *, known_secrets: Iterable[str] = ()) -> Artifact:
        return self.evidence.write_text(
            self, gate_id, name, value, known_secrets=known_secrets
        )
    def write_json(self, gate_id: str, name: str, value: Any, *, known_secrets: Iterable[str] = ()) -> Artifact:
        return self.evidence.write_json(
            self, gate_id, name, value, known_secrets=known_secrets
        )
    def write_bytes(self, gate_id: str, name: str, value: bytes) -> Artifact:
        return self.evidence.write(self, gate_id, name, value)
    def command_text(self, result: Any, *, known_secrets: Iterable[str] = ()) -> str:
        return (
            f"release_sha256: {self.candidate_sha256}\n"
            f"kube_context: {self.kube_context}\n"
            + result.evidence_text(known_secrets=known_secrets)
        )
    def contextualize(self, value: Any) -> dict[str, Any]:
        return {
            "release_sha256": self.candidate_sha256,
            "acceptance_tool_sha256": self.acceptance_tool_sha256,
            "gate_contract_revision": self.gate_contract_revision,
            "kube_context": self.kube_context,
            "result": value,
        }
    def record_gate(
        self,
        gate_id: str,
        result: GateResult,
        *,
        started_at: str | None = None,
    ) -> GateAttempt:
        self._ensure_writable()
        status = result.status
        failure_attribution = result.failure_attribution
        if status not in {"passed", "failed", "not_applicable"}:
            raise EvidenceError("unsupported gate status")
        if status == "failed":
            failure_attribution = failure_attribution or "inconclusive"
            if not valid_failure_attribution(failure_attribution):
                raise EvidenceError("unsupported failure attribution")
        elif failure_attribution is not None:
            raise EvidenceError("only failed gates may have failure attribution")
        if status == "not_applicable" and gate_id != "I04":
            raise EvidenceError("only I04 may be not_applicable")
        if status == "not_applicable" and self.access_profile != "http_nodeport":
            raise EvidenceError("I04 may be not_applicable only for http_nodeport")
        attempt = self._open_attempt(gate_id)
        if self._requires_reconciliation and status == "passed":
            succeeded = {
                item["operation_id"]
                for item in attempt["reconciliations"]
                if item["outcome"] == "succeeded"
            }
            if {item["operation_id"] for item in attempt["operations"]} - succeeded:
                raise EvidenceError("interrupted gate lacks proved terminal public facts")
        if started_at is not None and started_at != attempt["started_at"]:
            raise EvidenceError("gate start timestamp does not match durable execution")
        artifact_tuple = result.artifacts
        indexed = {item["path"] for item in attempt["artifacts"]}
        if {item.relative_path for item in artifact_tuple} != indexed:
            raise EvidenceError("terminal gate result must include every indexed artifact")
        for artifact in artifact_tuple:
            if artifact.gate_id != gate_id or artifact.attempt != 1:
                raise EvidenceError("artifact does not belong to this gate execution")
            if not self.evidence.artifact_exists(artifact) and status == "failed":
                next(item for item in attempt["artifacts"] if item["path"] == artifact.relative_path)["state"] = "missing"
                continue
            if not self.evidence.artifact_matches(artifact):
                raise EvidenceError("artifact changed before gate recording")
        completed_at = self._now()
        attempt["status"] = status
        if failure_attribution is not None:
            attempt["failure_attribution"] = failure_attribution
        attempt["completed_at"] = completed_at
        self._persist_manifest()
        return GateAttempt(
            gate_id, 1, status, attempt["started_at"], completed_at, artifact_tuple,
            failure_attribution,
        )
    def attestations_for(
        self, gate_id: str, *, conclusion: str = "passed", role: str | None = None
    ) -> list[dict[str, Any]]:
        self._phase(gate_id)
        self._validate_loaded()
        return self.evidence.matching_attestations(
            gate_id, conclusion=conclusion, role=role
        )
    def require_verified_attestation(self, gate_id: str, *, role: str) -> list[dict[str, Any]]:
        items = self.attestations_for(gate_id, role=role)
        if not items:
            raise EvidenceError(f"missing {role} attestation for {gate_id}")
        if self._attestation_verifier is None:
            raise EvidenceError("attestation verification Adapter is not configured")
        for item in items:
            self._attestation_verifier(item)
        return items
    def attestation_statement(
        self,
        *,
        actor: str,
        role: str,
        gate_ids: Iterable[str],
        conclusion: Literal["passed", "failed"],
        note: str,
    ) -> dict[str, Any]:
        gates = sorted(set(gate_ids))
        if not actor or not role or not note or not gates:
            raise EvidenceError("attestation fields must be non-empty")
        for gate_id in gates:
            self._phase(gate_id)
        return human_attestation.statement(
            acceptance_id=self._manifest["acceptance_id"],
            candidate_sha256=self.candidate_sha256,
            actor=actor,
            role=role,
            gate_ids=gates,
            conclusion=conclusion,
            observed_at=self._now(),
            note=note,
        )
    def append_attestation(
        self,
        statement: dict[str, Any],
        *,
        signature: str,
        public_key: str,
        fingerprint: str,
    ) -> None:
        with self.evidence.unchanged() as unchanged:
            if not unchanged:
                raise EvidenceError("stale ledger writer cannot overwrite newer evidence")
            self._ensure_writable()
            if human_attestation.signature_identity_error(signature, public_key, fingerprint):
                raise EvidenceError("attestation signature identity must be non-empty")
            if statement.get("acceptance_id") != self._manifest["acceptance_id"]:
                raise EvidenceError("attestation belongs to another acceptance run")
            if human_attestation.statement_error(
                statement,
                acceptance_id=self._manifest["acceptance_id"],
                candidate_sha256=self.candidate_sha256,
                gate_ids=set(GATE_SEQUENCE),
            ):
                raise EvidenceError("human attestation statement is invalid")
            digest = self.evidence.append_attestation(
                statement,
                signature=signature,
                public_key=public_key,
                fingerprint=fingerprint,
            )
            self._manifest["human_attestation"] = {
                "path": self.evidence.attestation_path.name,
                "sha256": digest,
            }
            self._persist_manifest(locked=True)
    def all_attestations(self) -> list[dict[str, Any]]:
        self._validate_loaded()
        return self.evidence.attestations()
    def evaluate(self) -> dict[str, Any]:
        if self._manifest.get("eligibility") is not None:
            raise promotion.PromotionError("eligibility has already been evaluated")
        self._ensure_writable()
        if self.failed_gate is None and self.frontier is not None:
            raise promotion.PromotionError(
                "evaluate requires C03 completion or a terminal gate failure"
            )
        integrity_failed = False
        try:
            self._validate_loaded()
        except ValueError:
            integrity_failed = True
        result = self._derive_eligibility()
        if integrity_failed:
            result["reasons"].insert(0, {"code": "ledger_integrity_failed"})
            result["conclusion"] = "ineligible"
        result["evaluated_at"] = self._now()
        self._manifest["eligibility"] = result
        self._persist_manifest()
        return json.loads(json.dumps(result))
    def seal(self) -> Path:
        if self.evidence.checksum_exists():
            raise promotion.PromotionError("evidence ledger is already sealed")
        self._validate_loaded()
        seal_fact = self._manifest.get("seal")
        if seal_fact is None:
            error = self._promotion_facts_error()
            if error:
                raise promotion.PromotionError(error)
            if self._manifest.get("eligibility") is None:
                raise promotion.PromotionError("seal requires eligibility evaluation")
            if self._manifest.get("promotion_decision") is None:
                raise promotion.PromotionError(
                    "seal requires a signed Promotion Decision"
                )
            self._manifest["seal"] = {
                "sealed_at": self._now(),
                "checksum_path": self.evidence.checksum_name,
            }
            self._persist_manifest()
        else:
            error = self._promotion_facts_error()
            if error:
                raise promotion.PromotionError(error)
        return self.evidence.materialize_seal()
    def _derive_eligibility(self) -> dict[str, Any]:
        return ledger_promotion.derive_eligibility(
            self, REQUIRED_ROLE_ATTESTATIONS
        )
    def _promotion_facts_error(self) -> str | None:
        return ledger_promotion.promotion_facts_error(
            self, REQUIRED_ROLE_ATTESTATIONS
        )
    def _open_attempt(self, gate_id: str) -> dict[str, Any]:
        self._phase(gate_id)
        attempts = self._manifest["gates"].get(gate_id, [])
        if len(attempts) != 1 or attempts[0].get("status") != "open":
            raise EvidenceError(f"{gate_id} has no open gate execution")
        return attempts[0]
    def _accepted(self, gate_id: str) -> bool:
        status = self.effective_status(gate_id)
        allowed = {"passed"}
        if gate_id == "I04" and self.access_profile == "http_nodeport":
            allowed.add("not_applicable")
        return status in allowed
    def _correction_validation_error(self) -> str | None:
        return ledger_validation.correction_validation_error(self)
    @staticmethod
    def _valid_public_reason(value: object) -> bool:
        return ledger_validation.valid_public_reason(value)
    def _phase(self, gate_id: str) -> str:
        try:
            return GATE_PHASE[gate_id]
        except KeyError as exc:
            raise EvidenceError(f"unknown gate ID: {gate_id}") from exc
    @staticmethod
    def _safe_artifact_name(name: str) -> None:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise EvidenceError("artifact name must be a single safe path component")
    def _validate_loaded(self) -> None:
        ledger_validation.validate_loaded(self, REQUIRED_ROLE_ATTESTATIONS)
    def _expected_gate_before(self, gate_id: str) -> str:
        for expected in GATE_SEQUENCE:
            if expected == gate_id:
                return expected
            if not self._accepted(expected):
                return expected
        return gate_id
    def _ensure_writable(self) -> None:
        if (
            self._manifest.get("eligibility")
            or self._manifest.get("seal")
            or self.evidence.checksum_exists()
        ):
            raise EvidenceError("evaluated or sealed evidence ledger is permanently read-only")
    def _persist_manifest(self, *, locked: bool = False) -> None:
        self.evidence.persist(self._manifest, locked=locked)
