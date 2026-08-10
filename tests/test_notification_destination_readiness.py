from __future__ import annotations

import base64
from pathlib import Path

import pytest

from notification_service.configuration import NotificationConfiguration, NotificationConfigurationError
from notification_service.delivery_sender import send_delivery
from notification_service.noise_controls import NotificationNoiseControls
from notification_service.requests import NotificationRequestError, NotificationRequestLifecycle


def _configuration(tmp_path: Path, *, clock=lambda: 1_700_000_000.0) -> NotificationConfiguration:
    key = tmp_path / "key"
    key.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    db_path = tmp_path / "notification.db"
    return NotificationConfiguration(
        db_path,
        key,
        NotificationNoiseControls(db_path, clock=clock),
        clock=clock,
    )


def _create(configuration: NotificationConfiguration) -> dict[str, object]:
    return configuration.create_destination(
        {
            "name": "Pilot Feishu",
            "provider": "feishu",
            "config": {
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token",
            },
        }
    )


def test_new_destination_exposes_opaque_unverified_revision(tmp_path: Path) -> None:
    destination = _create(_configuration(tmp_path))

    revision = destination["configuration_revision"]
    assert isinstance(revision, str)
    assert revision.startswith("notification-destination-revision:")
    assert destination["readiness"] == "not_ready"
    assert destination["verification"] == {
        "operation_id": None,
        "state": "unverified",
        "revision": revision,
        "checked_at": None,
        "reason_code": "test_required",
    }
    assert destination["availability"] == {
        "state": "unavailable",
        "observed_at": None,
        "reason_code": "test_required",
    }
    assert destination["pilot_route_selected"] is False


def test_sent_durable_test_verifies_exact_revision_without_selecting_route(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    destination = _create(configuration)
    destination_id = str(destination["id"])
    revision = str(destination["configuration_revision"])
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: 1_700_000_000.0)

    started = store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-1",
    )

    assert started == {
        "operation_id": "notification-delivery:test-1",
        "delivery_id": "notification-delivery:test-1",
        "revision": revision,
        "state": "verifying",
    }
    assert configuration.get_destination(destination_id)["verification"]["state"] == "verifying"

    sent: list[dict[str, object]] = []
    assert store.run_delivery_once(
        lambda payload: sent.append(payload) or {"ok": True, "message_id": "provider-message-1"}
    )

    detail = configuration.get_destination(destination_id)
    assert detail["verification"] == {
        "operation_id": "notification-delivery:test-1",
        "state": "verified",
        "revision": revision,
        "checked_at": 1_700_000_000.0,
        "reason_code": None,
    }
    assert detail["availability"] == {
        "state": "available",
        "observed_at": 1_700_000_000.0,
        "reason_code": None,
    }
    assert detail["pilot_route_selected"] is False
    assert detail["readiness"] == "not_ready"
    assert sent[0]["title"] == "[TEST] AIOps Notification Destination"
    assert "notification-delivery:test-1" in str(sent[0]["body"])
    result = store.list_delivery_results()[0]
    assert result["is_test"] is True
    assert result["destination_revision"] == revision


