"""测试飞书原生审批事件 webhook。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import importlib.util
import json
from pathlib import Path
import sys
import time

from aiohttp import web
import pytest


def _load_module():
    """按文件路径加载飞书审批事件 hook。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "feishu_approval_event.py"
    assert module_path.exists(), "hooks/feishu_approval_event.py is required"
    spec = importlib.util.spec_from_file_location("test_feishu_approval_event_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeApprovalAsync:
    def __init__(self) -> None:
        self.synced: list[dict] = []
        self.rows = {
            "ap-native-1": {
                "approval_id": "ap-native-1",
                "status": "external_pending",
                "external_instance_code": "INST-001",
            }
        }

    async def resolve_external_approval(self, *, external_uuid, external_instance_code, external_status, source, raw_event):
        if external_uuid not in self.rows and external_instance_code != "INST-001":
            return {"ok": False, "reason": "not_found", "status": "ignored"}
        item = {
            "external_uuid": external_uuid,
            "external_instance_code": external_instance_code,
            "external_status": external_status,
            "source": source,
            "raw_event": raw_event,
        }
        self.synced.append(item)
        status_map = {"APPROVED": "approved", "REJECTED": "denied", "CANCELED": "canceled"}
        return {"ok": True, "approval_id": external_uuid, "status": status_map[external_status]}


class FakeExecution:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute_approved(self, approval_id):
        self.calls.append(approval_id)
        return {"ok": True}

    async def process_pending_executions(self, *args, **kwargs):
        self.calls.append("process_pending_executions")
        return {"processed": 1}


def _config() -> dict:
    return {
        "platforms": {
            "feishu": {
                "app_id": "cli_test",
                "approval": {
                    "callback_path": "/webhooks/feishu/approval",
                    "event_verification_token": "verify-token",
                    "event_encrypt_key": "encrypt-key",
                    "signature_ttl_seconds": 300,
                },
            }
        }
    }


def _event_payload(status: str = "APPROVED", *, uuid: str = "ap-native-1", instance_code: str = "INST-001") -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": "event-1",
            "event_type": "approval_instance",
            "app_id": "cli_test",
            "token": "verify-token",
        },
        "event": {
            "uuid": uuid,
            "instance_code": instance_code,
            "status": status,
            "operator": {"open_id": "ou_approver"},
            "task_list": [{"status": status}],
            "command": "kubectl delete ns prod",
        },
    }


def _signed_headers(payload: dict, *, timestamp: str, nonce: str) -> dict[str, str]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    base = timestamp.encode("utf-8") + nonce.encode("utf-8") + body
    signature = base64.b64encode(hmac.new(b"encrypt-key", base, hashlib.sha256).digest()).decode("utf-8")
    return {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature,
    }


async def _handle_event(module, payload, fake_approval, fake_execution=None, headers=None):
    handler = getattr(module, "handle_feishu_approval_event", None)
    assert callable(handler), "handle_feishu_approval_event(...) is required"
    if fake_execution is not None:
        setattr(module, "approval_execution", fake_execution)
    setattr(module, "approval_async", fake_approval)
    return await handler(payload, headers=headers or {}, config=_config())


@pytest.mark.asyncio
async def test_callback_route_can_be_registered_or_request_handler_exported(**_kwargs) -> None:
    """飞书审批 callback 必须提供 aiohttp route 注册入口或 request handler。"""
    module = _load_module()
    setup = (
        getattr(module, "setup_feishu_approval_webhook", None)
        or getattr(module, "setup_feishu_approval_event_webhook", None)
    )
    request_handler = (
        getattr(module, "handle_feishu_approval_callback", None)
        or getattr(module, "handle_feishu_approval_request", None)
    )

    assert callable(setup) or callable(request_handler), "callback route/request handler is required"
    if callable(setup):
        app = web.Application()
        result = setup(app, config=_config())
        if inspect.isawaitable(result):
            await result
        assert any(route.resource.canonical == "/webhooks/feishu/approval" for route in app.router.routes())


