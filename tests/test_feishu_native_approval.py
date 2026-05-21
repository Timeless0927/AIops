"""测试飞书原生审批 OpenAPI 客户端。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _load_module():
    """按文件路径加载飞书原生审批模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "feishu_native_approval.py"
    assert module_path.exists(), "toolsets/feishu_native_approval.py is required"
    spec = importlib.util.spec_from_file_location("test_feishu_native_approval_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeFeishuHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def post_json(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _approval_config() -> dict:
    return {
        "platforms": {
            "feishu": {
                "app_id": "cli_test",
                "app_secret": "app_secret",
                "approval": {
                    "enabled": True,
                    "approval_code": "1D7CF6FF-2647-4A90-9FEE-D74C92D1D985",
                    "requester_open_id": "ou_requester",
                    "user_id_type": "open_id",
                    "mode": "summary_detail",
                    "fields": {
                        "summary": {"id": "widget17792695890", "type": "input"},
                        "detail": {"id": "widget17792695891", "type": "textarea"},
                    },
                    "approver_node_key": "APPROVAL_1",
                    "approver_open_ids": "ou_approver,ou_backup",
                    "timeout_seconds": 3,
                },
            }
        }
    }


async def _create_instance(module, http, config: dict | None = None):
    create_instance = getattr(module, "create_approval_instance", None)
    assert callable(create_instance), "create_approval_instance(...) is required"
    return await create_instance(
        approval_id="ap-native-1",
        operation_type="k8s_write",
        command="kubectl rollout restart deployment/nginx -n default",
        context={
            "action_signature": "restart_deployment:prod-a:default:deployment/nginx",
            "alertname": "PodCrashLooping",
            "cluster": "prod-a",
        },
        namespace="default",
        requester_open_id="ou_requester",
        risk_level="low",
        config=config or _approval_config(),
        http_client=http,
    )


@pytest.mark.asyncio
async def test_create_instance_success_returns_external_fields_and_payload(**_kwargs) -> None:
    """创建审批实例成功后应返回可写入本地 approval 的外部字段。"""
    module = _load_module()
    http = FakeFeishuHTTP(
        [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {
                "code": 0,
                "data": {
                    "instance_code": "INST-001",
                    "approve_url": "https://approval.feishu.cn/approval/INST-001",
                },
            },
        ]
    )

    result = await _create_instance(module, http)

    assert result["ok"] is True
    assert result["external_provider"] == "feishu"
    assert result["external_uuid"] == "ap-native-1"
    assert result["external_instance_code"] == "INST-001"
    assert result["external_url"] == "https://approval.feishu.cn/approval/INST-001"
    assert result["external_status"] in {"PENDING", "pending"}
    assert http.calls[0]["url"].endswith("/auth/v3/tenant_access_token/internal")
    assert http.calls[1]["url"].endswith("/approval/v4/instances")
    assert http.calls[1]["headers"]["Authorization"] == "Bearer tenant-token"
    payload = http.calls[1]["json"]
    assert payload["approval_code"] == "1D7CF6FF-2647-4A90-9FEE-D74C92D1D985"
    assert payload["uuid"] == "ap-native-1"
    assert payload["open_id"] == "ou_requester"
    assert payload["user_id_type"] == "open_id"
    assert payload["node_approver_open_id_list"] == [{"key": "APPROVAL_1", "value": ["ou_approver", "ou_backup"]}]
    form = json.loads(payload["form"]) if isinstance(payload.get("form"), str) else payload.get("form")
    assert form[0]["id"] == "widget17792695890"
    assert form[0]["type"] == "input"
    assert form[1]["id"] == "widget17792695891"
    assert form[1]["type"] == "textarea"
    form_text = json.dumps(form, ensure_ascii=False)
    assert "kubectl rollout restart deployment/nginx" in form_text
    assert "PodCrashLooping" in form_text
    assert "prod-a" in form_text
    assert "low" in form_text
    assert "本地审批 ID: ap-native-1" in form_text
    assert {"approval_id", "operation_type", "command", "namespace", "risk_level", "context"}.isdisjoint(
        {item["id"] for item in form}
    )


@pytest.mark.asyncio
async def test_create_instance_missing_summary_detail_config_fails_before_token(**_kwargs) -> None:
    """summary_detail 主路径缺控件配置时应给出清晰错误，且不发起 token 请求。"""
    module = _load_module()
    config = _approval_config()
    config["platforms"]["feishu"]["approval"]["fields"].pop("detail")
    http = FakeFeishuHTTP([{"code": 0, "tenant_access_token": "tenant-token"}])

    result = await module.create_approval_instance(
        approval_id="ap-native-1",
        operation_type="k8s_write",
        command="kubectl rollout restart deployment/nginx -n default",
        context={"alertname": "PodCrashLooping", "cluster": "prod-a"},
        namespace="default",
        requester_open_id="ou_requester",
        risk_level="low",
        config=config,
        http_client=http,
    )

    assert result["ok"] is False
    assert result["error_type"] == "config_error"
    assert result["missing_field"] == "detail"
    assert "detail" in result["message"]
    assert http.calls == []


@pytest.mark.asyncio
async def test_create_instance_field_type_mismatch_fails_before_token(**_kwargs) -> None:
    """审批定义控件类型与 summary_detail 配置不匹配时应明确报错。"""
    module = _load_module()
    config = _approval_config()
    config["platforms"]["feishu"]["approval"]["fields"]["detail"]["type"] = "input"
    http = FakeFeishuHTTP([{"code": 0, "tenant_access_token": "tenant-token"}])

    result = await module.create_approval_instance(
        approval_id="ap-native-1",
        operation_type="k8s_write",
        command="kubectl rollout restart deployment/nginx -n default",
        context={"alertname": "PodCrashLooping", "cluster": "prod-a"},
        namespace="default",
        requester_open_id="ou_requester",
        risk_level="low",
        config=config,
        http_client=http,
    )

    assert result["ok"] is False
    assert result["error_type"] == "config_error"
    assert result["field"] == "detail"
    assert result["expected_types"] == ["textarea"]
    assert result["actual_type"] == "input"
    assert "type mismatch" in result["message"]
    assert http.calls == []


@pytest.mark.asyncio
async def test_create_instance_supports_single_text_mode(**_kwargs) -> None:
    """single_text 模式仍可把审批摘要和详情写入单个真实控件。"""
    module = _load_module()
    config = _approval_config()
    config["platforms"]["feishu"]["approval"]["mode"] = "single_text"
    config["platforms"]["feishu"]["approval"]["fields"] = {
        "text": {"id": "widget_text", "type": "textarea"}
    }
    http = FakeFeishuHTTP(
        [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {"instance_code": "INST-001"}},
        ]
    )

    result = await _create_instance(module, http, config=config)

    assert result["ok"] is True
    form = json.loads(http.calls[1]["json"]["form"])
    assert form == [
        {
            "id": "widget_text",
            "type": "textarea",
            "value": form[0]["value"],
        }
    ]
    assert "ap-native-1" in form[0]["value"]
    assert "kubectl rollout restart deployment/nginx" in form[0]["value"]


