"""Pure validation and envelope construction for governed Deployment mutations."""

from __future__ import annotations

import hashlib
import hmac
import json

from aiops.k8s import CommandEnvelope


def build_mutation_envelopes(
    command: dict[str, object], *, now: float, validate_frozen: bool = True, validate_expiry: bool = True
) -> tuple[CommandEnvelope, CommandEnvelope, CommandEnvelope]:
    if set(command) - {
        "id", "cluster_id", "namespace", "action", "parameters", "status", "attempt_count",
        "lease_id", "lease_expires_at", "created_at", "result", "execution_grant_id",
        "execution_grant_expires_at", "action_hash", "rollback_plan", "frozen_action",
        "scale_replica_bounds",
    }:
        raise ValueError("unsupported Connector Command fields")
    parameters = command.get("parameters")
    action = command.get("action")
    expected = {
        "restart_deployment": {"resource_kind", "deployment_name"},
        "scale_deployment": {"resource_kind", "deployment_name", "current_replicas", "target_replicas"},
        "rollback_deployment": {"resource_kind", "deployment_name", "target_revision"},
    }
    if action not in expected or not isinstance(parameters, dict) or set(parameters) != expected[action]:
        raise ValueError("unsupported mutation action")
    name = parameters.get("deployment_name")
    grant_id = command.get("execution_grant_id")
    action_hash = command.get("action_hash")
    if parameters.get("resource_kind") != "Deployment" or not isinstance(name, str) or not name.strip():
        raise ValueError("invalid Deployment target")
    if not isinstance(grant_id, str) or not grant_id or not isinstance(action_hash, str) or len(action_hash) != 64:
        raise ValueError("Execution Grant and action hash are required")
    if validate_frozen:
        _validate_frozen_action(command, parameters, action_hash)
    if not isinstance(command.get("execution_grant_expires_at"), (int, float)) or (
        validate_expiry and float(command["execution_grant_expires_at"]) <= now
    ):
        raise ValueError("Execution Grant expired")
    command_id = str(command.get("id") or "")
    cluster_id = str(command.get("cluster_id") or "")
    namespace = str(command.get("namespace") or "")

    def envelope(suffix: str, action_type: str, argv: tuple[str, ...]) -> CommandEnvelope:
        return CommandEnvelope(
            envelope_version="v1", task_id=command_id, command_id=f"{command_id}:{suffix}",
            cluster_id=cluster_id, namespace=namespace, action_type=action_type, argv=argv,
            timeout_seconds=120, output_limit_bytes=1024 * 1024, risk_level="low",
            grant_id=grant_id, reason=action_hash,
        )

    target = f"deployment/{name.strip()}"
    namespace_args = ("--namespace", namespace)
    if action == "scale_deployment":
        current, desired = parameters["current_replicas"], parameters["target_replicas"]
        bounds = command.get("scale_replica_bounds")
        if (
            not isinstance(bounds, list) or len(bounds) != 2
            or any(not isinstance(value, int) or isinstance(value, bool) for value in bounds)
            or not 0 <= bounds[0] <= bounds[1] <= 20
            or any(value < bounds[0] or value > bounds[1] for value in (current, desired))
        ):
            raise ValueError("replica bounds reject the requested scale")
        if current == desired:
            raise ValueError("scale target must differ from current replicas")
        execution = ("kubectl", "scale", target, "--replicas", str(desired), *namespace_args)
        post_check = ("kubectl", "get", target, *namespace_args, "--output", "json")
    elif action == "rollback_deployment":
        revision = parameters["target_revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("explicit positive target revision is required")
        execution = ("kubectl", "rollout", "undo", target, "--to-revision", str(revision), *namespace_args)
        post_check = ("kubectl", "rollout", "status", target, *namespace_args)
    else:
        execution = ("kubectl", "rollout", "restart", target, *namespace_args)
        post_check = ("kubectl", "rollout", "status", target, *namespace_args)
    _validate_rollback_plan(action, parameters, command.get("rollback_plan"))
    return (
        envelope(
            "preflight", "read",
            ("kubectl", "rollout", "history", target, "--revision", str(parameters["target_revision"]), *namespace_args)
            if action == "rollback_deployment" else ("kubectl", "get", target, *namespace_args, "--output", "json"),
        ),
        envelope("execute", "mutation", execution),
        envelope("post-check", "read", post_check),
    )


def deployment_replicas(stdout: object) -> int | None:
    try:
        value = json.loads(stdout) if isinstance(stdout, str) else None
        replicas = value["spec"]["replicas"]
        return replicas if isinstance(replicas, int) and not isinstance(replicas, bool) else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _validate_rollback_plan(action: object, parameters: dict[str, object], plan: object) -> None:
    if plan is None or isinstance(plan, dict) and plan.get("type") == "none":
        return
    if not isinstance(plan, dict) or action != "scale_deployment" or set(plan) != {
        "condition", "action_type", "parameters", "target_assumptions"
    }:
        raise ValueError("unsafe conditional Rollback Plan")
    if (
        plan.get("condition") != "post_check_failed"
        or plan.get("action_type") != "scale_deployment"
        or plan.get("parameters") != {
            "current_replicas": parameters.get("target_replicas"),
            "target_replicas": parameters.get("current_replicas"),
        }
        or plan.get("target_assumptions") != {"replicas": parameters.get("target_replicas")}
    ):
        raise ValueError("unsafe conditional Rollback Plan")


def _validate_frozen_action(command: dict[str, object], parameters: dict[str, object], action_hash: str) -> None:
    frozen = command.get("frozen_action")
    if not isinstance(frozen, dict) or set(frozen) != {
        "action_type", "target", "typed_parameters", "evidence_step_ids", "safeguards", "rollback_plan"
    }:
        raise ValueError("frozen action is required")
    target = frozen.get("target")
    typed = {key: value for key, value in parameters.items() if key not in {"resource_kind", "deployment_name"}}
    if (
        not isinstance(target, dict)
        or frozen.get("action_type") != command.get("action")
        or frozen.get("typed_parameters") != typed
        or frozen.get("rollback_plan") != command.get("rollback_plan")
        or target.get("cluster_id") != command.get("cluster_id")
        or target.get("namespace") != command.get("namespace")
        or target.get("workload_kind") != "Deployment"
        or target.get("workload_name") != parameters.get("deployment_name")
    ):
        raise ValueError("frozen action does not match the Connector Command")
    digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    if not hmac.compare_digest(digest, action_hash):
        raise ValueError("action hash does not match the frozen action")
