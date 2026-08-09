"""HTTP adapter for durable Connector Commands."""

from __future__ import annotations

import math
from http import HTTPStatus
from typing import Any

from apps.internal_auth import enforce_internal_auth
from aiops.domain.identity import IdentityError

from .connector_commands import ConnectorCommandError, ConnectorCommands
from .connector_diagnosis_reads import run_diagnosis_read
from .connector_identity import ConnectorIdentity


def dispatch(
    handler: Any,
    path: str,
    commands: ConnectorCommands,
    identity: ConnectorIdentity,
    authorize_admin: Any,
    request_id_for: Any,
    extract_bearer: Any,
    error_payload: Any,
    verification_result_handler: Any = None,
    execution: Any = None,
) -> bool:
    if path == "/api/v1/internal/diagnosis/k8s-read":
        _diagnosis_read(handler, commands, request_id_for, error_payload)
        return True
    if path == "/api/v1/admin/connector-commands":
        _queue(handler, commands, authorize_admin, request_id_for, error_payload)
        return True
    prefix = "/api/v1/connectors/commands/"
    if path == f"{prefix}poll":
        _connector_action(
            handler, "poll", None, commands, identity, request_id_for, extract_bearer,
            error_payload, verification_result_handler, execution,
        )
        return True
    if path.startswith(prefix):
        parts = path[len(prefix) :].split("/")
        if len(parts) == 2 and parts[0] and parts[1] in {"start", "result"}:
            _connector_action(
                handler, parts[1], parts[0], commands, identity, request_id_for, extract_bearer, error_payload,
                verification_result_handler,
                execution,
            )
            return True
    return False


def _diagnosis_read(
    handler: Any,
    commands: ConnectorCommands,
    request_id_for: Any,
    error_payload: Any,
) -> None:
    request_id = request_id_for(handler)
    actor_id = enforce_internal_auth(
        handler,
        service_name="gateway",
        allowed_service_account="aiops-diagnosis",
    )
    if actor_id is None:
        return
    try:
        if getattr(handler, "command", "POST") != "POST":
            raise ConnectorCommandError("method_not_allowed", "Diagnosis read requires POST")
        payload = handler.read_json_body()
        if set(payload) != {"cluster_id", "namespace", "parameters", "reason"}:
            raise ConnectorCommandError("invalid_request", "invalid Diagnosis read fields")
        result = run_diagnosis_read(
            commands,
            cluster_id=payload["cluster_id"], namespace=payload["namespace"],
            parameters=payload["parameters"],
            actor_id=actor_id,
            reason=payload["reason"],
            request_id=request_id,
        )
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, **result})
    except (ConnectorCommandError, TypeError, ValueError) as exc:
        code = str(getattr(exc, "code", "invalid_request"))
        status = HTTPStatus.CONFLICT if code == "cluster_not_ready" else HTTPStatus.BAD_REQUEST
        handler.write_json(status, error_payload(code, str(exc), request_id))


def _queue(handler: Any, commands: ConnectorCommands, authorize_admin: Any, request_id_for: Any, error_payload: Any) -> None:
    request_id = request_id_for(handler)
    session = authorize_admin(
        handler,
        request_id,
        audit_target=("connector_commands", None, "connector_command_queue"),
    )
    if session is None:
        return
    try:
        payload = handler.read_json_body()
        if set(payload) != {"cluster_id", "namespace", "action", "parameters", "reason"}:
            raise ConnectorCommandError("invalid_request", "invalid Connector Command fields")
        reason = payload["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ConnectorCommandError("reason_required", "reason is required")
        command = commands.queue_read(
            cluster_id=payload["cluster_id"],
            namespace=payload["namespace"],
            action=payload["action"],
            parameters=payload["parameters"],
            actor_id=session.actor.actor_id,
            reason=reason.strip(),
            request_id=request_id,
        )
        handler.write_json(HTTPStatus.CREATED, {"request_id": request_id, "command": command})
    except (ConnectorCommandError, TypeError, ValueError) as exc:
        code = exc.code if isinstance(exc, ConnectorCommandError) else "invalid_request"
        status = HTTPStatus.CONFLICT if code == "cluster_not_ready" else HTTPStatus.NOT_FOUND if code == "cluster_not_found" else HTTPStatus.BAD_REQUEST
        handler.write_json(status, error_payload(code, str(exc), request_id))


def _connector_action(
    handler: Any,
    action: str,
    command_id: str | None,
    commands: ConnectorCommands,
    identity: ConnectorIdentity,
    request_id_for: Any,
    extract_bearer: Any,
    error_payload: Any,
    verification_result_handler: Any,
    execution: Any,
) -> None:
    request_id = request_id_for(handler)
    try:
        payload = handler.read_json_body()
        allowed = (
            {"connector_id", "cluster_id", "wait_seconds"}
            if action == "poll"
            else {"connector_id", "cluster_id", "lease_id"} | (
                {"result"} if action == "result" else set()
            )
        )
        if set(payload) not in (
            allowed,
            allowed | {"journal_evidence"} if action == "result" else allowed,
        ):
            raise ConnectorCommandError("invalid_request", "invalid Connector Command request fields")
        connector_id = payload.get("connector_id")
        cluster_id = payload.get("cluster_id")
        if not isinstance(connector_id, str) or not isinstance(cluster_id, str):
            raise ConnectorCommandError("invalid_request", "connector_id and cluster_id are required")
        credential = extract_bearer(handler.headers.get("Authorization")) or ""
        identity.authenticate(credential, connector_id, cluster_id, require_registered=True)
        if action == "poll":
            wait_seconds = payload["wait_seconds"]
            if (
                isinstance(wait_seconds, bool)
                or not isinstance(wait_seconds, (int, float))
                or not math.isfinite(wait_seconds)
            ):
                raise ConnectorCommandError("invalid_request", "wait_seconds must be a number")
            command = commands.poll(
                connector_id, cluster_id, wait_seconds,
                dispatcher=(
                    lambda owned_connector, owned_cluster: execution.dispatch_next(
                        owned_connector, owned_cluster, request_id=request_id,
                    )
                    if execution is not None else None
                ),
            )
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "command": command})
            return
        lease_id = payload.get("lease_id")
        if not isinstance(lease_id, str) or not lease_id:
            raise ConnectorCommandError("invalid_request", "lease_id is required")
        result = (
            commands.start(
                str(command_id), connector_id, cluster_id, lease_id,
                start_handler=execution.record_started_in if execution is not None else None,
            )
            if action == "start"
            else commands.submit_result(
                str(command_id), connector_id, cluster_id, lease_id, payload.get("result"),
                journal_evidence=payload.get("journal_evidence"),
                request_id=request_id,
                result_handler=verification_result_handler,
            )
        )
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, **result})
    except (ConnectorCommandError, IdentityError, TypeError, ValueError) as exc:
        code = str(getattr(exc, "code", "invalid_request"))
        status = {
            "invalid_connector_credential": HTTPStatus.UNAUTHORIZED,
            "identity_mismatch": HTTPStatus.FORBIDDEN,
            "command_not_found": HTTPStatus.NOT_FOUND,
            "conflicting_result": HTTPStatus.CONFLICT,
            "not_registered": HTTPStatus.CONFLICT,
            "cluster_not_ready": HTTPStatus.CONFLICT,
        }.get(code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, error_payload(code, str(exc), request_id))
