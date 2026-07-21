from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aiops.acceptance import freeze
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
from aiops.acceptance.tool_artifact import REQUIRED_CHECKS, build_acceptance_tool
from scripts.build_pilot_release import build_release, render_with_kubectl
from scripts.freeze_pilot_release import _prepare_output


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "1" * 40


def _reports() -> dict[str, dict[str, object]]:
    return {
        name: {
            "status": "passed",
            "command": f"verify {name}",
            "summary": f"{name} passed at the reviewed fixed point",
            "details": {
                "reviewed_commit": COMMIT,
                "selector_count": 1,
                **({"fixed_point": COMMIT, "blockers": []} if name.endswith("_review") else {}),
            },
        }
        for name in REQUIRED_CHECKS
    }


def _freeze(tmp_path: Path) -> tuple[dict, dict, dict]:
    product = tmp_path / "product"
    archive, checksums = build_release(
        "v0.1.0", product, source_root=ROOT, renderer=render_with_kubectl,
        image_verifier=lambda images: list(images),
    )
    release = inspect_release_bundle(archive, checksums)
    admission = {
        "statement": build_admission_statement(
            release_identity=release,
            source_inventory=relevant_source_inventory(ROOT),
            reports=_reports(),
            pre_f10_commit=COMMIT,
            reviewed_commit=COMMIT,
            reviewed_tree="2" * 40,
        ),
        "signature": "signed-admission",
        "public_key": "ssh-ed25519 release-maintainer",
        "fingerprint": "SHA256:release-maintainer",
    }

    def verify(item: dict) -> None:
        if item["signature"] != "signed-admission":
            raise ValueError("bad signature")

    tool = build_acceptance_tool(
        ROOT, admission, tmp_path / "acceptance-tool-v1.tar.gz", verifier=verify,
    )
    inputs = {
        "source_root": ROOT,
        "artifact_root": tmp_path,
        "release_archive": archive,
        "release_checksums": checksums,
        "acceptance_tool": tool,
        "signed_admission": admission,
        "excluded_wip": [{"path": "docs/user-wip.md", "exists": False}],
        "admission_verifier": verify,
    }
    return build_freeze_record(**inputs), inputs, release


def test_freeze_binds_both_artifacts_reports_contracts_images_and_defaults(
    tmp_path: Path,
) -> None:
    record, inputs, release = _freeze(tmp_path)

    verify_freeze_record(record, **inputs)
    assert record["live_evidence"] is False
    assert set(record["checks"]) == set(REQUIRED_CHECKS)
    assert record["release"]["images"] == release["images"]
    assert "aiops-runtime-config" in record["release"]["defaults"]["config_maps"]
    assert record["release"]["openapi"]["producer_sha256"]
    assert record["artifacts"]["pilot_release"]["sha256"] == release["archive_sha256"]
    assert record["contracts"] == {
        "gate_contract_revision": "pilot-clean-acceptance-v4",
        "evidence_format_version": 4,
        "environment_qualification_format_version": 1,
    }

    (tmp_path / "admission-report.json").write_text(
        json.dumps(inputs["signed_admission"]), encoding="utf-8",
    )
    (tmp_path / "freeze-record.json").write_text(json.dumps(record), encoding="utf-8")
    write_final_checksums(tmp_path)
    verify_final_checksums(tmp_path)
    (tmp_path / "freeze-record.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        verify_final_checksums(tmp_path)


def test_freeze_rejects_source_admission_and_product_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, inputs, _release = _freeze(tmp_path)
    inventory = relevant_source_inventory(ROOT)
    monkeypatch.setattr(
        freeze, "relevant_source_inventory",
        lambda _root: [{**inventory[0], "sha256": "0" * 64}, *inventory[1:]],
    )
    with pytest.raises(ValueError, match="identities drifted"):
        verify_freeze_record(record, **inputs)
    monkeypatch.undo()

    changed = json.loads(json.dumps(record))
    changed["checks"]["owner_tests"]["report"]["summary"] = "tampered"
    with pytest.raises(ValueError, match="does not match"):
        verify_freeze_record(changed, **inputs)

    checksum = inputs["release_checksums"]
    outside = tmp_path / "outside-checksum"
    outside.write_bytes(checksum.read_bytes())
    checksum.unlink()
    checksum.symlink_to(outside)
    with pytest.raises(ValueError, match="missing or linked"):
        verify_freeze_record(record, **inputs)
    checksum.unlink()
    checksum.write_text("0" * 64 + "  wrong.tar.gz\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        verify_freeze_record(record, **inputs)


def test_dirty_audit_preserves_unrelated_wip_and_rejects_relevant_changes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "aiops").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "aiops/owner.py").write_text("OWNER = True\n", encoding="utf-8")
    (repo / "docs/notes.md").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=F10",
            "-c", "user.email=f10@example.test", "commit", "-qm", "fixture",
        ],
        check=True,
    )

    (repo / "docs/notes.md").write_text("user wip\n", encoding="utf-8")
    [excluded] = assert_relevant_sources_clean(repo)
    assert excluded["path"] == "docs/notes.md"
    assert (repo / "docs/notes.md").read_text(encoding="utf-8") == "user wip\n"

    (repo / "aiops/owner.py").write_text("OWNER = False\n", encoding="utf-8")
    with pytest.raises(ValueError, match="relevant source inputs are dirty"):
        assert_relevant_sources_clean(repo)

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert resolve_review_fixed_point(repo, "HEAD", head) == head
    with pytest.raises(ValueError, match="valid commit"):
        resolve_review_fixed_point(repo, "missing-fixed-point", head)
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    unrelated = subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=F20",
            "-c", "user.email=f20@example.test", "commit-tree", tree,
            "-m", "unrelated fixture",
        ],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    with pytest.raises(ValueError, match="not an ancestor"):
        resolve_review_fixed_point(repo, unrelated, head)


def test_admission_requires_complete_fixed_point_reports(tmp_path: Path) -> None:
    product = tmp_path / "product"
    archive, checksums = build_release(
        "v0.1.0", product, source_root=ROOT, renderer=render_with_kubectl,
        image_verifier=lambda images: list(images),
    )
    reports = _reports()
    reports["spec_review"]["details"]["fixed_point"] = "wrong"  # type: ignore[index]
    with pytest.raises(ValueError, match="belongs to another commit"):
        build_admission_statement(
            release_identity=inspect_release_bundle(archive, checksums),
            source_inventory=relevant_source_inventory(ROOT), reports=reports,
            pre_f10_commit=COMMIT, reviewed_commit=COMMIT, reviewed_tree="2" * 40,
        )


def test_freeze_output_must_be_real_and_empty(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        _prepare_output(linked)

    (target / "existing").write_text("occupied", encoding="utf-8")
    with pytest.raises(ValueError, match="must not already contain"):
        _prepare_output(target)

    fresh = tmp_path / "fresh"
    _prepare_output(fresh)
    assert fresh.is_dir()
