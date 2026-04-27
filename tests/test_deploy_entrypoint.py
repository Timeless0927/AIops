from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


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
    env.update(
        {
            "HOME": str(tmp_path),
            "AIOPS_DATA_DIR": str(tmp_path / "data"),
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_MAIN_CHAT_ID": "oc_main",
            "AIOPS_MODEL_PROVIDER": "custom",
            "AIOPS_MODEL_BASE_URL": "http://model.local/v1",
            "AIOPS_MODEL_API_KEY": "token",
            "AIOPS_WEBHOOK_ONLY": "1",
            "PATH": f"{wrapper_dir}:{env['PATH']}",
        }
    )

    subprocess.run(
        ["bash", "deploy/entrypoint.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        timeout=10,
    )

    config_text = (tmp_path / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert 'main_chat_id: "oc_main"' in config_text
    assert 'base_url: "http://model.local/v1"' in config_text
    assert "toolsets:" in config_text
    invocations = log_path.read_text(encoding="utf-8")
    assert "python3:-m hooks.alert_webhook_server" in invocations


def test_dockerfile_aiops_contains_runtime_dependencies() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text(encoding="utf-8")
    assert "kubectl" in dockerfile
    assert "deploy/entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint.sh"]' in dockerfile
    assert 'pip install "/tmp/hermes-agent[messaging,feishu]"' not in dockerfile
    assert 'pip install "/tmp/hermes-agent[messaging,feishu]/."' in dockerfile


def test_entrypoint_fails_when_required_binary_missing(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "AIOPS_DATA_DIR": str(tmp_path / "data"),
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_MAIN_CHAT_ID": "oc_main",
            "AIOPS_MODEL_PROVIDER": "custom",
            "AIOPS_MODEL_BASE_URL": "http://model.local/v1",
            "AIOPS_MODEL_API_KEY": "token",
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
            "AIOPS_DATA_DIR": str(tmp_path / "data"),
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_MAIN_CHAT_ID": "oc_main",
            "AIOPS_MODEL_PROVIDER": "custom",
            "AIOPS_MODEL_BASE_URL": "http://model.local/v1",
            "AIOPS_MODEL_API_KEY": "token",
            "AIOPS_WEBHOOK_ONLY": "1",
            "PATH": f"{wrapper_dir}:{env['PATH']}",
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
    assert "hermes:" not in invocations
