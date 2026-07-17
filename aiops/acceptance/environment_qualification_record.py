"""Checksummed Environment Qualification record and attestation contract."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .command import CommandExecutor
from .credentials import assert_public_payload
from .evidence_files import atomic_write, sha256, sha256_bytes
from .human_attestation import signature_identity_error


FORMAT_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SIGNED_FIELDS = {"statement", "signature", "public_key", "fingerprint"}


def write_record(path: Path, record: dict[str, Any]) -> None:
    assert_public_payload(record)
    content = _json_bytes(record)
    atomic_write(path / "record.json", content, staging_dir=path)
    atomic_write(
        path / "SHA256SUMS",
        f"{sha256_bytes(content)}  record.json\n".encode(),
        staging_dir=path,
    )


def inspect_record(
    path: Path,
    *,
    require_signed: bool = False,
    verifier: Callable[[dict[str, Any]], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    record_path = path / "record.json"
    checksum_path = path / "SHA256SUMS"
    if record_path.is_symlink() or not record_path.is_file() or not checksum_path.is_file():
        raise ValueError("environment qualification record is incomplete")
    digest = sha256(record_path)
    if checksum_path.read_text(encoding="utf-8") != f"{digest}  record.json\n":
        raise ValueError("environment qualification checksum mismatch")
    record = _object(record_path, "environment qualification record")
    _validate_record(record)
    attestation_path = path / "attestation.json"
    attestation = _object(attestation_path, "environment qualification attestation") if attestation_path.is_file() else None
    if require_signed and attestation is None:
        raise ValueError("passed environment qualification is not signed")
    if attestation is not None:
        _validate_attestation(record, digest, attestation, verifier)
    bundle = {"record": record, "record_sha256": digest, "attestation": attestation}
    bundle["bundle_sha256"] = sha256_bytes(_json_bytes(bundle))
    if require_signed:
        validate_bundle(bundle, now=now, verifier=verifier)
    return bundle


def attestation_statement(
    path: Path,
    *,
    actor: str,
    note: str,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    bundle = inspect_record(path)
    record = bundle["record"]
    if record["outcome"] != "passed":
        raise ValueError("only a passed environment qualification may be signed")
    statement = {
        "qualification_id": record["qualification_id"],
        "record_sha256": bundle["record_sha256"],
        "freeze": record["freeze"],
        "cluster": record["cluster"],
        "access_profile": record["access_profile"],
        "observed_at": record["observed_at"],
        "expires_at": record["expires_at"],
        "actor": actor,
        "role": "platform_operator",
        "conclusion": "passed",
        "signed_at": _iso(now()),
        "note": note,
    }
    assert_public_payload(statement)
    return statement


def attach_attestation(path: Path, item: dict[str, Any]) -> None:
    if (path / "attestation.json").exists():
        raise ValueError("environment qualification is already signed")
    bundle = inspect_record(path)
    _validate_attestation(bundle["record"], bundle["record_sha256"], item, None)
    atomic_write(path / "attestation.json", _json_bytes(item), staging_dir=path)


def resume_cleanup(path: Path, *, commands: CommandExecutor) -> dict[str, Any]:
    bundle = inspect_record(path)
    record = bundle["record"]
    operation = record["operation"]
    if record["outcome"] != "running" or operation.get("status") != "dispatched":
        raise ValueError("environment qualification has no interrupted cleanup")
    cleanup = operation.get("cleanup")
    if cleanup is None:
        cleanup = {
            "id": f"{operation['id']}:cleanup", "status": "planned",
        }
        operation["cleanup"] = cleanup
        write_record(path, record)
    namespace = record["temporary_namespace"]
    observed = commands.run(
        ["kubectl", "get", "namespace", namespace, "-o", "name"], timeout=30,
    )
    absent = observed.exit_code != 0 and "NotFound" in observed.stderr
    exit_code: int | None = None
    if not absent and observed.exit_code == 0 and cleanup.get("status") == "planned":
        cleanup["status"] = "dispatched"
        write_record(path, record)
        deleted = commands.run(
            ["kubectl", "delete", "namespace", namespace, "--wait=true", "--timeout=5m"],
            timeout=330,
        )
        exit_code = deleted.exit_code
        absent = deleted.exit_code == 0 or "NotFound" in deleted.stderr
    cleanup["status"] = "terminal"
    operation["status"] = "terminal"
    record["cleanup"] = {"namespace_absent": absent, "exit_code": exit_code}
    record["outcome"] = "environment_not_ready"
    record["failure"] = (
        "interrupted qualification cleanup reconciled"
        if absent else "interrupted qualification cleanup could not be proved"
    )
    write_record(path, record)
    return inspect_record(path)


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
        raise ValueError("environment qualification signature verifier is required")
    if not isinstance(bundle, dict) or set(bundle) != {
        "record", "record_sha256", "attestation", "bundle_sha256"
    }:
        raise ValueError("environment qualification bundle is invalid")
    record = bundle.get("record")
    _validate_record(record)
    record_sha = sha256_bytes(_json_bytes(record))
    unsigned = {key: bundle[key] for key in ("record", "record_sha256", "attestation")}
    if (
        bundle.get("record_sha256") != record_sha
        or bundle.get("bundle_sha256") != sha256_bytes(_json_bytes(unsigned))
        or record["outcome"] != "passed"
        or record["operation"].get("status") != "terminal"
        or record["cleanup"] != {"namespace_absent": True, "exit_code": 0}
    ):
        raise ValueError("environment qualification did not pass cleanly")
    _validate_attestation(record, record_sha, bundle.get("attestation"), verifier)
    observed = _parse_utc(record["observed_at"])
    current = now()
    if current < observed or current > _parse_utc(record["expires_at"]):
        raise ValueError("environment qualification expired")
    expected = {
        "release_sha256": (record["freeze"], "product_sha256"),
        "acceptance_tool_sha256": (record["freeze"], "acceptance_tool_sha256"),
        "gate_contract_revision": (record["freeze"], "gate_contract_revision"),
        "kube_context": (record["cluster"], "kube_context"),
        "cluster_identity_sha256": (record["cluster"], "identity_sha256"),
        "access_profile": (record, "access_profile"),
    }
    supplied = locals()
    for name, (owner, field) in expected.items():
        value = supplied[name]
        if value is not None and owner.get(field) != value:
            raise ValueError(f"environment qualification {name} drifted")


def _validate_record(value: Any) -> None:
    if not isinstance(value, dict) or value.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported_environment_qualification_format")
    required = {
        "format_version", "qualification_id", "freeze", "cluster", "access_profile",
        "temporary_namespace", "operation", "facts", "cleanup", "outcome", "failure",
        "observed_at", "expires_at",
    }
    freeze = value.get("freeze")
    cluster = value.get("cluster")
    if (
        set(value) != required
        or not isinstance(freeze, dict)
        or set(freeze) != {
            "record_sha256", "product_sha256", "acceptance_tool_sha256",
            "gate_contract_revision", "evidence_format_version",
            "environment_qualification_format_version",
        }
        or any(_SHA256.fullmatch(str(freeze.get(name, ""))) is None for name in (
            "record_sha256", "product_sha256", "acceptance_tool_sha256",
        ))
        or freeze.get("evidence_format_version") != 3
        or freeze.get("environment_qualification_format_version") != FORMAT_VERSION
        or not isinstance(cluster, dict)
        or set(cluster) != {"kube_context", "identity_sha256"}
        or _SHA256.fullmatch(str(cluster.get("identity_sha256", ""))) is None
        or value.get("access_profile") not in {"http_nodeport", "https_ingress"}
        or value.get("outcome") not in {"running", "passed", "environment_not_ready"}
        or not isinstance(value.get("operation"), dict)
        or not isinstance(value.get("facts"), dict)
        or not isinstance(value.get("cleanup"), dict)
        or _parse_utc(str(value.get("observed_at", ""))) > _parse_utc(str(value.get("expires_at", "")))
    ):
        raise ValueError("environment qualification record contract is invalid")
    assert_public_payload(value)


def _validate_attestation(
    record: dict[str, Any],
    record_sha: str,
    item: Any,
    verifier: Callable[[dict[str, Any]], None] | None,
) -> None:
    if not isinstance(item, dict) or set(item) != _SIGNED_FIELDS:
        raise ValueError("environment qualification attestation is invalid")
    statement = item.get("statement")
    if (
        not isinstance(statement, dict)
        or statement.get("qualification_id") != record["qualification_id"]
        or statement.get("record_sha256") != record_sha
        or statement.get("freeze") != record["freeze"]
        or statement.get("cluster") != record["cluster"]
        or statement.get("access_profile") != record["access_profile"]
        or statement.get("observed_at") != record["observed_at"]
        or statement.get("expires_at") != record["expires_at"]
        or statement.get("role") != "platform_operator"
        or statement.get("conclusion") != "passed"
        or not (
            _parse_utc(record["observed_at"])
            <= _parse_utc(str(statement.get("signed_at", "")))
            <= _parse_utc(record["expires_at"])
        )
        or not isinstance(statement.get("actor"), str) or not statement["actor"]
        or not isinstance(statement.get("note"), str) or not statement["note"]
        or signature_identity_error(
            item.get("signature"), item.get("public_key"), item.get("fingerprint")
        )
    ):
        raise ValueError("environment qualification attestation contract is invalid")
    if verifier is not None:
        try:
            verifier(item)
        except Exception as exc:
            raise ValueError("environment qualification signature is invalid") from exc


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("environment qualification timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("environment qualification timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
