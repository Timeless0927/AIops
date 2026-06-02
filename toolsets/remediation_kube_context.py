"""Kubernetes context resolution for structured remediation actions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_ENV_MAPPING_KEYS = ("AIOPS_KUBE_CONTEXT_MAP", "AIOPS_CLUSTER_CONTEXT_MAP")


def resolve_kube_context(action: dict[str, Any], *, config: dict[str, Any] | None = None) -> str | None:
    """Resolve an optional kube context without treating cluster labels as contexts."""

    explicit = _first_text(action.get("kube_context"), action.get("context"))
    if explicit:
        return _validated_context(explicit, "action kube_context")

    cluster = _text(action.get("cluster"))
    if not cluster:
        return None

    mapped = _lookup_mapping(cluster, _env_cluster_context_map())
    if mapped is not None:
        return _validated_context(mapped, f"env mapping for cluster {cluster}") if mapped else None

    mapped = _lookup_mapping(cluster, _config_cluster_context_map(config))
    if mapped is not None:
        return _validated_context(mapped, f"config mapping for cluster {cluster}") if mapped else None

    return None


def _lookup_mapping(cluster: str, mapping: dict[str, Any]) -> str | None:
    if cluster not in mapping:
        return None
    return _text(mapping.get(cluster))


def _env_cluster_context_map() -> dict[str, Any]:
    for key in _ENV_MAPPING_KEYS:
        value = _text(os.getenv(key))
        if value:
            return _parse_mapping(value, key)
    return {}


def _config_cluster_context_map(config: dict[str, Any] | None) -> dict[str, Any]:
    effective_config = config if config is not None else _load_runtime_config()
    if not isinstance(effective_config, dict):
        return {}

    for section_name in ("kubernetes", "k8s", "sre"):
        section = effective_config.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in ("cluster_contexts", "kube_contexts", "cluster_context_map"):
            value = section.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip():
                return _parse_mapping(value, f"{section_name}.{key}")
    return {}


def _parse_mapping(value: str, source: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = _parse_csv_mapping(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{source} must be a JSON object or comma-separated cluster=context mapping")
    return parsed


def _parse_csv_mapping(value: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in value.split(","):
        entry = item.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError("cluster context mapping entries must use cluster=context")
        cluster, context = entry.split("=", 1)
        mapping[cluster.strip()] = context.strip()
    return mapping


def _load_runtime_config() -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        return {}

    for path in _runtime_config_candidates():
        try:
            if not path.exists():
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError:
            continue
        return data if isinstance(data, dict) else {}
    return {}


def _runtime_config_candidates() -> list[Path]:
    candidates: list[Path] = []
    hermes_config = os.getenv("HERMES_CONFIG")
    if hermes_config:
        candidates.append(Path(hermes_config).expanduser())
    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "config.yaml")
    return candidates


def _validated_context(context: str, source: str) -> str:
    if any(ch.isspace() for ch in context) or any(ch in context for ch in "\r\n\0"):
        raise ValueError(f"invalid kube context from {source}")
    return context


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip()
