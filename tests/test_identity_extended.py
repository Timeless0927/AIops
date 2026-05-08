"""测试扩展后的身份与审批模型。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hooks import identity


def _repo_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config.yaml"


def _write_permissions_config(path: Path, open_id: str) -> None:
    path.write_text(
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
  approval_rules:
    - tool: "k8s_exec"
      require_approval_from: "admin"
""",
        encoding="utf-8",
    )


def _operators() -> list[dict]:
    """构造测试专用 operator 列表。"""
    return [
        {
            "name": "管理员",
            "platform": "feishu",
            "platform_user_id": "ou_admin",
            "role": "admin",
            "namespaces": ["*"],
            "allowed_tools": ["k8s_read", "k8s_write", "k8s_exec"],
            "can_approve": True,
        },
        {
            "name": "运维员",
            "platform": "feishu",
            "platform_user_id": "ou_operator",
            "role": "operator",
            "namespaces": ["default", "staging"],
            "allowed_tools": ["k8s_read", "k8s_write"],
            "can_approve": False,
        },
    ]


@pytest.mark.asyncio
async def test_match_operator_returns_namespaces_and_allowed_tools() -> None:
    """匹配到的 operator 应包含命名空间和工具权限。"""
    matched = identity._match_operator(_operators(), "feishu", "ou_operator")

    assert matched is not None
    assert matched["namespaces"] == ["default", "staging"]
    assert matched["allowed_tools"] == ["k8s_read", "k8s_write"]


@pytest.mark.asyncio
async def test_check_permission_scenarios() -> None:
    """权限检查应覆盖工具拒绝、命名空间拒绝与放行。"""
    operator = identity._match_operator(_operators(), "feishu", "ou_operator")
    assert operator is not None

    tool_denied = identity.check_permission(operator, "k8s_exec", "staging")
    namespace_denied = identity.check_permission(operator, "k8s_write", "production")
    allowed = identity.check_permission(operator, "k8s_write", "staging")

    assert tool_denied["allowed"] is False
    assert namespace_denied["allowed"] is False
    assert allowed["allowed"] is True


@pytest.mark.asyncio
async def test_load_approval_rules_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """应能从配置加载审批规则。"""
    monkeypatch.setenv("HERMES_CONFIG", str(_repo_config_path()))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    rules = identity.load_approval_rules()

    assert any(rule.get("tool") == "k8s_exec" for rule in rules)
    assert any(rule.get("namespace") == "staging" and rule.get("auto_approve") for rule in rules)


@pytest.mark.asyncio
async def test_match_approval_rule_logic(monkeypatch: pytest.MonkeyPatch) -> None:
    """审批规则匹配应覆盖工具、命名空间和命令关键字。"""
    monkeypatch.setenv("HERMES_CONFIG", str(_repo_config_path()))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    exec_rule = identity.match_approval_rule("k8s_exec", "default")
    staging_rule = identity.match_approval_rule("k8s_write", "staging", "kubectl apply -f deploy.yaml")
    delete_rule = identity.match_approval_rule("k8s_write", "default", "kubectl delete pod test")
    miss_rule = identity.match_approval_rule("k8s_read", "default", "kubectl get pods")

    assert exec_rule == {"required": True, "approval_from": "admin", "auto_approve": False}
    assert staging_rule == {"required": False, "approval_from": None, "auto_approve": True}
    assert delete_rule == {"required": True, "approval_from": "admin", "auto_approve": False}
    assert miss_rule == {"required": False, "approval_from": None, "auto_approve": False}


@pytest.mark.asyncio
async def test_config_env_override_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """HERMES_CONFIG 应优先于 HERMES_HOME 和 repo fallback。"""
    override_config = tmp_path / "override.yaml"
    _write_permissions_config(override_config, "ou_env_override")
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("sre_permissions: {operators: []}\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_CONFIG", str(override_config))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)

    operators = await identity.load_operators()
    rules = identity.load_approval_rules()

    assert [operator["platform_user_id"] for operator in operators] == ["ou_env_override"]
    assert rules == [{"tool": "k8s_exec", "require_approval_from": "admin"}]


@pytest.mark.asyncio
async def test_config_path_env_override_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """HERMES_CONFIG_PATH 应优先于 HERMES_HOME 和 repo fallback。"""
    override_config = tmp_path / "config-path.yaml"
    _write_permissions_config(override_config, "ou_config_path_override")
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("sre_permissions: {operators: []}\n", encoding="utf-8")

    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(override_config))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    operators = await identity.load_operators()
    rules = identity.load_approval_rules()

    assert identity._config_path() == override_config
    assert [operator["platform_user_id"] for operator in operators] == ["ou_config_path_override"]
    assert rules == [{"tool": "k8s_exec", "require_approval_from": "admin"}]


@pytest.mark.asyncio
async def test_repo_config_fallback_for_dev_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """未配置 runtime 文件时，dev/test 环境回退到 repo config。"""
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    rules = identity.load_approval_rules()

    assert identity._config_path() == _repo_config_path()
    assert any(rule.get("tool") == "k8s_exec" for rule in rules)


@pytest.mark.asyncio
async def test_missing_explicit_config_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """显式配置源缺失时不得回退授权。"""
    missing_config = tmp_path / "missing.yaml"
    monkeypatch.setenv("HERMES_CONFIG", str(missing_config))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    operators = await identity.load_operators()
    rules = identity.load_approval_rules()
    matched = identity.match_approval_rule("k8s_exec", "default")

    assert operators == []
    assert rules == []
    assert matched == {"required": False, "approval_from": None, "auto_approve": False}


@pytest.mark.asyncio
async def test_missing_config_path_env_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """HERMES_CONFIG_PATH 缺失时不得回退 repo config 授权。"""
    missing_config = tmp_path / "missing-config-path.yaml"
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(missing_config))
    monkeypatch.delenv("HERMES_HOME", raising=False)

    operators = await identity.load_operators()
    rules = identity.load_approval_rules()
    matched = identity.match_approval_rule("k8s_exec", "default")

    assert identity._config_path() == missing_config
    assert operators == []
    assert rules == []
    assert matched == {"required": False, "approval_from": None, "auto_approve": False}
