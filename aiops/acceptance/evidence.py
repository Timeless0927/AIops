"""Append-only Pilot acceptance evidence ledger."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import yaml

from .redaction import assert_secrets_absent, redact_json, redact_text


GateStatus = Literal["passed", "failed", "not_applicable"]
PHASE_DIRECTORIES = (
    "00-package",
    "01-install",
    "02-setup",
    "03-recovery",
    "04-run-1",
    "05-run-2",
    "06-cleanup",
)
GATE_PHASE = {
    **{f"P{number:02d}": "00-package" for number in range(1, 4)},
    **{f"I{number:02d}": "01-install" for number in range(1, 6)},
    **{f"S{number:02d}": "02-setup" for number in range(1, 7)},
    **{f"R{number:02d}": "03-recovery" for number in range(1, 7)},
    **{f"V{number:02d}": "04-run-1" for number in range(1, 8)},
    "V08": "05-run-2",
    **{f"C{number:02d}": "06-cleanup" for number in range(1, 4)},
}
A01_REQUIRED_GATES = tuple(
    [f"P{number:02d}" for number in range(1, 4)]
    + [f"I{number:02d}" for number in range(1, 6)]
    + [f"S{number:02d}" for number in range(1, 7)]
)
A01_GATE_SEQUENCE = A01_REQUIRED_GATES
A01_ATTESTATION_ROLES = {
    "P03": "platform_operator",
    "I05": "platform_administrator",
    "S04": "platform_administrator",
    "S05": "platform_operator",
}
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EvidenceError(ValueError):
    """Acceptance evidence violates its immutable run contract."""


class GateFailed(RuntimeError):
    """An acceptance gate was recorded as failed."""


@dataclass(frozen=True)
class Artifact:
    path: Path
    relative_path: str
    sha256: str
    size: int
    gate_id: str
    attempt: int


@dataclass(frozen=True)
class GateAttempt:
    gate_id: str
    attempt: int
    status: GateStatus
    started_at: str
    completed_at: str
    artifacts: tuple[Artifact, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AcceptanceEvidence:
    """Owns one immutable candidate's local, append-only evidence index."""

    def __init__(
        self,
        root: Path,
        manifest: dict[str, Any],
        now: Callable[[], str],
        attestation_verifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.root = root
        self.manifest_path = root / "manifest.json"
        self.attestation_path = root / "human-attestation.yaml"
        self._manifest = manifest
        self._now = now
        self._attestation_verifier = attestation_verifier

    @classmethod
    def create(
        cls,
        parent: Path,
        *,
        acceptance_id: str,
        release_version: str,
        release_sha256: str,
        kube_context: str,
        cluster_identity_sha256: str,
        access_profile: str,
        now: Callable[[], str] = _utc_now,
        attestation_verifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> "AcceptanceEvidence":
        if not _ID_PATTERN.fullmatch(acceptance_id):
            raise EvidenceError("acceptance_id contains unsupported characters")
        if access_profile not in {"http_nodeport", "https_ingress"}:
            raise EvidenceError("unsupported access profile")
        if not re.fullmatch(r"[0-9a-f]{64}", release_sha256):
            raise EvidenceError("release SHA256 must be lowercase hexadecimal")
        if not re.fullmatch(r"[0-9a-f]{64}", cluster_identity_sha256):
            raise EvidenceError("cluster identity SHA256 must be lowercase hexadecimal")
        root = parent / acceptance_id
        candidate = {"version": release_version, "sha256": release_sha256}
        if (root / "manifest.json").exists():
            existing = cls.open(
                root, now=now, attestation_verifier=attestation_verifier
            )
            if existing._manifest.get("release") != candidate:
                raise EvidenceError("candidate identity cannot change within an acceptance run")
            if existing._manifest.get("cluster") != {
                "kube_context": kube_context,
                "identity_sha256": cluster_identity_sha256,
            } or existing.access_profile != access_profile:
                raise EvidenceError("cluster/access identity cannot change within an acceptance run")
            return existing
        root.mkdir(parents=True, exist_ok=False)
        for phase in PHASE_DIRECTORIES:
            (root / phase).mkdir()
        manifest: dict[str, Any] = {
            "format_version": 1,
            "acceptance_id": acceptance_id,
            "created_at": now(),
            "release": candidate,
            "cluster": {
                "kube_context": kube_context,
                "identity_sha256": cluster_identity_sha256,
            },
            "access_profile": access_profile,
            "gates": {},
            "promotion_eligible": False,
        }
        instance = cls(root, manifest, now, attestation_verifier)
        instance._persist_manifest()
        return instance

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        now: Callable[[], str] = _utc_now,
        attestation_verifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> "AcceptanceEvidence":
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise EvidenceError("acceptance manifest does not exist")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        instance = cls(root, manifest, now, attestation_verifier)
        instance._validate_loaded()
        return instance

    def start_gate(self, gate_id: str) -> str:
        self.require_frontier(gate_id)
        return self._now()

    def require_frontier(self, gate_id: str) -> None:
        self._phase(gate_id)
        if gate_id not in A01_GATE_SEQUENCE:
            return
        position = A01_GATE_SEQUENCE.index(gate_id)
        for predecessor in A01_GATE_SEQUENCE[:position]:
            attempts = self._manifest["gates"].get(predecessor, [])
            if not attempts or any(item["status"] == "failed" for item in attempts):
                raise EvidenceError(f"{gate_id} is not frontier; {predecessor} is incomplete or failed")
            allowed = {"passed"}
            if predecessor == "I04" and self.access_profile == "http_nodeport":
                allowed.add("not_applicable")
            if attempts[-1]["status"] not in allowed:
                raise EvidenceError(f"{gate_id} is not frontier; {predecessor} has no accepted result")

    def next_attempt(self, gate_id: str) -> int:
        self._phase(gate_id)
        return len(self._manifest["gates"].get(gate_id, [])) + 1

    @property
    def candidate_sha256(self) -> str:
        return str(self._manifest["release"]["sha256"])

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
    def promotion_eligible(self) -> bool:
        return self._promotion_eligible()

    def attestations_for(
        self,
        gate_id: str,
        *,
        conclusion: str = "passed",
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        self._phase(gate_id)
        if not self.attestation_path.exists():
            return []
        payload = yaml.safe_load(self.attestation_path.read_text(encoding="utf-8")) or {}
        return [
            item
            for item in payload.get("attestations", [])
            if gate_id in item.get("statement", {}).get("gate_ids", [])
            and item.get("statement", {}).get("conclusion") == conclusion
            and (role is None or item.get("statement", {}).get("role") == role)
        ]

    def require_verified_attestation(self, gate_id: str, *, role: str) -> list[dict[str, Any]]:
        items = self.attestations_for(gate_id, role=role)
        if not items:
            raise EvidenceError(f"missing {role} attestation for {gate_id}")
        if self._attestation_verifier is None:
            raise EvidenceError("attestation verification Adapter is not configured")
        for item in items:
            self._attestation_verifier(item)
        return items

    def write_text(
        self,
        gate_id: str,
        name: str,
        value: str,
        *,
        known_secrets: Iterable[str] = (),
    ) -> Artifact:
        secrets = tuple(known_secrets)
        safe = redact_text(value, known_secrets=secrets)
        assert_secrets_absent(safe, secrets)
        return self._write(gate_id, name, safe.encode("utf-8"))

    def write_json(
        self,
        gate_id: str,
        name: str,
        value: Any,
        *,
        known_secrets: Iterable[str] = (),
    ) -> Artifact:
        secrets = tuple(known_secrets)
        safe = redact_json(value, known_secrets=secrets)
        encoded = (json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        assert_secrets_absent(encoded.decode(), secrets)
        return self._write(gate_id, name, encoded)

    def write_bytes(self, gate_id: str, name: str, value: bytes) -> Artifact:
        """Store a trusted binary artifact such as a browser screenshot."""
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
            "kube_context": self.kube_context,
            "result": value,
        }

    def _write(self, gate_id: str, name: str, content: bytes) -> Artifact:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise EvidenceError("artifact name must be a single safe path component")
        attempt = self.next_attempt(gate_id)
        directory = self.root / self._phase(gate_id) / f"{gate_id}-attempt-{attempt}"
        directory.mkdir(exist_ok=True)
        path = directory / name
        if path.exists():
            raise EvidenceError("artifact is append-only and already exists")
        path.write_bytes(content)
        relative = str(path.relative_to(self.root))
        return Artifact(path, relative, _sha256(path), len(content), gate_id, attempt)

    def record_gate(
        self,
        gate_id: str,
        status: GateStatus,
        artifacts: Iterable[Artifact],
        *,
        started_at: str | None = None,
    ) -> GateAttempt:
        self.require_frontier(gate_id)
        if status not in {"passed", "failed", "not_applicable"}:
            raise EvidenceError("unsupported gate status")
        if status == "not_applicable" and gate_id != "I04":
            raise EvidenceError("only I04 may be not_applicable")
        if status == "not_applicable" and self.access_profile != "http_nodeport":
            raise EvidenceError("I04 may be not_applicable only for http_nodeport")
        attempt = self.next_attempt(gate_id)
        artifact_tuple = tuple(artifacts)
        for artifact in artifact_tuple:
            if artifact.gate_id != gate_id or artifact.attempt != attempt:
                raise EvidenceError("artifact does not belong to this gate attempt")
            if not artifact.path.is_file() or _sha256(artifact.path) != artifact.sha256:
                raise EvidenceError("artifact changed before gate recording")
        completed_at = self._now()
        started = started_at or completed_at
        record = {
            "attempt": attempt,
            "status": status,
            "started_at": started,
            "completed_at": completed_at,
            "artifacts": [
                {"path": item.relative_path, "sha256": item.sha256, "bytes": item.size}
                for item in artifact_tuple
            ],
        }
        self._manifest["gates"].setdefault(gate_id, []).append(record)
        self._manifest["promotion_eligible"] = self._promotion_eligible()
        self._persist_manifest()
        return GateAttempt(gate_id, attempt, status, started, completed_at, artifact_tuple)

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
        return {
            "acceptance_id": self._manifest["acceptance_id"],
            "candidate_sha256": self._manifest["release"]["sha256"],
            "actor": actor,
            "role": role,
            "gate_ids": gates,
            "conclusion": conclusion,
            "observed_at": self._now(),
            "note": note,
        }

    def append_attestation(
        self,
        statement: dict[str, Any],
        *,
        signature: str,
        public_key: str,
        fingerprint: str,
    ) -> None:
        if not signature or not public_key or not fingerprint:
            raise EvidenceError("attestation signature identity must be non-empty")
        if statement.get("acceptance_id") != self._manifest["acceptance_id"]:
            raise EvidenceError("attestation belongs to another acceptance run")
        current = {"format_version": 1, "attestations": []}
        if self.attestation_path.exists():
            current = yaml.safe_load(self.attestation_path.read_text(encoding="utf-8"))
        current["attestations"].append(
            {
                "statement": statement,
                "signature": signature,
                "public_key": public_key,
                "fingerprint": fingerprint,
            }
        )
        self._atomic_write(
            self.attestation_path,
            yaml.safe_dump(current, sort_keys=False, allow_unicode=True).encode(),
        )

    def finalize(
        self,
        attestation_verifier: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        self._validate_loaded()
        if not self._promotion_eligible():
            raise EvidenceError("A01 gate manifest is incomplete or contains a failed attempt")
        verifier = attestation_verifier or self._attestation_verifier
        if verifier is None:
            raise EvidenceError("attestation verification Adapter is not configured")
        attestations = self.all_attestations()
        for item in attestations:
            verifier(item)
        for gate_id, role in A01_ATTESTATION_ROLES.items():
            if not any(
                gate_id in item["statement"]["gate_ids"]
                and item["statement"]["role"] == role
                and item["statement"]["conclusion"] == "passed"
                for item in attestations
            ):
                raise EvidenceError(f"missing verified {role} attestation for {gate_id}")
        if self.attestation_path.exists():
            self._manifest["human_attestation"] = {
                "path": self.attestation_path.name,
                "sha256": _sha256(self.attestation_path),
            }
        self._manifest["finalized_at"] = self._now()
        self._persist_manifest()
        checksum_path = self.root / "SHA256SUMS"
        files = sorted(
            path for path in self.root.rglob("*") if path.is_file() and path != checksum_path
        )
        lines = [f"{_sha256(path)}  {path.relative_to(self.root)}" for path in files]
        self._atomic_write(checksum_path, ("\n".join(lines) + "\n").encode())
        return checksum_path

    def all_attestations(self) -> list[dict[str, Any]]:
        if not self.attestation_path.exists():
            return []
        payload = yaml.safe_load(self.attestation_path.read_text(encoding="utf-8")) or {}
        return list(payload.get("attestations", []))

    def _phase(self, gate_id: str) -> str:
        try:
            return GATE_PHASE[gate_id]
        except KeyError as exc:
            raise EvidenceError(f"unknown gate ID: {gate_id}") from exc

    def _promotion_eligible(self) -> bool:
        for gate_id in A01_REQUIRED_GATES:
            attempts = self._manifest["gates"].get(gate_id, [])
            if not attempts or any(item["status"] == "failed" for item in attempts):
                return False
            allowed = {"passed"}
            if gate_id == "I04" and self.access_profile == "http_nodeport":
                allowed.add("not_applicable")
            if attempts[-1]["status"] not in allowed:
                return False
        return True

    def _validate_loaded(self) -> None:
        manifest = self._manifest
        if manifest.get("format_version") != 1:
            raise EvidenceError("unsupported acceptance manifest format")
        if not _ID_PATTERN.fullmatch(str(manifest.get("acceptance_id", ""))):
            raise EvidenceError("invalid acceptance_id in manifest")
        release = manifest.get("release")
        cluster = manifest.get("cluster")
        if not isinstance(release, dict) or not re.fullmatch(
            r"[0-9a-f]{64}", str(release.get("sha256", ""))
        ):
            raise EvidenceError("invalid release identity in manifest")
        if not isinstance(cluster, dict) or not re.fullmatch(
            r"[0-9a-f]{64}", str(cluster.get("identity_sha256", ""))
        ):
            raise EvidenceError("invalid cluster identity in manifest")
        if manifest.get("access_profile") not in {"http_nodeport", "https_ingress"}:
            raise EvidenceError("invalid access profile in manifest")
        gates = manifest.get("gates")
        if not isinstance(gates, dict) or any(gate_id not in GATE_PHASE for gate_id in gates):
            raise EvidenceError("manifest contains an invalid gate index")
        seen_paths: set[str] = set()
        for gate_id, attempts in gates.items():
            if not isinstance(attempts, list):
                raise EvidenceError("gate attempts must be a list")
            for expected_attempt, attempt in enumerate(attempts, start=1):
                if not isinstance(attempt, dict) or attempt.get("attempt") != expected_attempt:
                    raise EvidenceError("gate attempt sequence is not append-only")
                status = attempt.get("status")
                if status not in {"passed", "failed", "not_applicable"}:
                    raise EvidenceError("manifest contains an invalid gate status")
                if status == "not_applicable" and (
                    gate_id != "I04" or self.access_profile != "http_nodeport"
                ):
                    raise EvidenceError("manifest contains an invalid conditional gate result")
                if not isinstance(attempt.get("started_at"), str) or not isinstance(
                    attempt.get("completed_at"), str
                ):
                    raise EvidenceError("gate attempt timestamps are invalid")
                artifacts = attempt.get("artifacts")
                if not isinstance(artifacts, list):
                    raise EvidenceError("gate artifact index must be a list")
                for artifact in artifacts:
                    self._validate_artifact(gate_id, expected_attempt, artifact, seen_paths)
        if manifest.get("promotion_eligible") != self._promotion_eligible():
            raise EvidenceError("stored promotion eligibility does not match gate history")
        if self.attestation_path.exists():
            self._validate_attestation_file()
        indexed_attestation = manifest.get("human_attestation")
        if indexed_attestation is not None:
            if (
                not isinstance(indexed_attestation, dict)
                or indexed_attestation.get("path") != "human-attestation.yaml"
                or not self.attestation_path.is_file()
                or indexed_attestation.get("sha256") != _sha256(self.attestation_path)
            ):
                raise EvidenceError("human attestation index is invalid")

    def _validate_artifact(
        self,
        gate_id: str,
        attempt: int,
        artifact: Any,
        seen_paths: set[str],
    ) -> None:
        if not isinstance(artifact, dict):
            raise EvidenceError("artifact index entry must be an object")
        relative_value = artifact.get("path")
        if not isinstance(relative_value, str):
            raise EvidenceError("artifact path is invalid")
        relative = Path(relative_value)
        expected_parent = Path(self._phase(gate_id)) / f"{gate_id}-attempt-{attempt}"
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.parent != expected_parent
            or relative_value in seen_paths
        ):
            raise EvidenceError("artifact path escapes or duplicates its gate attempt")
        path = self.root / relative
        if path.is_symlink() or not path.is_file():
            raise EvidenceError("indexed artifact is missing or not a regular file")
        if artifact.get("sha256") != _sha256(path) or artifact.get("bytes") != path.stat().st_size:
            raise EvidenceError("indexed artifact hash or size does not match disk")
        seen_paths.add(relative_value)

    def _validate_attestation_file(self) -> None:
        payload = yaml.safe_load(self.attestation_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("format_version") != 1:
            raise EvidenceError("human attestation file has an invalid format")
        items = payload.get("attestations")
        if not isinstance(items, list):
            raise EvidenceError("human attestation list is invalid")
        for item in items:
            if not isinstance(item, dict) or not all(
                isinstance(item.get(key), str) and item.get(key)
                for key in ("signature", "public_key", "fingerprint")
            ):
                raise EvidenceError("human attestation signature identity is invalid")
            statement = item.get("statement")
            if (
                not isinstance(statement, dict)
                or statement.get("acceptance_id") != self._manifest["acceptance_id"]
                or statement.get("candidate_sha256") != self.candidate_sha256
                or statement.get("conclusion") not in {"passed", "failed"}
                or not isinstance(statement.get("gate_ids"), list)
                or not statement["gate_ids"]
                or any(gate_id not in GATE_PHASE for gate_id in statement["gate_ids"])
            ):
                raise EvidenceError("human attestation statement is invalid")

    def _persist_manifest(self) -> None:
        encoded = (
            json.dumps(self._manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        self._atomic_write(self.manifest_path, encoded)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
