from __future__ import annotations

import base64
import io
import sqlite3
from http.client import IncompleteRead
from types import SimpleNamespace
from pathlib import Path
from urllib import error

import pytest

from aiops.contracts.notification import EVENT_TYPES
from notification_service.configuration import PROVIDERS, NotificationConfiguration, NotificationConfigurationError
from notification_service.noise_controls import NotificationNoiseControls
from notification_service.requests import NotificationStore
from apps.aiops_k8s_gateway import notification_admin_http


def _configuration(tmp_path: Path, sent: list[tuple[str, str, str]] | None = None) -> NotificationConfiguration:
    key = tmp_path / "key"
    key.write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    return NotificationConfiguration(
        tmp_path / "notification.db",
        key,
        NotificationNoiseControls(tmp_path / "notification.db", clock=lambda: 1_700_000_000),
        clock=lambda: 1_700_000_000,
        send=lambda url, title, body: (sent.append((url, title, body)) if sent is not None else None) is None,
    )


def _request(**scope: str) -> dict[str, object]:
    return {
        "event_id": "incident.opened:incident-1:1",
        "event_type": "incident.opened",
        "occurred_at": 1_700_000_000,
        "severity": "critical",
        "subject": {"type": "incident", "id": "incident-1", "version": 1},
        "scope": {"environment": "prod", "team_id": "team-1", "service_id": "service-1", **scope},
        "summary": "Checkout unavailable",
        "facts": {"incident_id": "incident-1", "status": "opened"},
        "console_path": "/incidents/incident-1",
    }


def _verify_and_enable(
    configuration: NotificationConfiguration,
    destination_id: str,
    operation: str,
) -> dict[str, object]:
    revision = str(configuration.get_destination(destination_id)["configuration_revision"])
    store = NotificationStore(configuration.db_path, clock=lambda: 1_700_000_000)
    store.accept_test(
        destination_id,
        expected_revision=revision,
        operation_id=f"notification-delivery:{operation}",
    )
    store.run_delivery_once(lambda payload: {
        **configuration.delivery_result(
            destination_id,
            str(payload["title"]),
            str(payload["body"]),
            expected_revision=revision,
            allow_disabled=True,
        ),
        "message_id": "test-message",
    })
    return configuration.update_destination(
        destination_id,
        {
            "enabled": True,
            "expected_revision": revision,
            "operation_id": f"notification-destination-enable:{operation}",
        },
    )


def test_destination_credentials_are_encrypted_masked_and_tested_before_activation(tmp_path: Path) -> None:
    sent: list[tuple[str, str, str]] = []
    configuration = _configuration(tmp_path, sent)
    destination = configuration.create_destination(
        {
            "name": "Primary DingTalk",
            "provider": "dingtalk",
            "config": {
                "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=token123",
                "signing_secret": "SECsecret123",
            },
        }
    )

    assert destination["config"] == {
        "webhook_url": "https://oapi.dingtalk.com/***",
        "signing_secret_configured": True,
    }
    raw_database = (tmp_path / "notification.db").read_bytes()
    assert b"token123" not in raw_database
    assert b"SECsecret123" not in raw_database
    with pytest.raises(NotificationConfigurationError, match="pass test delivery"):
        configuration.update_destination(str(destination["id"]), {
            "enabled": True,
            "expected_revision": destination["configuration_revision"],
            "operation_id": "notification-destination-enable:before-test",
        })

    active = _verify_and_enable(configuration, str(destination["id"]), "credentials")

    assert active["enabled"] is True
    assert sent[0][0] == "dingtalk://SECsecret123@token123"


