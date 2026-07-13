"""Pure contract for model-produced generic Kubernetes Change drafts."""

from __future__ import annotations

import copy
import hashlib
import json
import re

from aiops.security import contains_secure_input_placeholder, secure_input_ids


_API_VERSION = re.compile(r"^(?:[a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?v[0-9][a-z0-9]*$")
_KIND = re.compile(r"^[A-Z][A-Za-z0-9]{0,199}$")
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_PATCH_OPERATIONS = {"add", "remove", "replace"}
_IMMUTABLE_PATCH_PATHS = (
    "/apiVersion",
    "/kind",
    "/metadata/name",
    "/metadata/namespace",
    "/metadata/uid",
    "/metadata/resourceVersion",
    "/metadata/managedFields",
    "/status",
)
_KUBERNETES_POST_CHECKS = {
    "exists",
    "absent",
    "json_pointer",
    "condition",
    "observed_generation",
    "workload_rollout",
    "job_terminal",
    "crd_established",
}
_SENSITIVE_NAME = re.compile(
    r"(?i)^(?:password|passwd|token|secret|api[_-]?key|credential|private[_-]?key|client[_-]?secret)$"
)
_SENSITIVE_TEXT = re.compile(r"(?i)(?:\bBearer\s+[A-Za-z0-9._~+/=-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


class KubernetesChangeContractError(ValueError):
    pass


def validate_draft_kubernetes_change(raw: object) -> dict[str, object]:
    """Validate and detach one untrusted model-produced Change draft."""

    required = {"target", "operation", "payload", "post_checks"}
    if not isinstance(raw, dict) or frozenset(raw) not in {frozenset(required), frozenset(required | {"rollback"})}:
        raise KubernetesChangeContractError("Kubernetes Change fields are invalid")
    target = _target(raw.get("target"))
    operation = raw.get("operation")
    if operation not in {"create", "patch", "delete"}:
        raise KubernetesChangeContractError("operation must be create, patch, or delete")
    payload = (
        _create_payload(raw.get("payload"), target)
        if operation == "create"
        else _patch_payload(raw.get("payload"))
        if operation == "patch"
        else _delete_payload(raw.get("payload"))
    )
    if _contains_sensitive_payload(target, operation, payload):
        raise KubernetesChangeContractError("sensitive payload must be supplied through Secure Input")
    post_checks = _post_checks(raw.get("post_checks"))
    result = {
        "target": target,
        "operation": operation,
        "payload": payload,
        "post_checks": post_checks,
    }
    if "rollback" in raw:
        result["rollback"] = _rollback(raw["rollback"])
    return result


def validate_kubernetes_validation_result(raw: object, draft_raw: object) -> dict[str, object]:
    """Validate the bounded Connector result against the exact model draft."""

    draft = validate_draft_kubernetes_change(draft_raw)
    if not isinstance(raw, dict) or set(raw) != {"discovery", "live", "canonical_change", "dry_run"}:
        raise KubernetesChangeContractError("Connector validation result fields are invalid")
    target = draft["target"]
    assert isinstance(target, dict)
    discovery = raw.get("discovery")
    if not isinstance(discovery, dict) or set(discovery) != {
        "api_version", "kind", "resource", "namespaced", "verbs",
    }:
        raise KubernetesChangeContractError("Connector discovery result is invalid")
    verbs = discovery.get("verbs")
    if (
        discovery.get("api_version") != target["api_version"]
        or discovery.get("kind") != target["kind"]
        or not isinstance(discovery.get("resource"), str)
        or "/" in str(discovery.get("resource"))
        or not isinstance(discovery.get("namespaced"), bool)
        or discovery["namespaced"] != (target["namespace"] is not None)
        or not isinstance(verbs, list)
        or any(not isinstance(verb, str) for verb in verbs)
    ):
        raise KubernetesChangeContractError("Connector discovery does not match target")
    live = raw.get("live")
    if not isinstance(live, dict) or set(live) != {"exists", "uid", "resource_version"}:
        raise KubernetesChangeContractError("Connector live result is invalid")
    exists = live.get("exists")
    uid = live.get("uid")
    version = live.get("resource_version")
    if not isinstance(exists, bool) or (
        exists and (not isinstance(uid, str) or not uid or not isinstance(version, str) or not version)
    ) or (not exists and (uid is not None or version is not None)):
        raise KubernetesChangeContractError("Connector live precondition is invalid")
    operation = draft["operation"]
    if exists != (operation != "create"):
        raise KubernetesChangeContractError("Connector live existence conflicts with operation")

    canonical = raw.get("canonical_change")
    if not isinstance(canonical, dict) or set(canonical) != {"target", "operation", "payload", "post_checks"}:
        raise KubernetesChangeContractError("canonical Kubernetes Change fields are invalid")
    canonical_target = canonical.get("target")
    if not isinstance(canonical_target, dict) or set(canonical_target) != set(target) | {"uid", "resource_version"}:
        raise KubernetesChangeContractError("canonical target fields are invalid")
    if (
        any(canonical_target.get(field) != value for field, value in target.items())
        or canonical_target.get("uid") != uid
        or canonical_target.get("resource_version") != version
        or canonical.get("operation") != operation
        or canonical.get("post_checks") != draft["post_checks"]
    ):
        raise KubernetesChangeContractError("canonical Kubernetes Change conflicts with draft or live state")
    payload = canonical.get("payload")
    if operation == "create":
        if payload != draft["payload"]:
            raise KubernetesChangeContractError("canonical create payload conflicts with draft")
    elif operation == "patch":
        draft_patch = draft["payload"]
        if not isinstance(payload, list) or not isinstance(draft_patch, list) or payload[-len(draft_patch):] != draft_patch:
            raise KubernetesChangeContractError("canonical RFC 6902 patch conflicts with draft")
        tests = payload[:-len(draft_patch)]
        if not tests or any(
            not isinstance(item, dict) or set(item) != {"op", "path", "value"} or item.get("op") != "test"
            for item in tests
        ):
            raise KubernetesChangeContractError("canonical RFC 6902 patch lacks frozen tests")
        required_tests = {
            ("/metadata/uid", uid),
            ("/metadata/resourceVersion", version),
        }
        observed_tests = {(item["path"], _json_key(item["value"])) for item in tests}
        if {(path, _json_key(value)) for path, value in required_tests} - observed_tests:
            raise KubernetesChangeContractError("canonical RFC 6902 patch lacks identity tests")
        required_old_value_paths = {
            str(item["path"]) for item in draft_patch if item["op"] in {"remove", "replace"}
        }
        if required_old_value_paths - {str(item["path"]) for item in tests}:
            raise KubernetesChangeContractError("canonical RFC 6902 patch lacks relevant old-value tests")
    else:
        delete = draft["payload"]
        assert isinstance(delete, dict)
        expected = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": delete["propagation_policy"],
            "preconditions": {"uid": uid, "resourceVersion": version},
        }
        if payload != expected:
            raise KubernetesChangeContractError("canonical DeleteOptions conflicts with frozen preconditions")

    dry_run = raw.get("dry_run")
    if not isinstance(dry_run, dict) or set(dry_run) != {"diff", "hash"}:
        raise KubernetesChangeContractError("dry-run result fields are invalid")
    diff = dry_run.get("diff")
    if not isinstance(diff, list) or len(diff) > 1000:
        raise KubernetesChangeContractError("dry-run diff is invalid")
    normalized_diff: list[dict[str, object]] = []
    for item in diff:
        if (
            not isinstance(item, dict)
            or set(item) != {"op", "path", "before", "after"}
            or item.get("op") not in {"add", "remove", "replace"}
            or not isinstance(item.get("path"), str)
        ):
            raise KubernetesChangeContractError("dry-run diff entry is invalid")
        normalized_diff.append(copy.deepcopy(item))
    digest = dry_run.get("hash")
    expected_digest = hashlib.sha256(
        json.dumps(
            {"canonical_change": canonical, "diff": normalized_diff},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if not isinstance(digest, str) or digest != expected_digest:
        raise KubernetesChangeContractError("dry-run hash is invalid")
    return copy.deepcopy(raw)


def _target(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {"api_version", "kind", "namespace", "name"}:
        raise KubernetesChangeContractError("target fields are invalid")
    api_version = _text(raw.get("api_version"), "target api_version", 200)
    kind = _text(raw.get("kind"), "target kind", 200)
    name = _text(raw.get("name"), "target name", 253)
    namespace = raw.get("namespace")
    if _API_VERSION.fullmatch(api_version) is None:
        raise KubernetesChangeContractError("target api_version is invalid")
    if _KIND.fullmatch(kind) is None or "/" in kind:
        raise KubernetesChangeContractError("target kind or subresource is invalid")
    if len(name) > 253 or _NAME.fullmatch(name) is None:
        raise KubernetesChangeContractError("target name is invalid")
    if namespace is not None:
        namespace = _text(namespace, "target namespace", 63)
        if _NAME.fullmatch(namespace) is None:
            raise KubernetesChangeContractError("target namespace is invalid")
    return {"api_version": api_version, "kind": kind, "namespace": namespace, "name": name}


def _create_payload(raw: object, target: dict[str, object]) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise KubernetesChangeContractError("create payload must be a complete JSON object")
    payload = _json_value(raw, "create payload")
    assert isinstance(payload, dict)
    metadata = payload.get("metadata")
    if (
        payload.get("apiVersion") != target["api_version"]
        or payload.get("kind") != target["kind"]
        or not isinstance(metadata, dict)
        or metadata.get("name") != target["name"]
        or metadata.get("namespace") != target["namespace"]
    ):
        raise KubernetesChangeContractError("create payload identity must exactly match target")
    if "status" in payload or set(metadata) & {
        "uid", "resourceVersion", "managedFields", "creationTimestamp", "generation",
    }:
        raise KubernetesChangeContractError("create payload contains server-owned fields")
    return payload


def _patch_payload(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise KubernetesChangeContractError("patch payload must be an RFC 6902 operation array")
    result: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise KubernetesChangeContractError("RFC 6902 operation must be an object")
        operation = item.get("op")
        expected = {"op", "path"} if operation == "remove" else {"op", "path", "value"}
        if operation not in _PATCH_OPERATIONS or set(item) != expected:
            raise KubernetesChangeContractError("RFC 6902 operation is unsupported")
        path = _json_pointer(item.get("path"))
        if any(path == prefix or path.startswith(f"{prefix}/") for prefix in _IMMUTABLE_PATCH_PATHS):
            raise KubernetesChangeContractError("RFC 6902 operation changes identity, status, or server-owned fields")
        normalized: dict[str, object] = {"op": operation, "path": path}
        if operation != "remove":
            normalized["value"] = _json_value(item.get("value"), "patch value")
        result.append(normalized)
    return result


def _delete_payload(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {"propagation_policy"}:
        raise KubernetesChangeContractError("delete payload must contain an explicit propagation policy")
    policy = raw.get("propagation_policy")
    if policy not in {"Foreground", "Background", "Orphan"}:
        raise KubernetesChangeContractError("delete propagation policy is invalid")
    return {"propagation_policy": policy}


def _post_checks(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        raise KubernetesChangeContractError("at least one structured Kubernetes post-check is required")
    checks = [_post_check(item) for item in raw]
    if not any(item["type"] in _KUBERNETES_POST_CHECKS for item in checks):
        raise KubernetesChangeContractError("at least one structured Kubernetes post-check is required")
    return checks


def _post_check(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
        raise KubernetesChangeContractError("post-check must be a structured object")
    kind = raw["type"]
    if kind in {"exists", "absent", "observed_generation", "workload_rollout", "crd_established"}:
        if set(raw) != {"type"}:
            raise KubernetesChangeContractError(f"{kind} post-check fields are invalid")
        return {"type": kind}
    if kind == "json_pointer":
        if set(raw) != {"type", "path", "operator", "value"} or raw.get("operator") not in {
            "eq", "ne", "gt", "gte", "lt", "lte",
        }:
            raise KubernetesChangeContractError("json_pointer post-check fields are invalid")
        return {
            "type": kind,
            "path": _json_pointer(raw.get("path")),
            "operator": raw["operator"],
            "value": _json_value(raw.get("value"), "post-check value"),
        }
    if kind == "condition":
        if set(raw) != {"type", "condition_type", "status"} or raw.get("status") not in {
            "True", "False", "Unknown",
        }:
            raise KubernetesChangeContractError("condition post-check fields are invalid")
        return {
            "type": kind,
            "condition_type": _text(raw.get("condition_type"), "condition type", 200),
            "status": raw["status"],
        }
    if kind == "job_terminal":
        if set(raw) != {"type", "outcome"} or raw.get("outcome") not in {"complete", "failed"}:
            raise KubernetesChangeContractError("job_terminal post-check fields are invalid")
        return {"type": kind, "outcome": raw["outcome"]}
    if kind in {"prometheus", "loki"}:
        required = {"type", "query", "start", "end", "operator", "value"}
        allowed = required | ({"limit"} if kind == "loki" else set())
        fields = set(raw)
        if (fields != required and fields != allowed) or raw.get("operator") not in {
            "eq", "ne", "gt", "gte", "lt", "lte",
        }:
            raise KubernetesChangeContractError(f"{kind} post-check fields are invalid")
        result = {
            "type": kind,
            "query": _text(raw.get("query"), f"{kind} query", 4000),
            "start": _text(raw.get("start"), f"{kind} start", 100),
            "end": _text(raw.get("end"), f"{kind} end", 100),
            "operator": raw["operator"],
            "value": _json_value(raw.get("value"), "post-check value"),
        }
        if kind == "loki" and "limit" in raw:
            limit = raw["limit"]
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
                raise KubernetesChangeContractError("loki post-check limit is invalid")
            result["limit"] = limit
        return result
    raise KubernetesChangeContractError("post-check type is unsupported")


def _json_pointer(value: object) -> str:
    path = _text(value, "JSON Pointer", 2000)
    if not path.startswith("/") or any(
        char == "~" and (index + 1 == len(path) or path[index + 1] not in "01")
        for index, char in enumerate(path)
    ):
        raise KubernetesChangeContractError("JSON Pointer is invalid")
    return path


def _json_value(value: object, field: str) -> object:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise KubernetesChangeContractError(f"{field} must be JSON") from exc
    if len(encoded.encode()) > 512 * 1024:
        raise KubernetesChangeContractError(f"{field} exceeds 512 KiB")
    return copy.deepcopy(value)


def _contains_sensitive_payload(target: dict[str, object], operation: object, payload: object) -> bool:
    if target["kind"] == "Secret" and operation == "create":
        if not isinstance(payload, dict):
            return True
        sensitive_maps = [payload.get(field) for field in ("data", "stringData") if field in payload]
        return any(
            not isinstance(values, dict)
            or any(not _secure_placeholder_only(value) for value in values.values())
            for values in sensitive_maps
        )
    if target["kind"] == "Secret" and operation == "patch" and isinstance(payload, list):
        if any(
            str(item.get("path") or "").startswith(("/data", "/stringData"))
            and item.get("op") != "remove"
            and not _secure_placeholder_only(item.get("value"))
            for item in payload
        ):
            return True
    return _contains_sensitive_value(payload)


def _contains_sensitive_value(value: object, key: str = "") -> bool:
    if key and _SENSITIVE_NAME.fullmatch(key):
        return _nonempty(value) and not _secure_placeholder_only(value)
    if isinstance(value, str):
        if _secure_placeholder_only(value):
            return False
        return _SENSITIVE_TEXT.search(value) is not None
    if isinstance(value, dict):
        name = value.get("name")
        if (
            isinstance(name, str) and _SENSITIVE_NAME.fullmatch(name)
            and _nonempty(value.get("value")) and not _secure_placeholder_only(value.get("value"))
        ):
            return True
        return any(_contains_sensitive_value(item, str(item_key)) for item_key, item in value.items())
    if isinstance(value, list):
        return any(_contains_sensitive_value(item) for item in value)
    return False


def _nonempty(value: object) -> bool:
    return bool(value) if isinstance(value, (dict, list)) else value not in {None, ""}


def _secure_placeholder_only(value: object) -> bool:
    return (
        isinstance(value, str)
        and contains_secure_input_placeholder(value)
        and len(secure_input_ids(value)) == 1
    )


def _rollback(value: object) -> dict[str, str]:
    if value == {"status": "available"}:
        return {"status": "available"}
    if not isinstance(value, dict) or set(value) != {"status", "concrete_loss"} or value.get("status") != "unavailable":
        raise KubernetesChangeContractError("rollback declaration is invalid")
    loss = value.get("concrete_loss")
    if not isinstance(loss, str) or not loss.strip() or len(loss.strip()) > 2000:
        raise KubernetesChangeContractError("irreversible change concrete loss is invalid")
    return {"status": "unavailable", "concrete_loss": loss.strip()}


def _json_key(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: object, field: str, limit: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > limit:
        raise KubernetesChangeContractError(f"{field} is invalid")
    return normalized
