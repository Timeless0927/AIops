"""Deterministic remediation action schema builder."""

from __future__ import annotations

import re
from typing import Any, Dict


ACTION_SCHEMA_VERSION = "remediation.action.v1"
DEFAULT_MAX_REPLICAS = 20

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_SCALE_RE = re.compile(r"^扩容\s+deployment/([a-z0-9]([-a-z0-9]*[a-z0-9])?)\s+到\s+([0-9]+)\s+副本$")
_RESTART_RE = re.compile(r"^重启\s+deployment/([a-z0-9]([-a-z0-9]*[a-z0-9])?)$")


def build_remediation_context(
    analysis_action: str,
    *,
    incident_id: str,
    alertname: str | None,
    cluster: str | None,
    namespace: str | None,
    max_replicas: int = DEFAULT_MAX_REPLICAS,
) -> Dict[str, Any]:
    """Build approval context for an analysis suggestion without executing it."""
    action = str(analysis_action or "").strip()
    normalized_cluster = str(cluster or "default").strip() or "default"
    normalized_namespace = str(namespace or "").strip()

    scale_match = _SCALE_RE.match(action)
    if scale_match:
        resource_name = scale_match.group(1)
        replicas = int(scale_match.group(3))
        if not _valid_namespace(normalized_namespace):
            return _non_executable_context(action, normalized_cluster, normalized_namespace, "invalid_namespace")
        if not _valid_resource_name(resource_name):
            return _non_executable_context(action, normalized_cluster, normalized_namespace, "invalid_resource_name")
        if replicas < 0 or replicas > max_replicas:
            return _non_executable_context(action, normalized_cluster, normalized_namespace, "invalid_replicas")
        signature = (
            f"scale_deployment:{normalized_cluster}:{normalized_namespace}:"
            f"deployment/{resource_name}:replicas={replicas}"
        )
        return _executable_context(
            signature=signature,
            action_type="scale_deployment",
            cluster=normalized_cluster,
            namespace=normalized_namespace,
            resource_name=resource_name,
            parameters={"replicas": replicas},
            incident_id=incident_id,
            alertname=alertname,
            analysis_action=action,
        )

    restart_match = _RESTART_RE.match(action)
    if restart_match:
        resource_name = restart_match.group(1)
        if not _valid_namespace(normalized_namespace):
            return _non_executable_context(action, normalized_cluster, normalized_namespace, "invalid_namespace")
        if not _valid_resource_name(resource_name):
            return _non_executable_context(action, normalized_cluster, normalized_namespace, "invalid_resource_name")
        signature = f"restart_deployment:{normalized_cluster}:{normalized_namespace}:deployment/{resource_name}"
        return _executable_context(
            signature=signature,
            action_type="restart_deployment",
            cluster=normalized_cluster,
            namespace=normalized_namespace,
            resource_name=resource_name,
            parameters={"strategy": "rollout_restart"},
            incident_id=incident_id,
            alertname=alertname,
            analysis_action=action,
        )

    return _non_executable_context(action, normalized_cluster, normalized_namespace, "unsupported_action")


def _executable_context(
    *,
    signature: str,
    action_type: str,
    cluster: str,
    namespace: str,
    resource_name: str,
    parameters: Dict[str, Any],
    incident_id: str,
    alertname: str | None,
    analysis_action: str,
) -> Dict[str, Any]:
    remediation_action = {
        "action_schema_version": ACTION_SCHEMA_VERSION,
        "action_signature": signature,
        "action_type": action_type,
        "cluster": cluster,
        "namespace": namespace,
        "resource_kind": "deployment",
        "resource_name": resource_name,
        "parameters": parameters,
        "source": {
            "incident_id": incident_id,
            "alertname": alertname,
            "analysis_action": analysis_action,
        },
        "risk": {"risk_level": "low", "operation_type": "k8s_write"},
    }
    return {
        "action_signature": signature,
        "executable": True,
        "remediation_action": remediation_action,
    }


def _non_executable_context(action: str, cluster: str, namespace: str, reason: str) -> Dict[str, Any]:
    return {
        "action_signature": f"non_executable:{cluster}:{namespace}:{action}",
        "executable": False,
        "non_executable_reason": reason,
    }


def _valid_namespace(namespace: str) -> bool:
    return bool(namespace) and _valid_resource_name(namespace)


def _valid_resource_name(name: str) -> bool:
    return bool(name) and len(name) <= 63 and bool(_DNS_LABEL_RE.match(name))