def test_configuration_change_stales_verification_and_pauses_pending_delivery(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    destination = _create(configuration)
    destination_id = str(destination["id"])
    revision = str(destination["configuration_revision"])
    store = NotificationRequestLifecycle(
        tmp_path / "notification.db",
        clock=lambda: 1_700_000_000.0,
        router=lambda _payload: {
            "route_id": None,
            "destination_ids": [destination_id],
            "suppressed_reason": None,
            "deliveries": [{
                "destination_id": destination_id,
                "destination_revision": revision,
                "template_id": None,
                "template_version": None,
                "presentation": None,
            }],
        },
    )
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-before-change",
    )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "provider-message-1"})
    store.accept(
        {
            "event_id": "connector.offline:pending:1",
            "event_type": "connector.offline",
            "occurred_at": 1_700_000_000,
            "severity": "warning",
            "subject": {"type": "connector", "id": "connector-1", "version": 1},
            "scope": {"environment": "prod"},
            "summary": "Connector is offline",
            "facts": {"connector_id": "connector-1", "cluster_id": "cluster-1", "status": "offline"},
            "console_path": "/admin",
        }
    )

    updated = configuration.update_destination(
        destination_id,
        {
            "config": {
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/repaired-token",
            },
            "expected_revision": revision,
            "operation_id": "notification-destination-update:1",
        },
    )

    new_revision = str(updated["configuration_revision"])
    assert new_revision != revision
    assert updated["verification"] == {
        "operation_id": "notification-delivery:test-before-change",
        "state": "stale",
        "revision": revision,
        "checked_at": 1_700_000_000.0,
        "reason_code": "configuration_changed",
    }
    assert updated["availability"] == {
        "state": "unavailable",
        "observed_at": 1_700_000_000.0,
        "reason_code": "configuration_changed",
    }
    pending = next(item for item in store.list_delivery_results() if item["event_id"] == "connector.offline:pending:1")
    assert pending["attempt_count"] == 0
    assert pending["paused_reason"] == "configuration_changed"
    assert store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"}) is False
    with pytest.raises(NotificationConfigurationError, match="revision has changed"):
        configuration.update_destination(
            destination_id,
            {
                "name": "Stale browser edit",
                "expected_revision": revision,
                "operation_id": "notification-destination-update:stale",
            },
        )


def test_credential_rejection_pauses_and_successful_retest_resumes_pending_delivery(tmp_path: Path) -> None:
    now = [1_700_000_000.0]
    clock = lambda: now[0]
    configuration = _configuration(tmp_path, clock=clock)
    destination = _create(configuration)
    destination_id = str(destination["id"])
    revision = str(destination["configuration_revision"])
    store = NotificationRequestLifecycle(
        tmp_path / "notification.db",
        clock=clock,
        router=lambda _payload: {
            "route_id": None,
            "destination_ids": [destination_id],
            "suppressed_reason": None,
            "deliveries": [{
                "destination_id": destination_id,
                "destination_revision": revision,
                "template_id": None,
                "template_version": None,
                "presentation": {"title": "Original incident", "body": "Frozen original body"},
            }],
        },
    )
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-rejected",
    )
    now[0] += 1
    store.accept(
        {
            "event_id": "connector.offline:resume:1",
            "event_type": "connector.offline",
            "occurred_at": now[0],
            "severity": "warning",
            "subject": {"type": "connector", "id": "connector-1", "version": 1},
            "scope": {"environment": "prod"},
            "summary": "Connector is offline",
            "facts": {"connector_id": "connector-1", "cluster_id": "cluster-1", "status": "offline"},
            "console_path": "/admin",
        }
    )

    store.run_delivery_once(lambda _payload: {
        "ok": False,
        "retryable": False,
        "reason_code": "authentication_failed",
        "error": "raw credential rejection from provider",
    })

    failed = configuration.get_destination(destination_id)
    assert failed["verification"]["state"] == "failed"
    assert failed["verification"]["reason_code"] == "authentication_failed"
    assert failed["availability"]["state"] == "unavailable"
    pending = next(item for item in store.list_delivery_results() if item["event_id"] == "connector.offline:resume:1")
    assert pending["attempt_count"] == 0
    assert pending["paused_reason"] == "authentication_failed"
    failed_test = next(item for item in store.list_delivery_results() if item["is_test"])
    assert failed_test["last_reason_code"] == "authentication_failed"
    assert "raw credential" not in str(failed_test)

    now[0] += 1
    repaired = configuration.update_destination(
        destination_id,
        {
            "config": {
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/repaired-token",
            },
            "expected_revision": revision,
            "operation_id": "notification-destination-update:repair",
        },
    )
    repaired_revision = str(repaired["configuration_revision"])
    now[0] += 1
    store.accept_test(
        destination_id,
        expected_revision=repaired_revision,
        operation_id="notification-delivery:test-repaired",
    )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "provider-message-2"})

    resumed = next(item for item in store.list_delivery_results() if item["event_id"] == "connector.offline:resume:1")
    assert resumed["status"] == "pending"
    assert resumed["attempt_count"] == 0
    assert resumed["paused_reason"] is None
    assert resumed["destination_revision"] == repaired_revision
    delivered: list[dict[str, object]] = []
    store.run_delivery_once(lambda payload: delivered.append(payload) or {"ok": True, "message_id": "test-message"})
    assert delivered == [{
        "destination": destination_id,
        "event_id": "connector.offline:resume:1",
        "title": "Original incident",
        "body": "Frozen original body",
        "destination_revision": repaired_revision,
        "is_test": False,
    }]


