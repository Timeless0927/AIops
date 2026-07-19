"""Signed deployment handoff after a diagnosed Acceptance Runner failure."""
from __future__ import annotations
import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .credentials import assert_public_payload
from .evidence_files import atomic_write, sha256, sha256_bytes
from .environment_qualification_record import validate_bundle as validate_qualification
from .gate_contract import EVIDENCE_FORMAT_VERSION, GATE_CONTRACT_REVISION
from .human_attestation import signature_identity_error
from .freeze import verify_final_checksums

FORMAT_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ATTRIBUTIONS = {
    "product_failure", "tool_failure", "environment_failure", "inconclusive",
}
_SIGNED_FIELDS = {"statement", "signature", "public_key", "fingerprint"}
_RECONCILIATION_FIELDS = {
    "operation_id", "request_id", "object_identity", "revision_identity", "outcome",
    "unknown_side_effects", "irreversible_side_effects",
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
    validate_bundle(
        deployment_continuation, now=moment, verifier=verifier, **identity,
    )
    return "adopt_existing", deployment_continuation


def create_diagnostic_bundle(
    source: Any,
    parent: Path,
    *,
    diagnostic_id: str,
    diagnosed_attribution: str,
    conclusion_note: str,
) -> Path:
    """Create immutable troubleshooting evidence without touching the source ledger."""
    if not _ID.fullmatch(diagnostic_id):
        raise ValueError("diagnostic_id contains unsupported characters")
    if (
        not valid_failure_attribution(diagnosed_attribution)
        or not 1 <= len(conclusion_note.strip()) <= 2048
    ):
        raise ValueError("diagnostic conclusion is invalid")
    failure = source.failure_summary()
    root = parent / diagnostic_id
    root.mkdir(parents=True, exist_ok=False)
    manifest = {
        "format": "diagnostic_evidence_v1",
        "diagnostic_id": diagnostic_id,
        "source_acceptance_id": failure["acceptance_id"],
        "source_failed_gate": failure["gate_id"],
        "source_failure_attribution": failure["failure_attribution"],
        "diagnosed_attribution": diagnosed_attribution,
        "conclusion_note": conclusion_note,
        "release_sha256": failure["product_sha256"],
        "acceptance_tool_sha256": failure["acceptance_tool_sha256"],
        "created_at": source.now(),
    }
    assert_public_payload(manifest)
    atomic_write(
        root / "manifest.json", _json_bytes(manifest), staging_dir=parent,
    )
    return root


class DeploymentContinuation:
    """Own checksummed, Platform-Operator-signed continuation epochs."""

    def __init__(
        self,
        root: Path,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.root = root
        self.now = now

    def create(
        self,
        *,
        epoch_id: str,
        source: Any,
        diagnostic: Path,
        replacement: dict[str, object],
        reconciliations: Iterable[dict[str, object]],
        ttl_seconds: int = 3600,
    ) -> Path:
        if not _ID.fullmatch(epoch_id):
            raise ValueError("epoch_id contains unsupported characters")
        if not 60 <= ttl_seconds <= 86400:
            raise ValueError("continuation epoch TTL is invalid")
        failure = source.failure_summary()
        if source.status().get("status") != "sealed" or failure.get("decision") != "no_promote":
            raise ValueError("continuation requires a sealed no-promote source ledger")
        if failure["failure_attribution"] not in {"inconclusive", "tool_failure"}:
            raise ValueError("only a diagnosed Acceptance Runner failure may retain deployment")
        _validate_replacement(replacement)
        if replacement["product_sha256"] != failure["product_sha256"]:
            raise ValueError("product identity changed; rebuild required")
        if replacement["acceptance_tool_sha256"] == failure["acceptance_tool_sha256"]:
            raise ValueError("continuation requires a replacement Acceptance Runner freeze")
        diagnostic_digest = _diagnostic_sha256(diagnostic, failure)
        reconciled = _validated_reconciliations(
            reconciliations, expected=set(failure["issued_operation_ids"]),
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
            },
            "replacement": dict(replacement),
            "cluster": {
                "kube_context": failure["kube_context"],
                "identity_sha256": failure["cluster_identity_sha256"],
            },
            "access_profile": failure["access_profile"],
            "diagnosed_attribution": "tool_failure",
            "reconciliations": reconciled,
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
            "disposition": record["disposition"],
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "actor": actor,
            "role": "platform_operator",
            "conclusion": "retain_existing",
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


def _diagnostic_sha256(path: Path, failure: dict[str, Any]) -> str:
    manifest = _object(path / "manifest.json", "diagnostic manifest")
    if (
        manifest.get("format") != "diagnostic_evidence_v1"
        or manifest.get("source_acceptance_id") != failure["acceptance_id"]
        or manifest.get("source_failed_gate") != failure["gate_id"]
        or manifest.get("source_failure_attribution") != failure["failure_attribution"]
        or manifest.get("diagnosed_attribution") != "tool_failure"
        or not isinstance(manifest.get("conclusion_note"), str)
        or not manifest["conclusion_note"].strip()
        or manifest.get("release_sha256") != failure["product_sha256"]
        or manifest.get("acceptance_tool_sha256") != failure["acceptance_tool_sha256"]
    ):
        raise ValueError("diagnostic bundle does not prove an Acceptance Runner failure")
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if (
        not files or len(files) > 512
        or sum(item.stat().st_size for item in files) > 128 * 1024 * 1024
        or any(item.is_symlink() for item in path.rglob("*"))
    ):
        raise ValueError("diagnostic bundle is incomplete or unsafe")
    inventory = [
        {"path": str(item.relative_to(path)), "sha256": sha256(item), "bytes": item.stat().st_size}
        for item in files
    ]
    return sha256_bytes(_json_bytes(inventory))


def _validate_record(value: Any) -> None:
    required = {
        "format_version", "epoch_id", "source", "replacement", "cluster",
        "access_profile", "diagnosed_attribution", "reconciliations",
        "environment_contaminated", "disposition", "created_at", "expires_at",
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
        }
        or not valid_failure_attribution(source.get("failure_attribution"))
        or any(_SHA256.fullmatch(str(source.get(name, ""))) is None for name in (
            "seal_sha256", "diagnostic_sha256", "product_sha256", "acceptance_tool_sha256",
        ))
        or not isinstance(replacement, dict)
        or source.get("failure_attribution") not in {"inconclusive", "tool_failure"}
        or source.get("product_sha256") != replacement.get("product_sha256")
        or source.get("acceptance_tool_sha256") == replacement.get("acceptance_tool_sha256")
        or not isinstance(cluster, dict)
        or set(cluster) != {"kube_context", "identity_sha256"}
        or _SHA256.fullmatch(str(cluster.get("identity_sha256", ""))) is None
        or value.get("access_profile") not in {"http_nodeport", "https_ingress"}
        or value.get("diagnosed_attribution") != "tool_failure"
        or value.get("environment_contaminated") is not False
        or value.get("disposition") != "retain_existing"
        or not isinstance(value.get("reconciliations"), list)
        or _parse_utc(str(value.get("created_at", ""))) > _parse_utc(str(value.get("expires_at", "")))
    ):
        raise ValueError("deployment continuation record contract is invalid")
    _validate_replacement(replacement)
    _validated_reconciliations(
        value["reconciliations"],
        expected={str(item["operation_id"]) for item in value["reconciliations"]},
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
        "disposition": "retain_existing", "created_at": record["created_at"],
        "expires_at": record["expires_at"], "role": "platform_operator",
        "conclusion": "retain_existing",
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


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


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
