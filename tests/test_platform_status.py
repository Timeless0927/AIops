from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway.platform_status import PlatformStatus, PlatformSetupDecisions
from apps.aiops_k8s_gateway.platform_status import PlatformStatusError
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _verification(state: str, *, revision: str | None, checked_at: float | None) -> dict[str, object]:
    return {
        "operation_id": f"verification:{revision}" if revision else None,
        "state": state,
        "revision": revision,
        "checked_at": checked_at,
        "reason_code": None,
    }


def _availability(state: str, *, observed_at: float | None) -> dict[str, object]:
    return {"state": state, "observed_at": observed_at, "reason_code": None}


def test_platform_status_projects_four_owner_capabilities_without_global_completion(
    tmp_path: Path,
) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    decisions = PlatformSetupDecisions(database, clock=lambda: 1_700_000_000.0)
    status = PlatformStatus(
        decisions,
        model_status=lambda _request_id: {
            "readiness": "ready",
            "configuration": "present",
            "configuration_revision": "model-provider:revision-1",
            "verification": _verification(
                "verified", revision="model-provider:revision-1", checked_at=1_699_999_900.0,
            ),
            "availability": _availability("available", observed_at=1_699_999_950.0),
        },
        notification_status=lambda _request_id: {
            "readiness": "not_ready",
            "configuration": "absent",
            "configuration_revision": None,
            "setup_decision": "active",
            "verification": _verification("not_applicable", revision=None, checked_at=None),
            "availability": {
                "state": "unavailable", "observed_at": None, "reason_code": "not_configured",
            },
            "pilot_route_selected": False,
        },
        connector_status=lambda: {
            "connector_enrollments": [{
                "id": "enrollment-1",
                "cluster_id": "pilot-cluster",
                "state": "online",
                "read_verification": "verified",
            }],
            "clusters": [{
                "cluster_id": "pilot-cluster",
                "runtime_status": "online",
                "read_verification": {
                    "status": "verified", "checked_at": 1_699_999_980.0, "reason_code": None,
                },
            }],
        },
        observability_status=lambda _request_id: {
            "prometheus": {"state": "available", "observed_at": 1_699_999_990.0},
            "loki": {"state": "available", "observed_at": 1_699_999_991.0},
        },
        clock=lambda: 1_700_000_000.0,
    )

    result = status.snapshot("platform-status:1")

    assert result["generated_at"] == 1_700_000_000.0
    assert list(result["capabilities"]) == ["model", "notification", "connector", "observability"]
    assert result["capabilities"]["model"]["setup_decision"] == "active"
    assert result["capabilities"]["notification"]["readiness"] == "not_ready"
    assert result["capabilities"]["connector"] == {
        "readiness": "ready",
        "configuration": "present",
        "configuration_revision": None,
        "setup_decision": "active",
        "verification": {
            "operation_id": None,
            "state": "verified",
            "revision": None,
            "checked_at": 1_699_999_980.0,
            "reason_code": None,
        },
        "availability": {
            "state": "available",
            "observed_at": 1_700_000_000.0,
            "reason_code": None,
        },
        "connection": {"states": ["online"], "total": 1, "online": 1},
    }
    assert result["capabilities"]["observability"]["readiness"] == "ready"
    serialized = json.dumps(result)
    assert "all_ready" not in serialized
    assert "setup_complete" not in serialized


