"""Offline F10 product/tool artifact freeze and invalidation checks."""

from __future__ import annotations

import json
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from scripts.build_pilot_release import (
    RELEASE_FILES,
    config_revisions,
    documents,
    image_inventory,
)

from .credentials import assert_public_payload
from .evidence_files import atomic_write, sha256, sha256_bytes
from .tool_artifact import (
    ADMISSION_FORMAT_VERSION,
    EVIDENCE_FORMAT_VERSION,
    INVALIDATION_RULE,
    REQUIRED_CHECKS,
    inspect_acceptance_tool,
    self_check,
)
from .gate_contract import GATE_CONTRACT_REVISION


FREEZE_FORMAT_VERSION = 1
RELEVANT_PATHS = (
    "aiops", "api", "apps", "deploy", "scripts", "tests", "verification",
    "pyproject.toml", "uv.lock",
)
_MAX_RELEASE_BYTES = 128 * 1024 * 1024


def relevant_source_inventory(source_root: Path) -> list[dict[str, Any]]:
    """Hash every tracked executable, product, test and build input relevant to F10."""
    names = _git_lines(source_root, "ls-files", "-z", "--", *RELEVANT_PATHS)
    files = [source_root / name for name in names]
    if not files or any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError("F10 relevant source inventory is incomplete or unsafe")
    return [
        {
            "path": str(path.relative_to(source_root)),
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        }
        for path in files
    ]


def dirty_path_inventory(source_root: Path) -> list[dict[str, Any]]:
    changed = set(_git_lines(source_root, "diff", "--name-only", "-z", "HEAD", "--"))
    changed.update(
        _git_lines(source_root, "ls-files", "--others", "--exclude-standard", "-z", "--")
    )
    result = []
    for name in sorted(changed):
        path = source_root / name
        result.append(
            {
                "path": name,
                "exists": path.is_file() and not path.is_symlink(),
                **(
                    {"sha256": sha256(path), "bytes": path.stat().st_size}
                    if path.is_file() and not path.is_symlink() else {}
                ),
            }
        )
    return result


def assert_relevant_sources_clean(source_root: Path) -> list[dict[str, Any]]:
    dirty = dirty_path_inventory(source_root)
    relevant = [item["path"] for item in dirty if _is_relevant(item["path"])]
    if relevant:
        raise ValueError(f"F10 relevant source inputs are dirty: {relevant}")
    return dirty


def resolve_review_fixed_point(
    source_root: Path, fixed_point: str, reviewed_commit: str,
) -> str:
    """Resolve one real commit and require it to precede the reviewed commit."""
    fixed = _git_commit(source_root, fixed_point)
    reviewed = _git_commit(source_root, reviewed_commit)
    ancestor = subprocess.run(
        ["git", "-C", str(source_root), "merge-base", "--is-ancestor", fixed, reviewed],
        check=False, capture_output=True,
    )
    if ancestor.returncode != 0:
        raise ValueError("F20 review fixed point is not an ancestor of reviewed HEAD")
    return fixed


def build_admission_statement(
    *,
    release_identity: dict[str, Any],
    source_inventory: list[dict[str, Any]],
    reports: dict[str, dict[str, Any]],
    pre_f10_commit: str,
    reviewed_commit: str,
    reviewed_tree: str,
) -> dict[str, Any]:
    checks = {}
    if set(reports) != set(REQUIRED_CHECKS):
        raise ValueError("F10 reports do not cover every required check")
    for name in REQUIRED_CHECKS:
        report = reports[name]
        details = report.get("details") if isinstance(report, dict) else None
        if (
            not isinstance(report, dict)
            or report.get("status") != "passed"
            or not isinstance(report.get("command"), str)
            or not report["command"]
            or not isinstance(report.get("summary"), str)
            or not report["summary"]
            or not isinstance(details, dict)
            or details.get("reviewed_commit") != reviewed_commit
            or (
                name in {"standards_review", "spec_review"}
                and details.get("fixed_point") != pre_f10_commit
            )
        ):
            raise ValueError(f"F10 {name} report is incomplete or belongs to another commit")
        checks[name] = {"sha256": sha256_bytes(_json_bytes(report)), "report": report}
    statement = {
        "format_version": ADMISSION_FORMAT_VERSION,
        "release_sha256": release_identity["archive_sha256"],
        "gate_contract_revision": GATE_CONTRACT_REVISION,
        "evidence_format_version": EVIDENCE_FORMAT_VERSION,
        "fixed_point": {
            "pre_f10_commit": pre_f10_commit,
            "reviewed_commit": reviewed_commit,
            "reviewed_tree": reviewed_tree,
        },
        "release": {
            "openapi": release_identity["openapi"],
            "images_sha256": release_identity["images_sha256"],
            "config_revisions_sha256": release_identity["config_revisions_sha256"],
            "defaults_sha256": release_identity["defaults_sha256"],
        },
        "source_inventory_sha256": sha256_bytes(_json_bytes(source_inventory)),
        "checks": checks,
        "invalidation_rule": INVALIDATION_RULE,
        "live_evidence": False,
    }
    assert_public_payload(statement)
    return statement


