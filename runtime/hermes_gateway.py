"""Start Hermes gateway with AIOps runtime overlays installed."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DISABLED_VALUES = {"0", "false", "no", "off"}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _approval_execution_worker_enabled() -> bool:
    raw = os.getenv("AIOPS_APPROVAL_EXECUTION_WORKER_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES


def main() -> None:
    root = _project_root()
    hermes_src = root / "hermes-agent"
    if hermes_src.exists():
        sys.path.insert(0, str(hermes_src))

    from runtime.feishu_approval_overlay import install

    install()
    worker = None
    if _approval_execution_worker_enabled():
        try:
            from runtime.approval_execution_worker import start_approval_execution_worker

            worker = start_approval_execution_worker()
        except Exception:
            logger.exception("approval execution worker failed to start; gateway continuing")

    try:
        from hermes_cli.gateway import run_gateway

        run_gateway(replace=True)
    finally:
        if worker is not None:
            worker.stop()


if __name__ == "__main__":
    main()
