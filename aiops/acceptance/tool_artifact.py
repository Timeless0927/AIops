"""Deterministic Acceptance Tool source bundle and admission self-check."""

from __future__ import annotations

import gzip
import io
import json
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .credentials import assert_public_payload
from .evidence_files import atomic_write, sha256, sha256_bytes
from .environment_qualification_record import (
    FORMAT_VERSION as ENVIRONMENT_QUALIFICATION_FORMAT_VERSION,
)
from .gate_contract import EVIDENCE_FORMAT_VERSION, GATE_CONTRACT_REVISION
from .human_attestation import signature_identity_error


TOOL_FORMAT_VERSION = 2
ADMISSION_FORMAT_VERSION = 3
TOOL_ROOT = "aiops-acceptance-tool"
SELF_CHECK_ID = "aiops-acceptance-tool-self-check-v2"
REQUIRED_CHECKS = (
    "owner_tests", "direct_consumers", "static_checks",
    "dag_simulation", "standards_review", "spec_review",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPORT_FIELDS = {
    "format_version", "release_sha256", "gate_contract_revision",
    "evidence_format_version", "environment_qualification_format_version",
    "fixed_point", "release",
    "source_inventory_sha256", "checks", "invalidation_rule", "live_evidence",
}
_CHECK_FIELDS = {"sha256", "report"}
_FIXED_POINT_FIELDS = {"pre_f10_commit", "reviewed_commit", "reviewed_tree"}
_RELEASE_FIELDS = {
    "openapi", "images_sha256", "config_revisions_sha256", "defaults_sha256",
}
_OPENAPI_FIELDS = {"api_version", "producer_sha256", "console_consumer_sha256"}
_SIGNED_FIELDS = {"statement", "signature", "public_key", "fingerprint"}
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_BROWSER_SCRIPTS = (
    "pilot_acceptance_browser.mjs",
    "pilot_acceptance_governed_change.mjs",
    "pilot_acceptance_report.mjs",
    "pilot_acceptance_v01_browser.mjs",
)
_BROWSER_MODULES = ("playwright", "playwright-core")
_BROWSER_RUNTIME_FIELDS = {
    "playwright_version", "browser_version", "chromium_sha256", "chromium_bytes",
}
INVALIDATION_RULE = (
    "Any source, manifest, image, default, admission, product artifact or "
    "acceptance-tool artifact change invalidates the freeze and blocks Clean Acceptance."
)


def build_acceptance_tool(
    source_root: Path,
    signed_admission: dict[str, Any],
    output: Path,
    *,
    verifier: Callable[[dict[str, Any]], None],
) -> Path:
    """Build one deterministic source archive after admission signature verification."""
    _validate_admission(signed_admission, verifier)
    sources = _source_files(source_root)
    entries = {
        f"{TOOL_ROOT}/source/{relative}": path.read_bytes()
        for relative, path in sources
    }
    inventory = _inventory(entries, prefix=f"{TOOL_ROOT}/source/")
    source_digest = sha256_bytes(_json_bytes(inventory))
    browser_runtime = _browser_runtime_identity(source_root)
    admission_bytes = _json_bytes(signed_admission)
    manifest = {
        "format_version": TOOL_FORMAT_VERSION,
        "evidence_format_version": EVIDENCE_FORMAT_VERSION,
        "environment_qualification_format_version": ENVIRONMENT_QUALIFICATION_FORMAT_VERSION,
        "gate_contract_revision": GATE_CONTRACT_REVISION,
        "source_sha256": source_digest,
        "browser_runtime": browser_runtime,
        "admission_report_sha256": sha256_bytes(admission_bytes),
        "self_check": {
            "id": SELF_CHECK_ID,
            "evidence_format_version": EVIDENCE_FORMAT_VERSION,
            "environment_qualification_format_version": ENVIRONMENT_QUALIFICATION_FORMAT_VERSION,
            "gate_contract_revision": GATE_CONTRACT_REVISION,
            "source_sha256": source_digest,
        },
    }
    entries[f"{TOOL_ROOT}/manifest.json"] = _json_bytes(manifest)
    entries[f"{TOOL_ROOT}/admission-report.json"] = admission_bytes
    checksums = "".join(
        f"{sha256_bytes(content)}  {name}\n"
        for name, content in sorted(entries.items())
    ).encode()
    entries[f"{TOOL_ROOT}/SHA256SUMS"] = checksums
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, _archive(entries), staging_dir=output.parent)
    return output


