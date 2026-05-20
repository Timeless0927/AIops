from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _v2_approval_env() -> dict[str, str]:
    return {
        "FEISHU_APPROVAL_CODE": "1D7CF6FF-2647-4A90-9FEE-D74C92D1D985",
        "FEISHU_APPROVAL_REQUESTER_OPEN_ID": "ou_requester",
        "FEISHU_APPROVAL_SUMMARY_FIELD_ID": "widget17792695890",
        "FEISHU_APPROVAL_DETAIL_FIELD_ID": "widget17792695891",
        "FEISHU_APPROVAL_APPROVER_NODE_KEY": "APPROVAL_1",
        "FEISHU_APPROVAL_APPROVER_OPEN_ID": "ou_approver",
    }


def test_entrypoint_renders_config(tmp_path: Path) -> None:
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    log_path = tmp_path / "invocations.log"

    for name in ("python3", "hermes", "kubectl"):
        script = wrapper_dir / name
        if name == "python3":
            script.write_text(
                "#!/usr/bin/env bash\n"
                f"echo \"python3:$*\" >> {log_path}\n"
                "if [[ \"$1\" == \"-\" ]]; then exec /usr/bin/python3 \"$@\"; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
        else:
            script.write_text(
                "#!/usr/bin/env bash\n"
                f"echo \"{name}:$*\" >> {log_path}\n"
                "exit 0\n",
                encoding="utf-8",
            )
        script.chmod(0o755)

    env = os.environ.copy()
    env.pop("FEISHU_GROUP_POLICY", None)
    env.pop("FEISHU_ALLOWED_USERS", None)
    env.update(
        {
            "HOME": str(tmp_path),
            "HERMES_HOME": str(tmp_path / "data" / "hermes"),
            "HERMES_CONFIG": str(tmp_path / "data" / "hermes" / "config.yaml"),
            "AIOPS_DATA_DIR": str(tmp_path / "data" / "aiops"),
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_MAIN_CHAT_ID": "oc_main",
            "AIOPS_MODEL_PROVIDER": "custom",
            "AIOPS_MODEL_BASE_URL": "http://model.local/v1",
            "AIOPS_MODEL_API_KEY": "token",
            "AIOPS_SRE_ADMIN_NAME": "管理员",
            "AIOPS_SRE_ADMIN_OPEN_ID": "ou_admin",
            "AIOPS_SRE_OPERATOR_NAME": "运维员",
            "AIOPS_SRE_OPERATOR_OPEN_ID": "ou_operator",
            "AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK": "false",
            "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC": "true",
            "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS": "true",
            "AIOPS_WEBHOOK_ONLY": "1",
            "PATH": f"{wrapper_dir}:{env['PATH']}",
            **_v2_approval_env(),
        }
    )

    subprocess.run(
        ["bash", "deploy/entrypoint.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        timeout=10,
    )

    config_text = Path(env["HERMES_CONFIG"]).read_text(encoding="utf-8")
    assert 'main_chat_id: "oc_main"' in config_text
    assert 'base_url: "http://model.local/v1"' in config_text
    assert 'approval_code: "1D7CF6FF-2647-4A90-9FEE-D74C92D1D985"' in config_text
    assert 'requester_open_id: "ou_requester"' in config_text
    assert 'mode: "summary_detail"' in config_text
    assert 'id: "widget17792695890"' in config_text
    assert 'type: "input"' in config_text
    assert 'id: "widget17792695891"' in config_text
    assert 'type: "textarea"' in config_text
    assert 'approver_node_key: "APPROVAL_1"' in config_text
    assert '- "ou_approver"' in config_text
    assert "sre_permissions:" in config_text
    assert 'platform_user_id: "ou_admin"' in config_text
    assert 'platform_user_id: "ou_operator"' in config_text
    assert "approval_policy:" in config_text
    assert "allow_self_approval_low_risk: false" in config_text
    assert "require_admin_for_exec: true" in config_text
    assert "require_admin_for_dangerous: true" in config_text
    assert 'default_group_policy: "open"' in config_text
    assert "toolsets:" in config_text
    invocations = log_path.read_text(encoding="utf-8")
    assert "python3:-m hooks.alert_webhook_server" in invocations


def test_dockerfile_aiops_contains_runtime_dependencies() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text(encoding="utf-8")
    assert "kubectl" in dockerfile
    assert "deploy/entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint.sh"]' in dockerfile
    assert 'pip install "hermes-agent[messaging,feishu] @ file:///tmp/hermes-agent"' in dockerfile
    assert "HERMES_HOME=/data/hermes" in dockerfile
    assert "HERMES_CONFIG=/data/hermes/config.yaml" in dockerfile
    assert "AIOPS_DATA_DIR=/data/aiops" in dockerfile


def test_entrypoint_normal_mode_starts_gateway_wrapper(tmp_path: Path) -> None:
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    log_path = tmp_path / "invocations.log"

    for name in ("python3", "hermes", "kubectl"):
        script = wrapper_dir / name
        script.write_text(
            "#!/usr/bin/env bash\n"
            f"echo \"{name}:$*\" >> {log_path}\n"
            "if [[ \"$1 $2\" == \"-m hooks.alert_webhook_server\" ]]; then sleep 0.2; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "HERMES_HOME": str(tmp_path / "data" / "hermes"),
            "HERMES_CONFIG": str(tmp_path / "data" / "hermes" / "config.yaml"),
            "AIOPS_DATA_DIR": str(tmp_path / "data" / "aiops"),
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_MAIN_CHAT_ID": "oc_main",
            "AIOPS_MODEL_PROVIDER": "custom",
            "AIOPS_MODEL_BASE_URL": "http://model.local/v1",
            "AIOPS_MODEL_API_KEY": "token",
            "AIOPS_SRE_ADMIN_NAME": "管理员",
            "AIOPS_SRE_ADMIN_OPEN_ID": "ou_admin",
            "AIOPS_SRE_OPERATOR_NAME": "运维员",
            "AIOPS_SRE_OPERATOR_OPEN_ID": "ou_operator",
            "AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK": "false",
            "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC": "true",
            "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS": "true",
            "PATH": f"{wrapper_dir}:{env['PATH']}",
            **_v2_approval_env(),
        }
    )

    subprocess.run(
        ["bash", "deploy/entrypoint.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        timeout=10,
    )

    invocations = log_path.read_text(encoding="utf-8")
    assert "python3:-m hooks.alert_webhook_server" in invocations
    assert "python3:-m runtime.hermes_gateway" in invocations
    assert "hermes:" not in invocations


def test_entrypoint_fails_when_required_binary_missing(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "HERMES_HOME": str(tmp_path / "data" / "hermes"),
            "HERMES_CONFIG": str(tmp_path / "data" / "hermes" / "config.yaml"),
            "AIOPS_DATA_DIR": str(tmp_path / "data" / "aiops"),
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_MAIN_CHAT_ID": "oc_main",
            "AIOPS_MODEL_PROVIDER": "custom",
            "AIOPS_MODEL_BASE_URL": "http://model.local/v1",
            "AIOPS_MODEL_API_KEY": "token",
            "AIOPS_SRE_ADMIN_NAME": "管理员",
            "AIOPS_SRE_ADMIN_OPEN_ID": "ou_admin",
            "AIOPS_SRE_OPERATOR_NAME": "运维员",
            "AIOPS_SRE_OPERATOR_OPEN_ID": "ou_operator",
            "AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK": "false",
            "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC": "true",
            "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS": "true",
            "PATH": "/usr/bin:/bin",
        }
    )

    result = subprocess.run(
        ["bash", "deploy/entrypoint.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "missing required binary" in result.stderr


def test_entrypoint_webhook_only_attempts_webhook_start(tmp_path: Path) -> None:
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    log_path = tmp_path / "invocations.log"

    for name in ("python3", "hermes", "kubectl"):
        script = wrapper_dir / name
        if name == "python3":
            script.write_text(
                "#!/usr/bin/env bash\n"
                f"echo \"python3:$*\" >> {log_path}\n"
                "if [[ \"$1\" == \"-\" ]]; then exec /usr/bin/python3 \"$@\"; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
        else:
            script.write_text(
                "#!/usr/bin/env bash\n"
                f"echo \"{name}:$*\" >> {log_path}\n"
                "exit 0\n",
                encoding="utf-8",
            )
        script.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "HERMES_HOME": str(tmp_path / "data" / "hermes"),
            "HERMES_CONFIG": str(tmp_path / "data" / "hermes" / "config.yaml"),
            "AIOPS_DATA_DIR": str(tmp_path / "data" / "aiops"),
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_MAIN_CHAT_ID": "oc_main",
            "AIOPS_MODEL_PROVIDER": "custom",
            "AIOPS_MODEL_BASE_URL": "http://model.local/v1",
            "AIOPS_MODEL_API_KEY": "token",
            "AIOPS_SRE_ADMIN_NAME": "管理员",
            "AIOPS_SRE_ADMIN_OPEN_ID": "ou_admin",
            "AIOPS_SRE_OPERATOR_NAME": "运维员",
            "AIOPS_SRE_OPERATOR_OPEN_ID": "ou_operator",
            "AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK": "false",
            "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC": "true",
            "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS": "true",
            "AIOPS_WEBHOOK_ONLY": "1",
            "PATH": f"{wrapper_dir}:{env['PATH']}",
            **_v2_approval_env(),
        }
    )

    subprocess.run(
        ["bash", "deploy/entrypoint.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        timeout=10,
    )

    invocations = log_path.read_text(encoding="utf-8")
    assert "python3:-" in invocations
    assert "python3:-m hooks.alert_webhook_server" in invocations
    assert "runtime.hermes_gateway" not in invocations
    assert "hermes:" not in invocations
