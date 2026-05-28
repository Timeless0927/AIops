"""Server-side dry-run and safe execution adapter for remediation actions."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict

try:
    from . import audit_log, incident_store, k8s_write, message_delivery, operation_lock, remediation_health
    from .kube_context import resolve_kube_context
except ImportError:  # pragma: no cover - allow direct script imports in tests/tools
    import audit_log  # type: ignore
    import incident_store  # type: ignore
    import k8s_write  # type: ignore
    import message_delivery  # type: ignore
    import operation_lock  # type: ignore
    import remediation_health  # type: ignore
    from kube_context import resolve_kube_context  # type: ignore


ACTION_SCHEMA_VERSION = "remediation.action.v1"
DEFAULT_MAX_REPLICAS = 20
LOCK_TTL_SECONDS = 300

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_SUPPORTED_ACTIONS = {"scale_deployment", "restart_deployment"}
_COORDINATOR_TIMELINE_EVENT_MAP = {
    "approval_execution_queued": ("remediate_progress", "queued"),
    "approval_execution_dry_run_failed": ("remediate_progress", "dry_run_failed"),
    "approval_execution_failed": ("remediate_progress", "failed"),
    "approval_execution_succeeded": ("remediate_executed", "succeeded"),
    "approval_execution_rollback_required": ("rollback_required", "rollback_required"),
}


def _failure(reason_code: str, summary: str, **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": False,
        "reason_code": reason_code,
        "summary": summary,
    }
    result.update(extra)
    return result


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _valid_dns_label(value: Any) -> bool:
    text = _safe_text(value)
    return bool(text) and len(text) <= 63 and bool(_DNS_LABEL_RE.match(text))


def _expected_signature(action: Dict[str, Any]) -> str:
    action_type = action["action_type"]
    cluster = action["cluster"]
    namespace = action["namespace"]
    resource_name = action["resource_name"]
    if action_type == "scale_deployment":
        replicas = action["parameters"]["replicas"]
        return f"scale_deployment:{cluster}:{namespace}:deployment/{resource_name}:replicas={replicas}"
    return f"restart_deployment:{cluster}:{namespace}:deployment/{resource_name}"


def _summary_from_execution(execution: Dict[str, Any], fallback: str) -> str:
    output = _safe_text(execution.get("stderr") or execution.get("stdout"))
    if not output:
        return fallback
    return output.splitlines()[0][:500]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def validate_remediation_action(
    action: Dict[str, Any] | None,
    *,
    max_replicas: int = DEFAULT_MAX_REPLICAS,
) -> Dict[str, Any]:
    """Validate an allowlisted structured remediation action."""
    if not isinstance(action, dict):
        return _failure("invalid_action", "action must be an object")

    required = [
        "action_schema_version",
        "action_signature",
        "action_type",
        "cluster",
        "namespace",
        "resource_kind",
        "resource_name",
        "parameters",
    ]
    missing = [field for field in required if field not in action]
    if missing:
        return _failure("missing_required_field", f"missing required field: {missing[0]}", field=missing[0])

    if action.get("action_schema_version") != ACTION_SCHEMA_VERSION:
        return _failure("unsupported_schema_version", "unsupported action schema version")

    action_type = _safe_text(action.get("action_type"))
    if action_type not in _SUPPORTED_ACTIONS:
        return _failure("unsupported_action", "action_type is not allowlisted")

    if action.get("resource_kind") != "deployment":
        return _failure("unsupported_resource_kind", "only deployment actions are allowlisted")

    cluster = _safe_text(action.get("cluster"))
    if not cluster or any(ch in cluster for ch in "\r\n\0"):
        return _failure("invalid_cluster", "cluster must be a non-empty business cluster label")

    kube_context = _optional_kube_context(action)
    if kube_context and any(ch in kube_context for ch in "\r\n\0"):
        return _failure("invalid_kube_context", "kube_context must not contain control characters")

    if not _valid_dns_label(action.get("namespace")):
        return _failure("invalid_namespace", "namespace must be a DNS label")

    if not _valid_dns_label(action.get("resource_name")):
        return _failure("invalid_resource_name", "resource_name must be a DNS label")

    parameters = action.get("parameters")
    if not isinstance(parameters, dict):
        return _failure("invalid_parameters", "parameters must be an object")

    risk = action.get("risk") or {}
    if risk.get("risk_level") != "low":
        return _failure("unsupported_risk_level", "only low risk actions are auto-executable")
    if risk.get("operation_type") != "k8s_write":
        return _failure("unsupported_operation_type", "only k8s_write actions are auto-executable")

    if action_type == "scale_deployment":
        if set(parameters.keys()) != {"replicas"}:
            return _failure("invalid_parameters", "scale_deployment only accepts replicas")
        replicas = parameters.get("replicas")
        if not isinstance(replicas, int) or isinstance(replicas, bool):
            return _failure("invalid_replicas", "replicas must be an integer")
        if replicas < 0 or replicas > max_replicas:
            return _failure("invalid_replicas", "replicas outside configured range")
    elif action_type == "restart_deployment":
        if parameters != {"strategy": "rollout_restart"}:
            return _failure("invalid_strategy", "restart_deployment requires rollout_restart strategy")

    normalized = {
        **action,
        "action_type": action_type,
        "cluster": cluster,
        "kube_context": kube_context,
        "namespace": _safe_text(action.get("namespace")),
        "resource_name": _safe_text(action.get("resource_name")),
    }
    expected = _expected_signature(normalized)
    if action.get("action_signature") != expected:
        return _failure("signature_mismatch", "action_signature does not match structured fields")

    return {"ok": True, "action": normalized}


def build_kubectl_command(action: Dict[str, Any], *, dry_run: bool = False) -> str:
    """Build a stable kubectl command from an already validated action."""
    action_type = action["action_type"]
    deployment = f"deployment/{action['resource_name']}"
    namespace = action["namespace"]
    if action_type == "scale_deployment":
        replicas = action["parameters"]["replicas"]
        command = f"kubectl scale {deployment} --replicas={replicas} -n {namespace}"
        if dry_run:
            return f"{command} --dry-run=server"
    elif action_type == "restart_deployment":
        if dry_run:
            return f"kubectl patch {deployment} -n {namespace} --type=strategic -p '{{}}' --dry-run=server"
        command = f"kubectl rollout restart {deployment} -n {namespace}"
    else:
        raise ValueError(f"unsupported action_type: {action_type}")

    return command


def resource_lock_key(action: Dict[str, Any]) -> str:
    """Return the resource-level lock key for a validated action."""
    return (
        f"k8s:{action['cluster']}:{action['namespace']}:"
        f"{action['resource_kind']}/{action['resource_name']}"
    )


async def dry_run_action(
    action: Dict[str, Any],
    *,
    max_replicas: int = DEFAULT_MAX_REPLICAS,
) -> Dict[str, Any]:
    """Run the server-side dry-run for an allowlisted remediation action."""
    validation = validate_remediation_action(action, max_replicas=max_replicas)
    if not validation["ok"]:
        return {
            **validation,
            "mode": "server",
            "reason_code": "dry_run_unsupported"
            if validation["reason_code"] == "unsupported_action"
            else validation["reason_code"],
        }

    validated = validation["action"]
    command = build_kubectl_command(validated, dry_run=True)
    kube_context = kube_context_for_action(validated)
    execution = await k8s_write.execute_approved(
        command,
        validated["cluster"],
        kube_context=kube_context,
    )
    if not execution.get("ok"):
        return {
            "ok": False,
            "mode": "server",
            "action_type": validated["action_type"],
            "action_signature": validated["action_signature"],
            "command_preview": command,
            "kube_context": kube_context,
            "reason_code": "dry_run_failed",
            "summary": _summary_from_execution(execution, "server dry-run failed"),
            "stderr": execution.get("stderr", ""),
            "exit_code": execution.get("exit_code"),
        }

    return {
        "ok": True,
        "mode": "server",
        "action_type": validated["action_type"],
        "action_signature": validated["action_signature"],
        "command_preview": command,
        "kube_context": kube_context,
        "summary": _summary_from_execution(execution, "server dry-run accepted"),
        "warnings": [],
        "raw_result_ref": None,
        "stdout": execution.get("stdout", ""),
        "stderr": execution.get("stderr", ""),
        "exit_code": execution.get("exit_code"),
    }


async def execute_action(
    action: Dict[str, Any],
    *,
    max_replicas: int = DEFAULT_MAX_REPLICAS,
) -> Dict[str, Any]:
    """Execute an allowlisted remediation action through k8s_write."""
    validation = validate_remediation_action(action, max_replicas=max_replicas)
    if not validation["ok"]:
        return validation

    validated = validation["action"]
    command = build_kubectl_command(validated, dry_run=False)
    kube_context = kube_context_for_action(validated)
    execution = await k8s_write.execute_approved(
        command,
        validated["cluster"],
        kube_context=kube_context,
    )
    return {
        "ok": bool(execution.get("ok")),
        "action_type": validated["action_type"],
        "action_signature": validated["action_signature"],
        "command_preview": command,
        "kube_context": kube_context,
        "summary": _summary_from_execution(
            execution,
            "action executed" if execution.get("ok") else "action execution failed",
        ),
        "stdout": execution.get("stdout", ""),
        "stderr": execution.get("stderr", ""),
        "exit_code": execution.get("exit_code"),
        "result": execution.get("result"),
    }


async def _record_audit(
    *,
    action: Dict[str, Any],
    status: str,
    dry_run_result: Dict[str, Any] | None,
    execution_result: Dict[str, Any] | None,
    requested_by: str,
    approval_by: str | None,
    approval_at: float | None,
) -> int | None:
    source = action.get("source") or {}
    result = {
        "status": status,
        "action_type": action.get("action_type"),
        "action_signature": action.get("action_signature"),
        "execution": execution_result,
    }
    return await audit_log.record_audit(
        who=requested_by,
        what=f"remediation_execution:{action.get('action_type')}:{status}",
        cluster=action.get("cluster"),
        namespace=action.get("namespace"),
        trigger="approval_execution",
        tool_level="k8s_write",
        tool_name="remediation_execution",
        result=_json_dumps(result),
        dry_run=_json_dumps(dry_run_result) if dry_run_result is not None else None,
        approval_by=approval_by,
        approval_at=approval_at,
        rollback=False,
        incident_id=source.get("incident_id"),
    )


async def _record_timeline(
    *,
    action: Dict[str, Any],
    event_type: str,
    status: str,
    metadata: Dict[str, Any],
) -> int | None:
    source = action.get("source") or {}
    incident_id = source.get("incident_id")
    if not incident_id:
        return None
    return await incident_store.add_event(
        str(incident_id),
        event_type,
        "remediation_execution",
        str(action.get("action_signature", "")),
        status,
        metadata,
    )


async def safe_execute_action(
    action: Dict[str, Any],
    *,
    approval_id: str | None = None,
    requested_by: str = "approval_execution",
    approval_by: str | None = None,
    approval_at: float | None = None,
    session_id: str | None = None,
    lock_ttl_seconds: int = LOCK_TTL_SECONDS,
    max_replicas: int = DEFAULT_MAX_REPLICAS,
) -> Dict[str, Any]:
    """Dry-run, lock, execute, audit, and timeline an allowlisted action."""
    validation = validate_remediation_action(action, max_replicas=max_replicas)
    if not validation["ok"]:
        return {
            **validation,
            "status": "rejected",
        }

    validated = validation["action"]
    dry_run_result = await dry_run_action(validated, max_replicas=max_replicas)
    if not dry_run_result.get("ok"):
        audit_id = await _record_audit(
            action=validated,
            status="dry_run_failed",
            dry_run_result=dry_run_result,
            execution_result=None,
            requested_by=requested_by,
            approval_by=approval_by,
            approval_at=approval_at,
        )
        timeline_event_id = await _record_timeline(
            action=validated,
            event_type="remediate_progress",
            status="dry_run_failed",
            metadata={
                "approval_id": approval_id,
                "reason_code": dry_run_result.get("reason_code"),
                "command_preview": dry_run_result.get("command_preview"),
                "audit_id": audit_id,
            },
        )
        return {
            "ok": False,
            "status": "dry_run_failed",
            "reason_code": dry_run_result.get("reason_code", "dry_run_failed"),
            "action_type": validated["action_type"],
            "action_signature": validated["action_signature"],
            "dry_run": dry_run_result,
            "audit_id": audit_id,
            "timeline_event_id": timeline_event_id,
        }

    lock_key = resource_lock_key(validated)
    lock_session_id = session_id or f"remediation-execution:{approval_id or uuid.uuid4()}"
    acquired = await operation_lock.acquire_lock(lock_key, lock_session_id, lock_ttl_seconds)
    if not acquired:
        audit_id = await _record_audit(
            action=validated,
            status="lock_busy",
            dry_run_result=dry_run_result,
            execution_result=None,
            requested_by=requested_by,
            approval_by=approval_by,
            approval_at=approval_at,
        )
        timeline_event_id = await _record_timeline(
            action=validated,
            event_type="remediate_progress",
            status="lock_busy",
            metadata={
                "approval_id": approval_id,
                "resource_key": lock_key,
                "audit_id": audit_id,
            },
        )
        return {
            "ok": False,
            "status": "lock_busy",
            "reason_code": "operation_locked",
            "action_type": validated["action_type"],
            "action_signature": validated["action_signature"],
            "resource_key": lock_key,
            "dry_run": dry_run_result,
            "audit_id": audit_id,
            "timeline_event_id": timeline_event_id,
        }

    execution_result: Dict[str, Any] | None = None
    status = "execution_failed"
    try:
        execution_result = await execute_action(validated, max_replicas=max_replicas)
        status = "succeeded" if execution_result.get("ok") else "execution_failed"
        audit_id = await _record_audit(
            action=validated,
            status=status,
            dry_run_result=dry_run_result,
            execution_result=execution_result,
            requested_by=requested_by,
            approval_by=approval_by,
            approval_at=approval_at,
        )
        timeline_event_id = await _record_timeline(
            action=validated,
            event_type="remediate_executed",
            status=status,
            metadata={
                "approval_id": approval_id,
                "resource_key": lock_key,
                "command_preview": execution_result.get("command_preview"),
                "exit_code": execution_result.get("exit_code"),
                "audit_id": audit_id,
            },
        )
        return {
            "ok": status == "succeeded",
            "status": status,
            "reason_code": None if status == "succeeded" else "execution_failed",
            "action_type": validated["action_type"],
            "action_signature": validated["action_signature"],
            "resource_key": lock_key,
            "dry_run": dry_run_result,
            "execution": execution_result,
            "audit_id": audit_id,
            "timeline_event_id": timeline_event_id,
        }
    finally:
        await operation_lock.release_lock(lock_key, lock_session_id)


class RemediationExecutionAdapter:
    """Approval-execution adapter backed by discrete remediation stages."""

    def __init__(
        self,
        *,
        lock_ttl_seconds: int = LOCK_TTL_SECONDS,
        max_replicas: int = DEFAULT_MAX_REPLICAS,
        session_id_prefix: str = "approval-execution",
        health_timeout_seconds: int = 180,
        health_interval_seconds: int = 10,
    ) -> None:
        self.lock_ttl_seconds = lock_ttl_seconds
        self.max_replicas = max_replicas
        self.session_id_prefix = session_id_prefix
        self.health_timeout_seconds = health_timeout_seconds
        self.health_interval_seconds = health_interval_seconds

    async def dry_run_action(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await dry_run_action(action, max_replicas=self.max_replicas)

    async def acquire_lock(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        validation = validate_remediation_action(action, max_replicas=self.max_replicas)
        if not validation["ok"]:
            return {
                "ok": False,
                "reason_code": validation.get("reason_code") or "invalid_action",
                "error": validation.get("message") or "invalid remediation action",
                "source": "remediation_execution_adapter",
            }

        validated = validation["action"]
        lock_key = resource_lock_key(validated)
        lock_session_id = self._session_id(_approval_id(approval, execution), execution)
        acquired = await operation_lock.acquire_lock(
            lock_key,
            lock_session_id,
            self.lock_ttl_seconds,
        )
        if not acquired:
            return {
                "ok": False,
                "reason_code": "operation_locked",
                "error": "operation lock is busy",
                "lock_key": lock_key,
                "source": "remediation_execution_adapter",
            }
        return {
            "ok": True,
            "lock_key": lock_key,
            "source": "remediation_execution_adapter",
        }

    async def execute_action(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = await execute_action(action, max_replicas=self.max_replicas)
        if not result.get("ok") and "reason_code" not in result:
            return {**result, "reason_code": "execution_failed"}
        return result

    async def record_audit(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        status = "succeeded" if result.get("ok") else "failed"
        dry_run_result = execution.get("dry_run_result")
        audit_id = await _record_audit(
            action=action,
            status=status,
            dry_run_result=dry_run_result if isinstance(dry_run_result, dict) else None,
            execution_result=result,
            requested_by=str(approval.get("requester") or "approval_execution"),
            approval_by=_optional_text(approval.get("approver")),
            approval_at=_optional_float(approval.get("decided_at")),
        )
        return {
            "ok": True,
            "audit_id": audit_id,
            "source": "remediation_execution_adapter",
        }

    async def check_health(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        incident_id = _incident_id(approval, execution, action)
        approval_id = _approval_id(approval, execution)
        if not incident_id:
            return _health_unavailable(
                "incident_id_missing",
                "missing incident_id; health result cannot be recorded",
            )

        try:
            health_result = await remediation_health.check_and_record_action_health(
                action,
                incident_id=incident_id,
                approval_id=approval_id or None,
                timeout_seconds=self.health_timeout_seconds,
                interval_seconds=self.health_interval_seconds,
                notify=True,
            )
        except Exception as exc:  # pragma: no cover - fail-closed adapter boundary
            return _health_unavailable("health_check_exception", str(exc), incident_id=incident_id)

        health_result["stage"] = "health"
        health_result["source"] = "remediation_execution_adapter"
        if health_result.get("ok"):
            return health_result
        if health_result.get("rollback_required"):
            return health_result
        return _health_unavailable(
            str(health_result.get("reason_code") or "health_check_unavailable"),
            str(health_result.get("summary") or "health check unavailable"),
            incident_id=incident_id,
            health_result=health_result,
        )

    async def append_timeline(
        self,
        event_type: str,
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> None:
        mapping = _COORDINATOR_TIMELINE_EVENT_MAP.get(event_type)
        if mapping is None:
            return None
        incident_id = approval.get("incident_id") or execution.get("incident_id")
        if not incident_id:
            return None

        mapped_event_type, status = mapping
        if event_type == "approval_execution_rollback_required":
            recorded = _rollback_recorded(execution)
            if recorded:
                return None
            action = _approval_context(approval).get("remediation_action")
            health_result = execution.get("health_result")
            if isinstance(action, dict) and isinstance(health_result, dict):
                await remediation_health.record_rollback_required(
                    incident_id=str(incident_id),
                    action=action,
                    health_result=health_result,
                    approval_id=_approval_id(approval, execution) or None,
                    notify=True,
                )
                return None

        action_signature = str(
            execution.get("action_signature")
            or _approval_context(approval).get("action_signature")
            or ""
        )
        await incident_store.add_event(
            str(incident_id),
            mapped_event_type,
            "remediation_execution",
            action_signature,
            status,
            {
                "approval_id": approval.get("approval_id"),
                "coordinator_status": status,
                "execution_id": execution.get("id"),
                "execution_status": execution.get("status"),
            },
        )
        return None

    async def notify(
        self,
        event_type: str,
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> None:
        if event_type not in {
            "approval_execution_succeeded",
            "approval_execution_failed",
            "approval_execution_dry_run_failed",
            "approval_execution_rollback_required",
        }:
            return None

        incident_id = str(approval.get("incident_id") or execution.get("incident_id") or "").strip()
        if not incident_id:
            return None

        try:
            incident = await incident_store.get_incident(incident_id)
        except Exception:
            return None
        if not isinstance(incident, dict):
            return None

        platform = str(incident.get("platform") or "feishu").strip() or "feishu"
        chat_id = str(incident.get("chat_id") or "").strip()
        reply_message_id = str(
            incident.get("root_message_id")
            or incident.get("status_card_message_id")
            or ""
        ).strip()
        thread_id = str(
            incident.get("thread_id")
            or reply_message_id
            or ""
        ).strip()
        action = _approval_context(approval).get("remediation_action")
        action = action if isinstance(action, dict) else {}
        queued = _existing_rollback_required_delivery(execution) if event_type == "approval_execution_rollback_required" else None
        if queued is None:
            queued = await message_delivery.queue_approval_execution_notification(
                incident_id=incident_id,
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id or None,
                approval_id=_approval_id(approval, execution) or None,
                event_type=event_type,
                approval=approval,
                execution=execution,
                action=action,
            )
        delivery_id = str(queued.get("delivery_id") or "")
        if not chat_id or not reply_message_id:
            if delivery_id:
                await message_delivery.mark_failed(delivery_id, "incident 飞书 thread 绑定未就绪")
            return None

        publisher = getattr(_feishu_conversation_module(), "publish_approval_execution_notification", None)
        if not callable(publisher):
            if delivery_id:
                await message_delivery.mark_failed(delivery_id, "feishu execution notification publisher unavailable")
            return None

        try:
            response = await publisher(
                incident,
                queued["payload"],
                await _load_runtime_config(),
                payload_hash=str(queued.get("payload_hash") or ""),
            )
        except Exception as exc:
            if delivery_id:
                await message_delivery.mark_failed(delivery_id, str(exc))
            return None

        message_id = str(response.get("message_id") or "").strip() if isinstance(response, dict) else ""
        if not message_id:
            if delivery_id:
                await message_delivery.mark_failed(delivery_id, "飞书执行结果通知未返回 message_id")
            return None
        if delivery_id:
            await message_delivery.mark_sent(delivery_id, message_id)
        return None

    async def release_lock(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> None:
        lock_key = execution.get("lock_key") or _fallback_lock_key(action)
        if not lock_key:
            return None
        await operation_lock.release_lock(
            str(lock_key),
            self._session_id(_approval_id(approval, execution), execution),
        )
        return None

    def _session_id(self, approval_id: str, execution: Dict[str, Any]) -> str:
        execution_id = execution.get("id") or approval_id
        return f"{self.session_id_prefix}:{execution_id}"


def create_approval_execution_adapter(
    *,
    lock_ttl_seconds: int = LOCK_TTL_SECONDS,
    max_replicas: int = DEFAULT_MAX_REPLICAS,
    session_id_prefix: str = "approval-execution",
    health_timeout_seconds: int = 180,
    health_interval_seconds: int = 10,
) -> RemediationExecutionAdapter:
    """Return the real adapter for approval_execution.process_approval_execution."""
    return RemediationExecutionAdapter(
        lock_ttl_seconds=lock_ttl_seconds,
        max_replicas=max_replicas,
        session_id_prefix=session_id_prefix,
        health_timeout_seconds=health_timeout_seconds,
        health_interval_seconds=health_interval_seconds,
    )


def _approval_id(approval: Dict[str, Any], execution: Dict[str, Any]) -> str:
    return str(approval.get("approval_id") or execution.get("approval_id") or "")


def _approval_context(approval: Dict[str, Any]) -> Dict[str, Any]:
    context = approval.get("context")
    return context if isinstance(context, dict) else {}


def _incident_id(
    approval: Dict[str, Any],
    execution: Dict[str, Any],
    action: Dict[str, Any],
) -> str:
    source = action.get("source") if isinstance(action.get("source"), dict) else {}
    return _safe_text(
        approval.get("incident_id")
        or execution.get("incident_id")
        or source.get("incident_id")
    )


def _health_unavailable(
    reason_code: str,
    summary: str,
    *,
    incident_id: str | None = None,
    health_result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": False,
        "status": "needs_manual_verification",
        "reason_code": "health_check_unavailable",
        "summary": summary,
        "stage": "health",
        "source": "remediation_execution_adapter",
        "needs_manual_verification": True,
        "health_check_unavailable": True,
        "health_unavailable_reason": reason_code,
        "rollback_required": False,
    }
    if incident_id:
        result["incident_id"] = incident_id
    if health_result is not None:
        result["health_result"] = health_result
    return result


def _rollback_recorded(execution: Dict[str, Any]) -> bool:
    health_result = execution.get("health_result")
    if not isinstance(health_result, dict):
        return False
    record = health_result.get("rollback_required_record")
    return isinstance(record, dict) and record.get("ok") is True


def _existing_rollback_required_delivery(execution: Dict[str, Any]) -> Dict[str, Any] | None:
    health_result = execution.get("health_result")
    if not isinstance(health_result, dict):
        return None
    record = health_result.get("rollback_required_record")
    if not isinstance(record, dict):
        return None
    delivery = record.get("delivery")
    return delivery if isinstance(delivery, dict) else None


def _feishu_conversation_module() -> Any:
    try:
        from hooks import feishu_conversation  # type: ignore
    except ImportError:
        try:
            import feishu_conversation  # type: ignore
        except ImportError:
            return None
    return feishu_conversation


async def _load_runtime_config() -> Dict[str, Any]:
    try:
        from hooks import alert_webhook  # type: ignore

        return await alert_webhook._load_config()
    except Exception:
        return {}


def _optional_text(value: Any) -> str | None:
    text = _safe_text(value)
    return text or None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_kube_context(action: Dict[str, Any]) -> str | None:
    return _optional_text(action.get("kube_context"))


def kube_context_for_action(action: Dict[str, Any]) -> str | None:
    return resolve_kube_context(action.get("cluster"), explicit_context=_optional_kube_context(action))


def _fallback_lock_key(action: Dict[str, Any]) -> str | None:
    try:
        return resource_lock_key(action)
    except Exception:
        return None
