"""Kubernetes kube-context resolution helpers."""

from __future__ import annotations

import json
import os
from typing import Any


KUBE_CONTEXT_MAP_ENV = "AIOPS_KUBE_CONTEXT_MAP"
KUBE_CONTEXT_ENV = "AIOPS_KUBE_CONTEXT"


def resolve_kube_context(
    cluster_label: Any = None,
    *,
    explicit_context: Any = None,
) -> str | None:
    """Resolve a business cluster label to an optional kubectl context.

    No implicit label-to-context mapping is performed. In-cluster deployments
    normally leave both env vars empty so kubectl uses its current service
    account credentials.
    """

    explicit = _clean_context(explicit_context, "explicit kube context")
    if explicit:
        return explicit

    label = _clean_context(cluster_label, "cluster label")
    mapping = load_kube_context_map()
    if label and label in mapping:
        return mapping[label]

    return _clean_context(os.getenv(KUBE_CONTEXT_ENV), KUBE_CONTEXT_ENV)


def load_kube_context_map() -> dict[str, str]:
    """Load the explicit business-cluster-to-kube-context mapping from env."""

    raw = str(os.getenv(KUBE_CONTEXT_MAP_ENV) or "").strip()
    if not raw:
        return {}

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{KUBE_CONTEXT_MAP_ENV} must be a JSON object") from exc

    if not isinstance(loaded, dict):
        raise ValueError(f"{KUBE_CONTEXT_MAP_ENV} must be a JSON object")

    mapping: dict[str, str] = {}
    for key, value in loaded.items():
        label = _clean_context(key, "cluster label")
        context = _clean_context(value, "kube context")
        if label and context:
            mapping[label] = context
    return mapping


def _clean_context(value: Any, field_name: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if any(ch in text for ch in "\r\n\0"):
        raise ValueError(f"{field_name} must not contain control characters")
    return text