def test_owner_timeout_only_degrades_its_capability(tmp_path: Path) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    release = threading.Event()
    entered = threading.Event()

    def slow_model(_request_id: str) -> dict[str, object]:
        entered.set()
        release.wait(timeout=1)
        return {}

    decisions = PlatformSetupDecisions(database)
    decisions.decision("notification")
    status = PlatformStatus(
        decisions,
        model_status=slow_model,
        notification_status=lambda _request_id: {
            "readiness": "not_ready",
            "configuration": "absent",
            "configuration_revision": None,
            "verification": _verification("not_applicable", revision=None, checked_at=None),
            "availability": {
                "state": "unavailable", "observed_at": None, "reason_code": "not_configured",
            },
        },
        connector_status=lambda: {"connector_enrollments": [], "clusters": []},
        observability_status=lambda _request_id: {
            "prometheus": {"state": "available", "observed_at": 1_700_000_000.0},
            "loki": {"state": "available", "observed_at": 1_700_000_000.0},
        },
        owner_timeout_seconds=0.02,
        clock=lambda: 1_700_000_000.0,
    )

    started = time.monotonic()
    try:
        result = status.snapshot("platform-status:timeout")
    finally:
        release.set()

    assert entered.is_set()
    assert time.monotonic() - started < 0.25
    assert result["capabilities"]["model"] == {
        "readiness": "not_ready",
        "configuration": "absent",
        "configuration_revision": None,
        "setup_decision": "active",
        "verification": {
            "operation_id": None,
            "state": "not_applicable",
            "revision": None,
            "checked_at": None,
            "reason_code": "owner_unavailable",
        },
        "availability": {
            "state": "unavailable",
            "observed_at": 1_700_000_000.0,
            "reason_code": "owner_unavailable",
        },
    }
    assert result["capabilities"]["notification"]["availability"]["reason_code"] == "not_configured"
    assert result["capabilities"]["observability"]["readiness"] == "ready"


def test_notification_skip_is_durable_and_never_projects_ready(tmp_path: Path) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    notification = {
        "readiness": "not_ready",
        "configuration": "present",
        "configuration_revision": "notification-destination-revision:1",
        "verification": _verification(
            "failed", revision="notification-destination-revision:1", checked_at=1_700_000_000.0,
        ),
        "availability": {
            "state": "unavailable",
            "observed_at": 1_700_000_000.0,
            "reason_code": "authentication_failed",
        },
    }

    def service(decisions: PlatformSetupDecisions) -> PlatformStatus:
        return PlatformStatus(
            decisions,
            model_status=lambda _request_id: {},
            notification_status=lambda _request_id: notification,
            connector_status=lambda: {"connector_enrollments": [], "clusters": []},
            observability_status=lambda _request_id: {},
            clock=lambda: 1_700_000_010.0,
        )

    first = service(PlatformSetupDecisions(database, clock=lambda: 1_700_000_005.0))
    changed = first.set_setup_decision(
        capability="notification",
        decision="skipped",
        expected_revision="notification-destination-revision:1",
        actor_id="user:admin",
        reason="notification is optional for this workspace",
        request_id="setup-decision:1",
    )
    reopened = service(PlatformSetupDecisions(database)).snapshot("platform-status:reopened")

    assert changed == {
        "capability": "notification",
        "setup_decision": "skipped",
        "decided_by": "user:admin",
        "decided_at": 1_700_000_005.0,
    }
    assert reopened["capabilities"]["notification"]["readiness"] == "skipped"
    assert reopened["capabilities"]["notification"]["setup_decision"] == "skipped"
    assert reopened["capabilities"]["notification"]["verification"]["state"] == "failed"
    assert "optional for this workspace" not in json.dumps(reopened)


