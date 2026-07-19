"""Format v3 append-only Clean Acceptance evidence ledger."""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal
from .redaction import assert_secrets_absent, redact_json, redact_text
from . import evidence_creation, execution_journal, human_attestation, promotion
from .evidence_files import atomic_write as _atomic_write, json_matches as _json_matches
from .evidence_files import sha256 as _sha256, sha256_bytes as _sha256_bytes
from .environment_qualification_record import validate_bundle as _validate_qualification
from .evidence_types import Artifact, GateAttempt, GateExecution, GateStatus
from .gate_contract import (
    A01_GATE_SEQUENCE,
    GATE_CONTRACT_REVISION,
    GATE_PHASE,
    GATE_SEQUENCE,
)
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
class EvidenceError(ValueError):
    """Acceptance evidence violates its immutable run contract."""
class GateFailed(RuntimeError):
    """An acceptance gate was recorded as failed."""
def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
class AcceptanceEvidence:
    """Owns one exact candidate/tool/Cluster ledger and its only gate frontier."""
    def __init__(
        self,
        root: Path,
        manifest: dict[str, Any],
        now: Callable[[], str],
        new_execution_id: Callable[[], str],
        attestation_verifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.root = root
        self.manifest_path = root / "manifest.json"
        self.attestation_path = root / "human-attestation.yaml"
        self._manifest = manifest
        self._now = now
        self._new_execution_id = new_execution_id
        self._attestation_verifier = attestation_verifier
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
        environment_qualification: dict[str, Any],
        now: Callable[[], str] = _utc_now,
        new_execution_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        attestation_verifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> "AcceptanceEvidence":
        try:
            return evidence_creation.create(
                cls, parent, acceptance_id=acceptance_id,
                release_version=release_version, release_sha256=release_sha256,
                acceptance_tool_sha256=acceptance_tool_sha256,
                gate_contract_revision=gate_contract_revision, kube_context=kube_context,
                cluster_identity_sha256=cluster_identity_sha256,
                access_profile=access_profile,
                environment_qualification=environment_qualification,
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
    ) -> "AcceptanceEvidence":
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise EvidenceError("acceptance manifest does not exist")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError("acceptance manifest is invalid") from exc
        if manifest.get("format_version") != 3:
            raise EvidenceError("unsupported_evidence_format")
        instance = cls(root, manifest, now, new_execution_id, attestation_verifier)
        instance._validate_loaded()
        instance._requires_reconciliation = instance.open_gate is not None
        return instance
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
        environment_qualification_sha256: str | None = None,
    ) -> None:
        expected = {
            "release.version": release_version,
            "release.sha256": release_sha256,
            "acceptance_tool.sha256": acceptance_tool_sha256,
            "gate_contract_revision": gate_contract_revision,
            "cluster.kube_context": kube_context,
            "cluster.identity_sha256": cluster_identity_sha256,
            "access_profile": access_profile,
            "environment_qualification_sha256": (
                environment_qualification_sha256
                or self._manifest["environment_qualification_sha256"]
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
            "environment_qualification_sha256": self._manifest["environment_qualification_sha256"],
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
        if promotion.is_sealed(self):
            state = "sealed"
        elif self._manifest["identity_violations"] or failed:
            state = "ineligible"
        elif self._manifest.get("eligibility", {}).get("conclusion") in {"eligible", "ineligible"}:
            state = self._manifest["eligibility"]["conclusion"]
        elif self.open_gate is not None:
            state = "active/open"
        else:
            state = "active/ready"
        return {"status": state, "frontier": self.frontier, "open_gate": self.open_gate}
    def gate_attempt_count(self) -> int:
        self._validate_loaded(); return sum(bool(self._manifest["gates"].get(gate_id)) for gate_id in GATE_SEQUENCE)
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
                if self._manifest["gates"].get(gate_id, [{}])[0].get("status") == "failed"
            ),
            None,
        )
    def start_gate(self, gate_id: str) -> str:
        self.require_frontier(gate_id)
        if gate_id == "I01":
            try:
                self._validate_qualification(
                    self._manifest["environment_qualification"],
                    at=self._now(), verifier=self._attestation_verifier,
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
            tuple(self._artifact(gate_id, item) for item in attempt["artifacts"]),
            tuple(dict(item) for item in attempt["reconciliations"]),
        )
    def bind_operation(self, gate_id: str, *, kind: str, operation_id: str) -> None:
        """Persist an external operation identity before its Adapter dispatch."""
        self._ensure_writable()
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
    def completed_artifact_index(self) -> list[dict[str, Any]]:
        self._validate_loaded()
        return [{
            "gate_id": gate_id, "status": attempt["status"],
            "execution_id": attempt["execution_id"],
            "artifacts": [dict(item) for item in attempt["artifacts"]],
        }
            for gate_id in GATE_SEQUENCE
            for attempt in self._manifest["gates"].get(gate_id, [])
            if attempt["status"] != "open"
        ]
    def passed_artifact_json(self, gate_id: str, name: str) -> dict[str, Any]:
        artifact = self.passed_artifact(gate_id, name)
        try:
            value = json.loads(artifact.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"artifact is not valid JSON: {artifact.relative_path}") from exc
        if not isinstance(value, dict):
            raise EvidenceError(f"artifact JSON must be an object: {artifact.relative_path}")
        return {"path": artifact.relative_path, "sha256": artifact.sha256, "value": value}

    def passed_artifact(self, gate_id: str, name: str) -> Artifact:
        """Return one hash-verified artifact identity from a passed gate."""
        self._safe_artifact_name(name)
        attempts = self._manifest["gates"].get(gate_id, [])
        record = next(
            (
                artifact
                for artifact in (attempts[0].get("artifacts", []) if attempts else [])
                if attempts[0].get("status") == "passed" and Path(artifact["path"]).name == name
            ),
            None,
        )
        if record is None:
            raise EvidenceError(f"passed {gate_id} artifact is missing: {name}")
        path = self.root / record["path"]
        if path.is_symlink() or not path.is_file() or _sha256(path) != record["sha256"]:
            raise EvidenceError(f"artifact changed after recording: {record['path']}")
        return self._artifact(gate_id, record)

    def write_text(self, gate_id: str, name: str, value: str, *, known_secrets: Iterable[str] = ()) -> Artifact:
        secrets = tuple(known_secrets)
        safe = redact_text(value, known_secrets=secrets)
        assert_secrets_absent(safe, secrets)
        return self._write(gate_id, name, safe.encode("utf-8"))

    def write_json(self, gate_id: str, name: str, value: Any, *, known_secrets: Iterable[str] = ()) -> Artifact:
        secrets = tuple(known_secrets)
        safe = redact_json(value, known_secrets=secrets)
        encoded = (json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        assert_secrets_absent(encoded.decode(), secrets)
        return self._write(gate_id, name, encoded)

    def write_bytes(self, gate_id: str, name: str, value: bytes) -> Artifact:
        return self._write(gate_id, name, value)

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

    def _write(self, gate_id: str, name: str, content: bytes) -> Artifact:
        self._safe_artifact_name(name)
        if len(content) > MAX_ARTIFACT_BYTES:
            raise EvidenceError(f"artifact exceeds {MAX_ARTIFACT_BYTES} byte limit")
        attempt = self._open_attempt(gate_id)
        directory = self.root / self._phase(gate_id) / f"{gate_id}-attempt-1"
        directory.mkdir(exist_ok=True)
        path = directory / name
        retained = next(
            (item for item in attempt["artifacts"] if Path(item["path"]).name == name), None
        )
        if retained is not None:
            if path.exists():
                raise EvidenceError("artifact is append-only and already exists")
            if retained["sha256"] != _sha256_bytes(content) or retained["bytes"] != len(content):
                raise EvidenceError("pending artifact retry does not match its durable index")
            _atomic_write(path, content, staging_dir=self.root.parent)
            return self._artifact(gate_id, retained)
        artifact = Artifact(
            path, str(path.relative_to(self.root)), _sha256_bytes(content), len(content), gate_id
        )
        attempt["artifacts"].append({
            "path": artifact.relative_path,
            "sha256": artifact.sha256,
            "bytes": artifact.size,
        })
        self._persist_manifest()
        try:
            _atomic_write(path, content, staging_dir=self.root.parent)
        except Exception:
            attempt["artifacts"].pop()
            self._persist_manifest()
            path.unlink(missing_ok=True)
            raise
        return artifact

    def record_gate(
        self,
        gate_id: str,
        status: GateStatus,
        artifacts: Iterable[Artifact],
        *,
        started_at: str | None = None,
    ) -> GateAttempt:
        self._ensure_writable()
        if status not in {"passed", "failed", "not_applicable"}:
            raise EvidenceError("unsupported gate status")
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
        artifact_tuple = tuple(artifacts)
        indexed = {item["path"] for item in attempt["artifacts"]}
        if {item.relative_path for item in artifact_tuple} != indexed:
            raise EvidenceError("terminal gate result must include every indexed artifact")
        for artifact in artifact_tuple:
            if artifact.gate_id != gate_id or artifact.attempt != 1:
                raise EvidenceError("artifact does not belong to this gate execution")
            if not artifact.path.is_file() and status == "failed":
                next(item for item in attempt["artifacts"] if item["path"] == artifact.relative_path)["state"] = "missing"
                continue
            if artifact.path.is_symlink() or not artifact.path.is_file() or _sha256(artifact.path) != artifact.sha256:
                raise EvidenceError("artifact changed before gate recording")
        completed_at = self._now()
        attempt["status"] = status
        attempt["completed_at"] = completed_at
        self._persist_manifest()
        return GateAttempt(
            gate_id, 1, status, attempt["started_at"], completed_at, artifact_tuple
        )

    def create_diagnostic_bundle(self, parent: Path, *, diagnostic_id: str) -> Path:
        """Create a separate troubleshooting bundle without reopening the failed ledger."""
        if not _ID_PATTERN.fullmatch(diagnostic_id):
            raise EvidenceError("diagnostic_id contains unsupported characters")
        if self.failed_gate is None:
            raise EvidenceError("diagnostic evidence requires a failed mandatory gate")
        root = parent / diagnostic_id
        root.mkdir(parents=True, exist_ok=False)
        _atomic_write(
            root / "manifest.json",
            (json.dumps({
                "format": "diagnostic_evidence_v1",
                "diagnostic_id": diagnostic_id,
                "source_acceptance_id": self._manifest["acceptance_id"],
                "source_failed_gate": self.failed_gate,
                "release_sha256": self.candidate_sha256,
                "acceptance_tool_sha256": self.acceptance_tool_sha256,
                "created_at": self._now(),
            }, indent=2, sort_keys=True) + "\n").encode(),
            staging_dir=parent,
        )
        return root

    def attestations_for(
        self, gate_id: str, *, conclusion: str = "passed", role: str | None = None
    ) -> list[dict[str, Any]]:
        self._phase(gate_id)
        self._validate_loaded()
        return human_attestation.matching(
            self.attestation_path, gate_id, conclusion=conclusion, role=role
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
        digest = human_attestation.append(
            self.attestation_path,
            statement,
            signature=signature,
            public_key=public_key,
            fingerprint=fingerprint,
            atomic_write=lambda path, content: _atomic_write(
                path, content, staging_dir=self.root.parent
            ),
        )
        self._manifest["human_attestation"] = {
            "path": self.attestation_path.name,
            "sha256": digest,
        }
        self._persist_manifest()
    def all_attestations(self) -> list[dict[str, Any]]:
        self._validate_loaded()
        return human_attestation.load(self.attestation_path)
    evaluate = promotion.evaluate
    seal = promotion.seal
    def _open_attempt(self, gate_id: str) -> dict[str, Any]:
        self._phase(gate_id)
        attempts = self._manifest["gates"].get(gate_id, [])
        if len(attempts) != 1 or attempts[0].get("status") != "open":
            raise EvidenceError(f"{gate_id} has no open gate execution")
        return attempts[0]

    def _accepted(self, gate_id: str) -> bool:
        attempts = self._manifest["gates"].get(gate_id, [])
        if len(attempts) != 1:
            return False
        allowed = {"passed"}
        if gate_id == "I04" and self.access_profile == "http_nodeport":
            allowed.add("not_applicable")
        return attempts[0].get("status") in allowed
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
        manifest = self._manifest
        if not _json_matches(self.manifest_path, manifest):
            raise EvidenceError("acceptance manifest changed outside its owner Interface")
        if manifest.get("format_version") != 3:
            raise EvidenceError("unsupported_evidence_format")
        if not _ID_PATTERN.fullmatch(str(manifest.get("acceptance_id", ""))):
            raise EvidenceError("invalid acceptance_id in manifest")
        release = manifest.get("release")
        tool = manifest.get("acceptance_tool")
        cluster = manifest.get("cluster")
        if not isinstance(release, dict) or not _SHA256_PATTERN.fullmatch(str(release.get("sha256", ""))):
            raise EvidenceError("invalid release identity in manifest")
        if not isinstance(tool, dict) or not _SHA256_PATTERN.fullmatch(str(tool.get("sha256", ""))):
            raise EvidenceError("invalid acceptance-tool identity in manifest")
        if manifest.get("gate_contract_revision") != GATE_CONTRACT_REVISION:
            raise EvidenceError("unsupported gate contract revision")
        if not isinstance(cluster, dict) or not _SHA256_PATTERN.fullmatch(str(cluster.get("identity_sha256", ""))):
            raise EvidenceError("invalid cluster identity in manifest")
        if manifest.get("access_profile") not in {"http_nodeport", "https_ingress"}:
            raise EvidenceError("invalid access profile in manifest")
        try:
            self._validate_qualification(
                manifest.get("environment_qualification"),
                at=str(manifest.get("created_at", "")),
                verifier=self._attestation_verifier,
                release_sha256=release["sha256"], acceptance_tool_sha256=tool["sha256"],
                gate_contract_revision=manifest["gate_contract_revision"],
                kube_context=cluster.get("kube_context"),
                cluster_identity_sha256=cluster["identity_sha256"],
                access_profile=manifest["access_profile"],
            )
        except ValueError as exc:
            raise EvidenceError(str(exc)) from exc
        if manifest.get("environment_qualification_sha256") != manifest["environment_qualification"]["bundle_sha256"]:
            raise EvidenceError("environment qualification index is invalid")
        violations = manifest.get("identity_violations")
        if not isinstance(violations, list) or any(not isinstance(item, str) for item in violations):
            raise EvidenceError("identity violation facts are invalid")
        gates = manifest.get("gates")
        if not isinstance(gates, dict) or any(gate_id not in GATE_SEQUENCE for gate_id in gates):
            raise EvidenceError("manifest contains an invalid gate index")
        seen_paths: set[str] = set()
        seen_operation_ids: set[str] = set()
        open_gates = 0
        terminal_seen = False
        for gate_id in GATE_SEQUENCE:
            attempts = gates.get(gate_id, [])
            if not isinstance(attempts, list) or len(attempts) > 1:
                raise EvidenceError("each gate allows at most one attempt")
            if not attempts:
                continue
            attempt = attempts[0]
            if not isinstance(attempt, dict) or attempt.get("attempt") != 1:
                raise EvidenceError("gate attempt identity is invalid")
            status = attempt.get("status")
            if status not in {"open", "passed", "failed", "not_applicable"}:
                raise EvidenceError("manifest contains an invalid gate status")
            if terminal_seen:
                raise EvidenceError("manifest contains a gate after a terminal failure")
            if gate_id != self._expected_gate_before(gate_id):
                raise EvidenceError("manifest gate history is not the canonical frontier")
            if status == "open":
                open_gates += 1
            elif not isinstance(attempt.get("completed_at"), str):
                raise EvidenceError("terminal gate timestamp is invalid")
            if status == "failed":
                terminal_seen = True
            if status == "not_applicable" and (
                gate_id != "I04" or self.access_profile != "http_nodeport"
            ):
                raise EvidenceError("manifest contains an invalid conditional gate result")
            if not execution_journal.valid_operation_id(str(attempt.get("execution_id", ""))):
                raise EvidenceError("gate execution identity is invalid")
            if not isinstance(attempt.get("started_at"), str):
                raise EvidenceError("gate start timestamp is invalid")
            journal_error = execution_journal.validation_error(attempt)
            if journal_error:
                raise EvidenceError(journal_error)
            operation_ids = {item["operation_id"] for item in attempt["operations"]}
            if seen_operation_ids & operation_ids:
                raise EvidenceError("gate operation identity is duplicated across the ledger")
            seen_operation_ids.update(operation_ids)
            artifacts = attempt.get("artifacts")
            if not isinstance(artifacts, list):
                raise EvidenceError("gate artifact index must be a list")
            for artifact in artifacts:
                self._validate_artifact(gate_id, artifact, seen_paths, status=status)
        if open_gates > 1:
            raise EvidenceError("manifest contains more than one open gate")
        indexed = {
            self.manifest_path.resolve(),
            *(path.resolve() for path in (self.attestation_path, self.root / "SHA256SUMS") if path.exists()),
        }
        indexed.update((self.root / path).resolve() for path in seen_paths)
        for path in self.root.rglob("*"):
            if path.is_symlink():
                raise EvidenceError("evidence ledger contains a symlink")
            if path.is_file() and path.resolve() not in indexed:
                raise EvidenceError(f"evidence ledger contains an unindexed file: {path.name}")
        if self.attestation_path.exists():
            self._validate_attestation_file()
        attestation = manifest.get("human_attestation")
        if attestation is not None and (
            not isinstance(attestation, dict)
            or attestation.get("path") != self.attestation_path.name
            or not self.attestation_path.is_file()
            or attestation.get("sha256") != _sha256(self.attestation_path)
        ):
            raise EvidenceError("human attestation index is invalid")
        promotion_error = promotion.manifest_validation_error(self)
        if promotion_error:
            raise EvidenceError(promotion_error)

    def _expected_gate_before(self, gate_id: str) -> str:
        for expected in GATE_SEQUENCE:
            if expected == gate_id:
                return expected
            if not self._accepted(expected):
                return expected
        return gate_id

    def _validate_artifact(self, gate_id: str, artifact: Any, seen_paths: set[str], *, status: str) -> None:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise EvidenceError("artifact index entry is invalid")
        relative = Path(artifact["path"])
        expected_parent = Path(self._phase(gate_id)) / f"{gate_id}-attempt-1"
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.parent != expected_parent
            or artifact["path"] in seen_paths
        ):
            raise EvidenceError("artifact path escapes or duplicates its gate execution")
        path = self.root / relative
        if path.is_symlink() or not path.is_file():
            if status == "open" or (status == "failed" and artifact.get("state") == "missing"):
                seen_paths.add(artifact["path"])
                return
            raise EvidenceError("indexed artifact is missing or not a regular file")
        if (
            artifact.get("sha256") != _sha256(path)
            or artifact.get("bytes") != path.stat().st_size
            or path.stat().st_size > MAX_ARTIFACT_BYTES
        ):
            raise EvidenceError("indexed artifact hash, size or bound does not match disk")
        seen_paths.add(artifact["path"])

    def _artifact(self, gate_id: str, record: dict[str, Any]) -> Artifact:
        return Artifact(self.root / record["path"], record["path"], record["sha256"], record["bytes"], gate_id)

    def _validate_attestation_file(self) -> None:
        error = human_attestation.validation_error(
            self.attestation_path,
            acceptance_id=self._manifest["acceptance_id"],
            candidate_sha256=self.candidate_sha256,
            gate_ids=set(GATE_SEQUENCE),
        )
        if error:
            raise EvidenceError(error)

    def _ensure_writable(self) -> None:
        if self._manifest.get("eligibility") or self._manifest.get("seal") or (self.root / "SHA256SUMS").exists():
            raise EvidenceError("evaluated or sealed evidence ledger is permanently read-only")

    def _persist_manifest(self) -> None:
        encoded = (json.dumps(self._manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        _atomic_write(self.manifest_path, encoded, staging_dir=self.root.parent)
    @staticmethod
    def _validate_qualification(value: Any, *, at: str, verifier: Any, **identity: Any) -> None:
        moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
        _validate_qualification(value, now=lambda: moment, verifier=verifier, **identity)
