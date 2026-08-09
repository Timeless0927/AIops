"""HTTP adapter for the Gateway Resource Catalog."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from http import HTTPStatus
from typing import Any, Callable

from aiops.domain.identity import AuthSession, IdentityError
from apps.service_http import JsonHandler

from .connector_identity import ConnectorIdentity
from .resource_catalog import DiscoveryObservation, ResourceCatalog, ResourceCatalogError


@dataclass(frozen=True)
class ResourceCatalogHTTPAdapter:
    actor_view: Any
    record_admin_audit: Any
    catalog: ResourceCatalog
    connector_identity: ConnectorIdentity
    request_session: Any
    team_ids_for_actor: Any
    connector_status: Any
    authorize_admin: Any
    require_fresh_auth: Any
    request_id_for: Any
    extract_bearer_token: Any
    error_payload: Any

    def dispatch(self, handler: Any, route_path: str) -> bool:
        path = route_path
        actor_view = self.actor_view
        record_admin_audit = self.record_admin_audit
        catalog = self.catalog
        connector_identity = self.connector_identity
        request_session = self.request_session
        team_ids_for_actor = self.team_ids_for_actor
        connector_status = self.connector_status
        authorize_admin = self.authorize_admin
        require_fresh_auth = self.require_fresh_auth
        request_id_for = self.request_id_for
        extract_bearer_token = self.extract_bearer_token
        error_payload = self.error_payload
        if path == "/api/v1/resources" and handler.command == "GET":
            request_id = request_id_for(handler)
            session, _ = request_session(handler)
            if session is None:
                handler.write_json(
                    HTTPStatus.UNAUTHORIZED,
                    error_payload("unauthorized", "authentication required", request_id),
                )
                return True
            actor = actor_view(session.actor)
            if "view_incident" not in actor["capabilities"]:
                handler.write_json(
                    HTTPStatus.FORBIDDEN,
                    error_payload("forbidden", "access denied", request_id),
                )
                return True
            team_ids = None if actor["is_platform_administrator"] else team_ids_for_actor(
                session.actor.actor_id,
            )
            handler.write_json(HTTPStatus.OK, {
                "request_id": request_id,
                "can_administer": bool(actor["is_platform_administrator"]),
                **catalog.list_for_actor(
                    team_ids=team_ids,
                    connector_status=connector_status(),
                ),
            })
            return True
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
                record_admin_audit=record_admin_audit,
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
                record_admin_audit=record_admin_audit,
                catalog=catalog,
                connector_identity=connector_identity,
                request_id=request_id_for(handler),
                credential=extract_bearer_token(handler.headers.get("Authorization")) or "",
                error_payload=error_payload,
            )
            return True
        return False


def proposal_denial(
    catalog: ResourceCatalog,
    target: dict[str, object],
    request_id: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> tuple[HTTPStatus, dict[str, object]] | None:
    error = catalog.execution_target_error(target)
    if error is None:
        return None
    return HTTPStatus.CONFLICT, error_payload(error.code, error.message, request_id)


def deny_approval(
    handler: JsonHandler,
    catalog: ResourceCatalog,
    action_record: dict[str, Any] | None,
    request_id: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    denial = proposal_denial(catalog, action_record["target"], request_id, error_payload) if action_record else None
    if denial is None:
        return False
    handler.write_json(*denial)
    return True


def _dispatch_admin_write(
    handler: JsonHandler,
    *,
    collection: str,
    target_id: str | None,
    record_admin_audit: Callable[..., None],
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
        _record_denial(record_admin_audit, session, audit_target, "invalid_request", request_id, "unavailable_before_validation")
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id))
        return
    raw_reason = payload.pop("reason", "")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    if not reason:
        _record_denial(record_admin_audit, session, audit_target, "reason_required", request_id, "missing")
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
        record_admin_audit=record_admin_audit,
        catalog=catalog,
        error_payload=error_payload,
    )


def _dispatch_connector_write(
    handler: JsonHandler,
    *,
    record_admin_audit: Callable[..., None],
    catalog: ResourceCatalog,
    connector_identity: ConnectorIdentity,
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
            catalog=catalog,
            connector_identity=connector_identity,
        )
    except (IdentityError, ResourceCatalogError) as exc:
        record_admin_audit(
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
    record_admin_audit: Callable[..., None],
    session: AuthSession,
    audit_target: tuple[str, str | None, str],
    result: str,
    request_id: str,
    reason: str,
) -> None:
    target_type, target_id, action = audit_target
    record_admin_audit(
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
    record_admin_audit: Callable[..., None],
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
        record_admin_audit(
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
    catalog: ResourceCatalog,
    connector_identity: ConnectorIdentity,
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
    connector_identity.authenticate(credential, connector_id, cluster_id, require_registered=True)
    candidates = catalog.refresh_discovery(
        cluster_id,
        [DiscoveryObservation(**candidate) for candidate in raw_candidates],
    )
    handler.write_json(
        HTTPStatus.OK,
        {"request_id": request_id, "discovery_candidates": candidates},
    )
