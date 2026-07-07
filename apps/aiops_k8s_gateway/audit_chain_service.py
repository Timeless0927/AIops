"""Gateway-owned responsibility-chain audit projection."""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from typing import Any

from aiops.domain.identity import Actor, PERMISSION_QUERY_AUDIT, Scope, resource_scope
from toolsets import audit_log

from . import action_control_service
from . import agent_run_service
from . import approval_execution_service
from . import approval_service
from . import notification_center


JSON = dict[str, Any]


class AuditChainError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


async def list_chains(actor: Actor, *, limit: int = 100) -> list[JSON]:
    approvals = approval_service.list_requests(limit=max(1, min(limit, 500)))
    chains = [_chain_summary(approval) for approval in approvals if _can_read(actor, approval)]
    return [chain for chain in chains if chain is not None]


async def get_chain(actor: Actor, chain_id: str) -> JSON:
    approval_id = chain_id.removeprefix("chain-")
    approval = approval_service.get_request(approval_id)
    if approval is None or not _can_read(actor, approval):
        raise AuditChainError("not_found", "audit chain not found", status=HTTPStatus.NOT_FOUND)
    action = action_control_service.get_by_proposal_id(str(approval["action_proposal_id"]))
    execution = approval_execution_service.get_execution(str(approval["approval_id"]))
    notifications = _notifications_for(approval)
    tombstones = await _tombstones_for(actor, approval)
    raw_refs = await _raw_refs_for(approval)
    return {
        **_chain_summary(approval, action=action, execution=execution),
        "agent_request": _agent_request(action, approval),
        "evidence_refs": approval.get("evidence_refs") or (action or {}).get("evidence_refs") or [],
        "risk_classification": {
            "risk_level": approval.get("risk_level") or (action or {}).get("risk_level"),
            "policy_decision": (action or {}).get("policy_decision"),
            "policy_reason": (action or {}).get("policy_reason"),
            "policy_hit_id": (action or {}).get("policy_hit_id"),
        },
        "frozen_action": {
            "action": (action or {}).get("action"),
            "action_hash": (action or {}).get("action_hash"),
            "target": (action or {}).get("target") or approval.get("resource_scope") or {},
        },
        "approver_snapshot": {
            "requested_by": approval.get("requested_by"),
            "assigned_approvers": approval.get("assigned_approvers") or [],
            "approved_by": approval.get("approved_by"),
            "rejected_by": approval.get("rejected_by"),
            "decided_at": approval.get("decided_at"),
        },
        "approval_remark": approval.get("decision_reason"),
        "execution": _execution_summary(execution),
        "notifications": notifications,
        "delete_tombstones": tombstones,
        "raw_audit_refs": raw_refs,
    }


async def raw_logs(actor: Actor, *, cluster: str | None = None, namespace: str | None = None, limit: int = 100) -> list[JSON]:
    scope = resource_scope(cluster=cluster, namespace=namespace)
    if not actor.can(PERMISSION_QUERY_AUDIT, scope):
        return []
    rows = await audit_log.query_audit(cluster=cluster, namespace=namespace, limit=max(1, min(limit, 500)))
    return [row for row in rows if _row_visible(actor, row)]


async def tombstones(actor: Actor, *, limit: int = 100) -> list[JSON]:
    rows = await _tombstones_for(actor, None)
    return rows[: max(1, min(limit, 500))]


def _chain_summary(approval: JSON, *, action: JSON | None = None, execution: JSON | None = None) -> JSON | None:
    action = action if action is not None else action_control_service.get_by_proposal_id(str(approval["action_proposal_id"]))
    execution = execution if execution is not None else approval_execution_service.get_execution(str(approval["approval_id"]))
    scope = approval.get("resource_scope") if isinstance(approval.get("resource_scope"), dict) else {}
    return {
        "chain_id": f"chain-{approval['approval_id']}",
        "time": approval.get("created_at") or approval.get("requested_at"),
        "incident_id": approval.get("incident_id"),
        "conversation_id": None,
        "run_id": (action or {}).get("run_id"),
        "agent": approval.get("requested_by") or (action or {}).get("requested_by"),
        "requested_action": approval.get("action_summary") or (action or {}).get("action_type"),
        "risk": approval.get("risk_level") or (action or {}).get("risk_level"),
        "target_resource": (action or {}).get("target") or scope,
        "approver": approval.get("approved_by") or approval.get("rejected_by"),
        "approval_decision": approval.get("status"),
        "gateway_execution_result": (execution or {}).get("status"),
        "responsibility_status": _responsibility_status(approval, execution),
        "action_hash": (action or {}).get("action_hash"),
        "approval_id": approval.get("approval_id"),
        "action_proposal_id": approval.get("action_proposal_id"),
        "scope": scope,
    }


def _can_read(actor: Actor, approval: JSON) -> bool:
    return actor.can(PERMISSION_QUERY_AUDIT, _approval_scope(approval))


