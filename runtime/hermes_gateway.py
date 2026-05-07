"""Start Hermes gateway with AIOps runtime overlays installed."""

from __future__ import annotations

import sys
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> None:
    root = _project_root()
    hermes_src = root / "hermes-agent"
    if hermes_src.exists():
        sys.path.insert(0, str(hermes_src))

    from runtime.feishu_approval_overlay import install

    install()

    from hermes_cli.gateway import run_gateway

    run_gateway(replace=True)


if __name__ == "__main__":
    main()
