"""Filesystem Adapter for Clean Acceptance ledger evidence."""

from __future__ import annotations

import json
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from . import evidence_creation, human_attestation
from .evidence_files import (
    atomic_write,
    atomic_write_if_unchanged,
    json_matches,
    sha256,
    sha256_bytes,
    unchanged_file_lock,
)
from .evidence_types import Artifact
from .gate_contract import EVIDENCE_FORMAT_VERSION
from .redaction import assert_secrets_absent, redact_json, redact_text

if TYPE_CHECKING:
    from .ledger import AcceptanceLedger


MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


class EvidenceAdapter:
    """Materialize and verify one ledger's files without owning ledger decisions."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest_path = root / "manifest.json"
        self.attestation_path = root / "human-attestation.yaml"
        self._persisted_manifest_sha256 = (
            sha256(self.manifest_path) if self.manifest_path.is_file() else None
        )

    @classmethod
    def create(cls, owner: type[AcceptanceLedger], parent: Path, **values: Any) -> AcceptanceLedger:
        return evidence_creation.create(owner, parent, **values)

    @classmethod
    def open(
        cls,
        owner: type[AcceptanceLedger],
        root: Path,
        *,
        now: Any,
        new_execution_id: Any,
        attestation_verifier: Any,
    ) -> AcceptanceLedger:
        from .ledger import EvidenceError

        manifest_path = root / "manifest.json"
        if (
            root.is_symlink()
            or not root.is_dir()
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
        ):
            raise EvidenceError("acceptance manifest does not exist")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError("acceptance manifest is invalid") from exc
        if manifest.get("format_version") != EVIDENCE_FORMAT_VERSION:
            raise EvidenceError("unsupported_evidence_format")
        instance = owner(
            root=root,
            manifest=manifest,
            now=now,
            new_execution_id=new_execution_id,
            attestation_verifier=attestation_verifier,
            evidence=cls(root),
        )
        instance._validate_loaded()
        instance._requires_reconciliation = instance.open_gate is not None
        return instance

    def persist(self, manifest: dict[str, Any], *, locked: bool = False) -> None:
        encoded = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        if locked:
            atomic_write(self.manifest_path, encoded, staging_dir=self.root.parent)
        elif not atomic_write_if_unchanged(
            self.manifest_path,
            encoded,
            expected_sha256=self._persisted_manifest_sha256,
            staging_dir=self.root.parent,
        ):
            from .ledger import EvidenceError

            raise EvidenceError("stale ledger writer cannot overwrite newer evidence")
        self._persisted_manifest_sha256 = sha256_bytes(encoded)

    @contextmanager
    def unchanged(self) -> Iterator[bool]:
        with unchanged_file_lock(
            self.manifest_path, self._persisted_manifest_sha256
        ) as unchanged:
            yield unchanged

    def manifest_matches(self, manifest: dict[str, Any]) -> bool:
        return json_matches(self.manifest_path, manifest)

    def artifact(self, gate_id: str, record: dict[str, Any]) -> Artifact:
        return Artifact(
            self.root / record["path"],
            record["path"],
            record["sha256"],
            record["bytes"],
            gate_id,
        )

    def write_text(
        self,
        ledger: AcceptanceLedger,
        gate_id: str,
        name: str,
        value: str,
        *,
        known_secrets: Iterable[str] = (),
    ) -> Artifact:
        secrets = tuple(known_secrets)
        safe = redact_text(value, known_secrets=secrets)
        assert_secrets_absent(safe, secrets)
        return self.write(ledger, gate_id, name, safe.encode("utf-8"))

    def write_json(
        self,
        ledger: AcceptanceLedger,
        gate_id: str,
        name: str,
        value: Any,
        *,
        known_secrets: Iterable[str] = (),
    ) -> Artifact:
        secrets = tuple(known_secrets)
        safe = redact_json(value, known_secrets=secrets)
        encoded = (
            json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        assert_secrets_absent(encoded.decode(), secrets)
        return self.write(ledger, gate_id, name, encoded)

    def write(
        self, ledger: AcceptanceLedger, gate_id: str, name: str, content: bytes
    ) -> Artifact:
        from .ledger import EvidenceError

        with self.unchanged() as unchanged:
            if not unchanged:
                raise EvidenceError("stale ledger writer cannot overwrite newer evidence")
            ledger._safe_artifact_name(name)
            if len(content) > MAX_ARTIFACT_BYTES:
                raise EvidenceError(f"artifact exceeds {MAX_ARTIFACT_BYTES} byte limit")
            attempt = ledger._open_attempt(gate_id)
            directory = self.root / ledger._phase(gate_id) / f"{gate_id}-attempt-1"
            directory.mkdir(exist_ok=True)
            path = directory / name
            retained = next(
                (
                    item
                    for item in attempt["artifacts"]
                    if Path(item["path"]).name == name
                ),
                None,
            )
            if retained is not None:
                if path.exists():
                    raise EvidenceError("artifact is append-only and already exists")
                if retained["sha256"] != sha256_bytes(content) or retained["bytes"] != len(
                    content
                ):
                    raise EvidenceError(
                        "pending artifact retry does not match its durable index"
                    )
                atomic_write(path, content, staging_dir=self.root.parent)
                return self.artifact(gate_id, retained)
            artifact = Artifact(
                path,
                str(path.relative_to(self.root)),
                sha256_bytes(content),
                len(content),
                gate_id,
            )
            attempt["artifacts"].append(
                {
                    "path": artifact.relative_path,
                    "sha256": artifact.sha256,
                    "bytes": artifact.size,
                }
            )
            self.persist(ledger._manifest, locked=True)
            try:
                atomic_write(path, content, staging_dir=self.root.parent)
            except Exception:
                attempt["artifacts"].pop()
                self.persist(ledger._manifest, locked=True)
                path.unlink(missing_ok=True)
                raise
            return artifact

    def passed_artifact(
        self, ledger: AcceptanceLedger, gate_id: str, name: str
    ) -> Artifact:
        from .ledger import EvidenceError

        ledger._safe_artifact_name(name)
        attempts = ledger._manifest["gates"].get(gate_id, [])
        record = next(
            (
                artifact
                for artifact in (attempts[0].get("artifacts", []) if attempts else [])
                if attempts[0].get("status") == "passed"
                and Path(artifact["path"]).name == name
            ),
            None,
        )
        if record is None:
            raise EvidenceError(f"passed {gate_id} artifact is missing: {name}")
        path = self.root / record["path"]
        if path.is_symlink() or not path.is_file() or sha256(path) != record["sha256"]:
            raise EvidenceError(f"artifact changed after recording: {record['path']}")
        return self.artifact(gate_id, record)

    def passed_artifact_json(
        self, ledger: AcceptanceLedger, gate_id: str, name: str
    ) -> dict[str, Any]:
        from .ledger import EvidenceError

        artifact = self.passed_artifact(ledger, gate_id, name)
        try:
            value = json.loads(artifact.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError(
                f"artifact is not valid JSON: {artifact.relative_path}"
            ) from exc
        if not isinstance(value, dict):
            raise EvidenceError(
                f"artifact JSON must be an object: {artifact.relative_path}"
            )
        return {"path": artifact.relative_path, "sha256": artifact.sha256, "value": value}

    def artifact_matches(self, artifact: Artifact) -> bool:
        return (
            artifact.path.is_file()
            and not artifact.path.is_symlink()
            and sha256(artifact.path) == artifact.sha256
        )

    def artifact_exists(self, artifact: Artifact) -> bool:
        return artifact.path.is_file()

    def validate_artifact(
        self,
        gate_id: str,
        artifact: Any,
        seen_paths: set[str],
        *,
        status: str,
        phase: str,
    ) -> str | None:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            return "artifact index entry is invalid"
        relative = Path(artifact["path"])
        expected_parent = Path(phase) / f"{gate_id}-attempt-1"
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.parent != expected_parent
            or artifact["path"] in seen_paths
        ):
            return "artifact path escapes or duplicates its gate execution"
        path = self.root / relative
        if path.is_symlink() or not path.is_file():
            if status == "open" or (
                status == "failed" and artifact.get("state") == "missing"
            ):
                seen_paths.add(artifact["path"])
                return None
            return "indexed artifact is missing or not a regular file"
        if (
            artifact.get("sha256") != sha256(path)
            or artifact.get("bytes") != path.stat().st_size
            or path.stat().st_size > MAX_ARTIFACT_BYTES
        ):
            return "indexed artifact hash, size or bound does not match disk"
        seen_paths.add(artifact["path"])
        return None

    def unindexed_file_error(self, seen_paths: set[str]) -> str | None:
        indexed = {
            self.manifest_path.resolve(),
            *(
                path.resolve()
                for path in (self.attestation_path, self.root / "SHA256SUMS")
                if path.exists()
            ),
        }
        indexed.update((self.root / path).resolve() for path in seen_paths)
        for path in self.root.rglob("*"):
            if path.is_symlink():
                return "evidence ledger contains a symlink"
            if path.is_file() and path.resolve() not in indexed:
                return f"evidence ledger contains an unindexed file: {path.name}"
        return None

    def attestations(self) -> list[dict[str, Any]]:
        return human_attestation.load(self.attestation_path)

    def matching_attestations(
        self, gate_id: str, *, conclusion: str, role: str | None
    ) -> list[dict[str, Any]]:
        return human_attestation.matching(
            self.attestation_path, gate_id, conclusion=conclusion, role=role
        )

    def append_attestation(
        self,
        statement: dict[str, Any],
        *,
        signature: str,
        public_key: str,
        fingerprint: str,
    ) -> str:
        return human_attestation.append(
            self.attestation_path,
            statement,
            signature=signature,
            public_key=public_key,
            fingerprint=fingerprint,
            atomic_write=lambda path, content: atomic_write(
                path, content, staging_dir=self.root.parent
            ),
        )

    def attestation_validation_error(
        self, *, acceptance_id: str, candidate_sha256: str, gate_ids: set[str]
    ) -> str | None:
        return human_attestation.validation_error(
            self.attestation_path,
            acceptance_id=acceptance_id,
            candidate_sha256=candidate_sha256,
            gate_ids=gate_ids,
        )

    def attestation_exists(self) -> bool:
        return self.attestation_path.exists()

    def attestation_index_error(self, value: Any) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, dict)
            or value.get("path") != self.attestation_path.name
            or not self.attestation_path.is_file()
            or value.get("sha256") != sha256(self.attestation_path)
        ):
            return "human attestation index is invalid"
        return None

    def is_sealed(self, manifest: dict[str, Any]) -> bool:
        return manifest.get("seal") is not None and (self.root / "SHA256SUMS").is_file()

    @property
    def checksum_name(self) -> str:
        return "SHA256SUMS"

    def checksum_exists(self) -> bool:
        return (self.root / self.checksum_name).exists()

    def materialize_seal(self) -> Path:
        checksum_path = self.root / "SHA256SUMS"
        files = sorted(path for path in self.root.rglob("*") if path.is_file())
        lines = [f"{sha256(path)}  {path.relative_to(self.root)}" for path in files]
        atomic_write(
            checksum_path,
            ("\n".join(lines) + "\n").encode(),
            staging_dir=self.root.parent,
        )
        self._make_read_only()
        return checksum_path

    def seal_validation_error(self, seal_fact: Any) -> str | None:
        checksum_path = self.root / "SHA256SUMS"
        if seal_fact is None:
            return (
                "final checksum exists without a seal fact" if checksum_path.exists() else None
            )
        if (
            not isinstance(seal_fact, dict)
            or not isinstance(seal_fact.get("sealed_at"), str)
            or seal_fact.get("checksum_path") != checksum_path.name
        ):
            return "seal fact is invalid"
        if not checksum_path.exists():
            return None
        try:
            entries = self._checksum_entries(checksum_path)
        except (OSError, ValueError):
            return "final checksum manifest is invalid"
        files = sorted(
            path for path in self.root.rglob("*") if path.is_file() and path != checksum_path
        )
        actual = {str(path.relative_to(self.root)): sha256(path) for path in files}
        if entries != actual:
            return "final checksum does not match the sealed ledger"
        return self._read_only_error()

    def _make_read_only(self) -> None:
        from .promotion import PromotionError

        entries = [self.root, *self.root.rglob("*")]
        if any(
            path.is_symlink() or not (path.is_file() or path.is_dir()) for path in entries
        ):
            raise PromotionError("sealed ledger contains an unsupported filesystem entry")
        for path in entries:
            if path.is_file():
                path.chmod(0o444)
        for path in sorted(
            (item for item in entries if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            path.chmod(0o555)
        error = self._read_only_error()
        if error:
            raise PromotionError(error)

    def _read_only_error(self) -> str | None:
        for path in [self.root, *self.root.rglob("*")]:
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                return "sealed ledger contains an unsupported filesystem entry"
            expected = 0o555 if path.is_dir() else 0o444
            if stat.S_IMODE(path.stat().st_mode) != expected:
                return "sealed ledger is not permanently read-only"
        return None

    @staticmethod
    def _checksum_entries(path: Path) -> dict[str, str]:
        entries: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            digest, separator, relative_value = line.partition("  ")
            relative = Path(relative_value)
            if (
                separator != "  "
                or not _SHA256.fullmatch(digest)
                or relative.is_absolute()
                or ".." in relative.parts
                or relative_value in entries
                or relative_value == path.name
            ):
                raise ValueError("invalid checksum entry")
            entries[relative_value] = digest
        return entries
