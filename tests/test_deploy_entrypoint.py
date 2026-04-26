from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_entrypoint_renders_config(tmp_path: Path) -> None:
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


def test_dockerfile_aiops_contains_runtime_dependencies() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text(encoding="utf-8")
    assert "kubectl" in dockerfile
    assert "deploy/entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["/app/deploy/entrypoint.sh"]' in dockerfile