def inspect_release_bundle(archive: Path, checksums: Path) -> dict[str, Any]:
    if checksums.is_symlink() or not checksums.is_file():
        raise ValueError("Pilot Release checksum is missing or linked")
    expected_line = f"{sha256(archive)}  {archive.name}"
    if checksums.read_text(encoding="utf-8").splitlines() != [expected_line]:
        raise ValueError("Pilot Release checksum does not exactly match the archive")
    files, root = _release_files(archive)
    metadata = _json_object(files[f"{root}/release.json"], "release metadata")
    if archive.name != f"{root}.tar.gz" or metadata.get("release_version") != root.removeprefix("aiops-pilot-"):
        raise ValueError("Pilot Release name and version identity drifted")
    producer = files[f"{root}/contracts/gateway-v1.json"]
    consumer = files[f"{root}/contracts/gateway-v1.schema.d.ts"]
    openapi = _json_object(producer, "OpenAPI contract")
    expected_openapi = {
        "api_version": openapi["info"]["version"],
        "producer_sha256": sha256_bytes(producer),
        "console_consumer_sha256": sha256_bytes(consumer),
    }
    if metadata.get("openapi") != expected_openapi:
        raise ValueError("Pilot Release OpenAPI identities drifted")
    pilot = documents(files[f"{root}/manifest.yaml"].decode("utf-8"))
    images = image_inventory(pilot, "pilot")
    for name in ("base", "run"):
        prefix = f"{root}/verification/{name}/"
        resources = []
        kustomization = yaml.safe_load(files[f"{prefix}kustomization.yaml"])
        for resource in kustomization["resources"]:
            resources.extend(documents(files[f"{prefix}{resource}"].decode("utf-8")))
        images.update(image_inventory(resources, f"verification-{name}"))
    if metadata.get("images") != dict(sorted(images.items())):
        raise ValueError("Pilot Release image inventory drifted")
    revisions = config_revisions(pilot)
    if metadata.get("config_revisions") != revisions:
        raise ValueError("Pilot Release ConfigMap revisions drifted")
    defaults = {
        "config_maps": {
            item["metadata"]["name"]: item.get("data", {})
            for item in pilot if item.get("kind") == "ConfigMap"
        },
        "console_version": metadata.get("console_version"),
        "install_entrypoint": metadata.get("install_entrypoint"),
        "support": metadata.get("support"),
    }
    identity = {
        "archive_sha256": sha256(archive),
        "release_version": metadata["release_version"],
        "openapi": expected_openapi,
        "images": dict(sorted(images.items())),
        "images_sha256": sha256_bytes(_json_bytes(dict(sorted(images.items())))),
        "config_revisions": revisions,
        "config_revisions_sha256": sha256_bytes(_json_bytes(revisions)),
        "defaults": defaults,
        "defaults_sha256": sha256_bytes(_json_bytes(defaults)),
    }
    assert_public_payload(identity)
    return identity