def _approval_scope(approval: JSON) -> Scope:
    raw = approval.get("resource_scope") if isinstance(approval.get("resource_scope"), dict) else {}
    return resource_scope(
        cluster=str(raw.get("cluster_id") or raw.get("cluster") or "").strip() or None,
        service=str(raw.get("service_id") or raw.get("service") or "").strip() or None,
        team=str(raw.get("team_id") or raw.get("team") or "").strip() or None,
        namespace=str(raw.get("namespace") or "").strip() or None,
    )


def _row_visible(actor: Actor, row: JSON) -> bool:
    scope = _json_obj(row.get("resource_scope"))
    return actor.can(
        PERMISSION_QUERY_AUDIT,
        resource_scope(
            cluster=scope.get("cluster_id") or scope.get("cluster") or row.get("cluster"),
            service=scope.get("service_id") or scope.get("service"),
            team=scope.get("team_id") or scope.get("team"),
            namespace=scope.get("namespace") or row.get("namespace"),
        ),
    )


def _responsibility_status(approval: JSON, execution: JSON | None) -> str:
    if execution and execution.get("status") == "succeeded":
        return "closed"
    if execution and execution.get("status") == "rollback_required":
        return "rollback_required"
    if approval.get("status") in {"rejected", "expired", "cancelled"}:
        return str(approval.get("status"))
    if approval.get("status") == "approved":
        return "execution_pending"
    return "approval_pending"


def _agent_request(action: JSON | None, approval: JSON) -> JSON:
    return {
        "requested_by": approval.get("requested_by") or (action or {}).get("requested_by"),
        "reason": (action or {}).get("action", {}).get("reason") if isinstance((action or {}).get("action"), dict) else None,
        "summary": approval.get("action_summary"),
        "run_id": (action or {}).get("run_id"),
        "session_id": approval.get("session_id"),
    }


def _execution_summary(execution: JSON | None) -> JSON:
    if not execution:
        return {"status": "not_started", "preflight": None, "mutation": None, "post_check": None}
    return {
        "execution_id": execution.get("execution_id"),
        "status": execution.get("status"),
        "preflight": execution.get("preflight_result"),
        "mutation": execution.get("execution_result"),
        "post_check": execution.get("post_check_result"),
        "error_code": execution.get("error_code"),
        "error_message": execution.get("error_message"),
    }


def _notifications_for(approval: JSON) -> list[JSON]:
    rows = notification_center.list_deliveries(limit=500)
    approval_id = str(approval.get("approval_id") or "")
    incident_id = str(approval.get("incident_id") or "")
    return [
        _notification_ref(row)
        for row in rows
        if str(row.get("approval_id") or "") == approval_id or str(row.get("incident_id") or "") == incident_id
    ]


def _notification_ref(row: JSON) -> JSON:
    return {
        "id": row.get("id"),
        "notification_type": row.get("notification_type"),
        "delivery_status": row.get("delivery_status"),
        "platform": row.get("platform"),
        "target_message_id": row.get("target_message_id"),
        "created_at": row.get("created_at"),
    }


async def _raw_refs_for(approval: JSON) -> list[JSON]:
    rows = await audit_log.query_audit(limit=500)
    approval_id = str(approval.get("approval_id") or "")
    action_proposal_id = str(approval.get("action_proposal_id") or "")
    incident_id = str(approval.get("incident_id") or "")
    refs = [
        row
        for row in rows
        if str(row.get("approval_id") or "") == approval_id
        or str(row.get("action_proposal_id") or "") == action_proposal_id
        or str(row.get("incident_id") or "") == incident_id
    ]
    return [
        {
            "id": row.get("id"),
            "what": row.get("what"),
            "result": row.get("result"),
            "request_id": row.get("request_id"),
            "when_ts": row.get("when_ts"),
        }
        for row in refs
    ]


async def _tombstones_for(actor: Actor, approval: JSON | None) -> list[JSON]:
    rows = await agent_run_service.deleted_conversation_tombstones()
    approvals = approval_service.list_requests(limit=500)
    result: list[JSON] = []
    for row in rows:
        scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
        if not actor.can(
            PERMISSION_QUERY_AUDIT,
            resource_scope(
                cluster=scope.get("cluster"),
                service=scope.get("service"),
                team=scope.get("team"),
                namespace=scope.get("namespace"),
            ),
        ):
            continue
        linked = [
            item
            for item in approvals
            if item.get("incident_id") == row.get("incident_id")
            or item.get("session_id") == row.get("run_id")
            or item.get("action_proposal_id") == row.get("run_id")
        ]
        linked_execution_ids = [
            execution["execution_id"]
            for item in linked
            if (execution := approval_execution_service.get_execution(str(item["approval_id"]))) is not None
        ]
        enriched = {
            **row,
            "linked_incident_ids": [row["incident_id"]] if row.get("incident_id") else [],
            "linked_approval_ids": [item["approval_id"] for item in linked],
            "linked_execution_ids": linked_execution_ids,
        }
        if approval is None or approval.get("approval_id") in enriched["linked_approval_ids"] or approval.get("incident_id") == row.get("incident_id"):
            result.append(enriched)
    return result


def _json_obj(raw: Any) -> JSON:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}
