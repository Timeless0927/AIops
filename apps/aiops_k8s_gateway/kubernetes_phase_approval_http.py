"""HTTP adapter for generic Kubernetes Authority and Phase Approval."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import unquote

from .change_requests import ChangeRequestError, ChangeRequests
from .kubernetes_change_authorities import (
    KubernetesChangeAuthorities,
    KubernetesChangeAuthorityError,
)
from .kubernetes_phase_approvals import KubernetesPhaseApprovalError, KubernetesPhaseApprovals


@dataclass(frozen=True)
class KubernetesPhaseApprovalHTTPAdapter:
    is_fresh: Any
    record_admin_audit: Any
    changes: ChangeRequests
    authorities: KubernetesChangeAuthorities
    approvals: KubernetesPhaseApprovals
    authorize_admin: Any
    require_fresh_auth: Any
    request_session: Any
    csrf_valid: Any
    request_id_for: Any
    error_payload: Any

    def dispatch(self, handler: Any, route_path: str) -> bool:
        path = route_path
        is_fresh = self.is_fresh
        record_admin_audit = self.record_admin_audit
        changes = self.changes
        authorities = self.authorities
        approvals = self.approvals
        authorize_admin = self.authorize_admin
        require_fresh_auth = self.require_fresh_auth
        request_session = self.request_session
        csrf_valid = self.csrf_valid
        request_id_for = self.request_id_for
        error_payload = self.error_payload
        admin_id = _admin_route(path)
        if admin_id is not False:
            return _dispatch_admin(
                handler, admin_id, record_admin_audit, authorities, authorize_admin, require_fresh_auth,
                request_id_for, error_payload,
            )
        review_route = _review_route(path)
        if review_route is None:
            return False
        change_request_id, approving = review_route
        if (approving and handler.command != "POST") or (not approving and handler.command != "GET"):
            return False
        request_id = request_id_for(handler)
        session, auth_mode = request_session(handler)
        if session is None:
            approvals.record_denial(
                change_request_id, actor_id=None, result="unauthorized",
                reason="unavailable_before_authentication", request_id=request_id,
            )
            handler.write_json(
                HTTPStatus.UNAUTHORIZED,
                error_payload("unauthorized", "authentication required", request_id),
            )
            return True
        attempt_reason = "approval_denied"
        approval_delegated = False
        try:
            changes.get(change_request_id)
            if not approving:
                review = approvals.review(change_request_id, actor_id=session.actor.actor_id)
                handler.write_json(
                    HTTPStatus.OK, {"request_id": request_id, "phase_review": review},
                )
                return True
            if auth_mode == "cookie" and not csrf_valid(handler, session.token):
                approvals.record_denial(
                    change_request_id, actor_id=session.actor.actor_id, result="csrf_required",
                    reason=attempt_reason, request_id=request_id,
                )
                handler.write_json(
                    HTTPStatus.FORBIDDEN,
                    error_payload("csrf_required", "missing or invalid CSRF token", request_id),
                )
                return True
            if not is_fresh(session.token):
                approvals.record_denial(
                    change_request_id, actor_id=session.actor.actor_id, result="fresh_auth_required",
                    reason=attempt_reason, request_id=request_id,
                )
                handler.write_json(
                    HTTPStatus.FORBIDDEN,
                    error_payload("fresh_auth_required", "re-authentication is required", request_id),
                )
                return True
            payload = handler.read_json_body()
            required = {
                "revision_id", "dry_run_hashes", "target_confirmations",
                "rollback_policy", "reason", "idempotency_key",
            }
            if set(payload) != required:
                raise KubernetesPhaseApprovalError("invalid_request", "Exact Approval fields are required")
            approval_delegated = True
            review = approvals.approve(
                change_request_id,
                actor_id=session.actor.actor_id,
                revision_id=payload["revision_id"],
                dry_run_hashes=payload["dry_run_hashes"],
                target_confirmations=payload["target_confirmations"],
                rollback_policy=payload["rollback_policy"],
                reason=payload["reason"],
                idempotency_key=payload["idempotency_key"],
                request_id=request_id,
            )
            approval = review["approval"]
            assert isinstance(approval, dict)
            handler.write_json(
                HTTPStatus.OK if approval["idempotent"] else HTTPStatus.CREATED,
                {"request_id": request_id, "phase_review": review},
            )
        except (KubernetesPhaseApprovalError, ChangeRequestError) as exc:
            code = exc.code
            message = exc.message
            if not approval_delegated:
                approvals.record_denial(
                    change_request_id, actor_id=session.actor.actor_id, result=code,
                    reason=attempt_reason, request_id=request_id,
                )
            status = {
                "not_found": HTTPStatus.NOT_FOUND,
                "phase_stale": HTTPStatus.CONFLICT,
                "phase_expired": HTTPStatus.CONFLICT,
                "idempotency_conflict": HTTPStatus.CONFLICT,
            }.get(code, HTTPStatus.BAD_REQUEST)
            handler.write_json(status, error_payload(code, message, request_id))
        except (TypeError, ValueError) as exc:
            approvals.record_denial(
                change_request_id, actor_id=session.actor.actor_id, result="invalid_request",
                reason=attempt_reason, request_id=request_id,
            )
            handler.write_json(
                HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id),
            )
        return True


def _dispatch_admin(
    handler: Any,
    authority_id: str | None,
    record_admin_audit: Callable[..., None],
    authorities: KubernetesChangeAuthorities,
    authorize_admin: Callable[..., Any],
    require_fresh_auth: Callable[..., bool],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    if authority_id is None and handler.command == "GET":
        request_id = request_id_for(handler)
        if authorize_admin(handler, request_id) is not None:
            handler.write_json(HTTPStatus.OK, {
                "request_id": request_id,
                "kubernetes_change_authorities": authorities.list(),
            })
        return True
    if (authority_id is None and handler.command != "POST") or (
        authority_id is not None and handler.command != "PATCH"
    ):
        return False
    request_id = request_id_for(handler)
    action = f"kubernetes-change-authorities_{'update' if authority_id else 'create'}"
    target = ("kubernetes-change-authorities", authority_id, action)
    session = authorize_admin(handler, request_id, audit_target=target)
    if session is None:
        return True
    reason = ""
    try:
        payload = handler.read_json_body()
        reason = payload.pop("reason", "")
        if not isinstance(reason, str) or not reason.strip():
            raise KubernetesChangeAuthorityError("reason_required", "reason is required")
        if not require_fresh_auth(
            handler, session, request_id, audit_target=target, reason=reason.strip(),
        ):
            return True
        if authority_id is None:
            if set(payload) != {"user_id", "environment", "scope_type", "scope"}:
                raise KubernetesChangeAuthorityError("invalid_request", "Authority fields are invalid")
            authority = authorities.create(
                user_id=payload["user_id"], environment=payload["environment"],
                scope_type=payload["scope_type"], scope=payload["scope"],
                actor_id=session.actor.actor_id, reason=reason.strip(), request_id=request_id,
            )
            status = HTTPStatus.CREATED
        else:
            if set(payload) != {"active"}:
                raise KubernetesChangeAuthorityError("invalid_request", "active is required")
            authority = authorities.update(
                authority_id, active=payload["active"], actor_id=session.actor.actor_id,
                reason=reason.strip(), request_id=request_id,
            )
            status = HTTPStatus.OK
        handler.write_json(status, {
            "request_id": request_id, "kubernetes_change_authority": authority,
        })
    except KubernetesChangeAuthorityError as exc:
        _admin_denial(
            record_admin_audit, session.actor.actor_id, target, exc.code, request_id,
            reason.strip() if isinstance(reason, str) and reason.strip() else "unavailable_before_validation",
        )
        status = (
            HTTPStatus.NOT_FOUND if exc.code.endswith("not_found")
            else HTTPStatus.CONFLICT if exc.code == "authority_exists"
            else HTTPStatus.BAD_REQUEST
        )
        handler.write_json(status, error_payload(exc.code, exc.message, request_id))
    except (TypeError, ValueError) as exc:
        _admin_denial(
            record_admin_audit, session.actor.actor_id, target, "invalid_request", request_id,
            "unavailable_before_validation",
        )
        handler.write_json(
            HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id),
        )
    return True


def _admin_denial(
    record_admin_audit: Callable[..., None],
    actor_id: str,
    target: tuple[str, str | None, str],
    result: str,
    request_id: str,
    reason: str,
) -> None:
    target_type, target_id, action = target
    record_admin_audit(
        actor_id=actor_id, target_type=target_type, target_id=target_id,
        action=action, reason=reason, before=None, after=None,
        result=result, request_id=request_id,
    )


def _admin_route(path: str) -> str | None | bool:
    base = "/api/v1/admin/kubernetes-change-authorities"
    if path == base:
        return None
    prefix = f"{base}/"
    if path.startswith(prefix):
        authority_id = unquote(path[len(prefix):]).strip("/")
        return authority_id if authority_id and "/" not in authority_id else False
    return False


def _review_route(path: str) -> tuple[str, bool] | None:
    prefix = "/api/v1/change-requests/"
    approve_suffix = "/phase-approval/approve"
    review_suffix = "/phase-approval"
    if not path.startswith(prefix):
        return None
    if path.endswith(approve_suffix):
        suffix, approving = approve_suffix, True
    elif path.endswith(review_suffix):
        suffix, approving = review_suffix, False
    else:
        return None
    change_request_id = unquote(path[len(prefix):-len(suffix)]).strip("/")
    return (change_request_id, approving) if change_request_id and "/" not in change_request_id else None
