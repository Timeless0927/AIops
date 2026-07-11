"""HTTP adapter for the Gateway Resource Catalog."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus
from typing import Any, Callable

from aiops.domain.identity import AuthSession, IdentityError
from apps.service_http import JsonHandler

from .resource_catalog import DiscoveryObservation, ResourceCatalog, ResourceCatalogError
from .v1_store import GatewayV1Store


def dispatch(
    handler: JsonHandler,
    path: str,
    sessions: GatewayV1Store,
    catalog: ResourceCatalog,
    authorize_admin: Callable[..., AuthSession | None],
    require_fresh_auth: Callable[..., bool],
    request_id_for: Callable[[JsonHandler], str],
    extract_bearer_token: Callable[[str | None], str | None],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    prefix = "/api/v1/admin/"
    parts = path[len(prefix) :].strip("/").split("/") if path.startswith(prefix) else []
    collection = parts[0] if parts and parts[0] in {"resource-catalog", "services", "resource-bindings"} else None
    target_id = parts[1] if len(parts) == 2 else None
    if collection and len(parts) <= 2 and handler.command == "GET" and target_id is None:
        request_id = request_id_for(handler)
        if authorize_admin(handler, request_id) is not None:
            handler.write_json(
                HTTPStatus.OK,
                {"request_id": request_id, **catalog.list_state()},
            )
        return True
    if collection and len(parts) <= 2 and (
        (handler.command == "POST" and target_id is None and collection in {"services", "resource-bindings"})
        or (handler.command == "PATCH" and target_id is not None and collection == "resource-bindings")
    ):
        _dispatch_admin_write(
            handler,
            collection=collection,
            target_id=target_id,
            sessions=sessions,
            catalog=catalog,
            authorize_admin=authorize_admin,
            require_fresh_auth=require_fresh_auth,
            request_id=request_id_for(handler),
            error_payload=error_payload,
        )
        return True
    if handler.command == "POST" and path == "/api/v1/connectors/discovery-candidates":
        _dispatch_connector_write(
            handler,
            sessions=sessions,
            catalog=catalog,
            request_id=request_id_for(handler),
            credential=extract_bearer_token(handler.headers.get("Authorization")) or "",
            error_payload=error_payload,
        )
        return True
    return False


def _dispatch_admin_write(
    handler: JsonHandler,
    *,
    collection: str,
    target_id: str | None,
    sessions: GatewayV1Store,
    catalog: ResourceCatalog,
    authorize_admin: Callable[..., AuthSession | None],
    require_fresh_auth: Callable[..., bool],
    request_id: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> None:
    action = f"{collection}_{'update' if target_id else 'create'}"
    audit_target = (collection, target_id, action)
    session = authorize_admin(handler, request_id, audit_target=audit_target)
    if session is None:
        return
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        _record_denial(sessions, session, audit_target, "invalid_request", request_id, "unavailable_before_validation")
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id))
        return
    raw_reason = payload.pop("reason", "")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    if not reason:
        _record_denial(sessions, session, audit_target, "reason_required", request_id, "missing")
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("reason_required", "reason is required", request_id))
        return
    if not require_fresh_auth(
        handler,
        session,
        request_id,
        audit_target=audit_target,
        reason=reason,
    ):
        return
    handle_admin_mutation(
        handler,
        collection=collection,
        target_id=target_id,
        payload=payload,
        session=session,
        reason=reason,
        request_id=request_id,
        sessions=sessions,
        catalog=catalog,
        error_payload=error_payload,
    )


def _dispatch_connector_write(
    handler: JsonHandler,
    *,
    sessions: GatewayV1Store,
    catalog: ResourceCatalog,
    request_id: str,
    credential: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> None:
    connector_id = ""
    cluster_id = ""
    try:
        if not credential:
            raise IdentityError("invalid_connector_credential", "Connector credential is invalid or revoked")
        payload = handler.read_json_body()
        if set(payload) != {"connector_id", "cluster_id", "candidates"}:
            raise IdentityError("invalid_request", "invalid Connector request fields")
        connector_id = payload["connector_id"]
        cluster_id = payload["cluster_id"]
        if not isinstance(connector_id, str) or not isinstance(cluster_id, str):
            raise IdentityError("invalid_request", "connector_id and cluster_id are required")
        connector_id = connector_id.strip()
        cluster_id = cluster_id.strip()
        if not connector_id or not cluster_id:
            raise IdentityError("invalid_request", "connector_id and cluster_id are required")
        handle_connector_discovery(
            handler,
            payload=payload,
            credential=credential,
            connector_id=connector_id,
            cluster_id=cluster_id,
            request_id=request_id,
            sessions=sessions,
            catalog=catalog,
        )
    except (IdentityError, ResourceCatalogError) as exc:
        sessions.record_admin_audit(
            actor_id=None,
            target_type="connectors",
            target_id=connector_id or None,
            action="connector_discovery-candidates",
            reason="Connector authentication or payload validation",
            before=getattr(exc, "before", None),
            after={"cluster_id": cluster_id} if cluster_id else None,
            result=exc.code,
            request_id=request_id,
        )
        status = {
            "invalid_connector_credential": HTTPStatus.UNAUTHORIZED,
            "identity_mismatch": HTTPStatus.FORBIDDEN,
            "not_registered": HTTPStatus.CONFLICT,
        }.get(exc.code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, error_payload(exc.code, exc.message, request_id))
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id))


def _record_denial(
    sessions: GatewayV1Store,
    session: AuthSession,
    audit_target: tuple[str, str | None, str],
    result: str,
    request_id: str,
    reason: str,
) -> None:
    target_type, target_id, action = audit_target
    sessions.record_admin_audit(
        actor_id=session.actor.actor_id,
        target_type=target_type,
        target_id=target_id,
        action=action,
        reason=reason,
        before=None,
        after=None,
        result=result,
        request_id=request_id,
    )


def handle_admin_mutation(
    handler: JsonHandler,
    *,
    collection: str,
    target_id: str | None,
    payload: dict[str, Any],
    session: AuthSession,
    reason: str,
    request_id: str,
    sessions: GatewayV1Store,
    catalog: ResourceCatalog,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> None:
    action = f"{collection}_{'update' if target_id else 'create'}"
    try:
        if collection == "services" and target_id is None:
            if set(payload) - {"team_id", "name", "description"} or not {"team_id", "name"} <= set(payload):
                raise ResourceCatalogError("invalid_request", "team_id and name are required")
            if any(not isinstance(value, str) for value in payload.values()):
                raise ResourceCatalogError("invalid_request", "Service fields must be strings")
            service = catalog.create_service(
                team_id=payload["team_id"],
                name=payload["name"],
                description=payload.get("description", ""),
                actor_id=session.actor.actor_id,
                reason=reason,
                request_id=request_id,
            )
            handler.write_json(HTTPStatus.CREATED, {"request_id": request_id, "service": service})
            return
        if collection == "resource-bindings" and target_id is None:
            if set(payload) != {"candidate_id", "service_id"} or any(not isinstance(value, str) for value in payload.values()):
                raise ResourceCatalogError("invalid_request", "candidate_id and service_id are required")
            binding = catalog.confirm_binding(
                candidate_id=payload["candidate_id"],
                service_id=payload["service_id"],
                actor_id=session.actor.actor_id,
                reason=reason,
                request_id=request_id,
            )
            handler.write_json(HTTPStatus.CREATED, {"request_id": request_id, "resource_binding": binding})
            return
        if collection == "resource-bindings" and target_id is not None:
            if set(payload) != {"service_id"} or not isinstance(payload["service_id"], str):
                raise ResourceCatalogError("invalid_request", "service_id is required")
            binding = catalog.correct_binding(
                binding_id=target_id,
                service_id=payload["service_id"],
                actor_id=session.actor.actor_id,
                reason=reason,
                request_id=request_id,
            )
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "resource_binding": binding})
            return
        raise ResourceCatalogError("not_found", "Resource Catalog administration resource not found")
    except ResourceCatalogError as exc:
        sessions.record_admin_audit(
            actor_id=session.actor.actor_id,
            target_type=collection,
            target_id=target_id or str(payload.get("candidate_id") or payload.get("name") or "") or None,
            action=action,
            reason=reason,
            before=exc.before,
            after=None,
            result=exc.code,
            request_id=request_id,
        )
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "candidate_not_found": HTTPStatus.NOT_FOUND,
            "service_not_found": HTTPStatus.NOT_FOUND,
            "team_not_found": HTTPStatus.NOT_FOUND,
            "binding_not_found": HTTPStatus.NOT_FOUND,
            "service_exists": HTTPStatus.CONFLICT,
            "binding_exists": HTTPStatus.CONFLICT,
        }.get(exc.code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, error_payload(exc.code, exc.message, request_id))
    except sqlite3.Error:
        handler.write_json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            error_payload("resource_catalog_write_failed", "Resource Catalog change was not committed", request_id),
        )


def handle_connector_discovery(
    handler: JsonHandler,
    *,
    payload: dict[str, Any],
    credential: str,
    connector_id: str,
    cluster_id: str,
    request_id: str,
    sessions: GatewayV1Store,
    catalog: ResourceCatalog,
) -> None:
    raw_candidates = payload["candidates"]
    if not isinstance(raw_candidates, list) or len(raw_candidates) > 1000:
        raise IdentityError("invalid_request", "candidates must be an array with at most 1000 items")
    candidate_fields = {
        "namespace",
        "workload_kind",
        "workload_name",
        "service_name",
        "service_hint",
        "team_hint",
    }
    required_fields = {"namespace", "workload_kind", "workload_name"}
    if any(
        not isinstance(candidate, dict)
        or set(candidate) - candidate_fields
        or not required_fields <= set(candidate)
        or any(value is not None and not isinstance(value, str) for value in candidate.values())
        for candidate in raw_candidates
    ):
        raise IdentityError("invalid_request", "invalid Discovery Candidate fields")
    sessions.authenticate_connector(credential, connector_id, cluster_id)
    candidates = catalog.refresh_discovery(
        cluster_id,
        [DiscoveryObservation(**candidate) for candidate in raw_candidates],
    )
    handler.write_json(
        HTTPStatus.OK,
        {"request_id": request_id, "discovery_candidates": candidates},
    )
