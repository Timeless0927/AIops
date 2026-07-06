"""兼容 `python -m hermes` 启动方式。"""

from __future__ import annotations

import sys
from pathlib import Path


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def main() -> None:
    """启动本仓库自研诊断服务。"""
    from hermes.service_main import main as service_main

    sys.argv[0] = "python -m hermes"
    service_main()


if __name__ == "__main__":
    main()
