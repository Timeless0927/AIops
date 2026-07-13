"""Gateway-owned Authority and Approval for generic Kubernetes Change Phases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .change_plan_phases import ChangePlanPhases
from .change_requests import ChangeRequestError
from .connector_enrollments import ConnectorEnrollments
from .gateway_db import GatewayDatabase, register_migrations
from .kubernetes_change_authorities import KubernetesChangeAuthorities
from .kubernetes_change_validation import KubernetesChangeValidation
from .kubernetes_inverse_changes import freeze_inverse_change

_APPROVAL_SCHEMA_VERSION = 24
_APPROVAL_SCHEMA = """
CREATE TABLE kubernetes_phase_approvals (
    id TEXT PRIMARY KEY,
    phase_id TEXT NOT NULL UNIQUE REFERENCES change_plan_phases(id),
    revision_id TEXT NOT NULL UNIQUE REFERENCES change_plan_revisions(id),
    approver_id TEXT NOT NULL REFERENCES users(id),
    authority_ids_json TEXT NOT NULL CHECK (json_valid(authority_ids_json)),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    reason TEXT NOT NULL,
    request_id TEXT NOT NULL,
    rollback_policy TEXT NOT NULL CHECK (rollback_policy IN ('stop_only', 'rollback_completed')),
    target_confirmations_json TEXT NOT NULL CHECK (json_valid(target_confirmations_json)),
    frozen_changes_json TEXT NOT NULL CHECK (json_valid(frozen_changes_json)),
    dry_run_expires_at REAL NOT NULL,
    approved_at REAL NOT NULL,
    start_expires_at REAL NOT NULL,
    UNIQUE(approver_id, idempotency_key)
);
CREATE TABLE kubernetes_phase_approval_audit (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id),
    phase_id TEXT REFERENCES change_plan_phases(id),
    actor_id TEXT,
    result TEXT NOT NULL,
    reason TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""
register_migrations(((_APPROVAL_SCHEMA_VERSION, _APPROVAL_SCHEMA),))