def test_skip_rejects_required_or_ready_capability_and_new_configuration_resumes(
    tmp_path: Path,
) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    notification: dict[str, object] = {
        "readiness": "not_ready",
        "configuration": "absent",
        "configuration_revision": None,
        "verification": _verification("not_applicable", revision=None, checked_at=None),
        "availability": {
            "state": "unavailable", "observed_at": None, "reason_code": "not_configured",
        },
    }
    decisions = PlatformSetupDecisions(database, clock=lambda: 1_700_000_000.0)
    status = PlatformStatus(
        decisions,
        model_status=lambda _request_id: {},
        notification_status=lambda _request_id: notification,
        connector_status=lambda: {"connector_enrollments": [], "clusters": []},
        observability_status=lambda _request_id: {},
        clock=lambda: 1_700_000_001.0,
    )
    status.set_setup_decision(
        capability="notification",
        decision="skipped",
        expected_revision=None,
        actor_id="user:admin",
        reason="defer optional integration",
        request_id="setup-decision:skip",
    )

    with pytest.raises(PlatformStatusError, match="required capability") as required:
        status.set_setup_decision(
            capability="model",
            decision="skipped",
            expected_revision=None,
            actor_id="user:admin",
            reason="attempt invalid skip",
            request_id="setup-decision:model",
        )
    notification["readiness"] = "ready"
    with pytest.raises(PlatformStatusError, match="ready capability") as ready:
        status.set_setup_decision(
            capability="notification",
            decision="skipped",
            expected_revision=None,
            actor_id="user:admin",
            reason="attempt stale skip",
            request_id="setup-decision:ready",
        )

    notification.update({
        "readiness": "not_ready",
        "configuration": "present",
        "configuration_revision": "notification-destination-revision:2",
    })
    decisions.activate_after_configuration(
        actor_id="user:admin",
        reason="save repaired destination",
        request_id="notification-save:2",
    )
    resumed = status.snapshot("platform-status:resumed")

    assert required.value.code == "required_capability"
    assert ready.value.code == "capability_already_ready"
    assert resumed["capabilities"]["notification"]["setup_decision"] == "active"
    assert resumed["capabilities"]["notification"]["readiness"] == "not_ready"


def test_setup_decision_request_id_is_idempotent_without_reverting_newer_state(
    tmp_path: Path,
) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    times = iter((1_700_000_001.0, 1_700_000_002.0, 1_700_000_003.0))
    decisions = PlatformSetupDecisions(database, clock=lambda: next(times))
    first = decisions.set(
        capability="notification",
        decision="skipped",
        actor_id="user:admin",
        reason="defer notifications",
        request_id="setup-decision:idempotent",
    )
    decisions.set(
        capability="notification",
        decision="active",
        actor_id="user:admin",
        reason="resume notifications",
        request_id="setup-decision:newer",
    )

    replay = decisions.set(
        capability="notification",
        decision="skipped",
        actor_id="user:admin",
        reason="defer notifications",
        request_id="setup-decision:idempotent",
    )
    with pytest.raises(PlatformStatusError, match="different setup decision") as conflict:
        decisions.set(
            capability="notification",
            decision="active",
            actor_id="user:admin",
            reason="defer notifications",
            request_id="setup-decision:idempotent",
        )

    assert replay == first
    assert decisions.decision("notification") == "active"
    assert conflict.value.code == "idempotency_conflict"


def test_setup_decision_service_replays_before_reading_changed_owner_state(
    tmp_path: Path,
) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    owner = {
        "readiness": "not_ready",
        "configuration_revision": "notification-destination-revision:1",
    }
    decisions = PlatformSetupDecisions(database, clock=iter((1.0, 2.0)).__next__)
    status = PlatformStatus(
        decisions,
        model_status=lambda _request_id: {},
        notification_status=lambda _request_id: dict(owner),
        connector_status=lambda: {},
        observability_status=lambda _request_id: {},
    )
    first = status.set_setup_decision(
        capability="notification",
        decision="skipped",
        expected_revision="notification-destination-revision:1",
        actor_id="user:admin",
        reason="defer notifications",
        request_id="setup-decision:original",
    )
    owner["configuration_revision"] = "notification-destination-revision:2"
    status.set_setup_decision(
        capability="notification",
        decision="active",
        expected_revision="notification-destination-revision:2",
        actor_id="user:admin",
        reason="resume notifications",
        request_id="setup-decision:newer",
    )
    owner.update({
        "readiness": "ready",
        "configuration_revision": "notification-destination-revision:3",
    })

    replay = status.set_setup_decision(
        capability="notification",
        decision="skipped",
        expected_revision="notification-destination-revision:1",
        actor_id="user:admin",
        reason="defer notifications",
        request_id="setup-decision:original",
    )

    assert replay == first
    assert decisions.decision("notification") == "active"


