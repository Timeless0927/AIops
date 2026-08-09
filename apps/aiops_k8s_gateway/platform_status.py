"""Gateway-owned Platform Status projection and setup decisions."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from sqlite3 import Row

from .gateway_audit import insert_admin_audit
from .gateway_db import GatewayDatabase, register_migrations


JSON = dict[str, object]
OwnerStatus = Callable[[str], JSON]
ConnectorStatus = Callable[[], JSON]
_VERIFICATION_STATES = {
    "not_applicable", "unverified", "verifying", "verified", "failed", "stale",
}
_AVAILABILITY_STATES = {"available", "degraded", "unavailable"}
_SAFE_REASON_CODES = {
    "not_configured", "test_required", "configuration_changed", "authentication_failed",
    "rate_limited", "timeout", "provider_unavailable", "provider_rejected",
    "invalid_response", "owner_unavailable", "connector_degraded", "connector_disabled",
    "connector_offline", "connector_pending_registration", "connector_rotation_pending",
}
_CONNECTOR_STATES = {
    "pending_registration", "online", "offline", "rotation_pending", "disabled", "degraded",
}
_SCHEMA_VERSION = 39
_SCHEMA = """
CREATE TABLE platform_setup_decisions (
    capability TEXT PRIMARY KEY CHECK (capability = 'notification'),
    decision TEXT NOT NULL CHECK (decision IN ('active', 'skipped')),
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) > 0),
    decided_at REAL NOT NULL,
    request_id TEXT NOT NULL
);

CREATE TABLE platform_setup_decision_operations (
    request_id TEXT PRIMARY KEY,
    capability TEXT NOT NULL CHECK (capability = 'notification'),
    decision TEXT NOT NULL CHECK (decision IN ('active', 'skipped')),
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) > 0),
    decided_at REAL NOT NULL
);
"""
_SCHEMA_V40 = "ALTER TABLE platform_setup_decision_operations ADD COLUMN expected_revision TEXT;"
_SCHEMA_V41 = """
ALTER TABLE platform_setup_decision_operations
ADD COLUMN identity_version INTEGER NOT NULL DEFAULT 1;
CREATE TABLE platform_configuration_resume_operations (
    request_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) > 0),
    resumed INTEGER NOT NULL CHECK (resumed IN (0, 1)),
    decided_at REAL NOT NULL
);
"""
_SCHEMA_V42 = """
UPDATE platform_setup_decision_operations
SET identity_version = 2
WHERE expected_revision IS NOT NULL;
INSERT OR IGNORE INTO platform_configuration_resume_operations (
    request_id, actor_id, reason, resumed, decided_at
)
SELECT
    audit.request_id,
    audit.actor_id,
    audit.reason,
    CASE WHEN EXISTS (
        SELECT 1 FROM admin_audit AS resume
        WHERE resume.request_id = audit.request_id
          AND resume.action = 'setup_decision_resume_after_configuration'
          AND resume.result = 'success'
    ) THEN 1 ELSE 0 END,
    audit.created_at
FROM admin_audit AS audit
WHERE audit.target_type = 'notification-destinations'
  AND audit.action IN ('notification-destinations_create', 'notification-destinations_update')
  AND audit.result = 'success'
  AND audit.actor_id IS NOT NULL;