def build_freeze_record(
    *,
    source_root: Path,
    artifact_root: Path,
    release_archive: Path,
    release_checksums: Path,
    acceptance_tool: Path,
    signed_admission: dict[str, Any],
    excluded_wip: list[dict[str, Any]],
    admission_verifier: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    inventory = relevant_source_inventory(source_root)
    release = inspect_release_bundle(release_archive, release_checksums)
    inspected_tool = inspect_acceptance_tool(acceptance_tool)
    if inspected_tool["admission"] != signed_admission:
        raise ValueError("Acceptance Tool does not contain the exact F10 admission report")
    statement = signed_admission["statement"]
    self_check(
        acceptance_tool,
        release_sha256=release["archive_sha256"],
        acceptance_tool_sha256=sha256(acceptance_tool),
        gate_contract_revision=GATE_CONTRACT_REVISION,
        verifier=admission_verifier,
    )
    if (
        statement["source_inventory_sha256"] != sha256_bytes(_json_bytes(inventory))
        or statement["release"] != {
            "openapi": release["openapi"],
            "images_sha256": release["images_sha256"],
            "config_revisions_sha256": release["config_revisions_sha256"],
            "defaults_sha256": release["defaults_sha256"],
        }
        or sha256_bytes((source_root / "api/openapi/gateway-v1.json").read_bytes())
        != release["openapi"]["producer_sha256"]
        or sha256_bytes((source_root / "apps/aiops_console_web/src/api/schema.d.ts").read_bytes())
        != release["openapi"]["console_consumer_sha256"]
    ):
        raise ValueError("F10 admission, source and Pilot Release identities drifted")
    record = {
        "format_version": FREEZE_FORMAT_VERSION,
        "fixed_point": statement["fixed_point"],
        "invalidation_rule": INVALIDATION_RULE,
        "live_evidence": False,
        "artifacts": {
            "pilot_release": _artifact_identity(release_archive, artifact_root),
            "pilot_release_checksums": _artifact_identity(release_checksums, artifact_root),
            "acceptance_tool": _artifact_identity(acceptance_tool, artifact_root),
            "admission_report_sha256": sha256_bytes(_json_bytes(signed_admission)),
            "acceptance_tool_source_sha256": inspected_tool["manifest"]["source_sha256"],
        },
        "release": release,
        "checks": statement["checks"],
        "sources": {
            "sha256": statement["source_inventory_sha256"],
            "files": inventory,
        },
        "excluded_wip": excluded_wip,
    }
    assert_public_payload(record)
    return record


def verify_freeze_record(record: dict[str, Any], **inputs: Any) -> None:
    if record != build_freeze_record(**inputs):
        raise ValueError("F10 freeze record does not match current immutable inputs")


def write_final_checksums(root: Path) -> Path:
    output = root / "SHA256SUMS"
    files = _artifact_files(root, output)
    atomic_write(
        output,
        "".join(f"{sha256(path)}  {path.relative_to(root)}\n" for path in files).encode(),
        staging_dir=root,
    )
    return output


def verify_final_checksums(root: Path) -> None:
    checksum = root / "SHA256SUMS"
    expected = checksum.read_text(encoding="utf-8")
    actual = "".join(
        f"{sha256(path)}  {path.relative_to(root)}\n"
        for path in _artifact_files(root, checksum)
    )
    if expected != actual:
        raise ValueError("F10 final checksum inventory drifted")


def _release_files(archive: Path) -> tuple[dict[str, bytes], str]:
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > _MAX_RELEASE_BYTES:
        raise ValueError("Pilot Release archive is missing, linked or oversized")
    result: dict[str, bytes] = {}
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if not members or sum(item.size for item in members) > _MAX_RELEASE_BYTES:
                raise ValueError("Pilot Release archive expands beyond its bound")
            roots = {Path(item.name).parts[0] for item in members if Path(item.name).parts}
            if len(roots) != 1:
                raise ValueError("Pilot Release archive must contain one top-level directory")
            root = roots.pop()
            for member in members:
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                    raise ValueError("Pilot Release archive contains an unsafe entry")
                if member.isfile():
                    if member.name in result:
                        raise ValueError("Pilot Release archive contains a duplicate entry")
                    source = bundle.extractfile(member)
                    if source is None:
                        raise ValueError("Pilot Release archive entry is unreadable")
                    result[member.name] = source.read()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError("Pilot Release archive is invalid") from exc
    if set(result) != {f"{root}/{name}" for name in RELEASE_FILES}:
        raise ValueError("Pilot Release file inventory drifted")
    return result, root


def _artifact_identity(path: Path, root: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("F10 artifact is missing, linked or not a regular file")
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("F10 artifact is outside the freeze directory") from exc
    return {"path": str(relative), "sha256": sha256(path), "bytes": path.stat().st_size}


def _artifact_files(root: Path, checksum: Path) -> list[Path]:
    files = sorted(path for path in root.rglob("*") if path != checksum and path.is_file())
    if any(path.is_symlink() for path in root.rglob("*")) or not files:
        raise ValueError("F10 artifact directory is empty or contains a symlink")
    return files


def _is_relevant(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}/") for prefix in RELEVANT_PATHS)


def _git_lines(root: Path, *arguments: str) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True,
    )
    return [item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]


def _git_commit(root: Path, value: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"{value}^{{commit}}"],
        check=False, capture_output=True, text=True,
    )
    commit = completed.stdout.strip()
    if completed.returncode != 0 or len(commit) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise ValueError("F20 review fixed point is not a valid commit")
    return commit


def _json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"F10 {label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"F10 {label} is not an object")
    return value


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
