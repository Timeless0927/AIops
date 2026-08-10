#!/usr/bin/env python3
"""Push the current commit and refresh matching live service images by digest."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


SERVICE_REPOSITORIES = {
    "gateway": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway",
    "verification": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-verification",
    "diagnosis": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-diagnosis",
    "notification": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-notification",
    "connectors": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-connectors",
    "mcp-prometheus": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-prometheus",
    "mcp-loki": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki",
    "mcp-topology": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-topology",
}
WORKFLOW = "docker-image.yml"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseError(RuntimeError):
    """An expected release preflight or verification failure."""


def _run(command: list[str], *, check: bool = True) -> str:
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise ReleaseError(f"cannot run {command[0]}: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleaseError(f"command failed ({' '.join(command)}): {detail}")
    return result.stdout.strip()


def _git_commit() -> str:
    commit = _run(["git", "rev-parse", "HEAD"])
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("git HEAD is not a full commit SHA")
    return commit


def _git_branch() -> str:
    branch = _run(["git", "branch", "--show-current"])
    if not branch:
        raise ReleaseError("detached HEAD cannot be released")
    return branch


def _ci_run(branch: str, commit: str, *, timeout: float, poll: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        raw = _run([
            "gh", "run", "list", "--workflow", WORKFLOW, "--branch", branch,
            "--commit", commit, "--event", "push", "--limit", "20",
            "--json", "databaseId,headSha,status,conclusion,url",
        ])
        try:
            runs = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise ReleaseError("gh returned invalid run JSON") from exc
        if not isinstance(runs, list):
            raise ReleaseError("gh returned an invalid run list")
        matching = [
            run for run in runs
            if isinstance(run, dict) and run.get("headSha") == commit
        ]
        completed = [run for run in matching if run.get("status") == "completed"]
        if completed:
            run = completed[0]
            if run.get("conclusion") != "success":
                raise ReleaseError(
                    f"{WORKFLOW} run {run.get('databaseId')} concluded {run.get('conclusion')}"
                )
            if not isinstance(run.get("databaseId"), int) or not isinstance(run.get("url"), str):
                raise ReleaseError("successful CI run is missing identity")
            return {"id": run["databaseId"], "url": run["url"]}
        if time.monotonic() >= deadline:
            raise ReleaseError(f"timed out waiting for {WORKFLOW} at {commit}")
        time.sleep(poll)


def _download_digest(run_id: int, service: str, directory: Path, commit: str) -> dict[str, str]:
    artifact = directory / service
    artifact.mkdir()
    _run([
        "gh", "run", "download", str(run_id), "--name", f"published-image-{service}",
        "--dir", str(artifact),
    ])
    candidates = list(artifact.rglob("image-digest.json")) + list(artifact.rglob("digest.json"))
    if len(candidates) != 1:
        raise ReleaseError(f"CI artifact for {service} did not contain exactly one digest file")
    try:
        payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"CI artifact for {service} is not valid JSON") from exc
    repository = SERVICE_REPOSITORIES[service]
    if not isinstance(payload, dict) or payload.get("source_sha") != commit:
        raise ReleaseError(f"CI digest for {service} is not bound to {commit}")
    if payload.get("service") != service or payload.get("repository") != repository:
        raise ReleaseError(f"CI digest identity for {service} is invalid")
    digest = payload.get("digest")
    if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
        raise ReleaseError(f"CI digest for {service} is invalid")
    return {"service": service, "repository": repository, "digest": digest}


def _cluster_snapshot(context: str, namespace: str) -> dict[str, object]:
    raw = _run([
        "kubectl", "--context", context, "config", "view", "--minify", "-o", "json",
    ])
    try:
        config = json.loads(raw)
        api_server = config["clusters"][0]["cluster"]["server"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseError("kubectl context did not return a usable API server") from exc
    if not isinstance(api_server, str) or not api_server:
        raise ReleaseError("kubectl context did not return a usable API server")
    nodes = _run(["kubectl", "--context", context, "get", "nodes", "-o", "name"])
    return {
        "context": context,
        "api_server": api_server,
        "nodes": [line for line in nodes.splitlines() if line],
        "namespace": namespace,
    }


def _live_deployments(context: str, namespace: str) -> list[dict[str, object]]:
    raw = _run([
        "kubectl", "--context", context, "-n", namespace,
        "get", "deployments", "-o", "json",
    ])
    try:
        payload = json.loads(raw)
        deployments = payload["items"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseError("kubectl returned invalid deployment JSON") from exc
    if not isinstance(deployments, list):
        raise ReleaseError("kubectl returned invalid deployment items")
    return [item for item in deployments if isinstance(item, dict)]


def _repository(image: object) -> str | None:
    if not isinstance(image, str) or not image:
        return None
    repository = image.split("@", 1)[0]
    colon = repository.rfind(":")
    slash = repository.rfind("/")
    return repository[:colon] if colon > slash else repository


def _updates(images: list[dict[str, str]], deployments: list[dict[str, object]]) -> list[dict[str, str]]:
    by_repository = {item["repository"]: item for item in images}
    updates: list[dict[str, str]] = []
    for deployment in deployments:
        metadata = deployment.get("metadata")
        spec = deployment.get("spec")
        template = spec.get("template") if isinstance(spec, dict) else None
        pod_spec = template.get("spec") if isinstance(template, dict) else None
        containers = _containers(pod_spec)
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str):
            continue
        for container in containers:
            if not isinstance(container, dict):
                continue
            repository = _repository(container.get("image"))
            image = by_repository.get(repository or "")
            if not image or not isinstance(container.get("name"), str):
                continue
            expected = f"{repository}@{image['digest']}"
            if container.get("image") != expected:
                updates.append({
                    "deployment": name,
                    "container": container["name"],
                    "image": expected,
                })
    return updates


def _blocked_consumers(context: str, namespace: str, images: list[dict[str, str]]) -> list[str]:
    raw = _run([
        "kubectl", "--context", context, "-n", namespace,
        "get", "statefulsets,daemonsets,jobs,cronjobs", "-o", "json",
    ])
    try:
        workloads = json.loads(raw)["items"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReleaseError("kubectl returned invalid non-Deployment workload JSON") from exc
    if not isinstance(workloads, list):
        raise ReleaseError("kubectl returned invalid non-Deployment workload items")
    repositories = {image["repository"] for image in images}
    blocked: list[str] = []
    for workload in workloads:
        if not isinstance(workload, dict):
            continue
        kind = workload.get("kind")
        metadata = workload.get("metadata")
        spec = workload.get("spec")
        if not isinstance(kind, str) or not isinstance(metadata, dict) or not isinstance(spec, dict):
            continue
        if kind == "Job" and _terminal_job(workload):
            continue
        if kind == "CronJob":
            job_template = spec.get("jobTemplate")
            job_spec = job_template.get("spec") if isinstance(job_template, dict) else None
            template = job_spec.get("template") if isinstance(job_spec, dict) else None
        else:
            template = spec.get("template")
        pod_spec = template.get("spec") if isinstance(template, dict) else None
        containers = _containers(pod_spec)
        if any(
            isinstance(container, dict) and _repository(container.get("image")) in repositories
            for container in containers
        ):
            name = metadata.get("name")
            if isinstance(name, str):
                blocked.append(f"{kind}/{name}")
    return blocked


def _containers(pod_spec: object) -> list[object]:
    if not isinstance(pod_spec, dict):
        return []
    return [
        container
        for field in ("containers", "initContainers")
        for container in (pod_spec.get(field) if isinstance(pod_spec.get(field), list) else [])
    ]


def _terminal_job(workload: dict[str, object]) -> bool:
    status = workload.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    return isinstance(conditions, list) and any(
        isinstance(condition, dict)
        and condition.get("type") in {"Complete", "Failed"}
        and condition.get("status") == "True"
        for condition in conditions
    )


def _plan(args: argparse.Namespace, commit: str, branch: str, cluster: dict[str, object]) -> dict[str, object]:
    run = _ci_run(branch, commit, timeout=args.run_timeout, poll=args.poll_interval)
    with tempfile.TemporaryDirectory(prefix="aiops-release-") as temporary:
        images = [
            _download_digest(int(run["id"]), service, Path(temporary), commit)
            for service in args.services
        ]
    deployments = _live_deployments(args.context, args.namespace)
    return {
        "commit": commit,
        "branch": branch,
        "ci_run": run,
        "cluster": cluster,
        "images": images,
        "updates": _updates(images, deployments),
        "blocked_consumers": _blocked_consumers(args.context, args.namespace, images),
    }


def _apply(args: argparse.Namespace, plan: dict[str, object]) -> None:
    blocked = plan.get("blocked_consumers")
    if not isinstance(blocked, list):
        raise ReleaseError("release plan has invalid blocked consumers")
    if blocked:
        raise ReleaseError(f"cannot safely refresh non-Deployment consumers: {', '.join(blocked)}")
    updates = plan["updates"]
    if not isinstance(updates, list):
        raise ReleaseError("release plan has invalid updates")
    by_deployment: dict[str, list[dict[str, object]]] = {}
    for update in updates:
        if not isinstance(update, dict):
            raise ReleaseError("release plan has invalid update")
        deployment = update.get("deployment")
        if not isinstance(deployment, str):
            raise ReleaseError("release plan has invalid deployment")
        by_deployment.setdefault(deployment, []).append(update)
    for deployment, changes in by_deployment.items():
        _run([
            "kubectl", "--context", args.context, "-n", args.namespace,
            "set", "image", f"deployment/{deployment}",
            *[f"{change['container']}={change['image']}" for change in changes],
        ])
    for deployment in by_deployment:
        _run([
            "kubectl", "--context", args.context, "-n", args.namespace,
            "rollout", "status", f"deployment/{deployment}",
            f"--timeout={args.rollout_timeout}",
        ])
        _run([
            "kubectl", "--context", args.context, "-n", args.namespace,
            "wait", "--for=condition=available", f"deployment/{deployment}",
            f"--timeout={args.rollout_timeout}",
        ])
    images = plan.get("images")
    if not isinstance(images, list):
        raise ReleaseError("release plan has invalid images")
    remaining = _updates(images, _live_deployments(args.context, args.namespace))
    if remaining:
        raise ReleaseError("live Deployment images do not match the CI digests")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="print the digest-bound deployment plan")
    mode.add_argument("--apply", action="store_true", help="push, wait for CI, update and verify live Deployments")
    parser.add_argument("--context", required=True, help="explicit kubectl context for the live cluster")
    parser.add_argument("--namespace", required=True, help="live namespace to update")
    parser.add_argument("--service", action="append", dest="services", choices=SERVICE_REPOSITORIES)
    parser.add_argument("--run-timeout", type=float, default=1_800, help="CI wait timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=10, help="CI poll interval in seconds")
    parser.add_argument("--rollout-timeout", default="5m", help="kubectl rollout timeout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.services = args.services or list(SERVICE_REPOSITORIES)
    try:
        if args.apply:
            if _run(["git", "status", "--porcelain"]):
                print("release_local_images: uncommitted changes are not included", file=sys.stderr)
            _run(["git", "push", "origin", "HEAD"])
        commit = _git_commit()
        branch = _git_branch()
        cluster = _cluster_snapshot(args.context, args.namespace)
        plan = _plan(args, commit, branch, cluster)
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
        if args.apply:
            _apply(args, plan)
    except ReleaseError as exc:
        print(f"release_local_images: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