"""
register_migrations((
    (_SCHEMA_VERSION, _SCHEMA), (40, _SCHEMA_V40), (41, _SCHEMA_V41), (42, _SCHEMA_V42),
))


class PlatformSetupDecisions:
    """Owns durable platform-level decisions for optional capabilities."""

    def __init__(
        self,
        database: GatewayDatabase,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._clock = clock

    def decision(self, capability: str) -> str:
        if capability != "notification":
            return "active"
        with self._database.connect() as conn:
            row = conn.execute(
                "SELECT decision FROM platform_setup_decisions WHERE capability = ?",
                (capability,),
            ).fetchone()
        return str(row["decision"]) if row is not None else "active"

    def set(
        self,
        *,
        capability: str,
        decision: str,
        expected_revision: str | None = None,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> JSON:
        reason = _validate_setup_decision(capability, decision, reason)
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT * FROM platform_setup_decision_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if replay is not None:
                return _replayed_decision(
                    replay, capability=capability, decision=decision,
                    expected_revision=expected_revision, actor_id=actor_id, reason=reason,
                )
            decided_at = self._clock()
            before_row = conn.execute(
                "SELECT decision, actor_id, decided_at FROM platform_setup_decisions WHERE capability = ?",
                (capability,),
            ).fetchone()
            before = dict(before_row) if before_row is not None else None
            conn.execute(
                """INSERT INTO platform_setup_decisions
                   (capability, decision, actor_id, reason, decided_at, request_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(capability) DO UPDATE SET
                     decision = excluded.decision,
                     actor_id = excluded.actor_id,
                     reason = excluded.reason,
                     decided_at = excluded.decided_at,
                     request_id = excluded.request_id""",
                (capability, decision, actor_id, reason, decided_at, request_id),
            )
            conn.execute(
                """INSERT INTO platform_setup_decision_operations
                   (request_id, capability, decision, expected_revision, identity_version,
                    actor_id, reason, decided_at)
                   VALUES (?, ?, ?, ?, 2, ?, ?, ?)""",
                (
                    request_id, capability, decision, expected_revision,
                    actor_id, reason, decided_at,
                ),
            )
            result = {
                "capability": capability,
                "setup_decision": decision,
                "decided_by": actor_id,
                "decided_at": decided_at,
            }
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="platform_capability",
                target_id=capability,
                action="setup_decision_update",
                reason=reason,
                before=before,
                after=result,
                result="success",
                request_id=request_id,
            )
            conn.commit()
        return result

    def replay(
        self,
        *,
        capability: str,
        decision: str,
        expected_revision: str | None,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> JSON | None:
        reason = _validate_setup_decision(capability, decision, reason)
        with self._database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_setup_decision_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return _replayed_decision(
            row, capability=capability, decision=decision,
            expected_revision=expected_revision, actor_id=actor_id, reason=reason,
        )

    def activate_after_configuration(
        self,
        *,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> JSON | None:
        """Resume skipped Notification setup after a known successful owner save."""
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT * FROM platform_configuration_resume_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if replay is not None:
                if (str(replay["actor_id"]), str(replay["reason"])) != (actor_id, reason):
                    raise PlatformStatusError(
                        "idempotency_conflict",
                        "request_id was already used for a different configuration save",
                    )
                return {
                    "capability": "notification",
                    "setup_decision": "active",
                    "decided_by": actor_id,
                    "decided_at": float(replay["decided_at"]),
                } if replay["resumed"] else None
            before_row = conn.execute(
                "SELECT decision, actor_id, decided_at FROM platform_setup_decisions "
                "WHERE capability = 'notification'",
            ).fetchone()
            decided_at = self._clock()
            resumed = before_row is not None and before_row["decision"] == "skipped"
            conn.execute(
                """INSERT INTO platform_configuration_resume_operations
                   (request_id, actor_id, reason, resumed, decided_at) VALUES (?, ?, ?, ?, ?)""",
                (request_id, actor_id, reason, resumed, decided_at),
            )
            if not resumed:
                conn.commit()
                return None
            result = {
                "capability": "notification",
                "setup_decision": "active",
                "decided_by": actor_id,
                "decided_at": decided_at,
            }
            conn.execute(
                """UPDATE platform_setup_decisions
                   SET decision = 'active', actor_id = ?, reason = ?, decided_at = ?, request_id = ?
                   WHERE capability = 'notification' AND decision = 'skipped'""",
                (actor_id, reason, decided_at, request_id),
            )
            insert_admin_audit(
                conn,
                actor_id=actor_id,
                target_type="platform_capability",
                target_id="notification",
                action="setup_decision_resume_after_configuration",
                reason=reason,
                before=dict(before_row),
                after=result,
                result="success",
                request_id=request_id,
            )
            conn.commit()
        return result


class PlatformStatusError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_setup_decision(capability: str, decision: str, reason: str) -> str:
    if capability != "notification":
        raise PlatformStatusError("required_capability", "required capability cannot be skipped")
    if decision not in {"active", "skipped"}:
        raise PlatformStatusError("invalid_setup_decision", "setup decision is invalid")
    reason = reason.strip()
    if not reason:
        raise PlatformStatusError("reason_required", "reason is required")
    return reason


def _replayed_decision(
    row: Row,
    *,
    capability: str,
    decision: str,
    expected_revision: str | None,
    actor_id: str,
    reason: str,
) -> JSON:
    mutation = (
        str(row["capability"]),
        str(row["decision"]),
        str(row["actor_id"]),
        str(row["reason"]),
    )
    if mutation != (capability, decision, actor_id, reason) or (
        int(row["identity_version"]) >= 2 and row["expected_revision"] != expected_revision
    ):
        raise PlatformStatusError(
            "idempotency_conflict",
            "request_id was already used for a different setup decision",
        )
    return {
        "capability": str(row["capability"]),
        "setup_decision": str(row["decision"]),
        "decided_by": str(row["actor_id"]),
        "decided_at": float(row["decided_at"]),
    }


class PlatformStatus:
    """Aggregates owner-held status without persisting integration state."""

    def __init__(
        self,
        decisions: PlatformSetupDecisions,
        *,
        model_status: OwnerStatus,
        notification_status: OwnerStatus,
        connector_status: ConnectorStatus,
        observability_status: OwnerStatus,
        clock: Callable[[], float] = time.time,
        owner_timeout_seconds: float = 3.0,
    ) -> None:
        self._decisions = decisions
        self._model_status = model_status
        self._notification_status = notification_status
        self._connector_status = connector_status
        self._observability_status = observability_status
        self._clock = clock
        self._owner_timeout_seconds = owner_timeout_seconds

    def snapshot(self, request_id: str) -> JSON:
        generated_at = self._clock()
        executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="platform-status")
        futures = {
            "model": executor.submit(self._model_status, request_id),
            "notification": executor.submit(self._notification_status, request_id),
            "connector": executor.submit(self._connector_status),
            "observability": executor.submit(self._observability_status, request_id),
        }
        completed, _ = wait(futures.values(), timeout=self._owner_timeout_seconds)
        owner_values: dict[str, JSON | None] = {}
        for name, future in futures.items():
            if future not in completed:
                owner_values[name] = None
                continue
            try:
                value = future.result()
            except Exception:
                value = None
            owner_values[name] = value if isinstance(value, dict) else None
        executor.shutdown(wait=False, cancel_futures=True)

        model = (
            _owner_capability(owner_values["model"])
            if owner_values["model"] is not None
            else _owner_unavailable(generated_at)
        )
        notification = (
            _owner_capability(owner_values["notification"])
            if owner_values["notification"] is not None
            else _owner_unavailable(generated_at)
        )
        notification["setup_decision"] = self._decisions.decision("notification")
        if notification["setup_decision"] == "skipped":
            notification["readiness"] = "skipped"
        return {
            "generated_at": generated_at,
            "capabilities": {
                "model": model,
                "notification": notification,
                "connector": _connector_capability(owner_values["connector"], generated_at)
                if owner_values["connector"] is not None else _owner_unavailable(generated_at),
                "observability": _observability_capability(
                    owner_values["observability"], generated_at,
                ) if owner_values["observability"] is not None else _owner_unavailable(generated_at),
            },
        }

    def set_setup_decision(
        self,
        *,
        capability: str,
        decision: str,
        expected_revision: str | None,
        actor_id: str,
        reason: str,
        request_id: str,
    ) -> JSON:
        replay = self._decisions.replay(
            capability=capability,
            decision=decision,
            expected_revision=expected_revision,
            actor_id=actor_id,
            reason=reason,
            request_id=request_id,
        )
        if replay is not None:
            return replay
        try:
            owner_status = self._notification_status(request_id)
        except Exception as exc:
            raise PlatformStatusError(
                "owner_unavailable", "Notification owner is unavailable",
            ) from exc
        if owner_status.get("configuration_revision") != expected_revision:
            raise PlatformStatusError("stale_configuration", "Notification configuration has changed")
        if decision == "skipped" and owner_status.get("readiness") == "ready":
            raise PlatformStatusError("capability_already_ready", "ready capability cannot be skipped")
        return self._decisions.set(
            capability=capability,
            decision=decision,
            expected_revision=expected_revision,
            actor_id=actor_id,
            reason=reason,
            request_id=request_id,
        )

def _owner_capability(value: JSON) -> JSON:
    readiness = value.get("readiness")
    configuration = value.get("configuration")
    revision = value.get("configuration_revision")
    return {
        "readiness": readiness
        if isinstance(readiness, str) and readiness in {"ready", "not_ready"}
        else "not_ready",
        "configuration": configuration
        if isinstance(configuration, str) and configuration in {"absent", "present"}
        else "absent",
        "configuration_revision": revision if isinstance(revision, str) else None,
        "setup_decision": "active",
        "verification": _bounded_verification(value.get("verification")),
        "availability": _bounded_availability(value.get("availability")),
    }


def _owner_unavailable(observed_at: float) -> JSON:
    return {
        "readiness": "not_ready",
        "configuration": "absent",
        "configuration_revision": None,
        "setup_decision": "active",
        "verification": _verification("not_applicable", reason_code="owner_unavailable"),
        "availability": _availability(
            "unavailable", observed_at=observed_at, reason_code="owner_unavailable",
        ),
    }


def _bounded_verification(value: object) -> JSON:
    item = value if isinstance(value, dict) else {}
    state = item.get("state")
    operation_id = item.get("operation_id")
    revision = item.get("revision")
    checked_at = item.get("checked_at")
    return {
        "operation_id": operation_id if isinstance(operation_id, str) else None,
        "state": state
        if isinstance(state, str) and state in _VERIFICATION_STATES
        else "not_applicable",
        "revision": revision if isinstance(revision, str) else None,
        "checked_at": _bounded_timestamp(checked_at),
        "reason_code": _bounded_reason(item.get("reason_code")),
    }


def _bounded_availability(value: object) -> JSON:
    item = value if isinstance(value, dict) else {}
    state = item.get("state")
    observed_at = item.get("observed_at")
    return {
        "state": state
        if isinstance(state, str) and state in _AVAILABILITY_STATES
        else "unavailable",
        "observed_at": _bounded_timestamp(observed_at),
        "reason_code": _bounded_reason(item.get("reason_code")),
    }


def _bounded_reason(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in _SAFE_REASON_CODES else "owner_unavailable"


def _bounded_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _connector_capability(value: JSON, observed_at: float) -> JSON:
    enrollments = value.get("connector_enrollments")
    clusters = value.get("clusters")
    enrollment_items = [item for item in enrollments if isinstance(item, dict)] \
        if isinstance(enrollments, list) else []
    cluster_items = [item for item in clusters if isinstance(item, dict)] \
        if isinstance(clusters, list) else []
    configured = bool(enrollment_items)
    cluster_by_id = {
        item.get("cluster_id"): item
        for item in cluster_items
        if isinstance(item.get("cluster_id"), str)
    }
    connection_states = []
    connection_by_cluster: dict[str, str] = {}
    for item in enrollment_items:
        cluster_id = item.get("cluster_id")
        cluster = cluster_by_id.get(cluster_id) if isinstance(cluster_id, str) else None
        connection_state = _connector_state(item, cluster)
        connection_states.append(connection_state)
        if isinstance(cluster_id, str):
            connection_by_cluster[cluster_id] = connection_state
    online_connections = connection_states.count("online")
    ready_clusters = [
        item for item in cluster_items
        if isinstance(item.get("cluster_id"), str)
        and connection_by_cluster.get(item["cluster_id"]) == "online"
        and item.get("runtime_status") == "online"
        and isinstance(item.get("read_verification"), dict)
        and item["read_verification"].get("status") == "verified"
    ]
    verification_records = [
        item["read_verification"] for item in cluster_items
        if isinstance(item, dict) and isinstance(item.get("read_verification"), dict)
    ]
    checked_at = max(
        (timestamp for record in verification_records
         if (timestamp := _bounded_timestamp(record.get("checked_at"))) is not None),
        default=None,
    )
    if ready_clusters:
        verification_state = "verified"
        verification_reason = None
        if online_connections == len(connection_states):
            availability_state = "available"
            availability_reason = None
        else:
            availability_state = "degraded"
            availability_reason = "connector_degraded"
    elif configured:
        verification_state = "verified" if any(
            record.get("status") == "verified" for record in verification_records
        ) else "failed" if any(
            record.get("status") == "failed" for record in verification_records
        ) else "verifying" if any(
            record.get("status") == "verifying" for record in verification_records
        ) else "unverified"
        availability_state = "unavailable"
        verification_reason = "test_required" if verification_state != "verified" else None
        availability_reason = _connector_unavailable_reason(connection_states)
    else:
        verification_state = "not_applicable"
        availability_state = "unavailable"
        verification_reason = "not_configured"
        availability_reason = "not_configured"
    return {
        "readiness": "ready" if ready_clusters else "not_ready",
        "configuration": "present" if configured else "absent",
        "configuration_revision": None,
        "setup_decision": "active",
        "verification": _verification(
            verification_state, checked_at=checked_at, reason_code=verification_reason,
        ),
        "availability": _availability(
            availability_state, observed_at=observed_at, reason_code=availability_reason,
        ),
        "connection": {
            "states": sorted(set(connection_states)),
            "total": len(connection_states),
            "online": online_connections,
        },
    }


def _connector_state(enrollment: JSON, cluster: JSON | None) -> str:
    state = enrollment.get("state")
    if not isinstance(state, str) or state not in _CONNECTOR_STATES:
        return "offline"
    runtime = cluster.get("runtime_status") if cluster is not None else None
    if state == "online":
        if cluster is None or not isinstance(runtime, str):
            return "offline"
        if runtime in {"offline", "degraded"}:
            return runtime
    return state


def _connector_unavailable_reason(states: list[str]) -> str:
    priorities = (
        ("rotation_pending", "connector_rotation_pending"),
        ("pending_registration", "connector_pending_registration"),
        ("degraded", "connector_degraded"),
        ("offline", "connector_offline"),
        ("disabled", "connector_disabled"),
    )
    return next((reason for state, reason in priorities if state in states), "test_required")


def _observability_capability(value: JSON, observed_at: float) -> JSON:
    probes = [value.get("prometheus"), value.get("loki")]
    configured = value.get("configuration", "present") == "present"
    available = all(
        isinstance(probe, dict) and probe.get("state") == "available" for probe in probes
    )
    seen_at = max(
        (timestamp for probe in probes
         if isinstance(probe, dict)
         and (timestamp := _bounded_timestamp(probe.get("observed_at"))) is not None),
        default=observed_at,
    )
    reason_code = None if available else "owner_unavailable" if configured else "not_configured"
    return {
        "readiness": "ready" if available else "not_ready",
        "configuration": "present" if configured else "absent",
        "configuration_revision": None,
        "setup_decision": "active",
        "verification": _verification(
            "verified" if available else "failed" if configured else "not_applicable",
            checked_at=seen_at if configured else None,
            reason_code=reason_code,
        ),
        "availability": _availability(
            "available" if available else "unavailable",
            observed_at=seen_at,
            reason_code=reason_code,
        ),
    }


def _verification(
    state: str,
    *,
    checked_at: float | None = None,
    reason_code: str | None = None,
) -> JSON:
    return {
        "operation_id": None,
        "state": state,
        "revision": None,
        "checked_at": checked_at,
        "reason_code": reason_code,
    }


def _availability(
    state: str,
    *,
    observed_at: float | None = None,
    reason_code: str | None = None,
) -> JSON:
    return {"state": state, "observed_at": observed_at, "reason_code": reason_code}
