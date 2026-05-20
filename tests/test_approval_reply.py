"""测试飞书文本审批回复处理。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "approval_reply.py"
    module_name = "test_approval_reply_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_runtime_operator_config(tmp_path: Path, open_id: str) -> Path:
    """写入 Hermes runtime config，避免依赖仓库默认 config。"""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        f"""
sre_permissions:
  operators:
    - name: "运行时审批人"
      platform: "feishu"
      platform_user_id: "{open_id}"
      role: "admin"
      namespaces: ["*"]
      allowed_tools: ["k8s_read", "k8s_write", "k8s_exec"]
      can_approve: true
""",
        encoding="utf-8",
    )
    return hermes_home


def test_parse_approve_reply() -> None:
    """批准文本应解析为 approved 决策。"""
    module = _load_module()

    parsed = module.parse_approval_reply("批准 abc-123")

    assert parsed == {"decision": "approved", "approval_id": "abc-123", "reason": None}


def test_parse_deny_reply_with_reason() -> None:
    """拒绝文本应保留拒绝原因。"""
    module = _load_module()

    parsed = module.parse_approval_reply("拒绝 abc-123 风险过高")

    assert parsed == {"decision": "denied", "approval_id": "abc-123", "reason": "风险过高"}


def test_parse_non_approval_reply() -> None:
    """非审批文本应忽略。"""
    module = _load_module()

    assert module.parse_approval_reply("看一下 nginx") is None


def test_approval_reply_does_not_import_or_call_execution() -> None:
    """文本审批 hook 只改审批状态，不直接触发修复执行。"""
    source = (Path(__file__).resolve().parents[1] / "hooks" / "approval_reply.py").read_text(
        encoding="utf-8",
    )
    forbidden_tokens = [
        "approval_execution",
        "remediation_execution",
        "process_pending_executions",
        "process_approval_execution",
    ]

    assert [token for token in forbidden_tokens if token in source] == []


@pytest.mark.asyncio
async def test_handle_approve_reply_records_timeline(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """批准回复应 resolve approval 并记录 timeline。"""
    module = _load_module()
    events: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            assert (approval_id, decision, approver, reason) == ("ap-1", "approved", "ou_admin", None)
            return {"ok": True, "approval_id": approval_id, "status": "approved"}

        @staticmethod
        async def check_approval(approval_id):
            assert approval_id == "ap-1"
            return {
                "approval_id": approval_id,
                "status": "pending",
                "operation_type": "k8s_write",
                "namespace": "default",
                "risk_level": "medium",
                "requester": "alert_webhook",
                "context": {},
                "incident_id": "inc-1",
            }

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    async def _authorize(**kwargs):
        return {"ok": True, "operator": {"role": "admin"}}

    monkeypatch.setattr(
        module.approval_authorization,
        "authorize_approval_reply",
        _authorize,
    )

    result = await module.handle_approval_reply("批准 ap-1", "ou_admin")

    assert result == {"handled": True, "ok": True, "approval_id": "ap-1", "status": "approved"}
    assert events == [("inc-1", "approval_approved", "approval_reply", "ap-1", "ou_admin", None)]


@pytest.mark.asyncio
async def test_handle_deny_reply_records_timeline(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """拒绝回复应 resolve approval 并记录 timeline。"""
    module = _load_module()
    events: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            assert (approval_id, decision, approver, reason) == ("ap-2", "denied", "ou_admin", "风险过高")
            return {"ok": True, "approval_id": approval_id, "status": "denied"}

        @staticmethod
        async def check_approval(approval_id):
            assert approval_id == "ap-2"
            return {
                "approval_id": approval_id,
                "status": "pending",
                "operation_type": "k8s_write",
                "namespace": "default",
                "risk_level": "medium",
                "requester": "alert_webhook",
                "context": {},
                "incident_id": "inc-2",
            }

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    async def _authorize(**kwargs):
        return {"ok": True, "operator": {"role": "admin"}}

    monkeypatch.setattr(
        module.approval_authorization,
        "authorize_approval_reply",
        _authorize,
    )

    result = await module.handle_approval_reply("拒绝 ap-2 风险过高", "ou_admin")

    assert result == {"handled": True, "ok": True, "approval_id": "ap-2", "status": "denied"}
    assert events == [("inc-2", "approval_denied", "approval_reply", "ap-2", "ou_admin", None)]


@pytest.mark.asyncio
async def test_handle_unknown_approval_reply_returns_error(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """未知审批 ID 应返回清晰错误且不写 timeline。"""
    module = _load_module()

    class _ApprovalAsync:
        @staticmethod
        async def check_approval(approval_id):
            return {"found": False, "approval_id": approval_id, "message": "审批记录不存在"}

        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            raise AssertionError("missing approval must not resolve")

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)

    result = await module.handle_approval_reply("批准 missing", "ou_admin")

    assert result == {"handled": True, "ok": False, "approval_id": "missing", "message": "审批记录不存在"}


@pytest.mark.asyncio
async def test_unauthorized_approver_does_not_resolve(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """未授权审批人不得触发 resolve_approval。"""
    module = _load_module()
    events: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def check_approval(approval_id):
            return {
                "approval_id": approval_id,
                "status": "pending",
                "operation_type": "k8s_write",
                "namespace": "default",
                "risk_level": "medium",
                "requester": "alert_webhook",
                "context": {},
                "incident_id": "inc-1",
            }

        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            raise AssertionError("unauthorized approver must not resolve")

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    async def _authorize(**kwargs):
        return {
            "ok": False,
            "message": "审批人未授权",
            "reason_code": "unknown_approver",
        }

    monkeypatch.setattr(
        module.approval_authorization,
        "authorize_approval_reply",
        _authorize,
    )

    result = await module.handle_approval_reply("批准 ap-1", "ou_unknown")

    assert result == {"handled": True, "ok": False, "approval_id": "ap-1", "message": "审批人未授权"}
    assert events == [
        (
            "inc-1",
            "approval_unauthorized",
            "approval_reply",
            "ap-1",
            "ou_unknown",
            {
                "approval_id": "ap-1",
                "approver_id": "ou_unknown",
                "decision": "approved",
                "reason_code": "unknown_approver",
                "operation_type": "k8s_write",
                "namespace": "default",
            },
        )
    ]


@pytest.mark.asyncio
async def test_non_pending_approval_does_not_write_success_timeline(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """已处理 approval 不得重复 resolve 或写成功 timeline。"""
    module = _load_module()
    events: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def check_approval(approval_id):
            return {
                "approval_id": approval_id,
                "status": "expired",
                "operation_type": "k8s_write",
                "namespace": "default",
                "risk_level": "medium",
                "requester": "alert_webhook",
                "context": {},
                "incident_id": "inc-1",
            }

        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            raise AssertionError("non-pending approval must not resolve")

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    async def _authorize(**kwargs):
        return {
            "ok": False,
            "message": "审批已处理或已过期",
            "reason_code": "approval_not_pending",
        }

    monkeypatch.setattr(
        module.approval_authorization,
        "authorize_approval_reply",
        _authorize,
    )

    result = await module.handle_approval_reply("拒绝 ap-1 风险过高", "ou_admin")

    assert result == {"handled": True, "ok": False, "approval_id": "ap-1", "message": "审批已处理或已过期"}
    assert [event[1] for event in events] == ["approval_unauthorized"]


@pytest.mark.asyncio
async def test_card_decision_unauthorized_does_not_resolve(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """Feishu card 未授权点击不得触发 resolve_approval。"""
    module = _load_module()
    events: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def check_approval(approval_id):
            return {
                "approval_id": approval_id,
                "status": "pending",
                "operation_type": "k8s_write",
                "namespace": "default",
                "risk_level": "medium",
                "requester": "alert_webhook",
                "context": {},
                "incident_id": "inc-card",
            }

        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            raise AssertionError("unauthorized card callback must not resolve")

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    async def _authorize(**kwargs):
        return {"ok": False, "message": "审批人未授权", "reason_code": "unknown_approver"}

    monkeypatch.setattr(module.approval_authorization, "authorize_approval_reply", _authorize)

    result = await module.handle_approval_decision(
        approval_id="ap-card",
        decision="approved",
        reason=None,
        approver_id="ou_unknown",
        source="feishu_card",
    )

    assert result == {"handled": True, "ok": False, "approval_id": "ap-card", "message": "审批人未授权"}
    assert events[0][1:5] == ("approval_unauthorized", "feishu_card", "ap-card", "ou_unknown")


@pytest.mark.asyncio
async def test_card_decision_duplicate_or_stale_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    """Feishu card 重复点击/已决 approval 不得二次 mutate。"""
    module = _load_module()
    events: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def check_approval(approval_id):
            return {
                "approval_id": approval_id,
                "status": "approved",
                "operation_type": "k8s_write",
                "namespace": "default",
                "risk_level": "medium",
                "requester": "alert_webhook",
                "context": {},
                "incident_id": "inc-card",
            }

        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            raise AssertionError("stale card callback must not resolve")

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    async def _authorize(**kwargs):
        return {"ok": False, "message": "审批已处理或已过期", "reason_code": "approval_not_pending"}

    monkeypatch.setattr(module.approval_authorization, "authorize_approval_reply", _authorize)

    result = await module.handle_approval_decision(
        approval_id="ap-card",
        decision="denied",
        reason="重复点击",
        approver_id="ou_admin",
        source="feishu_card",
    )

    assert result == {"handled": True, "ok": False, "approval_id": "ap-card", "message": "审批已处理或已过期"}
    assert events[0][1:5] == ("approval_unauthorized", "feishu_card", "ap-card", "ou_admin")


@pytest.mark.asyncio
async def test_card_decision_missing_approval_id_fails_closed(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """缺 approval_id 不得查询或修改 approval。"""
    module = _load_module()

    class _ApprovalAsync:
        @staticmethod
        async def check_approval(approval_id):
            raise AssertionError("missing approval_id must fail before lookup")

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)

    result = await module.handle_approval_decision(
        approval_id="",
        decision="approved",
        approver_id="ou_admin",
        source="feishu_card",
    )

    assert result == {"handled": True, "ok": False, "approval_id": "", "message": "缺少 approval_id"}


@pytest.mark.asyncio
async def test_text_approval_authorizes_operator_from_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """文本审批应使用 Hermes runtime config 中的 Feishu operator 授权。"""
    runtime_open_id = "ou_runtime_text_approver"
    hermes_home = _write_runtime_operator_config(tmp_path, runtime_open_id)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)

    module = _load_module()
    events: list[tuple] = []
    resolved: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def check_approval(approval_id):
            return {
                "approval_id": approval_id,
                "status": "pending",
                "operation_type": "k8s_write",
                "namespace": "default",
                "risk_level": "medium",
                "requester": "alert_webhook",
                "context": {},
                "incident_id": "inc-runtime",
            }

        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            resolved.append((approval_id, decision, approver, reason))
            return {"ok": True, "approval_id": approval_id, "status": "approved"}

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    result = await module.handle_approval_reply("批准 ap-runtime", runtime_open_id)

    assert result == {"handled": True, "ok": True, "approval_id": "ap-runtime", "status": "approved"}
    assert resolved == [("ap-runtime", "approved", runtime_open_id, None)]
    assert events == [("inc-runtime", "approval_approved", "approval_reply", "ap-runtime", runtime_open_id, None)]


@pytest.mark.asyncio
async def test_card_approval_authorizes_operator_from_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """Card callback 审批应使用 Hermes runtime config 中的 Feishu operator 授权。"""
    runtime_open_id = "ou_runtime_card_approver"
    hermes_home = _write_runtime_operator_config(tmp_path, runtime_open_id)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)

    module = _load_module()
    events: list[tuple] = []
    resolved: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def check_approval(approval_id):
            return {
                "approval_id": approval_id,
                "status": "pending",
                "operation_type": "k8s_write",
                "namespace": "default",
                "risk_level": "medium",
                "requester": "alert_webhook",
                "context": {},
                "incident_id": "inc-runtime",
            }

        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            resolved.append((approval_id, decision, approver, reason))
            return {"ok": True, "approval_id": approval_id, "status": "approved"}

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    result = await module.handle_approval_decision(
        approval_id="ap-runtime-card",
        decision="approved",
        reason=None,
        approver_id=runtime_open_id,
        source="feishu_card",
    )

    assert result == {"handled": True, "ok": True, "approval_id": "ap-runtime-card", "status": "approved"}
    assert resolved == [("ap-runtime-card", "approved", runtime_open_id, None)]
    assert events == [
        ("inc-runtime", "approval_approved", "feishu_card", "ap-runtime-card", runtime_open_id, None),
    ]