@pytest.mark.asyncio
async def test_create_instance_uses_approval_code_definition_override(**_kwargs) -> None:
    """支持按 approval_code 配置多套审批定义字段映射。"""
    module = _load_module()
    config = _approval_config()
    approval = config["platforms"]["feishu"]["approval"]
    approval.pop("mode")
    approval.pop("fields")
    approval["definitions"] = {
        "1D7CF6FF-2647-4A90-9FEE-D74C92D1D985": {
            "mode": "summary_detail",
            "fields": {
                "summary": "widget_definition_summary",
                "detail": "widget_definition_detail",
            },
        }
    }
    http = FakeFeishuHTTP(
        [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {"instance_code": "INST-001"}},
        ]
    )

    result = await _create_instance(module, http, config=config)

    assert result["ok"] is True
    form = json.loads(http.calls[1]["json"]["form"])
    assert [item["id"] for item in form] == ["widget_definition_summary", "widget_definition_detail"]


@pytest.mark.asyncio
async def test_create_instance_supports_legacy_fields_mode_for_terminal_reject_definition(**_kwargs) -> None:
    """legacy_fields 模式应按旧审批定义控件 ID 生成表单 payload。"""
    module = _load_module()
    config = _approval_config()
    approval = config["platforms"]["feishu"]["approval"]
    approval["approval_code"] = "EF5705C5-0107-4DEE-B9AE-9F5EE6040690"
    approval["mode"] = "legacy_fields"
    approval["legacy_fields"] = {
        "source": {"id": "widget17788287542540001", "type": "input"},
        "incident_id": {"id": "widget17788288041580001", "type": "input"},
        "risk_level": {"id": "widget17788288021020001", "type": "input"},
        "command": {"id": "widget17788287996940001", "type": "input"},
        "namespace": {"id": "widget17788288799990001", "type": "input"},
        "reason": {"id": "widget17788289055130001", "type": "input"},
    }
    http = FakeFeishuHTTP(
        [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {"instance_code": "INST-LEGACY"}},
        ]
    )

    result = await module.create_approval_instance(
        approval_id="ap-native-legacy",
        operation_type="k8s_write",
        command="kubectl rollout restart deployment/nginx -n default",
        context={
            "source": "alert_webhook",
            "remediation_action": {
                "namespace": "default",
                "source": {
                    "incident_id": "incident-1",
                    "alertname": "PodCrashLooping",
                },
                "risk": {"risk_level": "low", "operation_type": "k8s_write"},
            },
        },
        namespace="fallback",
        requester_open_id="ou_requester",
        risk_level="standard",
        config=config,
        http_client=http,
    )

    assert result["ok"] is True
    payload = http.calls[1]["json"]
    assert payload["approval_code"] == "EF5705C5-0107-4DEE-B9AE-9F5EE6040690"
    form = json.loads(payload["form"])
    assert form == [
        {"id": "widget17788287542540001", "type": "input", "value": "alert_webhook"},
        {"id": "widget17788288041580001", "type": "input", "value": "incident-1"},
        {"id": "widget17788288021020001", "type": "input", "value": "low"},
        {
            "id": "widget17788287996940001",
            "type": "input",
            "value": "kubectl rollout restart deployment/nginx -n default",
        },
        {"id": "widget17788288799990001", "type": "input", "value": "default"},
        {"id": "widget17788289055130001", "type": "input", "value": "PodCrashLooping 自动触发"},
    ]