def test_first_enabled_exact_route_wins_collapses_duplicates_and_can_suppress(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    destination = configuration.create_destination(
        {
            "name": "Ops mail",
            "provider": "smtp",
            "config": {
                "host": "smtp.example.test",
                "port": 587,
                "username": "alerts@example.test",
                "password": "smtp-password",
                "from_address": "alerts@example.test",
                "to_addresses": ["ops@example.test"],
                "tls_mode": "starttls",
            },
        }
    )
    destination_id = str(destination["id"])
    _verify_and_enable(configuration, destination_id, "first-route")
    configuration.create_route(
        {
            "name": "Production incidents",
            "priority": 10,
            "enabled": True,
            "match": {"event": "incident.opened", "severity": ["critical"], "environment": "prod", "team": "team-1", "service": "service-1"},
            "destination_ids": [destination_id, destination_id],
        }
    )
    configuration.create_route(
        {
            "name": "Suppress lower priority",
            "priority": 20,
            "enabled": True,
            "match": {"event": "incident.opened"},
            "suppress_reason": "covered by primary route",
        }
    )

    matched = configuration.simulate(_request())
    suppressed = configuration.simulate(_request(team_id="team-2"))

    assert matched["route_name"] == "Production incidents"
    assert matched["destination_ids"] == [destination_id]
    assert matched["destinations"][0]["config"]["password_configured"] is True
    assert suppressed["route_name"] == "Suppress lower priority"
    assert suppressed["suppressed_reason"] == "covered by primary route"
    with pytest.raises(NotificationConfigurationError, match="disable routes"):
        configuration.update_destination(destination_id, {
            "enabled": False,
            "expected_revision": destination["configuration_revision"],
            "operation_id": "notification-destination-disable:route-active",
        })


def test_database_never_contains_encryption_key(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    configuration.create_destination(
        {
            "name": "Feishu",
            "provider": "feishu",
            "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc-123"},
        }
    )
    assert b"a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=" not in (tmp_path / "notification.db").read_bytes()
    with sqlite3.connect(tmp_path / "notification.db") as conn:
        assert conn.execute("SELECT count(*) FROM notification_routes WHERE is_default = 1 AND enabled = 1").fetchone()[0] == 1
    with pytest.raises(NotificationConfigurationError, match="unsupported fields"):
        configuration.create_route({"name": "invalid", "priority": 1, "match": {}, "suppress_reason": "no", "script": "true"})


def test_request_routing_fans_out_once_per_destination_or_records_suppression(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    destination = configuration.create_destination(
        {"name": "Feishu", "provider": "feishu", "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"}}
    )
    destination_id = str(destination["id"])
    _verify_and_enable(configuration, destination_id, "fanout")
    configuration.create_route({"name": "Critical", "priority": 1, "enabled": True, "match": {"severity": "critical"}, "destination_ids": [destination_id, destination_id]})
    store = NotificationStore(tmp_path / "notification.db", router=configuration.route)

    store.accept(_request())
    suppressed_request = _request() | {"event_id": "incident.opened:incident-2:1", "severity": "warning", "subject": {"type": "incident", "id": "incident-2", "version": 1}, "facts": {"incident_id": "incident-2", "status": "opened"}}
    store.accept(suppressed_request)

    with sqlite3.connect(tmp_path / "notification.db") as conn:
        assert conn.execute("SELECT destination FROM notification_deliveries WHERE event_id = ?", (_request()["event_id"],)).fetchall() == [(destination_id,)]
        assert conn.execute("SELECT suppressed_reason FROM notification_route_results WHERE event_id = ?", (suppressed_request["event_id"],)).fetchone() == ("No notification destination configured",)
    assert store.get_request(str(suppressed_request["event_id"]))["delivery_status"] == "suppressed"


def test_builtin_templates_cover_every_event_and_provider_and_custom_fields_are_restricted(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)

    builtins = configuration.list_templates()

    assert {(item["event_type"], item["provider"]) for item in builtins} == {
        (event, provider) for event in EVENT_TYPES for provider in PROVIDERS
    }
    source = next(item for item in builtins if item["event_type"] == "incident.opened" and item["provider"] == "smtp")
    draft = configuration.copy_template(str(source["id"]), {"name": "Checkout incident"})
    with pytest.raises(NotificationConfigurationError, match="unsupported template variable"):
        configuration.update_template(str(draft["id"]), {"body": "{{recipient}}"})
    with pytest.raises(NotificationConfigurationError, match="unsupported fields"):
        configuration.update_template(str(draft["id"]), {"script": "alert(1)"})
    with pytest.raises(NotificationConfigurationError, match="HTML"):
        configuration.update_template(str(draft["id"]), {"body": "<script>alert(1)</script>"})
    feishu = next(item for item in builtins if item["event_type"] == "incident.opened" and item["provider"] == "feishu")
    _, rendered = configuration.templates.render_for(str(feishu["id"]), "feishu", _request() | {"summary": "<script>alert(1)</script>"})
    assert "<script>" not in rendered["body"]


def test_template_preview_is_safe_and_required_for_each_version_before_activation(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    source = next(
        item
        for item in configuration.list_templates()
        if item["event_type"] == "incident.opened" and item["provider"] == "smtp"
    )
    draft = configuration.copy_template(str(source["id"]), {"name": "SMTP incident"})
    draft = configuration.update_template(
        str(draft["id"]),
        {
            "title": "{{severity}}: {{summary}}",
            "body": "**{{summary}}**\n\n[Open]({{console_url}})",
            "color": "#b42318",
            "button_label": "Open incident",
            "subject": "[{{severity}}] {{summary}}",
        },
    )
    with pytest.raises(NotificationConfigurationError, match="preview or test"):
        configuration.update_template(str(draft["id"]), {"enabled": True})

    preview = configuration.preview_template(str(draft["id"]), _request())
    assert preview["subject"] == "[critical] Checkout unavailable"
    assert "<strong>Checkout unavailable</strong>" in str(preview["html"])
    assert "Checkout unavailable" in str(preview["plain_text"])
    enabled = configuration.update_template(str(draft["id"]), {"enabled": True})
    assert enabled["enabled"] is True

    edited = configuration.update_template(str(draft["id"]), {"title": "Updated {{summary}}"})
    assert edited["version"] == 3
    assert edited["enabled"] is False
    with pytest.raises(NotificationConfigurationError, match="preview or test"):
        configuration.update_template(str(draft["id"]), {"enabled": True})


def test_route_freezes_compatible_template_version_and_rendered_content_on_delivery(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    destination = configuration.create_destination(
        {
            "name": "Ops mail",
            "provider": "smtp",
            "config": {
                "host": "smtp.example.test", "port": 587, "username": "alerts@example.test",
                "password": "smtp-password", "from_address": "alerts@example.test",
                "to_addresses": ["ops@example.test"], "tls_mode": "starttls",
            },
        }
    )
    destination_id = str(destination["id"])
    _verify_and_enable(configuration, destination_id, "frozen-template")
    source = next(item for item in configuration.list_templates() if item["event_type"] == "incident.opened" and item["provider"] == "smtp")
    template = configuration.copy_template(str(source["id"]), {"name": "Frozen incident"})
    template = configuration.update_template(str(template["id"]), {"title": "Original {{summary}}"})
    configuration.preview_template(str(template["id"]), _request())
    configuration.update_template(str(template["id"]), {"enabled": True})
    configuration.create_route(
        {"name": "SMTP incidents", "priority": 1, "enabled": True, "match": {"event": "incident.opened"},
         "destination_ids": [destination_id], "template_id": template["id"]}
    )
    store = NotificationStore(tmp_path / "notification.db", router=configuration.route)

    store.accept(_request())
    configuration.update_template(str(template["id"]), {"title": "Edited {{summary}}"})
    second = _request() | {"event_id": "incident.opened:incident-2:1", "subject": {"type": "incident", "id": "incident-2", "version": 1}, "facts": {"incident_id": "incident-2", "status": "opened"}}
    store.accept(second)
    sent: list[dict[str, object]] = []
    store.run_delivery_once(lambda payload: sent.append(payload) or {"ok": True, "message_id": "smtp-1"})

    assert sent[0]["title"] == "Original Checkout unavailable"
    delivery = store.list_deliveries(str(_request()["event_id"]))[0]
    assert delivery["template_id"] == template["id"]
    assert delivery["template_version"] == 2
    assert delivery["presentation"]["subject"] == "[AIOps] Checkout unavailable"
    assert store.list_deliveries(str(second["event_id"]))[0]["template_version"] == 2


def test_route_rejects_disabled_or_provider_incompatible_template(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    destination = configuration.create_destination(
        {"name": "Feishu", "provider": "feishu", "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"}}
    )
    destination_id = str(destination["id"])
    _verify_and_enable(configuration, destination_id, "template-provider")
    smtp = next(item for item in configuration.list_templates() if item["event_type"] == "incident.opened" and item["provider"] == "smtp")

    with pytest.raises(NotificationConfigurationError, match="compatible"):
        configuration.create_route(
            {"name": "Wrong provider", "priority": 1, "enabled": False, "match": {"event": "incident.opened"},
             "destination_ids": [destination_id], "template_id": smtp["id"]}
        )


def test_template_test_delivery_unlocks_only_after_successful_compatible_send(tmp_path: Path) -> None:
    sent: list[tuple[str, str, str]] = []
    configuration = _configuration(tmp_path, sent)
    destination = configuration.create_destination(
        {"name": "Feishu", "provider": "feishu", "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"}}
    )
    destination_id = str(destination["id"])
    _verify_and_enable(configuration, destination_id, "template-test")
    source = next(item for item in configuration.list_templates() if item["event_type"] == "incident.opened" and item["provider"] == "feishu")
    draft = configuration.copy_template(str(source["id"]), {"name": "Feishu incident"})

    result = configuration.test_template(str(draft["id"]), destination_id, _request())

    assert result["template"]["validated_at"] == 1_700_000_000
    assert sent[-1][1:] == ("critical: Checkout unavailable", "Checkout unavailable\n\nEvent: incident.opened\nSeverity: critical\n\n[Open in AIOps](https://aiops.invalid/incidents/incident-1)")


def test_engine_startup_freezes_builtin_presentation_for_pre_t18_unfinished_delivery(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    destination = configuration.create_destination(
        {"name": "Feishu", "provider": "feishu", "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"}}
    )
    destination_id = str(destination["id"])
    store = NotificationStore(
        tmp_path / "notification.db",
        router=lambda _request: {"route_id": None, "destination_ids": [destination_id], "suppressed_reason": None},
    )
    store.accept(_request())
    assert store.list_deliveries(str(_request()["event_id"]))[0]["presentation"] is None

    _configuration(tmp_path)

    frozen = store.list_deliveries(str(_request()["event_id"]))[0]
    assert frozen["template_id"] == "template:builtin:feishu:incident.opened"
    assert frozen["template_version"] == 1
    assert frozen["presentation"]["title"] == "critical: Checkout unavailable"

def test_gateway_proxy_strips_reason_and_audits_only_masked_engine_result(monkeypatch) -> None:
    forwarded: list[dict[str, object]] = []
    audits: list[dict[str, object]] = []
    masked = {"destination": {"id": "destination:1", "name": "Feishu", "provider": "feishu", "enabled": False, "config": {"webhook_url": "https://open.feishu.cn/***", "signing_secret_configured": False}}}
    monkeypatch.setattr(notification_admin_http, "_send", lambda _method, _path, payload, _request_id: (forwarded.append(payload) or (201, masked, True)))

    class Handler:
        command = "POST"
        def read_json_body(self): return {"name": "Feishu", "provider": "feishu", "config": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"}, "reason": "configure route"}
        def write_json(self, status, payload): self.response = (status, payload)

    handler = Handler()
    def unresolved_request(*_target):
        matching = [item for item in audits if item["request_id"] == "req-unknown"]
        return "req-unknown" if matching and matching[-1]["result"] == "outcome_unknown" else None

    sessions = SimpleNamespace(
        record_admin_audit=lambda **values: audits.append(values),
        unresolved_admin_request=unresolved_request,
    )
    authorize = lambda *_args, **_kwargs: SimpleNamespace(actor=SimpleNamespace(actor_id="admin-1"))

    assert notification_admin_http.dispatch(handler, "/api/v1/admin/notification-destinations", sessions, authorize, lambda *_args, **_kwargs: True, lambda _handler: (None, None), lambda _handler: "req-1", lambda code, message, request_id: {"error": {"code": code, "message": message}, "request_id": request_id})
    assert "reason" not in forwarded[0]
    assert "secret-token" in str(forwarded[0])
    assert "secret-token" not in str(audits)
    assert handler.response[0] == 201

    monkeypatch.setattr(
        notification_admin_http,
        "_send",
        lambda *_args: (503, {"error": "Notification Engine outcome is unknown"}, False),
    )
    unknown = Handler()
    assert notification_admin_http.dispatch(unknown, "/api/v1/admin/notification-destinations", sessions, authorize, lambda *_args, **_kwargs: True, lambda _handler: (None, None), lambda _handler: "req-unknown", lambda code, message, request_id: {"error": {"code": code, "message": message}, "request_id": request_id})
    assert unknown.response[0] == 503
    assert audits[-1]["request_id"] == "req-unknown"
    assert audits[-1]["result"] == "outcome_unknown"

    blocked = Handler()
    assert notification_admin_http.dispatch(blocked, "/api/v1/admin/notification-destinations", sessions, authorize, lambda *_args, **_kwargs: True, lambda _handler: (None, None), lambda _handler: "req-new", lambda code, message, request_id: {"error": {"code": code, "message": message}, "request_id": request_id})
    assert blocked.response[0] == 409
    assert blocked.response[1]["request_id"] == "req-unknown"

    monkeypatch.setattr(notification_admin_http, "_send", lambda *_args: (201, masked, True))
    reconciled = Handler()
    assert notification_admin_http.dispatch(reconciled, "/api/v1/admin/notification-destinations", sessions, authorize, lambda *_args, **_kwargs: True, lambda _handler: (None, None), lambda _handler: "req-unknown", lambda code, message, request_id: {"error": {"code": code, "message": message}, "request_id": request_id})
    assert reconciled.response[0] == 201
    assert audits[-1]["request_id"] == "req-unknown"
    assert audits[-1]["result"] == "success"


def test_gateway_proxy_bounds_malformed_owner_error(monkeypatch) -> None:
    monkeypatch.setenv("AIOPS_NOTIFICATION_ENGINE_URL", "http://notification.invalid")
    monkeypatch.setattr(notification_admin_http, "internal_auth_headers", lambda: {})

    def reject(*_args, **_kwargs):
        raise error.HTTPError(
            "http://notification.invalid/admin/notification-destinations",
            400,
            "Bad Request",
            {},
            io.BytesIO(b"not-json"),
        )

    monkeypatch.setattr(notification_admin_http.request, "urlopen", reject)

    assert notification_admin_http._send(
        "POST", "/admin/notification-destinations", {}, "request:malformed",
    ) == (400, {"error": "Notification Engine rejected the request"}, True)
    assert notification_admin_http._decode_owner_response(
        SimpleNamespace(read=lambda _limit: (_ for _ in ()).throw(IncompleteRead(b"{"))),
    ) is None
