"""Alertmanager webhook 独立服务入口。"""

from __future__ import annotations

import argparse
import os
from typing import Sequence

from aiohttp import web

try:
    from hooks.alert_webhook import setup_alert_webhook
except ImportError:  # pragma: no cover - 直接以脚本路径运行时兼容
    from alert_webhook import setup_alert_webhook  # type: ignore


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765


def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="AIOps Alertmanager webhook service")
    parser.add_argument("--host", default=os.getenv("AIOPS_ALERT_WEBHOOK_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("AIOPS_ALERT_WEBHOOK_PORT", str(DEFAULT_PORT))),
    )
    return parser


async def create_app() -> web.Application:
    """创建 aiohttp app 并注册 Alertmanager webhook。"""
    app = web.Application()
    await setup_alert_webhook(app)
    return app


def main(argv: Sequence[str] | None = None) -> None:
    """启动独立 webhook 服务。"""
    args = _build_parser().parse_args(argv)
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
