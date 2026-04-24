"""测试并发操作锁模块。"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    """按文件路径加载模块，并重定向数据库路径。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "operation_lock.py"
    spec = importlib.util.spec_from_file_location("test_operation_lock_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._DB.close()
    module._DB = module.OperationLockDB(tmp_path / "operation_locks.db")
    return module


@pytest.mark.asyncio
async def test_acquire_lock_success(tmp_path: Path, **_kwargs) -> None:
    """首次获取锁应成功。"""
    module = _load_module(tmp_path)

    assert await module.acquire_lock("cluster/prod", "session-a", 300) is True
    assert await module.is_locked("cluster/prod") is True


@pytest.mark.asyncio
async def test_duplicate_acquire_fails(tmp_path: Path, **_kwargs) -> None:
    """同一资源重复获取锁应失败。"""
    module = _load_module(tmp_path)

    assert await module.acquire_lock("cluster/prod", "session-a", 300) is True
    assert await module.acquire_lock("cluster/prod", "session-b", 300) is False


@pytest.mark.asyncio
async def test_reacquire_after_ttl_expired(tmp_path: Path, **_kwargs) -> None:
    """TTL 过期后应允许重新获取。"""
    module = _load_module(tmp_path)

    assert await module.acquire_lock("cluster/prod", "session-a", 1) is True
    time.sleep(1.1)
    assert await module.acquire_lock("cluster/prod", "session-b", 300) is True


@pytest.mark.asyncio
async def test_reacquire_after_release(tmp_path: Path, **_kwargs) -> None:
    """显式释放后应允许重新获取。"""
    module = _load_module(tmp_path)

    assert await module.acquire_lock("cluster/prod", "session-a", 300) is True
    assert await module.release_lock("cluster/prod", "session-a") is True
    assert await module.acquire_lock("cluster/prod", "session-b", 300) is True


@pytest.mark.asyncio
async def test_cleanup_expired_removes_stale_locks(tmp_path: Path, **_kwargs) -> None:
    """cleanup_expired 应删除过期锁。"""
    module = _load_module(tmp_path)

    assert await module.acquire_lock("cluster/prod", "session-a", 1) is True
    time.sleep(1.1)
    result = await module.cleanup_expired()

    assert result["ok"] is True
    assert result["deleted"] == 1
    assert await module.is_locked("cluster/prod") is False