def inspect_acceptance_tool(path: Path) -> dict[str, Any]:
    entries = _read_archive(path)
    checksum_name = f"{TOOL_ROOT}/SHA256SUMS"
    manifest_name = f"{TOOL_ROOT}/manifest.json"
    admission_name = f"{TOOL_ROOT}/admission-report.json"
    if not all(name in entries for name in (checksum_name, manifest_name, admission_name)):
        raise ValueError("acceptance-tool archive is incomplete")
    expected = _checksum_entries(entries[checksum_name])
    actual = {
        name: sha256_bytes(content)
        for name, content in entries.items() if name != checksum_name
    }
    if expected != actual:
        raise ValueError("acceptance-tool internal checksum mismatch")
    manifest = _json_object(entries[manifest_name], "manifest")
    admission = _json_object(entries[admission_name], "admission report")
    source_entries = {
        name: content for name, content in entries.items()
        if name.startswith(f"{TOOL_ROOT}/source/")
    }
    metadata_names = {checksum_name, manifest_name, admission_name}
    if set(entries) != metadata_names | set(source_entries) or not source_entries:
        raise ValueError("acceptance-tool archive contains an undeclared entry")
    inventory = _inventory(source_entries, prefix=f"{TOOL_ROOT}/source/")
    source_paths = {item["path"] for item in inventory}
    if any(not _declared_source(path) for path in source_paths):
        raise ValueError("acceptance-tool archive contains an undeclared entry")
    source_digest = sha256_bytes(_json_bytes(inventory))
    browser_runtime = manifest.get("browser_runtime")
    _validate_browser_runtime_identity(browser_runtime)
    expected_manifest = {
        "format_version": TOOL_FORMAT_VERSION,
        "evidence_format_version": EVIDENCE_FORMAT_VERSION,
        "environment_qualification_format_version": ENVIRONMENT_QUALIFICATION_FORMAT_VERSION,
        "gate_contract_revision": GATE_CONTRACT_REVISION,
        "source_sha256": source_digest,
        "browser_runtime": browser_runtime,
        "admission_report_sha256": sha256_bytes(entries[admission_name]),
        "self_check": {
            "id": SELF_CHECK_ID,
            "evidence_format_version": EVIDENCE_FORMAT_VERSION,
            "environment_qualification_format_version": ENVIRONMENT_QUALIFICATION_FORMAT_VERSION,
            "gate_contract_revision": GATE_CONTRACT_REVISION,
            "source_sha256": source_digest,
        },
    }
    required_sources = {
        "aiops/acceptance/conductor.py",
        "aiops/acceptance/deployment_continuation.py",
        "aiops/acceptance/evidence.py",
        "aiops/acceptance/evidence_creation.py",
        "aiops/acceptance/environment_qualification.py",
        "aiops/acceptance/environment_qualification_record.py",
        "aiops/acceptance/gate_contract.py",
        "aiops/acceptance/gate_reuse.py",
        "aiops/acceptance/tool_artifact.py",
        "scripts/build_pilot_release.py",
        "scripts/freeze_pilot_release.py",
        "scripts/run_pilot_acceptance.py",
        *{f"scripts/{name}" for name in _BROWSER_SCRIPTS},
        "apps/aiops_console_web/package.json",
        "apps/aiops_console_web/node_modules/playwright/package.json",
        "apps/aiops_console_web/node_modules/playwright-core/package.json",
    }
    if manifest != expected_manifest or not required_sources <= source_paths:
        raise ValueError("acceptance-tool manifest or source inventory is invalid")
    return {"manifest": manifest, "admission": admission, "source_inventory": inventory}


