from __future__ import annotations

from pathlib import Path

import pytest

from aiops.contracts.notification import notification_request
from notification_service.configuration import NotificationConfiguration
from notification_service.noise_controls import NotificationNoiseControlError, NotificationNoiseControls
from notification_service.requests import NotificationStore


def _request(*, event_id: str = "incident.opened:incident-1:1", severity: str = "warning") -> dict[str, object]:
    return notification_request(
        event_id=event_id,
        event_type="incident.opened",
        occurred_at=1_700_000_000,
        severity=severity,
        subject={"type": "incident", "id": "incident-1", "version": 1},
        scope={"environment": "prod", "team_id": "team-payments", "service_id": "service-checkout"},
        summary="Checkout is unavailable",
        facts={"incident_id": "incident-1", "status": "opened"},
        console_path="/incidents/incident-1",
    )


def _configuration(tmp_path: Path, now: list[float]) -> tuple[NotificationConfiguration, NotificationNoiseControls, str]:
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    noise = NotificationNoiseControls(tmp_path / "notification.db", clock=lambda: now[0])
    configuration = NotificationConfiguration(tmp_path / "notification.db", key, noise, clock=lambda: now[0], send=lambda *_args: True)
    destination = configuration.create_destination(
        {"name": "Feishu", "provider": "feishu", "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"}}
    )
    return configuration, noise, str(destination["id"])


def test_destination_noise_control_defers_lower_severity_and_critical_bypasses(tmp_path: Path) -> None:
    now = [1_704_153_600.0]  # 2024-01-02 00:00:00 UTC, 08:00 Asia/Shanghai
    configuration, noise, destination_id = _configuration(tmp_path, now)
    policy = noise.update_destination(
        destination_id,
        {"timezone": "Asia/Shanghai", "quiet_hours": {"start": "07:30", "end": "09:00"}, "hourly_limit": 5, "digest_interval_seconds": 900},
    )

    deferred = noise.evaluate(destination_id, _request())
    critical = noise.evaluate(destination_id, _request(severity="critical"))

    assert policy == {
        "timezone": "Asia/Shanghai",
        "quiet_hours": {"start": "07:30", "end": "09:00"},
        "hourly_limit": 5,
        "digest_interval_seconds": 900,
    }
    assert deferred == {"result": "quiet_hours", "next_attempt_at": 1_704_157_200.0, "reason": "quiet hours until 2024-01-02T09:00:00+08:00"}
    assert critical == {"result": "immediate", "next_attempt_at": now[0], "reason": None}


def test_digest_and_hourly_limit_results_are_deterministic(tmp_path: Path) -> None:
    now = [1_704_153_601.0]
    configuration, noise, destination_id = _configuration(tmp_path, now)
    noise.update_destination(destination_id, {"timezone": "UTC", "quiet_hours": None, "hourly_limit": None, "digest_interval_seconds": 900})

    assert noise.evaluate(destination_id, _request())["next_attempt_at"] == 1_704_154_500.0
    assert noise.evaluate(destination_id, _request())["result"] == "digest"

    noise.update_destination(destination_id, {"hourly_limit": 1, "digest_interval_seconds": None})
    now[0] -= 10
    store = NotificationStore(
        tmp_path / "notification.db",
        clock=lambda: now[0],
        router=lambda _request: {"route_id": None, "destination_ids": [destination_id], "suppressed_reason": None},
    )
    store.accept(_request())
    store.run_delivery_once(lambda _payload: {"ok": True})
    now[0] += 10
    limited = noise.evaluate(destination_id, _request(event_id="incident.opened:incident-2:1"), 1, now[0] - 10)
    assert limited == {"result": "hourly_limit", "next_attempt_at": now[0] + 3590, "reason": "hourly limit 1 reached"}


def test_scoped_bounded_silence_can_suppress_critical(tmp_path: Path) -> None:
    now = [1_704_153_600.0]
    _configuration_owner, noise, destination_id = _configuration(tmp_path, now)
    silence = noise.create_silence(
        {
            "match": {"event": ["incident.opened"], "environment": ["prod"], "team": ["team-payments"]},
            "reason": "planned payment maintenance",
            "expires_at": now[0] + 3600,
        }
    )

    result = noise.evaluate(destination_id, _request(severity="critical"))

    assert result == {"result": "silence", "next_attempt_at": None, "reason": "planned payment maintenance", "silence_id": silence["id"]}
    assert noise.list_silences()[0]["active"] is True
    with pytest.raises(NotificationNoiseControlError, match="30 days"):
        noise.create_silence({"match": {"event": ["incident.opened"]}, "reason": "too long", "expires_at": now[0] + 31 * 86400})


def test_route_persists_noise_result_on_the_destination_delivery(tmp_path: Path) -> None:
    now = [1_704_153_600.0]
    configuration, noise, destination_id = _configuration(tmp_path, now)
    configuration.test_destination(destination_id)
    configuration.update_destination(destination_id, {"enabled": True})
    noise.update_destination(
        destination_id,
        {"timezone": "Asia/Shanghai", "quiet_hours": {"start": "07:30", "end": "09:00"}},
    )
    configuration.create_route(
        {"name": "Production", "priority": 1, "enabled": True, "match": {"environment": ["prod"]}, "destination_ids": [destination_id]}
    )
    store = NotificationStore(tmp_path / "notification.db", clock=lambda: now[0], router=configuration.route, noise_evaluator=noise.evaluate)

    store.accept(_request())

    delivery = store.list_deliveries(str(_request()["event_id"]))[0]
    assert delivery["status"] == "pending"
    assert delivery["noise_result"] == "quiet_hours"


def test_hourly_limit_rechecks_quota_before_releasing_each_deferred_delivery(tmp_path: Path) -> None:
    now = [1_704_153_600.0]
    configuration, noise, destination_id = _configuration(tmp_path, now)
    configuration.test_destination(destination_id)
    configuration.update_destination(destination_id, {"enabled": True})
    noise.update_destination(destination_id, {"hourly_limit": 1})
    configuration.create_route(
        {"name": "Production", "priority": 1, "enabled": True, "match": {"environment": ["prod"]}, "destination_ids": [destination_id]}
    )
    store = NotificationStore(tmp_path / "notification.db", clock=lambda: now[0], router=configuration.route, noise_evaluator=noise.evaluate)
    sent: list[str] = []
    first = _request(event_id="incident.opened:incident-1:1")
    second = _request(event_id="incident.opened:incident-2:1")
    third = _request(event_id="incident.opened:incident-3:1")
    store.accept(first)
    store.run_delivery_once(lambda payload: sent.append(str(payload["event_id"])) or {"ok": True})
    store.accept(second)
    store.accept(third)

    now[0] += 3600
    store.run_delivery_once(lambda payload: sent.append(str(payload["event_id"])) or {"ok": True})
    store.run_delivery_once(lambda payload: sent.append(str(payload["event_id"])) or {"ok": True})

    assert sent == [first["event_id"], second["event_id"]]
    third_delivery = store.list_deliveries(str(third["event_id"]))[0]
    assert third_delivery["status"] == "pending"
    assert third_delivery["noise_result"] == "hourly_limit"