class KubernetesPhaseApprovalError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KubernetesPhaseApprovals:
    """Owns explicit Kubernetes mutation Authority and frozen Phase Approvals."""

    def __init__(
        self,
        database: GatewayDatabase | Path | str,
        *,
        enrollments: ConnectorEnrollments,
        authorities: KubernetesChangeAuthorities,
        phases: ChangePlanPhases,
        validation: KubernetesChangeValidation,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
        dry_run_seconds: float = 10 * 60,
        approved_start_seconds: float = 15 * 60,
    ) -> None:
        self._database = database if isinstance(database, GatewayDatabase) else GatewayDatabase(database)
        self._enrollments = enrollments
        self._authorities = authorities
        self._phases = phases
        self._validation = validation
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")
        self._dry_run_seconds = dry_run_seconds
        self._approved_start_seconds = approved_start_seconds

    def record_denial(
        self,
        change_request_id: str,
        *,
        actor_id: str | None,
        result: str,
        reason: str,
        request_id: str,
    ) -> None:
        with self._database.connect() as conn:
            context = self._phases.approval_context_in(conn, change_request_id)
            if context is None:
                return
            _audit_attempt_in(
                conn, change_request_id=change_request_id,
                phase_id=str(context["phase_id"]) if context else None,
                actor_id=actor_id, result=result, reason=reason,
                request_id=request_id, now=self._clock(),
            )
            conn.commit()

    def audit_history(self, change_request_id: str) -> list[dict[str, object]]:
        with self._database.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, phase_id, actor_id, result, reason, request_id, created_at
                FROM kubernetes_phase_approval_audit
                WHERE change_request_id = ? ORDER BY event_id
                """,
                (change_request_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def review(self, change_request_id: str, *, actor_id: str) -> dict[str, object]:
        now = self._clock()
        with self._database.connect() as conn:
            return self._review_in(conn, change_request_id, actor_id=actor_id, now=now)

    def access_for_projection(
        self, change_request_id: str, actor_id: str, phase_status: str,
    ) -> tuple[bool, dict[str, object] | None]:
        if phase_status not in {
            "awaiting_approval", "approved", "expired", "executing", "succeeded", "failed",
            "unknown_outcome", "cancel_requested", "cancelled", "rolling_back", "rolled_back",
            "rollback_failed",
        }:
            with self._database.connect() as conn:
                return self._draft_authorized_in(conn, change_request_id, actor_id=actor_id), None
        try:
            return True, self.review(change_request_id, actor_id=actor_id)
        except KubernetesPhaseApprovalError as exc:
            if exc.code != "not_found":
                raise
            return False, None

    def approve(
        self,
        change_request_id: str,
        *,
        actor_id: str,
        revision_id: str,
        dry_run_hashes: list[str],
        target_confirmations: list[str],
        rollback_policy: str,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        try:
            return self._approve(
                change_request_id, actor_id=actor_id, revision_id=revision_id,
                dry_run_hashes=dry_run_hashes, target_confirmations=target_confirmations,
                rollback_policy=rollback_policy, reason=reason,
                idempotency_key=idempotency_key, request_id=request_id,
            )
        except (KubernetesPhaseApprovalError, ChangeRequestError) as exc:
            audit_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "unavailable_before_validation"
            self.record_denial(
                change_request_id, actor_id=actor_id, result=exc.code,
                reason=audit_reason, request_id=request_id,
            )
            if isinstance(exc, ChangeRequestError):
                raise KubernetesPhaseApprovalError(exc.code, exc.message) from exc
            raise

    def _approve(
        self,
        change_request_id: str,
        *,
        actor_id: str,
        revision_id: str,
        dry_run_hashes: list[str],
        target_confirmations: list[str],
        rollback_policy: str,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        revision_id = _text(revision_id, "revision_id")
        reason = _text(reason, "reason")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if rollback_policy not in {"stop_only", "rollback_completed"}:
            raise KubernetesPhaseApprovalError("invalid_request", "rollback_policy is invalid")
        if not isinstance(dry_run_hashes, list) or not all(
            isinstance(value, str) and len(value) == 64 for value in dry_run_hashes
        ):
            raise KubernetesPhaseApprovalError("invalid_request", "dry_run_hashes are invalid")
        if not isinstance(target_confirmations, list) or not all(
            isinstance(value, str) and value for value in target_confirmations
        ):
            raise KubernetesPhaseApprovalError("invalid_request", "target_confirmations are invalid")
        request_hash = hashlib.sha256(_json({
            "change_request_id": change_request_id,
            "revision_id": revision_id,
            "dry_run_hashes": dry_run_hashes,
            "target_confirmations": target_confirmations,
            "rollback_policy": rollback_policy,
            "reason": reason,
        }).encode()).hexdigest()
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            review = self._review_in(conn, change_request_id, actor_id=actor_id, now=now)
            replay = conn.execute(
                "SELECT * FROM kubernetes_phase_approvals WHERE approver_id = ? AND idempotency_key = ?",
                (actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    self._reject_in(
                        conn, "idempotency_conflict",
                        "Idempotency key was used for another Approval",
                    )
                review["approval"] = _approval(replay, idempotent=True)
                conn.rollback()
                return review
            if str(review["revision_id"]) != revision_id:
                self._reject_in(
                    conn, "phase_stale", "Change Plan revision is stale",
                )
            if str(review["status"]) == "expired":
                self._reject_in(
                    conn, "phase_expired", "Change Plan Phase has expired",
                )
            if str(review["status"]) != "awaiting_approval":
                self._reject_in(
                    conn, "phase_stale", "Change Plan Phase is no longer approvable",
                )
            if now > float(review["dry_run_expires_at"]):
                self._expire_phase_in(conn, review, now=now, reason="dry_run_expired")
                self._reject_in(
                    conn, "phase_expired", "Server-side dry-run has expired",
                )
            changes = review["changes"]
            assert isinstance(changes, list)
            expected_hashes = [str(change["dry_run_hash"]) for change in changes]
            expected_confirmations = [str(change["target_confirmation"]) for change in changes]
            if dry_run_hashes != expected_hashes or target_confirmations != expected_confirmations:
                self._reject_in(
                    conn, "phase_stale", "Exact Change Plan confirmation does not match",
                )
            if rollback_policy == "rollback_completed" and any(
                change.get("inverse_change") is None for change in changes
            ):
                self._reject_in(
                    conn, "rollback_unavailable",
                    "rollback_completed requires an exact frozen inverse for every Change",
                )
            approval_id = self._id_factory("kubernetes-approval")
            authority_ids = [str(change["authority_id"]) for change in changes]
            start_expires_at = now + self._approved_start_seconds
            conn.execute(
                """
                INSERT INTO kubernetes_phase_approvals (
                    id, phase_id, revision_id, approver_id, authority_ids_json,
                    idempotency_key, request_hash, reason, request_id, rollback_policy,
                    target_confirmations_json, frozen_changes_json, dry_run_expires_at,
                    approved_at, start_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id, review["phase_id"], revision_id, actor_id, _json(authority_ids),
                    idempotency_key, request_hash, reason, request_id, rollback_policy,
                    _json(target_confirmations), _json(changes), review["dry_run_expires_at"],
                    now, start_expires_at,
                ),
            )
            self._phases.record_approved_in(
                conn, change_request_id=change_request_id, phase_id=str(review["phase_id"]),
                revision_id=revision_id, approval_id=approval_id, actor_id=actor_id,
                rollback_policy=rollback_policy, start_expires_at=start_expires_at,
                request_id=request_id, now=now,
            )
            _audit_attempt_in(
                conn, change_request_id=change_request_id, phase_id=str(review["phase_id"]),
                actor_id=actor_id, result="approved", reason=reason,
                request_id=request_id, now=now,
            )
            row = conn.execute(
                "SELECT * FROM kubernetes_phase_approvals WHERE id = ?", (approval_id,),
            ).fetchone()
            assert row is not None
            conn.commit()
        review["status"] = "approved"
        review["approval"] = _approval(row, idempotent=False)
        return review

    def authorize_start(
        self, phase_id: str, *, request_id: str, stage: str,
    ) -> dict[str, object]:
        if stage not in {"grant", "dispatch"}:
            raise ValueError("stage must be grant or dispatch")
        try:
            approval = self._authorize_start(phase_id)
        except KubernetesPhaseApprovalError as exc:
            self._audit_start_check(
                phase_id, result=exc.code, stage=stage, request_id=request_id,
            )
            raise
        self._audit_start_check(
            phase_id, result="authorized", stage=stage, request_id=request_id,
            actor_id=str(approval["approver_id"]),
        )
        return approval

    def authorize_cancel(
        self, phase_id: str, *, actor_id: str, request_id: str,
    ) -> dict[str, object]:
        try:
            approval = self._authorize_start(
                phase_id, enforce_start_window=False, allow_expired=True,
            )
            if approval["approver_id"] != actor_id:
                raise KubernetesPhaseApprovalError(
                    "approval_actor_mismatch", "Only the approver may cancel this execution",
                )
        except KubernetesPhaseApprovalError as exc:
            self._audit_start_check(
                phase_id, result=exc.code, stage="cancel", request_id=request_id,
                actor_id=actor_id,
            )
            raise
        self._audit_start_check(
            phase_id, result="authorized", stage="cancel", request_id=request_id,
            actor_id=actor_id,
        )
        return approval

    def _authorize_start(
        self, phase_id: str, *, enforce_start_window: bool = True,
        allow_expired: bool = False,
    ) -> dict[str, object]:
        now = self._clock()
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM kubernetes_phase_approvals WHERE phase_id = ?", (phase_id,),
            ).fetchone()
            if row is None:
                raise KubernetesPhaseApprovalError("phase_not_approved", "Change Plan Phase is not approved")
            change_request_id = self._phases.change_request_id_in(conn, phase_id)
            if change_request_id is None:
                raise KubernetesPhaseApprovalError("phase_not_approved", "Change Plan Phase is not approved")
            context = self._phase_context_for_approval_in(
                conn, phase_id, str(row["revision_id"]),
            )
            changes = context["changes"]
            assert isinstance(changes, list)
            authority_ids = self._authorities.matching_ids_in(
                conn,
                actor_id=str(row["approver_id"]),
                environment=str(context["environment"]),
                cluster_id=str(context["cluster_id"]),
                targets=_validated_targets(changes),
            )
            if authority_ids is None:
                raise KubernetesPhaseApprovalError("authority_revoked", "Approval Authority no longer covers this Phase")
            phase_status = str(context["phase_status"])
            allowed_statuses = {"approved", "executing", "rolling_back"}
            if allow_expired:
                allowed_statuses.add("expired")
            if phase_status not in allowed_statuses:
                raise KubernetesPhaseApprovalError("phase_expired", "Approved Phase is no longer startable")
            if (
                enforce_start_window and phase_status == "approved"
                and now > float(row["start_expires_at"])
            ):
                review = {
                    "change_request_id": context["change_request_id"],
                    "phase_id": phase_id,
                    "revision_id": context["revision_id"],
                }
                self._expire_phase_in(conn, review, now=now, reason="approved_start_expired")
                conn.commit()
                raise KubernetesPhaseApprovalError("phase_expired", "Approved Phase start window has expired")
            conn.rollback()
            return {
                **_approval(row, idempotent=False),
                "change_request_id": str(context["change_request_id"]),
                "cluster_id": str(context["cluster_id"]),
                "environment": str(context["environment"]),
            }

    def _audit_start_check(
        self,
        phase_id: str,
        *,
        result: str,
        stage: str,
        request_id: str,
        actor_id: str | None = None,
    ) -> None:
        with self._database.connect() as conn:
            change_request_id = self._phases.change_request_id_in(conn, phase_id)
            if change_request_id is None:
                return
            if actor_id is None:
                row = conn.execute(
                    "SELECT approver_id FROM kubernetes_phase_approvals WHERE phase_id = ?",
                    (phase_id,),
                ).fetchone()
                actor_id = str(row["approver_id"]) if row else None
            _audit_attempt_in(
                conn, change_request_id=change_request_id, phase_id=phase_id,
                actor_id=actor_id, result=result, reason=f"{stage}_authorization",
                request_id=request_id, now=self._clock(),
            )
            conn.commit()

    def _review_in(
        self,
        conn: sqlite3.Connection,
        change_request_id: str,
        *,
        actor_id: str,
        now: float,
    ) -> dict[str, object]:
        context = self._phase_context_in(conn, change_request_id)
        changes = context["changes"]
        assert isinstance(changes, list)
        authority_ids = self._authorities.matching_ids_in(
            conn,
            actor_id=actor_id,
            environment=str(context["environment"]),
            cluster_id=str(context["cluster_id"]),
            targets=_validated_targets(changes),
        )
        if authority_ids is None:
            raise KubernetesPhaseApprovalError("not_found", "Change Plan Phase not found")
        expires_at = min(float(change["validated_at"]) for change in changes) + self._dry_run_seconds
        approval = conn.execute(
            "SELECT * FROM kubernetes_phase_approvals WHERE phase_id = ?", (context["phase_id"],),
        ).fetchone()
        phase_status = str(context["phase_status"])
        expiry_reason = None
        if phase_status == "awaiting_approval" and now > expires_at:
            expiry_reason = "dry_run_expired"
        elif (
            phase_status == "approved"
            and approval is not None
            and now > float(approval["start_expires_at"])
        ):
            expiry_reason = "approved_start_expired"
        if expiry_reason is not None:
            self._expire_phase_in(
                conn,
                {
                    "change_request_id": context["change_request_id"],
                    "phase_id": context["phase_id"],
                    "revision_id": context["revision_id"],
                },
                now=now,
                reason=expiry_reason,
            )
            phase_status = "expired"
        return {
            "change_request_id": str(context["change_request_id"]),
            "phase_id": str(context["phase_id"]),
            "revision_id": str(context["revision_id"]),
            "revision_number": int(context["revision_number"]),
            "status": phase_status,
            "environment": str(context["environment"]),
            "summary": str(context["summary"]),
            "changes": [
                _review_change(change, authority_id=authority_ids[index])
                for index, change in enumerate(changes)
            ],
            "dry_run_expires_at": expires_at,
            "approval": _approval(approval, idempotent=False) if approval is not None else None,
            "reviewed_at": now,
        }

    def _phase_context_in(
        self, conn: sqlite3.Connection, change_request_id: str,
    ) -> dict[str, object]:
        context = self._phases.approval_context_in(conn, change_request_id)
        return self._complete_phase_context_in(conn, context)

    def _phase_context_for_approval_in(
        self, conn: sqlite3.Connection, phase_id: str, revision_id: str,
    ) -> dict[str, object]:
        context = self._phases.approval_context_for_phase_in(conn, phase_id, revision_id)
        return self._complete_phase_context_in(conn, context)

    def _complete_phase_context_in(
        self, conn: sqlite3.Connection, context: dict[str, object] | None,
    ) -> dict[str, object]:
        if context is None or context["phase_status"] not in {
            "awaiting_approval", "approved", "expired", "executing", "succeeded", "failed",
            "unknown_outcome", "cancel_requested", "cancelled", "rolling_back", "rolled_back",
            "rollback_failed",
        }:
            raise KubernetesPhaseApprovalError("not_found", "Change Plan Phase not found")
        validated = self._validation.approval_results_in(conn, str(context["revision_id"]))
        if validated is None:
            raise KubernetesPhaseApprovalError("not_found", "Change Plan Phase not found")
        cluster_id = str(validated["cluster_id"])
        environment = self._enrollments.cluster_environment_in(conn, cluster_id)
        plan = context["plan"]
        if environment is None or not isinstance(plan, dict):
            raise KubernetesPhaseApprovalError("not_found", "Change Plan Phase not found")
        return {
            **context, "cluster_id": cluster_id, "environment": environment,
            "summary": str(plan["summary"]), "changes": validated["changes"],
        }

    def _draft_authorized_in(
        self, conn: sqlite3.Connection, change_request_id: str, *, actor_id: str,
    ) -> bool:
        context = self._phases.approval_context_in(conn, change_request_id)
        if context is None or not isinstance(context["plan"], dict):
            return False
        cluster_id = self._validation.revision_cluster_in(conn, str(context["revision_id"]))
        environment = self._enrollments.cluster_environment_in(conn, cluster_id or "")
        changes = context["plan"]["changes"]
        targets = [change.get("target") if isinstance(change, dict) else None for change in changes]
        return bool(environment) and bool(targets) and all(isinstance(target, dict) for target in targets) and self._authorities.targets_authorized_in(
            conn, actor_id=actor_id, environment=str(environment), cluster_id=str(cluster_id),
            targets=targets,  # type: ignore[arg-type]
        )

    def _expire_phase_in(
        self,
        conn: sqlite3.Connection,
        review: dict[str, object],
        *,
        now: float,
        reason: str,
    ) -> None:
        self._phases.record_expired_in(
            conn,
            change_request_id=str(review["change_request_id"]),
            phase_id=str(review["phase_id"]),
            revision_id=str(review["revision_id"]),
            reason=reason,
            now=now,
        )

    @staticmethod
    def _reject_in(conn: sqlite3.Connection, result: str, message: str) -> None:
        conn.commit()
        raise KubernetesPhaseApprovalError(result, message)


