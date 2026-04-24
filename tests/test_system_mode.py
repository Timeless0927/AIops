"""测试系统运行模式存储。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "system_mode.py"
    spec = importlib.util.spec_from_file_location("test_system_mode_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._DB.close()
    module._DB = module.SystemModeDB(tmp_path / "system_mode.db")
    return module


@pytest.mark.asyncio
async def test_system_mode_defaults_and_updates(tmp_path: Path, **_: object) -> None:
    module = _load_module(tmp_path)

    default = await module.get_system_mode()
    await module.set_system_mode("read_only", "database unavailable")
    updated = await module.get_system_mode()

    assert default["mode"] == "normal"
    assert updated["mode"] == "read_only"
    assert updated["reason"] == "database unavailable"


@pytest.mark.asyncio
async def test_invalid_system_mode_is_rejected(tmp_path: Path, **_: object) -> None:
    module = _load_module(tmp_path)

    with pytest.raises(ValueError, match="不支持的 system_mode"):
        await module.set_system_mode("maintenance", "bad")
