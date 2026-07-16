"""Structured Gateway payload for Diagnosis Kubernetes reads."""

from __future__ import annotations

from typing import Any


def gateway_read_payload(args: dict[str, Any]) -> dict[str, Any]:
    argv = args.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
        argv = _default_argv(args)
    resource_kind, separator, name = str(argv[2]).lower().partition("/")
    aliases = {
        "pod": "pods", "pods": "pods",
        "deployment": "deployments", "deployments": "deployments",
        "service": "services", "services": "services",
        "event": "events", "events": "events",
    }
    if resource_kind not in aliases:
        raise ValueError("run_k8s_read requested an unsupported resource kind")
    parameters: dict[str, Any] = {"resource_kind": aliases[resource_kind], "output": "json"}
    if separator and name:
        parameters["name"] = name
    selector = str(args.get("selector") or "").strip()
    if not selector:
        for index, value in enumerate(argv[:-1]):
            if value in {"-l", "--selector"}:
                selector = str(argv[index + 1]).strip()
                break
    if selector:
        parameters["selector"] = selector
    return {
        "cluster_id": args.get("cluster_id") or "",
        "namespace": args.get("namespace") or "",
        "parameters": parameters,
        "reason": args.get("reason") or "Diagnosis live Kubernetes evidence",
    }


def _default_argv(args: dict[str, Any]) -> list[str]:
    argv = ["kubectl", "get", "pods"]
    namespace = str(args.get("namespace") or "").strip()
    service = str(args.get("service") or "").strip()
    selector = str(args.get("selector") or "").strip()
    if namespace:
        argv.extend(["-n", namespace])
    if not selector and service:
        selector = f"app.kubernetes.io/name={service}"
    if selector:
        argv.extend(["-l", selector])
    return argv
