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
    assert "callback_path:" in content
    assert "/webhooks/feishu/approval" in content
    assert "polling_interval_seconds:" in content


def test_default_config_declares_feishu_approval_callback_and_polling_interval() -> None:
    _assert_feishu_approval_callback_and_polling_interval(_project_root() / "config.yaml")


def test_deploy_template_declares_feishu_approval_callback_and_polling_interval() -> None:
    _assert_feishu_approval_callback_and_polling_interval(
        _project_root() / "deploy" / "hermes-config.template.yaml"
    )