def test_concurrent_setup_decision_duplicate_replays_instead_of_failing(tmp_path: Path) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    start = threading.Barrier(2)

    def slow_clock() -> float:
        time.sleep(0.1)
        return 1_700_000_000.0

    decisions = PlatformSetupDecisions(database, clock=slow_clock)

    def submit() -> dict[str, object]:
        start.wait(timeout=1)
        return decisions.set(
            capability="notification",
            decision="skipped",
            expected_revision="notification:1",
            actor_id="user:admin",
            reason="defer notifications",
            request_id="setup-decision:concurrent",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [executor.submit(submit), executor.submit(submit)]]

    assert results[0] == results[1]
    assert decisions.decision("notification") == "skipped"


def test_existing_platform_status_v39_database_adds_expected_revision_column(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "gateway.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
            INSERT INTO schema_migrations VALUES (39, 1);
            CREATE TABLE platform_setup_decisions (
                capability TEXT PRIMARY KEY,
                decision TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                decided_at REAL NOT NULL,
                request_id TEXT NOT NULL
            );
            CREATE TABLE platform_setup_decision_operations (
                request_id TEXT PRIMARY KEY,
                capability TEXT NOT NULL,
                decision TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                decided_at REAL NOT NULL
            );
            INSERT INTO platform_setup_decisions VALUES (
                'notification', 'skipped', 'user:admin', 'defer notifications', 1,
                'setup-decision:upgraded-v39'
            );
            INSERT INTO platform_setup_decision_operations VALUES (
                'setup-decision:upgraded-v39', 'notification', 'skipped',
                'user:admin', 'defer notifications', 1
            );
            """
        )

    decisions = PlatformSetupDecisions(GatewayV1Store(db_path).database, clock=lambda: 1.0)
    result = decisions.set(
        capability="notification",
        decision="skipped",
        expected_revision="notification:1",
        actor_id="user:admin",
        reason="defer notifications",
        request_id="setup-decision:upgraded-v39",
    )

    assert result["setup_decision"] == "skipped"


def test_configuration_save_replay_does_not_resume_a_newer_skip(tmp_path: Path) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    decisions = PlatformSetupDecisions(database, clock=iter((1.0, 2.0, 3.0)).__next__)
    decisions.set(
        capability="notification", decision="skipped", expected_revision=None,
        actor_id="user:admin", reason="initial skip", request_id="skip:initial",
    )
    first = decisions.activate_after_configuration(
        actor_id="user:admin", reason="save destination", request_id="config:save",
    )
    decisions.set(
        capability="notification", decision="skipped", expected_revision=None,
        actor_id="user:admin", reason="newer skip", request_id="skip:newer",
    )

    replay = decisions.activate_after_configuration(
        actor_id="user:admin", reason="save destination", request_id="config:save",
    )

    assert replay == first
    assert decisions.decision("notification") == "skipped"


def test_v42_backfills_v40_identity_and_configuration_operations(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)",
        )
        conn.executemany(
            "INSERT INTO schema_migrations VALUES (?, 1)",
            ((version,) for version in range(1, 41)),
        )
        conn.executescript(
            """
            CREATE TABLE platform_setup_decisions (
                capability TEXT PRIMARY KEY, decision TEXT NOT NULL, actor_id TEXT NOT NULL,
                reason TEXT NOT NULL, decided_at REAL NOT NULL, request_id TEXT NOT NULL
            );
            CREATE TABLE platform_setup_decision_operations (
                request_id TEXT PRIMARY KEY, capability TEXT NOT NULL, decision TEXT NOT NULL,
                actor_id TEXT NOT NULL, reason TEXT NOT NULL, decided_at REAL NOT NULL,
                expected_revision TEXT
            );
            CREATE TABLE admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT, target_type TEXT NOT NULL,
                target_id TEXT, action TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT,
                after_json TEXT, result TEXT NOT NULL, request_id TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            INSERT INTO platform_setup_decisions VALUES (
                'notification', 'skipped', 'user:admin', 'newer skip', 3, 'skip:newer'
            );
            INSERT INTO platform_setup_decision_operations VALUES (
                'setup:v40', 'notification', 'skipped', 'user:admin', 'v40 skip', 1,
                'notification:1'
            );
            INSERT INTO admin_audit (
                actor_id, target_type, action, reason, result, request_id, created_at
            ) VALUES (
                'user:admin', 'notification-destinations', 'notification-destinations_create',
                'legacy config save', 'success', 'config:legacy', 2
            );
            """
        )

    decisions = PlatformSetupDecisions(GatewayV1Store(db_path).database)

    with pytest.raises(PlatformStatusError) as conflict:
        decisions.set(
            capability="notification", decision="skipped", expected_revision="notification:2",
            actor_id="user:admin", reason="v40 skip", request_id="setup:v40",
        )
    replay = decisions.activate_after_configuration(
        actor_id="user:admin", reason="legacy config save", request_id="config:legacy",
    )

    assert conflict.value.code == "idempotency_conflict"
    assert replay is None
    assert decisions.decision("notification") == "skipped"


def test_offline_connector_retains_read_verification_but_is_not_ready(tmp_path: Path) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    status = PlatformStatus(
        PlatformSetupDecisions(database),
        model_status=lambda _request_id: {},
        notification_status=lambda _request_id: {},
        connector_status=lambda: {
            "connector_enrollments": [{
                "id": "enrollment-1", "state": "offline", "read_verification": "verified",
            }],
            "clusters": [{
                "cluster_id": "pilot-cluster",
                "runtime_status": "offline",
                "read_verification": {
                    "status": "verified", "checked_at": 1_700_000_000.0, "reason_code": None,
                },
            }],
        },
        observability_status=lambda _request_id: {
            "configuration": "present",
            "prometheus": {"state": "available", "observed_at": 1_700_000_000.0},
            "loki": {"state": "available", "observed_at": 1_700_000_000.0},
        },
        clock=lambda: 1_700_000_010.0,
    )

    connector = status.snapshot("platform-status:offline")["capabilities"]["connector"]

    assert connector["readiness"] == "not_ready"
    assert connector["verification"]["state"] == "verified"
    assert connector["verification"]["checked_at"] == 1_700_000_000.0
    assert connector["availability"] == {
        "state": "unavailable",
        "observed_at": 1_700_000_010.0,
        "reason_code": "connector_offline",
    }
    assert connector["connection"] == {"states": ["offline"], "total": 1, "online": 0}


def test_connector_connection_projection_distinguishes_degraded_and_rotation_states(
    tmp_path: Path,
) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    owner = {
        "connector_enrollments": [
            {"cluster_id": "online", "state": "online", "read_verification": "verified"},
            {"cluster_id": "offline", "state": "offline", "read_verification": "verified"},
            {
                "cluster_id": "rotating",
                "state": "rotation_pending",
                "read_verification": "verified",
            },
        ],
        "clusters": [
            {
                "cluster_id": "online", "runtime_status": "online",
                "read_verification": {"status": "verified", "checked_at": 10.0},
            },
            {
                "cluster_id": "offline", "runtime_status": "offline",
                "read_verification": {"status": "verified", "checked_at": 11.0},
            },
            {
                "cluster_id": "rotating", "runtime_status": "online",
                "read_verification": {"status": "verified", "checked_at": 12.0},
            },
        ],
    }
    status = PlatformStatus(
        PlatformSetupDecisions(database),
        model_status=lambda _request_id: {},
        notification_status=lambda _request_id: {},
        connector_status=lambda: owner,
        observability_status=lambda _request_id: {},
        clock=lambda: 20.0,
    )

    connector = status.snapshot("platform-status:connections")["capabilities"]["connector"]

    assert connector["readiness"] == "ready"
    assert connector["availability"] == {
        "state": "degraded", "observed_at": 20.0, "reason_code": "connector_degraded",
    }
    assert connector["connection"] == {
        "states": ["offline", "online", "rotation_pending"], "total": 3, "online": 1,
    }


@pytest.mark.parametrize(
    ("connection_state", "reason_code"),
    [
        ("disabled", "connector_disabled"),
        ("rotation_pending", "connector_rotation_pending"),
    ],
)
def test_non_online_connector_enrollment_cannot_reuse_historical_ready_cluster(
    tmp_path: Path,
    connection_state: str,
    reason_code: str,
) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    status = PlatformStatus(
        PlatformSetupDecisions(database),
        model_status=lambda _request_id: {},
        notification_status=lambda _request_id: {},
        connector_status=lambda: {
            "connector_enrollments": [{
                "cluster_id": "pilot", "state": connection_state,
                "read_verification": "verified",
            }],
            "clusters": [{
                "cluster_id": "pilot", "runtime_status": "online",
                "read_verification": {"status": "verified", "checked_at": 10.0},
            }],
        },
        observability_status=lambda _request_id: {},
        clock=lambda: 20.0,
    )

    connector = status.snapshot("platform-status:inactive")["capabilities"]["connector"]

    assert connector["readiness"] == "not_ready"
    assert connector["availability"] == {
        "state": "unavailable", "observed_at": 20.0, "reason_code": reason_code,
    }


def test_observability_without_both_owner_urls_is_not_configured(tmp_path: Path) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    status = PlatformStatus(
        PlatformSetupDecisions(database),
        model_status=lambda _request_id: {},
        notification_status=lambda _request_id: {},
        connector_status=lambda: {"connector_enrollments": [], "clusters": []},
        observability_status=lambda _request_id: {
            "configuration": "absent",
            "prometheus": {"state": "unavailable", "observed_at": None},
            "loki": {"state": "unavailable", "observed_at": None},
        },
        clock=lambda: 1_700_000_000.0,
    )

    observability = status.snapshot("platform-status:not-configured")["capabilities"]["observability"]

    assert observability["readiness"] == "not_ready"
    assert observability["configuration"] == "absent"
    assert observability["verification"]["state"] == "not_applicable"
    assert observability["availability"]["reason_code"] == "not_configured"


def test_malformed_owner_fields_degrade_without_breaking_other_capabilities(tmp_path: Path) -> None:
    database = GatewayV1Store(tmp_path / "gateway.db").database
    status = PlatformStatus(
        PlatformSetupDecisions(database),
        model_status=lambda _request_id: {
            "readiness": [],
            "configuration": {},
            "verification": {
                "state": [], "checked_at": True, "reason_code": {"raw": "secret"},
            },
            "availability": {
                "state": {}, "observed_at": float("inf"), "reason_code": ["raw-error"],
            },
        },
        notification_status=lambda _request_id: {},
        connector_status=lambda: {
            "connector_enrollments": [
                {"cluster_id": [], "state": "online", "read_verification": "verified"},
                {"cluster_id": "cluster-2", "state": "online", "read_verification": "verified"},
            ],
            "clusters": [{
                "cluster_id": "cluster-2",
                "runtime_status": {},
                "read_verification": {
                    "status": "verified", "checked_at": float("inf"), "reason_code": None,
                },
            }],
        },
        observability_status=lambda _request_id: {
            "configuration": "present",
            "prometheus": {"state": "available", "observed_at": True},
            "loki": {"state": "available", "observed_at": float("inf")},
        },
        clock=lambda: 1_700_000_000.0,
    )

    result = status.snapshot("platform-status:malformed")

    assert result["capabilities"]["model"] == {
        "readiness": "not_ready",
        "configuration": "absent",
        "configuration_revision": None,
        "setup_decision": "active",
        "verification": {
            "operation_id": None, "state": "not_applicable", "revision": None,
            "checked_at": None, "reason_code": "owner_unavailable",
        },
        "availability": {
            "state": "unavailable", "observed_at": None, "reason_code": "owner_unavailable",
        },
    }
    assert result["capabilities"]["connector"]["connection"] == {
        "states": ["offline"], "total": 2, "online": 0,
    }
    assert result["capabilities"]["observability"]["availability"] == {
        "state": "available", "observed_at": 1_700_000_000.0, "reason_code": None,
    }
    assert result["capabilities"]["observability"]["readiness"] == "ready"