def test_verified_destination_requires_explicit_exact_pilot_route_selection(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    destination = _create(configuration)
    destination_id = str(destination["id"])
    revision = str(destination["configuration_revision"])
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: 1_700_000_000.0)
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-select",
    )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})

    selected = configuration.select_pilot_route(
        destination_id,
        expected_revision=revision,
        operation_id="notification-pilot-route:select-1",
    )
    replayed = configuration.select_pilot_route(
        destination_id,
        expected_revision=revision,
        operation_id="notification-pilot-route:select-1",
    )

    assert selected["readiness"] == "ready"
    assert selected["pilot_route_selected"] is True
    assert selected["enabled"] is True
    assert replayed == selected
    with pytest.raises(NotificationRequestError, match="operation_id conflict"):
        store.accept_test(
            destination_id,
            expected_revision=revision,
            operation_id="notification-pilot-route:select-1",
        )
    pilot_route = configuration.get_route("route:pilot-catch-all")
    assert pilot_route["enabled"] is True
    assert pilot_route["match"] == {}
    assert pilot_route["destination_ids"] == [destination_id]
    assert pilot_route["selected_destination_revision"] == revision
    routed = configuration.route(
        {
            "event_id": "connector.recovered:pilot-route:1",
            "event_type": "connector.recovered",
            "occurred_at": 1_700_000_000,
            "severity": "info",
            "subject": {"type": "connector", "id": "connector-1", "version": 1},
            "scope": {"environment": "prod"},
            "summary": "Connector recovered",
            "facts": {"connector_id": "connector-1", "cluster_id": "cluster-1", "status": "recovered"},
            "console_path": "/admin",
        }
    )
    assert routed["route_id"] == "route:pilot-catch-all"
    assert routed["deliveries"][0]["destination_revision"] == revision
    with pytest.raises(NotificationConfigurationError, match="revision has changed"):
        configuration.select_pilot_route(
            destination_id,
            expected_revision="notification-destination-revision:stale",
            operation_id="notification-pilot-route:stale",
        )


def test_transient_test_failure_retries_and_recovers_after_store_restart(tmp_path: Path) -> None:
    now = [1_700_000_000.0]
    configuration = _configuration(tmp_path, clock=lambda: now[0])
    destination = _create(configuration)
    destination_id = str(destination["id"])
    revision = str(destination["configuration_revision"])
    first_store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: now[0])
    started = first_store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-restart",
    )
    replayed = first_store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-restart",
    )
    first_store.run_delivery_once(lambda _payload: {
        "ok": False,
        "retryable": True,
        "retry_after": 45,
        "reason_code": "rate_limited",
        "error": "raw provider response",
    })

    degraded = configuration.get_destination(destination_id)
    delivery = first_store.list_delivery_results()[0]
    assert replayed == started
    assert degraded["verification"]["state"] == "verifying"
    assert degraded["availability"] == {
        "state": "degraded",
        "observed_at": 1_700_000_000.0,
        "reason_code": "rate_limited",
    }
    assert delivery["status"] == "failed"
    assert delivery["attempt_count"] == 1
    assert delivery["next_attempt_at"] == 1_700_000_045.0

    now[0] += 45
    restarted_store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: now[0])
    restarted_configuration = _configuration(tmp_path, clock=lambda: now[0])
    assert restarted_store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})
    assert restarted_configuration.get_destination(destination_id)["verification"]["state"] == "verified"
    assert restarted_configuration.get_destination(destination_id)["availability"]["state"] == "available"


def test_verified_test_delivery_is_not_removed_by_normal_terminal_retention(tmp_path: Path) -> None:
    now = [1_700_000_000.0]
    configuration = _configuration(tmp_path, clock=lambda: now[0])
    destination = _create(configuration)
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: now[0])
    store.accept_test(
        str(destination["id"]),
        expected_revision=str(destination["configuration_revision"]),
        operation_id="notification-delivery:test-retained",
    )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})

    now[0] += 91 * 24 * 60 * 60

    assert store.cleanup_expired() == 0
    assert configuration.get_destination(str(destination["id"]))["verification"]["state"] == "verified"


