"""Signed deployment handoff after a diagnosed Acceptance Runner failure."""
from __future__ import annotations
import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from .command import CommandExecutor
from .credentials import assert_public_payload
from .execution_journal import valid_operation_id
from .evidence_files import atomic_write, sha256, sha256_bytes
from .environment_qualification_record import validate_bundle as validate_qualification
from .gate_contract import EVIDENCE_FORMAT_VERSION, GATE_CONTRACT_REVISION, GATE_SEQUENCE
from .gate_reuse import freeze_reuse_plan, validate_reusable_gates
from .human_attestation import signature_identity_error
from .freeze import verify_final_checksums
from .redaction import redact_json, redact_text
FORMAT_VERSION = 2
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ATTRIBUTIONS = {"product_failure", "tool_failure", "environment_failure", "inconclusive"}
_RETAINABLE_ATTRIBUTIONS = {"tool_failure", "environment_failure"}
_SIGNED_FIELDS = {"statement", "signature", "public_key", "fingerprint"}
_RECONCILIATION_FIELDS = {
    "operation_id", "request_id", "object_identity", "revision_identity", "outcome",
    "unknown_side_effects", "irreversible_side_effects",
}
_DEPLOYMENT_IDENTITY_FIELDS = {
    "product_sha256", "cluster_identity_sha256", "rendered_manifest_sha256",
    "deployment_images_sha256", "deployment_configuration_sha256",
    "health_snapshot_sha256", "manifest_diff_sha256", "manifest_diff_exit_code",
    "manifest_diff_server_generation_only", "healthy", "observed_at",
}
def valid_failure_attribution(value: object) -> bool:
    return value in _ATTRIBUTIONS
def replacement_identity(freeze_root: Path) -> dict[str, object]:
    verify_final_checksums(freeze_root)
    record = _object(freeze_root / "freeze-record.json", "freeze record")
    contracts = record.get("contracts")
    artifacts = record.get("artifacts")
    if not isinstance(contracts, dict) or not isinstance(artifacts, dict):
        raise ValueError("replacement freeze record is invalid")
    product = artifacts.get("pilot_release")
    tool = artifacts.get("acceptance_tool")
    value = {
        "product_sha256": product.get("sha256") if isinstance(product, dict) else None,
        "acceptance_tool_sha256": tool.get("sha256") if isinstance(tool, dict) else None,
        "gate_contract_revision": contracts.get("gate_contract_revision"),
        "evidence_format_version": contracts.get("evidence_format_version"),
    }
    _validate_replacement(value)
    return value

def deployment_precondition(
    *,
    environment_qualification: dict[str, Any] | None,
    deployment_continuation: dict[str, Any] | None,
    at: str,
    verifier: Callable[[dict[str, Any]], None] | None,
    **identity: Any,
) -> tuple[str, dict[str, Any]]:
    if (environment_qualification is None) == (deployment_continuation is None):
        raise ValueError(
            "exactly one clean-install qualification or deployment continuation is required"
        )
    moment = lambda: _parse_utc(at)
    if environment_qualification is not None:
        validate_qualification(
            environment_qualification, now=moment, verifier=verifier, **identity,
        )
        return "clean_install", environment_qualification
    assert deployment_continuation is not None
    from .evaluator_successor import FORMAT as SUCCESSOR_FORMAT, validate as validate_successor
    if deployment_continuation.get("format") == SUCCESSOR_FORMAT:
        validate_successor(deployment_continuation, **identity)
        return "adopt_existing", deployment_continuation
    validate_bundle(
        deployment_continuation, now=moment, verifier=verifier, **identity,
    )
    return "adopt_existing", deployment_continuation

