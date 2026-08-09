"""HTTP adapter for Connector Enrollment and registered Cluster state."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from aiops.domain.identity import AuthSession, IdentityError

from .connector_commands import ConnectorCommands
from .connector_enrollments import ConnectorEnrollments


class ConnectorEnrollmentHTTPAdapter:
    def __init__(
        self,
        enrollments: ConnectorEnrollments,
        commands: ConnectorCommands,
        authorize_admin: Any,
        require_fresh_auth: Any,
        request_session: Any,
        audit_denial: Any,
        audit_record: Any,
        request_id_for: Any,
        extract_bearer: Any,
        error_payload: Any,
    ) -> None:
        self._enrollments = enrollments
        self._commands = commands
        self._authorize_admin = authorize_admin
        self._require_fresh_auth = require_fresh_auth
        self._request_session = request_session
        self._audit_denial = audit_denial
        self._audit_record = audit_record
        self._request_id_for = request_id_for
        self._extract_bearer = extract_bearer
        self._error_payload = error_payload

    def dispatch(self, handler: Any, route_path: str) -> bool:
        method = getattr(handler, "command", "GET")
        if method == "GET" and route_path == "/api/v1/connectors/status":
            self._status(handler)
            return True
        admin_route = _admin_route(route_path)
        if admin_route is not None:
            collection, target_id = admin_route
            if method == "GET" and target_id is None:
                self._admin_get(handler)
                return True
            if method == "POST" and target_id is None:
                self._admin_mutation(handler, collection, None)
                return True
            if method == "PATCH" and target_id is not None:
                self._admin_mutation(handler, collection, target_id)
                return True
        if method == "POST" and route_path in {
            "/api/v1/connectors/register",
            "/api/v1/connectors/heartbeat",
        }:
            self._connector_request(handler, route_path.rsplit("/", 1)[-1])
            return True
        return False

    def _status(self, handler: Any) -> None:
        request_id = self._request_id_for(handler)
        session, _ = self._request_session(handler)
        if session is None:
            handler.write_json(
                HTTPStatus.UNAUTHORIZED,
                self._error_payload("unauthorized", "authentication required", request_id),
            )
            return
        handler.write_json(
            HTTPStatus.OK,
            {"request_id": request_id, "connectors": self._enrollments.public_status()},
        )

    def _admin_get(self, handler: Any) -> None:
        request_id = self._request_id_for(handler)
        if self._authorize_admin(handler, request_id) is None:
            return
        state = self._enrollments.admin_state()
        state["clusters"] = self._commands.summarize_clusters(state["clusters"])
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, **state})

    def _admin_mutation(
        self,
        handler: Any,
        collection: str,
        target_id: str | None,
    ) -> None:
        request_id = self._request_id_for(handler)
        action = f"{collection}_{'update' if target_id else 'create'}"
        audit_target = (collection, target_id, action)
        session = self._authorize_admin(handler, request_id, audit_target=audit_target)
        if session is None:
            return
        try:
            payload = handler.read_json_body()
        except (TypeError, ValueError) as exc:
            self._audit_denial(
                session.actor.actor_id, audit_target, "invalid_request", request_id,
            )
            handler.write_json(
                HTTPStatus.BAD_REQUEST,
                self._error_payload("invalid_request", str(exc), request_id),
            )
            return
        raw_reason = payload.pop("reason", "")
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        if not reason:
            self._audit_denial(
                session.actor.actor_id, audit_target, "reason_required", request_id, "missing",
            )
            handler.write_json(
                HTTPStatus.BAD_REQUEST,
                self._error_payload("reason_required", "reason is required", request_id),
            )
            return
        if not self._require_fresh_auth(
            handler,
            session,
            request_id,
            audit_target=audit_target,
            reason=reason,
        ):
            return
        self._apply_admin_mutation(
            handler, collection, target_id, payload, session, reason, request_id,
        )

    def _apply_admin_mutation(
        self,
        handler: Any,
        collection: str,
        target_id: str | None,
        payload: dict[str, Any],
        session: AuthSession,
        reason: str,
        request_id: str,
    ) -> None:
        try:
            if collection == "connector-enrollments" and target_id is None:
                if (
                    set(payload) != {"connector_id", "cluster_id", "expected_revision"}
                    or not all(
                        isinstance(payload[field], str)
                        for field in ("connector_id", "cluster_id")
                    )
                    or payload["expected_revision"] is not None
                ):
                    raise IdentityError(
                        "invalid_enrollment",
                        "connector_id, cluster_id and null expected_revision are required",
                    )
                enrollment, credential = self._enrollments.create(
                    connector_id=payload["connector_id"],
                    cluster_id=payload["cluster_id"],
                    actor_id=session.actor.actor_id,
                    reason=reason,
                    request_id=request_id,
                )
                handler.write_json(
                    HTTPStatus.CREATED,
                    {
                        "request_id": request_id,
                        "connector_enrollment": enrollment,
                        "credential": credential,
                    },
                )
                return
            if collection == "connector-enrollments" and target_id is not None:
                if (
                    not payload
                    or set(payload)
                    - {"active", "rotate_credential", "retry_read_verification"}
                    or any(not isinstance(value, bool) for value in payload.values())
                ):
                    raise IdentityError(
                        "invalid_enrollment", "Enrollment update fields must be booleans",
                    )
                enrollment, credential = self._enrollments.update(
                    target_id,
                    active=payload.get("active"),
                    rotate_credential=bool(payload.get("rotate_credential")),
                    retry_read_verification=bool(payload.get("retry_read_verification")),
                    commands=self._commands,
                    actor_id=session.actor.actor_id,
                    reason=reason,
                    request_id=request_id,
                )
                response: dict[str, Any] = {
                    "request_id": request_id,
                    "connector_enrollment": enrollment,
                }
                if credential:
                    response["credential"] = credential
                handler.write_json(HTTPStatus.OK, response)
                return
            if collection == "clusters" and target_id is not None:
                allowed = {
                    "display_name", "environment", "governance_notes", "mutation_enabled",
                }
                if (
                    not payload
                    or set(payload) - allowed
                    or any(
                        not isinstance(
                            value, bool if key == "mutation_enabled" else str,
                        )
                        for key, value in payload.items()
                    )
                ):
                    raise IdentityError(
                        "invalid_cluster", "invalid Cluster administration fields",
                    )
                cluster = self._enrollments.update_cluster(
                    target_id,
                    payload=payload,
                    actor_id=session.actor.actor_id,
                    reason=reason,
                    request_id=request_id,
                )
                handler.write_json(
                    HTTPStatus.OK, {"request_id": request_id, "cluster": cluster},
                )
                return
            raise IdentityError("not_found", "administration resource not found")
        except IdentityError as exc:
            attempted_target = (
                target_id
                or ":".join(
                    str(payload.get(field) or "").strip()
                    for field in ("connector_id", "cluster_id")
                ).strip(":")
                or None
            )
            self._audit_record(
                actor_id=session.actor.actor_id,
                target_type=collection,
                target_id=attempted_target,
                action=f"{collection}_{'update' if target_id else 'create'}",
                reason=reason,
                before=None,
                after=None,
                result=exc.code,
                request_id=request_id,
            )
            status = (
                HTTPStatus.NOT_FOUND
                if exc.code == "not_found"
                else HTTPStatus.CONFLICT
                if exc.code in {"enrollment_exists", "rotation_pending", "rotation_blocked"}
                else HTTPStatus.BAD_REQUEST
            )
            handler.write_json(
                status, self._error_payload(exc.code, exc.message, request_id),
            )

    def _connector_request(self, handler: Any, action: str) -> None:
        request_id = self._request_id_for(handler)
        credential = self._extract_bearer(handler.headers.get("Authorization")) or ""
        connector_id = ""
        cluster_id = ""
        try:
            if not credential:
                raise IdentityError(
                    "invalid_connector_credential",
                    "Connector credential is invalid or revoked",
                )
            payload = handler.read_json_body()
            allowed = (
                {"connector_id", "cluster_id", "namespace_scope", "capabilities"}
                if action == "register"
                else {"connector_id", "cluster_id", "status", "failure_summary"}
            )
            required = (
                {"connector_id", "cluster_id"}
                if action == "register"
                else {"connector_id", "cluster_id", "status"}
            )
            if set(payload) - allowed or not required <= set(payload):
                raise IdentityError("invalid_request", "invalid Connector request fields")
            connector_id = payload["connector_id"]
            cluster_id = payload["cluster_id"]
            if (
                not isinstance(connector_id, str)
                or not isinstance(cluster_id, str)
                or not connector_id.strip()
                or not cluster_id.strip()
            ):
                raise IdentityError(
                    "invalid_request", "connector_id and cluster_id are required",
                )
            connector_id = connector_id.strip()
            cluster_id = cluster_id.strip()
            if action == "register":
                for field in ("namespace_scope", "capabilities"):
                    value = payload.get(field, [])
                    if not isinstance(value, list) or any(
                        not isinstance(item, str) for item in value
                    ):
                        raise IdentityError(
                            "invalid_request", f"{field} must be an array of strings",
                        )
                cluster, created = self._enrollments.register(
                    credential,
                    connector_id,
                    cluster_id,
                    namespace_scope=payload.get("namespace_scope", []),
                    capabilities=payload.get("capabilities", []),
                    commands=self._commands,
                    request_id=request_id,
                )
                handler.write_json(
                    HTTPStatus.CREATED if created else HTTPStatus.OK,
                    {"request_id": request_id, "status": "registered", "cluster": cluster},
                )
                return
            if not isinstance(payload["status"], str) or not isinstance(
                payload.get("failure_summary", ""), str,
            ):
                raise IdentityError(
                    "invalid_request", "status and failure_summary must be strings",
                )
            cluster = self._enrollments.heartbeat(
                credential,
                connector_id,
                cluster_id,
                status=payload["status"],
                failure_summary=payload.get("failure_summary", ""),
                request_id=request_id,
            )
            handler.write_json(
                HTTPStatus.OK,
                {"request_id": request_id, "status": "accepted", "cluster": cluster},
            )
        except IdentityError as exc:
            self._audit_record(
                actor_id=None,
                target_type="connectors",
                target_id=connector_id or None,
                action=f"connector_{action}",
                reason="Connector authentication or payload validation",
                before=None,
                after={"cluster_id": cluster_id} if cluster_id else None,
                result=exc.code,
                request_id=request_id,
            )
            status = {
                "invalid_connector_credential": HTTPStatus.UNAUTHORIZED,
                "identity_mismatch": HTTPStatus.FORBIDDEN,
                "not_registered": HTTPStatus.CONFLICT,
            }.get(exc.code, HTTPStatus.BAD_REQUEST)
            handler.write_json(
                status, self._error_payload(exc.code, exc.message, request_id),
            )
        except (TypeError, ValueError) as exc:
            handler.write_json(
                HTTPStatus.BAD_REQUEST,
                self._error_payload("invalid_request", str(exc), request_id),
            )


def _admin_route(path: str) -> tuple[str, str | None] | None:
    prefix = "/api/v1/admin/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix) :].strip("/").split("/")
    if (
        not parts
        or len(parts) > 2
        or parts[0] not in {"connector-enrollments", "clusters"}
    ):
        return None
    return parts[0], unquote(parts[1]) if len(parts) == 2 else None