def _review_change(change: dict[str, object], *, authority_id: str) -> dict[str, object]:
    result = change["result"]
    assert isinstance(result, dict)
    canonical = result["canonical_change"]
    dry_run = result["dry_run"]
    assert isinstance(canonical, dict) and isinstance(dry_run, dict)
    target = canonical["target"]
    assert isinstance(target, dict)
    namespace = target.get("namespace") or "cluster"
    operation = str(canonical["operation"])
    risk = "high" if operation == "delete" or target.get("namespace") is None else "medium"
    projected = {
        "ordinal": int(change["ordinal"]),
        "target": target,
        "target_confirmation": (
            f"{target['api_version']}:{target['kind']}:{namespace}/{target['name']}"
        ),
        "operation": operation,
        "canonical_change": canonical,
        "diff": dry_run["diff"],
        "dry_run_hash": str(dry_run["hash"]),
        "risk": risk,
        "post_checks": canonical["post_checks"],
        "authority_id": authority_id,
    }
    projected["inverse_change"] = freeze_inverse_change(projected)
    return projected


def _approval(row: sqlite3.Row, *, idempotent: bool) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "phase_id": str(row["phase_id"]),
        "revision_id": str(row["revision_id"]),
        "approver_id": str(row["approver_id"]),
        "authority_ids": json.loads(str(row["authority_ids_json"])),
        "reason": str(row["reason"]),
        "request_id": str(row["request_id"]),
        "rollback_policy": str(row["rollback_policy"]),
        "target_confirmations": json.loads(str(row["target_confirmations_json"])),
        "frozen_changes": json.loads(str(row["frozen_changes_json"])),
        "dry_run_expires_at": float(row["dry_run_expires_at"]),
        "approved_at": float(row["approved_at"]),
        "start_expires_at": float(row["start_expires_at"]),
        "idempotent": idempotent,
    }


def _audit_attempt_in(
    conn: sqlite3.Connection,
    *,
    change_request_id: str,
    phase_id: str | None,
    actor_id: str | None,
    result: str,
    reason: str,
    request_id: str,
    now: float,
) -> None:
    conn.execute(
        """
        INSERT INTO kubernetes_phase_approval_audit (
            change_request_id, phase_id, actor_id, result, reason, request_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (change_request_id, phase_id, actor_id, result, reason, request_id, now),
    )


def _validated_targets(changes: list[dict[str, object]]) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for change in changes:
        result = change["result"]
        assert isinstance(result, dict)
        canonical = result["canonical_change"]
        assert isinstance(canonical, dict) and isinstance(canonical["target"], dict)
        targets.append(canonical["target"])
    return targets


def _text(value: object, field: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > 500:
        raise KubernetesPhaseApprovalError("invalid_request", f"{field} is required")
    return normalized


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
