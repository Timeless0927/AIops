"""Pure frozen inverse Change construction and trustworthy identity binding."""

from __future__ import annotations

import copy


_UID = "$forward.uid"
_RESOURCE_VERSION = "$forward.resource_version"


class KubernetesInverseChangeError(ValueError):
    pass


def freeze_inverse_change(review_change: object) -> dict[str, object] | None:
    if not isinstance(review_change, dict):
        return None
    change = review_change.get("canonical_change")
    diff = review_change.get("diff")
    if not isinstance(change, dict) or not isinstance(diff, list):
        return None
    target = change.get("target")
    operation = change.get("operation")
    if not isinstance(target, dict) or operation == "delete" or _contains_redacted(diff):
        return None
    inverse_target = {
        "api_version": target.get("api_version"), "kind": target.get("kind"),
        "namespace": target.get("namespace"), "name": target.get("name"),
        "uid": _UID, "resource_version": _RESOURCE_VERSION,
    }
    if operation == "create":
        return {
            "target": inverse_target,
            "operation": "delete",
            "payload": {
                "apiVersion": "v1", "kind": "DeleteOptions",
                "propagationPolicy": "Foreground",
                "preconditions": {"uid": _UID, "resourceVersion": _RESOURCE_VERSION},
            },
            "post_checks": [{"type": "absent"}],
        }
    if operation != "patch" or not diff:
        return None
    tests: list[dict[str, object]] = [
        {"op": "test", "path": "/metadata/uid", "value": _UID},
        {"op": "test", "path": "/metadata/resourceVersion", "value": _RESOURCE_VERSION},
    ]
    mutations: list[dict[str, object]] = []
    post_checks: list[dict[str, object]] = []
    for item in reversed(diff):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return None
        path, before, after = item["path"], item.get("before"), item.get("after")
        if before is None or after is None:
            return None
        tests.append({"op": "test", "path": path, "value": copy.deepcopy(after)})
        mutations.append({"op": "replace", "path": path, "value": copy.deepcopy(before)})
        post_checks.append({
            "type": "json_pointer", "path": path, "operator": "eq",
            "value": copy.deepcopy(before),
        })
    return {
        "target": inverse_target, "operation": "patch",
        "payload": tests + mutations, "post_checks": post_checks,
    }


def bind_inverse_change(
    inverse_change: object, execution: object,
) -> dict[str, object]:
    target_result = execution.get("target") if isinstance(execution, dict) else None
    uid = target_result.get("uid") if isinstance(target_result, dict) else None
    resource_version = target_result.get("resource_version") if isinstance(target_result, dict) else None
    if (
        not isinstance(inverse_change, dict)
        or target_result is None
        or target_result.get("exists") is not True
        or not isinstance(uid, str) or not uid
        or not isinstance(resource_version, str) or not resource_version
    ):
        raise KubernetesInverseChangeError("trustworthy forward target identity is required")
    return _bind(copy.deepcopy(inverse_change), uid, resource_version)  # type: ignore[return-value]


def _bind(value: object, uid: str, resource_version: str) -> object:
    if value == _UID:
        return uid
    if value == _RESOURCE_VERSION:
        return resource_version
    if isinstance(value, dict):
        return {key: _bind(item, uid, resource_version) for key, item in value.items()}
    if isinstance(value, list):
        return [_bind(item, uid, resource_version) for item in value]
    return value


def _contains_redacted(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("redacted") is True:
            return True
        return any(_contains_redacted(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redacted(item) for item in value)
    return False
