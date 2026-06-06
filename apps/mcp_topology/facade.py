"""Topology MCP facade boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiops.contracts import ErrorCode, EvidenceRef, ToolEnvelope, ToolError
from toolsets.topology_store import get_service_topology as _get_service_topology


TOOL_NAME = "get_service_topology"


def tool_name() -> str:
    """Return the V1 Topology MCP tool name."""
    return TOOL_NAME


def empty_response(request_id: str, correlation_id: str | None = None) -> ToolEnvelope:
    """Build a placeholder response envelope for contract-level tests."""
    return ToolEnvelope(
        request_id=request_id,
        correlation_id=correlation_id,
        tool_name=TOOL_NAME,
        status="partial",
        summary="Topology MCP facade is not implemented yet",
    )


def get_service_topology(args: dict[str, Any], *, db_path: Path | None = None) -> ToolEnvelope:
    """Return service topology as a V1 MCP envelope."""
    request_id = str(args.get("request_id") or "get_service_topology")
    correlation_id = args.get("correlation_id")
    cluster_id = str(args.get("cluster_id") or "").strip()
    namespace = str(args.get("namespace") or "").strip()
    service = str(args.get("service") or "").strip()
    missing = [
        name
        for name, value in (
            ("cluster_id", cluster_id),
            ("namespace", namespace),
            ("service", service),
        )
        if not value
    ]
    if missing:
        return ToolEnvelope(
            request_id=request_id,
            correlation_id=correlation_id,
            tool_name=TOOL_NAME,
            status="failed",
            summary="get_service_topology 请求缺少必填字段",
            audit={
                "status": "failed",
                "tool_name": TOOL_NAME,
                "cluster_id": cluster_id or None,
                "namespace": namespace or None,
                "service": service or None,
                "error_code": ErrorCode.INVALID_REQUEST.value,
            },
            errors=(
                ToolError(
                    code=ErrorCode.INVALID_REQUEST,
                    message=f"缺少必填字段: {', '.join(missing)}",
                    details={"missing": missing},
                ),
            ),
        )

    topology = _get_service_topology(cluster_id, namespace, service, db_path=db_path)
    service_info = topology.get("service", {})
    found = bool(service_info.get("found"))
    status = "succeeded" if found else "partial"
    warnings = tuple(topology.get("warnings") or ())
    identity = f"{service_info.get('cluster_id')}/{service_info.get('namespace')}/{service_info.get('service')}"
    evidence = EvidenceRef(
        ref_id=f"ev_topology_{identity.replace('/', '_')}",
        source="topology",
        cluster_id=str(service_info.get("cluster_id") or cluster_id),
        namespace=str(service_info.get("namespace") or namespace),
        service=str(service_info.get("service") or service),
    )
    errors: tuple[ToolError, ...] = ()
    if not found:
        errors = (
            ToolError(
                code=ErrorCode.SERVICE_NOT_FOUND,
                message="service topology not found",
                details={"warnings": list(warnings)},
            ),
        )

    return ToolEnvelope(
        request_id=request_id,
        correlation_id=correlation_id,
        tool_name=TOOL_NAME,
        status=status,
        summary=f"Topology get_service_topology {'found' if found else 'did not find'} {identity}",
        data=topology,
        evidence_refs=(evidence,),
        audit={
            "status": status,
            "tool_name": TOOL_NAME,
            "cluster_id": evidence.cluster_id,
            "namespace": evidence.namespace,
            "service": evidence.service,
            "warnings": list(warnings),
            "error_code": None if found else ErrorCode.SERVICE_NOT_FOUND.value,
        },
        errors=errors,
    )