@pytest.mark.asyncio
async def test_create_instance_legacy_fields_missing_mapping_fails_before_token(**_kwargs) -> None:
    """legacy_fields 缺少任一语义字段映射时应失败，避免创建错误流程。"""
    module = _load_module()
    config = _approval_config()
    approval = config["platforms"]["feishu"]["approval"]
    approval["approval_code"] = "EF5705C5-0107-4DEE-B9AE-9F5EE6040690"
    approval["mode"] = "legacy_fields"
    approval["legacy_fields"] = {
        "source": "widget17788287542540001",
        "incident_id": "widget17788288041580001",
        "risk_level": "widget17788288021020001",
        "command": "widget17788287996940001",
        "namespace": "widget17788288799990001",
    }
    http = FakeFeishuHTTP([{"code": 0, "tenant_access_token": "tenant-token"}])

    result = await _create_instance(module, http, config=config)

    assert result["ok"] is False
    assert result["error_type"] == "config_error"
    assert result["missing_field"] == "reason"
    assert http.calls == []


@pytest.mark.asyncio
async def test_create_instance_uses_user_id_payload_when_configured(**_kwargs) -> None:
    """user_id_type=user_id 时应发送 user_id 和 node_approver_user_id_list。"""
    module = _load_module()
    config = _approval_config()
    approval = config["platforms"]["feishu"]["approval"]
    approval["user_id_type"] = "user_id"
    approval.pop("requester_open_id")
    approval["requester_user_id"] = "u_requester"
    approval.pop("approver_open_ids")
    approval["approver_user_ids"] = ["u_approver"]
    http = FakeFeishuHTTP(
        [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {"instance_code": "INST-001"}},
        ]
    )

    result = await _create_instance(module, http, config=config)

    assert result["ok"] is True
    payload = http.calls[1]["json"]
    assert payload["user_id"] == "u_requester"
    assert "open_id" not in payload
    assert payload["user_id_type"] == "user_id"
    assert payload["node_approver_user_id_list"] == [{"key": "APPROVAL_1", "value": ["u_approver"]}]


@pytest.mark.parametrize(
    ("responses", "error_type", "message_part"),
    [
        ([{"code": 99991663, "msg": "invalid app_secret"}], "token_error", "tenant_access_token"),
        (
            [
                {"code": 0, "tenant_access_token": "tenant-token"},
                {"code": 1254001, "msg": "approval_code not found"},
            ],
            "feishu_error",
            "approval_code not found",
        ),
        ([TimeoutError("request timed out")], "http_timeout", "timeout"),
        ([{"code": 0, "tenant_access_token": "tenant-token"}, ValueError("response is not JSON")], "invalid_json", "json"),
    ],
)
@pytest.mark.asyncio
async def test_create_instance_errors_are_explicit(responses, error_type, message_part, **_kwargs) -> None:
    """飞书错误、token 失败、超时和非 JSON 响应应给出明确错误分类。"""
    module = _load_module()
    http = FakeFeishuHTTP(responses)

    result = await _create_instance(module, http)

    assert result["ok"] is False
    assert result["error_type"] == error_type
    assert message_part.lower() in str(result["message"]).lower()
