from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from scripts.build_pilot_release import RELEASE_FILES, build_release, render_with_kubectl


ROOT = Path(__file__).resolve().parents[1]
VERSION = "v0.1.0"
RELEASE_NAME = f"aiops-pilot-{VERSION}"
IMAGE = re.compile(r"@sha256:[0-9a-f]{64}$")


def _build(
    version: str,
    output: Path,
    image_verifier=lambda _images: None,
) -> tuple[Path, Path]:
    return build_release(
        version,
        output,
        source_root=ROOT,
        renderer=render_with_kubectl,
        image_verifier=image_verifier,
    )


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    output = tmp_path_factory.mktemp("pilot-package-output")
    archive, checksums = _build(VERSION, output)
    empty = tmp_path_factory.mktemp("pilot-package-empty")
    shutil.copy2(archive, empty / archive.name)
    shutil.copy2(checksums, empty / checksums.name)
    with tarfile.open(empty / archive.name, "r:gz") as bundle:
        assert all(
            not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
            for member in bundle.getmembers()
        )
        bundle.extractall(empty, filter="data")
    return empty, empty / RELEASE_NAME, empty / archive.name


def _render(path: Path) -> tuple[str, list[dict]]:
    text = subprocess.run(
        ["kubectl", "kustomize", str(path)],
        cwd=path.parent,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return text, [document for document in yaml.safe_load_all(text) if document]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_archive_and_checksum_are_deterministic_and_verify_from_empty_directory(
    package: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    empty, _release, archive = package
    checksum_line = (empty / "SHA256SUMS").read_text(encoding="utf-8").strip()
    assert checksum_line == f"{_sha256(archive)}  {archive.name}"

    second, _ = _build(VERSION, tmp_path)
    assert second.read_bytes() == archive.read_bytes()


def test_release_version_is_semver_and_matches_the_console(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="vX.Y.Z"):
        _build("0.1.0", tmp_path)
    with pytest.raises(ValueError, match="Console package version"):
        _build("v9.9.9", tmp_path)


def test_extracted_top_level_is_the_only_local_kustomize_entrypoint(
    package: tuple[Path, Path, Path],
) -> None:
    empty, release, archive = package
    with tarfile.open(archive, "r:gz") as bundle:
        top_levels = {Path(member.name).parts[0] for member in bundle.getmembers()}
    assert top_levels == {RELEASE_NAME}
    assert (release / "kustomization.yaml").is_file()
    assert (release / "manifest.yaml").is_file()
    assert (release / "verification/base/kustomization.yaml").is_file()
    assert (release / "verification/run/kustomization.yaml").is_file()

    for path in release.rglob("kustomization.yaml"):
        kustomization = yaml.safe_load(path.read_text(encoding="utf-8"))
        for resource in kustomization["resources"]:
            candidate = (path.parent / resource).resolve()
            assert candidate.is_relative_to(release.resolve())
            assert candidate.exists()
    assert {
        str(path.relative_to(release)) for path in release.rglob("*") if path.is_file()
    } == RELEASE_FILES
    assert empty != ROOT


def test_package_render_has_only_digest_workloads_and_one_release_version(
    package: tuple[Path, Path, Path],
) -> None:
    _empty, release, _archive = package
    _text, resources = _render(release)
    images: dict[str, str] = {}
    for resource in resources:
        assert resource["metadata"]["annotations"]["aiops.dev/release-version"] == VERSION
        if resource["kind"] not in {"Deployment", "DaemonSet", "StatefulSet", "Job"}:
            continue
        template = resource["spec"]["template"]
        assert template["metadata"]["annotations"]["aiops.dev/release-version"] == VERSION
        for container in template["spec"].get("containers", []):
            assert IMAGE.search(container["image"])
            assert not container["image"].endswith("sha256:" + "0" * 64)
            images[f"{resource['kind']}/{resource['metadata']['name']}/{container['name']}"] = (
                container["image"]
            )
    assert len(images) >= 15
    assert not any(resource["kind"] in {"Ingress", "ServiceMonitor", "PrometheusRule"} for resource in resources)

    metadata = json.loads((release / "release.json").read_text(encoding="utf-8"))
    assert metadata["release_version"] == VERSION
    assert metadata["console_version"] == VERSION.removeprefix("v")
    assert metadata["support"] == {"clean_install": True, "same_version_reapply": True}
    assert set(images.values()) <= set(metadata["images"].values())
    assert metadata["config_revisions"]


def test_builder_sends_every_product_and_fixture_digest_to_anonymous_verification(
    tmp_path: Path,
) -> None:
    checked: list[str] = []
    _build(VERSION, tmp_path, lambda images: checked.extend(images))

    assert len(set(checked)) >= 13
    assert all(IMAGE.search(image) for image in checked)
    assert any("aiops-verification@sha256:" in image for image in checked)


def test_version_matched_verification_overlays_render_without_product_sources(
    package: tuple[Path, Path, Path],
) -> None:
    _empty, release, _archive = package
    images = set()
    for name in ("base", "run"):
        text, resources = _render(release / "verification" / name)
        result = subprocess.run(
            ["kubectl", "apply", "--dry-run=client", "--validate=false", "-f", "-"],
            cwd=release,
            input=text,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        for resource in resources:
            assert resource["metadata"]["annotations"]["aiops.dev/release-version"] == VERSION
            if resource["kind"] in {"Deployment", "Job"}:
                images.add(resource["spec"]["template"]["spec"]["containers"][0]["image"])
    assert len(images) == 1


def test_bundled_openapi_producer_consumer_and_instructions_are_fixed(
    package: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    _empty, release, _archive = package
    producer = release / "contracts/gateway-v1.json"
    consumer = release / "contracts/gateway-v1.schema.d.ts"
    spec = json.loads(producer.read_text(encoding="utf-8"))
    generated = tmp_path / "gateway-v1.schema.d.ts"
    subprocess.run(
        [
            str(ROOT / "apps/aiops_console_web/node_modules/.bin/openapi-typescript"),
            str(producer),
            "-o",
            str(generated),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert generated.read_bytes() == consumer.read_bytes()

    metadata = json.loads((release / "release.json").read_text(encoding="utf-8"))
    assert metadata["openapi"] == {
        "api_version": spec["info"]["version"],
        "producer_sha256": _sha256(producer),
        "console_consumer_sha256": _sha256(consumer),
    }
    instructions = (release / "README.md").read_text(encoding="utf-8")
    assert f"kubectl apply -k ./{RELEASE_NAME}" in instructions
    assert "deploy/k8s/pilot" not in instructions
    for unsupported in ("upgrade", "downgrade", "backup", "data-preserving uninstall"):
        assert unsupported in instructions