def self_check(
    path: Path,
    *,
    release_sha256: str,
    acceptance_tool_sha256: str,
    gate_contract_revision: str,
    verifier: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    if sha256(path) != acceptance_tool_sha256:
        raise ValueError("acceptance-tool archive does not match the ledger identity")
    inspected = inspect_acceptance_tool(path)
    admission = inspected["admission"]
    _validate_admission(admission, verifier)
    if admission["statement"]["release_sha256"] != release_sha256:
        raise ValueError("F10 admission report does not match the Pilot Release Bundle")
    manifest = inspected["manifest"]
    if manifest["gate_contract_revision"] != gate_contract_revision:
        raise ValueError("acceptance-tool gate contract does not match the ledger")
    with tempfile.TemporaryDirectory(prefix="aiops-acceptance-tool-") as temporary:
        with tarfile.open(path, "r:gz") as bundle:
            bundle.extractall(temporary, filter="data")
        browser_runtime = _browser_runtime_identity(
            Path(temporary) / TOOL_ROOT / "source"
        )
    if browser_runtime != manifest["browser_runtime"]:
        raise ValueError("acceptance-tool browser runtime identity drifted")
    return {
        "id": manifest["self_check"]["id"],
        "acceptance_tool_sha256": acceptance_tool_sha256,
        "source_sha256": manifest["source_sha256"],
        "browser_runtime": browser_runtime,
        "admission_report_sha256": manifest["admission_report_sha256"],
        "admission_checks": {
            name: admission["statement"]["checks"][name]["sha256"]
            for name in REQUIRED_CHECKS
        },
        "fixed_point": admission["statement"]["fixed_point"],
        "source_inventory_sha256": admission["statement"]["source_inventory_sha256"],
        "invalidation_rule": admission["statement"]["invalidation_rule"],
        "release_sha256": release_sha256,
        "gate_contract_revision": manifest["gate_contract_revision"],
        "evidence_format_version": manifest["evidence_format_version"],
        "environment_qualification_format_version": manifest[
            "environment_qualification_format_version"
        ],
        "live_evidence": False,
    }


def _validate_admission(
    value: Any, verifier: Callable[[dict[str, Any]], None],
) -> None:
    if not isinstance(value, dict) or set(value) != _SIGNED_FIELDS:
        raise ValueError("F10 admission report signature envelope is invalid")
    statement = value.get("statement")
    checks = statement.get("checks") if isinstance(statement, dict) else None
    fixed_point = statement.get("fixed_point") if isinstance(statement, dict) else None
    release = statement.get("release") if isinstance(statement, dict) else None
    if (
        not isinstance(statement, dict)
        or set(statement) != _REPORT_FIELDS
        or statement.get("format_version") != ADMISSION_FORMAT_VERSION
        or statement.get("evidence_format_version") != EVIDENCE_FORMAT_VERSION
        or statement.get("environment_qualification_format_version")
        != ENVIRONMENT_QUALIFICATION_FORMAT_VERSION
        or statement.get("gate_contract_revision") != GATE_CONTRACT_REVISION
        or _SHA256.fullmatch(str(statement.get("release_sha256", ""))) is None
        or _SHA256.fullmatch(str(statement.get("source_inventory_sha256", ""))) is None
        or not isinstance(fixed_point, dict)
        or set(fixed_point) != _FIXED_POINT_FIELDS
        or any(re.fullmatch(r"[0-9a-f]{40,64}", str(value)) is None for value in fixed_point.values())
        or not isinstance(release, dict)
        or set(release) != _RELEASE_FIELDS
        or not isinstance(release.get("openapi"), dict)
        or set(release["openapi"]) != _OPENAPI_FIELDS
        or not isinstance(release["openapi"].get("api_version"), str)
        or not release["openapi"]["api_version"]
        or any(
            _SHA256.fullmatch(str(release["openapi"].get(field, ""))) is None
            for field in _OPENAPI_FIELDS - {"api_version"}
        )
        or any(
            _SHA256.fullmatch(str(release.get(field, ""))) is None
            for field in _RELEASE_FIELDS - {"openapi"}
        )
        or statement.get("invalidation_rule") != INVALIDATION_RULE
        or statement.get("live_evidence") is not False
        or not _valid_checks(checks)
        or signature_identity_error(
            value.get("signature"), value.get("public_key"), value.get("fingerprint"),
        )
    ):
        raise ValueError("F10 admission report contract is invalid")
    assert_public_payload(statement)
    try:
        verifier(value)
    except Exception as exc:
        raise ValueError("F10 admission report signature is invalid") from exc


def _valid_checks(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != set(REQUIRED_CHECKS):
        return False
    for check in value.values():
        if not isinstance(check, dict) or set(check) != _CHECK_FIELDS:
            return False
        report = check.get("report")
        if (
            not isinstance(report, dict)
            or report.get("status") != "passed"
            or not isinstance(report.get("command"), str)
            or not report["command"]
            or not isinstance(report.get("summary"), str)
            or not report["summary"]
            or not isinstance(report.get("details"), dict)
            or not report["details"]
            or _SHA256.fullmatch(str(check.get("sha256", ""))) is None
            or check["sha256"] != sha256_bytes(_json_bytes(report))
        ):
            return False
    return True


def _source_files(source_root: Path) -> list[tuple[str, Path]]:
    acceptance = source_root / "aiops/acceptance"
    scripts = [
        source_root / "scripts/build_pilot_release.py",
        source_root / "scripts/freeze_pilot_release.py",
        source_root / "scripts/run_pilot_acceptance.py",
    ]
    browser_root = source_root / "apps/aiops_console_web"
    browser_scripts = [source_root / "scripts" / name for name in _BROWSER_SCRIPTS]
    browser_runtime = [browser_root / "package.json"]
    for name in _BROWSER_MODULES:
        browser_runtime.extend(
            path for path in sorted((browser_root / "node_modules" / name).rglob("*"))
            if path.is_file()
        )
    paths = [
        *sorted(acceptance.glob("*.py")), *scripts, *browser_scripts, *browser_runtime,
    ]
    if (
        not acceptance.is_dir() or acceptance.is_symlink()
        or any(path.is_symlink() or not path.is_file() for path in paths)
    ):
        raise ValueError("acceptance-tool source tree is incomplete or unsafe")
    return [(str(path.relative_to(source_root)), path) for path in paths]


def _declared_source(path: str) -> bool:
    if path.startswith("aiops/acceptance/"):
        return Path(path).parent == Path("aiops/acceptance") and path.endswith(".py")
    if path in {
        "scripts/build_pilot_release.py",
        "scripts/freeze_pilot_release.py",
        "scripts/run_pilot_acceptance.py",
        "apps/aiops_console_web/package.json",
        *{f"scripts/{name}" for name in _BROWSER_SCRIPTS},
    }:
        return True
    return any(
        path.startswith(f"apps/aiops_console_web/node_modules/{name}/")
        for name in _BROWSER_MODULES
    )


def _browser_runtime_identity(source_root: Path) -> dict[str, Any]:
    browser_root = source_root / "apps/aiops_console_web"
    script = """
const {createRequire} = require("node:module");
const requireFromBrowser = createRequire(process.cwd() + "/package.json");
const {chromium} = requireFromBrowser("playwright");
const playwrightVersion = requireFromBrowser("playwright/package.json").version;
(async () => {
  const browser = await chromium.launch({headless: true, args: ["--no-proxy-server"]});
  const result = {
    playwright_version: playwrightVersion,
    browser_version: browser.version(),
    executable_path: chromium.executablePath(),
  };
  await browser.close();
  process.stdout.write(JSON.stringify(result));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    try:
        completed = subprocess.run(
            ["node", "-e", script], cwd=browser_root, capture_output=True,
            text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("acceptance-tool browser runtime is unavailable") from exc
    if completed.returncode != 0:
        raise ValueError("acceptance-tool browser runtime probe failed")
    try:
        result = json.loads(completed.stdout)
        executable = Path(result.pop("executable_path"))
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError) as exc:
        raise ValueError("acceptance-tool browser runtime probe is invalid") from exc
    if executable.is_symlink() or not executable.is_file():
        raise ValueError("acceptance-tool Chromium executable is unavailable")
    identity = {
        **result,
        "chromium_sha256": sha256(executable),
        "chromium_bytes": executable.stat().st_size,
    }
    _validate_browser_runtime_identity(identity)
    return identity


def _validate_browser_runtime_identity(value: Any) -> None:
    if (
        not isinstance(value, dict) or set(value) != _BROWSER_RUNTIME_FIELDS
        or not all(
            isinstance(value.get(name), str) and value[name]
            for name in ("playwright_version", "browser_version")
        )
        or _SHA256.fullmatch(str(value.get("chromium_sha256", ""))) is None
        or not isinstance(value.get("chromium_bytes"), int)
        or value["chromium_bytes"] <= 0
    ):
        raise ValueError("acceptance-tool browser runtime identity is invalid")


def _archive(entries: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            for name, content in sorted(entries.items()):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                info.mtime = info.uid = info.gid = 0
                bundle.addfile(info, io.BytesIO(content))
    return raw.getvalue()


def _read_archive(path: Path) -> dict[str, bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("acceptance-tool archive is missing, linked or oversized")
    entries: dict[str, bytes] = {}
    try:
        with tarfile.open(path, "r:gz") as bundle:
            members = bundle.getmembers()
            if sum(member.size for member in members) > _MAX_ARCHIVE_BYTES:
                raise ValueError("acceptance-tool archive expands beyond its bound")
            for member in members:
                relative = Path(member.name)
                if (
                    not member.isfile() or relative.is_absolute() or ".." in relative.parts
                    or not relative.parts or relative.parts[0] != TOOL_ROOT
                    or member.name in entries
                ):
                    raise ValueError("acceptance-tool archive contains an unsafe entry")
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("acceptance-tool archive entry is unreadable")
                entries[member.name] = source.read()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError("acceptance-tool archive is invalid") from exc
    return entries


def _inventory(entries: dict[str, bytes], *, prefix: str) -> list[dict[str, Any]]:
    return [
        {"path": name.removeprefix(prefix), "sha256": sha256_bytes(content), "bytes": len(content)}
        for name, content in sorted(entries.items()) if name.startswith(prefix)
    ]


def _checksum_entries(content: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in content.decode("utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if separator != "  " or _SHA256.fullmatch(digest) is None or name in result:
            raise ValueError("acceptance-tool checksum manifest is invalid")
        result[name] = digest
    return result


def _json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"acceptance-tool {label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"acceptance-tool {label} is not an object")
    return value


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
