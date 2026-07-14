#!/usr/bin/env python3
"""Build a deterministic, source-independent Pilot release archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
IMAGE_PATTERN = re.compile(r"^[^:@\s]+(?:/[^:@\s]+)+@sha256:([0-9a-f]{64})$")
PUBLIC_REGISTRIES = (
    "docker.m.daocloud.io/",
    "quay.io/",
    "registry.cn-hangzhou.aliyuncs.com/timelessmao/",
    "registry.k8s.io/",
)
WORKLOAD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "Job"}
RELEASE_FILES = {
    "README.md",
    "contracts/gateway-v1.json",
    "contracts/gateway-v1.schema.d.ts",
    "kustomization.yaml",
    "manifest.yaml",
    "release.json",
    "verification/README.md",
    "verification/base/kustomization.yaml",
    "verification/base/namespace.yaml",
    "verification/base/networkpolicy.yaml",
    "verification/base/workload.yaml",
    "verification/run/job.yaml",
    "verification/run/kustomization.yaml",
}


def build_release(
    version: str,
    output_dir: Path,
    *,
    source_root: Path,
    renderer: Callable[[Path], str],
    image_verifier: Callable[[Iterable[str]], None],
) -> tuple[Path, Path]:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must match vX.Y.Z")
    release_name = f"aiops-pilot-{version}"
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aiops-pilot-release-") as temporary:
        release_dir = Path(temporary) / release_name
        release_dir.mkdir()
        pilot_text = renderer(source_root / "deploy/k8s/pilot")
        pilot_documents = documents(pilot_text)
        pilot_images = image_inventory(pilot_documents, "pilot")

        (release_dir / "manifest.yaml").write_text(pilot_text, encoding="utf-8")
        write_yaml(
            release_dir / "kustomization.yaml",
            {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": ["manifest.yaml"],
                "commonAnnotations": {"aiops.dev/release-version": version},
            },
        )
        copy_verification(release_dir, version, source_root)
        contracts = release_dir / "contracts"
        contracts.mkdir()
        shutil.copy2(source_root / "api/openapi/gateway-v1.json", contracts / "gateway-v1.json")
        shutil.copy2(
            source_root / "apps/aiops_console_web/src/api/schema.d.ts",
            contracts / "gateway-v1.schema.d.ts",
        )
        verification_images = {}
        for name in ("base", "run"):
            verification_images.update(
                image_inventory(
                    documents(renderer(release_dir / "verification" / name)),
                    f"verification-{name}",
                )
            )
        console_package = json.loads(
            (source_root / "apps/aiops_console_web/package.json").read_text(encoding="utf-8")
        )
        if console_package["version"] != version.removeprefix("v"):
            raise ValueError("release version must match the Console package version")
        openapi = json.loads((contracts / "gateway-v1.json").read_text(encoding="utf-8"))
        metadata = {
            "format_version": 1,
            "release_version": version,
            "console_version": console_package["version"],
            "install_entrypoint": f"kubectl apply -k ./{release_name}",
            "support": {"clean_install": True, "same_version_reapply": True},
            "openapi": {
                "api_version": openapi["info"]["version"],
                "producer_sha256": sha256(contracts / "gateway-v1.json"),
                "console_consumer_sha256": sha256(contracts / "gateway-v1.schema.d.ts"),
            },
            "images": dict(sorted((pilot_images | verification_images).items())),
            "config_revisions": config_revisions(pilot_documents),
        }
        image_verifier(metadata["images"].values())
        (release_dir / "release.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (release_dir / "README.md").write_text(release_readme(release_name), encoding="utf-8")
        validate_release(release_dir, version, renderer)

        archive = output_dir / f"{release_name}.tar.gz"
        write_archive(release_dir, archive)
    checksums = output_dir / "SHA256SUMS"
    checksums.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return archive, checksums


def render_with_kubectl(path: Path) -> str:
    return subprocess.run(
        ["kubectl", "kustomize", str(path)],
        cwd=path.parent,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def documents(text: str) -> list[dict]:
    return [document for document in yaml.safe_load_all(text) if document]


def image_inventory(resources: Iterable[dict], prefix: str) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for resource in resources:
        if resource.get("kind") not in WORKLOAD_KINDS:
            continue
        pod = resource["spec"]["template"]["spec"]
        for container in [*pod.get("initContainers", []), *pod.get("containers", [])]:
            image = container["image"]
            match = IMAGE_PATTERN.fullmatch(image)
            if not match or match.group(1) == "0" * 64:
                raise ValueError(f"workload image is not an immutable digest: {image}")
            if not image.startswith(PUBLIC_REGISTRIES):
                raise ValueError(f"workload image is not in an approved public registry: {image}")
            key = f"{prefix}/{resource['kind']}/{resource['metadata']['name']}/{container['name']}"
            inventory[key] = image
    return inventory


def config_revisions(resources: Iterable[dict]) -> dict[str, str]:
    return {
        resource["metadata"]["name"]: hashlib.sha256(
            json.dumps(resource.get("data", {}), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for resource in resources
        if resource.get("kind") == "ConfigMap"
    }


def copy_verification(release_dir: Path, version: str, source_root: Path) -> None:
    target = release_dir / "verification"
    target.mkdir()
    shutil.copy2(source_root / "verification/README.md", target / "README.md")
    for name in ("base", "run"):
        source = source_root / "verification" / name
        destination = target / name
        destination.mkdir()
        kustomization = yaml.safe_load(
            (source / "kustomization.yaml").read_text(encoding="utf-8")
        )
        for resource in kustomization["resources"]:
            shutil.copy2(source / resource, destination / resource)
        kustomization["commonAnnotations"] = {"aiops.dev/release-version": version}
        write_yaml(destination / "kustomization.yaml", kustomization)


def validate_release(
    release_dir: Path, version: str, renderer: Callable[[Path], str]
) -> None:
    actual_files = {
        str(path.relative_to(release_dir)) for path in release_dir.rglob("*") if path.is_file()
    }
    if actual_files != RELEASE_FILES:
        raise ValueError(f"release file inventory mismatch: {sorted(actual_files ^ RELEASE_FILES)}")
    for path in release_dir.rglob("kustomization.yaml"):
        kustomization = yaml.safe_load(path.read_text(encoding="utf-8"))
        for resource in kustomization.get("resources", []):
            candidate = (path.parent / resource).resolve()
            try:
                candidate.relative_to(release_dir.resolve())
            except ValueError as exc:
                raise ValueError(f"Kustomize resource escapes release root: {resource}") from exc
            if not candidate.exists():
                raise ValueError(f"Kustomize resource is not local: {resource}")
    for path in (release_dir, release_dir / "verification/base", release_dir / "verification/run"):
        rendered = documents(renderer(path))
        image_inventory(rendered, path.name)
        for resource in rendered:
            annotations = resource.get("metadata", {}).get("annotations", {})
            if annotations.get("aiops.dev/release-version") != version:
                raise ValueError(f"resource is missing release version: {resource.get('kind')}")
            if resource.get("kind") in WORKLOAD_KINDS:
                template_annotations = resource["spec"]["template"]["metadata"].get("annotations", {})
                if template_annotations.get("aiops.dev/release-version") != version:
                    raise ValueError(f"workload template is missing release version: {resource['metadata']['name']}")


def write_yaml(path: Path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def write_archive(release_dir: Path, archive: Path) -> None:
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for path in [release_dir, *sorted(release_dir.rglob("*"))]:
                    info = tar.gettarinfo(str(path), arcname=str(path.relative_to(release_dir.parent)))
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    info.mode = 0o755 if path.is_dir() else 0o644
                    if path.is_file():
                        with path.open("rb") as source:
                            tar.addfile(info, source)
                    else:
                        tar.addfile(info)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_anonymous_images(images: Iterable[str]) -> None:
    unique = sorted(set(images))
    with tempfile.TemporaryDirectory(prefix="aiops-anonymous-docker-") as config:
        def inspect(image: str) -> None:
            command = ["docker", "--config", config, "manifest", "inspect", image]
            try:
                subprocess.run(
                    command, check=True, capture_output=True, text=True, timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"anonymous image verification timed out: {image}") from exc
            except subprocess.CalledProcessError as exc:
                detail = exc.stderr.strip() or "manifest inspect failed"
                raise RuntimeError(f"anonymous image verification failed: {image}: {detail}") from exc

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(inspect, unique))


def release_readme(release_name: str) -> str:
    return f"""# AIOps Pilot Release

