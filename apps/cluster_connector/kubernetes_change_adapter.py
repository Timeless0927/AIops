"""Connector-only Kubernetes API Adapter for live validation and server dry-run."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from aiops.contracts.kubernetes_change import (
    KubernetesChangeContractError,
    validate_draft_kubernetes_change,
)


_SENSITIVE_KEY = re.compile(r"(?i)(?:password|passwd|token|secret|api[_-]?key|credential|private[_-]?key)")
_IGNORED_DIFF_PATHS = {
    "/metadata/creationTimestamp",
    "/metadata/generation",
    "/metadata/managedFields",
    "/metadata/resourceVersion",
    "/metadata/uid",
}


class KubernetesAdapterError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def execute_validation_command(
    command: dict[str, object],
    *,
    connector_cluster_id: str,
    allowed_namespaces: set[str],
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, object]:
    """Perform exact discovery, live read, canonicalization, and API Server dry-run."""

    try:
        change = _command_change(command, connector_cluster_id, allowed_namespaces)
        client = (client_factory or _dynamic_client)()
        validation = _validate_with_server(client, change)
        return {"status": "succeeded", "validation": validation}
    except (KubernetesAdapterError, KubernetesChangeContractError) as exc:
        code = exc.code if isinstance(exc, KubernetesAdapterError) else "invalid_kubernetes_change"
        return {"status": "rejected", "error_code": code, "error_message": str(exc)}
    except Exception as exc:  # Kubernetes client exception types vary by API group and transport.
        status = getattr(exc, "status", None)
        if status in {400, 409, 422}:
            code, message = "server_dry_run_rejected", "API Server rejected the dry-run request"
        elif status in {401, 403}:
            code, message = "kubernetes_forbidden", "Connector is not authorized for the exact Kubernetes target"
        elif status == 404:
            code, message = "gvk_not_found", "Exact Kubernetes GVK was not found by discovery"
        else:
            code, message = "kubernetes_api_unavailable", "Kubernetes API request failed"
        return {"status": "rejected", "error_code": code, "error_message": message}


def _command_change(
    command: dict[str, object], connector_cluster_id: str, allowed_namespaces: set[str]
) -> dict[str, object]:
    if command.get("action") != "validate_kubernetes_change" or command.get("cluster_id") != connector_cluster_id:
        raise KubernetesAdapterError("identity_mismatch", "validation command belongs to another Cluster")
    parameters = command.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {"change"}:
        raise KubernetesAdapterError("invalid_validation_command", "validation command parameters are invalid")
    change = validate_draft_kubernetes_change(parameters.get("change"))
    target = change["target"]
    assert isinstance(target, dict)
    namespace = target["namespace"]
    if "*" not in allowed_namespaces and (namespace is None or namespace not in allowed_namespaces):
        raise KubernetesAdapterError("namespace_forbidden", "target is outside Connector namespace scope")
    return change


def _validate_with_server(client: Any, change: dict[str, object]) -> dict[str, object]:
    target = change["target"]
    assert isinstance(target, dict)
    resource = client.resources.get(api_version=target["api_version"], kind=target["kind"])
    if (
        getattr(resource, "api_version", None) != target["api_version"]
        or getattr(resource, "kind", None) != target["kind"]
    ):
        raise KubernetesAdapterError("discovery_mismatch", "API discovery did not return the exact GVK")
    if "/" in str(getattr(resource, "name", "")):
        raise KubernetesAdapterError("subresource_forbidden", "Kubernetes subresources are forbidden")
    namespaced = bool(getattr(resource, "namespaced", False))
    namespace = target["namespace"]
    if namespaced != (namespace is not None):
        raise KubernetesAdapterError("scope_mismatch", "target namespace does not match API discovery scope")
    operation = str(change["operation"])
    required_verb = {"create": "create", "patch": "patch", "delete": "delete"}[operation]
    verbs = set(getattr(resource, "verbs", ()) or ())
    if not {"get", required_verb} <= verbs:
        raise KubernetesAdapterError("verb_unsupported", "discovered resource does not support required operations")

    live = _read_live(client, resource, str(target["name"]), namespace)
    if operation == "create" and live is not None:
        raise KubernetesAdapterError("target_exists", "create target already exists")
    if operation != "create" and live is None:
        raise KubernetesAdapterError("target_not_found", "existing target was not found")

    live_identity = _live_identity(live)
    canonical = _canonical_change(change, live_identity, live)
    payload = canonical["payload"]
    if operation == "create":
        final = _as_dict(client.create(resource, body=payload, namespace=namespace, dry_run="All"))
    elif operation == "patch":
        final = _as_dict(client.patch(
            resource,
            name=target["name"],
            namespace=namespace,
            body=payload,
            content_type="application/json-patch+json",
            dry_run="All",
        ))
    else:
        dry_run_delete = copy.deepcopy(payload)
        assert isinstance(dry_run_delete, dict)
        dry_run_delete["dryRun"] = ["All"]
        client.delete(
            resource,
            name=target["name"],
            namespace=namespace,
            body=dry_run_delete,
            dry_run="All",
        )
        final = None
    before = _redact(live)
    after = _redact(final)
    diff = _object_diff(before, after)
    digest_input = {"canonical_change": canonical, "diff": diff}
    return {
        "discovery": {
            "api_version": target["api_version"],
            "kind": target["kind"],
            "resource": str(getattr(resource, "name", "")),
            "namespaced": namespaced,
            "verbs": sorted(verbs),
        },
        "live": {
            "exists": live is not None,
            "uid": live_identity["uid"],
            "resource_version": live_identity["resource_version"],
        },
        "canonical_change": canonical,
        "dry_run": {
            "diff": diff,
            "hash": hashlib.sha256(_json(digest_input).encode()).hexdigest(),
        },
    }


def _read_live(client: Any, resource: Any, name: str, namespace: object) -> dict[str, object] | None:
    try:
        return _as_dict(client.get(resource, name=name, namespace=namespace))
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return None
        raise


def _live_identity(live: dict[str, object] | None) -> dict[str, str | None]:
    if live is None:
        return {"uid": None, "resource_version": None}
    metadata = live.get("metadata")
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    version = metadata.get("resourceVersion") if isinstance(metadata, dict) else None
    if not isinstance(uid, str) or not uid or not isinstance(version, str) or not version:
        raise KubernetesAdapterError("live_precondition_missing", "live target lacks UID or resourceVersion")
    return {"uid": uid, "resource_version": version}


def _canonical_change(
    change: dict[str, object],
    identity: dict[str, str | None],
    live: dict[str, object] | None,
) -> dict[str, object]:
    target = copy.deepcopy(change["target"])
    assert isinstance(target, dict)
    target.update(identity)
    operation = change["operation"]
    if operation == "patch":
        assert live is not None
        tests = [
            {"op": "test", "path": "/metadata/uid", "value": identity["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": identity["resource_version"]},
        ]
        payload = change["payload"]
        assert isinstance(payload, list)
        for item in payload:
            path = str(item["path"])
            present, old_value = _pointer_value(live, path)
            if item["op"] in {"remove", "replace"} and not present:
                raise KubernetesAdapterError("old_value_missing", f"patch path does not exist: {path}")
            if present:
                tests.append({"op": "test", "path": path, "value": old_value})
        canonical_payload: object = tests + copy.deepcopy(payload)
    elif operation == "delete":
        delete = change["payload"]
        assert isinstance(delete, dict)
        canonical_payload = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": delete["propagation_policy"],
            "preconditions": {"uid": identity["uid"], "resourceVersion": identity["resource_version"]},
        }
    else:
        canonical_payload = copy.deepcopy(change["payload"])
    return {
        "target": target,
        "operation": operation,
        "payload": canonical_payload,
        "post_checks": copy.deepcopy(change["post_checks"]),
    }


def _pointer_value(document: object, pointer: str) -> tuple[bool, object]:
    current = document
    for raw_part in pointer.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, copy.deepcopy(current)


def _object_diff(before: object, after: object, path: str = "") -> list[dict[str, object]]:
    if path in _IGNORED_DIFF_PATHS:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result: list[dict[str, object]] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}/{_escape_pointer(key)}"
            if key not in before:
                result.append({"op": "add", "path": child, "before": None, "after": after[key]})
            elif key not in after:
                result.append({"op": "remove", "path": child, "before": before[key], "after": None})
            else:
                result.extend(_object_diff(before[key], after[key], child))
        return result
    if isinstance(before, list) and isinstance(after, list):
        if before == after:
            return []
        return [{"op": "replace", "path": path, "before": before, "after": after}]
    if before == after:
        return []
    operation = "add" if before is None else "remove" if after is None else "replace"
    return [{"op": operation, "path": path, "before": before, "after": after}]


def _redact(value: object, key: str = "") -> object:
    if _SENSITIVE_KEY.search(key):
        encoded = _json(value)
        return {"redacted": True, "sha256": hashlib.sha256(encoded.encode()).hexdigest()}
    if isinstance(value, dict):
        secret = value.get("kind") == "Secret" and isinstance(value.get("apiVersion"), str)
        named_sensitive_value = (
            isinstance(value.get("name"), str) and _SENSITIVE_KEY.search(str(value["name"])) is not None
        )
        return {
            item_key: _redact_secret_map(item)
            if secret and item_key in {"data", "stringData"}
            else _redact(item, "secret")
            if named_sensitive_value and item_key in {"value", "valueFrom"}
            else _redact(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return copy.deepcopy(value)


def _as_dict(value: Any) -> dict[str, object]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    converted = value.to_dict() if hasattr(value, "to_dict") else None
    if not isinstance(converted, dict):
        raise KubernetesAdapterError("invalid_kubernetes_response", "Kubernetes API response is not an object")
    return converted


def _dynamic_client() -> Any:
    from kubernetes import client, config  # type: ignore
    from kubernetes.config.config_exception import ConfigException  # type: ignore
    from kubernetes.dynamic import DynamicClient  # type: ignore

    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config()
    return DynamicClient(client.ApiClient())


def _escape_pointer(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _redact_secret_map(value: object) -> object:
    if not isinstance(value, dict):
        return {"redacted": True, "sha256": hashlib.sha256(_json(value).encode()).hexdigest()}
    return {
        key: {"redacted": True, "sha256": hashlib.sha256(_json(item).encode()).hexdigest()}
        for key, item in value.items()
    }