def test_terminal_retention_keeps_only_latest_test_for_each_revision(tmp_path: Path) -> None:
    now = [1_700_000_000.0]
    configuration = _configuration(tmp_path, clock=lambda: now[0])
    destination = _create(configuration)
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: now[0])
    destination_id = str(destination["id"])
    revision = str(destination["configuration_revision"])
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-old",
    )
    store.run_delivery_once(lambda _payload: {
        "ok": False,
        "retryable": False,
        "reason_code": "provider_rejected",
    })
    now[0] += 1
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-latest",
    )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})
    now[0] += 91 * 24 * 60 * 60

    assert "aiops_notification_cleanup_eligible 1" in store.metrics()
    assert store.cleanup_expired() == 1
    with pytest.raises(NotificationRequestError, match="delivery results not found"):
        store.get_delivery_results("notification-delivery:test-old")
    verification = configuration.get_destination(destination_id)["verification"]
    assert verification["operation_id"] == "notification-delivery:test-latest"
    assert verification["state"] == "verified"
    assert "aiops_notification_cleanup_eligible 0" in store.metrics()


def test_latest_test_must_be_sent_before_destination_can_be_selected(tmp_path: Path) -> None:
    now = [1_700_000_000.0]
    configuration = _configuration(tmp_path, clock=lambda: now[0])
    destination = _create(configuration)
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: now[0])
    destination_id = str(destination["id"])
    revision = str(destination["configuration_revision"])
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-older-retry",
    )
    store.run_delivery_once(lambda _payload: {
        "ok": False,
        "retryable": True,
        "reason_code": "provider_unavailable",
    })
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-earlier-sent",
    )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-latest-pending",
    )

    with pytest.raises(NotificationConfigurationError, match="must be verified"):
        configuration.select_pilot_route(
            destination_id,
            expected_revision=revision,
            operation_id="notification-pilot-route:latest-pending",
        )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})
    now[0] += 2
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})

    verification = configuration.get_destination(destination_id)["verification"]
    assert verification["operation_id"] == "notification-delivery:test-latest-pending"
    assert verification["state"] == "verified"


def test_successful_retest_does_not_rebind_unfinished_test_from_old_revision(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    destination = _create(configuration)
    destination_id = str(destination["id"])
    old_revision = str(destination["configuration_revision"])
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: 1_700_000_000.0)
    old_operation = "notification-delivery:test-old-unfinished"
    store.accept_test(destination_id, expected_revision=old_revision, operation_id=old_operation)
    store.run_delivery_once(lambda _payload: {
        "ok": False, "retryable": True, "reason_code": "provider_unavailable",
    })
    repaired = configuration.update_destination(destination_id, {
        "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/new-revision"},
        "expected_revision": old_revision,
        "operation_id": "notification-destination-update:test-old-unfinished",
    })
    new_revision = str(repaired["configuration_revision"])
    store.accept_test(
        destination_id,
        expected_revision=new_revision,
        operation_id="notification-delivery:test-new-sent",
    )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})

    old = store.get_delivery_results(old_operation)[0]
    assert old["destination_revision"] == old_revision
    assert old["paused_reason"] == "configuration_changed"
    assert store.accept_test(
        destination_id, expected_revision=old_revision, operation_id=old_operation,
    )["revision"] == old_revision


