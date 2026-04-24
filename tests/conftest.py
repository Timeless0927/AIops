"""测试公共配置。"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path


# 将仓库根目录加入 sys.path，保证 tests/ 下可以直接导入项目模块。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_configure(config) -> None:
    """注册本项目用到的自定义 marker。"""
    config.addinivalue_line("markers", "asyncio: 运行异步测试用例")


def pytest_pyfunc_call(pyfuncitem) -> bool | None:
    """在未安装 pytest-asyncio 时运行 async 测试函数。"""
    test_func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_func):
        return None

    asyncio.run(test_func(**pyfuncitem.funcargs))
    return True
