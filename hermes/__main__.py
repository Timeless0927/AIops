"""兼容 `python -m hermes` 启动方式。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    """构建兼容层参数解析器。"""
    parser = argparse.ArgumentParser(description="AIOps SRE Agent Hermes 兼容入口")
    parser.add_argument("--mode", choices=["cli", "gateway"], default="cli")
    parser.add_argument("-q", "--query", help="CLI 模式下的一次性提问")
    parser.add_argument("--provider", help="覆盖默认 provider")
    parser.add_argument("--model", help="覆盖默认模型")
    parser.add_argument("--toolsets", help="覆盖默认 toolsets，逗号分隔")
    parser.add_argument("remainder", nargs=argparse.REMAINDER, help="透传给 Hermes 原生 CLI 的参数")
    return parser


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def main() -> None:
    """将兼容参数转换为 Hermes 原生命令。"""
    parser = _build_parser()
    args = parser.parse_args()

    root = _project_root()
    hermes_src = root / "hermes-agent"
    if hermes_src.exists():
        sys.path.insert(0, str(hermes_src))

    from hermes_cli.main import main as hermes_main

    forwarded = list(args.remainder)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]

    if args.mode == "cli":
        argv = ["chat"]
        if args.query:
            argv.extend(["-q", args.query])
    else:
        argv = ["gateway", "run"]

    if args.provider:
        argv.extend(["--provider", args.provider])
    if args.model:
        argv.extend(["--model", args.model])
    if args.toolsets:
        argv.extend(["--toolsets", args.toolsets])

    argv.extend(forwarded)
    sys.argv = ["python -m hermes", *argv]
    hermes_main()


if __name__ == "__main__":
    main()
