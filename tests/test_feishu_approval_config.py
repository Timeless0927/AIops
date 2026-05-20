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
    assert "mode: \"summary_detail\"" in content
    assert "FEISHU_APPROVAL_SUMMARY_FIELD_ID" in content
    assert "FEISHU_APPROVAL_DETAIL_FIELD_ID" in content
    assert "FEISHU_APPROVAL_APPROVER_NODE_KEY" in content
    assert "FEISHU_APPROVAL_APPROVER_OPEN_ID" in content
    assert "callback_path:" in content
    assert "/webhooks/feishu/approval" in content
    assert "polling_interval_seconds:" in content


def test_deploy_template_declares_feishu_approval_callback_and_polling_interval() -> None:
    _assert_feishu_approval_callback_and_polling_interval(
        _project_root() / "deploy" / "hermes-config.template.yaml"
    )
