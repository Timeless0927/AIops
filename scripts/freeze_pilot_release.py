#!/usr/bin/env python3
"""Build and verify the one offline F10 product/tool freeze."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiops.acceptance.adapters import OpenSshSigner
from aiops.acceptance.evidence_files import atomic_write
from aiops.acceptance.freeze import (
    assert_relevant_sources_clean,
    build_admission_statement,
    build_freeze_record,
    inspect_release_bundle,
    relevant_source_inventory,
    resolve_review_fixed_point,
    verify_final_checksums,
    verify_freeze_record,
    write_final_checksums,
)
from aiops.acceptance.tool_artifact import build_acceptance_tool
from scripts.build_pilot_release import build_release, render_with_kubectl


ROOT = Path(__file__).resolve().parents[1]
VERSION = "v0.1.0"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--fixed-point", required=True)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _prepare_output(args.output)

    excluded_wip = assert_relevant_sources_clean(ROOT)
    reviewed_commit = _git("rev-parse", "HEAD")
    fixed_point = resolve_review_fixed_point(ROOT, args.fixed_point, reviewed_commit)
    reviewed_tree = _git("rev-parse", "HEAD^{tree}")
    reports = json.loads(args.reports.read_text(encoding="utf-8"))
    if not isinstance(reports, dict):
        raise ValueError("F10 reports must be one JSON object")

    product_dir = args.output / "product"
    release_archive, release_checksums = build_release(
        VERSION,
        product_dir,
        source_root=ROOT,
        renderer=render_with_kubectl,
        image_verifier=lambda images: list(images),
    )
    release = inspect_release_bundle(release_archive, release_checksums)
    statement = build_admission_statement(
        release_identity=release,
        source_inventory=relevant_source_inventory(ROOT),
        reports=reports,
        pre_f10_commit=fixed_point,
        reviewed_commit=reviewed_commit,
        reviewed_tree=reviewed_tree,
    )
    with _signing_key(args.key) as key:
        signer = OpenSshSigner()
        admission = {"statement": statement, **signer.sign(statement, key_path=key)}

    def verify(item: dict[str, Any]) -> None:
        signer = OpenSshSigner()
        signer.verify(
            item["statement"], signature=item["signature"],
            public_key=item["public_key"], identity="release-maintainer",
        )
        if signer.fingerprint(item["public_key"]) != item["fingerprint"]:
            raise ValueError("F10 admission signing fingerprint drifted")

    verify(admission)
    admission_path = args.output / "admission-report.json"
    _write_json(admission_path, admission)
    tool = build_acceptance_tool(
        ROOT, admission, args.output / "acceptance-tool-v1.tar.gz", verifier=verify,
    )
    inputs = {
        "source_root": ROOT,
        "artifact_root": args.output,
        "release_archive": release_archive,
        "release_checksums": release_checksums,
        "acceptance_tool": tool,
        "signed_admission": admission,
        "excluded_wip": excluded_wip,
        "admission_verifier": verify,
    }
    record = build_freeze_record(**inputs)
    record_path = args.output / "freeze-record.json"
    _write_json(record_path, record)
    verify_freeze_record(record, **inputs)
    write_final_checksums(args.output)
    verify_final_checksums(args.output)
    print(json.dumps({"output": str(args.output), "fixed_point": record["fixed_point"]}))


@contextmanager
def _signing_key(path: Path | None) -> Iterator[Path]:
    if path is not None:
        if path.is_symlink() or not path.is_file():
            raise ValueError("F10 signing key is missing or unsafe")
        yield path
        return
    memory = Path("/dev/shm")
    with tempfile.TemporaryDirectory(
        prefix="aiops-f10-sign-", dir=memory if memory.is_dir() else None,
    ) as temporary:
        key = Path(temporary) / "release-maintainer"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
        )
        yield key


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *arguments], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _prepare_output(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("freeze output must be a real directory")
    if path.exists() and any(path.iterdir()):
        raise ValueError("freeze output directory must not already contain artifacts")
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, value: object) -> None:
    content = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    atomic_write(path, content, staging_dir=path.parent)


if __name__ == "__main__":
    main()
