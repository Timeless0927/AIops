"""Read-only Kubernetes observation for execution reconciliation."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiops.security import (
    DEFAULT_CHANGE_KEY_PATH,
    SecureInputCryptoError,
    materialize_secure_input_placeholders,
)

from .kubernetes_change_adapter import (
    KubernetesAdapterError,
    json_pointer_value,
    observe_change_state,
)


def execute_reconciliation_command(
    command: dict[str, object],
    *,
    connector_cluster_id: str,
    allowed_namespaces: set[str],
    observed_at: float,
    client_factory: Callable[[], Any] | None = None,
    secure_input_key_path: Path | str = DEFAULT_CHANGE_KEY_PATH,
) -> dict[str, object]:
    """Observe expected state and frozen post-checks without mutating Kubernetes."""

    try:
        parameters = command.get("parameters")
        if (
            command.get("action") != "reconcile_kubernetes_change"
            or command.get("cluster_id") != connector_cluster_id
            or not isinstance(parameters, dict)
            or set(parameters) not in ({"change"}, {"change", "secure_inputs"})
        ):
            raise KubernetesAdapterError(
                "invalid_reconciliation_command", "reconciliation command is invalid",
            )
        change = parameters.get("change")
        secure_inputs = parameters.get("secure_inputs", [])
        if not isinstance(change, dict) or not isinstance(secure_inputs, list):
            raise KubernetesAdapterError(
                "invalid_reconciliation_command", "reconciliation payload is invalid",
            )
        target = change.get("target")
        if not isinstance(target, dict) or set(change) != {
            "target", "operation", "payload", "post_checks",
        }:
            raise KubernetesAdapterError(
                "invalid_reconciliation_command", "frozen Change is invalid",
            )
        namespace = target.get("namespace")
        if "*" not in allowed_namespaces and (
            namespace is None or namespace not in allowed_namespaces
        ):
            raise KubernetesAdapterError(
                "namespace_forbidden", "target is outside Connector namespace scope",
            )
        materialized, _plaintext = materialize_secure_input_placeholders(
            copy.deepcopy(change), secure_inputs, key_path=secure_input_key_path,
        )
        assert isinstance(materialized, dict)
        live, target_result, post_checks = observe_change_state(
            materialized, client_factory=client_factory,
        )
        effect_matches = _expected_effect_matches(materialized, live)
        classification = (
            "effect_observed"
            if effect_matches and post_checks
            and all(item["status"] == "succeeded" for item in post_checks)
            else "unknown_outcome"
        )
        evidence: dict[str, object] = {
            "classification": classification,
            "target": target_result,
            "effect_matches": effect_matches,
            "post_checks": post_checks,
            "observed_at": observed_at,
        }
        encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        evidence["evidence_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        return {"status": "succeeded", "observation": evidence}
    except (KubernetesAdapterError, SecureInputCryptoError) as exc:
        return {
            "status": "rejected",
            "error_code": (
                "secure_input_unavailable" if isinstance(exc, SecureInputCryptoError) else exc.code
            ),
            "error_message": str(exc),
        }
    except Exception:
        return {
            "status": "failed",
            "error_code": "kubernetes_api_unavailable",
            "error_message": "Kubernetes reconciliation read failed",
        }


def _expected_effect_matches(
    change: dict[str, object], live: dict[str, object] | None,
) -> bool:
    operation = change.get("operation")
    payload = change.get("payload")
    if operation == "delete":
        return live is None
    if live is None:
        return False
    target = change.get("target")
    metadata = live.get("metadata")
    if (
        operation == "patch"
        and isinstance(target, dict)
        and target.get("uid") is not None
        and (
            not isinstance(metadata, dict)
            or metadata.get("uid") != target.get("uid")
        )
    ):
        return False
    if operation == "create":
        return _is_subset(payload, live)
    if operation != "patch" or not isinstance(payload, list):
        return False
    for item in payload:
        if not isinstance(item, dict) or item.get("op") == "test":
            continue
        path = item.get("path")
        if not isinstance(path, str):
            return False
        present, actual = json_pointer_value(live, path)
        if item.get("op") == "remove":
            if present:
                return False
        elif item.get("op") in {"add", "replace"}:
            if not present or actual != item.get("value"):
                return False
        else:
            return False
    return True


def _is_subset(expected: object, actual: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _is_subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            _is_subset(left, right) for left, right in zip(expected, actual, strict=True)
        )
    return expected == actual
