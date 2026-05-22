"""Alertmanager webhook 独立服务入口。"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
from typing import Sequence

from aiohttp import web

try:
    from hooks.alert_webhook import setup_alert_webhook
except ImportError:  # pragma: no cover - 直接以脚本路径运行时兼容
    from alert_webhook import setup_alert_webhook  # type: ignore

try:
    from hooks.feishu_approval_event import setup_feishu_approval_webhook
except ImportError:  # pragma: no cover - 直接以脚本路径运行时兼容
    from feishu_approval_event import setup_feishu_approval_webhook  # type: ignore

try:
    from hooks import recovery
except ImportError:  # pragma: no cover - 直接以脚本路径运行时兼容
    import recovery  # type: ignore


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
_POLLING_TASK_KEY = web.AppKey("feishu_approval_polling_task", asyncio.Task)
_LOGGER = logging.getLogger(__name__)


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


def _polling_config(config: dict | None) -> dict:
    approval = recovery._polling_config(config)  # type: ignore[attr-defined]
    return approval if isinstance(approval, dict) else {}


def _polling_interval_seconds(config: dict | None) -> int:
    polling = _polling_config(config)
    try:
        return max(1, int(polling.get("polling_interval_seconds") or polling.get("interval_seconds") or 60))
    except (TypeError, ValueError):
        return 60


async def _run_feishu_approval_polling_loop(config: dict | None) -> None:
    """持续补偿同步 webhook 遗失的飞书原生审批状态。"""
    interval_seconds = _polling_interval_seconds(config)
    while True:
        try:
            result = await recovery.poll_external_pending_approvals(config=config)
            if result.get("enabled") is False:
                return
            if result.get("scanned") or result.get("synced") or result.get("failed"):
                _LOGGER.info("feishu approval polling tick: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("feishu approval polling tick failed")
        await asyncio.sleep(interval_seconds)


async def _feishu_approval_polling_lifecycle(app: web.Application):
    config = app.get("alert_webhook_config")
    if not isinstance(config, dict):
        config = None
    if recovery._polling_enabled(config):  # type: ignore[attr-defined]
        app[_POLLING_TASK_KEY] = asyncio.create_task(
            _run_feishu_approval_polling_loop(config),
            name="aiops-feishu-approval-polling",
        )
    yield
    task = app.get(_POLLING_TASK_KEY)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def create_app() -> web.Application:
    """创建 aiohttp app 并注册 webhook 路由。"""
    app = web.Application()
    await setup_alert_webhook(app)
    config = app.get("alert_webhook_config")
    setup_result = setup_feishu_approval_webhook(
        app,
        config=config if isinstance(config, dict) else None,
    )
    if inspect.isawaitable(setup_result):
        await setup_result
    app.cleanup_ctx.append(_feishu_approval_polling_lifecycle)
    return app


def main(argv: Sequence[str] | None = None) -> None:
    """启动独立 webhook 服务。"""
    args = _build_parser().parse_args(argv)
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