@pytest.mark.parametrize(
    ("external_status", "local_status"),
    [("APPROVED", "approved"), ("REJECTED", "denied"), ("CANCELED", "canceled")],
)
@pytest.mark.asyncio
async def test_event_syncs_supported_statuses_without_direct_execution(external_status, local_status, **_kwargs) -> None:
    module = _load_module()
    fake_approval = FakeApprovalAsync()
    fake_execution = FakeExecution()

    result = await _handle_event(module, _event_payload(external_status), fake_approval, fake_execution)

    assert result["ok"] is True
    assert result["status"] == local_status
    assert fake_approval.synced[0]["external_status"] == external_status
    assert fake_execution.calls == []


@pytest.mark.asyncio
async def test_duplicate_events_are_idempotent_and_do_not_execute(**_kwargs) -> None:
    module = _load_module()
    fake_approval = FakeApprovalAsync()
    fake_execution = FakeExecution()

    first = await _handle_event(module, _event_payload("APPROVED"), fake_approval, fake_execution)
    second = await _handle_event(module, _event_payload("APPROVED"), fake_approval, fake_execution)

    assert first["status"] == "approved"
    assert second["status"] == "approved"
    assert len(fake_approval.synced) == 2
    assert fake_execution.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        _event_payload("APPROVED", uuid="missing-uuid", instance_code="missing-inst"),
        _event_payload("UNKNOWN"),
    ],
)
@pytest.mark.asyncio
async def test_unknown_uuid_instance_or_status_is_ignored(payload, **_kwargs) -> None:
    module = _load_module()
    fake_approval = FakeApprovalAsync()
    fake_execution = FakeExecution()

    result = await _handle_event(module, payload, fake_approval, fake_execution)

    assert result["ok"] is False
    assert result["status"] == "ignored"
    assert fake_execution.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {**_event_payload("APPROVED"), "header": {**_event_payload("APPROVED")["header"], "token": "bad-token"}},
        {**_event_payload("APPROVED"), "header": {**_event_payload("APPROVED")["header"], "app_id": "wrong-app"}},
    ],
)
@pytest.mark.asyncio
async def test_invalid_token_or_app_id_is_rejected_before_sync(payload, **_kwargs) -> None:
    module = _load_module()
    fake_approval = FakeApprovalAsync()

    result = await _handle_event(module, payload, fake_approval, FakeExecution())

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert fake_approval.synced == []


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected_before_sync(**_kwargs) -> None:
    module = _load_module()
    fake_approval = FakeApprovalAsync()

    result = await _handle_event(
        module,
        _event_payload("APPROVED"),
        fake_approval,
        FakeExecution(),
        headers={"X-Lark-Request-Timestamp": "1710000000", "X-Lark-Signature": "bad-signature"},
    )

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert fake_approval.synced == []


@pytest.mark.asyncio
async def test_missing_signature_is_rejected_before_sync(**_kwargs) -> None:
    module = _load_module()
    fake_approval = FakeApprovalAsync()

    result = await _handle_event(
        module,
        _event_payload("APPROVED"),
        fake_approval,
        FakeExecution(),
        headers={
            "X-Lark-Request-Timestamp": str(int(time.time())),
            "X-Lark-Request-Nonce": "nonce-missing-signature",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert fake_approval.synced == []


@pytest.mark.asyncio
async def test_expired_timestamp_is_rejected_before_sync(**_kwargs) -> None:
    module = _load_module()
    fake_approval = FakeApprovalAsync()
    payload = _event_payload("APPROVED")

    result = await _handle_event(
        module,
        payload,
        fake_approval,
        FakeExecution(),
        headers=_signed_headers(payload, timestamp="1", nonce="nonce-expired"),
    )

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert fake_approval.synced == []


@pytest.mark.asyncio
async def test_replayed_nonce_is_rejected_before_second_sync(**_kwargs) -> None:
    module = _load_module()
    fake_approval = FakeApprovalAsync()
    payload = _event_payload("APPROVED")
    headers = _signed_headers(payload, timestamp=str(int(time.time())), nonce="nonce-replay")

    first = await _handle_event(module, payload, fake_approval, FakeExecution(), headers=headers)
    second = await _handle_event(module, payload, fake_approval, FakeExecution(), headers=headers)

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["status"] == "rejected"
    assert len(fake_approval.synced) == 1