本目录是唯一、自包含的 Kustomize 安装入口，固定安装到 `aiops-system`。安装前需确认当前 kube context 指向非生产 Cluster、默认 StorageClass 可动态供给至少 32Gi RWO、NodePort 30088 可用、CNI 实施 NetworkPolicy，且节点可匿名拉取 `release.json` 中的全部 digest。

```bash
kubectl apply -k ./{release_name}
kubectl wait -n aiops-system --for=condition=complete job/aiops-bootstrap --timeout=2m
kubectl wait -n aiops-system --for=jsonpath='{{.status.phase}}'=Bound pvc --all --timeout=10m
kubectl wait -n aiops-system --for=condition=Available deployment --all --timeout=10m
```

浏览器入口为 `http://<NodeIP>:30088`。Model、Notification 与 Connector Enrollment 在首次登录后配置，不阻塞 Installation Ready。

Controlled verification 默认不安装；仅在 setup gates 就绪后按 `verification/README.md` 显式安装。

当前只支持 clean install 与同版本 reapply。失败的 bootstrap Job 只可删除 Job 后重试，不得删除已生成 Secret。本发布不承诺 upgrade、downgrade、backup、rollback 或 data-preserving uninstall。
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    archive, checksums = build_release(
        args.version,
        args.output_dir,
        source_root=ROOT,
        renderer=render_with_kubectl,
        image_verifier=verify_anonymous_images,
    )
    print(archive)
    print(checksums)


if __name__ == "__main__":
    main()