def create_diagnostic_bundle(
    source: Any,
    parent: Path,
    *,
    diagnostic_id: str,
) -> Path:
    """Create separate troubleshooting evidence without touching the source ledger."""
    if not _ID.fullmatch(diagnostic_id):
        raise ValueError("diagnostic_id contains unsupported characters")
    failure = source.failure_summary()
    rendered_manifest_sha256, deployment_images_sha256 = _source_deployment_baselines(
        source, required=False,
    )
    root = parent / diagnostic_id
    root.mkdir(parents=True, exist_ok=False)
    manifest = {
        "format": "diagnostic_evidence_v1",
        "diagnostic_id": diagnostic_id,
        "source_acceptance_id": failure["acceptance_id"],
        "source_failed_gate": failure["gate_id"],
        "source_failure_attribution": failure["failure_attribution"],
        "release_sha256": failure["product_sha256"],
        "acceptance_tool_sha256": failure["acceptance_tool_sha256"],
        "rendered_manifest_sha256": rendered_manifest_sha256,
        "deployment_images_sha256": deployment_images_sha256,
        "created_at": source.now(),
    }
    assert_public_payload(manifest)
    atomic_write(
        root / "manifest.json", _json_bytes(manifest), staging_dir=parent,
    )
    return root

def conclude_diagnostic_bundle(
    path: Path,
    *,
    diagnosed_attribution: str,
    conclusion_note: str,
    evidence: Iterable[Path],
    recovered_operation_ids: Iterable[str] = (),
    operation_accounting_complete: bool = False,
) -> Path:
    """Append one conclusion bound to concrete diagnostic artifacts."""
    if (path / "conclusion.json").exists():
        raise ValueError("diagnostic bundle is already concluded")
    if (
        not valid_failure_attribution(diagnosed_attribution)
        or not 1 <= len(conclusion_note.strip()) <= 2048
        or redact_text(conclusion_note) != conclusion_note
    ):
        raise ValueError("diagnostic conclusion is invalid")
    root = path.resolve()
    manifest = _object(path / "manifest.json", "diagnostic manifest")
    if manifest.get("format") != "diagnostic_evidence_v1":
        raise ValueError("diagnostic manifest is invalid")
    artifacts: list[dict[str, object]] = []
    referenced: set[Path] = set()
    for item in evidence:
        if item.is_symlink() or not item.is_file():
            raise ValueError("diagnostic evidence is invalid")
        _public_diagnostic_fact(item)
        try:
            relative = item.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError("diagnostic evidence must be inside its bundle") from exc
        if relative.as_posix() in {"manifest.json", "conclusion.json"}:
            raise ValueError("diagnostic evidence cannot cite bundle metadata")
        referenced.add(item.resolve())
        artifacts.append({
            "path": relative.as_posix(), "sha256": sha256(item),
            "bytes": item.stat().st_size,
        })
    if (
        not artifacts
        or len(artifacts) > 128
        or len({item["path"] for item in artifacts}) != len(artifacts)
    ):
        raise ValueError("diagnostic conclusion requires unique evidence")
    files = {item.resolve() for item in path.rglob("*") if item.is_file()}
    if files != referenced | {(path / "manifest.json").resolve()}:
        raise ValueError("every diagnostic artifact must support the conclusion")
    recovered = list(recovered_operation_ids)
    if (
        len(recovered) > 128
        or any(not isinstance(item, str) or not valid_operation_id(item) for item in recovered)
        or len(recovered) != len(set(recovered))
    ):
        raise ValueError("diagnostic recovered operation identities are invalid")
    conclusion = {
        "format": "diagnostic_conclusion_v1",
        "diagnosed_attribution": diagnosed_attribution,
        "conclusion_note": conclusion_note,
        "operation_accounting_complete": operation_accounting_complete,
        "recovered_operation_ids": sorted(recovered),
        "evidence": sorted(artifacts, key=lambda item: str(item["path"])),
    }
    assert_public_payload(conclusion)
    target = path / "conclusion.json"
    atomic_write(target, _json_bytes(conclusion), staging_dir=path)
    return target