def test_digest_claim_excludes_delivery_paused_on_older_revision(tmp_path: Path) -> None:
    now = [1_700_000_000.0]
    configuration = _configuration(tmp_path, clock=lambda: now[0])
    destination = _create(configuration)
    destination_id = str(destination["id"])
    revisions = [str(destination["configuration_revision"])]

    def route(_payload: dict[str, object]) -> dict[str, object]:
        return {
            "route_id": None,
            "destination_ids": [destination_id],
            "suppressed_reason": None,
            "deliveries": [{
                "destination_id": destination_id,
                "destination_revision": revisions[0],
                "template_id": None,
                "template_version": None,
                "presentation": {"title": "Frozen", "body": "Frozen"},
                "noise": {"result": "digest", "next_attempt_at": now[0] + 100, "reason": "digest"},
            }],
        }

    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: now[0], router=route)
    old_request = {
        "event_id": "connector.offline:old-revision",
        "event_type": "connector.offline",
        "occurred_at": now[0],
        "severity": "warning",
        "subject": {"type": "connector", "id": "connector-old", "version": 1},
        "scope": {"environment": "prod"},
        "summary": "Old revision",
        "facts": {"connector_id": "connector-old", "cluster_id": "cluster-1", "status": "offline"},
        "console_path": "/admin",
    }
    store.accept(old_request)
    updated = configuration.update_destination(
        destination_id,
        {
            "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/revision-2"},
            "expected_revision": revisions[0],
            "operation_id": "notification-destination-update:digest",
        },
    )
    revisions[0] = str(updated["configuration_revision"])
    new_request = {
        **old_request,
        "event_id": "connector.offline:new-revision",
        "subject": {"type": "connector", "id": "connector-new", "version": 1},
        "summary": "New revision",
        "facts": {"connector_id": "connector-new", "cluster_id": "cluster-1", "status": "offline"},
    }
    store.accept(new_request)
    now[0] += 100
    sent: list[dict[str, object]] = []

    assert store.run_delivery_once(lambda payload: sent.append(payload) or {"ok": True, "message_id": "test-message"})
    assert sent[0]["event_id"] == "connector.offline:new-revision"
    assert sent[0].get("digest_count") is None
    old_delivery = next(item for item in store.list_delivery_results() if item["event_id"] == old_request["event_id"])
    assert old_delivery["attempt_count"] == 0
    assert old_delivery["paused_reason"] == "configuration_changed"


def test_public_status_does_not_switch_to_an_unselected_verified_destination(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    selected = _create(configuration)
    alternate = configuration.create_destination({
        "name": "Alternate Feishu",
        "provider": "feishu",
        "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/alternate-token"},
    })
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: 1_700_000_000.0)
    for operation, destination in (("selected", selected), ("alternate", alternate)):
        store.accept_test(
            str(destination["id"]),
            expected_revision=str(destination["configuration_revision"]),
            operation_id=f"notification-delivery:test-status-{operation}",
        )
        store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})
    configuration.select_pilot_route(
        str(selected["id"]),
        expected_revision=str(selected["configuration_revision"]),
        operation_id="notification-pilot-route:status-selected",
    )
    changed = configuration.update_destination(
        str(selected["id"]),
        {
            "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/selected-repaired"},
            "expected_revision": selected["configuration_revision"],
            "operation_id": "notification-destination-update:status-selected",
        },
    )

    status = configuration.public_status()

    assert status["configuration_revision"] == changed["configuration_revision"]
    assert status["verification"]["state"] == "stale"
    assert status["pilot_route_selected"] is True
    assert configuration.get_route("route:pilot-catch-all")["enabled"] is True

    routed_store = NotificationRequestLifecycle(
        tmp_path / "notification.db",
        clock=lambda: 1_700_000_000.0,
        router=configuration.route,
    )
    routed_store.accept({
        "event_id": "connector.offline:selected-destination-stale",
        "event_type": "connector.offline",
        "occurred_at": 1_700_000_000,
        "severity": "warning",
        "subject": {"type": "connector", "id": "connector-selected", "version": 1},
        "scope": {"environment": "prod"},
        "summary": "Selected destination is stale",
        "facts": {"connector_id": "connector-selected", "cluster_id": "cluster-1", "status": "offline"},
        "console_path": "/admin",
    })
    paused = next(
        item for item in routed_store.list_delivery_results()
        if item["event_id"] == "connector.offline:selected-destination-stale"
    )
    assert paused["destination_id"] == selected["id"]
    assert paused["destination_revision"] == changed["configuration_revision"]
    assert paused["paused_reason"] == "configuration_changed"

    routed_store.accept_test(
        str(selected["id"]),
        expected_revision=str(changed["configuration_revision"]),
        operation_id="notification-delivery:test-status-repaired",
    )
    routed_store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})
    resumed = next(
        item for item in routed_store.list_delivery_results()
        if item["event_id"] == "connector.offline:selected-destination-stale"
    )
    assert resumed["paused_reason"] is None
    assert configuration.public_status()["readiness"] == "ready"


