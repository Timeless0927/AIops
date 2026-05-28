"""Post-remediation health checks."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

try:
    from . import incident_store, message_delivery
    from .k8s_read import _run_kubectl
    from .kube_context import resolve_kube_context
except ImportError:  # pragma: no cover - script-style import compatibility
    import incident_store  # type: ignore
    import message_delivery  # type: ignore
    from k8s_read import _run_kubectl  # type: ignore
    from kube_context import resolve_kube_context  # type: ignore


_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DEPLOYMENT_ACTIONS = {"scale_deployment", "restart_deployment"}
_RETRYABLE_REASON_CODES = {
    "deployment_unavailable",
    "events_read_failed",
    "pods_not_ready",
    "pods_read_failed",
    "replica_mismatch",
    "rollout_generation_stale",
    "rollout_incomplete",
    "warning_events_present",
}


async def check_action_health(
    action: dict[str, Any],
    *,
    timeout_seconds: int = 180,
    interval_seconds: int = 10,
) -> dict[str, Any]:
    """Check deterministic health for a supported remediation action."""

    validation_error = _validate_deployment_action(action)
    if validation_error is not None:
        return _result(
            ok=False,
            status="unsupported",
            reason_code=validation_error,
            summary="不支持该 action 的健康检查",
            checks=[],
            rollback_required=False,
        )

    deadline = time.monotonic() + max(timeout_seconds, 0)
    last_result: dict[str, Any] | None = None

    while True:
        current = await _check_deployment_once(action)
        if current["ok"]:
            return current

        last_result = current
        reason_code = str(current.get("reason_code") or "")
        timed_out = timeout_seconds > 0 and time.monotonic() >= deadline
        immediate = timeout_seconds <= 0 or reason_code not in _RETRYABLE_REASON_CODES
        if immediate:
            return current
        if timed_out:
            return _result(
                ok=False,
                status="rollback_required",
                reason_code="health_check_timeout",
                summary="健康检查超时，仍未观察到稳定健康状态",
                checks=current.get("checks", []),
                rollback_required=True,
                extra={"last_result": last_result},
            )

        await asyncio.sleep(max(interval_seconds, 0.01))


async def check_and_record_action_health(
    action: dict[str, Any],
    *,
    incident_id: str,
    approval_id: str | None = None,
    timeout_seconds: int = 180,
    interval_seconds: int = 10,
    notify: bool = True,
) -> dict[str, Any]:
    """Run health check and mark rollback_required on failure."""

    result = await check_action_health(
        action,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )
    if not result["ok"] and result.get("rollback_required"):
        result["rollback_required_record"] = await record_rollback_required(
            incident_id=incident_id,
            action=action,
            health_result=result,
            approval_id=approval_id,
            notify=notify,
        )
    return result


async def record_rollback_required(
    *,
    incident_id: str,
    action: dict[str, Any],
    health_result: dict[str, Any],
    approval_id: str | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """Connect a failed health check to incident timeline/status and notification queue."""

    try:
        reason_code = str(health_result.get("reason_code") or "health_check_failed")
        summary = str(health_result.get("summary") or "健康检查失败，需要人工判断 rollback")
        event_id = await incident_store.mark_rollback_required(
            incident_id,
            reason_code=reason_code,
            summary=summary,
            metadata={
                "action": _action_ref(action),
                "health_result": _compact_health_result(health_result),
                "approval_id": approval_id,
            },
        )
        incident = await incident_store.get_incident(incident_id)
    except Exception as exc:  # pragma: no cover - defensive boundary
        return {
            "ok": False,
            "reason_code": "rollback_required_record_failed",
            "error": str(exc),
        }

    delivery: dict[str, Any] | None = None
    if notify:
        if incident.get("platform") and incident.get("chat_id"):
            delivery = await message_delivery.queue_rollback_required_notification(
                incident_id=incident_id,
                platform=str(incident["platform"]),
                chat_id=str(incident["chat_id"]),
                thread_id=incident.get("thread_id"),
                approval_id=approval_id,
                action=action,
                health_result=health_result,
            )
        else:
            delivery = {
                "ok": False,
                "reason_code": "incident_notification_binding_missing",
            }

    return {
        "ok": True,
        "incident_id": incident_id,
        "status": "rollback_required",
        "event_id": event_id,
        "delivery": delivery,
    }


async def _check_deployment_once(action: dict[str, Any]) -> dict[str, Any]:
    namespace = str(action["namespace"])
    resource_name = str(action["resource_name"])
    context = _kube_context(action)
    checks: list[dict[str, Any]] = []

    deployment_result = await _read_json(
        f"kubectl get deployment/{resource_name} -n {namespace} -o json",
        context,
        "deployment_read_failed",
    )
    if not deployment_result["ok"]:
        return _result(
            ok=False,
            status="rollback_required",
            reason_code=str(deployment_result["reason_code"]),
            summary=str(deployment_result["summary"]),
            checks=checks,
            rollback_required=True,
        )

    deployment = deployment_result["data"]
    replica_result = _check_deployment_replicas(action, deployment)
    checks.extend(replica_result["checks"])
    if not replica_result["ok"]:
        return _result(
            ok=False,
            status="rollback_required",
            reason_code=str(replica_result["reason_code"]),
            summary=str(replica_result["summary"]),
            checks=checks,
            rollback_required=True,
        )

    selector = _selector_from_deployment(deployment)
    if not selector:
        return _result(
            ok=False,
            status="rollback_required",
            reason_code="selector_missing",
            summary="deployment 缺少 matchLabels selector",
            checks=checks,
            rollback_required=True,
        )

    expected_replicas = int(replica_result["expected_replicas"])
    pod_names: set[str] = set()
    if expected_replicas > 0:
        pods_result = await _read_json(
            f"kubectl get pods -n {namespace} -l {selector} -o json",
            context,
            "pods_read_failed",
        )
        if not pods_result["ok"]:
            return _result(
                ok=False,
                status="rollback_required",
                reason_code=str(pods_result["reason_code"]),
                summary=str(pods_result["summary"]),
                checks=checks,
                rollback_required=True,
            )

        pods = pods_result["data"].get("items", [])
        pod_names = {str(pod.get("metadata", {}).get("name")) for pod in pods if pod.get("metadata")}
        ready_count = sum(1 for pod in pods if _pod_ready(pod))
        pods_ok = ready_count >= expected_replicas
        checks.append(
            {
                "name": "pods_ready",
                "ok": pods_ok,
                "summary": f"{ready_count}/{expected_replicas} selected pods Ready",
            }
        )
        if not pods_ok:
            return _result(
                ok=False,
                status="rollback_required",
                reason_code="pods_not_ready",
                summary="selected pods 未全部 Ready",
                checks=checks,
                rollback_required=True,
            )
    else:
        checks.append(
            {
                "name": "pods_ready",
                "ok": True,
                "summary": "target replicas is 0, pod readiness skipped",
            }
        )

    events_result = await _read_json(
        f"kubectl get events -n {namespace} --field-selector type=Warning -o json",
        context,
        "events_read_failed",
    )
    if not events_result["ok"]:
        return _result(
            ok=False,
            status="rollback_required",
            reason_code=str(events_result["reason_code"]),
            summary=str(events_result["summary"]),
            checks=checks,
            rollback_required=True,
        )

    warning_events = _matching_warning_events(events_result["data"], resource_name, pod_names)
    warnings_ok = len(warning_events) == 0
    checks.append(
        {
            "name": "no_new_warning_events",
            "ok": warnings_ok,
            "summary": "no matching warning events" if warnings_ok else f"{len(warning_events)} warning events",
        }
    )
    if not warnings_ok:
        return _result(
            ok=False,
            status="rollback_required",
            reason_code="warning_events_present",
            summary="发现与修复对象相关的 Warning event",
            checks=checks,
            rollback_required=True,
            extra={"warning_events": warning_events[:5]},
        )

    return _result(
        ok=True,
        status="healthy",
        reason_code=None,
        summary="deployment rollout and replicas healthy",
        checks=checks,
        rollback_required=False,
    )


def _check_deployment_replicas(action: dict[str, Any], deployment: dict[str, Any]) -> dict[str, Any]:
    status = deployment.get("status", {})
    spec = deployment.get("spec", {})
    action_type = str(action.get("action_type") or "")
    expected_replicas = _expected_replicas(action, spec)
    spec_replicas = int(spec.get("replicas") or 0)
    available = int(status.get("availableReplicas") or 0)
    updated = int(status.get("updatedReplicas") or 0)
    unavailable = int(status.get("unavailableReplicas") or 0)
    generation = int(deployment.get("metadata", {}).get("generation") or 0)
    observed_generation = int(status.get("observedGeneration") or 0)

    checks = [
        {
            "name": "deployment_available",
            "ok": available >= expected_replicas,
            "summary": f"{available}/{expected_replicas} available",
        },
        {
            "name": "deployment_updated",
            "ok": updated >= expected_replicas,
            "summary": f"{updated}/{expected_replicas} updated",
        },
        {
            "name": "deployment_generation_observed",
            "ok": generation == 0 or observed_generation >= generation,
            "summary": f"observedGeneration={observed_generation}, generation={generation}",
        },
    ]

    if action_type == "scale_deployment" and spec_replicas != expected_replicas:
        return {
            "ok": False,
            "reason_code": "replica_mismatch",
            "summary": f"desired replicas {spec_replicas} != expected {expected_replicas}",
            "checks": checks,
            "expected_replicas": expected_replicas,
        }
    if unavailable > 0 or available < expected_replicas:
        return {
            "ok": False,
            "reason_code": "deployment_unavailable",
            "summary": f"{available}/{expected_replicas} replicas available",
            "checks": checks,
            "expected_replicas": expected_replicas,
        }
    if updated < expected_replicas:
        return {
            "ok": False,
            "reason_code": "rollout_incomplete",
            "summary": f"{updated}/{expected_replicas} replicas updated",
            "checks": checks,
            "expected_replicas": expected_replicas,
        }
    if generation and observed_generation < generation:
        return {
            "ok": False,
            "reason_code": "rollout_generation_stale",
            "summary": "deployment generation 尚未被 controller 观察",
            "checks": checks,
            "expected_replicas": expected_replicas,
        }
    return {"ok": True, "checks": checks, "expected_replicas": expected_replicas}


async def _read_json(command: str, context: str | None, failure_reason: str) -> dict[str, Any]:
    execution = await _run_kubectl(command, context)
    if not execution.get("ok"):
        return {
            "ok": False,
            "reason_code": failure_reason,
            "summary": str(execution.get("stderr") or execution.get("stdout") or "kubectl read failed"),
        }
    try:
        return {"ok": True, "data": json.loads(str(execution.get("stdout") or "{}"))}
    except json.JSONDecodeError:
        return {
            "ok": False,
            "reason_code": f"{failure_reason}_json_invalid",
            "summary": "kubectl 输出不是合法 JSON",
        }


def _validate_deployment_action(action: dict[str, Any]) -> str | None:
    if str(action.get("action_type") or "") not in _DEPLOYMENT_ACTIONS:
        return "unsupported_action_health_check"
    if str(action.get("resource_kind") or "") != "deployment":
        return "unsupported_resource_kind"
    namespace = str(action.get("namespace") or "")
    resource_name = str(action.get("resource_name") or "")
    if not _valid_dns_label(namespace) or not _valid_dns_label(resource_name):
        return "invalid_action_identity"
    if action.get("action_type") == "scale_deployment":
        replicas = action.get("parameters", {}).get("replicas")
        if not isinstance(replicas, int) or replicas < 0:
            return "invalid_replicas"
    return None


def _expected_replicas(action: dict[str, Any], deployment_spec: dict[str, Any]) -> int:
    if action.get("action_type") == "scale_deployment":
        return int(action.get("parameters", {}).get("replicas"))
    return int(deployment_spec.get("replicas") or 1)


def _selector_from_deployment(deployment: dict[str, Any]) -> str:
    labels = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
    if not isinstance(labels, dict) or not labels:
        return ""
    return ",".join(f"{key}={labels[key]}" for key in sorted(labels))


def _pod_ready(pod: dict[str, Any]) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )


def _matching_warning_events(
    events: dict[str, Any],
    deployment_name: str,
    pod_names: set[str],
) -> list[dict[str, str]]:
    matched: list[dict[str, str]] = []
    for event in events.get("items", []):
        involved = event.get("involvedObject") or event.get("regarding") or {}
        kind = str(involved.get("kind") or "")
        name = str(involved.get("name") or "")
        related = (
            (kind == "Deployment" and name == deployment_name)
            or (kind == "Pod" and name in pod_names)
            or (kind == "ReplicaSet" and name.startswith(f"{deployment_name}-"))
        )
        if not related:
            continue
        matched.append(
            {
                "kind": kind,
                "name": name,
                "reason": str(event.get("reason") or ""),
                "message": str(event.get("message") or ""),
            }
        )
    return matched


def _result(
    *,
    ok: bool,
    status: str,
    reason_code: str | None,
    summary: str,
    checks: list[dict[str, Any]],
    rollback_required: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "ok": ok,
        "status": status,
        "reason_code": reason_code,
        "summary": summary,
        "checks": checks,
        "rollback_required": rollback_required,
        "observed_at": time.time(),
    }
    if extra:
        result.update(extra)
    return result


def _compact_health_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "status": result.get("status"),
        "reason_code": result.get("reason_code"),
        "summary": result.get("summary"),
        "checks": result.get("checks", []),
        "observed_at": result.get("observed_at"),
    }


def _action_ref(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_type": action.get("action_type"),
        "cluster": action.get("cluster"),
        "namespace": action.get("namespace"),
        "resource_kind": action.get("resource_kind"),
        "resource_name": action.get("resource_name"),
        "parameters": action.get("parameters", {}),
    }


def _kube_context(action: dict[str, Any]) -> str | None:
    return resolve_kube_context(action.get("cluster"), explicit_context=action.get("kube_context"))


def _valid_dns_label(value: str) -> bool:
    return bool(value and len(value) <= 63 and _DNS_LABEL_RE.match(value))