class DeploymentContinuation:
    """Own checksummed, Platform-Operator-signed continuation epochs."""

    def __init__(
        self,
        root: Path,
        *,
        commands: CommandExecutor | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.root = root
        self.commands = commands
        self.now = now

    def create(
        self,
        *,
        epoch_id: str,
        source: Any,
        diagnostic: Path,
        replacement: dict[str, object],
        reconciliations: Iterable[dict[str, object]],
        release_archive: Path,
        release_checksums: Path,
        gate_reuse_plan: Iterable[dict[str, object]] = (),
        ttl_seconds: int = 3600,
    ) -> Path:
        if not _ID.fullmatch(epoch_id):
            raise ValueError("epoch_id contains unsupported characters")
        if not 60 <= ttl_seconds <= 86400:
            raise ValueError("continuation epoch TTL is invalid")
        failure = source.failure_summary()
        rendered_manifest_sha256, deployment_images_sha256 = _source_deployment_baselines(
            source, required=True,
        )
        assert rendered_manifest_sha256 is not None
        assert deployment_images_sha256 is not None
        if source.status().get("status") != "sealed" or failure.get("decision") != "no_promote":
            raise ValueError("continuation requires a sealed no-promote source ledger")
        if failure["failure_attribution"] == "product_failure":
            raise ValueError("diagnosed Product failure requires rebuild")
        _validate_replacement(replacement)
        if replacement["product_sha256"] != failure["product_sha256"]:
            raise ValueError("product identity changed; rebuild required")
        if replacement["acceptance_tool_sha256"] == failure["acceptance_tool_sha256"]:
            raise ValueError("continuation requires a replacement Acceptance Runner freeze")
        diagnostic_digest, diagnosed_attribution, operation_ids = _diagnostic_facts(
            diagnostic, failure,
            rendered_manifest_sha256=rendered_manifest_sha256,
            deployment_images_sha256=deployment_images_sha256,
        )
        if self.commands is None:
            raise ValueError("deployment continuation requires a Kubernetes command adapter")
        from .deployment_observation import observe_existing_deployment

        deployment_identity = observe_existing_deployment(
            source, self.commands, release_archive, release_checksums,
            rendered_manifest_sha256=rendered_manifest_sha256,
            deployment_images_sha256=deployment_images_sha256,
            observed_at=lambda: _iso(self.now()),
        )
        _validate_deployment_identity(
            deployment_identity,
            product_sha256=failure["product_sha256"],
            cluster_identity_sha256=failure["cluster_identity_sha256"],
            rendered_manifest_sha256=rendered_manifest_sha256,
            deployment_images_sha256=deployment_images_sha256,
        )
        reconciled = _validated_reconciliations(
            reconciliations, expected=operation_ids,
        )
        reusable_gates = freeze_reuse_plan(
            source, gate_reuse_plan,
            failed_gate=failure["gate_id"], reconciled=operation_ids,
        )
        created = self.now()
        record = {
            "format_version": FORMAT_VERSION,
            "epoch_id": epoch_id,
            "source": {
                "acceptance_id": failure["acceptance_id"],
                "failed_gate": failure["gate_id"],
                "seal_sha256": sha256(source.root / "SHA256SUMS"),
                "diagnostic_sha256": diagnostic_digest,
                "product_sha256": failure["product_sha256"],
                "acceptance_tool_sha256": failure["acceptance_tool_sha256"],
                "failure_attribution": failure["failure_attribution"],
                "rendered_manifest_sha256": rendered_manifest_sha256,
                "deployment_images_sha256": deployment_images_sha256,
                "issued_operation_ids": sorted(operation_ids),
            },
            "replacement": dict(replacement),
            "cluster": {
                "kube_context": failure["kube_context"],
                "identity_sha256": failure["cluster_identity_sha256"],
            },
            "access_profile": failure["access_profile"],
            "deployment_identity": deployment_identity,
            "diagnosed_attribution": diagnosed_attribution,
            "reconciliations": reconciled,
            "reusable_gates": reusable_gates,
            "environment_contaminated": False,
            "disposition": "retain_existing",
            "created_at": _iso(created),
            "expires_at": _iso(created + timedelta(seconds=ttl_seconds)),
        }
        assert_public_payload(record)
        path = self.root / epoch_id
        path.mkdir(parents=True, exist_ok=False)
        write_record(path, record)
        return path

    def attestation_statement(
        self, path: Path, *, actor: str, note: str,
    ) -> dict[str, Any]:
        bundle = inspect(path)
        record = bundle["record"]
        statement = {
            "epoch_id": record["epoch_id"],
            "record_sha256": bundle["record_sha256"],
            "source": record["source"],
            "replacement": record["replacement"],
            "cluster": record["cluster"],
            "access_profile": record["access_profile"],
            "deployment_identity": record["deployment_identity"],
            "reusable_gates": record["reusable_gates"],
            "disposition": record["disposition"],
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "actor": actor,
            "role": "platform_operator",
            "conclusion": "retain_existing_and_reuse_exact_gates",
            "signed_at": _iso(self.now()),
            "note": note,
        }
        assert_public_payload(statement)
        return statement

    @staticmethod
    def attach_attestation(path: Path, item: dict[str, Any]) -> None:
        if (path / "attestation.json").exists():
            raise ValueError("deployment continuation is already signed")
        bundle = inspect(path)
        _validate_attestation(bundle["record"], bundle["record_sha256"], item, None)
        atomic_write(path / "attestation.json", _json_bytes(item), staging_dir=path)

    inspect = staticmethod(lambda path, **kwargs: inspect(path, **kwargs))

def write_record(path: Path, record: dict[str, Any]) -> None:
    content = _json_bytes(record)
    atomic_write(path / "record.json", content, staging_dir=path)
    atomic_write(
        path / "SHA256SUMS",
        f"{sha256_bytes(content)}  record.json\n".encode(), staging_dir=path,
    )

def inspect(
    path: Path,
    *,
    require_signed: bool = False,
    verifier: Callable[[dict[str, Any]], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    record_path = path / "record.json"
    checksum_path = path / "SHA256SUMS"
    if record_path.is_symlink() or not record_path.is_file() or not checksum_path.is_file():
        raise ValueError("deployment continuation record is incomplete")
    digest = sha256(record_path)
    if checksum_path.read_text(encoding="utf-8") != f"{digest}  record.json\n":
        raise ValueError("deployment continuation checksum mismatch")
    record = _object(record_path, "deployment continuation record")
    _validate_record(record)
    attestation_path = path / "attestation.json"
    attestation = (
        _object(attestation_path, "deployment continuation attestation")
        if attestation_path.is_file() else None
    )
    if require_signed and attestation is None:
        raise ValueError("deployment continuation is not signed")
    if attestation is not None:
        _validate_attestation(record, digest, attestation, verifier)
    unsigned = {"record": record, "record_sha256": digest, "attestation": attestation}
    bundle = {**unsigned, "bundle_sha256": sha256_bytes(_json_bytes(unsigned))}
    if require_signed:
        validate_bundle(bundle, now=now, verifier=verifier)
    return bundle

def validate_bundle(
    bundle: Any,
    *,
    now: Callable[[], datetime],
    verifier: Callable[[dict[str, Any]], None] | None,
    release_sha256: str | None = None,
    acceptance_tool_sha256: str | None = None,
    gate_contract_revision: str | None = None,
    kube_context: str | None = None,
    cluster_identity_sha256: str | None = None,
    access_profile: str | None = None,
) -> None:
    if verifier is None:
        raise ValueError("deployment continuation signature verifier is required")
    if not isinstance(bundle, dict) or set(bundle) != {
        "record", "record_sha256", "attestation", "bundle_sha256",
    }:
        raise ValueError("deployment continuation bundle is invalid")
    record = bundle.get("record")
    _validate_record(record)
    digest = sha256_bytes(_json_bytes(record))
    unsigned = {key: bundle[key] for key in ("record", "record_sha256", "attestation")}
    if (
        bundle.get("record_sha256") != digest
        or bundle.get("bundle_sha256") != sha256_bytes(_json_bytes(unsigned))
    ):
        raise ValueError("deployment continuation bundle checksum mismatch")
    _validate_attestation(record, digest, bundle.get("attestation"), verifier)
    current = now()
    if current < _parse_utc(record["created_at"]) or current > _parse_utc(record["expires_at"]):
        raise ValueError("deployment continuation expired")
    expected = {
        "release_sha256": (record["replacement"], "product_sha256"),
        "acceptance_tool_sha256": (record["replacement"], "acceptance_tool_sha256"),
        "gate_contract_revision": (record["replacement"], "gate_contract_revision"),
        "kube_context": (record["cluster"], "kube_context"),
        "cluster_identity_sha256": (record["cluster"], "identity_sha256"),
        "access_profile": (record, "access_profile"),
    }
    supplied = locals()
    for name, (owner, field) in expected.items():
        if supplied[name] is not None and owner.get(field) != supplied[name]:
            raise ValueError(f"deployment continuation {name} drifted")

def _validated_reconciliations(
    values: Iterable[dict[str, object]], *, expected: set[str],
) -> list[dict[str, object]]:
    result = list(values)
    if (
        len(result) > 128
        or any(not isinstance(item, dict) or set(item) != _RECONCILIATION_FIELDS for item in result)
    ):
        raise ValueError("deployment continuation reconciliation is invalid")
    operation_ids = [str(item["operation_id"]) for item in result]
    if len(operation_ids) != len(set(operation_ids)) or set(operation_ids) != expected:
        raise ValueError("every issued mutation requires one unique reconciliation")
    for item in result:
        if (
            item["operation_id"] != item["request_id"]
            or item["outcome"] not in {"succeeded", "rejected"}
            or item["unknown_side_effects"] is not False
            or item["irreversible_side_effects"] is not False
            or not _object_facts(item["object_identity"])
            or not _revision_facts(item["revision_identity"])
        ):
            message = (
                "unknown side effects require rebuild"
                if item.get("unknown_side_effects") is not False
                else "irreversible side effects require rebuild"
                if item.get("irreversible_side_effects") is not False
                else "deployment continuation reconciliation is unprovable"
            )
            raise ValueError(message)
        assert_public_payload(item)
    return sorted(result, key=lambda item: str(item["operation_id"]))

def _validate_replacement(value: dict[str, object]) -> None:
    if set(value) != {
        "product_sha256", "acceptance_tool_sha256", "gate_contract_revision",
        "evidence_format_version",
    } or any(
        _SHA256.fullmatch(str(value.get(name, ""))) is None
        for name in ("product_sha256", "acceptance_tool_sha256")
    ) or (
        value.get("evidence_format_version") != EVIDENCE_FORMAT_VERSION
        or value.get("gate_contract_revision") != GATE_CONTRACT_REVISION
    ):
        raise ValueError("replacement freeze identity is invalid")

def _diagnostic_facts(
    path: Path,
    failure: dict[str, Any],
    *,
    rendered_manifest_sha256: str,
    deployment_images_sha256: str,
) -> tuple[str, str, set[str]]:
    manifest = _object(path / "manifest.json", "diagnostic manifest")
    conclusion = _object(path / "conclusion.json", "diagnostic conclusion")
    if (
        manifest.get("format") != "diagnostic_evidence_v1"
        or manifest.get("source_acceptance_id") != failure["acceptance_id"]
        or manifest.get("source_failed_gate") != failure["gate_id"]
        or manifest.get("source_failure_attribution") != failure["failure_attribution"]
        or manifest.get("release_sha256") != failure["product_sha256"]
        or manifest.get("acceptance_tool_sha256") != failure["acceptance_tool_sha256"]
        or manifest.get("rendered_manifest_sha256") != rendered_manifest_sha256
        or manifest.get("deployment_images_sha256") != deployment_images_sha256
    ):
        raise ValueError("diagnostic bundle belongs to another failed run")
    references = conclusion.get("evidence")
    if (
        set(conclusion) != {
            "format", "diagnosed_attribution", "conclusion_note",
            "operation_accounting_complete", "recovered_operation_ids", "evidence",
        }
        or conclusion.get("format") != "diagnostic_conclusion_v1"
        or conclusion.get("diagnosed_attribution") not in _RETAINABLE_ATTRIBUTIONS
        or conclusion.get("operation_accounting_complete") is not True
        or not isinstance(conclusion.get("conclusion_note"), str)
        or not conclusion["conclusion_note"].strip()
        or redact_text(conclusion["conclusion_note"]) != conclusion["conclusion_note"]
        or not isinstance(references, list)
        or not references
        or len(references) > 128
    ):
        raise ValueError("diagnostic bundle does not prove a retainable failure")
    recovered = conclusion.get("recovered_operation_ids")
    if (
        not isinstance(recovered, list)
        or len(recovered) > 128
        or any(not isinstance(item, str) or not valid_operation_id(item) for item in recovered)
        or len(recovered) != len(set(recovered))
    ):
        raise ValueError("diagnostic recovered operation identities are invalid")
    source_operations = set(failure["issued_operation_ids"])
    if source_operations & set(recovered):
        raise ValueError("diagnostic recovered operation identity duplicates source ledger")
    if not source_operations and not recovered:
        raise ValueError("diagnostic operation accounting is unprovable")
    referenced: set[Path] = set()
    for item in references:
        relative = Path(str(item.get("path", ""))) if isinstance(item, dict) else Path()
        candidate = path / relative
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "bytes"}
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() in {"manifest.json", "conclusion.json", "."}
            or candidate.is_symlink()
            or not candidate.is_file()
            or item.get("sha256") != sha256(candidate)
            or item.get("bytes") != candidate.stat().st_size
        ):
            raise ValueError("diagnostic conclusion evidence is invalid")
        _public_diagnostic_fact(candidate)
        referenced.add(candidate)
    if len(referenced) != len(references):
        raise ValueError("diagnostic conclusion evidence is duplicated")
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if (
        not files or len(files) > 512
        or sum(item.stat().st_size for item in files) > 128 * 1024 * 1024
        or any(item.is_symlink() for item in path.rglob("*"))
        or set(files) != referenced | {path / "manifest.json", path / "conclusion.json"}
    ):
        raise ValueError("diagnostic bundle is incomplete or unsafe")
    inventory = [
        {"path": str(item.relative_to(path)), "sha256": sha256(item), "bytes": item.stat().st_size}
        for item in files
    ]
    return (
        sha256_bytes(_json_bytes(inventory)),
        str(conclusion["diagnosed_attribution"]),
        source_operations | set(recovered),
    )


def _validate_record(value: Any) -> None:
    required = {
        "format_version", "epoch_id", "source", "replacement", "cluster",
        "access_profile", "deployment_identity", "diagnosed_attribution", "reconciliations",
        "reusable_gates", "environment_contaminated", "disposition", "created_at", "expires_at",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("format_version") != FORMAT_VERSION:
        raise ValueError("deployment continuation record contract is invalid")
    source = value.get("source")
    replacement = value.get("replacement")
    cluster = value.get("cluster")
    if (
        not _ID.fullmatch(str(value.get("epoch_id", "")))
        or not isinstance(source, dict)
        or set(source) != {
            "acceptance_id", "failed_gate", "seal_sha256", "diagnostic_sha256",
            "product_sha256", "acceptance_tool_sha256", "failure_attribution",
            "rendered_manifest_sha256", "deployment_images_sha256",
            "issued_operation_ids",
        }
        or not _ID.fullmatch(str(source.get("acceptance_id", "")))
        or source.get("failed_gate") not in GATE_SEQUENCE
        or not valid_failure_attribution(source.get("failure_attribution"))
        or any(_SHA256.fullmatch(str(source.get(name, ""))) is None for name in (
            "seal_sha256", "diagnostic_sha256", "product_sha256", "acceptance_tool_sha256",
            "rendered_manifest_sha256", "deployment_images_sha256",
        ))
        or not isinstance(source.get("issued_operation_ids"), list)
        or not source["issued_operation_ids"]
        or len(source["issued_operation_ids"]) > 128
        or any(
            not isinstance(item, str) or not valid_operation_id(item)
            for item in source["issued_operation_ids"]
        )
        or len(source["issued_operation_ids"]) != len(set(source["issued_operation_ids"]))
        or not isinstance(replacement, dict)
        or source.get("failure_attribution") == "product_failure"
        or source.get("product_sha256") != replacement.get("product_sha256")
        or source.get("acceptance_tool_sha256") == replacement.get("acceptance_tool_sha256")
        or not isinstance(cluster, dict)
        or set(cluster) != {"kube_context", "identity_sha256"}
        or not isinstance(cluster.get("kube_context"), str)
        or not 1 <= len(cluster["kube_context"]) <= 512
        or _SHA256.fullmatch(str(cluster.get("identity_sha256", ""))) is None
        or value.get("access_profile") not in {"http_nodeport", "https_ingress"}
        or value.get("diagnosed_attribution") not in _RETAINABLE_ATTRIBUTIONS
        or value.get("environment_contaminated") is not False
        or value.get("disposition") != "retain_existing"
        or not isinstance(value.get("reconciliations"), list)
        or _parse_utc(str(value.get("created_at", ""))) > _parse_utc(str(value.get("expires_at", "")))
    ):
        raise ValueError("deployment continuation record contract is invalid")
    _validate_replacement(replacement)
    _validate_deployment_identity(
        value["deployment_identity"],
        product_sha256=str(source["product_sha256"]),
        cluster_identity_sha256=str(cluster["identity_sha256"]),
        rendered_manifest_sha256=str(source["rendered_manifest_sha256"]),
        deployment_images_sha256=str(source["deployment_images_sha256"]),
    )
    _validated_reconciliations(
        value["reconciliations"],
        expected=set(source["issued_operation_ids"]),
    )
    validate_reusable_gates(
        value["reusable_gates"], failed_gate=source["failed_gate"],
        reconciled=set(source["issued_operation_ids"]),
    )
    assert_public_payload(value)


def _validate_attestation(
    record: dict[str, Any], record_sha: str, item: Any,
    verifier: Callable[[dict[str, Any]], None] | None,
) -> None:
    statement = item.get("statement") if isinstance(item, dict) else None
    expected = {
        "epoch_id": record["epoch_id"], "record_sha256": record_sha,
        "source": record["source"], "replacement": record["replacement"],
        "cluster": record["cluster"], "access_profile": record["access_profile"],
        "deployment_identity": record["deployment_identity"],
        "reusable_gates": record["reusable_gates"],
        "disposition": "retain_existing", "created_at": record["created_at"],
        "expires_at": record["expires_at"], "role": "platform_operator",
        "conclusion": "retain_existing_and_reuse_exact_gates",
    }
    if (
        not isinstance(item, dict) or set(item) != _SIGNED_FIELDS
        or not isinstance(statement, dict)
        or any(statement.get(key) != value for key, value in expected.items())
        or not all(isinstance(statement.get(key), str) and statement[key] for key in (
            "actor", "signed_at", "note",
        ))
        or not (
            _parse_utc(record["created_at"])
            <= _parse_utc(statement["signed_at"])
            <= _parse_utc(record["expires_at"])
        )
        or signature_identity_error(
            item.get("signature"), item.get("public_key"), item.get("fingerprint"),
        )
    ):
        raise ValueError("deployment continuation attestation contract is invalid")
    if verifier is not None:
        try:
            verifier(item)
        except Exception as exc:
            raise ValueError("deployment continuation signature is invalid") from exc


def _fact_map(value: object) -> bool:
    return isinstance(value, dict) and 0 < len(value) <= 32 and all(
        isinstance(key, str) and 0 < len(key) <= 128
        and isinstance(item, (str, int, float))
        and (not isinstance(item, str) or len(item) <= 2048)
        for key, item in value.items()
    )


def _object_facts(value: object) -> bool:
    return _fact_map(value) and any(
        key == "id" or key.endswith((".id", "_id")) for key in value
    )


def _revision_facts(value: object) -> bool:
    return _fact_map(value) and any(
        key in {"revision", "revision_id", "updated_at", "sequence"}
        or key.endswith((
            ".revision", "_revision", ".revision_id", "_revision_id",
            ".updated_at", "_updated_at", ".sequence", "_sequence",
        ))
        for key in value
    )


def _validate_deployment_identity(
    value: object,
    *,
    product_sha256: str | None = None,
    cluster_identity_sha256: str | None = None,
    rendered_manifest_sha256: str | None = None,
    deployment_images_sha256: str | None = None,
) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _DEPLOYMENT_IDENTITY_FIELDS
        or any(
            _SHA256.fullmatch(str(value.get(name, ""))) is None
            for name in (
                "product_sha256", "cluster_identity_sha256",
                "rendered_manifest_sha256", "deployment_images_sha256",
                "deployment_configuration_sha256", "health_snapshot_sha256",
                "manifest_diff_sha256",
            )
        )
        or (
            value.get("manifest_diff_exit_code"),
            value.get("manifest_diff_server_generation_only"),
        ) not in {(0, False), (1, True)}
        or value.get("healthy") is not True
        or not isinstance(value.get("observed_at"), str)
        or (product_sha256 is not None and value.get("product_sha256") != product_sha256)
        or (
            cluster_identity_sha256 is not None
            and value.get("cluster_identity_sha256") != cluster_identity_sha256
        )
        or (
            rendered_manifest_sha256 is not None
            and value.get("rendered_manifest_sha256") != rendered_manifest_sha256
        )
        or (
            deployment_images_sha256 is not None
            and value.get("deployment_images_sha256") != deployment_images_sha256
        )
    ):
        raise ValueError("deployment identity is invalid or drifted")
    _parse_utc(value["observed_at"])
    assert_public_payload(value)


def _source_deployment_baselines(
    source: Any, *, required: bool,
) -> tuple[str | None, str | None]:
    p01_passed = any(
        item["gate_id"] == "P01" and item["status"] == "passed"
        for item in source.completed_artifact_index()
    )
    if not p01_passed:
        if required:
            raise ValueError("deployment continuation requires a passed P01 baseline")
        return None, None
    inventory = _artifact_json(source, "artifact-inventory.json")
    images = _artifact_json(source, "image-list.json")
    manifest = [
        item for item in inventory
        if isinstance(item, dict) and item.get("path") == "manifest.yaml"
    ] if isinstance(inventory, list) else []
    if (
        len(manifest) != 1
        or set(manifest[0]) != {"path", "sha256", "bytes"}
        or _SHA256.fullmatch(str(manifest[0].get("sha256", ""))) is None
        or not isinstance(images, list)
        or not images
        or any(not isinstance(item, str) or not item for item in images)
        or len(images) != len(set(images))
    ):
        raise ValueError("source P01 deployment baseline is invalid")
    return str(manifest[0]["sha256"]), sha256_bytes(_json_bytes(sorted(images)))


def _artifact_json(source: Any, name: str) -> Any:
    artifact = source.passed_artifact("P01", name)
    try:
        return json.loads(artifact.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"source P01 {name} is invalid") from exc


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _public_diagnostic_fact(path: Path) -> None:
    value = _object(path, "diagnostic evidence")
    assert_public_payload(value)
    if redact_json(value) != value:
        raise ValueError("diagnostic evidence is not redacted public JSON")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("deployment continuation timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("deployment continuation timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