def test_revision_change_during_claim_pauses_without_consuming_provider_attempt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    configuration = _configuration(tmp_path)
    destination = _create(configuration)
    destination_id = str(destination["id"])
    revision = str(destination["configuration_revision"])
    store = NotificationRequestLifecycle(tmp_path / "notification.db", clock=lambda: 1_700_000_000.0)
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-before-claim",
    )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})
    configuration.select_pilot_route(
        destination_id,
        expected_revision=revision,
        operation_id="notification-pilot-route:before-claim",
    )
    routed_store = NotificationRequestLifecycle(
        tmp_path / "notification.db",
        clock=lambda: 1_700_000_000.0,
        router=configuration.route,
    )
    request = {
        "event_id": "connector.offline:claimed-revision-change",
        "event_type": "connector.offline",
        "occurred_at": 1_700_000_000,
        "severity": "warning",
        "subject": {"type": "connector", "id": "connector-1", "version": 1},
        "scope": {"environment": "prod"},
        "summary": "Connector is offline",
        "facts": {"connector_id": "connector-1", "cluster_id": "cluster-1", "status": "offline"},
        "console_path": "/admin",
    }
    routed_store.accept(request)

    def change_before_send(_payload: dict[str, object]) -> dict[str, object]:
        configuration.update_destination(
            destination_id,
            {
                "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/claim-repaired"},
                "expected_revision": revision,
                "operation_id": "notification-destination-update:during-claim",
            },
        )
        return {"ok": True, "message_id": "provider-accepted-old-revision"}

    assert routed_store.run_delivery_once(change_before_send)
    delivery = next(
        item for item in routed_store.list_delivery_results()
        if item["event_id"] == request["event_id"]
    )
    assert delivery["status"] == "pending"
    assert delivery["attempt_count"] == 0
    assert delivery["attempts"] == []
    assert delivery["paused_reason"] == "configuration_changed"


def test_successful_retest_recovers_claim_paused_by_concurrent_revision_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))
    configuration = _configuration(tmp_path)
    destination = _create(configuration)
    destination_id = str(destination["id"])
    revision = str(destination["configuration_revision"])
    store = NotificationRequestLifecycle(
        tmp_path / "notification.db",
        clock=lambda: 1_700_000_000.0,
        router=configuration.route,
    )
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id="notification-delivery:test-race-initial",
    )
    store.run_delivery_once(lambda _payload: {"ok": True, "message_id": "test-message"})
    configuration.select_pilot_route(
        destination_id,
        expected_revision=revision,
        operation_id="notification-pilot-route:race-initial",
    )
    request = {
        "event_id": "connector.offline:retest-race",
        "event_type": "connector.offline",
        "occurred_at": 1_700_000_000,
        "severity": "warning",
        "subject": {"type": "connector", "id": "connector-race", "version": 1},
        "scope": {"environment": "prod"},
        "summary": "Connector is offline",
        "facts": {"connector_id": "connector-race", "cluster_id": "cluster-1", "status": "offline"},
        "console_path": "/admin",
    }
    store.accept(request)
    def repair_and_verify_while_claimed(payload: dict[str, object]) -> dict[str, object]:
        updated = configuration.update_destination(
            destination_id,
            {
                "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/race-repaired"},
                "expected_revision": revision,
                "operation_id": "notification-destination-update:race-repair",
            },
        )
        repaired_revision = str(updated["configuration_revision"])
        store.accept_test(
            destination_id,
            expected_revision=repaired_revision,
            operation_id="notification-delivery:test-race-repaired",
        )
        assert store.run_delivery_once(lambda _test_payload: {"ok": True, "message_id": "test-message"})
        return send_delivery(configuration, payload)

    assert store.run_delivery_once(repair_and_verify_while_claimed)
    delivery = next(
        item for item in store.list_delivery_results()
        if item["event_id"] == request["event_id"]
    )
    assert delivery["status"] == "pending"
    assert delivery["attempt_count"] == 0
    assert delivery["attempts"] == []
    assert delivery["paused_reason"] is None
    assert delivery["destination_revision"] == configuration.get_destination(destination_id)["configuration_revision"]
