"""飞书原生审批配置验收测试。"""

from __future__ import annotations

from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _assert_feishu_approval_callback_and_polling_interval(path: Path) -> None:
    content = path.read_text(encoding="utf-8")

    assert "platforms:" in content
    assert "feishu:" in content
    assert "approval:" in content
    assert "mode: \"${FEISHU_APPROVAL_FORM_MODE}\"" in content
    assert "FEISHU_APPROVAL_SUMMARY_FIELD_ID" in content
    assert "FEISHU_APPROVAL_DETAIL_FIELD_ID" in content
    assert "legacy_fields:" in content
    assert "FEISHU_APPROVAL_SOURCE_FIELD_ID" in content
    assert "FEISHU_APPROVAL_INCIDENT_ID_FIELD_ID" in content
    assert "FEISHU_APPROVAL_RISK_LEVEL_FIELD_ID" in content
    assert "FEISHU_APPROVAL_COMMAND_FIELD_ID" in content
    assert "FEISHU_APPROVAL_NAMESPACE_FIELD_ID" in content
    assert "FEISHU_APPROVAL_REASON_FIELD_ID" in content
    assert "FEISHU_APPROVAL_APPROVER_NODE_KEY" in content
    assert "FEISHU_APPROVAL_APPROVER_OPEN_ID" in content
    assert "callback_path:" in content
    assert "/webhooks/feishu/approval" in content
    assert "polling_interval_seconds:" in content


def test_deploy_template_declares_feishu_approval_callback_and_polling_interval() -> None:
    _assert_feishu_approval_callback_and_polling_interval(
        _project_root() / "deploy" / "hermes-config.template.yaml"
    )


def test_k8s_configmap_uses_terminal_reject_legacy_approval_definition() -> None:
    content = (_project_root() / "deploy" / "k8s" / "configmap.yaml").read_text(encoding="utf-8")

    assert 'FEISHU_APPROVAL_CODE: "EF5705C5-0107-4DEE-B9AE-9F5EE6040690"' in content
    assert 'FEISHU_APPROVAL_FORM_MODE: "legacy_fields"' in content
    assert 'FEISHU_APPROVAL_SOURCE_FIELD_ID: "widget17788287542540001"' in content
    assert 'FEISHU_APPROVAL_INCIDENT_ID_FIELD_ID: "widget17788288041580001"' in content
    assert 'FEISHU_APPROVAL_RISK_LEVEL_FIELD_ID: "widget17788288021020001"' in content
    assert 'FEISHU_APPROVAL_COMMAND_FIELD_ID: "widget17788287996940001"' in content
    assert 'FEISHU_APPROVAL_NAMESPACE_FIELD_ID: "widget17788288799990001"' in content
    assert 'FEISHU_APPROVAL_REASON_FIELD_ID: "widget17788289055130001"' in content
    assert "FEISHU_APP_SECRET" not in content
    assert "FEISHU_ENCRYPT_KEY" not in content
