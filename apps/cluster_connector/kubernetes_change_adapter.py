"""Connector-only Kubernetes API Adapter for live validation and server dry-run."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiops.contracts.kubernetes_change import (
    CONTROLLED_RESTART_ANNOTATION_PATH,
    CONTROLLED_RESTART_ANNOTATIONS_PATH,
    KubernetesChangeContractError,
    validate_draft_kubernetes_change,
)
from aiops.security import (
    DEFAULT_CHANGE_KEY_PATH,
    SecureInputCryptoError,
    materialize_secure_input_placeholders,
    redact_materialized_secure_values,
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
    def __init__(
        self, code: str, message: str, *, execution: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.execution = execution


def execute_validation_command(
    command: dict[str, object],
    *,
    connector_cluster_id: str,
    allowed_namespaces: set[str],
    client_factory: Callable[[], Any] | None = None,
    secure_input_key_path: Path | str = DEFAULT_CHANGE_KEY_PATH,
) -> dict[str, object]:
    """Perform exact discovery, live read, canonicalization, and API Server dry-run."""

    try:
        change, secure_refs = _command_change(command, connector_cluster_id, allowed_namespaces)
        materialized, plaintext = materialize_secure_input_placeholders(
            change, secure_refs, key_path=secure_input_key_path,
        )
        assert isinstance(materialized, dict)
        client = (client_factory or _dynamic_client)()
        validation = _validate_with_server(
            client, materialized, secure_refs=secure_refs,
            plaintext_by_placeholder=plaintext,
        )
        return {"status": "succeeded", "validation": validation}
    except (KubernetesAdapterError, KubernetesChangeContractError, SecureInputCryptoError) as exc:
        code = (
            exc.code if isinstance(exc, KubernetesAdapterError)
            else "secure_input_unavailable" if isinstance(exc, SecureInputCryptoError)
            else "invalid_kubernetes_change"
        )
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


def execute_change_command(
    command: dict[str, object],
    *,
    connector_cluster_id: str,
    allowed_namespaces: set[str],
    now: float,
    client_factory: Callable[[], Any] | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    secure_input_key_path: Path | str = DEFAULT_CHANGE_KEY_PATH,
) -> dict[str, object]:
    """Revalidate and execute one frozen canonical Change through the Dynamic API."""

    try:
        change, timeout, secure_refs = _execution_change(
            command, connector_cluster_id, allowed_namespaces, now,
        )
        materialized, _plaintext = materialize_secure_input_placeholders(
            change, secure_refs, key_path=secure_input_key_path,
        )
        assert isinstance(materialized, dict)
        client = (client_factory or _dynamic_client)()
        execution = _execute_with_server(
            client, materialized, deadline=now + timeout, clock=clock, sleeper=sleeper,
        )
        return {"status": "succeeded", "execution": execution}
    except (KubernetesAdapterError, SecureInputCryptoError) as exc:
        if isinstance(exc, SecureInputCryptoError):
            return {
                "status": "rejected", "error_code": "secure_input_unavailable",
                "error_message": str(exc),
            }
        status = "rejected" if exc.code in {
            "execution_grant_invalid", "identity_mismatch", "namespace_forbidden", "stale_change",
            "discovery_mismatch", "subresource_forbidden", "scope_mismatch", "verb_unsupported",
        } else "failed"
        result = {"status": status, "error_code": exc.code, "error_message": str(exc)}
        if exc.execution is not None:
            result["execution"] = exc.execution
        return result
    except Exception as exc:  # Kubernetes client exception types vary by API group and transport.
        status = getattr(exc, "status", None)
        if status in {400, 409, 422}:
            code, message = "kubernetes_api_rejected", "API Server rejected the Kubernetes Change"
        elif status in {401, 403}:
            code, message = "kubernetes_forbidden", "Connector is not authorized for the exact Kubernetes target"
        else:
            code, message = "kubernetes_api_unavailable", "Kubernetes API request failed"
        return {"status": "failed", "error_code": code, "error_message": message}


def observe_change_state(
    change: dict[str, object], *, client_factory: Callable[[], Any] | None = None,
) -> tuple[dict[str, object] | None, dict[str, object], list[dict[str, object]]]:
    """Read one frozen target and evaluate its post-checks without mutation."""

    target = change.get("target")
    if not isinstance(target, dict):
        raise KubernetesAdapterError("invalid_reconciliation_command", "frozen target is invalid")
    client = (client_factory or _dynamic_client)()
    resource, _, _ = _discover_resource(client, target, "get")
    live = _read_live(
        client, resource, str(target.get("name")), target.get("namespace"),
        request_timeout=None,
    )
    checks = [_evaluate_post_check(item, live) for item in change.get("post_checks", [])]
    return live, _target_result(live), checks


def _execution_change(
    command: dict[str, object], connector_cluster_id: str,
    allowed_namespaces: set[str], now: float,
) -> tuple[dict[str, object], int, list[dict[str, object]]]:
    parameters = command.get("parameters")
    grant = parameters.get("grant") if isinstance(parameters, dict) else None
    change = parameters.get("change") if isinstance(parameters, dict) else None
    secure_inputs = parameters.get("secure_inputs", []) if isinstance(parameters, dict) else None
    digest = hashlib.sha256(_json(change).encode()).hexdigest() if isinstance(change, dict) else ""
    if (
        command.get("action") != "execute_kubernetes_change"
        or command.get("cluster_id") != connector_cluster_id
        or not isinstance(grant, dict)
        or set(grant) != {
            "id", "phase_id", "approval_id", "change_hash", "issued_at", "expires_at",
            "execution_timeout_seconds",
        }
        or not isinstance(change, dict)
        or set(change) != {"target", "operation", "payload", "post_checks"}
        or not isinstance(secure_inputs, list)
        or any(not isinstance(item, dict) for item in secure_inputs)
        or command.get("execution_grant_id") != grant.get("id")
        or command.get("execution_grant_expires_at") != grant.get("expires_at")
        or grant.get("change_hash") != digest
        or command.get("action_hash") != digest
        or isinstance(grant.get("expires_at"), bool)
        or not isinstance(grant.get("expires_at"), (int, float))
        or isinstance(grant.get("issued_at"), bool)
        or not isinstance(grant.get("issued_at"), (int, float))
        or float(grant["issued_at"]) > now
        or float(grant["expires_at"]) <= now
        or isinstance(grant.get("execution_timeout_seconds"), bool)
        or not isinstance(grant.get("execution_timeout_seconds"), int)
        or not 300 <= int(grant["execution_timeout_seconds"]) <= 1800
    ):
        raise KubernetesAdapterError("execution_grant_invalid", "execution grant or frozen Change is invalid")
    target = change.get("target")
    if not isinstance(target, dict) or set(target) != {
        "api_version", "kind", "namespace", "name", "uid", "resource_version",
    } or change.get("operation") not in {"create", "patch", "delete"}:
        raise KubernetesAdapterError("execution_grant_invalid", "frozen canonical Change is invalid")
    namespace = target.get("namespace")
    if command.get("namespace") != (namespace or "default"):
        raise KubernetesAdapterError("identity_mismatch", "command namespace conflicts with frozen target")
    if "*" not in allowed_namespaces and (namespace is None or namespace not in allowed_namespaces):
        raise KubernetesAdapterError("namespace_forbidden", "target is outside Connector namespace scope")
    return (
        copy.deepcopy(change), int(grant["execution_timeout_seconds"]),
        copy.deepcopy(secure_inputs),  # type: ignore[arg-type]
    )


def _execute_with_server(
    client: Any, change: dict[str, object], *, deadline: float,
    clock: Callable[[], float] | None, sleeper: Callable[[float], None],
) -> dict[str, object]:
    target = change["target"]
    assert isinstance(target, dict)
    operation = str(change["operation"])
    resource, _, _ = _discover_resource(
        client, target, operation, deadline=deadline, clock=clock,
    )
    timeout = _request_timeout(deadline, clock)
    live = _read_live(
        client, resource, str(target["name"]), target["namespace"], request_timeout=timeout,
    )
    _revalidate_preconditions(change, live)
    payload = copy.deepcopy(change["payload"])
    timeout = _request_timeout(deadline, clock)
    timeout_args = {"_request_timeout": timeout} if timeout is not None else {}
    if operation == "create":
        client.create(resource, body=payload, namespace=target["namespace"], **timeout_args)
    elif operation == "patch":
        client.patch(
            resource, name=target["name"], namespace=target["namespace"], body=payload,
            content_type="application/json-patch+json", **timeout_args,
        )
    else:
        client.delete(
            resource, name=target["name"], namespace=target["namespace"], body=payload,
            **timeout_args,
        )
    while True:
        final = _read_live(
            client, resource, str(target["name"]), target["namespace"],
            request_timeout=_request_timeout(
                deadline, clock, expired_code="post_check_failed",
            ),
        )
        checks = [_evaluate_post_check(item, final) for item in change["post_checks"]]  # type: ignore[union-attr]
        execution = {
            "operation": operation, "target": _target_result(final), "post_checks": checks,
        }
        if all(item["status"] == "succeeded" for item in checks):
            return execution
        if clock is None or clock() >= deadline:
            raise KubernetesAdapterError(
                "post_check_failed", "frozen Kubernetes post-check failed",
                execution=execution,
            )
        sleeper(min(1.0, max(0.0, deadline - clock())))


def _revalidate_preconditions(
    change: dict[str, object], live: dict[str, object] | None,
) -> None:
    target = change["target"]
    assert isinstance(target, dict)
    operation = change["operation"]
    if operation == "create":
        if live is not None or target["uid"] is not None or target["resource_version"] is not None:
            raise KubernetesAdapterError("stale_change", "create target now exists")
        return
    if live is None:
        raise KubernetesAdapterError("stale_change", "frozen target no longer exists")
    identity = _live_identity(live)
    if identity != {"uid": target["uid"], "resource_version": target["resource_version"]}:
        raise KubernetesAdapterError("stale_change", "frozen target identity or resourceVersion changed")
    payload = change["payload"]
    if operation == "patch":
        if not isinstance(payload, list) or not payload:
            raise KubernetesAdapterError("execution_grant_invalid", "canonical patch is invalid")
        tests: list[dict[str, object]] = []
        mutations: list[dict[str, object]] = []
        seen_mutation = False
        for item in payload:
            if not isinstance(item, dict) or item.get("op") not in {"test", "add", "remove", "replace"}:
                raise KubernetesAdapterError("execution_grant_invalid", "canonical patch operation is invalid")
            if item["op"] == "test":
                if seen_mutation:
                    raise KubernetesAdapterError("execution_grant_invalid", "canonical tests must precede mutation")
                tests.append(item)
            else:
                seen_mutation = True
                mutations.append(item)
        test_paths = {str(item.get("path")) for item in tests}
        required = {"/metadata/uid", "/metadata/resourceVersion"}
        required.update(
            str(item.get("path")) for item in mutations
            if item["op"] in {"remove", "replace"}
            or json_pointer_value(live, str(item.get("path")))[0]
        )
        if not mutations or required - test_paths:
            raise KubernetesAdapterError("execution_grant_invalid", "canonical patch lacks frozen old-value tests")
        if any(not _test_matches(live, item) for item in tests):
            raise KubernetesAdapterError("stale_change", "frozen RFC 6902 old-value test failed")
    elif not isinstance(payload, dict) or payload.get("preconditions") != {
        "uid": target["uid"], "resourceVersion": target["resource_version"],
    }:
        raise KubernetesAdapterError("execution_grant_invalid", "canonical delete preconditions are invalid")


def _test_matches(live: dict[str, object], item: dict[str, object]) -> bool:
    path = item.get("path")
    if not isinstance(path, str):
        return False
    present, value = json_pointer_value(live, path)
    return present and value == item.get("value")


def _evaluate_post_check(raw: object, live: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
        return {"type": "invalid", "status": "failed"}
    kind = str(raw["type"])
    passed = (live is not None) if kind == "exists" else (live is None) if kind == "absent" else False
    if kind == "json_pointer" and live is not None:
        present, actual = json_pointer_value(live, str(raw.get("path") or ""))
        passed = present and _compare(actual, raw.get("operator"), raw.get("value"))
    elif kind in {"condition", "job_terminal", "crd_established"} and live is not None:
        conditions = live.get("status", {}).get("conditions", []) if isinstance(live.get("status"), dict) else []
        expected_type = raw.get("condition_type") if kind == "condition" else (
            "Complete" if raw.get("outcome") == "complete" else "Failed" if kind == "job_terminal" else "Established"
        )
        expected_status = raw.get("status", "True")
        passed = any(
            isinstance(item, dict) and item.get("type") == expected_type and item.get("status") == expected_status
            for item in conditions
        )
    elif kind == "observed_generation" and live is not None:
        metadata, status = live.get("metadata"), live.get("status")
        passed = isinstance(metadata, dict) and isinstance(status, dict) and status.get("observedGeneration") == metadata.get("generation")
    elif kind == "workload_rollout" and live is not None:
        metadata, spec, status = live.get("metadata"), live.get("spec"), live.get("status")
        replicas = spec.get("replicas", 1) if isinstance(spec, dict) else None
        passed = isinstance(metadata, dict) and isinstance(status, dict) and (
            status.get("observedGeneration") == metadata.get("generation")
            and status.get("updatedReplicas") == replicas and status.get("availableReplicas") == replicas
        )
    return {"type": kind, "status": "succeeded" if passed else "failed"}


def _target_result(live: dict[str, object] | None) -> dict[str, object]:
    identity = _live_identity(live)
    return {
        "exists": live is not None,
        "uid": identity["uid"], "resource_version": identity["resource_version"],
    }


def _compare(actual: object, operator: object, expected: object) -> bool:
    try:
        if operator == "eq":
            return actual == expected
        if operator == "ne":
            return actual != expected
        if operator == "gt":
            return actual > expected
        if operator == "gte":
            return actual >= expected
        if operator == "lt":
            return actual < expected
        if operator == "lte":
            return actual <= expected
        return False
    except TypeError:
        return False


def _command_change(
    command: dict[str, object], connector_cluster_id: str, allowed_namespaces: set[str]
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if command.get("action") != "validate_kubernetes_change" or command.get("cluster_id") != connector_cluster_id:
        raise KubernetesAdapterError("identity_mismatch", "validation command belongs to another Cluster")
    parameters = command.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) not in ({"change"}, {"change", "secure_inputs"}):
        raise KubernetesAdapterError("invalid_validation_command", "validation command parameters are invalid")
    change = validate_draft_kubernetes_change(parameters.get("change"))
    secure_inputs = parameters.get("secure_inputs", [])
    if not isinstance(secure_inputs, list) or any(not isinstance(item, dict) for item in secure_inputs):
        raise KubernetesAdapterError("invalid_validation_command", "Secure Input refs are invalid")
    target = change["target"]
    assert isinstance(target, dict)
    namespace = target["namespace"]
    if "*" not in allowed_namespaces and (namespace is None or namespace not in allowed_namespaces):
        raise KubernetesAdapterError("namespace_forbidden", "target is outside Connector namespace scope")
    return change, secure_inputs  # type: ignore[return-value]


def _validate_with_server(
    client: Any,
    change: dict[str, object],
    *,
    secure_refs: list[dict[str, object]] | None = None,
    plaintext_by_placeholder: dict[str, str] | None = None,
) -> dict[str, object]:
    target = change["target"]
    assert isinstance(target, dict)
    namespace = target["namespace"]
    operation = str(change["operation"])
    resource, namespaced, verbs = _discover_resource(client, target, operation)

    live = _read_live(client, resource, str(target["name"]), namespace)
    if operation == "create" and live is not None:
        raise KubernetesAdapterError("target_exists", "create target already exists")
    if operation != "create" and live is None:
        raise KubernetesAdapterError("target_not_found", "existing target was not found")

    live_identity = _live_identity(live)
    canonical = _canonical_change(change, live_identity, live)
    if _has_unsealed_sensitive_precondition(
        canonical, set((plaintext_by_placeholder or {}).values()),
    ):
        raise KubernetesAdapterError(
            "secure_input_precondition_unavailable",
            "Sensitive old value cannot be frozen without encrypted Secure Input",
        )
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
    refs = secure_refs or []
    plaintext = plaintext_by_placeholder or {}
    before = _redact(redact_materialized_secure_values(
        live, plaintext, public=True, refs=refs,
    ))
    after = _redact(redact_materialized_secure_values(
        final, plaintext, public=True, refs=refs,
    ))
    diff = _object_diff(before, after)
    canonical = redact_materialized_secure_values(
        canonical, plaintext, public=False, refs=refs,
    )
    assert isinstance(canonical, dict)
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


def _discover_resource(
    client: Any, target: dict[str, object], operation: str,
    *, deadline: float | None = None, clock: Callable[[], float] | None = None,
) -> tuple[Any, bool, set[str]]:
    if deadline is not None:
        _request_timeout(deadline, clock)
    resource = client.resources.get(api_version=target["api_version"], kind=target["kind"])
    if deadline is not None:
        _request_timeout(deadline, clock)
    if (
        getattr(resource, "api_version", None) != target["api_version"]
        or getattr(resource, "kind", None) != target["kind"]
    ):
        raise KubernetesAdapterError("discovery_mismatch", "API discovery did not return the exact GVK")
    if "/" in str(getattr(resource, "name", "")):
        raise KubernetesAdapterError("subresource_forbidden", "Kubernetes subresources are forbidden")
    namespaced = bool(getattr(resource, "namespaced", False))
    if namespaced != (target["namespace"] is not None):
        raise KubernetesAdapterError("scope_mismatch", "target namespace does not match API discovery scope")
    verbs = set(getattr(resource, "verbs", ()) or ())
    if not {"get", operation} <= verbs:
        raise KubernetesAdapterError("verb_unsupported", "discovered resource does not support required operations")
    return resource, namespaced, verbs


def _request_timeout(
    deadline: float, clock: Callable[[], float] | None,
    *, expired_code: str = "execution_timeout",
) -> float | None:
    if clock is None:
        return None
    remaining = deadline - clock()
    if remaining <= 0:
        raise KubernetesAdapterError(expired_code, "Kubernetes execution deadline expired")
    return max(0.1, remaining)


def _read_live(
    client: Any, resource: Any, name: str, namespace: object,
    *, request_timeout: float | None = None,
) -> dict[str, object] | None:
    try:
        timeout = {"_request_timeout": request_timeout} if request_timeout is not None else {}
        return _as_dict(client.get(resource, name=name, namespace=namespace, **timeout))
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
        preparations: list[dict[str, object]] = []
        for item in payload:
            path = str(item["path"])
            present, old_value = json_pointer_value(live, path)
            if item["op"] in {"remove", "replace"} and not present:
                raise KubernetesAdapterError("old_value_missing", f"patch path does not exist: {path}")
            if present:
                tests.append({"op": "test", "path": path, "value": old_value})
            elif path == CONTROLLED_RESTART_ANNOTATION_PATH and item["op"] == "add":
                annotations_present, annotations = json_pointer_value(
                    live, CONTROLLED_RESTART_ANNOTATIONS_PATH,
                )
                if annotations_present and not isinstance(annotations, dict):
                    raise KubernetesAdapterError(
                        "old_value_invalid", "pod-template annotations must be an object",
                    )
                if not annotations_present:
                    preparations.append({
                        "op": "add",
                        "path": CONTROLLED_RESTART_ANNOTATIONS_PATH,
                        "value": {},
                    })
        canonical_payload: object = tests + preparations + copy.deepcopy(payload)
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


def json_pointer_value(document: object, pointer: str) -> tuple[bool, object]:
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
        key: copy.deepcopy(item)
        if isinstance(item, dict) and set(item) == {"secure_input"}
        else {"redacted": True, "sha256": hashlib.sha256(_json(item).encode()).hexdigest()}
        for key, item in value.items()
    }


def _has_unsealed_sensitive_precondition(
    change: dict[str, object], secure_values: set[str],
) -> bool:
    if change.get("operation") != "patch" or not secure_values:
        return False
    payload = change.get("payload")
    if not isinstance(payload, list):
        return False
    secure_paths = {
        str(item.get("path"))
        for item in payload
        if isinstance(item, dict)
        and item.get("op") in {"add", "replace"}
        and isinstance(item.get("value"), str)
        and item["value"] in secure_values
    }
    return any(
        isinstance(item, dict)
        and item.get("op") == "test"
        and str(item.get("path")) in secure_paths
        and item.get("value") not in secure_values
        for item in payload
    )
